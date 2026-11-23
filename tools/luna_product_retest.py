"""Product-side 6luna development retest for the frozen SciFact gap tasks.

This runner produces the product side only.  The paired evaluation, original gold
scoring, and first-loss diagnostics stay in :mod:`tools.luna_gap_retest`; this module
does not duplicate or alter that evaluator.

The product uses the local Kimi CLI JSON-envelope seam from
:mod:`tools.kimi_chat_model`, not the normal Kimi agent.  Tool schemas and messages are
carried in a temporary agent profile, the CLI has no tools, and tool requests are returned
to LangChain as JSON.  The baseline Kimi run uses its normal tools in a fresh session.
Model-name equality therefore does not imply transport equality, native function-calling
equivalence, or an architecture advantage.

Each case gets its own SQLite database and Kimi model-call audit directory.  Every model
call is checked from the exported session; a call is valid only when the observed model is
exactly ``gpt-6-luna``.  Failures and timeouts are saved as failed records and are never
overwritten.  The fixed 5,183-record corpus must use the prebuilt E5 cache; missing or
mismatched cache data aborts before any document embedding.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from tools.benchmark.core import ROOT, BenchmarkUsageCallback, file_hash, secret_leak
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, save_new
from tools.kimi_chat_model import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_ALIAS,
    DEFAULT_PROVIDER,
    TRANSPORT,
    KimiLunaGateway,
    build_discovery_components,
    build_kimi_gateway,
)
from tools.luna_gap_retest import prepare_run
from tools.quality_benchmark.core import (
    EMBEDDING_CACHE_DIR,
    artifact_hash,
    output_details,
    visible_output,
)

from citation_tool.agent import AgentRunCreate
from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.publication import canonical_work_id
from citation_tool.semantic import (
    EMBEDDING_CACHE_VERSION,
    EmbeddingIndex,
    SemanticMetadataAdapter,
    hybrid_candidate_window,
)

logger = logging.getLogger(__name__)

SCHEMA = "scifact-luna-product-record-v1"
SUMMARY_SCHEMA = "scifact-luna-product-summary-v1"
CONTRACT_SCHEMA = "scifact-luna-product-contract-v1"
REQUEST_MAPPING_VERSION = "scientific-input-with-user-task-v2"
EXPECTED_CASES = 28
EXPECTED_RECORDS = 5183
MAX_PARALLEL_SUBJECTS = 2
MAX_PROCESS_MODEL_CALLS = 2
MODEL_TIMEOUT_S = 300.0
AGENT_TIMEOUT_MS = 900_000
AGENT_POLL_GRACE_S = 10.0
CASE_CLEANUP_TIMEOUT_S = 10.0
TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}
AGENT_MAX_TOOL_CALLS = 32
AGENT_RECURSION_LIMIT = 96
TRANSPORT_NOTE = (
    "Product side uses the local Kimi CLI agent-file JSON tool envelope with tools disabled "
    "in the CLI and plugin session-start prompt overhead. Baseline Kimi uses its normal "
    "tools/skills. The model name is the same, but transport and prompt envelope differ; "
    "this is not native function-calling equivalence and cannot support architecture claims."
)
_SECRET_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}")


def _redact(value: str) -> str:
    value = _SECRET_PATTERN.sub("[REDACTED]", value)
    value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    return value[:500]


def product_settings(runtime: Path, task: dict[str, Any]) -> Settings:
    library = LibraryConfig(
        library_id="scifact",
        display_name="SciFact 原始题名与摘要",
        fixture_set="",
        delay_ms=0,
        adapter_id="metadata-hybrid-e5-rrf-v1",
        simulation=False,
        source_type="local_snapshot",
        corpus_path=RUNTIME / "corpus.json",
    )
    settings = replace(
        Settings(),
        db_path=runtime / "app.db",
        libraries=(library,),
        library_mode="metadata",
        candidate_pipeline="agentic",
        model_config_path=None,
        allow_user_models=False,
        model_max_inflight=MAX_PROCESS_MODEL_CALLS,
        model_timeout_ms=MODEL_TIMEOUT_S * 1000,
        agent_timeout_ms=AGENT_TIMEOUT_MS,
        agent_max_tool_calls=AGENT_MAX_TOOL_CALLS,
        agent_recursion_limit=AGENT_RECURSION_LIMIT,
        agent_max_concurrent_runs=MAX_PARALLEL_SUBJECTS,
        default_soft_deadline_ms=180_000,
        embedding_enabled=True,
        embedding_cache_dir=EMBEDDING_CACHE_DIR,
        embedding_dense_threshold=float(task["dense_threshold"]),
        online_enabled=False,
        auth_required=False,
        auth_db_path=runtime / "auth.db",
        manuscript_upload_enabled=False,
        citation_text_max_concurrent=1,
        trusted_hosts=("127.0.0.1", "localhost"),
        base_path="",
    )
    settings.validate()
    return settings


def require_prebuilt_index(
    corpus: dict[str, Any], settings: Settings, *, index_type: type[EmbeddingIndex] = EmbeddingIndex
) -> EmbeddingIndex:
    index = index_type(
        corpus["records"],
        model_name=settings.embedding_model,
        cache_dir=settings.embedding_cache_dir,
    )
    path = index.cache_path
    if not path.is_file():
        raise RuntimeError(
            f"prebuilt E5 cache missing for the frozen {len(corpus['records'])}-record corpus: "
            f"{path}; refusing to embed the whole corpus"
        )
    expected_ids = [canonical_work_id(record) for record in corpus["records"]]
    try:
        with np.load(path, allow_pickle=False) as cached:
            ids = cached["ids"].tolist()
            vectors = cached["vectors"]
            valid = (
                cached["version"].item() == EMBEDDING_CACHE_VERSION
                and cached["model"].item() == index.model_name
                and cached["model_revision"].item() == index.model_revision
                and ids == expected_ids
                and vectors.ndim == 2
                and vectors.shape[0] == len(expected_ids)
                and np.isfinite(vectors).all()
            )
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"prebuilt E5 cache cannot be validated: {path}") from exc
    if not valid:
        raise RuntimeError(
            f"prebuilt E5 cache does not match the frozen corpus/model: {path}; "
            "refusing to rebuild it during the product retest"
        )
    return index


def build_product_adapter(
    settings: Settings,
    corpus: dict[str, Any],
    index: EmbeddingIndex,
    task: dict[str, Any],
):
    library = settings.libraries[0]
    if task.get("retriever") == "hybrid":
        return SemanticMetadataAdapter(
            library,
            corpus["records"],
            settings=settings,
            index=index,
            dense_threshold=float(task["dense_threshold"]),
            top_k=hybrid_candidate_window(settings),
        )
    return MetadataCorpusAdapter(library, corpus["records"])


def _attach_callbacks(
    callback: BenchmarkUsageCallback,
    reasons: Any,
    agent_model: Any,
    discovery_models: Any,
) -> None:
    reasons._model.callbacks = [callback]
    agent_model.callbacks = [callback]
    discovery_models.source_model.callbacks = [callback]
    discovery_models.ranking_model.callbacks = [callback]


def build_product_app(
    settings: Settings,
    adapter: Any,
    gateway: KimiLunaGateway,
    callback: BenchmarkUsageCallback,
):
    from citation_tool.api import create_app

    reasons, agent_model, discovery_models = build_discovery_components(
        gateway, max_inflight=MAX_PROCESS_MODEL_CALLS, timeout_ms=int(MODEL_TIMEOUT_S * 1000)
    )
    _attach_callbacks(callback, reasons, agent_model, discovery_models)
    app = create_app(
        settings,
        adapters={adapter.library.library_id: adapter},
        reason_generator=reasons,
        agent_model=agent_model,
        discovery_models=discovery_models,
    )
    reasons._cache = app.state.store
    app.state.agent_runner.provider = gateway.config.provider
    if app.state.search_service.reason_provider != DEFAULT_PROVIDER:
        raise RuntimeError("product app is not using the Kimi reason model")
    if app.state.agent_runner.model_name != DEFAULT_MODEL:
        raise RuntimeError("product agent is not using the expected model")
    return app, reasons, discovery_models


def request_payload(task: dict[str, Any]) -> tuple[dict[str, Any], str]:
    subject = copy.deepcopy(task)
    subject.pop("expect", None)
    request = AgentRunCreate.model_validate(
        {**subject["request"], "message": subject["baseline_query"]}
    )
    payload = request.model_dump(mode="json", exclude_none=True)

    def contains_key(value: Any, key: str) -> bool:
        if isinstance(value, dict):
            return key in value or any(contains_key(item, key) for item in value.values())
        if isinstance(value, list):
            return any(contains_key(item, key) for item in value)
        return False

    if contains_key(payload, "expect"):
        raise ValueError("gold expect leaked into the product request")
    return payload, sha256_json(payload)


def _artifact_rows(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    artifacts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if not directory.exists():
        return artifacts, errors
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"path": path.name, "type": type(exc).__name__})
            continue
        artifacts.append(
            {
                "call_id": value.get("call_id"),
                "session_id": value.get("session_id"),
                "model_observed": list(value.get("model_observed") or []),
                "model_valid": bool(value.get("model_valid")),
                "model_calls": int(value.get("model_calls", 0) or 0),
                "usage": value.get("usage") or {},
                "duration_ms": value.get("duration_ms"),
                "tool_calls_requested": list(value.get("tool_calls_requested") or []),
                "tool_choice": value.get("tool_choice"),
                "json_mode": bool(value.get("json_mode")),
                "request_sha256": value.get("request_sha256"),
                "error": value.get("error"),
            }
        )
    return artifacts, errors


def collect_model_audit(directory: Path) -> dict[str, Any]:
    artifacts, artifact_errors = _artifact_rows(directory)
    served: set[str] = set()
    sessions: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    model_calls = 0
    usage_report_count = 0
    usage_complete = bool(artifacts) and not artifact_errors
    verified = bool(artifacts) and not artifact_errors
    for artifact in artifacts:
        observed = artifact["model_observed"]
        served.update(observed)
        if artifact["session_id"]:
            sessions.append(artifact["session_id"])
        for key in usage:
            usage[key] += int((artifact["usage"] or {}).get(key, 0) or 0)
        model_calls += artifact["model_calls"]
        reported = artifact["usage"] or {}
        usage_report_count += int(reported.get("usage_report_count", 0) or 0)
        usage_complete = usage_complete and reported.get("usage_complete") is True
        if not artifact["model_valid"] or observed != [DEFAULT_MODEL] or not artifact["session_id"]:
            verified = False
    usage["model_calls"] = model_calls
    usage["usage_report_count"] = usage_report_count
    usage["usage_complete"] = usage_complete
    usage["measurement_boundary"] = (
        "observed_requests_and_reported_usage"
        if usage_complete
        else "observable_lower_bound; missing request or token reports are not zero usage"
    )
    usage["served_models"] = sorted(served)
    usage["uncached_input_tokens"] = max(0, usage["input_tokens"] - usage["cache_read_tokens"])
    return {
        "artifacts": artifacts,
        "artifact_errors": artifact_errors,
        "artifact_count": len(artifacts),
        "model_calls": model_calls,
        "usage": usage,
        "served_models": sorted(served),
        "session_ids": sessions,
        "session_isolation_valid": bool(sessions) and len(sessions) == len(set(sessions)),
        "model_valid": verified,
    }


def reason_metrics(queries: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = [
        item["reason"]
        for query in queries
        for item in query.get("candidate_diagnostics", [])
        if item.get("reason")
    ]
    statuses = Counter(str(item.get("status")) for item in reasons)
    failures = Counter(
        str(item.get("failure_code")) for item in reasons if item.get("failure_code")
    )
    generated = statuses.get("generated", 0)
    return {
        "total": len(reasons),
        "generated": generated,
        "failed": statuses.get("failed", 0),
        "unavailable": statuses.get("unavailable", 0),
        "failure_codes": dict(sorted(failures.items())),
        "served_models": sorted(
            {str(item["served_model"]) for item in reasons if item.get("served_model")}
        ),
        "all_generated": bool(reasons) and generated == len(reasons),
    }


def ranking_metrics(queries: list[dict[str, Any]]) -> dict[str, Any]:
    rankings = [item for query in queries for item in query.get("candidate_rankings", [])]
    statuses = Counter(str(item.get("status")) for item in rankings)
    failures = Counter(
        str(item.get("failure_code")) for item in rankings if item.get("failure_code")
    )
    completed = statuses.get("completed", 0)
    queries_with_completed = sum(
        any(item.get("status") == "completed" for item in query.get("candidate_rankings", []))
        for query in queries
    )
    return {
        "query_count": len(queries),
        "total": len(rankings),
        "completed": completed,
        "failed": statuses.get("failed", 0),
        "unavailable": statuses.get("unavailable", 0),
        "pending": statuses.get("pending", 0),
        "failure_codes": dict(sorted(failures.items())),
        "queries_with_completed": queries_with_completed,
        "queries_without_ranking": sum(
            not query.get("candidate_rankings", []) for query in queries
        ),
        "all_completed": bool(rankings) and completed == len(rankings),
    }


def _source_actions(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        action
        for query in queries
        for worker in query.get("workers", [])
        for action in worker.get("source_actions", [])
    ]


def build_record(
    task: dict[str, Any],
    *,
    run: dict[str, Any],
    queries: list[dict[str, Any]],
    audit: dict[str, Any],
    callback: BenchmarkUsageCallback,
    runtime: Path,
    root: Path,
    implementation_hash: str,
    request_sha256: str | None,
    started_at: str,
    started: float,
    error: dict[str, Any] | None,
    missing_query_states: list[str],
) -> dict[str, Any]:
    reasons = reason_metrics(queries)
    rankings = ranking_metrics(queries)
    served = set(audit["served_models"])
    served.update(callback.totals.served_models)
    if isinstance(run.get("served_model"), str):
        served.add(run["served_model"])
    served.update(reasons["served_models"])
    model_valid = bool(audit["model_valid"]) and served == {DEFAULT_MODEL}
    visible = visible_output(run, queries)
    source_actions = _source_actions(queries)
    parent_calls = run.get("tool_calls", [])
    retrieval_actions = sum(
        len(query.get("workers", []))
        + sum(
            action.get("tool") == "search_source" and action.get("executed", True)
            for worker in query.get("workers", [])
            for action in worker.get("source_actions", [])
        )
        for query in queries
    ) or max(len(parent_calls), len(queries))
    return {
        "schema": SCHEMA,
        "system": "product",
        "case_id": task["case_id"],
        "run_case_id": task["run_case_id"],
        "repetition": task["repetition"],
        "split": "development",
        "slice": task["slice"],
        "family": task["family"],
        "started_at": started_at,
        "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
        "completed": bool(run and run.get("status") == "completed" and not error),
        "response_text": run.get("summary") or "",
        "visible_output": visible,
        "error": error or run.get("error"),
        "agent_run_id": run.get("run_id"),
        "response_grounding": run.get("response_grounding"),
        "design_reviews": run.get("design_reviews", []),
        "model_requested": DEFAULT_MODEL_ALIAS,
        "expected_model": DEFAULT_MODEL,
        "model_observed": sorted(served),
        "model_valid": model_valid,
        "usage": audit["usage"],
        "langchain_usage": {
            key: value for key, value in callback.totals.as_dict().items() if "cost" not in key
        },
        "pricing": "unavailable; legacy DeepSeek tariff scenarios do not price 6luna",
        "model_calls": audit["artifacts"],
        "model_artifact_count": audit["artifact_count"],
        "model_request_count": audit["model_calls"],
        "model_artifact_errors": audit["artifact_errors"],
        "session_isolation_valid": audit["session_isolation_valid"],
        "reason_metrics": reasons,
        "ranking_metrics": rankings,
        "queries": queries,
        "missing_query_states": missing_query_states,
        "tool_calls": parent_calls,
        "parent_tool_calls": len(parent_calls),
        "source_tool_calls": source_actions,
        "total_tool_calls": len(parent_calls) + len(source_actions),
        "retrieval_actions": retrieval_actions,
        "secret_leak": secret_leak(visible, []),
        "transport": TRANSPORT,
        "transport_boundary": TRANSPORT_NOTE,
        "implementation_hash": implementation_hash,
        "request_sha256": request_sha256,
        "gold_sent_to_model": False,
        "runtime_dir": str(runtime.relative_to(root)),
        "db_path": str((runtime / "app.db").relative_to(root)),
        "model_audit_dir": str((runtime / "model-calls").relative_to(root)),
        "public_test_data_only": True,
    }


def failure_record(
    task: dict[str, Any],
    *,
    runtime: Path,
    root: Path,
    implementation_hash: str,
    error: dict[str, Any],
    started_at: str,
    started: float,
) -> dict[str, Any]:
    audit = collect_model_audit(runtime / "model-calls")
    record = build_record(
        task,
        run={},
        queries=[],
        audit=audit,
        callback=BenchmarkUsageCallback(),
        runtime=runtime,
        root=root,
        implementation_hash=implementation_hash,
        request_sha256=None,
        started_at=started_at,
        started=started,
        error=error,
        missing_query_states=[],
    )
    record["response_text"] = ""
    record["visible_output"] = ""
    return record


def _error_from_exception(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, TimeoutError):
        return {"code": "BENCHMARK_TIMEOUT", "message": "product run timed out"}
    if isinstance(exc, httpx.HTTPError):
        return {"code": "PRODUCT_HTTP_ERROR", "message": type(exc).__name__}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return {"code": code, "message": _redact(str(exc))}
    return {"code": "PRODUCT_RUNNER_ERROR", "message": _redact(str(exc)) or type(exc).__name__}


async def _settle_failed_run(
    client: httpx.AsyncClient, app, tenant: str, run: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str] | None]:
    run_id = run["run_id"]
    path = f"/api/v1/agent/runs/{run_id}"
    headers = {"X-Tenant-ID": tenant}
    try:
        async with asyncio.timeout(CASE_CLEANUP_TIMEOUT_S):
            if run["status"] not in TERMINAL_RUN_STATES:
                response = await client.post(f"{path}/cancel", headers=headers)
                run = response.raise_for_status().json()
            await app.state.agent_runner.wait_for_exit(run_id)
            saved = await asyncio.to_thread(app.state.store.get_agent_run, tenant, run_id)
            if saved is None:
                return run, {"code": "BENCHMARK_CLEANUP_INCOMPLETE", "message": "Missing Agent run"}
            run = saved.model_dump(mode="json")
            query_ids = [query["created"]["query_id"] for query in run["queries"]]
            coordinator = app.state.coordinator
            await asyncio.gather(
                *(
                    coordinator.wait(query_id, timeout_s=CASE_CLEANUP_TIMEOUT_S)
                    for query_id in query_ids
                )
            )
            settled = not coordinator.has_live_searches(query_ids)
            if run["status"] in TERMINAL_RUN_STATES and settled:
                return run, None
            return run, {
                "code": "BENCHMARK_CLEANUP_INCOMPLETE",
                "message": "Case work is not terminal",
            }
    except TimeoutError:
        return run, {"code": "BENCHMARK_CLEANUP_TIMEOUT", "message": "Case work did not settle"}
    except Exception as exc:
        return run, {"code": "BENCHMARK_CLEANUP_ERROR", "message": type(exc).__name__}


async def run_case(
    task: dict[str, Any],
    *,
    root: Path,
    runtime: Path,
    corpus: dict[str, Any],
    index: EmbeddingIndex,
    implementation_hash: str,
    shared_async: asyncio.Semaphore,
    shared_sync: threading.BoundedSemaphore,
    gateway_override: Any = None,
    app_builder: Callable[..., tuple] | None = None,
    audit_collector: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    case = task["case_id"]
    started_at = utc_now_iso()
    started = time.monotonic()
    tenant = f"luna-product-{case}"
    payload, request_sha256 = request_payload(task)
    save_new(
        runtime / "request.json",
        {
            "schema": "scifact-luna-product-request-v1",
            "case_id": case,
            "public_test_data": True,
            "gold_sent_to_model": False,
            "request": payload,
            "request_sha256": request_sha256,
        },
    )
    settings = await asyncio.to_thread(product_settings, runtime, task)
    adapter = await asyncio.to_thread(build_product_adapter, settings, corpus, index, task)
    gateway = gateway_override
    if gateway is None:
        gateway = build_kimi_gateway(
            timeout_s=MODEL_TIMEOUT_S,
            max_concurrency=MAX_PROCESS_MODEL_CALLS,
            artifact_root=runtime / "model-calls",
        )
        gateway._async_semaphore = shared_async
        gateway._sync_semaphore = shared_sync
    builder = app_builder or build_product_app
    collect_audit = audit_collector or collect_model_audit
    callback = BenchmarkUsageCallback()
    app = None
    run: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    cleanup_error: dict[str, str] | None = None
    missing_query_states: list[str] = []
    try:
        app, _, _ = await asyncio.to_thread(builder, settings, adapter, gateway, callback)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://luna-product.local", timeout=30
        ) as client:
            submission_key = uuid.uuid4().hex
            headers = {"X-Tenant-ID": tenant}
            try:
                async with asyncio.timeout(AGENT_TIMEOUT_MS / 1000 + AGENT_POLL_GRACE_S):
                    response = await client.post(
                        "/api/v1/agent/runs",
                        json=payload,
                        headers=headers | {"Idempotency-Key": submission_key},
                    )
                    run = response.raise_for_status().json()
                    run_path = f"/api/v1/agent/runs/{run['run_id']}"
                    while run["status"] not in TERMINAL_RUN_STATES:
                        response = await client.get(run_path, headers=headers)
                        run = response.raise_for_status().json()
                        if run["status"] not in TERMINAL_RUN_STATES:
                            await asyncio.sleep(0.2)
            except Exception as exc:
                error = _error_from_exception(exc)
            finally:
                if not run:
                    try:
                        async with asyncio.timeout(CASE_CLEANUP_TIMEOUT_S):
                            saved = await asyncio.to_thread(
                                app.state.store.get_agent_run_by_key, tenant, submission_key
                            )
                        if saved is not None:
                            run = saved.model_dump(mode="json")
                    except Exception as exc:
                        cleanup_error = {
                            "code": "BENCHMARK_SUBMISSION_LOOKUP_FAILED",
                            "message": type(exc).__name__,
                        }
                if run and (run.get("status") != "completed" or error is not None):
                    run, cleanup_error = await _settle_failed_run(client, app, tenant, run)
        if run and run.get("status") != "completed" and error is None:
            error = run.get("error") or {
                "code": "AGENT_RUN_NOT_COMPLETED",
                "message": str(run.get("status")),
            }
        queries: list[dict[str, Any]] = []
        for agent_query in run.get("queries", []):
            query_id = agent_query.get("created", {}).get("query_id")
            state = (
                app.state.search_service.state(tenant, query_id)
                if app is not None and query_id
                else None
            )
            if state is None:
                if query_id:
                    missing_query_states.append(query_id)
                continue
            queries.append(output_details(state))
        audit = await asyncio.to_thread(collect_audit, runtime / "model-calls")
        record = build_record(
            task,
            run=run,
            queries=queries,
            audit=audit,
            callback=callback,
            runtime=runtime,
            root=root,
            implementation_hash=implementation_hash,
            request_sha256=request_sha256,
            started_at=started_at,
            started=started,
            error=error,
            missing_query_states=missing_query_states,
        )
        if cleanup_error is not None:
            record["cleanup_error"] = cleanup_error
        return record
    finally:
        if app is not None:
            try:
                await app.state.citation_texts.aclose()
            except Exception:
                logger.warning("failed to close product citation service", exc_info=True)
            try:
                await asyncio.to_thread(app.state.manuscripts.close)
            except Exception:
                logger.warning("failed to close product manuscript service", exc_info=True)


def new_attempt_dir(root: Path, case: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / "product-runtime" / case / f"{stamp}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def load_reusable_record(
    path: Path, *, resume: bool, implementation_hash: str
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if not resume:
        raise FileExistsError("existing product outputs require --resume or a new run name")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("implementation_hash") != implementation_hash:
        raise ValueError("product record implementation changed; refusing to reuse it")
    if value.get("system") != "product" or value.get("schema") != SCHEMA:
        raise ValueError("existing product record has an unexpected schema/system")
    return value


def product_implementation_hash(root: Path, corpus: dict[str, Any]) -> str:
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    frozen_plan = {key: value for key, value in plan.items() if key != "created_at"}
    return sha256_json(
        {
            "product_runner": file_hash(Path(__file__)),
            "kimi_seam": file_hash(ROOT / "tools/kimi_chat_model.py"),
            "quality_artifact_hash": artifact_hash(),
            "gap_data": file_hash(ROOT / "tools/gap_benchmark/data.py"),
            "gap_retest": file_hash(ROOT / "tools/luna_gap_retest.py"),
            "gap_tasks": file_hash(SUITE_ROOT / "tasks.json"),
            "gap_protocol": file_hash(SUITE_ROOT / "protocol.md"),
            "plan": frozen_plan,
            "corpus_sha256": corpus["corpus_sha256"],
        }
    )


def product_contract(root: Path, suite: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any]:
    if len(suite["tasks"]) != EXPECTED_CASES:
        raise ValueError(f"frozen product retest requires {EXPECTED_CASES} tasks")
    if len(corpus["records"]) != EXPECTED_RECORDS:
        raise ValueError(f"frozen product retest requires {EXPECTED_RECORDS} records")
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    if plan.get("baseline_model_alias") != DEFAULT_MODEL_ALIAS:
        raise ValueError("frozen plan does not use the expected 6luna alias")
    if plan.get("expected_actual_model") != DEFAULT_MODEL:
        raise ValueError("frozen plan does not use the expected actual model")
    if int(plan.get("max_parallel_subjects", 0)) > MAX_PARALLEL_SUBJECTS:
        raise ValueError("frozen plan exceeds the allowed subject concurrency")
    index = require_prebuilt_index(
        corpus, product_settings(root / "product-runtime" / "_preflight", suite["tasks"][0])
    )
    return {
        "schema": CONTRACT_SCHEMA,
        "implementation_hash": product_implementation_hash(root, corpus),
        "model_alias": DEFAULT_MODEL_ALIAS,
        "expected_model": DEFAULT_MODEL,
        "provider": DEFAULT_PROVIDER,
        "transport": TRANSPORT,
        "transport_boundary": TRANSPORT_NOTE,
        "tasks": len(suite["tasks"]),
        "corpus_records": len(corpus["records"]),
        "corpus_sha256": corpus["corpus_sha256"],
        "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "protocol_sha256": file_hash(SUITE_ROOT / "protocol.md"),
        "e5_cache": str(index.cache_path.relative_to(ROOT)),
        "e5_cache_sha256": file_hash(index.cache_path),
        "limits": {
            "timeout_seconds": AGENT_TIMEOUT_MS // 1000,
            "steps": AGENT_RECURSION_LIMIT,
            "parent_tools": AGENT_MAX_TOOL_CALLS,
            "max_parallel_subjects": MAX_PARALLEL_SUBJECTS,
            "max_process_model_calls": MAX_PROCESS_MODEL_CALLS,
        },
        "gold_sent_to_model": False,
        "input_mapping": REQUEST_MAPPING_VERSION,
        "public_test_data_only": True,
        "created_at": utc_now_iso(),
    }


def ensure_product_contract(root: Path, contract: dict[str, Any]) -> None:
    path = root / "product-contract.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        keys = [key for key in contract if key != "created_at"]
        if {key: previous.get(key) for key in keys} != {key: contract[key] for key in keys}:
            raise ValueError("frozen product contract changed; choose a new run name")
        return
    save_new(path, contract)


def prepare(name: str) -> dict[str, Any]:
    root, suite, corpus = prepare_run(name)
    contract = product_contract(root, suite, corpus)
    return {
        "prepared_without_model_calls": True,
        "name": name,
        "root": str(root.relative_to(ROOT)),
        "tasks": contract["tasks"],
        "corpus_records": contract["corpus_records"],
        "corpus_sha256": contract["corpus_sha256"],
        "model_alias": contract["model_alias"],
        "expected_model": contract["expected_model"],
        "e5_cache": contract["e5_cache"],
        "e5_cache_sha256": contract["e5_cache_sha256"],
        "limits": contract["limits"],
        "implementation_hash": contract["implementation_hash"],
        "transport": contract["transport"],
        "transport_boundary": contract["transport_boundary"],
    }


async def run_products(
    name: str, *, resume: bool, concurrency: int = MAX_PARALLEL_SUBJECTS
) -> dict[str, Any]:
    if concurrency not in {1, 2}:
        raise ValueError("subject concurrency must be 1 or 2")
    root, suite, corpus = await asyncio.to_thread(prepare_run, name)
    contract = await asyncio.to_thread(product_contract, root, suite, corpus)
    await asyncio.to_thread(ensure_product_contract, root, contract)
    frozen_hash = contract["implementation_hash"]
    preflight_settings = await asyncio.to_thread(
        product_settings, root / "product-runtime" / "_preflight", suite["tasks"][0]
    )
    index = await asyncio.to_thread(require_prebuilt_index, corpus, preflight_settings)
    records: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for task in suite["tasks"]:
        path = root / "product" / f"{task['case_id']}.json"
        previous = await asyncio.to_thread(
            load_reusable_record, path, resume=resume, implementation_hash=frozen_hash
        )
        if previous is not None:
            records[task["case_id"]] = previous
        else:
            pending.append(task)
    case_semaphore = asyncio.Semaphore(concurrency)
    shared_async = asyncio.Semaphore(MAX_PROCESS_MODEL_CALLS)
    shared_sync = threading.BoundedSemaphore(MAX_PROCESS_MODEL_CALLS)

    async def one(task: dict[str, Any]) -> dict[str, Any]:
        case = task["case_id"]
        path = root / "product" / f"{case}.json"
        case_started_at = utc_now_iso()
        case_started = time.monotonic()
        runtime = await asyncio.to_thread(new_attempt_dir, root, case)
        async with case_semaphore:
            try:
                record = await run_case(
                    task,
                    root=root,
                    runtime=runtime,
                    corpus=corpus,
                    index=index,
                    implementation_hash=frozen_hash,
                    shared_async=shared_async,
                    shared_sync=shared_sync,
                )
            except Exception as exc:
                record = await asyncio.to_thread(
                    failure_record,
                    task,
                    runtime=runtime,
                    root=root,
                    implementation_hash=frozen_hash,
                    error=_error_from_exception(exc),
                    started_at=case_started_at,
                    started=case_started,
                )
        try:
            await asyncio.to_thread(save_new, path, record)
        except FileExistsError:
            previous = await asyncio.to_thread(
                load_reusable_record, path, resume=resume, implementation_hash=frozen_hash
            )
            if previous is None:
                raise
            record = previous
        print(
            json.dumps(
                {
                    "case": case,
                    "completed": record["completed"],
                    "model_valid": record["model_valid"],
                    "model_calls": record["model_request_count"],
                    "reason_generated": record["reason_metrics"]["generated"],
                    "reason_total": record["reason_metrics"]["total"],
                    "ranking_completed": record["ranking_metrics"]["completed"],
                    "ranking_total": record["ranking_metrics"]["total"],
                    "error": record["error"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return record

    if pending:
        await asyncio.gather(*(one(task) for task in pending))
    for task in suite["tasks"]:
        path = root / "product" / f"{task['case_id']}.json"
        if task["case_id"] not in records:
            record = await asyncio.to_thread(
                load_reusable_record, path, resume=True, implementation_hash=frozen_hash
            )
            if record is None:
                raise RuntimeError(f"product record missing after run: {task['case_id']}")
            records[task["case_id"]] = record
    ordered = [records[task["case_id"]] for task in suite["tasks"]]
    after_hash = await asyncio.to_thread(product_implementation_hash, root, corpus)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "name": name,
        "created_at": utc_now_iso(),
        "cases": len(ordered),
        "completed": sum(record["completed"] for record in ordered),
        "model_valid": sum(record["model_valid"] for record in ordered),
        "model_request_count": sum(record["model_request_count"] for record in ordered),
        "reason_generated": sum(record["reason_metrics"]["generated"] for record in ordered),
        "reason_total": sum(record["reason_metrics"]["total"] for record in ordered),
        "ranking_completed": sum(record["ranking_metrics"]["completed"] for record in ordered),
        "ranking_total": sum(record["ranking_metrics"]["total"] for record in ordered),
        "implementation_hash": frozen_hash,
        "implementation_changed_during_run": after_hash != frozen_hash,
        "transport": TRANSPORT,
        "transport_boundary": TRANSPORT_NOTE,
        "interpretation": (
            "Engineering/completion only. The paired gold evaluation remains in "
            "tools.luna_gap_retest.evaluate; no round/cost quality gate was added."
        ),
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / f"product-summary-{stamp}-{uuid.uuid4().hex}.json"
    await asyncio.to_thread(save_new, path, summary)
    if after_hash != frozen_hash:
        raise ValueError(
            "product implementation changed during the frozen run; records retained but invalid"
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Product-side 6luna SciFact dev retest")
    parser.add_argument("command", choices=["prepare", "run"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, default=MAX_PARALLEL_SUBJECTS)
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        result = prepare(arguments.name)
    else:
        result = asyncio.run(
            run_products(
                arguments.name,
                resume=arguments.resume,
                concurrency=arguments.concurrency,
            )
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
