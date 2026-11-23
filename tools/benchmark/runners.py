from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.reasons import (
    DeepSeekModelGateway,
    DeepSeekReasonGenerator,
    load_model_config,
)

from .core import (
    KIMI_MODEL_ALIAS,
    MODEL_ID,
    ROOT,
    RUNTIME_ROOT,
    BenchmarkUsageCallback,
    UsageTotals,
    benchmark_adapters,
    elapsed_ms,
    export_corpus,
    kimi_executable,
    kimi_prompt,
    product_request,
    secret_leak,
    utc_now_iso,
)

KIMI_MAX_STEPS_PER_TURN = 96


class BenchmarkReasonGenerator(DeepSeekReasonGenerator):
    provider = "deepseek"


def _gateway(settings: Settings) -> DeepSeekModelGateway:
    config = load_model_config(settings.model_config_path)
    if config is None:
        raise ValueError("benchmark model configuration is unavailable")
    return DeepSeekModelGateway(
        config,
        max_inflight=settings.model_max_inflight,
        timeout_ms=settings.model_timeout_ms,
    )


async def run_product(task: dict[str, Any], run_name: str) -> dict[str, Any]:
    case_id = task["run_case_id"]
    started_at = utc_now_iso()
    started = time.monotonic()
    records = export_corpus(inject=task["expect"].get("inject_untrusted_corpus_note", False))
    runtime = RUNTIME_ROOT / run_name / "product" / case_id
    runtime.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings(), db_path=runtime / "app.db")
    settings.validate()
    gateway = _gateway(settings)
    callback = BenchmarkUsageCallback()
    agent_model = gateway.agent_model()
    reason_model = gateway.reason_model()
    agent_model.callbacks = [callback]
    reason_model.callbacks = [callback]
    reasons = BenchmarkReasonGenerator(
        gateway,
        max_inflight=settings.model_max_inflight,
        timeout_ms=settings.model_timeout_ms,
        model=reason_model,
    )
    adapters = benchmark_adapters(settings, records)
    app = create_app(settings, adapters=adapters, reason_generator=reasons, agent_model=agent_model)
    tenant = f"benchmark-{case_id}"
    transport = httpx.ASGITransport(app=app)
    run: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://benchmark.local", timeout=20
        ) as client:
            response = await client.post(
                "/api/v1/agent/runs",
                json=product_request(task).model_dump(mode="json", exclude_none=True),
                headers={"X-Tenant-ID": tenant},
            )
            response.raise_for_status()
            run_id = response.json()["run_id"]
            deadline = time.monotonic() + settings.agent_timeout_ms / 1000 + 10
            while time.monotonic() < deadline:
                response = await client.get(
                    f"/api/v1/agent/runs/{run_id}", headers={"X-Tenant-ID": tenant}
                )
                response.raise_for_status()
                run = response.json()
                if run["status"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.05)
            else:
                error = {"code": "BENCHMARK_TIMEOUT", "message": "Agent polling timed out"}
    except Exception as exc:
        error = {"code": "BENCHMARK_RUNNER_ERROR", "message": type(exc).__name__}

    queries = []
    first_result_ms = None
    if run:
        for agent_query in run.get("queries", []):
            query_id = agent_query["created"]["query_id"]
            state = app.state.search_service.state(tenant, query_id)
            if state is None:
                continue
            events = app.state.store.list_events(tenant, query_id, 0, limit=10_000)
            first = next(
                (event for event in events if event["event_type"] == "recommendation_created"),
                None,
            )
            if first:
                elapsed = elapsed_ms(run["created_at"], first["occurred_at"])
                first_result_ms = (
                    elapsed if first_result_ms is None else min(first_result_ms, elapsed)
                )
            queries.append(_query_details(state))

    visible_output = _product_visible_output(run, queries, error)
    usage = callback.totals.as_dict()
    served = set(usage["served_models"])
    if run and run.get("served_model"):
        served.add(run["served_model"])
    reason_models = {
        card["reason"].get("served_model")
        for query in queries
        for card in query["results"]
        if card.get("reason", {}).get("served_model")
    }
    served.update(reason_models)
    model_valid = bool(usage["model_calls"]) and served == {MODEL_ID}
    reasons = [card["reason"] for query in queries for card in query["results"]]
    generated_reasons = [reason for reason in reasons if reason["status"] == "generated"]
    reason_texts = [reason.get("text") or "" for reason in generated_reasons]
    return {
        "system": "product",
        "case_id": task["case_id"],
        "run_case_id": case_id,
        "repetition": task["repetition"],
        "mode": task["mode"],
        "started_at": started_at,
        "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
        "completed": bool(run and run["status"] == "completed" and not error),
        "response_text": run.get("summary") or "",
        "visible_output": visible_output,
        "error": error or run.get("error"),
        "agent_run_id": run.get("run_id"),
        "agent_status": run.get("status"),
        "model_requested": MODEL_ID,
        "model_observed": sorted(served),
        "model_valid": model_valid,
        "usage": usage,
        "tool_calls": run.get("tool_calls", []),
        "retrieval_actions": len(run.get("tool_calls", [])),
        "total_tool_calls": len(run.get("tool_calls", [])),
        "queries": queries,
        "first_result_commit_ms": first_result_ms,
        "reason_metrics": {
            "count": len(reasons),
            "generated": len(generated_reasons),
            "failed": len(reasons) - len(generated_reasons),
            "generation_rate": len(generated_reasons) / len(reasons) if reasons else None,
            "length_80_180_rate": (
                sum(80 <= len(text) <= 180 for text in reason_texts) / len(reason_texts)
                if reason_texts
                else None
            ),
            "distinct_text_rate": (
                len(set(reason_texts)) / len(reason_texts) if reason_texts else None
            ),
            "input_hashes_unique": len({reason["input_hash"] for reason in reasons})
            == len(reasons),
        },
        "transport": "real LangChain/SearchService/SQLite through in-process ASGI",
        "thinking": "disabled",
        "secret_leak": secret_leak(visible_output, []),
    }


def _query_details(state) -> dict[str, Any]:
    ranks = {entry.result_id: entry.rank for group in state.groups for entry in group.entries}
    cards = []
    for item in state.results:
        source = item.sources[0]
        cards.append(
            {
                "result_id": item.result_id,
                "work_id": item.work_id,
                "rank": ranks.get(item.result_id),
                "rankable_group": item.rankable_group,
                "title": source.title,
                "year": source.year,
                "doi": source.doi,
                "library_ids": [source.library_id for source in item.sources],
                "display_score": item.display_score,
                "relationship": item.relationship,
                "verification_status": item.verification_status,
                "citable": item.citable,
                "simulation": item.simulation,
                "claim_scope": item.claim.scope.model_dump(mode="json"),
                "evidence": [
                    {
                        "quote": evidence.quote,
                        "pdf_page": evidence.locator.pdf_page,
                        "section_path": evidence.locator.section_path,
                        "text_offset_valid": evidence.text_offset_valid,
                        "pdf_location_valid": evidence.pdf_location_valid,
                        "reading_order_valid": evidence.reading_order_valid,
                        "source_trace_valid": evidence.source_trace_valid,
                        "scope_match": evidence.scope_match,
                        "scope": evidence.scope.model_dump(mode="json"),
                    }
                    for evidence in item.evidence
                ],
                "reason": item.reason.model_dump(mode="json") if item.reason else None,
            }
        )
    cards.sort(key=lambda card: (card["rank"] is None, card["rank"] or 10_000, card["result_id"]))
    return {
        "query_id": state.session.query_id,
        "input_type": state.session.input_type,
        "search_state": state.session.search_state,
        "requested_library_ids": state.session.requested_library_ids,
        "coverage": state.coverage.model_dump(mode="json"),
        "workers": [
            {
                "library_id": worker.library_id,
                "state": worker.worker_state,
                "candidates_seen": worker.candidates_seen,
                "recommendations_emitted": worker.recommendations_emitted,
            }
            for worker in state.workers
        ],
        "scope_source": state.scope_source,
        "final_snapshot_id": state.session.final_snapshot_id,
        "results": cards,
    }


def _product_visible_output(
    run: dict[str, Any], queries: list[dict[str, Any]], error: dict[str, Any] | None
) -> str:
    sections = [
        "[Agent 最终摘要]",
        run.get("summary") or json.dumps(error or run.get("error"), ensure_ascii=False),
    ]
    for query in queries:
        lines = [
            f"[结果卡片 query_id={query['query_id']}；模拟 fixture；状态={query['search_state']}]"
        ]
        if not query["results"]:
            lines.append("没有可展示的可引用候选。")
        for card in query["results"]:
            evidence = card["evidence"][0] if card["evidence"] else {}
            reason = card.get("reason") or {}
            lines.append(
                json.dumps(
                    {
                        "rank": card["rank"],
                        "title": card["title"],
                        "doi": card["doi"],
                        "year": card["year"],
                        "display_score_fixture": card["display_score"],
                        "relationship_deterministic": card["relationship"],
                        "pdf_page": evidence.get("pdf_page"),
                        "section": evidence.get("section_path"),
                        "quote": evidence.get("quote"),
                        "reason": reason.get("text"),
                        "reason_status": reason.get("status"),
                    },
                    ensure_ascii=False,
                )
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def run_kimi(
    task: dict[str, Any],
    run_name: str,
    secrets: list[str],
    *,
    corpus_payload: dict[str, Any] | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    case_id = task["run_case_id"]
    started_at = utc_now_iso()
    started = time.monotonic()
    runtime = RUNTIME_ROOT / run_name / "kimi"
    runtime.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="literature-benchmark-kimi-") as temporary:
        workspace = Path(temporary)
        (workspace / "corpus.json").write_text(
            json.dumps(
                corpus_payload
                or {
                    "corpus_id": "m0-fixture-v1",
                    "simulation": True,
                    "records": export_corpus(
                        inject=task["expect"].get("inject_untrusted_corpus_note", False)
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
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
        try:
            process = subprocess.run(
                [
                    kimi_executable(),
                    "-m",
                    KIMI_MODEL_ALIAS,
                    "--output-format",
                    "stream-json",
                    "-p",
                    prompt or kimi_prompt(task),
                ],
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            stdout = process.stdout
            stderr = process.stderr
            returncode = process.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = "benchmark subprocess timeout"
            returncode = 124
        stream_path = runtime / f"{case_id}.stream.jsonl"
        stream_path.write_text(stdout, encoding="utf-8")
        leaked = secret_leak(stdout, secrets)
        parsed = _parse_stream(stdout)
        session_id = parsed["session_id"]
        export_path = runtime / f"{case_id}.zip"
        export_error = None
        if session_id:
            exported = subprocess.run(
                [
                    kimi_executable(),
                    "export",
                    session_id,
                    "--no-include-global-log",
                    "-o",
                    str(export_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
            if exported.returncode or not export_path.is_file():
                export_error = "session export failed"
        else:
            export_error = "session id missing"
        if export_path.is_file():
            raw = _parse_kimi_export(export_path, started_at)
        raw.update(
            {
                "system": "kimi_code",
                "case_id": task["case_id"],
                "run_case_id": case_id,
                "repetition": task["repetition"],
                "mode": task.get("mode", task.get("slice", "discovery")),
                "started_at": started_at,
                "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
                "completed": returncode == 0 and bool(parsed["response_text"]),
                "response_text": "[WITHHELD: potential secret]"
                if leaked
                else parsed["response_text"],
                "visible_output": "[WITHHELD: potential secret]"
                if leaked
                else parsed["response_text"],
                "transcript_text": "[WITHHELD: potential secret]"
                if leaked
                else parsed["transcript_text"],
                "error": None
                if returncode == 0
                else {"code": "KIMI_PROCESS_FAILED", "message": stderr[-500:]},
                "session_id": session_id,
                "observability_error": export_error,
                "model_requested": KIMI_MODEL_ALIAS,
                "transport": "fresh Kimi Code CLI print-mode session",
                "stream_path": str(stream_path.relative_to(ROOT)),
                "export_path": str(export_path.relative_to(ROOT))
                if export_path.is_file()
                else None,
                "secret_leak": leaked,
            }
        )
    raw.setdefault("model_valid", False)
    raw.setdefault("usage", UsageTotals().as_dict())
    raw.setdefault("retrieval_actions", 0)
    raw.setdefault("total_tool_calls", 0)
    raw.setdefault("tool_calls", [])
    return raw


def _parse_stream(stdout: str) -> dict[str, Any]:
    messages = []
    session_id = None
    cli_version = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "session.resume_hint":
            session_id = event.get("session_id")
        elif event.get("type") == "system.version":
            cli_version = event.get("version")
        elif event.get("role") == "assistant" and isinstance(event.get("content"), str):
            if event["content"].strip():
                messages.append(event["content"].strip())
    return {
        "session_id": session_id,
        "cli_version": cli_version,
        "response_text": messages[-1] if messages else "",
        "transcript_text": "\n\n".join(messages).strip(),
    }


def _parse_kimi_export(path: Path, started_at: str) -> dict[str, Any]:
    usage = UsageTotals()
    models = set()
    aliases = set()
    thinking = set()
    tool_calls = []
    tool_results = []
    agent_md_paths = set()
    prompt_hashes = set()
    model_requests = 0
    usage_reports = 0
    with zipfile.ZipFile(path) as archive:
        wires = [name for name in archive.namelist() if name.endswith("/wire.jsonl")]
        for name in wires:
            for line in archive.read(name).decode("utf-8").splitlines():
                record = json.loads(line)
                kind = record.get("type")
                if kind == "profile.bind":
                    aliases.add(record.get("modelAlias"))
                    thinking.add(record.get("thinkingEffort"))
                    agent_md_paths.update(record.get("agentsMdPaths") or [])
                elif kind == "llm.request":
                    model_requests += 1
                    models.add(record.get("model"))
                    aliases.add(record.get("modelAlias"))
                    thinking.add(record.get("thinkingEffort"))
                    prompt_hashes.add(record.get("systemPromptHash"))
                elif kind == "context.append_loop_event":
                    event = record.get("event", {})
                    event_type = event.get("type")
                    if event_type == "step.end" and event.get("usage"):
                        usage_reports += 1
                        usage.add_kimi(event["usage"])
                    elif event_type == "tool.call":
                        tool_calls.append(
                            {
                                "name": event.get("name"),
                                "arguments": event.get("args") or {},
                                "time": record.get("time"),
                                "tool_call_id": event.get("toolCallId"),
                            }
                        )
                    elif event_type == "tool.result":
                        tool_results.append(record.get("time"))
    retrieval_actions = sum(
        _is_retrieval_action(index, call, tool_calls) for index, call in enumerate(tool_calls)
    )
    observed_models = {model for model in models if model}
    usage.served_models.update(observed_models)
    return {
        "usage": usage.as_dict(),
        "model_request_count": model_requests,
        "usage_report_count": usage_reports,
        "usage_complete": usage_reports == model_requests,
        "model_observed": sorted(observed_models),
        "model_aliases": sorted(alias for alias in aliases if alias),
        "model_valid": bool(models) and observed_models == {MODEL_ID},
        "thinking_efforts": sorted(value for value in thinking if value),
        "agents_md_paths": sorted(agent_md_paths),
        "system_prompt_hashes": sorted(value for value in prompt_hashes if value),
        "tool_calls": tool_calls,
        "retrieval_actions": retrieval_actions,
        "total_tool_calls": len(tool_calls),
        "first_tool_result_ms": min(
            (elapsed_ms(started_at, value) for value in tool_results if value),
            default=None,
        ),
    }


def _is_retrieval_action(index: int, call: dict[str, Any], all_calls: list[dict[str, Any]]) -> bool:
    name = str(call.get("name") or "")
    arguments = call.get("arguments") or {}
    serialized = json.dumps(arguments, ensure_ascii=False).lower()
    if any(
        previous.get("name") == name
        and json.dumps(previous.get("arguments") or {}, sort_keys=True)
        == json.dumps(arguments, sort_keys=True)
        for previous in all_calls[:index]
    ):
        return False
    if name in {"WebSearch", "Grep"}:
        return True
    if name == "Read" and "corpus.json" in serialized:
        return not any(
            previous.get("name") in {"Read", "Grep", "Bash", "WebSearch"}
            and "corpus.json" in json.dumps(previous.get("arguments") or {}).lower()
            for previous in all_calls[:index]
        )
    if name == "Bash" and "corpus.json" in serialized:
        previous_access = any(
            previous.get("name") in {"Read", "Grep", "Bash", "WebSearch"}
            and "corpus.json" in json.dumps(previous.get("arguments") or {}).lower()
            for previous in all_calls[:index]
        )
        if not previous_access:
            return True
        if re.search(r"10\.\d{4,9}/[a-z0-9._;()/:-]+", serialized):
            return False
        return bool(
            re.search(
                r"\bany\s*\(|\bfilter\s*\(|\bkws?\b|\bhits?\b|\bkeywords?\b|"
                r"\bterms?\b|if[^\n]*\bin\s+(?:r\[|recs?\b|\(|t\b|a\b|s\b)",
                serialized,
            )
        )
    lowered = name.lower()
    return "search" in lowered and name not in {"search_issues", "search_repositories"}
