"""Live Luna regression: a conversation changes scope and then reaffirms the first scope."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from tools import luna_native_product_retest as native
from tools import luna_product_retest as product
from tools.benchmark.core import BenchmarkUsageCallback
from tools.gap_benchmark.data import load_data, save_new
from tools.luna_credentials import MODEL, authorized_config

from citation_tool.jcs import sha256_json

FIRST = (
    "请在选定的摘要库检索睡眠时长与注意力的实证研究，本轮只看成年人。"
    "按实际找到的范围简短回复；无直接证据就如实说明。"
)
CHANGE = "换成青少年，不要成年人。请按更新后的年龄范围检索同一主题。按实际找到的范围简短回复。"


async def _turn(client, endpoint: str, payload: dict, number: int) -> dict:
    response = await client.post(
        endpoint, json=payload, headers={"Idempotency-Key": f"revision-{number}"}
    )
    response.raise_for_status()
    run_id = response.json()["run_id"]
    async with asyncio.timeout(910):
        while True:
            current = await client.get(f"/api/v1/agent/runs/{run_id}")
            current.raise_for_status()
            run = current.json()
            if run["status"] in {"completed", "failed", "cancelled"}:
                return run
            await asyncio.sleep(0.25)


async def _artifact(client, query_id: str | None) -> dict | None:
    if query_id is None:
        return None
    response = await client.get(f"/api/v1/searches/{query_id}")
    response.raise_for_status()
    state = response.json()
    return {
        "snapshot": state["session"]["final_snapshot_id"],
        "rankings": sha256_json(state["candidate_rankings"]),
        "results": sha256_json(state["results"]),
        "candidates": sha256_json(state["candidate_diagnostics"]),
    }


async def run(runtime: Path) -> dict:
    await asyncio.to_thread(runtime.mkdir, parents=True, exist_ok=False)
    started = time.monotonic()
    _, corpus = await asyncio.to_thread(load_data)
    task = {"retriever": "hybrid", "dense_threshold": 0.8}
    settings = product.product_settings(runtime, task)
    index = await asyncio.to_thread(product.require_prebuilt_index, corpus, settings)
    adapter = await asyncio.to_thread(product.build_product_adapter, settings, corpus, index, task)
    audit_dir = runtime / "model-calls"
    audit = native.NativeModelAudit(audit_dir, native.NativeModelSlots())
    config = await asyncio.to_thread(authorized_config)
    gateway = native.NativeLunaGateway(config, audit)
    app, _, _ = native.build_product_app(settings, adapter, gateway, BenchmarkUsageCallback())
    turns = []
    previous = None
    conversation = None
    unchanged = False
    error = None
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                timeout=30,
            ) as client,
        ):
            for number, message in enumerate((FIRST, CHANGE, FIRST), 1):
                endpoint = (
                    f"/api/v1/conversations/{conversation}/turns"
                    if conversation
                    else "/api/v1/agent/runs"
                )
                payload = (
                    {"message": message, "expected_revision": number - 1}
                    if conversation
                    else {"input_type": "abstract", "abstract": message, "library_ids": ["scifact"]}
                )
                turn = await _turn(client, endpoint, payload, number)
                turns.append(turn)
                conversation = turn["conversation_id"]
                if number == 1:
                    previous = await _artifact(client, turn["latest_query_id"])
            unchanged = bool(
                previous and previous == await _artifact(client, turns[0]["latest_query_id"])
            )
    except Exception as exc:
        error = type(exc).__name__
    finally:
        await gateway.aclose()
    calls = native.collect_model_audit(audit_dir)
    checks = {
        "three_completed_turns": len(turns) == 3
        and all(turn["status"] == "completed" for turn in turns),
        "native_luna": calls["model_valid"]
        and all(turn["served_model"] == MODEL for turn in turns),
        "latest_revision_restored": len(turns) == 3
        and (turns[-1]["evaluation_anchor"] or {}).get("claim_text", "").endswith(FIRST),
        "real_queries": len(turns) == 3
        and len({turn["latest_query_id"] for turn in turns if turn["latest_query_id"]}) == 3,
        "old_artifact_unchanged": unchanged,
    }
    result = {
        "passed": not error and all(checks.values()),
        "mechanism_only": True,
        "corpus_sha256": corpus["corpus_sha256"],
        "checks": checks,
        "error_type": error,
        "model_observed": calls["served_models"],
        "model_calls": calls["model_calls"],
        "elapsed_s": round(time.monotonic() - started, 2),
        "turns": [
            {
                key: turn[key]
                for key in (
                    "status",
                    "summary",
                    "error",
                    "evaluation_anchor",
                    "latest_query_id",
                    "served_model",
                )
            }
            for turn in turns
        ],
    }
    save_new(runtime / "assessment.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(run(args.output_directory))
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "turns"}, ensure_ascii=False
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
