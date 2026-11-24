"""Local HTTP surface for the Skill-guided agent and its streaming search tool.

X-Tenant-ID is only a local data-partition header, never authentication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, Literal
from weakref import WeakValueDictionary

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import Field

from .account_api import (
    WORKSPACE_HEADER,
    build_account_router,
    check_workspace_account,
    require_account,
    validate_account_transport,
)
from .account_models import AccountModels
from .accounts import (
    ACCOUNT_COOKIE,
    Account,
    AccountError,
    AccountStore,
    account_session_error,
    is_account_tenant,
)
from .agent import (
    AgentCapacityError,
    AgentPurgeBusy,
    AgentRunCreate,
    AgentRunner,
    AgentUnavailableError,
    LocalSearchUnavailableError,
)
from .auth import (
    COOKIE_NAME,
    SESSION_TTL_S,
    AuthManager,
    auth_error,
    authenticate_request,
)
from .citation_texts import CitationTextError, CitationTextRequest, CitationTextService
from .config import PROJECT_ROOT, Settings
from .entities import (
    AgentRun,
    SearchCreate,
    SnapshotCreate,
    StrictModel,
)
from .history_api import build_history_router
from .learning import LearningLoop
from .learning_store import LearningStore
from .learning_web import DeepSeekWebSearch, search_key
from .listwise import DiscoveryModels
from .manuscript_api import build_manuscript_router, http_resource_path
from .manuscript_scan import ClamAVScanner, ManuscriptScanner, scan_capability
from .manuscripts import ManuscriptError, ManuscriptLimits, ManuscriptService
from .mcp import PROTOCOL_VERSION, McpHandler
from .model_egress import MODEL_PROTOCOLS
from .project_api import build_project_router
from .project_connectors import CONNECTORS, ProjectMetadataAdapter
from .projects import ProjectStore
from .reasons import (
    MODEL_PROVIDERS,
    DeepSeekModelGateway,
    DeepSeekReasonGenerator,
    ReasonGenerator,
    UnavailableReasonGenerator,
    UserModelSelection,
    load_model_config,
)
from .retrieval import LibraryAdapter, build_fixture_adapters
from .runtime import build_metadata_adapters, settings_from_env
from .search import SearchRequestError, SearchService, library_infos
from .store import (
    IdempotencyConflict,
    Store,
    StoreConflict,
)

logger = logging.getLogger(__name__)
API_VERSION = "0.4.0-accounts-m0"
SCHEMA_VERSION = "2.0.0"
TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}


class ConversationTurnCreate(StrictModel):
    message: str = Field(min_length=1, max_length=8000)
    active_query_id: str | None = Field(default=None, max_length=80)
    library_ids: list[str] | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    model: UserModelSelection | None = Field(default=None, exclude=True)
    prepare_citations: bool = False


class CitationTextCreate(CitationTextRequest):
    model: UserModelSelection | None = Field(default=None, exclude=True)

    def citation_request(self) -> CitationTextRequest:
        return CitationTextRequest.model_validate(self.model_dump(exclude={"model"}))


def _user_model_config(selection: UserModelSelection | None, settings: Settings):
    if selection is None:
        return None
    if not settings.allow_user_models:
        raise ApiError(403, "USER_MODEL_DISABLED", "服务端未开放自有模型。")
    try:
        return selection.to_model_config()
    except ValueError as exc:
        raise ApiError(400, "MODEL_CONFIG_INVALID", str(exc)) from None


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict | None = None,
        retryable: bool = False,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tenant(request: Request, settings: Settings) -> str:
    account = getattr(request.state, "account", None)
    if account is not None:
        return account.tenant_id
    if settings.auth_required:
        tenant = getattr(request.state, "tenant", None)
        if tenant is None or is_account_tenant(tenant):
            raise ApiError(401, "AUTH_REQUIRED", "需要有效的访问令牌或登录会话。")
        return tenant
    value = request.headers.get("X-Tenant-ID", settings.default_tenant).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
        raise ApiError(400, "VALIDATION_ERROR", "X-Tenant-ID 格式不合法。")
    return value


def _route_path(request: Request) -> str:
    path = request.scope["path"]
    root = request.scope.get("root_path", "").rstrip("/")
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root) :] or "/"
    return path


def _request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied):
        return supplied
    return f"req_{uuid.uuid4().hex}"


def _sse_envelope(event: dict) -> str:
    return _compact(
        {
            "schema_version": SCHEMA_VERSION,
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "query_id": event["query_id"],
            "sequence": event["sequence"],
            "occurred_at": event["occurred_at"],
            "producer": "coordinator",
            "payload": event["payload"],
        }
    )


def create_app(
    settings: Settings | None = None,
    adapters: Mapping[str, LibraryAdapter] | None = None,
    reason_generator: ReasonGenerator | None = None,
    agent_model: BaseChatModel | None = None,
    discovery_models: DiscoveryModels | None = None,
    citation_model: BaseChatModel | None = None,
    citation_review_model: BaseChatModel | None = None,
    citation_resolver=None,
    manuscript_scanner: ManuscriptScanner | None = None,
) -> FastAPI:
    settings = settings or settings_from_env()
    settings.validate()
    if adapters is None:
        adapters = (
            build_fixture_adapters(settings)
            if settings.library_mode == "fixture"
            else build_metadata_adapters(settings, require_prebuilt=True)
        )
    adapters = dict(adapters)
    settings = replace(
        settings,
        libraries=tuple(
            replace(library, record_count=adapters[library.library_id].record_count)
            if library.library_id in adapters
            and hasattr(adapters[library.library_id], "record_count")
            else library
            for library in settings.libraries
        ),
    )
    missing = [
        library.library_id for library in settings.libraries if library.library_id not in adapters
    ]
    if missing:
        raise ValueError(f"missing adapters for configured libraries: {missing}")

    model_config = load_model_config(settings.model_config_path)
    gateway = (
        DeepSeekModelGateway(
            model_config,
            max_inflight=settings.model_max_inflight,
            timeout_ms=settings.model_timeout_ms,
        )
        if model_config
        else None
    )
    personal_workspace = settings.accounts_enabled and not settings.auth_required
    connectors = {
        key: recipe
        for key, recipe in CONNECTORS.items()
        if personal_workspace and key not in adapters
    }
    project_adapters = {key: ProjectMetadataAdapter(recipe) for key, recipe in connectors.items()}
    store = Store(
        settings.db_path, learning_enabled=personal_workspace and settings.learning_enabled
    )
    projects = (
        ProjectStore(
            store,
            tuple(item.library_id for item in settings.libraries),
            {key: recipe.version for key, recipe in connectors.items()},
        )
        if personal_workspace
        else None
    )
    manuscripts = ManuscriptService(
        settings.db_path,
        settings.db_path.parent / "private-manuscripts",
        limits=ManuscriptLimits(
            max_upload_bytes=settings.manuscript_max_upload_bytes,
            max_pages=settings.manuscript_max_pages,
        ),
        parse_concurrency=settings.manuscript_max_concurrent,
        parse_timeout_s=settings.manuscript_parse_timeout_s,
    )
    if manuscript_scanner is None and settings.manuscript_scan_required:
        manuscript_scanner = ClamAVScanner(
            settings.manuscript_scan_socket, timeout_s=settings.manuscript_scan_timeout_s
        )
    reasons = reason_generator or (
        DeepSeekReasonGenerator(
            gateway,
            max_inflight=settings.model_max_inflight,
            timeout_ms=settings.model_timeout_ms,
            cache=store,
        )
        if gateway
        else UnavailableReasonGenerator()
    )
    reason_provider = (
        gateway.config.provider
        if gateway and reason_generator is None
        else getattr(reason_generator, "provider", None)
        if reason_generator is not None
        else None
    )
    service = SearchService(
        store,
        settings,
        {**adapters, **project_adapters},
        reasons,
        reason_provider=reason_provider,
        discovery_models=discovery_models,
        manuscripts=manuscripts,
        projects=projects,
    )
    agent = AgentRunner(
        store,
        service,
        settings,
        agent_model or (gateway.agent_model() if gateway else None),
        provider=gateway.config.provider if gateway else reason_provider or "test",
        model_name=(
            gateway.config.model
            if gateway
            else str(getattr(agent_model, "model_name", "test-scripted-agent"))
        ),
    )
    citations = CitationTextService(
        store,
        service,
        gateway=getattr(reasons, "gateway", None) or gateway,
        model=citation_model,
        review_model=citation_review_model,
        resolver=citation_resolver,
        max_concurrent=settings.citation_text_max_concurrent,
        timeout_ms=min(120_000, max(30_000, settings.model_timeout_ms)),
        max_output_tokens=1800,
    )

    learning = None
    if (
        projects is not None
        and connectors
        and settings.learning_enabled
        and agent.model is not None
    ):
        key = search_key()
        learning = LearningLoop(
            LearningStore(store, connectors),
            projects,
            project_adapters,
            agent.model,
            DeepSeekWebSearch(key) if key else None,
        )
    store.learning_enabled = learning is not None

    @asynccontextmanager
    async def lifespan(application):
        worker = None
        try:
            await asyncio.to_thread(store.settle_interrupted_work)
            worker = asyncio.create_task(learning.run()) if learning is not None else None
            yield
        finally:
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await citations.aclose()
            await asyncio.to_thread(manuscripts.close)

    app = FastAPI(
        title="Citation Recommendation Agent M0",
        version=API_VERSION,
        root_path=settings.base_path,
        lifespan=lifespan,
    )
    app.state.manuscripts = manuscripts
    app.state.manuscript_scanner = manuscript_scanner
    app.state.citation_texts = citations
    app.state.settings = settings
    app.state.store = store
    app.state.search_service = service
    app.state.coordinator = service.coordinator
    app.state.adapters = adapters
    app.state.agent_runner = agent
    app.state.auth = (
        AuthManager(
            settings.auth_db_path,
            tenant_id=settings.default_tenant,
            token_sha256=os.environ.get("CITATION_ACCESS_TOKEN_SHA256", ""),
        )
        if settings.auth_required
        else None
    )
    app.state.mcp = McpHandler(service, store)
    accounts = (
        AccountStore(settings.db_path)
        if settings.accounts_enabled and not settings.auth_required
        else None
    )
    app.state.accounts = accounts
    account_models = (
        AccountModels(settings.db_path, settings.account_model_key_file)
        if accounts is not None and settings.allow_user_models
        else None
    )

    async def request_model_config(request: Request, selection: UserModelSelection | None):
        if selection is not None:
            return _user_model_config(selection, settings)
        account = getattr(request.state, "account", None)
        if account is not None and account_models is not None:
            return await asyncio.to_thread(account_models.resolve, account.account_id)
        return None

    mutation_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
    if settings.auth_required:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    async def delete_account_data(account: Account) -> None:
        scope = await asyncio.to_thread(store.account_purge_scope, account.tenant_id)
        if agent.has_live_runs(scope["run_ids"]) or service.coordinator.has_live_searches(
            scope["query_ids"]
        ):
            raise AccountError(
                409, "ACCOUNT_HAS_ACTIVE_WORK", "还有检索正在运行。请先停止或等待完成。"
            )
        await citations.purge_queries(account.tenant_id, set(scope["query_ids"]))
        for manuscript_id in scope["manuscript_ids"]:
            await asyncio.to_thread(manuscripts.delete, account.tenant_id, manuscript_id)
        await asyncio.to_thread(store.purge_account_rows, account.account_id)

    if accounts is not None:
        app.include_router(
            build_account_router(
                accounts,
                cookie_path=settings.base_path or "/",
                secure=settings.secure_cookies,
                delete_data=delete_account_data,
                models=account_models,
            )
        )
        app.include_router(build_history_router(store, agent, citations))
        assert projects is not None
        app.include_router(build_project_router(projects, service, learning))
    app.state.learning = learning

    @app.middleware("http")
    async def authentication_middleware(request: Request, call_next):
        path = _route_path(request)
        if settings.auth_required:
            public = path in {"/", "/healthz", "/login"} or path.startswith(
                ("/js/", "/styles.css", "/workspace.css", "/project-workspace.css", "/favicon")
            )
            tenant = await asyncio.to_thread(authenticate_request, request, settings)
            if tenant is None and not public:
                return auth_error()
            request.state.tenant = tenant
            return await call_next(request)
        if not (path.startswith("/api/v1/") or path == "/mcp"):
            return await call_next(request)
        if accounts is None:
            if path != "/api/v1/libraries" and request.headers.get(WORKSPACE_HEADER) not in (
                None,
                "guest",
            ):
                return await resource_error_handler(request, account_session_error())
            return await call_next(request)
        raw_session = request.cookies.get(ACCOUNT_COOKIE, "")
        try:
            account = None
            if raw_session:
                validate_account_transport(request)
                account = await asyncio.to_thread(accounts.session_account, raw_session)
            request.state.account = account
            request.state.stale_account = bool(raw_session and account is None)
            if path in {
                "/api/v1/account/session",
                "/api/v1/account/register",
                "/api/v1/account/login",
                "/api/v1/account/recover",
                "/api/v1/libraries",
            }:
                return await call_next(request)
            check_workspace_account(request)
            if account is not None and request.method not in {"GET", "HEAD", "OPTIONS"}:
                lock = mutation_locks.setdefault(account.account_id, asyncio.Lock())
                async with lock:
                    if await asyncio.to_thread(accounts.session_account, raw_session) is None:
                        raise account_session_error()
                    return await call_next(request)
        except AccountError as exc:
            return await resource_error_handler(request, exc)
        return await call_next(request)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request.state.request_id = _request_id(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        path = _route_path(request)
        if path == "/" or path.startswith(
            ("/js/", "/styles.css", "/workspace.css", "/project-workspace.css")
        ):
            response.headers["Cache-Control"] = "no-cache"
        if path.startswith(("/api/", "/mcp", "/login", "/logout")):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": exc.code.replace("_", " ").title(),
                "status": exc.status,
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "request_id": getattr(request.state, "request_id", "unknown"),
                "details": exc.details,
            },
        )

    @app.exception_handler(AccountError)
    @app.exception_handler(AgentPurgeBusy)
    @app.exception_handler(ManuscriptError)
    @app.exception_handler(CitationTextError)
    @app.exception_handler(StoreConflict)
    async def resource_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return await api_error_handler(
            request,
            ApiError(
                getattr(exc, "status", getattr(exc, "http_status", 409)),
                getattr(exc, "code", "RESOURCE_CONFLICT"),
                getattr(exc, "message", "资源不存在、版本已变化或当前状态不允许此操作。"),
                getattr(exc, "details", None),
                retryable=getattr(exc, "retryable", False),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = {
            "errors": [
                {
                    "loc": list(error.get("loc", [])),
                    "msg": error.get("msg", ""),
                    "type": error.get("type", ""),
                }
                for error in exc.errors()
            ]
        }
        return JSONResponse(
            status_code=400,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "Validation Error",
                "status": 400,
                "code": "VALIDATION_ERROR",
                "message": "请求字段或组合不合法。",
                "retryable": False,
                "request_id": getattr(request.state, "request_id", "unknown"),
                "details": details,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled API error request_id=%s type=%s",
            getattr(request.state, "request_id", "unknown"),
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            media_type="application/problem+json",
            content={
                "type": "about:blank",
                "title": "Internal Error",
                "status": 500,
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误；未输出私有正文或堆栈。",
                "retryable": True,
                "request_id": getattr(request.state, "request_id", "unknown"),
                "details": {},
            },
        )

    @app.post("/login")
    async def login(request: Request):
        if not settings.auth_required:
            raise ApiError(404, "NOT_FOUND", "本地开发模式未启用登录。")
        body = await request.body()
        if len(body) > 4096:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "登录请求过大。")
        try:
            token = json.loads(body).get("token", "")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise ApiError(400, "VALIDATION_ERROR", "登录请求必须是 JSON 对象。") from exc
        if not isinstance(token, str):
            raise ApiError(400, "VALIDATION_ERROR", "token 必须是字符串。")
        key = request.client.host if request.client else "unknown"
        if not app.state.auth.login_allowed(key):
            return JSONResponse(
                status_code=429,
                content={"code": "LOGIN_RATE_LIMITED", "message": "尝试过于频繁，请稍后重试。"},
                headers={"Retry-After": "60", "Cache-Control": "no-store"},
            )
        created = app.state.auth.create_session(token)
        if created is None:
            return auth_error()
        session, tenant = created
        response = JSONResponse({"tenant_id": tenant})
        response.set_cookie(
            COOKIE_NAME,
            session,
            max_age=SESSION_TTL_S,
            path=settings.base_path or "/",
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        if settings.auth_required:
            app.state.auth.revoke_session(request.cookies.get(COOKIE_NAME, ""))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path=settings.base_path or "/")
        return response

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        length = request.headers.get("Content-Length")
        try:
            oversized = bool(length and int(length) > 1_000_000)
        except ValueError as exc:
            raise ApiError(400, "VALIDATION_ERROR", "Content-Length 不合法。") from exc
        if oversized:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "MCP 请求超过 1MB 上限。")
        try:
            message = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                },
            )
        if not isinstance(message, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Expected one JSON-RPC object"},
                },
            )
        result = await app.state.mcp.handle(_tenant(request, settings), message)
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION, "Cache-Control": "no-store"}
        if result is None:
            return Response(status_code=202, headers=headers)
        return JSONResponse(result, headers=headers)

    @app.get("/mcp")
    async def mcp_method_not_allowed():
        return JSONResponse(
            status_code=405,
            content={"code": "METHOD_NOT_ALLOWED", "message": "MCP 使用 POST。"},
            headers={"Allow": "POST"},
        )

    @app.get("/api/v1/libraries")
    async def libraries(request: Request) -> dict:
        upload_capability = await asyncio.to_thread(
            scan_capability,
            uploads_enabled=settings.manuscript_upload_enabled,
            required=settings.manuscript_scan_required,
            scanner=manuscript_scanner,
        )
        return {
            "accounts_enabled": accounts is not None,
            "projects_enabled": projects is not None,
            "authentication_required": settings.auth_required,
            "libraries": library_infos(settings),
            "simulation": service.simulation,
            "agent_available": agent.available,
            "agent_mode": "skill_guided_langchain",
            "model": agent.model_name if agent.available else None,
            "account_model_sync": account_models is not None,
            "demo_local_search_available": bool(
                service.demo_library_ids(_tenant(request, settings))
            ),
            "conversation_enabled": True,
            **upload_capability,
            "max_upload_bytes": manuscripts.limits.max_upload_bytes,
            "ocr_enabled": manuscripts.ocr_enabled,
            "citation_preparation_enabled": True,
            "citation_styles": ["apa", "vancouver"],
            "user_model_protocols": [
                {"id": name, "display_name": spec[0]} for name, spec in MODEL_PROTOCOLS.items()
            ]
            if settings.allow_user_models
            else [],
            "user_model_providers": [
                {"provider": provider, "display_name": config["display_name"]}
                for provider, config in MODEL_PROVIDERS.items()
            ]
            if settings.allow_user_models
            else [],
            "notice": service.notice,
        }

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "api_version": API_VERSION,
            "schema_version": SCHEMA_VERSION,
            "storage": "sqlite_local",
            "worker_mode": "in_process_asyncio",
            "durable_worker_queue": False,
            "in_progress_recovery": False,
            "persistence": "completed_searches_events_snapshots_agent_runs",
            "adapter_mode": service.adapter_mode,
            "simulation": service.simulation,
            "agent_mode": "skill_guided_langchain",
            "agent_available": agent.available,
            "reason_model": agent.model_name if reason_provider else None,
            "authentication_required": settings.auth_required,
            "mcp_path": f"{settings.base_path}/mcp",
            "notice": service.notice,
        }

    @app.post("/api/v1/agent/runs", status_code=202)
    async def create_agent_run(request: Request, body: AgentRunCreate) -> AgentRun:
        if body.project_id is not None:
            require_account(request)
        if body.library_ids is not None and getattr(request.state, "account", None) is None:
            configured = {library.library_id for library in settings.libraries}
            if (
                not body.library_ids
                or len(body.library_ids) != len(set(body.library_ids))
                or any(library not in configured for library in body.library_ids)
            ):
                raise ApiError(400, "VALIDATION_ERROR", "请选择非空且不重复的已配置数据源。")
        tenant_id = _tenant(request, settings)
        model_config = (
            _user_model_config(body.model, settings)
            if body.local_only
            else await request_model_config(request, body.model)
        )

        def prepare(query_id: str):
            return citations.schedule_preparation(tenant_id, query_id, model_config)

        try:
            return agent.start(
                tenant_id,
                body.search_create(),
                request.headers.get("Idempotency-Key"),
                model_config=model_config,
                on_query_ready=prepare if body.prepare_citations else None,
                conversation_id=body.conversation_id,
                message=body.message,
                active_query_id=body.active_query_id,
                expected_revision=body.expected_revision,
                project_id=body.project_id,
                local_only=body.local_only,
            )
        except LocalSearchUnavailableError as exc:
            raise ApiError(400, "LOCAL_SEARCH_UNAVAILABLE", str(exc)) from exc
        except AgentCapacityError as exc:
            raise ApiError(
                429,
                "AGENT_CAPACITY_EXCEEDED",
                "当前检索任务较多，请稍后重试。",
                retryable=True,
            ) from exc
        except AgentUnavailableError as exc:
            raise ApiError(
                503,
                "MODEL_NOT_CONFIGURED",
                "服务端尚未配置模型，无法启动自由 Agent；不会用固定脚本冒充 Agent。",
                retryable=False,
            ) from exc
        except IdempotencyConflict as exc:
            raise ApiError(409, exc.code, "相同 Idempotency-Key 绑定了不同 Agent 请求。") from exc

    @app.get("/api/v1/agent/runs/{run_id}")
    async def get_agent_run(request: Request, run_id: str) -> AgentRun:
        run = store.get_agent_run(_tenant(request, settings), run_id)
        if run is None:
            raise ApiError(404, "AGENT_RUN_NOT_FOUND", "Agent 运行不存在或不属于当前租户。")
        return run

    @app.post("/api/v1/agent/runs/{run_id}/cancel")
    async def cancel_agent_run(request: Request, run_id: str) -> AgentRun:
        run = store.get_agent_run(_tenant(request, settings), run_id)
        if run is None:
            raise ApiError(404, "AGENT_RUN_NOT_FOUND", "Agent 运行不存在或不属于当前租户。")
        return await agent.cancel(run)

    @app.get("/api/v1/conversations/{conversation_id}")
    async def get_conversation(request: Request, conversation_id: str):
        tenant_id = _tenant(request, settings)

        def read():
            conversation = store.get_conversation(tenant_id, conversation_id)
            if conversation is None:
                raise ApiError(404, "CONVERSATION_NOT_FOUND", "会话不存在或不属于当前租户。")
            payload = conversation.model_dump(mode="json")
            payload["runs"] = [
                run.model_dump(mode="json")
                for turn in conversation.turns
                if (run := store.get_agent_run(tenant_id, turn.run_id)) is not None
            ]
            return payload

        return await asyncio.to_thread(read)

    @app.post("/api/v1/conversations/{conversation_id}/turns", status_code=202)
    async def create_conversation_turn(
        request: Request, conversation_id: str, body: ConversationTurnCreate
    ) -> AgentRun:
        tenant_id = _tenant(request, settings)
        conversation = await asyncio.to_thread(store.get_conversation, tenant_id, conversation_id)
        if conversation is None:
            raise ApiError(404, "CONVERSATION_NOT_FOUND", "会话不存在或不属于当前租户。")
        if not body.message.strip():
            raise ApiError(400, "VALIDATION_ERROR", "消息不能为空。")
        library_ids = body.library_ids
        if (
            library_ids is None
            and conversation.turns
            and getattr(request.state, "account", None) is None
        ):
            previous = await asyncio.to_thread(
                store.get_agent_run, tenant_id, conversation.turns[-1].run_id
            )
            if previous is not None:
                library_ids = previous.request.library_ids
        return await create_agent_run(
            request,
            AgentRunCreate(
                input_type="abstract",
                abstract=body.message,
                library_ids=library_ids,
                model=body.model,
                conversation_id=conversation_id,
                message=body.message,
                active_query_id=body.active_query_id,
                expected_revision=body.expected_revision,
                prepare_citations=body.prepare_citations,
            ),
        )

    @app.get("/api/v1/searches/{query_id}/citation-suggestion")
    @app.get("/api/v1/searches/{query_id}/citation-formats")
    async def get_citation_for_result(
        request: Request,
        query_id: str = Path(min_length=1, max_length=128),
        result_id: str = Query(min_length=1, max_length=128),
        result_version: int = Query(ge=0),
        result_kind: Literal["candidate", "recommendation"] = "candidate",
    ) -> JSONResponse:
        target = CitationTextRequest(
            query_id=query_id,
            result_id=result_id,
            result_version=result_version,
            result_kind=result_kind,
            citation_style="vancouver",
            language="zh",
        )
        handler = (
            citations.get_formats
            if request.url.path.endswith("/citation-formats")
            else citations.get_prepared
        )
        payload = await asyncio.to_thread(handler, _tenant(request, settings), target)
        return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/citation-texts", status_code=202)
    async def create_citation_text(request: Request, body: CitationTextCreate):
        started = await citations.start(
            _tenant(request, settings),
            body.citation_request(),
            request.headers.get("Idempotency-Key"),
            model_config=await request_model_config(request, body.model),
        )
        payload = started.model_dump(mode="json")
        payload["status_url"] = http_resource_path(
            request, f"/api/v1/citation-texts/{started.citation_text_id}"
        )
        return JSONResponse(
            status_code=200 if started.cached else 202,
            content=payload,
        )

    @app.get("/api/v1/citation-texts/{citation_text_id}")
    async def get_citation_text(request: Request, citation_text_id: str):
        return await asyncio.to_thread(citations.get, _tenant(request, settings), citation_text_id)

    @app.post("/api/v1/citation-texts/{citation_text_id}/cancel")
    async def cancel_citation_text(request: Request, citation_text_id: str):
        return await citations.cancel(_tenant(request, settings), citation_text_id)

    deleting_manuscripts: set[tuple[str, str]] = set()

    async def delete_manuscript_data(tenant_id: str, manuscript_id: str):
        record = await asyncio.to_thread(manuscripts.get, tenant_id, manuscript_id, catalog_limit=0)
        if record is None:
            return await asyncio.to_thread(manuscripts.delete, tenant_id, manuscript_id)
        key = (tenant_id, manuscript_id)
        if key in deleting_manuscripts:
            raise ApiError(409, "MANUSCRIPT_DELETE_BUSY", "该稿件正在删除，请稍后重试。")
        deleting_manuscripts.add(key)
        service.purging_documents.add(key)
        try:
            await agent.purge_manuscript(
                tenant_id,
                manuscript_id,
                before_query_delete=citations.purge_queries,
            )
            service.purging_documents.add(key)
            return await asyncio.to_thread(manuscripts.delete, tenant_id, manuscript_id)
        finally:
            service.purging_documents.discard(key)
            deleting_manuscripts.discard(key)

    app.include_router(
        build_manuscript_router(
            manuscripts,
            lambda request: _tenant(request, settings),
            uploads_enabled=settings.manuscript_upload_enabled,
            scan_required=settings.manuscript_scan_required,
            scanner=manuscript_scanner,
            delete_handler=delete_manuscript_data,
        )
    )

    @app.post("/api/v1/searches", status_code=202)
    async def create_search(request: Request, body: SearchCreate):
        try:
            created, _ = service.create(
                _tenant(request, settings), body, request.headers.get("Idempotency-Key")
            )
        except SearchRequestError as exc:
            raise ApiError(400, "VALIDATION_ERROR", exc.message, exc.details) from exc
        except IdempotencyConflict as exc:
            raise ApiError(409, exc.code, "相同 Idempotency-Key 绑定了不同请求。") from exc
        except StoreConflict as exc:
            raise ApiError(409, exc.code, "搜索状态冲突。") from exc
        return JSONResponse(status_code=202, content=created.model_dump(mode="json"))

    @app.post("/api/v1/searches/{query_id}/cancel", status_code=202)
    async def cancel_search(request: Request, query_id: str):
        tenant_id = _tenant(request, settings)
        search = store.get_search(tenant_id, query_id)
        if search is None:
            raise ApiError(404, "QUERY_NOT_FOUND", "搜索会话不存在或不属于当前租户。")
        state = service.state(tenant_id, query_id)
        if search["search_state"] in TERMINAL_STATES:
            snapshot_id = (
                search["final_snapshot_id"] if search["search_state"] == "cancelled" else None
            )
            return {"session": state.session, "cancel_snapshot_id": snapshot_id}
        service.coordinator.request_cancel(query_id)
        return {"session": state.session, "cancel_snapshot_id": None}

    @app.get("/api/v1/searches/{query_id}")
    async def get_search_state(request: Request, query_id: str):
        state = service.state(_tenant(request, settings), query_id)
        if state is None:
            raise ApiError(404, "QUERY_NOT_FOUND", "搜索会话不存在或不属于当前租户。")
        return state

    @app.get("/api/v1/searches/{query_id}/candidate-rankings")
    async def candidate_ranking_history(request: Request, query_id: str):
        tenant_id = _tenant(request, settings)
        if store.get_search(tenant_id, query_id) is None:
            raise ApiError(404, "QUERY_NOT_FOUND", "搜索会话不存在或不属于当前租户。")
        return {
            "query_id": query_id,
            "history": store.get_candidate_ranking_history(tenant_id, query_id),
        }

    @app.get("/api/v1/searches/{query_id}/events")
    async def stream_events(request: Request, query_id: str):
        tenant_id = _tenant(request, settings)
        search = store.get_search(tenant_id, query_id)
        if search is None:
            raise ApiError(404, "QUERY_NOT_FOUND", "搜索会话不存在或不属于当前租户。")
        after_query = request.query_params.get("after_sequence")
        after_header = request.headers.get("Last-Event-ID")
        after: int | None = None
        for value in (after_query, after_header):
            if value is None or value == "":
                continue
            if not re.fullmatch(r"\d{1,19}", value):
                raise ApiError(400, "VALIDATION_ERROR", "after_sequence 必须是非负整数。")
            parsed = int(value)
            if after is not None and parsed != after:
                raise ApiError(
                    409, "REPLAY_REQUEST_CONFLICT", "Last-Event-ID 与 after_sequence 不一致。"
                )
            after = parsed
        after = after or 0
        latest = search["latest_sequence"]
        if after > latest:
            raise ApiError(409, "REPLAY_REQUEST_CONFLICT", "续传起点超前于已提交水位。")
        floor = max(0, latest - settings.event_retention_max_per_query)
        if after < floor:
            raise ApiError(
                410,
                "REPLAY_GAP",
                "续传起点已超出保留窗口，请先读取当前状态重建。",
                {"earliest_replayable_sequence": floor + 1},
            )

        account = getattr(request.state, "account", None)
        session_token = request.cookies.get(ACCOUNT_COOKIE, "")

        async def event_stream():
            cursor = after
            last_heartbeat = time.monotonic()
            next_auth_check = 0.0
            while True:
                if (
                    account is not None
                    and accounts is not None
                    and time.monotonic() >= next_auth_check
                ):
                    if await asyncio.to_thread(accounts.session_account, session_token) is None:
                        break
                    next_auth_check = time.monotonic() + 1.0
                events = store.list_events(tenant_id, query_id, cursor, limit=500)
                for event in events:
                    cursor = event["sequence"]
                    yield (
                        f"id: {event['sequence']}\n"
                        f"event: {event['event_type']}\n"
                        f"data: {_sse_envelope(event)}\n\n"
                    )
                current = store.get_search(tenant_id, query_id)
                if current is None:
                    break
                if current["search_state"] in TERMINAL_STATES and not events:
                    break
                now = time.monotonic()
                if (now - last_heartbeat) * 1000 >= settings.sse_heartbeat_ms:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
                await asyncio.sleep(settings.sse_poll_ms / 1000)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/v1/searches/{query_id}/snapshots")
    async def create_snapshot(request: Request, query_id: str, body: SnapshotCreate):
        tenant_id = _tenant(request, settings)
        search = store.get_search(tenant_id, query_id)
        if search is None:
            raise ApiError(404, "QUERY_NOT_FOUND", "搜索会话不存在或不属于当前租户。")
        if body.kind == "checkpoint":
            try:
                manifest = store.create_checkpoint(
                    tenant_id, query_id, request.headers.get("Idempotency-Key")
                )
            except IdempotencyConflict as exc:
                raise ApiError(409, exc.code, "相同 Idempotency-Key 绑定了不同快照请求。") from exc
            except StoreConflict as exc:
                raise ApiError(409, "QUERY_STATE_CONFLICT", str(exc)) from exc
            return JSONResponse(status_code=201, content=manifest)
        try:
            manifest = store.reserve_final_request(
                tenant_id, query_id, request.headers.get("Idempotency-Key")
            )
        except IdempotencyConflict as exc:
            raise ApiError(409, exc.code, "相同 Idempotency-Key 绑定了不同快照请求。") from exc
        except StoreConflict as exc:
            raise ApiError(409, "QUERY_STATE_CONFLICT", str(exc)) from exc
        if manifest is not None:
            return JSONResponse(content=manifest)
        service.coordinator.request_finalization(query_id)
        return JSONResponse(
            status_code=202,
            content={"query_id": query_id, "snapshot_kind": "final", "snapshot_id": None},
        )

    @app.get("/api/v1/snapshots/{snapshot_id}")
    async def get_snapshot(request: Request, snapshot_id: str):
        snapshot = store.get_snapshot(_tenant(request, settings), snapshot_id)
        if snapshot is None:
            raise ApiError(404, "SNAPSHOT_NOT_FOUND", "快照不存在或不属于当前租户。")
        return Response(
            content=snapshot["manifest_json"],
            media_type="application/json",
            headers={"ETag": f'"{snapshot["immutable_hash"]}"', "Cache-Control": "no-store"},
        )

    @app.get("/api/v1/snapshots/{snapshot_id}/evidence/{evidence_id}")
    async def get_snapshot_evidence(request: Request, snapshot_id: str, evidence_id: str):
        tenant_id = _tenant(request, settings)
        if store.get_snapshot(tenant_id, snapshot_id) is None:
            raise ApiError(404, "SNAPSHOT_NOT_FOUND", "快照不存在或不属于当前租户。")
        evidence = store.get_evidence(tenant_id, snapshot_id, evidence_id)
        if evidence is None:
            raise ApiError(404, "EVIDENCE_NOT_FOUND", "证据不存在于该快照。")
        return evidence

    web_root = PROJECT_ROOT / "web"
    if (web_root / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_root, html=True), name="local-web")

    return app
