"""Repeated 8K/16K paired screen on identical, actually observed Qwen evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from filelock import FileLock
from tools.benchmark.core import file_hash
from tools.gap_benchmark import opencode_budget, opencode_native, opencode_receipt_replay
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import aggregate, mean
from tools.gap_benchmark.opencode_assessment import _read
from tools.gap_benchmark.opencode_baseline import freeze

from citation_tool.jcs import sha256_json

MODEL = opencode_budget.MODEL
CASES = ("sf-179", "sf-1137", "sf-1086", "sf-219")
LIMITS = opencode_budget.BUDGETS
REPETITIONS = 2


def dispatch_order() -> list[tuple[str, int, int]]:
    return [
        (case, repetition, limit)
        for repetition in range(1, REPETITIONS + 1)
        for index, case in enumerate(CASES)
        for limit in (LIMITS if (index + repetition) % 2 else LIMITS[::-1])
    ]


def summarize(rows: dict[tuple[str, int, int], dict]) -> dict:
    if set(rows) != set(dispatch_order()):
        raise ValueError("screen requires every registered budget and repetition")
    replies = [
        metadata["response_id"]
        for row in rows.values()
        for metadata in row["call_metadata"]
        if metadata.get("response_id")
    ]
    if len(set(replies)) != len(replies):
        raise ValueError("provider response reused across isolated arms")
    coverage = "in_pool_direction_evidence_coverage"
    systems = {}
    for cap in LIMITS:
        evidence = [r for (_, _, limit), r in rows.items() if cap == limit]
        systems[str(cap)] = {
            **aggregate(evidence),
            "strict_visible_coverage": mean(r["assessment"][coverage] for r in evidence),
            "max_tokens_stops": sum(r["finish"] == "max_tokens" for r in evidence),
            "off_receipt_cases": sum(bool(r["assessment"]["out_of_pool_ids"]) for r in evidence),
        }
    return {
        "systems": systems,
        "ordered_pairs": [
            {
                "case_id": case,
                "repetition": repetition,
                "arms": {
                    str(cap): {
                        "coverage": row["assessment"][coverage],
                        "format_valid": row["assessment"]["format_valid"],
                        "wrong_directions": row["assessment"]["wrong_official_stances"],
                        **{
                            k: row[k]
                            for k in (
                                "completed",
                                "finish",
                                "error",
                                "output_tokens",
                                "wall_time_ms",
                            )
                        },
                    }
                    for cap in LIMITS
                    for row in [rows[(case, repetition, cap)]]
                },
            }
            for case in CASES
            for repetition in range(1, REPETITIONS + 1)
        ],
    }


async def run(name: str, concurrency: int, *, resume: bool) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", name):
        raise ValueError("invalid run name")
    output = RUNTIME / "opencode-receipt-budget" / name
    with FileLock(output / ".run.lock", timeout=0):
        return await _run(output, concurrency, resume=resume)


async def _run(output: Path, concurrency: int, *, resume: bool) -> dict:
    root = RUNTIME / "opencode-retests" / "opencode-flash-retest-v1" / MODEL
    contract = _read(root / "product-contract.json")
    if contract["model"] != MODEL:
        raise ValueError("source run model does not match")
    suite, corpus = load_data()
    tasks = {t["case_id"]: t for t in suite["tasks"] if t["case_id"] in CASES}
    if len({t["family"] for t in tasks.values()}) != len(CASES):
        raise ValueError("use distinct development families")
    abstracts = {row["work_id"]: row["abstract"] for row in corpus["records"]}
    inputs = {}
    for case in CASES:
        product = _read(root / "product" / f"{case}.json")
        if (
            product["case_id"] != case
            or product["secret_leak"]
            or product["gold_sent_to_model"]
            or not product["public_test_data_only"]
            or product["model_observed"] != [MODEL]
            or product["implementation_hash"] != contract["implementation_hash"]
        ):
            raise ValueError("not a verified public Qwen tool trace")
        trace = (root / product["parent_trace_path"]).resolve()
        if not trace.is_relative_to(root.resolve()):
            raise ValueError("trace outside the frozen run")
        visible, pool = opencode_receipt_replay.actual_observations(_read(trace), abstracts)
        prompt, _ = opencode_receipt_replay.messages(visible, "format", "control")
        inputs[case] = (prompt, pool, sha256_json([m.text for m in prompt]), file_hash(trace))

    order = dispatch_order()
    plan = {
        "schema": "scifact-opencode-repeated-receipt-budget-v1",
        "model": MODEL,
        "scope": "known cases and actual previous receipts; no new retrieval or E5",
        "selection": "two past cap stops, one long-context success, one syntax failure",
        "changed_parameter": "parent max_tokens; prompt and evidence fixed within case",
        "case_concurrency": concurrency,
        "order": [list(row) for row in order],
        "inputs": {
            case: {"prompt_sha256": v[2], "trace_sha256": v[3]} for case, v in inputs.items()
        },
        "gold_sent_to_model": False,
        "corpus_sha256": corpus["corpus_sha256"],
        "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "source_contract_sha256": file_hash(root / "product-contract.json"),
        "code_sha256": {
            p.name: file_hash(p)
            for p in (
                Path(__file__),
                Path(opencode_native.__file__),
                Path(opencode_budget.__file__),
                Path(opencode_receipt_replay.__file__),
                Path(__file__).with_name("metrics.py"),
            )
        },
    }
    freeze(output / "plan.json", plan)
    plan_hash = sha256_json(plan)
    route = await asyncio.to_thread(opencode_native.configured_route, MODEL)
    gate, halted = asyncio.Semaphore(concurrency), asyncio.Event()

    async def execute(key: tuple[str, int, int]):
        case, repetition, limit = key
        prompt, pool, input_hash, _ = inputs[case]
        path = output / "records" / f"{case}.{repetition}.{limit}.json"
        identity = (case, repetition, limit, plan_hash, input_hash)
        if path.exists():
            if not resume:
                raise FileExistsError(path)
            row = _read(path)
            if (
                tuple(
                    row[k]
                    for k in ("case_id", "repetition", "max_tokens", "plan_sha256", "input_sha256")
                )
                != identity
            ):
                raise ValueError("record identity does not match the frozen plan")
        else:
            async with gate:
                if halted.is_set():
                    return None
                row = await opencode_budget.invoke(
                    route, prompt, case, limit, input_hash, plan_hash
                )
                row["repetition"] = repetition
                save_new(path, row)
                if any(m.get("status_code") in {401, 403, 429} for m in row["call_metadata"]):
                    halted.set()
        scored = opencode_budget.score(row, tasks[case], corpus, pool)
        print(
            json.dumps(
                {
                    "case": case,
                    "repetition": repetition,
                    "max_tokens": limit,
                    "finish": row["finish"],
                    "error": row["error"],
                    "format_valid": scored["assessment"]["format_valid"],
                    "strict_visible_coverage": scored["assessment"][
                        "in_pool_direction_evidence_coverage"
                    ],
                }
            ),
            flush=True,
        )
        return scored

    outcomes = await asyncio.gather(*(execute(key) for key in order))
    if any(row is None for row in outcomes):
        return {"status": "paused_after_provider_rejection", "not_attempted": outcomes.count(None)}
    result = summarize(dict(zip(order, outcomes, strict=True)))
    freeze(output / "assessment.json", {"plan": plan, **result})
    return {"status": "sealed", **result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4), default=4)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args.name, args.concurrency, resume=args.resume))
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "sealed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
