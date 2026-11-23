"""Run an external quality suite against production HTTP and isolated remote Kimi sessions."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from tools.benchmark.core import (
    MODEL_ID,
    file_hash,
    withhold_secrets,
    write_json,
)
from tools.benchmark.runners import KIMI_MAX_STEPS_PER_TURN, run_kimi
from tools.deploy_citation import ssh_command
from tools.quality_benchmark.__main__ import finalize
from tools.quality_benchmark.core import (
    artifact_hash,
    expanded_tasks,
    kimi_prompt,
    load_corpus,
    load_suite,
    validate_gold,
)
from tools.quality_benchmark.remote import run_product_http

from citation_tool.reasons import ModelConfig

ROOT = Path(__file__).resolve().parents[1]
RESULTS_RELATIVE = "benchmark/external/astra-v4/results"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _remote(command: str, payload: bytes | None = None, timeout: int = 600) -> bytes:
    completed = subprocess.run(
        [*ssh_command(connect_timeout=15), command],
        input=payload,
        capture_output=True,
        timeout=timeout,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace")[-1000:]
        raise RuntimeError(f"remote command failed ({completed.returncode}): {detail}")
    return completed.stdout


def _package(suite_path: Path, protocol_path: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("src", "tools", ".agents"):
            for path in sorted((ROOT / name).rglob("*")):
                if (
                    not path.is_file()
                    or "__pycache__" in path.parts
                    or path.suffix not in {".py", ".md"}
                ):
                    continue
                archive.add(path, arcname=path.relative_to(ROOT).as_posix())
        for path in (suite_path, protocol_path):
            archive.add(
                path,
                arcname=(
                    PurePosixPath("benchmark/external") / suite_path.parent.name / path.name
                ).as_posix(),
            )
    return buffer.getvalue()


def _decrypt_judge_config() -> ModelConfig:
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/systemd-creds", "decrypt", "deepseek.local.json", "-"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("the encrypted benchmark model credential is unavailable")
    try:
        values = json.loads(result.stdout)
        config = ModelConfig(**values)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError(
            "the encrypted benchmark model credential has an invalid schema"
        ) from None
    finally:
        values = None
        result.stdout = b""
    config.validate()
    if config.model != MODEL_ID or config.base_url != "https://api.deepseek.com":
        raise ValueError("the remote judge must use official deepseek-flash")
    return config


def _plan(
    args: argparse.Namespace,
    suite: dict[str, Any],
    corpus: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    suite_path = ROOT / RESULTS_RELATIVE.removesuffix("/results") / "tasks.json"
    protocol_path = suite_path.with_name("protocol.md")
    return {
        "schema": "abiotech-abstract-ood-plan-v1",
        "run_name": args.name,
        "created_at": _now(),
        "suite_id": suite["suite_id"],
        "suite_path": suite_path.relative_to(ROOT).as_posix(),
        "protocol_path": protocol_path.relative_to(ROOT).as_posix(),
        "tasks_sha256": file_hash(suite_path),
        "protocol_sha256": file_hash(protocol_path),
        "corpus_sha256": corpus["corpus_sha256"],
        "implementation_hash": artifact_hash(suite_path, protocol_path),
        "orchestrator_sha256": file_hash(ROOT / "tools/remote_quality.py"),
        "execution_environment": "remote production loopback HTTP and fresh remote Kimi Code CLI",
        "production_release": str(Path("/opt/citation-tool/current").resolve()),
        "product_library_ids": ["europepmc-snapshot"],
        "online_library_included": False,
        "product_usage_availability": "not_exposed_by_http; never interpreted as zero",
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
        "kimi_process_timeout_s": 900,
        "kimi_max_steps_per_turn": KIMI_MAX_STEPS_PER_TURN,
        "kimi_sessions": (
            "fresh per task; actual model, session identity and tool traces are exported"
        ),
        "evidence_status": "same-question development evidence; not a new permanent holdout",
        "corpus_evidence": "real title/abstract only; no full-text citation eligibility",
        "gold": (
            "independent Astra-authored, source-anchored LLM labels; not human double annotation"
        ),
        "input_parity": "both systems receive baseline_query and request; expect remains hidden",
        "external_ndcg_label": "required 二元 nDCG@5",
        "report_title": "Astra v4 生产快照与 Kimi Code 对照（开发证据）",
        "status": "running",
    }


def _package_results(remote_root: str, suite_id: str, name: str) -> bytes:
    result_root = PurePosixPath(RESULTS_RELATIVE) / name
    runtime_root = PurePosixPath("data/runtime/benchmark/external") / suite_id / name
    files = []
    for relative_root in (result_root, runtime_root):
        root = PurePosixPath(remote_root) / relative_root
        if not root.is_dir():
            continue
        files.extend(path for path in root.rglob("*") if path.is_file())
    manifest = {
        "schema": "remote-quality-transfer-v1",
        "created_at": _now(),
        "result_root": result_root.as_posix(),
        "runtime_root": runtime_root.as_posix(),
        "files": {
            path.relative_to(remote_root).as_posix(): file_hash(path) for path in sorted(files)
        },
    }
    manifest_path = Path(remote_root) / "remote-execution.json"
    write_json(manifest_path, manifest)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(manifest_path, arcname=(result_root / "remote-execution.json").as_posix())
        for path in sorted(files):
            archive.add(path, arcname=path.relative_to(remote_root).as_posix())
    return buffer.getvalue()


async def _execute(args: argparse.Namespace) -> int:
    suite_path = (ROOT / args.suite).resolve()
    if not (ROOT / args.protocol).is_file():
        raise ValueError("the remote benchmark protocol is missing")
    suite = load_suite(suite_path)
    corpus = load_corpus(suite)
    tasks = expanded_tasks(suite, args.split)
    if args.cases:
        selected = set(args.cases.split(","))
        unknown = selected - {task["case_id"] for task in tasks}
        if unknown:
            raise ValueError(f"unknown cases: {sorted(unknown)}")
        tasks = [task for task in tasks if task["case_id"] in selected]
    validate_gold(tasks, corpus)
    if suite["suite_id"] != "astra-independent-abstract-v4":
        raise ValueError("remote evaluation currently requires the frozen astra-v4 suite")
    judge_config = _decrypt_judge_config()
    secrets = [judge_config.api_key]
    output = ROOT / RESULTS_RELATIVE / args.name
    if output.exists() and not args.resume:
        raise ValueError("result directory exists; choose a new name")
    plan = _plan(args, suite, corpus, tasks)
    write_json(output / "plan.json", plan)
    semaphore = asyncio.Semaphore(args.concurrency)
    records_root = (
        ROOT / "data/runtime/benchmark/external" / suite["suite_id"] / args.name / "records"
    )

    async def one(task: dict[str, Any], system: str) -> dict[str, Any]:
        path = records_root / f"{task['run_case_id']}.{system}.json"
        if args.resume and path.is_file():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("implementation_hash") == plan["implementation_hash"] and (
                previous.get("completed")
                or previous.get("agent_run_id")
                or previous.get("session_id")
            ):
                print(f"reuse {task['run_case_id']} {system}", flush=True)
                return previous
        async with semaphore:
            if system == "product":
                record = await run_product_http(
                    task, expected_index_snapshot=f"europepmc-{corpus['corpus_sha256'][:16]}"
                )
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
        record["implementation_hash"] = plan["implementation_hash"]
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
    status = await finalize(
        records,
        tasks,
        corpus,
        plan,
        output,
        args.concurrency,
        judge_config=judge_config,
        secrets=secrets,
    )
    print(f"remote_status={status}", flush=True)
    return status


def _verify_download(archive: bytes, destination: Path, runtime: Path, name: str) -> dict[str, Any]:
    allowed = {destination.resolve(), runtime.resolve()}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as packaged:
        manifest_file = packaged.extractfile(f"{RESULTS_RELATIVE}/{name}/remote-execution.json")
        if manifest_file is None:
            raise ValueError("remote result manifest is missing")
        manifest = json.load(manifest_file)
        for member in packaged.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not member.isfile():
                raise ValueError("unsafe remote result archive")
            target = (ROOT / path).resolve()
            if target not in allowed and not any(root in target.parents for root in allowed):
                raise ValueError("unexpected remote result path")
            if destination.exists() and target.exists():
                raise ValueError("refusing to overwrite an existing local benchmark result")
        packaged.extractall(ROOT, filter="data")
    for name, expected in manifest["files"].items():
        if file_hash(ROOT / name) != expected:
            raise ValueError(f"download integrity mismatch: {name}")
    return manifest


def _local(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,100}", args.name):
        raise ValueError("run name must be a lowercase slug")
    suite_path = args.suite.resolve()
    protocol_path = args.protocol.resolve()
    suite = load_suite(suite_path)
    corpus = load_corpus(suite)
    destination = suite_path.parent / "results" / args.name
    runtime = ROOT / "data/runtime/benchmark/external" / suite["suite_id"] / args.name
    if destination.exists() or runtime.exists():
        raise ValueError("local result or runtime already exists; choose a new name")
    remote_root = f"/opt/citation-tool/evaluations/{args.name}-{uuid.uuid4().hex[:12]}"
    log_dir = ROOT / "data/runtime/remote-quality"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{args.name}.log"
    archive = _package(suite_path, protocol_path)
    _remote(f"sudo install -d -o ubuntu -g ubuntu -m 0755 {shlex.quote(remote_root)}")
    try:
        _remote(
            f"tar -xzf - -C {shlex.quote(remote_root)}",
            archive,
        )
        remote_corpus = f"{remote_root}/{suite['corpus_path']}"
        _remote(
            "install -D -m 0600 /opt/citation-tool/current/corpus.json "
            f"{shlex.quote(remote_corpus)} && sha256sum {shlex.quote(remote_corpus)}"
        )
        suite_remote = f"benchmark/external/{suite_path.parent.name}/tasks.json"
        protocol_remote = f"benchmark/external/{suite_path.parent.name}/protocol.md"
        command = (
            f"cd {shlex.quote(remote_root)} && "
            f"env PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 "
            f"PYTHONPATH={shlex.quote(remote_root + '/src')} "
            "/opt/citation-tool/current/.venv/bin/python -m tools.remote_quality --execute "
            f"--suite {shlex.quote(suite_remote)} --protocol {shlex.quote(protocol_remote)} "
            f"--name {shlex.quote(args.name)} --concurrency {args.concurrency}"
            + (f" --cases {shlex.quote(args.cases)}" if args.cases else "")
        )
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                [*ssh_command(connect_timeout=15), command],
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=86_400,
            )
        print(f"remote log: {log_path.relative_to(ROOT)}", flush=True)
        result_archive = _remote(
            f"env PYTHONPATH={shlex.quote(remote_root + '/src')} "
            "/opt/citation-tool/current/.venv/bin/python -m tools.remote_quality --package-only "
            f"--suite {shlex.quote(suite_remote)} --name {shlex.quote(args.name)}",
            timeout=300,
        )
        manifest = _verify_download(result_archive, destination, runtime, args.name)
        result_plan = destination / "plan.json"
        if result_plan.is_file():
            plan = json.loads(result_plan.read_text(encoding="utf-8"))
            if plan["corpus_sha256"] != corpus["corpus_sha256"]:
                raise ValueError("remote production corpus differs from the frozen local corpus")
            plan["local_download_complete"] = True
            plan["remote_log"] = log_path.relative_to(ROOT).as_posix()
            write_json(result_plan, plan)
        manifest["local_download_complete"] = True
        manifest["remote_returncode"] = completed.returncode
        write_json(destination / "remote-execution.json", manifest)
    except Exception:
        print(f"remote workspace preserved for diagnosis: {remote_root}", file=sys.stderr)
        raise
    _remote(
        "/usr/bin/python3 -c "
        + shlex.quote(
            "import pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]).resolve(); "
            "assert p.parent == pathlib.Path('/opt/citation-tool/evaluations'); "
            "assert p.name.startswith(sys.argv[2] + '-'); shutil.rmtree(p)"
        )
        + f" {shlex.quote(remote_root)} {shlex.quote(args.name)}"
    )
    print(f"downloaded: {destination.relative_to(ROOT)}", flush=True)
    return completed.returncode


def _package_only(args: argparse.Namespace) -> int:
    suite = load_suite(ROOT / args.suite)
    sys.stdout.buffer.write(_package_results(str(ROOT), suite["suite_id"], args.name))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--package-only", action="store_true")
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--cases")
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=2)
    parser.add_argument(
        "--split", default="all", choices=["all", "tuning", "development", "holdout"]
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.package_only:
        return _package_only(args)
    if args.execute:
        if not args.protocol:
            parser.error("--execute requires --protocol")
        return asyncio.run(_execute(args))
    if not args.protocol:
        parser.error("--protocol is required")
    return _local(args)


if __name__ == "__main__":
    import sys

    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"remote quality error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
