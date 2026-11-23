"""Small native Luna paired check of direct library reading on the inspected SciFact set."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import httpx
from langchain_core.messages import ToolMessage
from tools.benchmark.core import file_hash, secret_leak
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import aggregate, parse_answer, score_record
from tools.luna_credentials import MODEL, authorized_config
from tools.luna_native_product_retest import (
    NativeModelAudit,
    NativeModelSlots,
    build_product_app,
    collect_model_audit,
)
from tools.luna_product_retest import product_settings, ranking_metrics, request_payload
from tools.parallel_luna import MAX_INFLIGHT, SUBJECT, ObservedLunaClient, ParallelLunaGateway
from tools.qasper_benchmark import fetch_json, read_json
from tools.qasper_reader_pair import fingerprint, paired
from tools.quality_benchmark.core import output_details

from citation_tool.entities import utc_now_iso
from citation_tool.metadata_retrieval import MetadataCorpusAdapter

SLICES = ("multi_document", "support", "contradict", "evidence_gap")
ROOT = RUNTIME / "luna-reader-pairs"
KIMI = RUNTIME / "luna-retests/scientific-gap-luna-native-dev-20261118-v3-window10-doi"


def contract(tasks, corpus, ceiling):
    return {
        "schema": "scifact-reader-pair-v1",
        "model": MODEL,
        "ceiling": ceiling,
        "cases": [task["case_id"] for task in tasks],
        "selection": "first 2 per original slice",
        "corpus_sha256": corpus["corpus_sha256"],
        "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "code_sha256": fingerprint(),
        "runner_sha256": file_hash(Path(__file__)),
        "arm_delta": "CITATION_AGENT_LITERATURE_READER",
        "library": "identical sparse BM25; no E5",
        "primary": "correct direction and verbatim quote actually shown; 6 labeled cases",
        "historical_kimi": KIMI.name,
        "historical_hashes": {
            task["case_id"]: file_hash(KIMI / "kimi" / f"{task['case_id']}.json") for task in tasks
        },
    }


def selected(suite):
    return [t for group in SLICES for t in [x for x in suite["tasks"] if x["slice"] == group][:2]]


def doc_id(value):
    match = re.fullmatch(r"(?:work:)?scifact:(\d+)", str(value))
    return match[1] if match else None


class LibraryAudit(NativeModelAudit):
    def __init__(self, directory):
        super().__init__(directory, NativeModelSlots())
        self.delivered: dict[str, set[str]] = defaultdict(set)
        self.opened: dict[str, str] = {}
        self.reader_actions = Counter()
        self.reader_docs = set()
        self.seen = set()

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        await super().on_chat_model_start(serialized, messages, run_id=run_id, **kwargs)
        for group in messages:
            for message in group:
                if (
                    not isinstance(message, ToolMessage)
                    or message.name not in {"search_literature", "read_literature"}
                    or message.tool_call_id in self.seen
                ):
                    continue
                self.seen.add(message.tool_call_id)
                try:
                    receipt = json.loads(message.content)
                except (ValueError, TypeError):
                    continue
                if message.name == "search_literature":
                    for row in receipt.get("candidate_diagnostics", []):
                        if key := doc_id(row.get("work_id")):
                            self.delivered[key].add(row.get("evidence_excerpt") or "")
                            for s in row.get("source_records", []):
                                self.delivered[key].add(s.get("abstract") or "")
                    continue
                action = receipt.get("action") or "error"
                self.reader_actions[action] += 1
                ref = receipt.get("text_ref")
                if action == "open" and ref:
                    if key := doc_id(receipt.get("source", {}).get("work_id")):
                        self.opened[ref] = key
                if key := self.opened.get(ref):
                    for row in receipt.get("passages", []) + receipt.get("matches", []):
                        if text := row.get("text"):
                            self.delivered[key].add(text)
                            self.reader_docs.add(key)


async def subject(root, task, arm, adapter, config, api):
    case = task["case_id"]
    runtime = root / "runs" / case / arm
    await asyncio.to_thread(runtime.mkdir, parents=True, exist_ok=False)
    audit = LibraryAudit(runtime / "model-calls")
    token = SUBJECT.set(f"{case}/{arm}")
    started = time.monotonic()
    run = {}
    queries = []
    error = None
    try:
        settings = replace(
            product_settings(runtime, task),
            embedding_enabled=False,
            libraries=(adapter.library,),
            agent_literature_reader=arm == "on",
        )
        gateway = ParallelLunaGateway(config, api, audit)
        app, _, _ = build_product_app(settings, adapter, gateway, None)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as local,
        ):
            payload, _ = request_payload(task)
            created = await fetch_json(local, "POST", "/api/v1/agent/runs", json=payload)
            job = app.state.agent_runner._tasks.get(created["run_id"])
            if job is not None:
                await job
            run = await fetch_json(local, "GET", f"/api/v1/agent/runs/{created['run_id']}")
            for item in run.get("queries", []):
                q = app.state.search_service.state(
                    settings.default_tenant, item["created"]["query_id"]
                )
                if q is not None:
                    queries.append(output_details(q))
    except Exception as exc:  # noqa: BLE001 - record failed first allocations
        error = type(exc).__name__
    finally:
        SUBJECT.reset(token)
    models = collect_model_audit(runtime / "model-calls")
    answer = run.get("summary") or ""
    if secret_leak(answer, [config.api_key]):
        answer, error = "", "POTENTIAL_SECRET_WITHHELD"
    parsed, _ = parse_answer(answer)
    evidence_seen = (
        [
            any(item.quote in text for text in audit.delivered.get(item.doc_id, ()))
            for item in parsed.papers
        ]
        if parsed
        else []
    )
    item = {
        "arm": arm,
        "system": "product",
        "case_id": case,
        "run_case_id": case,
        "response_text": answer,
        "model_valid": models["model_valid"],
        "model_observed": models["served_models"],
        "completed": bool(answer and not error and run.get("status") == "completed"),
        "error": error or (run.get("error") or {}).get("code"),
        "run_id": run.get("run_id"),
        "queries": queries,
        "model_calls": models["model_calls"],
        "wall_time_ms": (time.monotonic() - started) * 1000,
        "reader_actions": dict(audit.reader_actions),
        "reader_doc_ids": sorted(audit.reader_docs),
        "delivered_doc_ids": sorted(audit.delivered),
        "quoted_text_observed": evidence_seen,
        "rankings": ranking_metrics(queries),
    }
    await asyncio.to_thread(save_new, runtime / "record.json", item)
    print(
        json.dumps(
            {
                key: item[key]
                for key in (
                    "case_id",
                    "arm",
                    "completed",
                    "error",
                    "reader_actions",
                    "model_calls",
                )
            }
        ),
        flush=True,
    )
    return item


def assess(root, tasks, corpus):
    rows = []
    for task in tasks:
        case = task["case_id"]
        for arm in ("off", "on", "historical_kimi"):
            path = (
                KIMI / "kimi" / f"{case}.json"
                if arm == "historical_kimi"
                else root / "runs" / case / arm / "record.json"
            )
            if not path.exists():
                continue
            raw = read_json(path)
            row = score_record(raw, task, corpus)
            targets = task["expect"]["evidence"]
            assessment = row["assessment"]
            valid = assessment["valid_completion"] and assessment["format_valid"]
            seen = raw.get("quoted_text_observed") or []
            score = (
                len(
                    {
                        p["doc_id"]
                        for p in assessment["papers"]
                        if p["exact_quote"]
                        and p["stance_correct"] is True
                        and not p["duplicate"]
                        and p["rank"] <= len(seen)
                        and seen[p["rank"] - 1]
                        and len(re.findall(r"\w+", p["quote"])) >= 4
                    }
                )
                / len(targets)
                if valid and targets
                else 0.0
                if targets
                else None
            )
            rows.append(
                {
                    **row,
                    "arm": arm,
                    "case": case,
                    "legal_final": valid,
                    "strict_delivered": score if arm != "historical_kimi" else None,
                }
            )
    result = {}
    for arm in ("off", "on", "historical_kimi"):
        arm_rows = [x for x in rows if x["arm"] == arm]
        result[arm] = {
            **aggregate(arm_rows),
            "legal_finals": sum(x["legal_final"] for x in arm_rows),
            "read_tool_used": sum(bool(x.get("reader_actions")) for x in arm_rows),
            "errors": dict(Counter(x["error"] for x in arm_rows if x.get("error"))),
            "queries": sum(len(x.get("queries", [])) for x in arm_rows),
            "strict_delivered": paired(rows, "strict_delivered") if arm == "on" else None,
        }
    return {
        "systems": result,
        "rows": [{k: v for k, v in x.items() if k != "queries"} for x in rows],
    }


async def run(root, ceiling):
    suite, corpus = await asyncio.to_thread(load_data)
    tasks = selected(suite)
    plan = contract(tasks, corpus, ceiling)
    save_new(root / "plan.json", {**plan, "created_at": utc_now_iso()})
    config = await asyncio.to_thread(authorized_config)
    lib = replace(
        product_settings(root, tasks[0]).libraries[0], adapter_id="metadata-snapshot-bm25-v1"
    )
    adapter = await asyncio.to_thread(MetadataCorpusAdapter, lib, corpus["records"], top_k=10)
    jobs = [
        (case, arm)
        for i, case in enumerate(tasks)
        for arm in (("on", "off") if i % 2 else ("off", "on"))
    ]
    async with ObservedLunaClient(ceiling) as api:
        slots = asyncio.Semaphore(ceiling)

        async def visit(case, arm):
            async with slots:
                if not api.blocked:
                    return await subject(root, case, arm, adapter, config, api)
                return None

        first = await asyncio.gather(*(visit(c, a) for c, a in jobs[:4]))
        if any(row and row["model_valid"] for row in first) and not api.blocked:
            await api.expand_after_pilot()
            await asyncio.gather(*(visit(c, a) for c, a in jobs[4:]))
        save_new(
            root / "http.json", {"rows": api.rows, "capacity": api.transitions, "peak": api.peak}
        )
    if contract(tasks, corpus, ceiling) != plan:
        raise ValueError("frozen SciFact execution contract changed")
    result = assess(root, tasks, corpus)
    result.update(
        allocated_per_arm=len(tasks),
        peak=api.peak,
        http_calls=len(api.rows),
        http_status=dict(Counter(str(x["status"]) for x in api.rows)),
        blocked=api.blocked,
        capacity=api.transitions,
        wall_s=time.monotonic() - api.started_at,
    )
    save_new(root / "assessment.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--ceiling", type=int, choices=range(1, MAX_INFLIGHT + 1), default=MAX_INFLIGHT
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", args.name):
        parser.error("invalid --name")
    result = asyncio.run(run(ROOT / args.name, args.ceiling))
    print(
        json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False, indent=2)
    )
    return 2 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
