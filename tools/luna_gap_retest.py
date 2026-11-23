from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from tools.benchmark.core import file_hash
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import aggregate, paired_summary, score_record
from tools.gap_benchmark.runners import baseline_prompt
from tools.kimi_evaluation import DEFAULT_ALIAS, DEFAULT_EXPECTED_MODEL, run

from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json


def prepare_run(name: str) -> tuple[Path, dict, dict]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,90}", name):
        raise ValueError("invalid run name")
    suite, corpus = load_data()
    root = RUNTIME / "luna-retests" / name
    root.mkdir(parents=True, exist_ok=True)
    plan_path = root / "plan.json"
    contract = {
        "schema": "scifact-luna-development-retest-v1",
        "purpose": "Previously inspected tasks: development regression, not unseen holdout.",
        "task_ids": [task["case_id"] for task in suite["tasks"]],
        "source_tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "corpus_sha256": corpus["corpus_sha256"],
        "baseline_model_alias": DEFAULT_ALIAS,
        "expected_actual_model": DEFAULT_EXPECTED_MODEL,
        "max_parallel_subjects": 2,
        "scientific_rules": "Original gold and final-answer metrics unchanged; no round/cost gate.",
        "transport_boundary": (
            "Kimi baseline uses its normal tools in a fresh session. Product transport, if run, "
            "must be separately identified; model names alone do not imply identical transport."
        ),
        "task_prompt_hashes": {
            task["case_id"]: sha256_json(baseline_prompt(task)) for task in suite["tasks"]
        },
    }
    if plan_path.exists():
        previous = json.loads(plan_path.read_text(encoding="utf-8"))
        if {key: previous[key] for key in contract} != contract:
            raise ValueError("frozen retest plan changed; choose a new name")
    else:
        save_new(plan_path, {**contract, "created_at": utc_now_iso()})
    return root, suite, corpus


def reuse_baseline(name: str, source_name: str) -> dict:
    if name == source_name:
        raise ValueError("baseline reuse requires a distinct destination")
    root, suite, _ = prepare_run(name)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,90}", source_name):
        raise ValueError("invalid source run name")
    source = RUNTIME / "luna-retests" / source_name
    source_plan = json.loads((source / "plan.json").read_text(encoding="utf-8"))
    target_plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    if {k: v for k, v in source_plan.items() if k != "created_at"} != {
        k: v for k, v in target_plan.items() if k != "created_at"
    }:
        raise ValueError("baseline plan, model, corpus or task prompts differ")
    prepared = []
    sessions = set()
    for task in suite["tasks"]:
        case = task["case_id"]
        path = source / "kimi" / f"{case}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("case_id") != case or record.get("system") != "kimi_code":
            raise ValueError("baseline case or system identity differs")
        prompt_path = source / "prompts" / f"{case}.txt"
        if prompt_path.read_text(encoding="utf-8") != baseline_prompt(task):
            raise ValueError("baseline subject prompt differs")
        if record.get("prompt_sha256") != file_hash(prompt_path):
            raise ValueError("baseline prompt hash differs")
        if record.get("corpus_sha256") != file_hash(RUNTIME / "corpus.json"):
            raise ValueError("baseline corpus file differs")
        if record.get("model_valid") is not True or record.get("model_observed") != [
            DEFAULT_EXPECTED_MODEL
        ]:
            raise ValueError("baseline model identity is not verified")
        session = record.get("session_id")
        if not session or session in sessions:
            raise ValueError("baseline sessions are missing or not isolated")
        sessions.add(session)
        prepared.append(
            (
                root / "kimi" / f"{case}.json",
                {
                    **record,
                    "baseline_reused_from": {
                        "name": source_name,
                        "record_sha256": file_hash(path),
                        "session_id": session,
                    },
                },
            )
        )
    for destination, record in prepared:
        if destination.exists() and json.loads(destination.read_text(encoding="utf-8")) != record:
            raise ValueError("destination baseline already contains a different record")
    for destination, record in prepared:
        if not destination.exists():
            save_new(destination, record)
    return {
        "baseline_reused": len(prepared),
        "source_name": source_name,
        "name": name,
        "new_model_calls": 0,
        "interpretation": "Exact frozen baseline outputs reused; not fresh independent subjects.",
    }


async def baseline(name: str, *, resume: bool, concurrency: int = 2) -> dict:
    if concurrency not in {1, 2}:
        raise ValueError("subject concurrency must be 1 or 2")
    root, suite, _ = prepare_run(name)
    semaphore = asyncio.Semaphore(concurrency)
    corpus_path = RUNTIME / "corpus.json"

    async def one(task: dict) -> dict:
        case = task["case_id"]
        destination = root / "kimi" / f"{case}.json"
        if destination.exists():
            if not resume:
                raise FileExistsError("existing outputs require --resume or a new run name")
            return json.loads(destination.read_text(encoding="utf-8"))
        prompt_path = root / "prompts" / f"{case}.txt"
        prompt = baseline_prompt(task)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        if prompt_path.exists():
            if prompt_path.read_text(encoding="utf-8") != prompt:
                raise ValueError("subject prompt changed")
        else:
            prompt_path.write_text(prompt, encoding="utf-8")
        async with semaphore:
            record = await asyncio.to_thread(run, f"{name}-{case}", prompt_path, corpus_path)
        result = {
            "system": "kimi_code",
            "case_id": case,
            "family": task["family"],
            "split": "development",
            **record,
        }
        save_new(destination, result)
        print(
            json.dumps(
                {
                    "case": case,
                    "completed": result["completed"],
                    "model_valid": result["model_valid"],
                }
            ),
            flush=True,
        )
        return result

    records = await asyncio.gather(*(one(task) for task in suite["tasks"]))
    summary = {
        "cases": len(records),
        "completed": sum(record["completed"] for record in records),
        "actual_model_verified": sum(record["model_valid"] for record in records),
        "created_at": utc_now_iso(),
        "interpretation": "Completion only; scientific quality has not been scored here.",
    }
    path = root / "baseline-summary.json"
    if not path.exists():
        save_new(path, summary)
    return summary


def first_loss_by_target(record: dict, task: dict) -> list[dict]:
    targets = set(task["expect"]["evidence"])
    available, submitted, base_top, llm_top = set(), set(), set(), set()
    for query in record.get("queries", []):
        for worker in query.get("workers", []):
            available.update(worker.get("source_available_work_ids", []))
        candidates = query.get("candidate_diagnostics", [])
        submitted.update(item["work_id"] for item in candidates)
        for field, destination in (("base_rank", base_top), ("llm_rank", llm_top)):
            destination.update(
                item["work_id"]
                for item in candidates
                if isinstance(item.get(field), int) and item[field] <= 10
            )
    final = {
        row["doc_id"]
        for row in record.get("assessment", {}).get("papers", [])
        if row["rank"] <= 10 and not row["duplicate"]
    }
    stages = [
        ("retrieval", available),
        ("source_submission", submitted),
        ("common_top10", base_top),
        ("llm_top10", llm_top),
    ]
    rows = []
    for target in sorted(targets):
        work = f"work:scifact:{target}"
        observed = {name: work in values for name, values in stages}
        observed["final_answer"] = target in final
        rows.append(
            {
                "doc_id": target,
                "observed": observed,
                "first_missing_stage": next(
                    (name for name, present in observed.items() if not present), None
                ),
                "note": "Cross-query union diagnostic only, not a causal ablation or final score.",
            }
        )
    return rows


def evaluate(name: str) -> dict:
    root, suite, corpus = prepare_run(name)
    records = []
    missing = []
    for system, directory in (("product", "product"), ("kimi_code", "kimi")):
        for task in suite["tasks"]:
            case = task["case_id"]
            path = root / directory / f"{case}.json"
            if not path.exists():
                missing.append({"system": system, "case_id": case})
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = score_record({**raw, "system": system, "run_case_id": case}, task, corpus)
            if system == "product":
                record["first_loss_by_target"] = first_loss_by_target(record, task)
            records.append(record)
    result = {
        "schema": "scifact-luna-development-assessment-v1",
        "evaluated_at": utc_now_iso(),
        "interpretation": (
            "Previously inspected development tasks. Official labels retained despite known "
            "ambiguities; model/transport changes are not attributed solely to architecture."
        ),
        "complete_paired_data": not missing,
        "missing": missing,
        "systems": {
            system: aggregate([row for row in records if row["system"] == system])
            for system in ("product", "kimi_code")
        },
        "paired": paired_summary(records) if not missing else None,
        "records": records,
    }
    index = 1
    while (root / f"assessment-{index}.json").exists():
        index += 1
    save_new(root / f"assessment-{index}.json", result)
    return {key: value for key, value in result.items() if key != "records"}


def main() -> int:
    parser = argparse.ArgumentParser(description="6luna development retest without copying keys")
    parser.add_argument("command", choices=["baseline", "reuse-baseline", "prepare", "evaluate"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--source-name")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, default=2)
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        root, suite, _ = prepare_run(arguments.name)
        result = {
            "prepared_without_model_calls": True,
            "tasks": len(suite["tasks"]),
            "root": str(root),
        }
    elif arguments.command == "evaluate":
        result = evaluate(arguments.name)
    elif arguments.command == "reuse-baseline":
        if not arguments.source_name:
            parser.error("reuse-baseline requires --source-name")
        result = reuse_baseline(arguments.name, arguments.source_name)
    else:
        result = asyncio.run(
            baseline(arguments.name, resume=arguments.resume, concurrency=arguments.concurrency)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
