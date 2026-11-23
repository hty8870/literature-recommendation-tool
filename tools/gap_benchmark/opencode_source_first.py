"""Paired development pilot for parent access to source evidence before ranking."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path

from tools import luna_product_retest as shared
from tools.benchmark.core import file_hash, withhold_secrets
from tools.gap_benchmark.data import RUNTIME, load_data, save_new
from tools.gap_benchmark.metrics import aggregate, score_record
from tools.gap_benchmark.opencode_baseline import MODELS, freeze
from tools.gap_benchmark.opencode_native import TOKEN_LIMITS, OpenCodeGateway, configured_route
from tools.gap_benchmark.opencode_product import build_app
from tools.quality_benchmark.core import artifact_hash

from citation_tool.jcs import sha256_json

TASK_IDS = ("sf-873", "sf-1029", "sf-1086")
BASELINE = RUNTIME / "opencode-retests/opencode-flash-retest-v1"


async def one(
    task,
    original,
    *,
    root,
    corpus,
    index,
    fingerprint,
    route,
    hide_rank_rationale=False,
    source_evidence_first=True,
    evidence_delta=False,
    abstract_references=False,
    parent_max_tokens: int = TOKEN_LIMITS["parent"],
    request_slots: asyncio.Semaphore | None = None,
    observe_adapter=None,
    observe_gateway=None,
):
    runtime = shared.new_attempt_dir(root, task["case_id"])
    gateway = OpenCodeGateway(route, parent_max_tokens=parent_max_tokens)
    if request_slots is not None:
        gateway.slots = request_slots
    if observe_gateway is not None:
        observe_gateway(gateway)
    applications = []

    def builder(settings, adapter, g, callback):
        if observe_adapter is not None:
            observe_adapter(adapter)
        result = build_app(
            replace(
                settings,
                agent_source_evidence_first=source_evidence_first,
                agent_parent_hide_rank_rationale=hide_rank_rationale,
                agent_parent_evidence_delta=evidence_delta,
                agent_parent_abstract_references=abstract_references,
            ),
            adapter,
            g,
            callback,
        )
        applications.append(result[0])
        return result

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
            app_builder=builder,
            audit_collector=lambda _: gateway.audit(),
        )
        record.update(
            model_observed=gateway.audit()["served_models"],
            expected_model=route.model,
            model_requested=MODELS[route.model],
            session_isolation_valid=bool(record["agent_run_id"]),
            model_valid=bool(
                record["completed"] and gateway.audit()["served_models"] == [route.model]
            ),
            parent_trace_path=str((runtime / "parent-trace.json").relative_to(root)),
            parent_replied_with_search_open=any(
                q["search_state"] == "running" for q in record["queries"]
            ),
        )
        record, leaked = withhold_secrets(record, [route.api_key])
        record["secret_leak"] = bool(leaked or record["secret_leak"])
        scored = score_record(record, original, corpus)
        save_new(
            runtime / "parent-delivery.json",
            {
                **record,
                "assessment": scored["assessment"],
                "verification_boundary": "parent valid; separately await background ranking audit",
            },
        )
        save_new(runtime / "parent-trace.json", gateway.parent_turns)
        print(
            json.dumps(
                {
                    "case_id": task["case_id"],
                    "completed": record["completed"],
                    "coverage": scored["assessment"]["correct_direction_evidence_coverage"],
                    "format": scored["assessment"]["format_valid"],
                    "seconds": record["wall_time_ms"] / 1000,
                    "replied_while_ranking": record["parent_replied_with_search_open"],
                }
            ),
            flush=True,
        )
        if applications:
            service = applications[0].state.search_service
            keys = [q["query_id"] for q in record["queries"]]
            await asyncio.gather(*(service.coordinator.wait(q, timeout_s=900) for q in keys))
            states = [service.state(f"luna-product-{task['case_id']}", q) for q in keys]
            final = [shared.output_details(state) for state in states if state is not None]
            audit = gateway.audit()
            record.update(
                queries=final,
                ranking_metrics=shared.ranking_metrics(final),
                reason_metrics=shared.reason_metrics(final),
                model_calls=audit["artifacts"],
                model_request_count=audit["model_calls"],
                model_valid=audit["model_valid"],
                session_isolation_valid=bool(
                    record["agent_run_id"] and audit["session_isolation_valid"]
                ),
                parent_coverage_at_delivery=scored["assessment"][
                    "correct_direction_evidence_coverage"
                ],
            )
        return withhold_secrets(record, [route.api_key])[0]
    finally:
        await gateway.aclose()


async def run(name: str, model: str, hide_rank_rationale: bool, resume: bool):
    suite, corpus = load_data()
    root = RUNTIME / "agent-source-ready" / name / model
    originals = {item["case_id"]: item for item in suite["tasks"] if item["case_id"] in TASK_IDS}
    index = shared.require_prebuilt_index(
        corpus, shared.product_settings(root, originals[TASK_IDS[0]])
    )
    route = await asyncio.to_thread(configured_route, model)
    fingerprint = sha256_json({"product": artifact_hash(), "runner": file_hash(Path(__file__))})
    freeze(
        root / "plan.json",
        {
            "cases": list(TASK_IDS),
            "product_code_hash": fingerprint,
            "model": model,
            "same_candidate_pipeline": True,
            "hide_rank_rationale_receipt": hide_rank_rationale,
            "source_evidence_first": True,
            "e5_cache_sha256": file_hash(index.cache_path),
            "controls": str((BASELINE / model / "product-contract.json").resolve()),
            "scope": "previously inspected development families; same official scoring",
        },
    )
    compared, variant, missing = [], [], []
    for case in TASK_IDS:
        original = originals[case]
        path = root / "product" / f"{case}.json"
        if path.exists():
            if not resume:
                raise FileExistsError(path)
            row = json.loads(path.read_text(encoding="utf-8"))
        else:
            task = {k: v for k, v in original.items() if k != "expect"}
            row = await one(
                task,
                original,
                root=root,
                corpus=corpus,
                index=index,
                fingerprint=fingerprint,
                route=route,
                hide_rank_rationale=hide_rank_rationale,
            )
            save_new(path, row)
        scored = score_record(row, original, corpus)
        variant.append(scored)
        baseline = BASELINE / model / "product" / path.name
        if baseline.is_file():
            previous = score_record(
                json.loads(baseline.read_text(encoding="utf-8")), original, corpus
            )
            compared.append(
                {
                    "case_id": case,
                    "baseline": previous["assessment"],
                    "source_ready": scored["assessment"],
                    "artifact": row["ranking_metrics"],
                    "parent_coverage_if_rank_failed": row.get("parent_coverage_at_delivery"),
                }
            )
        else:
            missing.append(case)
    save_new(
        root / "assessment.json",
        {"aggregate": aggregate(variant), "case_pairs": compared, "control_pending": missing},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name", required=True, choices=("source-ready-v1", "source-ready-no-rationale-v2")
    )
    parser.add_argument("--model", required=True, choices=MODELS)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.name, args.model, args.name.endswith("v2"), args.resume))


if __name__ == "__main__":
    main()
