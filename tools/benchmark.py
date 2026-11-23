from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmark.core import (
    PROTOCOL_PATH,
    RESULTS_ROOT,
    RUNTIME_ROOT,
    SUITE_PATH,
    empty_usage,
    expanded_tasks,
    export_corpus,
    file_hash,
    kimi_executable,
    load_suite,
    model_secrets,
    sha256_json,
    withhold_secrets,
    write_json,
    write_jsonl,
)
from benchmark.evaluation import (
    evaluate_records,
    judge_records,
    render_report,
    summarize,
)
from benchmark.runners import (
    KIMI_MAX_STEPS_PER_TURN,
    _is_retrieval_action,
    run_kimi,
    run_product,
)
from citation_tool.config import Settings


def build_plan(run_name: str) -> dict[str, Any]:
    versions = {}
    for package in ("citation-tool", "langchain", "langchain-openai", "openai", "kimi-code"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            continue
    cli = subprocess.run(
        [kimi_executable(), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    source_paths = [
        *sorted(Path("src/citation_tool").glob("*.py")),
        Path(".agents/skills/recommend-literature/SKILL.md"),
        Path("tools/benchmark.py"),
        *sorted(Path("tools/benchmark").glob("*.py")),
    ]
    settings = Settings()
    implementation_hashes = {str(path): file_hash(path) for path in source_paths if path.is_file()}
    return {
        "schema": "m0-agent-benchmark-plan-v1",
        "run_name": run_name,
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "protocol_sha256": file_hash(PROTOCOL_PATH),
        "tasks_sha256": file_hash(SUITE_PATH),
        "corpus_sha256": sha256_json(export_corpus()),
        "expanded_runs_per_system": 18,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": versions,
            "kimi_cli_version": cli.stdout.strip() if cli.returncode == 0 else "unknown",
        },
        "implementation_sha256": implementation_hashes,
        "implementation_bundle_sha256": sha256_json(implementation_hashes),
        "model": {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "documented_version": "DeepSeek-V4.1-Flash-1017",
            "product_thinking": "disabled",
            "kimi_thinking": "existing per-session alias configuration; recorded in each run",
            "pricing_and_version_source": "https://api.deepseek.com/quick_start/pricing",
        },
        "resource_envelope": {
            "product": {
                "agent_max_tool_calls": settings.agent_max_tool_calls,
                "agent_recursion_limit": settings.agent_recursion_limit,
                "agent_timeout_ms": settings.agent_timeout_ms,
                "model_max_inflight": settings.model_max_inflight,
                "model_timeout_ms": settings.model_timeout_ms,
                "library_delays_ms": {
                    library.library_id: library.delay_ms for library in settings.libraries
                },
                "prebuilt_fixture_index": True,
                "normal_tools": ["search_literature"],
            },
            "kimi_code": {
                "fresh_session_per_run": True,
                "session_or_continue_flags": False,
                "skills": "normal user skills; project Skill excluded by isolated cwd",
                "project_skill_or_spec_access": False,
                "normal_file_shell_web_tools": True,
                "global_agents_md": True,
                "loop_max_steps_per_turn": KIMI_MAX_STEPS_PER_TURN,
                "subprocess_timeout_s": 900,
                "prebuilt_fixture_index": False,
                "product_search_api_access": False,
            },
            "judge": {
                "model": "deepseek-flash",
                "thinking": "disabled",
                "tools": [],
                "cost_reported_separately": True,
            },
        },
    }


async def execute(args: argparse.Namespace) -> int:
    suite = load_suite()
    tasks = expanded_tasks(suite)
    if args.cases:
        selected = set(args.cases.split(","))
        unknown = selected - {task["case_id"] for task in tasks}
        if unknown:
            raise ValueError(f"unknown cases: {sorted(unknown)}")
        tasks = [task for task in tasks if task["case_id"] in selected]
    run_name = args.name or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    )
    output = RESULTS_ROOT / run_name
    records_runtime = RUNTIME_ROOT / run_name / "records"
    plan = build_plan(run_name)
    plan["selected_cases"] = sorted({task["case_id"] for task in tasks})
    plan["selected_runs_per_system"] = len(tasks)
    if output.exists() and not args.resume:
        raise ValueError("result directory already exists; use --resume with its explicit name")
    if args.resume and not args.name:
        raise ValueError("--resume requires --name")
    write_json(output / "plan.json", plan)
    secrets = model_secrets()
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(task: dict[str, Any], system: str) -> dict[str, Any]:
        record_path = records_runtime / f"{task['run_case_id']}.{system}.json"
        if args.resume and record_path.is_file():
            previous = json.loads(record_path.read_text(encoding="utf-8"))
            if (
                previous.get("tasks_sha256") == plan["tasks_sha256"]
                and previous.get("implementation_bundle_sha256")
                == plan["implementation_bundle_sha256"]
            ):
                print(f"reuse {task['run_case_id']} {system}", flush=True)
                return previous
        async with semaphore:
            try:
                if system == "product":
                    record = await run_product(task, run_name)
                else:
                    record = await asyncio.to_thread(run_kimi, task, run_name, secrets)
            except Exception as exc:
                record = {
                    "system": system,
                    "case_id": task["case_id"],
                    "run_case_id": task["run_case_id"],
                    "repetition": task["repetition"],
                    "mode": task["mode"],
                    "started_at": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "wall_time_ms": 0.0,
                    "completed": False,
                    "response_text": "",
                    "visible_output": "",
                    "error": {"code": "BENCHMARK_HARNESS_ERROR", "message": type(exc).__name__},
                    "model_valid": False,
                    "usage": empty_usage(),
                    "retrieval_actions": 0,
                    "total_tool_calls": 0,
                    "tool_calls": [],
                    "secret_leak": False,
                }
        record["tasks_sha256"] = plan["tasks_sha256"]
        record["implementation_bundle_sha256"] = plan["implementation_bundle_sha256"]
        record, leaked = withhold_secrets(record, secrets)
        record["secret_leak"] = bool(record.get("secret_leak") or leaked)
        write_json(record_path, record)
        print(
            f"done {task['run_case_id']} {system}: completed={record['completed']} "
            f"seconds={record.get('wall_time_ms', 0) / 1000:.1f} "
            f"retrieval={record.get('retrieval_actions', 0)}",
            flush=True,
        )
        return record

    jobs = [
        one(task, system)
        for task in tasks
        for system in ("product", "kimi_code")
        if not args.systems or system in args.systems.split(",")
    ]
    records = await asyncio.gather(*jobs)
    expected_pairs = len(tasks)
    if len({(record["run_case_id"], record["system"]) for record in records}) != len(records):
        raise ValueError("duplicate benchmark record")
    if len(records) != expected_pairs * 2:
        raise ValueError("both systems are required for the paired report")
    mark_session_isolation(records)
    summary = await finalize(records, tasks, plan, output, args.concurrency)
    print(f"report={output / 'report.md'}", flush=True)
    print(json.dumps(summary["systems"], ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["judgments_completed"] == len(tasks) else 2


def mark_session_isolation(records: list[dict[str, Any]]) -> None:
    for system, id_key in (("product", "agent_run_id"), ("kimi_code", "session_id")):
        identifiers = [record.get(id_key) for record in records if record["system"] == system]
        for record in records:
            if record["system"] == system:
                identifier = record.get(id_key)
                record["session_isolation_valid"] = (
                    bool(identifier) and identifiers.count(identifier) == 1
                )


async def finalize(
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    plan: dict[str, Any],
    output: Path,
    concurrency: int,
) -> dict[str, Any]:
    records = evaluate_records(records, tasks, export_corpus())
    judgments, judge_usage = await judge_records(records, tasks, concurrency=concurrency)
    summary = summarize(records, judgments, judge_usage, tasks)
    summary["run_name"] = plan["run_name"]
    summary["session_isolation_valid"] = all(
        record.get("session_isolation_valid") for record in records
    )
    write_jsonl(output / "runs.jsonl", records)
    write_json(output / "judgments.json", judgments)
    write_json(output / "summary.json", summary)
    (output / "report.md").write_text(
        render_report(summary, records, judgments, plan), encoding="utf-8"
    )
    plan["status"] = (
        "completed" if summary["judgments_completed"] == len(tasks) else "judge_incomplete"
    )
    plan["judgments_completed"] = summary["judgments_completed"]
    write_json(output / "plan.json", plan)
    _, output_leak = withhold_secrets(
        {"runs": records, "judgments": judgments, "summary": summary}, model_secrets()
    )
    if output_leak:
        raise ValueError("secret scanner prevented publication of benchmark artifacts")
    return summary


async def rescore(args: argparse.Namespace) -> int:
    output = RESULTS_ROOT / args.name
    plan_path = output / "plan.json"
    if not plan_path.is_file():
        raise ValueError("cannot rescore a run without its plan")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan["tasks_sha256"] != file_hash(SUITE_PATH):
        raise ValueError("task definitions changed; run a new benchmark instead")
    selected = set(plan.get("selected_cases", []))
    tasks = [task for task in expanded_tasks(load_suite()) if task["case_id"] in selected]
    records = []
    for task in tasks:
        for system in ("product", "kimi_code"):
            path = RUNTIME_ROOT / args.name / "records" / f"{task['run_case_id']}.{system}.json"
            if not path.is_file():
                raise ValueError(f"missing original record: {path.name}")
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("tasks_sha256") != plan["tasks_sha256"]:
                raise ValueError(f"task hash mismatch in {path.name}")
            if system == "kimi_code":
                calls = record.get("tool_calls", [])
                record["raw_retrieval_actions_before_metric_v2"] = record["retrieval_actions"]
                record["retrieval_actions"] = sum(
                    _is_retrieval_action(index, call, calls) for index, call in enumerate(calls)
                )
            records.append(record)
    mark_session_isolation(records)
    previous_judgments = output / "judgments.json"
    if previous_judgments.is_file():
        archived = json.loads(previous_judgments.read_text(encoding="utf-8"))
        write_json(
            RUNTIME_ROOT / args.name / f"judgments-before-rescore-{uuid.uuid4().hex[:8]}.json",
            archived,
        )
    current = build_plan(args.name)
    rescore_entry = {
        "previous_implementation_bundle_sha256": plan.get("implementation_bundle_sha256"),
        "new_implementation_bundle_sha256": current["implementation_bundle_sha256"],
        "reason": args.reason,
        "rescored_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "original_agent_calls_repeated": False,
        "judge_calls_repeated": True,
    }
    history = list(plan.get("rescore_history", []))
    if "rescore" in plan:
        history.append(plan.pop("rescore"))
    history.append(rescore_entry)
    plan["rescore_history"] = history
    plan["implementation_sha256"] = current["implementation_sha256"]
    plan["implementation_bundle_sha256"] = current["implementation_bundle_sha256"]
    plan["protocol_sha256"] = current["protocol_sha256"]
    summary = await finalize(records, tasks, plan, output, args.concurrency)
    print(f"report={output / 'report.md'}", flush=True)
    return 0 if summary["judgments_completed"] == len(tasks) else 2


def prepare() -> int:
    suite = load_suite()
    tasks = expanded_tasks(suite)
    corpus = export_corpus()
    model_secrets()
    plan = build_plan("preflight")
    print(
        json.dumps(
            {
                "suite": suite["suite_id"],
                "base_tasks": len(suite["tasks"]),
                "runs_per_system": len(tasks),
                "corpus_records": len(corpus),
                "tasks_sha256": file_hash(SUITE_PATH),
                "protocol_sha256": file_hash(PROTOCOL_PATH),
                "corpus_sha256": sha256_json(corpus),
                "model": plan["model"],
                "kimi_cli_version": plan["environment"]["kimi_cli_version"],
                "status": "ready",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real-model M0 agent benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare", help="validate tasks, corpus, model and CLI without running")
    run = subparsers.add_parser("run", help="run both systems, judge and render a report")
    run.add_argument("--name", help="immutable run name; generated when omitted")
    run.add_argument("--resume", action="store_true", help="reuse sanitized completed records")
    run.add_argument("--concurrency", type=int, default=2, choices=range(1, 5))
    run.add_argument("--cases", help="comma-separated base case IDs for a pilot")
    run.add_argument("--systems", help=argparse.SUPPRESS)
    score = subparsers.add_parser(
        "rescore", help="re-evaluate saved outputs without rerunning agents"
    )
    score.add_argument("--name", required=True, help="existing run name")
    score.add_argument("--reason", required=True, help="auditable reason for changing the scorer")
    score.add_argument("--concurrency", type=int, default=2, choices=range(1, 5))
    args = parser.parse_args()
    if args.command == "prepare":
        return prepare()
    if args.command == "rescore":
        return asyncio.run(rescore(args))
    return asyncio.run(execute(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except ValueError as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
