"""Same-origin browser endpoints for the optional account workspace."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, SecretStr, ValidationError

from .accounts import (
    ACCOUNT_COOKIE,
    ACCOUNT_SESSION_TTL,
    Account,
    AccountError,
    AccountGrant,
    AccountRateLimiter,
    AccountStore,
    account_session_error,
    username_key,
)
from .entities import StrictModel

WORKSPACE_HEADER = "X-Workspace-Account"
_MUTATION = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class SignIn(StrictModel):
    username: str = Field(min_length=1, max_length=40)
    password: SecretStr = Field(min_length=1, max_length=128, exclude=True)


class Register(SignIn):
    display_name: str = Field(default="", max_length=48)


class Recover(SignIn):
    recovery_code: SecretStr = Field(min_length=1, max_length=128, exclude=True)


class ChangePassword(StrictModel):
    current_password: SecretStr = Field(min_length=1, max_length=128, exclude=True)
    new_password: SecretStr = Field(min_length=1, max_length=128, exclude=True)


class Profile(StrictModel):
    display_name: str = Field(min_length=1, max_length=48)


class ConfirmDeletion(StrictModel):
    current_password: SecretStr = Field(min_length=1, max_length=128, exclude=True)


Body = TypeVar("Body", bound=StrictModel)
Result = TypeVar("Result")


def enforce_account_origin(request: Request) -> None:
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if request.headers.get("origin") != expected or request.headers.get("sec-fetch-site") not in (
        None,
        "same-origin",
    ):
        raise AccountError(403, "ACCOUNT_ORIGIN_REJECTED", "请从本站页面操作账户。")


def validate_account_transport(request: Request) -> None:
    if request.url.scheme != "https" and not (
        request.url.scheme == "http"
        and request.url.hostname in _LOOPBACK
        and request.client is not None
        and request.client.host in _LOOPBACK
    ):
        raise AccountError(403, "ACCOUNT_HTTPS_REQUIRED", "请使用 HTTPS 访问账户。")


def check_workspace_account(request: Request) -> Account | None:
    account = getattr(request.state, "account", None)
    expected = request.headers.get(WORKSPACE_HEADER)
    if expected is not None and expected != (account.account_id if account else "guest"):
        if account is None:
            raise account_session_error()
        raise AccountError(
            409, "WORKSPACE_CHANGED", "当前浏览器登录的账户已变更，请重新打开工作区。"
        )
    if account is None and expected is None and getattr(request.state, "stale_account", False):
        raise account_session_error()
    if account is not None and request.method in _MUTATION:
        enforce_account_origin(request)
    return account


def require_account(request: Request) -> Account:
    account = check_workspace_account(request)
    if account is None:
        raise AccountError(401, "ACCOUNT_REQUIRED", "请登录后查看个人对话记录。")
    return account


async def _bounded_json[T: StrictModel](request: Request, model: type[T]) -> T:
    mime = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if mime != "application/json":
        raise AccountError(415, "ACCOUNT_JSON_REQUIRED", "账户请求必须使用 JSON。")
    data = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(data) + len(chunk) > 4096:
                    raise AccountError(413, "ACCOUNT_BODY_TOO_LARGE", "账户请求超过大小上限。")
                data.extend(chunk)
    except TimeoutError:
        raise AccountError(408, "ACCOUNT_BODY_TIMEOUT", "账户请求读取超时，请重试。") from None
    try:
        return model.model_validate_json(data)
    except ValidationError:
        raise AccountError(400, "ACCOUNT_FORM_INVALID", "账户表单不完整或字段过长。") from None


def build_account_router(
    store: AccountStore,
    *,
    cookie_path: str,
    secure: bool,
    delete_data: Callable[[Account], Awaitable[None]],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/account")
    rate = AccountRateLimiter()
    slots = asyncio.Semaphore(2)

    def token(request: Request) -> str:
        return request.cookies.get(ACCOUNT_COOKIE, "")

    def secure_cookie(request: Request) -> bool:
        return secure or request.url.scheme == "https"

    async def protected_form(request: Request, model: type[Body]) -> Body:
        validate_account_transport(request)
        enforce_account_origin(request)
        return await _bounded_json(request, model)

    async def expensive(
        request: Request,
        action: str,
        identity: str,
        fn: Callable[..., Result],
        *args,
    ) -> Result:
        if slots.locked():
            raise AccountError(
                429, "ACCOUNT_BUSY", "账户服务正在处理其他请求，请稍后重试。", retryable=True
            )
        rate.check(action, request.client.host if request.client else "unknown", identity)
        async with slots:
            job = asyncio.create_task(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                # Do not release the memory-hard hashing slot before its worker has stopped.
                await asyncio.gather(job, return_exceptions=True)
                raise

    def granted(request: Request, grant: AccountGrant, *, status: int = 200) -> JSONResponse:
        response = JSONResponse(
            {
                "account": grant.account.payload(),
                **({"recovery_code": grant.recovery_code} if grant.recovery_code else {}),
            },
            status_code=status,
        )
        response.set_cookie(
            ACCOUNT_COOKIE,
            grant.session,
            max_age=ACCOUNT_SESSION_TTL,
            path=cookie_path,
            secure=secure_cookie(request),
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/session")
    async def session(request: Request) -> dict:
        validate_account_transport(request)
        account = getattr(request.state, "account", None)
        return {"account": account.payload() if account else None}

    @router.post("/register")
    async def register(request: Request) -> JSONResponse:
        body = await protected_form(request, Register)
        username = username_key(body.username)
        grant = await expensive(
            request,
            "register",
            username,
            store.register,
            username,
            body.password.get_secret_value(),
            body.display_name,
            token(request),
        )
        return granted(request, grant, status=201)

    @router.post("/login")
    async def log_in(request: Request) -> JSONResponse:
        body = await protected_form(request, SignIn)
        username = username_key(body.username)
        grant = await expensive(
            request,
            "login",
            username,
            store.sign_in,
            username,
            body.password.get_secret_value(),
            token(request),
        )
        return granted(request, grant)

    @router.post("/recover")
    async def recover(request: Request) -> JSONResponse:
        body = await protected_form(request, Recover)
        username = username_key(body.username)
        grant = await expensive(
            request,
            "recover",
            username,
            store.reset_password,
            username,
            body.recovery_code.get_secret_value(),
            body.password.get_secret_value(),
            token(request),
        )
        return granted(request, grant)

    def cleared_session(request: Request) -> Response:
        response = Response(status_code=204)
        response.delete_cookie(
            ACCOUNT_COOKIE,
            path=cookie_path,
            secure=secure_cookie(request),
            httponly=True,
            samesite="lax",
        )
        return response

    @router.post("/logout", status_code=204)
    async def log_out(request: Request) -> Response:
        validate_account_transport(request)
        enforce_account_origin(request)
        check_workspace_account(request)
        await asyncio.to_thread(store.sign_out, token(request))
        return cleared_session(request)

    @router.post("/delete", status_code=204)
    async def delete_account(request: Request) -> Response:
        account = require_account(request)
        body = await protected_form(request, ConfirmDeletion)
        verified = await expensive(
            request,
            "delete",
            account.username,
            store.verify_password,
            token(request),
            body.current_password.get_secret_value(),
        )
        await delete_data(verified)
        return cleared_session(request)

    @router.patch("/profile")
    async def profile(request: Request) -> dict:
        require_account(request)
        body = await protected_form(request, Profile)
        account = await asyncio.to_thread(store.update_profile, token(request), body.display_name)
        return {"account": account.payload()}

    @router.post("/password")
    async def change_password(request: Request) -> JSONResponse:
        account = require_account(request)
        body = await protected_form(request, ChangePassword)
        grant = await expensive(
            request,
            "password",
            account.username,
            store.change_password,
            token(request),
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
        )
        return granted(request, grant)

    return router
