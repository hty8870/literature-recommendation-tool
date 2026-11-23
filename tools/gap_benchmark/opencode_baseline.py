"""General-agent arm of the OpenCode-model SciFact development retest."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from tools.benchmark.core import file_hash, secret_leak
from tools.benchmark.runners import _parse_kimi_export, _parse_stream
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.runners import baseline_prompt
from tools.kimi_chat_model import _child_env, resolve_kimi_command
from tools.quality_benchmark.core import project_skill_isolation_valid

from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json

MODELS = {
    "deepseek-v4.1-flash": "OpenCode Go/deepseek-v4.1-flash",
    "qwen3.8-flash": "OpenCode Zen/qwen3.8-flash",
}
TIMEOUT = 900
STEPS = 96


def freeze(path: Path, value: dict) -> None:
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError(f"frozen input changed: {path.name}")
    else:
        save_new(path, value)


def prepare(name: str, model: str) -> tuple[Path, dict, dict, dict]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,70}", name):
        raise ValueError("invalid run name")
    suite, corpus = load_data()
    if len(suite["tasks"]) != 28 or len(corpus["records"]) != 5183:
        raise ValueError("wrong frozen cohort")
    root = RUNTIME / "opencode-retests" / name / model
    plan = {
        "schema": "scifact-opencode-development-retest-v1",
        "cohort": "same previously inspected 28 tasks, not an independent holdout",
        "model": model,
        "cli_alias": MODELS[model],
        "corpus_sha256": file_hash(RUNTIME / "corpus.json"),
        "task_file_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "scorer_sha256": file_hash(Path(__file__).with_name("metrics.py")),
        "baseline_code_sha256": file_hash(Path(__file__)),
        "task_prompt_sha256": {
            task["case_id"]: sha256_json(baseline_prompt(task)) for task in suite["tasks"]
        },
        "timeout_seconds": TIMEOUT,
        "max_steps": STEPS,
        "gold_sent_to_model": False,
        "primary_metric": "official direction plus exact quote, positive-case macro mean",
        "baseline": "Kimi Code CLI fresh standard session; only benchmark corpus supplied",
    }
    freeze(root / "plan.json", plan)
    return root, suite, corpus, plan


def evidence_trace(archive: Path, known_ids: set[str]) -> list[dict]:
    observed = []
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.namelist():
            if not entry.endswith("/wire.jsonl"):
                continue
            with bundle.open(entry) as stream:
                for line in stream:
                    record = json.loads(line)
                    if record.get("type") != "context.append_loop_event":
                        continue
                    event = record.get("event") or {}
                    if event.get("type") != "tool.result":
                        continue
                    result = event.get("result") or {}
                    if not isinstance(result, dict):
                        continue
                    output = result.get("output") or ""
                    if not isinstance(output, str):
                        output = ""
                    leaked = secret_leak(output, [])
                    observed.append(
                        {
                            "tool_call_id": event.get("toolCallId"),
                            "time": record.get("time"),
                            "error": bool(result.get("is_error") or result.get("isError")),
                            "corpus_ids_mentioned": sorted(
                                set(re.findall(r"\b\d{3,10}\b", output)) & known_ids
                            )
                            if not leaked
                            else [],
                            "output": output[:40_000] if not leaked else "[WITHHELD]",
                            "truncated": len(output) > 40_000 or bool(result.get("output_path")),
                            "potential_secret_withheld": leaked,
                        }
                    )
    return observed


def run_subject(task: dict, corpus: dict, plan: dict) -> dict:
    prompt = baseline_prompt(task)
    env = _child_env()
    env.pop("KIMI_CODE_BUILTIN_PRODUCT_SKILLS", None)
    env["KIMI_LOOP_MAX_STEPS_PER_TURN"] = str(STEPS)
    command = resolve_kimi_command()
    started_at, started = utc_now_iso(), time.monotonic()
    with tempfile.TemporaryDirectory(prefix="scifact-opencode-subject-") as temporary:
        workspace = Path(temporary) / "subject"
        workspace.mkdir()
        source = workspace / "corpus.json"
        shutil.copyfile(RUNTIME / "corpus.json", source)
        if file_hash(source) != plan["corpus_sha256"]:
            raise ValueError("CLI input corpus differs")
        try:
            result = subprocess.run(
                [*command, "-m", plan["cli_alias"], "--output-format", "stream-json", "-p", prompt],
                cwd=workspace,
                env=env,
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
            )
            stdout, returncode = result.stdout.decode("utf-8", "replace"), result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, returncode = (exc.stdout or b"").decode("utf-8", "replace"), 124
        parsed = _parse_stream(stdout)
        session_id = parsed["session_id"]
        metadata, trace, error = {}, [], None
        if isinstance(session_id, str) and re.fullmatch(r"session_[0-9a-f-]{36}", session_id):
            archive = Path(temporary) / "subject.zip"
            try:
                exported = subprocess.run(
                    [*command, "export", session_id, "--no-include-global-log", "-o", str(archive)],
                    env=env,
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
                if exported.returncode or not archive.is_file():
                    raise ValueError("export unavailable")
                metadata = _parse_kimi_export(archive, started_at)
                trace = evidence_trace(archive, {row["doc_id"] for row in corpus["records"]})
            except (OSError, ValueError, subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
                error = type(exc).__name__
        else:
            error = "missing_session"
    answer = parsed["response_text"]
    leaked = secret_leak(answer + json.dumps(metadata.get("tool_calls", [])), [])
    leaked |= any(event["potential_secret_withheld"] for event in trace)
    model_valid = (
        not error
        and metadata.get("model_observed") == [plan["model"]]
        and metadata.get("model_aliases") == [plan["cli_alias"]]
    )
    record = {
        **metadata,
        "schema": "scifact-opencode-kimi-record-v1",
        "system": "kimi_code",
        "case_id": task["case_id"],
        "run_case_id": task["case_id"],
        "session_id": session_id,
        "cli_version": parsed["cli_version"],
        "completed": returncode == 0 and bool(answer) and not leaked,
        "model_valid": model_valid,
        "model_verification_basis": (
            "CLI export dispatch model/alias and fresh session; provider separately probed"
        ),
        "response_text": answer if not leaked else "[WITHHELD]",
        "tool_calls": metadata.get("tool_calls", []) if not leaked else [],
        "tool_observations": trace if not leaked else [],
        "secret_leak": leaked,
        "observability_error": error,
        "returncode": returncode,
        "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
        "corpus_sha256": plan["corpus_sha256"],
        "prompt_sha256": sha256_json(prompt),
        "plan_sha256": sha256_json(plan),
    }
    record["usage"] = {k: v for k, v in metadata.get("usage", {}).items() if "cost" not in k}
    record["project_skill_isolation_valid"] = project_skill_isolation_valid(record)
    return record


async def run(name: str, model: str, *, resume: bool, concurrency: int) -> None:
    root, suite, corpus, plan = prepare(name, model)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(original: dict) -> None:
        case = original["case_id"]
        path = root / "kimi" / f"{case}.json"
        if path.exists():
            if not resume or json.loads(path.read_text(encoding="utf-8"))[
                "plan_sha256"
            ] != sha256_json(plan):
                raise ValueError("existing record requires matching --resume")
            return
        task = {key: value for key, value in original.items() if key != "expect"}
        async with semaphore:
            row = await asyncio.to_thread(run_subject, task, corpus, plan)
            save_new(path, row)
        print(
            json.dumps(
                {
                    k: row[k]
                    for k in (
                        "case_id",
                        "completed",
                        "model_valid",
                        "returncode",
                        "observability_error",
                        "wall_time_ms",
                    )
                }
            ),
            flush=True,
        )

    await asyncio.gather(*(one(task) for task in suite["tasks"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", required=True, choices=MODELS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    asyncio.run(run(args.name, args.model, resume=args.resume, concurrency=args.concurrency))


if __name__ == "__main__":
    main()
