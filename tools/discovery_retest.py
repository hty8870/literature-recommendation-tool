from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re

from tools.benchmark.core import file_hash, model_secrets, withhold_secrets, write_json
from tools.quality_benchmark.__main__ import _roots, finalize
from tools.quality_benchmark.core import (
    ROOT,
    artifact_hash,
    expanded_tasks,
    load_corpus,
    load_suite,
    validate_gold,
)
from tools.quality_benchmark.monitoring import monitoring_report
from tools.quality_benchmark.runners import run_product


async def run(args: argparse.Namespace) -> int:
    for value in (args.name, args.source_name):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            raise ValueError("run names must not contain paths")
    suite_path = (ROOT / "benchmark/external/astra-v4/tasks.json").resolve()
    protocol_path = suite_path.parent / "protocol.md"
    suite = load_suite(suite_path)
    corpus = load_corpus(suite)
    tasks = expanded_tasks(suite)
    if args.cases:
        selected = set(args.cases.split(","))
        if selected - {task["case_id"] for task in tasks}:
            raise ValueError("unknown case")
        tasks = [task for task in tasks if task["case_id"] in selected]
    validate_gold(tasks, corpus)
    results_root, runtime_root = _roots(suite, suite_path)
    source_plan = json.loads(
        (results_root / args.source_name / "plan.json").read_text(encoding="utf8")
    )
    if (
        source_plan["tasks_sha256"] != file_hash(suite_path)
        or source_plan["corpus_sha256"] != corpus["corpus_sha256"]
    ):
        raise ValueError("frozen tasks or corpus changed")
    output = results_root / args.name
    if output.exists():
        raise ValueError("use a new run name; previous evidence must not be overwritten")
    baseline = {}
    for task in tasks:
        path = runtime_root / args.source_name / "records" / f"{task['run_case_id']}.kimi_code.json"
        baseline[task["run_case_id"]] = json.loads(path.read_text(encoding="utf8"))
    plan = copy.deepcopy(source_plan)
    plan.update(
        {
            "run_name": args.name,
            "status": "running",
            "runs_per_system": len(tasks),
            "cases": sorted({task["case_id"] for task in tasks}),
            "implementation_hash": artifact_hash(suite_path, protocol_path),
            "report_title": "来源自治修复后产品复测（复用冻结 Kimi 对照）",
            "evidence_status": "development_retest; not independent holdout; Kimi outputs reused",
            "baseline_reused_from": args.source_name,
            "baseline_plan_sha256": file_hash(results_root / args.source_name / "plan.json"),
            "baseline_note": (
                "Kimi is not rerun; exact frozen fresh-session outputs "
                "are reused for a controlled comparison"
            ),
        }
    )
    write_json(output / "plan.json", plan)
    secrets = model_secrets()
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(task):
        task["retriever"] = "hybrid"
        async with semaphore:
            record = await run_product(task, args.name, corpus, runtime_root=runtime_root)
        record["implementation_hash"] = plan["implementation_hash"]
        record, leaked = withhold_secrets(record, secrets)
        record["secret_leak"] = bool(record.get("secret_leak") or leaked)
        write_json(
            runtime_root / args.name / "records" / f"{task['run_case_id']}.product.json", record
        )
        print(
            f"done {task['run_case_id']} product: completed={record['completed']} "
            f"seconds={record['wall_time_ms'] / 1000:.1f}",
            flush=True,
        )
        return record

    products = await asyncio.gather(*(one(copy.deepcopy(task)) for task in tasks))
    records = [*products, *baseline.values()]
    monitoring = monitoring_report(products, tasks)
    write_json(output / "discovery-monitoring.json", monitoring)
    status = await finalize(records, tasks, corpus, plan, output, args.concurrency, secrets=secrets)
    print(
        "discovery_monitoring=" + json.dumps(monitoring["totals"], ensure_ascii=False), flush=True
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retest the product against an unchanged frozen Kimi baseline"
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--source-name", default="architecture-agentic-full-v1")
    parser.add_argument("--cases")
    parser.add_argument("--concurrency", type=int, default=3, choices=range(1, 5))
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
