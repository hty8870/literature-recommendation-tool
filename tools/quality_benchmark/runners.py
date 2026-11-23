from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
from tools.benchmark.core import (
    MODEL_ID,
    BenchmarkUsageCallback,
    elapsed_ms,
    secret_leak,
    utc_now_iso,
)
from tools.benchmark.runners import _gateway, run_kimi

from citation_tool.api import create_app
from citation_tool.config import LibraryConfig, Settings
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.reasons import DeepSeekReasonGenerator
from citation_tool.semantic import SemanticMetadataAdapter
from citation_tool.store import Store

from .core import (
    EMBEDDING_CACHE_DIR,
    QUALITY_RUNTIME,
    output_details,
    product_request,
    visible_output,
)


async def run_product(
    task: dict[str, Any],
    run_name: str,
    corpus: dict[str, Any],
    *,
    runtime_root: Path = QUALITY_RUNTIME,
    library_override: LibraryConfig | None = None,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    started = time.monotonic()
    library = library_override or LibraryConfig(
        "abiotech-real",
        "aBiotech 真实题录与摘要",
        "fast",
        1,
        simulation=False,
    )
    runtime = runtime_root / run_name / "product" / task["case_id"]
    runtime.mkdir(parents=True, exist_ok=True)
    settings = replace(
        Settings(),
        db_path=runtime / "app.db",
        libraries=(library,),
        default_soft_deadline_ms=180_000,
    )
    settings.validate()
    gateway = _gateway(settings)
    callback = BenchmarkUsageCallback()
    agent_model = gateway.agent_model()
    reason_model = gateway.reason_model()
    agent_model.callbacks = [callback]
    reason_model.callbacks = [callback]
    reasons = DeepSeekReasonGenerator(
        gateway,
        max_inflight=settings.model_max_inflight,
        timeout_ms=settings.model_timeout_ms,
        model=reason_model,
        cache=Store(settings.db_path),
    )
    adapter = (
        SemanticMetadataAdapter(
            library,
            corpus["records"],
            settings=replace(settings, embedding_cache_dir=EMBEDDING_CACHE_DIR),
            dense_threshold=task["dense_threshold"],
        )
        if task["retriever"] == "hybrid"
        else MetadataCorpusAdapter(library, corpus["records"])
    )
    app = create_app(
        settings,
        adapters={library.library_id: adapter},
        reason_generator=reasons,
        agent_model=agent_model,
    )
    if app.state.search_service.reason_provider != "deepseek":
        raise RuntimeError("reason model instrumentation is not configured correctly")
    tenant = f"quality-{task['case_id']}"
    transport = httpx.ASGITransport(app=app)
    run: dict[str, Any] = {}
    error = None
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://quality.local", timeout=30
        ) as client:
            response = await client.post(
                "/api/v1/agent/runs",
                json=product_request(task).model_dump(mode="json", exclude_none=True),
                headers={"X-Tenant-ID": tenant},
            )
            response.raise_for_status()
            run_id = response.json()["run_id"]
            deadline = time.monotonic() + settings.agent_timeout_ms / 1000 + 10
            while time.monotonic() < deadline:
                response = await client.get(
                    f"/api/v1/agent/runs/{run_id}", headers={"X-Tenant-ID": tenant}
                )
                response.raise_for_status()
                run = response.json()
                if run["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.05)
            else:
                error = {"code": "BENCHMARK_TIMEOUT"}
    except Exception as exc:
        error = {"code": "BENCHMARK_RUNNER_ERROR", "message": type(exc).__name__}
    queries = []
    for agent_query in run.get("queries", []):
        state = app.state.search_service.state(tenant, agent_query["created"]["query_id"])
        if state is not None:
            queries.append(output_details(state))
    reasons = [
        item["reason"]
        for query in queries
        for item in query["candidate_diagnostics"]
        if item.get("reason")
    ]
    served = set(callback.totals.served_models)
    if run.get("served_model"):
        served.add(run["served_model"])
    served.update(reason["served_model"] for reason in reasons if reason.get("served_model"))
    visible = visible_output(run, queries)
    return {
        "system": "product",
        "retriever": adapter.model_versions,
        "case_id": task["case_id"],
        "run_case_id": task["run_case_id"],
        "repetition": task["repetition"],
        "split": task["split"],
        "started_at": started_at,
        "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
        "completed": bool(run and run["status"] == "completed" and not error),
        "response_text": run.get("summary") or "",
        "visible_output": visible,
        "error": error or run.get("error"),
        "agent_run_id": run.get("run_id"),
        "response_grounding": run.get("response_grounding"),
        "design_reviews": run.get("design_reviews", []),
        "model_requested": MODEL_ID,
        "model_observed": sorted(served),
        "model_valid": bool(callback.totals.calls) and served == {MODEL_ID},
        "usage": callback.totals.as_dict(),
        "tool_calls": run.get("tool_calls", []),
        "retrieval_actions": sum(
            len(query.get("workers", []))
            + sum(
                action.get("tool") == "search_source" and action.get("executed", True)
                for worker in query.get("workers", [])
                for action in worker.get("source_actions", [])
            )
            for query in queries
        )
        or max(len(run.get("tool_calls", [])), len(queries)),
        "parent_tool_calls": len(run.get("tool_calls", [])),
        "source_tool_calls": [
            action
            for query in queries
            for worker in query.get("workers", [])
            for action in worker.get("source_actions", [])
        ],
        "total_tool_calls": len(run.get("tool_calls", []))
        + sum(
            len(worker.get("source_actions", []))
            for query in queries
            for worker in query.get("workers", [])
        ),
        "queries": queries,
        "reason_metrics": {
            "count": len(reasons),
            "generated": sum(item["status"] == "generated" for item in reasons),
            "ungrounded_rejected": sum(
                item.get("failure_code") == "MODEL_UNGROUNDED_OUTPUT" for item in reasons
            ),
        },
        "secret_leak": secret_leak(visible, []),
    }


__all__ = ["run_product", "run_kimi", "elapsed_ms"]
