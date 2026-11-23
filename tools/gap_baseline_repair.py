from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
from pathlib import Path

from tools.benchmark.core import file_hash, model_secrets, withhold_secrets
from tools.benchmark.runners import run_kimi
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.runners import baseline_prompt, implementation_hash
from tools.quality_benchmark.core import project_skill_isolation_valid

from citation_tool.config import Settings
from citation_tool.reasons import load_model_config


def pin_process_model() -> None:
    model_secrets()
    config = load_model_config(Settings().model_config_path)
    if config is None:
        raise ValueError("model is not configured")
    os.environ.update(
        {
            "KIMI_MODEL_NAME": config.model,
            "KIMI_MODEL_API_KEY": config.api_key,
            "KIMI_MODEL_BASE_URL": config.base_url,
            "KIMI_MODEL_PROVIDER_TYPE": "openai",
            "KIMI_MODEL_THINKING_EFFORT": "max",
        }
    )


async def repair(source_name: str, name: str, concurrency: int, resume: bool) -> dict:
    if not all(re.fullmatch(r"[a-zA-Z0-9_-]+", value) for value in (source_name, name)):
        raise ValueError("run names must not contain paths")
    if source_name == name:
        raise ValueError("repair must retain the original run unchanged")
    suite, corpus = load_data()
    source_plan_path = SUITE_ROOT / "results" / source_name / "plan.json"
    original = json.loads(source_plan_path.read_text(encoding="utf-8"))
    frozen_hash = original["implementation_hash"]
    if implementation_hash() != frozen_hash:
        raise ValueError("product or frozen evaluator changed before baseline recovery")
    pin_process_model()
    secrets = model_secrets()
    output = SUITE_ROOT / "results" / name
    runtime = RUNTIME / name
    plan_path = output / "plan.json"
    if plan_path.exists():
        if not resume:
            raise FileExistsError("baseline recovery requires a new run name or --resume")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan["implementation_hash"] != frozen_hash or plan["repair_tool_sha256"] != file_hash(
            Path(__file__)
        ):
            raise ValueError("recovery implementation changed")
    else:
        plan = {
            **original,
            "run_name": name,
            "status": "baseline_environment_recovery",
            "source_run": source_name,
            "source_plan_sha256": file_hash(source_plan_path),
            "repair_tool_sha256": file_hash(Path(__file__)),
            "product_outputs_reused": "all 28 first frozen product outputs, including failures",
            "baseline_outputs_reused": False,
            "reason": "original Kimi startup failed: unresolved global default alias",
            "baseline_pin": "KIMI_MODEL_* variables; process memory only; no config writes",
            "normal_tools_and_skills": "unchanged; home and skills directories not replaced",
            "reference": "https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/env-vars.html",
        }
        save_new(plan_path, plan)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(original_task):
        path = runtime / "records" / f"{original_task['case_id']}.kimi_code.json"
        if path.exists():
            if not resume:
                raise FileExistsError(path)
            return json.loads(path.read_text(encoding="utf-8"))
        task = copy.deepcopy(original_task)
        task.pop("expect")
        async with semaphore:
            row = await asyncio.to_thread(
                run_kimi,
                task,
                f"gap-env-fixed-{name}",
                secrets,
                corpus_payload=corpus,
                prompt=baseline_prompt(task),
            )
        row["implementation_hash"] = frozen_hash
        row["baseline_model_pin"] = "process-memory KIMI_MODEL_*"
        row, leaked = withhold_secrets(row, secrets)
        row["secret_leak"] = bool(row.get("secret_leak") or leaked)
        save_new(path, row)
        print(
            f"baseline {task['case_id']}: completed={row['completed']} "
            f"model_valid={row['model_valid']} "
            f"seconds={row['wall_time_ms'] / 1000:.1f}",
            flush=True,
        )
        return row

    baselines = await asyncio.gather(*(one(task) for task in suite["tasks"]))
    products = []
    product_hashes = {}
    for task in suite["tasks"]:
        path = RUNTIME / source_name / "records" / f"{task['case_id']}.product.json"
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["implementation_hash"] != frozen_hash:
            raise ValueError("source product output has a different implementation")
        product_hashes[task["case_id"]] = file_hash(path)
        products.append(row)
    records = [*products, *baselines]
    for system, key in (("product", "agent_run_id"), ("kimi_code", "session_id")):
        selected = [row for row in records if row["system"] == system]
        ids = [row.get(key) for row in selected]
        for row in selected:
            row["session_isolation_valid"] = bool(row.get(key)) and ids.count(row[key]) == 1
            if system == "kimi_code":
                row["project_skill_isolation_valid"] = project_skill_isolation_valid(row)
    if implementation_hash() != frozen_hash:
        raise ValueError("frozen product/evaluator changed during baseline repair")
    save_new(output / "product-record-hashes.json", product_hashes)
    save_new(output / "raw-records.json", records)
    return {
        "run_name": name,
        "records": len(records),
        "kimi_valid": sum(row["completed"] and row["model_valid"] for row in baselines),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover a Kimi environment failure without product reruns"
    )
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--concurrency", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(repair(args.source_name, args.name, args.concurrency, args.resume)),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
