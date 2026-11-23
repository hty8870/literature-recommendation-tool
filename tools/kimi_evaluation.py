from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from tools.benchmark.core import ROOT, file_hash, kimi_executable, secret_leak, utc_now_iso
from tools.benchmark.runners import KIMI_MAX_STEPS_PER_TURN, _parse_kimi_export, _parse_stream
from tools.gap_benchmark.data import save_new

DEFAULT_ALIAS = "OpenAI/gpt-6-luna"
DEFAULT_EXPECTED_MODEL = "gpt-6-luna"


def actual_model_matches(observed: list[str], expected: str) -> bool:
    return bool(observed) and set(observed) == {expected}


def run(name: str, prompt_file: Path, corpus: Path | None) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("evaluation name must not contain paths")
    output = ROOT / "data/runtime/kimi-evaluation" / name
    if output.exists():
        raise FileExistsError("use a new evaluation name; previous outputs remain unchanged")
    prompt = prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("evaluation prompt is empty")
    output.mkdir(parents=True)
    started_at = utc_now_iso()
    started = time.monotonic()
    import os

    env = os.environ.copy()
    for key in list(env):
        if key.startswith("KIMI_MODEL_"):
            env.pop(key)
    env.update(
        {
            "KIMI_DISABLE_TELEMETRY": "1",
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "KIMI_DISABLE_CRON": "1",
            "KIMI_CODE_BACKGROUND_KEEP_ALIVE_ON_EXIT": "0",
            "KIMI_LOOP_MAX_STEPS_PER_TURN": str(KIMI_MAX_STEPS_PER_TURN),
            "CI": "1",
        }
    )
    with tempfile.TemporaryDirectory(prefix="literature-luna-evaluation-") as directory:
        workspace = Path(directory)
        if corpus is not None:
            data = json.loads(corpus.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ValueError("evaluation corpus must contain a records array, without gold")
            if any(key in data for key in ("tasks", "all_claims", "expect", "evidence")):
                raise ValueError("subject corpus must not contain benchmark gold")
            (workspace / "corpus.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        try:
            process = subprocess.run(
                [
                    kimi_executable(),
                    "-m",
                    DEFAULT_ALIAS,
                    "--output-format",
                    "stream-json",
                    "-p",
                    prompt,
                ],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            stdout, returncode = process.stdout, process.returncode
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or b""
            stdout = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            returncode = 124
        parsed = _parse_stream(stdout)
        leak = secret_leak(stdout, [])
        (output / "stream.jsonl").write_text(
            "[WITHHELD: potential secret]" if leak else stdout, encoding="utf-8"
        )
        observed = {}
        export_error = None
        if parsed["session_id"]:
            archive = output / "session.zip"
            exported = subprocess.run(
                [
                    kimi_executable(),
                    "export",
                    parsed["session_id"],
                    "--no-include-global-log",
                    "-o",
                    str(archive),
                ],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=45,
            )
            if exported.returncode or not archive.is_file():
                export_error = "session export failed"
            else:
                observed = _parse_kimi_export(archive, started_at)
        else:
            export_error = "session id missing"
        record = {
            **observed,
            "schema": "kimi-luna-evaluation-v1",
            "model_alias_requested": DEFAULT_ALIAS,
            "expected_model": DEFAULT_EXPECTED_MODEL,
            "model_valid": actual_model_matches(
                observed.get("model_observed", []), DEFAULT_EXPECTED_MODEL
            ),
            "session_id": parsed["session_id"],
            "started_at": started_at,
            "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
            "completed": returncode == 0 and bool(parsed["response_text"]) and not leak,
            "response_text": "[WITHHELD: potential secret]" if leak else parsed["response_text"],
            "returncode": returncode,
            "observability_error": export_error,
            "secret_leak": leak,
            "prompt_sha256": file_hash(prompt_file),
            "corpus_sha256": file_hash(corpus) if corpus is not None else None,
            "config_policy": "existing provider; no key copied or global config changed",
            "product_model": "unchanged; this entry only runs Kimi subjects or reviewers",
        }
        save_new(output / "record.json", record)
        return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fresh Kimi evaluation session using the user's 6luna group"
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--corpus", type=Path)
    args = parser.parse_args()
    record = run(args.name, args.prompt_file, args.corpus)
    print(
        json.dumps(
            {
                key: record.get(key)
                for key in (
                    "completed",
                    "model_alias_requested",
                    "model_valid",
                    "model_observed",
                    "session_id",
                    "returncode",
                    "observability_error",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if record["completed"] and record["model_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
