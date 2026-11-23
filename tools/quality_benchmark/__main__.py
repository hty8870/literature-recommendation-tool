from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.benchmark.core import (
    file_hash,
    model_secrets,
    withhold_secrets,
    write_json,
    write_jsonl,
)
from tools.benchmark.runners import KIMI_MAX_STEPS_PER_TURN, _is_retrieval_action, _parse_stream
from tools.quality_benchmark.core import (
    EMBEDDING_CACHE_DIR,
    PROTOCOL_PATH,
    QUALITY_RUNTIME,
    RESULTS_ROOT,
    ROOT,
    SUITE_PATH,
    artifact_hash,
    expanded_tasks,
    is_external_suite,
    kimi_prompt,
    load_corpus,
    load_suite,
    project_skill_isolation_valid,
    validate_gold,
)
from tools.quality_benchmark.evaluation import (
    evaluate_records,
    judge_records,
    render_report,
    render_stability_summary,
    stability_summary,
    summarize,
)
from tools.quality_benchmark.runners import run_kimi, run_product

from citation_tool.config import Settings
from citation_tool.reasons import ModelConfig
from citation_tool.semantic import EmbeddingIndex


def _suite_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    return (
        (getattr(args, "suite", None) or SUITE_PATH).resolve(),
        (getattr(args, "protocol", None) or PROTOCOL_PATH).resolve(),
    )


def _roots(suite: dict[str, Any], suite_path: Path) -> tuple[Path, Path]:
    if not is_external_suite(suite):
        return RESULTS_ROOT, QUALITY_RUNTIME
    return (
        suite_path.parent / "results",
        QUALITY_RUNTIME.parent / "external" / suite["suite_id"],
    )


def _implementation_hash(suite: dict[str, Any], suite_path: Path, protocol_path: Path) -> str:
    if is_external_suite(suite):
        return artifact_hash(suite_path, protocol_path)
    return artifact_hash()


def _path_string(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def prepare(args: argparse.Namespace) -> int:
    suite_path, protocol_path = _suite_paths(args)
    suite = load_suite(suite_path)
    corpus = load_corpus(suite)
    tasks = expanded_tasks(suite)
    validate_gold(tasks, corpus)
    model_secrets()
    print(
        json.dumps(
            {
                "suite": suite["suite_id"],
                "base_tasks": len(suite["tasks"]),
                "all_runs_per_system": len(tasks),
                "tuning_runs": len(expanded_tasks(suite, "tuning")),
                "holdout_runs": len(expanded_tasks(suite, "holdout")),
                "corpus": corpus["corpus_id"],
                "real_abstracts": len(corpus["records"]),
                "corpus_sha256": corpus["corpus_sha256"],
                "implementation_hash": _implementation_hash(suite, suite_path, protocol_path),
                "suite_path": _path_string(suite_path),
                "status": "ready",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


async def run(args: argparse.Namespace) -> int:
    suite_path, protocol_path = _suite_paths(args)
    suite = load_suite(suite_path)
    corpus = load_corpus(suite)
    tasks = expanded_tasks(suite, args.split)
    if args.cases:
        selected = set(args.cases.split(","))
        unknown = selected - {task["case_id"] for task in tasks}
        if unknown:
            raise ValueError(f"unknown or out-of-split cases: {sorted(unknown)}")
        tasks = [task for task in tasks if task["case_id"] in selected]
    for task in tasks:
        task["retriever"] = args.retriever or task["retriever"]
    if any(task["retriever"] == "hybrid" for task in tasks):
        embedding = EmbeddingIndex(
            corpus["records"],
            model_name=Settings().embedding_model,
            cache_dir=EMBEDDING_CACHE_DIR,
        )
        if not embedding.cache_path.is_file():
            raise ValueError(
                f"prebuilt embedding cache is required for quality Agent runs: "
                f"{embedding.cache_path}; run retrieval_eval --retriever hybrid first"
            )
    validate_gold(tasks, corpus)
    results_root, runtime_root = _roots(suite, suite_path)
    output = results_root / args.name
    if output.exists() and not args.resume:
        raise ValueError("result directory exists; choose another name or explicit --resume")
    runtime = runtime_root / args.name / "records"
    implementation_hash = _implementation_hash(suite, suite_path, protocol_path)
    first_external_run = (
        suite["suite_id"] == "astra-independent-abstract-v4" and args.name == "independent-v4-first"
    )
    plan = {
        "schema": "abiotech-abstract-ood-plan-v1",
        "run_name": args.name,
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "suite_id": suite["suite_id"],
        "suite_path": _path_string(suite_path),
        "protocol_path": _path_string(protocol_path),
        "tasks_sha256": file_hash(suite_path),
        "protocol_sha256": file_hash(protocol_path),
        "corpus_sha256": corpus["corpus_sha256"],
        "implementation_hash": implementation_hash,
        "selection": args.split,
        "cases": sorted({task["case_id"] for task in tasks}),
        "runs_per_system": len(tasks),
        "model": "deepseek-flash",
        "product_thinking": "disabled",
        "repeat_reason_cache": (
            "same-case tenant and persisted generated reasons; fresh Agent contexts; "
            "prior conversations and answers are not exposed"
        ),
        "resource_policy": "quality-first-v1; retrieval count is not a quality gate",
        "product_safety_limits": {
            "agent_timeout_ms": Settings().agent_timeout_ms,
            "agent_max_tool_calls": Settings().agent_max_tool_calls,
            "agent_recursion_limit": Settings().agent_recursion_limit,
        },
        "kimi_process_timeout_s": 900,
        "legacy_max_retrieval_rounds_field": "ignored; not shown to either system and not scored",
        "evidence_status": (
            "first independent holdout run; any later tuning converts this suite to development"
            if first_external_run
            else "same-question development rerun; not unseen independent confirmation"
            if is_external_suite(suite)
            else "registered suite split"
        ),
        "kimi_max_steps_per_turn": KIMI_MAX_STEPS_PER_TURN,
        "kimi_sessions": (
            "fresh per task; missing startup identities are retained as failures, "
            "not claimed as valid sessions"
        ),
        "corpus_evidence": "real title/abstract only; no full-text citation eligibility",
        "gold": (
            "independent Astra-authored, source-anchored LLM labels; not human double annotation"
            if is_external_suite(suite)
            else "source-reviewed convenience labels; not independent double human annotation"
        ),
        "holdout_rule": "holdout results are not used to modify the implementation before that run",
        "report_title": (
            "Astra 独立摘要候选评测（首次留出）"
            if first_external_run
            else "Astra 摘要检索质量优先复测（开发证据）"
            if is_external_suite(suite)
            else "真实 aBiotech 摘要 OOD 对照报告"
        ),
        "input_parity": "both systems receive baseline_query and request; expect remains hidden",
        "external_ndcg_label": (
            "required 二元 nDCG@5"
            if suite["suite_id"]
            in {
                "astra-independent-abstract-v2",
                "astra-independent-abstract-v3",
                "astra-independent-abstract-v4",
            }
            else "分级 nDCG@5"
        ),
        "status": "running",
    }
    if is_external_suite(suite):
        old_targets = {
            work_id
            for task in load_suite(SUITE_PATH)["tasks"]
            for work_id in task["expect"].get("required_ids", [])
        }
        new_targets = {
            work_id for task in suite["tasks"] for work_id in task["expect"].get("required_ids", [])
        }
        prior_external_suites = {
            "astra-independent-abstract-v2": ["astra-v1"],
            "astra-independent-abstract-v3": ["astra-v1", "astra-v2"],
            "astra-independent-abstract-v4": ["astra-v1", "astra-v2", "astra-v3"],
        }
        for prior_suite in prior_external_suites.get(suite["suite_id"], []):
            old_targets.update(
                work_id
                for task in load_suite(ROOT / "benchmark/external" / prior_suite / "tasks.json")[
                    "tasks"
                ]
                for work_id in task["expect"].get("required_ids", [])
            )
        plan["target_overlap_with_prior_suites"] = {
            "external_targets": len(new_targets),
            "novel_targets": sorted(new_targets - old_targets),
            "overlapping_targets": sorted(new_targets & old_targets),
            "interpretation": "independent new questions; target overlap is reported explicitly",
        }
    write_json(output / "plan.json", plan)
    secrets = model_secrets()
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(task: dict[str, Any], system: str) -> dict[str, Any]:
        path = runtime / f"{task['run_case_id']}.{system}.json"
        if args.resume and path.is_file():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("implementation_hash") == implementation_hash and (
                previous.get("completed")
                or previous.get("session_id")
                or previous.get("agent_run_id")
            ):
                print(f"reuse {task['run_case_id']} {system}", flush=True)
                return previous
        async with semaphore:
            if system == "product":
                record = await run_product(task, args.name, corpus, runtime_root=runtime_root)
            else:
                record = await asyncio.to_thread(
                    run_kimi,
                    task,
                    f"quality-{args.name}",
                    secrets,
                    corpus_payload=corpus,
                    prompt=kimi_prompt(task),
                )
                record["split"] = task["split"]
        record["implementation_hash"] = implementation_hash
        record, leaked = withhold_secrets(record, secrets)
        record["secret_leak"] = bool(record.get("secret_leak") or leaked)
        write_json(path, record)
        print(
            f"done {task['run_case_id']} {system}: completed={record['completed']} "
            f"seconds={record['wall_time_ms'] / 1000:.1f}",
            flush=True,
        )
        return record

    records = await asyncio.gather(
        *(one(task, system) for task in tasks for system in ("product", "kimi_code"))
    )
    return await finalize(records, tasks, corpus, plan, output, args.concurrency)


async def finalize(
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    corpus: dict[str, Any],
    plan: dict[str, Any],
    output,
    concurrency: int,
    judge_config: ModelConfig | None = None,
    secrets: list[str] | None = None,
) -> int:
    for system, key in (("product", "agent_run_id"), ("kimi_code", "session_id")):
        identifiers = [record.get(key) for record in records if record["system"] == system]
        for record in records:
            if record["system"] == system:
                record["session_isolation_valid"] = (
                    bool(record.get(key)) and identifiers.count(record.get(key)) == 1
                )
    for record in records:
        record["session_identity_verified"] = bool(
            record.get("agent_run_id")
            if record["system"] == "product"
            else record.get("session_id")
        )
        if record["system"] == "kimi_code":
            record["project_skill_isolation_valid"] = project_skill_isolation_valid(record)
    if not all(
        (
            record["session_isolation_valid"]
            or (not record["completed"] and not record["session_identity_verified"])
        )
        and record.get("project_skill_isolation_valid", True)
        for record in records
    ):
        raise ValueError("completed run lacks isolation, or session identity is duplicated")
    evaluated = evaluate_records(records, tasks, corpus["records"])
    injected_judge = {"config": judge_config} if judge_config is not None else {}
    judgments, judge_usage = await judge_records(
        evaluated,
        tasks,
        corpus["records"],
        concurrency=concurrency,
        corpus_context=corpus,
        **injected_judge,
    )
    summary = summarize(evaluated, judgments, judge_usage)
    stability = stability_summary(evaluated, corpus["records"])
    write_jsonl(output / "runs.jsonl", evaluated)
    write_json(output / "judgments.json", judgments)
    write_json(output / "summary.json", summary)
    write_json(output / "stability.json", stability)
    (output / "report.md").write_text(
        render_report(summary, evaluated, plan) + render_stability_summary(stability),
        encoding="utf-8",
    )
    plan["status"] = (
        "completed" if summary["judgments_completed"] == len(tasks) else "judge_incomplete"
    )
    write_json(output / "plan.json", plan)
    _, leak = withhold_secrets(
        {"records": evaluated, "judgments": judgments, "summary": summary},
        model_secrets() if secrets is None else secrets,
    )
    if leak:
        raise ValueError("secret scanner prevented artifact publication")
    print(f"report={output / 'report.md'}", flush=True)
    print(json.dumps(summary["systems"], ensure_ascii=False, indent=2), flush=True)
    return 0 if plan["status"] == "completed" else 2


def original_record_path(
    source: Path, runtime_root: Path, source_plan: dict[str, Any], filename: str, system: str
) -> Path:
    if Path(filename).name != filename:
        raise ValueError("record filename must not contain a path")
    path = runtime_root / source.name / "records" / filename
    if path.is_file():
        return path
    baseline = source_plan.get("baseline_reused_from")
    if system == "kimi_code" and baseline:
        if not isinstance(baseline, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", baseline):
            raise ValueError("invalid reused baseline name")
        plan_path = source.parent / baseline / "plan.json"
        if not plan_path.is_file() or file_hash(plan_path) != source_plan.get(
            "baseline_plan_sha256"
        ):
            raise ValueError("frozen baseline plan changed or is unverifiable")
        path = runtime_root / baseline / "records" / filename
        if path.is_file():
            return path
    raise ValueError(f"missing original record: {filename}")


async def rescore(args: argparse.Namespace) -> int:
    suite_path, protocol_path = _suite_paths(args)
    suite = load_suite(suite_path)
    results_root, runtime_root = _roots(suite, suite_path)
    source = results_root / args.source_name
    source_plan = json.loads((source / "plan.json").read_text(encoding="utf-8"))
    output = results_root / args.name
    if output.exists():
        raise ValueError("rescore must write to a new result directory")
    corpus = load_corpus(suite)
    if source_plan["corpus_sha256"] != corpus["corpus_sha256"]:
        raise ValueError("corpus changed; cannot rescore original outputs")
    tasks = expanded_tasks(suite, source_plan["selection"])
    if source_plan["tasks_sha256"] != file_hash(suite_path):
        raise ValueError("tasks changed; cannot rescore original outputs")
    validate_gold(tasks, corpus)
    records = []
    for task in tasks:
        for system in ("product", "kimi_code"):
            path = original_record_path(
                source, runtime_root, source_plan, f"{task['run_case_id']}.{system}.json", system
            )
            record = json.loads(path.read_text(encoding="utf-8"))
            if system == "kimi_code":
                calls = record.get("tool_calls", [])
                record["raw_retrieval_actions_before_metric_v2"] = record["retrieval_actions"]
                record["retrieval_actions"] = sum(
                    _is_retrieval_action(index, call, calls) for index, call in enumerate(calls)
                )
                stream_path = record.get("stream_path")
                if stream_path and (ROOT / stream_path).is_file():
                    parsed = _parse_stream((ROOT / stream_path).read_text(encoding="utf-8"))
                    if parsed["response_text"]:
                        record["raw_response_text_before_final_answer_v3"] = record.get(
                            "response_text"
                        )
                        record["response_text"] = parsed["response_text"]
                        record["visible_output"] = parsed["response_text"]
                        record["transcript_text"] = parsed["transcript_text"]
            records.append(record)
    plan = {
        **source_plan,
        "run_name": args.name,
        "source_run_name": args.source_name,
        "source_created_at": source_plan["created_at"],
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "protocol_sha256": file_hash(protocol_path),
        "scoring_implementation_hash": _implementation_hash(suite, suite_path, protocol_path),
        "rescore_reason": args.reason,
        "original_agent_calls_repeated": False,
        "judge_calls_repeated": True,
        "status": "running",
    }
    plan["rescore_history"] = [
        *source_plan.get("rescore_history", []),
        {
            "source_run_name": args.source_name,
            "reason": args.reason,
            "scoring_implementation_hash": plan["scoring_implementation_hash"],
            "original_agent_calls_repeated": False,
        },
    ]
    write_json(output / "plan.json", plan)
    print(f"reuse {len(records)} original agent outputs; only the judge will run", flush=True)
    return await finalize(records, tasks, corpus, plan, output, args.concurrency)


def backfill_stability(args: argparse.Namespace) -> int:
    suite_path, _ = _suite_paths(args)
    suite = load_suite(suite_path)
    corpus = load_corpus(suite)
    results_root, _ = _roots(suite, suite_path)
    output = results_root / args.source_name
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    if plan["corpus_sha256"] != corpus["corpus_sha256"]:
        raise ValueError("corpus changed; cannot analyse the original outputs")
    records = [
        json.loads(line)
        for line in (output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(records) != plan["runs_per_system"] * 2:
        raise ValueError("stability backfill requires the complete original run")
    result = stability_summary(
        records,
        corpus["records"],
        provenance="post-hoc deterministic analysis of original outputs; no Agent or judge calls",
    )
    write_json(output / "stability.json", result)
    (output / "stability.md").write_text(
        "# 重复运行稳定性\n\n原始 Agent 与评审输出未改变；本文件仅为确定性补算，"
        "文本差异是否构成科学漂移需回查原回答。\n" + render_stability_summary(result),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"stability_cases": len(result["cases"]), "path": str(output)}, ensure_ascii=False
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Real aBiotech abstract OOD A/C benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--suite", type=Path)
    prepare_parser.add_argument("--protocol", type=Path)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--suite", type=Path)
    run_parser.add_argument("--protocol", type=Path)
    run_parser.add_argument("--name", required=True)
    run_parser.add_argument(
        "--split", choices=["all", "tuning", "development", "holdout"], default="all"
    )
    run_parser.add_argument("--retriever", choices=["sparse", "hybrid"])
    run_parser.add_argument("--cases")
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=2)
    score_parser = subparsers.add_parser("rescore")
    score_parser.add_argument("--suite", type=Path)
    score_parser.add_argument("--protocol", type=Path)
    score_parser.add_argument("--source-name", required=True)
    score_parser.add_argument("--name", required=True)
    score_parser.add_argument("--reason", required=True)
    score_parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=2)
    stability_parser = subparsers.add_parser("stability")
    stability_parser.add_argument("--suite", type=Path)
    stability_parser.add_argument("--protocol", type=Path)
    stability_parser.add_argument("--source-name", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        return prepare(args)
    if args.command == "stability":
        return backfill_stability(args)
    if args.command == "rescore":
        return asyncio.run(rescore(args))
    return asyncio.run(run(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"quality benchmark error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
