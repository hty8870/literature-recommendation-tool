from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
from tools.benchmark.core import MODEL_ID, empty_usage, secret_leak, utc_now_iso

from citation_tool.entities import AgentRun, SearchStateResponse

from .core import output_details, product_request, visible_output

SNAPSHOT_LIBRARY = "europepmc-snapshot"


async def run_product_http(
    task: dict[str, Any],
    *,
    base_url: str = "http://127.0.0.1:8765",
    expected_index_snapshot: str,
    timeout_ms: int = 900_000,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    tenant = f"quality-{task['case_id']}"
    headers = {"X-Tenant-ID": tenant}
    payload = (
        product_request(task)
        .model_copy(update={"library_ids": [SNAPSHOT_LIBRARY]})
        .model_dump(mode="json", exclude_none=True)
    )
    run: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    queries: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        try:
            while True:
                response = await client.post(
                    "/api/v1/agent/runs",
                    json=payload,
                    headers={**headers, "Idempotency-Key": f"quality-{uuid.uuid4().hex}"},
                )
                if response.status_code != 429:
                    break
                if time.monotonic() + 2 >= deadline:
                    raise TimeoutError
                await asyncio.sleep(2)
            response.raise_for_status()
            run = AgentRun.model_validate(response.json()).model_dump(mode="json")
            while time.monotonic() < deadline:
                response = await client.get(f"/api/v1/agent/runs/{run['run_id']}", headers=headers)
                response.raise_for_status()
                run = AgentRun.model_validate(response.json()).model_dump(mode="json")
                if run["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.2)
            else:
                error = {"code": "BENCHMARK_TIMEOUT"}
        except Exception as exc:
            error = {"code": "BENCHMARK_RUNNER_ERROR", "message": type(exc).__name__}
        for agent_query in run.get("queries", []):
            query_id = agent_query["created"]["query_id"]
            try:
                response = await client.get(f"/api/v1/searches/{query_id}", headers=headers)
                response.raise_for_status()
                state = SearchStateResponse.model_validate(response.json())
                queries.append(output_details(state))
                workers.extend(
                    worker.model_dump(mode="json")
                    for worker in state.workers
                    if worker.library_id == SNAPSHOT_LIBRARY
                )
            except Exception as exc:
                error = {"code": "BENCHMARK_STATE_ERROR", "message": type(exc).__name__}

    contract_errors = []
    if run.get("response_grounding") != "design-attributes-v4":
        contract_errors.append("response_grounding")
    for worker in workers:
        if worker.get("index_snapshot") != expected_index_snapshot:
            contract_errors.append("index_snapshot")
        if worker.get("model_versions", {}).get("dense_status") != "available":
            contract_errors.append("dense_status")
    if any(query["requested_library_ids"] != [SNAPSHOT_LIBRARY] for query in queries):
        contract_errors.append("fixed_snapshot_scope")
    if contract_errors:
        error = {
            "code": "BENCHMARK_CONTRACT_INVALID",
            "fields": sorted(set(contract_errors)),
        }
    reasons = [
        item["reason"]
        for query in queries
        for item in query["candidate_diagnostics"]
        if item.get("reason")
    ]
    served = {value for value in [run.get("served_model")] if value}
    served.update(reason["served_model"] for reason in reasons if reason.get("served_model"))
    visible = visible_output(run, queries)
    usage = empty_usage() | {
        "status": "unavailable",
        "reason": "not_exposed_by_http",
    }
    return {
        "system": "product",
        "retriever": {
            key: value
            for worker in workers
            for key, value in worker.get("model_versions", {}).items()
        },
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
        "model_requested": MODEL_ID,
        "model_observed": sorted(served),
        "model_valid": bool(run.get("served_model")) and served == {MODEL_ID},
        "usage": usage,
        "tool_calls": run.get("tool_calls", []),
        "retrieval_actions": max(len(run.get("tool_calls", [])), len(queries)),
        "total_tool_calls": len(run.get("tool_calls", [])),
        "queries": queries,
        "reason_metrics": {
            "count": len(reasons),
            "generated": sum(item["status"] == "generated" for item in reasons),
            "ungrounded_rejected": sum(
                item.get("failure_code") == "MODEL_UNGROUNDED_OUTPUT" for item in reasons
            ),
        },
        "transport": "production loopback HTTP; persisted AgentRun and SearchStateResponse",
        "snapshot_workers": workers,
        "secret_leak": secret_leak(visible, []),
    }


__all__ = ["run_product_http"]
