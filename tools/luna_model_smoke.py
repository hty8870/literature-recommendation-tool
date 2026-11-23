"""Real, small smoke test for the Kimi ``gpt-6-luna`` LangChain seam.

This tool makes four public model calls (plain text, JSON, weather tool, time tool)
through :mod:`tools.kimi_chat_model`.  It never reads a provider key, never changes the
global Kimi configuration, and never writes prompt or response text into the model call
artifacts.  The public prompts and responses are stored in a new runtime directory so a
reviewer can inspect exactly what was sent.

Usage::

    uv run python -m tools.luna_model_smoke --name <new-name>

Exit code 0 means every check passed and every call's exported session reported the actual
model ``gpt-6-luna``.  Any transport, protocol, identity, or tool-call failure exits 2 and
is preserved in ``record.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from tools.kimi_chat_model import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_ALIAS,
    PROTOCOL_VERSION,
    TRANSPORT,
    KimiChatModel,
    KimiLunaConfig,
    KimiLunaGateway,
    KimiSeamError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMOKE_ROOT = PROJECT_ROOT / "data" / "runtime" / "kimi-model-smoke"
SMOKE_SCHEMA = "kimi-luna-model-smoke-v1"
_SECRET_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current local time for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact(text: str) -> str:
    return _SECRET_PATTERN.sub("[REDACTED]", text)[:500]


def public_prompt(check: str, messages: list[Any]) -> str:
    lines = [f"check: {check}", ""]
    for message in messages:
        lines.append(f"[{message.type}]")
        lines.append(message.content)
        lines.append("")
    return "\n".join(lines)


def write_new(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text_new(path: Path, value: str) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")


def tool_call_summary(message: Any) -> list[dict[str, Any]]:
    return [
        {"name": call.get("name"), "args": call.get("args", {})}
        for call in (message.tool_calls or [])
    ]


def check_text(message: Any) -> tuple[bool, str]:
    content = message.content.strip()
    if content.upper() == "LUNA" and not message.tool_calls:
        return True, "returned LUNA without tool calls"
    return False, f"expected LUNA, got {content[:80]!r}"


def check_json(message: Any) -> tuple[bool, str]:
    try:
        value = json.loads(message.content)
    except json.JSONDecodeError:
        return False, "content is not valid JSON"
    expected = {"ok": True, "total": 5}
    if value == expected:
        return True, "returned the requested JSON object"
    return False, f"expected {expected}, got {value!r}"[:200]


def check_tool(message: Any, expected_name: str) -> tuple[bool, str]:
    calls = message.tool_calls or []
    if len(calls) != 1:
        return False, f"expected exactly one tool call, got {len(calls)}"
    call = calls[0]
    if call.get("name") != expected_name:
        return False, f"expected {expected_name}, got {call.get('name')}"
    city = str((call.get("args") or {}).get("city", ""))
    if "shanghai" not in city.casefold():
        return False, f"tool args did not name Shanghai: {call.get('args')!r}"
    return True, f"selected {expected_name} with city={city}"


async def run_check(
    model: KimiChatModel,
    *,
    check: str,
    messages: list[Any],
    verifier: Any,
    output: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    prompt_path = output / "prompts" / f"{check}.txt"
    write_text_new(prompt_path, public_prompt(check, messages))
    record: dict[str, Any] = {
        "check": check,
        "passed": False,
        "session_id": None,
        "model_observed": [],
        "model_valid": False,
        "usage": {},
        "model_calls": 0,
        "request_sha256": None,
        "response_sha256": None,
        "content_chars": 0,
        "tool_calls": [],
        "duration_ms": None,
        "error": None,
        "prompt_path": str(prompt_path.relative_to(output)),
        "response_path": None,
    }
    try:
        message = await model.ainvoke(messages)
        passed, detail = verifier(message)
        record.update(
            {
                "passed": passed,
                "detail": detail,
                "session_id": message.response_metadata.get("session_id"),
                "model_observed": message.response_metadata.get("model_observed", []),
                "model_valid": bool(message.response_metadata.get("model_valid")),
                "usage": message.response_metadata.get("usage", {}),
                "model_calls": int(
                    (message.response_metadata.get("usage") or {}).get("model_calls", 0)
                ),
                "request_sha256": message.response_metadata.get("request_sha256"),
                "response_sha256": digest(message.content),
                "content_chars": len(message.content),
                "tool_calls": tool_call_summary(message),
            }
        )
        response_path = output / "responses" / f"{check}.json"
        write_new(
            response_path,
            {
                "check": check,
                "public_test_data": True,
                "content": message.content,
                "tool_calls": tool_call_summary(message),
                "response_metadata": message.response_metadata,
            },
        )
        record["response_path"] = str(response_path.relative_to(output))
    except KimiSeamError as exc:
        record["detail"] = redact(str(exc))
        record["error"] = {
            "code": exc.code,
            "message": redact(str(exc)),
            "details": {
                key: value
                for key, value in exc.details.items()
                if key in {"model_observed", "session_id", "returncode", "tool_calls"}
            },
        }
        record["model_observed"] = list(exc.details.get("model_observed") or [])
        record["model_valid"] = record["model_observed"] == [DEFAULT_MODEL]
        record["session_id"] = exc.details.get("session_id")
        record["usage"] = exc.details.get("usage") or {}
        record["model_calls"] = int(record["usage"].get("model_calls", 0) or 0)
    except Exception as exc:
        record["detail"] = f"{type(exc).__name__}: {redact(str(exc))}"
        record["error"] = {
            "code": "KIMI_SMOKE_UNEXPECTED",
            "message": redact(str(exc)),
            "details": {"type": type(exc).__name__},
        }
    record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    return record


async def run_reason(gateway: KimiLunaGateway, *, timeout_s: float, output: Path) -> dict[str, Any]:
    from tools.benchmark.core import benchmark_claim, build_fixture_adapters
    from tools.kimi_chat_model import build_discovery_components

    from citation_tool.config import Settings

    started = time.monotonic()
    artifact_dir = output / "call-artifacts"
    before = {path.name for path in artifact_dir.glob("*.json")} if artifact_dir.exists() else set()
    reasons, _, _ = build_discovery_components(
        gateway, max_inflight=1, timeout_ms=int(timeout_s * 1000)
    )
    claim = benchmark_claim()
    adapter = next(iter(build_fixture_adapters(Settings()).values()))
    candidate = adapter.search(claim)[0]
    prompt_path = output / "prompts" / "reason.json"
    write_new(
        prompt_path,
        {
            "check": "reason",
            "public_test_data": True,
            "claim": claim.claim_text,
            "candidate_work_id": candidate.passage.work_id,
            "candidate_title": candidate.source.title,
            "note": "synthetic fixture candidate, not a real publication",
        },
    )
    record: dict[str, Any] = {
        "check": "reason",
        "passed": False,
        "status": None,
        "provider": None,
        "model": None,
        "served_model": None,
        "alignment_score": None,
        "missing_constraints": [],
        "failure_code": None,
        "session_id": None,
        "session_ids": [],
        "model_observed": [],
        "model_valid": False,
        "transport_model_valid": False,
        "usage": {},
        "model_calls": 0,
        "request_sha256s": [],
        "text_sha256": None,
        "duration_ms": None,
        "error": None,
        "prompt_path": str(prompt_path.relative_to(output)),
        "response_path": None,
        "artifact_paths": [],
    }
    reason = None
    try:
        reason = await reasons.generate(claim, candidate)
        record.update(
            {
                "status": reason.status,
                "provider": reason.provider,
                "model": reason.model,
                "served_model": reason.served_model,
                "alignment_score": reason.alignment_score,
                "missing_constraints": reason.missing_constraints,
                "failure_code": reason.failure_code,
                "text_sha256": digest(reason.text or ""),
            }
        )
        response_path = output / "responses" / "reason.json"
        write_new(
            response_path,
            {
                "check": "reason",
                "public_test_data": True,
                "status": reason.status,
                "provider": reason.provider,
                "model": reason.model,
                "served_model": reason.served_model,
                "alignment_score": reason.alignment_score,
                "missing_constraints": reason.missing_constraints,
                "failure_code": reason.failure_code,
                "text": reason.text,
            },
        )
        record["response_path"] = str(response_path.relative_to(output))
    except Exception as exc:
        record["failure_code"] = type(exc).__name__
        record["error"] = {
            "code": "KIMI_REASON_UNEXPECTED",
            "message": redact(str(exc)),
            "details": {"type": type(exc).__name__},
        }
    after = {path.name for path in artifact_dir.glob("*.json")} if artifact_dir.exists() else set()
    new_artifacts = [
        artifact_dir / name for name in sorted(after - before) if (artifact_dir / name).is_file()
    ]
    record["artifact_paths"] = [path.name for path in new_artifacts]
    observed: set[str] = set()
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    model_calls = 0
    artifact_valid = bool(new_artifacts)
    for path in new_artifacts:
        value = json.loads(path.read_text(encoding="utf-8"))
        observed.update(value.get("model_observed") or [])
        usage = value.get("usage") or {}
        for key in usage_totals:
            usage_totals[key] += int(usage.get(key, 0) or 0)
        model_calls += int(value.get("model_calls", 0) or 0)
        if value.get("session_id"):
            record["session_ids"].append(value["session_id"])
        if value.get("request_sha256"):
            record["request_sha256s"].append(value["request_sha256"])
        artifact_valid = artifact_valid and bool(value.get("model_valid"))
    usage_totals["model_calls"] = model_calls
    record["usage"] = usage_totals
    record["model_calls"] = model_calls
    record["model_observed"] = sorted(observed)
    record["transport_model_valid"] = artifact_valid
    served = reason.served_model if reason is not None else None
    generated = reason is not None and reason.status == "generated"
    record["model_valid"] = artifact_valid and (served == DEFAULT_MODEL if generated else True)
    record["passed"] = generated and record["model_valid"]
    record["detail"] = (
        f"reason {reason.status}"
        if reason is not None
        else f"reason error {record['failure_code']}"
    )
    if record["session_ids"]:
        record["session_id"] = record["session_ids"][-1]
    record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    return record


def prepare_output(output: Path) -> None:
    if output.exists():
        raise FileExistsError("use a new smoke name; previous outputs remain unchanged")
    output.mkdir(parents=True)


async def run(
    name: str, *, timeout_s: float, output: Path, with_reason: bool = False
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("smoke name must not contain paths")
    await asyncio.to_thread(prepare_output, output)
    started_at = utc_now()
    gateway = KimiLunaGateway(
        KimiLunaConfig(
            timeout_s=timeout_s,
            max_concurrency=1,
            artifact_root=output / "call-artifacts",
        )
    )
    checks: list[dict[str, Any]] = []
    checks.append(
        await run_check(
            gateway.agent_model(),
            check="text",
            messages=[
                SystemMessage(content="You are a terse public test assistant."),
                HumanMessage(content="Reply with exactly the token LUNA and nothing else."),
            ],
            verifier=check_text,
            output=output,
        )
    )
    checks.append(
        await run_check(
            gateway.reason_model(),
            check="json",
            messages=[
                SystemMessage(content="Return only a JSON object; do not add prose."),
                HumanMessage(
                    content='Return a JSON object with keys "ok" (boolean true) and '
                    '"total" (the integer 2+3).'
                ),
            ],
            verifier=check_json,
            output=output,
        )
    )
    checks.append(
        await run_check(
            gateway.agent_model().bind_tools([WEATHER_TOOL, TIME_TOOL], tool_choice="auto"),
            check="tool_weather",
            messages=[
                SystemMessage(
                    content="Choose the single offered tool that matches the request. "
                    "Do not answer in prose."
                ),
                HumanMessage(content="Use the tool that returns the current weather for Shanghai."),
            ],
            verifier=lambda message: check_tool(message, "get_weather"),
            output=output,
        )
    )
    checks.append(
        await run_check(
            gateway.agent_model().bind_tools([WEATHER_TOOL, TIME_TOOL], tool_choice="auto"),
            check="tool_time",
            messages=[
                SystemMessage(
                    content="Choose the single offered tool that matches the request. "
                    "Do not answer in prose."
                ),
                HumanMessage(
                    content="Use the tool that returns the current local time for Shanghai."
                ),
            ],
            verifier=lambda message: check_tool(message, "get_time"),
            output=output,
        )
    )
    if with_reason:
        checks.append(await run_reason(gateway, timeout_s=timeout_s, output=output))
    record = {
        "schema": SMOKE_SCHEMA,
        "name": name,
        "started_at": started_at,
        "finished_at": utc_now(),
        "model_alias_requested": DEFAULT_MODEL_ALIAS,
        "expected_model": DEFAULT_MODEL,
        "transport": TRANSPORT,
        "protocol": PROTOCOL_VERSION,
        "public_test_data_only": True,
        "global_config_changed": False,
        "provider_keys_read": False,
        "checks": checks,
        "overall_ok": all(check["passed"] and check["model_valid"] for check in checks),
        "notes": [
            "Each call is a fresh Kimi CLI session with tools: [] and subagents: [].",
            "The CLI still injects plugin session-start material; the profile tells "
            "the model to ignore it, which is a prompt-level mitigation, not a sandbox.",
            "The CLI sessions remain in the local Kimi home; public data only.",
            "The alias runs at its normal max thinking effort; simple calls can take "
            "10-20 s and reason calls can take over a minute.",
            "This transport differs from ordinary Kimi Code agent runs and cannot be "
            "used to claim architectural gains.",
        ],
    }
    write_new(output / "record.json", record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Real small smoke test for the Kimi gpt-6-luna LangChain seam"
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output-root", type=Path, default=SMOKE_ROOT)
    parser.add_argument(
        "--with-reason",
        action="store_true",
        help="also run one real DeepSeekReasonGenerator call on a public fixture candidate",
    )
    args = parser.parse_args()
    output = args.output_root / args.name
    record = asyncio.run(
        run(
            args.name,
            timeout_s=args.timeout,
            output=output,
            with_reason=args.with_reason,
        )
    )
    record_path = output / "record.json"
    try:
        display = str(record_path.relative_to(PROJECT_ROOT))
    except ValueError:
        display = str(record_path)
    summary = {
        "ok": record["overall_ok"],
        "name": record["name"],
        "record": display,
        "checks": [
            {
                "check": check["check"],
                "passed": check["passed"],
                "model_observed": check["model_observed"],
                "model_valid": check["model_valid"],
                "session_id": check["session_id"],
                "model_calls": check["model_calls"],
                "detail": check.get("detail"),
                "error": check["error"],
            }
            for check in record["checks"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if record["overall_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
