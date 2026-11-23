"""Run the unchanged SciFact product path with the selected OpenCode model."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from pathlib import Path

from tools import luna_product_retest as shared
from tools.benchmark.core import file_hash, withhold_secrets
from tools.gap_benchmark import opencode_native
from tools.gap_benchmark.data import save_new
from tools.gap_benchmark.opencode_baseline import MODELS, freeze, prepare
from tools.gap_benchmark.opencode_native import OpenCodeGateway, configured_route

from citation_tool.jcs import sha256_json
from citation_tool.listwise import DiscoveryModels
from citation_tool.reasons import DeepSeekReasonGenerator


def build_app(settings, adapter, gateway, callback):
    from citation_tool.api import create_app

    parent = gateway.agent_model()
    source = gateway.model_for("source")
    rank = gateway.rerank_model()
    reasoning = gateway.reason_model()
    for model in (parent, source, rank, reasoning):
        model.callbacks = [callback]
    provider, model_id = gateway.config.provider, gateway.config.model
    reasons = DeepSeekReasonGenerator(
        gateway, model=reasoning, max_inflight=2, timeout_ms=int(shared.MODEL_TIMEOUT_S * 1000)
    )
    discovery = DiscoveryModels(source, rank, provider, model_id, shared.MODEL_TIMEOUT_S)
    app = create_app(
        settings,
        adapters={adapter.library.library_id: adapter},
        reason_generator=reasons,
        agent_model=parent,
        discovery_models=discovery,
    )
    reasons._cache = app.state.store
    if (
        app.state.agent_runner.model_name != model_id
        or app.state.search_service.reason_provider != provider
    ):
        raise ValueError("product is not using the expected OpenCode model")
    return app, reasons, discovery


def implementation(root: Path, corpus: dict) -> str:
    return sha256_json(
        {
            "product": shared.product_implementation_hash(root, corpus),
            "transport": file_hash(Path(opencode_native.__file__)),
            "runner": file_hash(Path(__file__)),
        }
    )


async def case(task: dict, *, root: Path, corpus: dict, index, fingerprint: str, route) -> dict:
    runtime = shared.new_attempt_dir(root, task["case_id"])
    gateway = OpenCodeGateway(route)
    try:
        record = await shared.run_case(
            task,
            root=root,
            runtime=runtime,
            corpus=corpus,
            index=index,
            implementation_hash=fingerprint,
            shared_async=asyncio.Semaphore(2),
            shared_sync=threading.BoundedSemaphore(2),
            gateway_override=gateway,
            app_builder=build_app,
            audit_collector=lambda _: gateway.audit(),
        )
    finally:
        await gateway.aclose()
    audit = gateway.audit()
    served = sorted(set(audit["served_models"]) | set(record["model_observed"]))
    save_new(runtime / "parent-trace.json", gateway.parent_turns)
    record.update(
        split="development",
        model_requested=MODELS[route.model],
        expected_model=route.model,
        model_observed=served,
        model_valid=bool(audit["model_valid"] and served == [route.model]),
        session_isolation_valid=audit["session_isolation_valid"] and bool(record["agent_run_id"]),
        model_calls=audit["artifacts"],
        model_request_count=audit["model_calls"],
        parent_trace_path=str((runtime / "parent-trace.json").relative_to(root)),
        transport="OpenCode native " + route.protocol,
        transport_boundary=(
            "Kimi Code stream vs product native nonstream; same configured route and model"
        ),
        model_audit_dir=None,
        pricing="not scored",
        relay_session=gateway.session,
    )
    record, leaked = withhold_secrets(record, [route.api_key])
    record["secret_leak"] = bool(leaked or record["secret_leak"])
    if leaked:
        record.update(completed=False, response_text="[WITHHELD]", visible_output="[WITHHELD]")
    return record


async def run(name: str, model: str, *, resume: bool, pilot: bool) -> None:
    root, suite, corpus, plan = prepare(name, model)
    fingerprint = implementation(root, corpus)
    index = shared.require_prebuilt_index(corpus, shared.product_settings(root, suite["tasks"][0]))
    route = await asyncio.to_thread(configured_route, model)
    contract = {
        "model": model,
        "provider": route.provider,
        "transport": route.protocol,
        "plan_sha256": sha256_json(plan),
        "implementation_hash": fingerprint,
        "e5_cache_sha256": file_hash(index.cache_path),
        "token_limits": opencode_native.TOKEN_LIMITS,
        "native_reasoning": "configured endpoint defaults; opaque continuation kept in memory only",
        "measurement_boundary": "same model and route; not equal inference or tool harness",
    }
    freeze(root / "product-contract.json", contract)
    for original in suite["tasks"][:1] if pilot else suite["tasks"]:
        path = root / "product" / f"{original['case_id']}.json"
        if shared.load_reusable_record(path, resume=resume, implementation_hash=fingerprint):
            continue
        task = {key: value for key, value in original.items() if key != "expect"}
        row = await case(
            task, root=root, corpus=corpus, index=index, fingerprint=fingerprint, route=route
        )
        save_new(path, row)
        print(
            json.dumps(
                {
                    **{
                        key: row[key]
                        for key in (
                            "case_id",
                            "completed",
                            "model_valid",
                            "model_request_count",
                            "wall_time_ms",
                        )
                    },
                    "rank_successes": row["ranking_metrics"]["completed"],
                    "query_count": row["ranking_metrics"]["query_count"],
                    "case_error": (row["error"] or {}).get("code"),
                    "http_failures": [
                        call.get("status_code")
                        for call in row["model_calls"]
                        if call["status"] == "failed"
                    ],
                }
            ),
            flush=True,
        )
        if not row["model_request_count"]:
            raise RuntimeError(
                "no native dispatch; preserved case and stopped systematic runner failure"
            )
    if implementation(root, corpus) != fingerprint:
        raise RuntimeError("product implementation changed during frozen evaluation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", required=True, choices=MODELS)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.name, args.model, resume=args.resume, pilot=args.pilot))


if __name__ == "__main__":
    main()
