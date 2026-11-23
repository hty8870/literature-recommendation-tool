"""Paired Qwen output-budget screen on frozen public SciFact development pools."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from tools.benchmark.core import file_hash, secret_leak
from tools.gap_benchmark import opencode_ablation, opencode_native
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import aggregate, mean, score_record
from tools.gap_benchmark.opencode_assessment import _read
from tools.gap_benchmark.opencode_baseline import freeze

from citation_tool.agent import final_answer_text
from citation_tool.jcs import sha256_json

MODEL = "qwen3.8-flash"
BUDGETS = (8192, 16384)
CASES = ("sf-1187", "sf-793", "sf-5", "sf-922")


def score(record: dict, task: dict, corpus: dict, pool_ids: set[str]) -> dict:
    result = score_record(record, task, corpus)
    check = result["assessment"]
    in_pool = [paper for paper in check["papers"] if paper["doc_id"] in pool_ids]
    check["out_of_pool_ids"] = sorted({p["doc_id"] for p in check["papers"]} - pool_ids)
    targets = task["expect"]["evidence"]
    covered = {p["doc_id"] for p in in_pool if p["stance_correct"] and p["exact_quote"]}
    check["in_pool_direction_evidence_coverage"] = (
        len(covered) / len(targets) if check["valid_completion"] and targets else 0.0
    )
    return result


def summarize(records: list[dict]) -> dict:
    budget_rows = {
        limit: {r["case_id"]: r for r in records if r["max_tokens"] == limit} for limit in BUDGETS
    }
    if any(set(rows) != set(CASES) for rows in budget_rows.values()):
        raise ValueError("both budgets require the complete registered cohort")
    field = "in_pool_direction_evidence_coverage"
    summaries = {}
    for limit, cases in budget_rows.items():
        rows = list(cases.values())
        summaries[str(limit)] = {
            **aggregate(rows),
            "fully_delivered": sum(
                r["assessment"]["valid_completion"] and r["assessment"]["format_valid"]
                for r in rows
            ),
            "max_tokens_stops": sum(r["finish"] == "max_tokens" for r in rows),
            "off_pool_cases": sum(bool(r["assessment"]["out_of_pool_ids"]) for r in rows),
            field: mean(r["assessment"][field] for r in rows),
        }
    return {
        "by_max_tokens": summaries,
        "pairs": [
            {
                "case_id": case,
                "arms": {
                    str(limit): {
                        "finish": row["finish"],
                        "error": row["error"],
                        "output_tokens": row["output_tokens"],
                        "format_valid": row["assessment"]["format_valid"],
                        "valid_completion": row["assessment"]["valid_completion"],
                        field: row["assessment"][field],
                    }
                    for limit, cases in budget_rows.items()
                    for row in (cases[case],)
                },
            }
            for case in CASES
        ],
    }


async def invoke(route, prompt, case: str, limit: int, input_hash: str, plan_hash: str) -> dict:
    gateway = opencode_native.OpenCodeGateway(route, parent_max_tokens=limit)
    started = time.monotonic()
    text, finish, error = "", None, None
    try:
        response = await gateway.model_for("parent", json_mode=False).ainvoke(prompt)
        finish = response.response_metadata["finish_reason"]
        text = response.text or ""
        if finish == "max_tokens":
            error = "OUTPUT_LIMIT"
        elif finish != "end_turn" or final_answer_text(response) is None:
            error = "NO_TERMINAL_OUTPUT"
    except (opencode_native.ModelFailure, TimeoutError) as exc:
        error = getattr(exc, "code", type(exc).__name__)
    finally:
        await gateway.aclose()
    audit = gateway.audit()
    leaked = secret_leak(text, [route.api_key])
    return {
        "case_id": case,
        "run_case_id": case,
        "system": "replay",
        "max_tokens": limit,
        "plan_sha256": plan_hash,
        "input_sha256": input_hash,
        "model_valid": audit["model_valid"],
        "session_isolation_valid": audit["session_isolation_valid"],
        "observed_model": audit["served_models"],
        "completed": not error and bool(text) and not leaked,
        "response_text": text if not leaked else "[WITHHELD]",
        "secret_leak": leaked,
        "finish": finish,
        "error": error,
        "output_tokens": audit["usage"]["output_tokens"],
        "call_metadata": audit["artifacts"],
        "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
    }


async def run(name: str, *, resume: bool) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,70}", name):
        raise ValueError("invalid run name")
    root = RUNTIME / "opencode-budget" / name / MODEL
    suite, corpus = load_data()
    originals = {t["case_id"]: t for t in suite["tasks"] if t["case_id"] in CASES}
    if len({t["family"] for t in originals.values()}) != len(CASES):
        raise ValueError("development cases must represent distinct families")
    sources = {r["doc_id"]: r for r in corpus["records"]}
    history = {
        r["case_id"]: r
        for r in _read(opencode_ablation.HISTORICAL)["records"]
        if r["system"] == "product"
    }
    pools = {case: opencode_ablation.checked_pool(history[case], sources) for case in CASES}
    prompts = {
        case: opencode_ablation.messages(
            {k: v for k, v in originals[case].items() if k != "expect"}, pools[case], "legacy", root
        )
        for case in CASES
    }
    hashes = {case: sha256_json([m.text for m in prompts[case]]) for case in CASES}
    order = [
        [case, limit]
        for case in CASES
        for limit in sorted(BUDGETS, key=lambda n: hashlib.sha256(f"{case}:{n}".encode()).digest())
    ]
    route = await asyncio.to_thread(opencode_native.configured_route, MODEL)
    here = Path(__file__)
    plan = {
        "schema": "scifact-opencode-parent-budget-v1",
        "model": route.model,
        "endpoint": route.base_url + route.path,
        "protocol": route.protocol,
        "scope": "known development cases, direct single-response replay, no retrieval tools",
        "selection": "two historic cap stops and two non-cap controls; all cases retained",
        "order": order,
        "changed_parameter": "max_tokens only; no schema, repair, or prompt change",
        "gold_sent_to_model": False,
        "pool_sha256": {case: sha256_json(pools[case]) for case in CASES},
        "prompt_sha256": hashes,
        "scorer_sha256": file_hash(here.with_name("metrics.py")),
        "task_file_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "corpus_sha256": corpus["corpus_sha256"],
        "historical_sha256": file_hash(opencode_ablation.HISTORICAL),
        "runner_sha256": file_hash(here),
        "transport_sha256": file_hash(Path(opencode_native.__file__)),
    }
    freeze(root / "plan.json", plan)
    plan_hash = sha256_json(plan)
    records = []
    for case, limit in order:
        path = root / "records" / f"{case}.{limit}.json"
        if path.is_file():
            if not resume:
                raise FileExistsError(path)
            row = _read(path)
            if (row["case_id"], row["max_tokens"], row["input_sha256"], row["plan_sha256"]) != (
                case,
                limit,
                hashes[case],
                plan_hash,
            ):
                raise ValueError("record does not belong to the frozen budget arm")
        else:
            row = await invoke(route, prompts[case], case, limit, hashes[case], plan_hash)
            save_new(path, row)
        ids = {
            p[0].removeprefix("work:scifact:")
            for query in opencode_ablation.source_material(pools[case])
            for p in query
        }
        assessed = score(row, originals[case], corpus, ids)
        records.append(assessed)
        fields = ("case_id", "max_tokens", "error", "finish", "output_tokens", "wall_time_ms")
        print(
            json.dumps(
                {key: row[key] for key in fields}
                | {"format_valid": assessed["assessment"]["format_valid"]}
            ),
            flush=True,
        )
    result = summarize(records)
    freeze(root / "assessment.json", {"plan": plan, **result, "records": records})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.name, resume=args.resume)), indent=2))


if __name__ == "__main__":
    main()
