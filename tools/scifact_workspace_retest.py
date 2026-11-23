"""Frozen SciFact product replay with native Luna, real E5 and observable evidence delivery."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from langchain_core.messages import ToolMessage
from tools import luna_native_product_retest as native
from tools import luna_product_retest as product
from tools.benchmark.core import file_hash
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, save_new
from tools.gap_benchmark.metrics import aggregate, paired_summary, score_record
from tools.gap_benchmark.opencode_parallel import (
    CASE_RESERVE,
    START_RESERVE,
    SharedEmbeddingIndex,
    full_dense_common_score,
    require_memory,
)
from tools.gap_benchmark.opencode_parent_pair import attach_retrieval_audit, ready_index
from tools.luna_credentials import MODEL, authorized_config
from tools.luna_gap_retest import first_loss_by_target, prepare_run
from tools.parallel_luna import SUBJECT, ObservedLunaClient, ParallelLunaGateway
from tools.quality_benchmark.core import project_skill_isolation_valid

from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json

HISTORY = RUNTIME / "luna-retests/scifact-luna-v12-20261119"
ARMS = ("default", "workspace")
PROBES = ("sf-873", "sf-1029")


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def numeric_id(value) -> str | None:
    match = re.fullmatch(r"(?:(?:work:)?scifact:)?([0-9]+)", str(value))
    return match[1] if match else None


class AuditedLunaClient(ObservedLunaClient):
    async def send(self, request, **kwargs):
        body = json.loads(request.content)
        if body.get("model") != MODEL or body.get("store") is not False or body.get("reasoning"):
            raise ValueError("transport differs from the frozen Luna product configuration")
        return await super().send(request, **kwargs)


class EvidenceAudit(native.NativeModelAudit):
    def __init__(self, directory, abstracts):
        super().__init__(directory, native.NativeModelSlots())
        self.abstracts = abstracts
        self.seen = set()
        self.deliveries = []
        self.opened = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        await super().on_chat_model_start(serialized, messages, run_id=run_id, **kwargs)
        for group in messages:
            for message in group:
                if (
                    isinstance(message, ToolMessage)
                    and message.name
                    in {"search_literature", "read_literature", "Read", "Grep", "Glob"}
                    and message.tool_call_id not in self.seen
                ):
                    self.seen.add(message.tool_call_id)
                    try:
                        receipt = json.loads(message.content)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(receipt, dict):
                        self.observe(message.name, receipt)

    def observe(self, tool, receipt):
        spans = defaultdict(set)

        def put(identifier, text):
            key = numeric_id(identifier)
            if (
                not key
                or key not in self.abstracts
                or not isinstance(text, str)
                or not text.strip()
            ):
                return
            source = self.abstracts[key]
            start = source.find(text)
            while start >= 0:
                spans[key].add((start, start + len(text)))
                start = source.find(text, start + 1)

        rank_reason_texts = None
        if tool == "search_literature":
            candidates = receipt.get("candidate_diagnostics", [])
            rank_reason_texts = sum(
                bool((candidate.get("reason") or {}).get("text")) for candidate in candidates
            )
            for candidate in candidates:
                work = candidate.get("work_id")
                put(work, candidate.get("evidence_excerpt"))
                for source in candidate.get("source_records", []):
                    put(source.get("paper_id") or work, source.get("abstract"))
        elif tool == "read_literature":
            ref = receipt.get("text_ref")
            if ref and receipt.get("action") == "open":
                source = receipt.get("source", {})
                self.opened[ref] = source.get("work_id") or source.get("paper_id")
            for part in receipt.get("passages", []) + receipt.get("matches", []):
                put(self.opened.get(ref), part.get("text"))
        elif tool in {"Read", "Grep"}:
            provenance = receipt.get("provenance", {})
            source = provenance.get("source", {})
            work = provenance.get("work_id") or source.get("work_id") or source.get("paper_id")
            for line in receipt.get("lines", []):
                anchor = line.get("anchor") or {}
                if anchor.get("kind") in {"abstract", "abstract_excerpt"} or (
                    receipt.get("kind") == "abstract" and anchor.get("block_index") is not None
                ):
                    put(anchor.get("paper_id") or work, line.get("text"))
        self.deliveries.append(
            {
                "tool": tool,
                "query_id": receipt.get("query_id"),
                "action": receipt.get("action"),
                "kind": receipt.get("kind"),
                "path": receipt.get("path"),
                "error": (receipt.get("error") or {}).get("code"),
                "rank_reason_texts": rank_reason_texts,
                "spans": {key: sorted(value) for key, value in spans.items()},
            }
        )


def contract(root, suite, corpus, cache_hash, cases, arms):
    historical = read(HISTORY / "product-contract.json")
    if (
        historical["corpus_sha256"] != corpus["corpus_sha256"]
        or historical["tasks_sha256"] != file_hash(SUITE_ROOT / "tasks.json")
        or historical["e5_cache_sha256"] != cache_hash
    ):
        raise ValueError("must use the 69.7%/83.2% run's exact cases, corpus and E5 cache")
    return {
        "schema": "scifact-workspace-retest-v1",
        "native_implementation": native.product_implementation_hash(root, corpus),
        "runner_sha256": file_hash(Path(__file__)),
        "http_sha256": file_hash(Path(__file__).with_name("parallel_luna.py")),
        "scoring_sha256": file_hash(SUITE_ROOT.parents[2] / "tools/gap_benchmark/metrics.py"),
        "historical": {
            "name": HISTORY.name,
            "assessment_sha256": file_hash(HISTORY / "assessment-1.json"),
        },
        "arms": arms,
        "features": {
            "default": "current product with workspace, richer context and separate reader off",
            "workspace": "workspace and research context on; workspace also exposes reader tools",
        },
        "cases": [t["case_id"] for t in suite["tasks"] if t["case_id"] in cases],
        "tasks_sha256": historical["tasks_sha256"],
        "corpus_sha256": corpus["corpus_sha256"],
        "e5_cache_sha256": cache_hash,
        "probes": list(PROBES),
        "model": MODEL,
        "reasoning": "official Responses; no reasoning parameter; Kimi effort verified separately",
        "provider_storage": False,
        "transport": "observed Responses without SDK retries; single shared E5 index",
        "limits": historical["limits"] | {"max_parallel_subjects": 2},
        "primary": "original correct_direction_evidence_coverage (24 labeled claims per arm)",
        "scope": "inspected development tasks; no gold, LTR or sparse substitution",
    }


async def settle_case(audit, *, drain_seconds):
    started = time.monotonic()
    try:
        async with asyncio.timeout(drain_seconds):
            await audit.slots.idle.wait()
        audit.seal()
    except TimeoutError:
        code = "NATIVE_MODEL_DRAIN_TIMEOUT"
    except RuntimeError:
        code = "NATIVE_MODEL_AUDIT_UNSEALED"
    else:
        code = None
    return code, round((time.monotonic() - started) * 1000, 3)


async def one(
    root,
    task,
    arm,
    config,
    api,
    corpus,
    abstracts,
    index,
    frozen_id,
    phase,
    *,
    hide_rank_rationale=False,
):
    case = task["case_id"]
    target = root / arm / f"{case}.json"
    previous = product.load_reusable_record(target, resume=True, implementation_hash=frozen_id)
    if previous is not None:
        return previous
    runtime = product.new_attempt_dir(root, f"{arm}-{case}")
    memory = require_memory(CASE_RESERVE)
    audit = EvidenceAudit(runtime / "model-calls", abstracts)
    gateway = ParallelLunaGateway(config, api, audit)
    events = []

    def build(settings, adapter, gateway, callback):
        attach_retrieval_audit(adapter, events)
        settings = replace(
            settings,
            agent_research_workspace=arm == "workspace",
            agent_research_context=arm == "workspace",
            agent_parent_hide_rank_rationale=hide_rank_rationale,
        )
        return native.build_product_app(settings, adapter, gateway, callback)

    started = time.monotonic() - api.started_at
    token = SUBJECT.set(f"{arm}/{case}")
    try:
        row = await product.run_case(
            {k: v for k, v in task.items() if k != "expect"},
            root=root,
            runtime=runtime,
            corpus=corpus,
            index=index,
            implementation_hash=frozen_id,
            shared_async=asyncio.Semaphore(2),
            shared_sync=threading.BoundedSemaphore(2),
            gateway_override=gateway,
            app_builder=build,
            audit_collector=native.collect_model_audit,
        )
        drain_error, drain_ms = await settle_case(audit, drain_seconds=product.MODEL_TIMEOUT_S + 10)
    finally:
        SUBJECT.reset(token)
    row = native._native_record(row, native.collect_model_audit(runtime / "model-calls"), runtime)
    if drain_error:
        row.update(
            completed=False,
            model_valid=False,
            session_isolation_valid=False,
            error={"code": drain_error, "message": "Benchmark could not close the model audit"},
        )
    row.update(
        arm=arm,
        phase=phase,
        audit_shutdown={"error": drain_error, "wait_ms": drain_ms},
        evidence_deliveries=audit.deliveries,
        retrieval_audit=events,
        dense_retrieval=bool(events) and all(e["dense_status"] == "available" for e in events),
        dense_common_score=full_dense_common_score(row["queries"]),
        memory_on_start=memory,
        started_s=started,
        ended_s=time.monotonic() - api.started_at,
    )
    if row["secret_leak"] or config.api_key in row["visible_output"]:
        raise ValueError("potential credential leak; output not persisted")
    save_new(target, row)
    print(
        json.dumps(
            {
                "case": case,
                "arm": arm,
                "completed": row["completed"],
                "model_valid": row["model_valid"],
                "dense": row["dense_retrieval"],
                "common": row["dense_common_score"],
                "rank": row["ranking_metrics"]["completed"],
                "error": (row["error"] or {}).get("code"),
                "calls": row["model_request_count"],
                "seconds": round(row["ended_s"] - started, 1),
            }
        ),
        flush=True,
    )
    return row


def qualified(row, task, corpus):
    scored = score_record(row, task, corpus)["assessment"]
    rank = row["ranking_metrics"]
    return (
        scored["valid_completion"]
        and scored["format_valid"]
        and row["dense_retrieval"]
        and row["dense_common_score"]
        and rank["all_completed"]
        and rank["query_count"] == rank["queries_with_completed"]
    )


def overlap(rows):
    successful = [r for r in rows if r["status"] == 200]
    return any(
        left["subject"] != right["subject"]
        and left["start_s"] < right["end_s"]
        and right["start_s"] < left["end_s"]
        for i, left in enumerate(successful)
        for right in successful[i + 1 :]
    )


async def run(name, *, pilot_only):
    root, suite, corpus = prepare_run(name)
    tasks = {t["case_id"]: t for t in suite["tasks"]}
    abstracts = {r["doc_id"]: r["abstract"] for r in corpus["records"]}
    run_id = uuid4().hex[:10]
    lock_path = RUNTIME / "opencode-parallel/.shared-e5.lock"
    with FileLock(lock_path, timeout=0):
        memory = require_memory(START_RESERVE)
        index, cache_hash = await asyncio.to_thread(
            ready_index, root, tasks[PROBES[0]], corpus, index_type=SharedEmbeddingIndex
        )
        plan = contract(root, suite, corpus, cache_hash, tasks, list(ARMS))
        plan_path = root / "workspace-contract.json"
        if plan_path.exists():
            if plan != read(plan_path):
                raise ValueError("frozen workspace plan changed; choose a different run name")
        else:
            save_new(plan_path, plan)
        frozen_id = sha256_json(plan)
        config = await asyncio.to_thread(authorized_config)
        gate_path = root / "workspace-pilot.json"
        async with AuditedLunaClient(2) as api:
            drain_blocked = False

            async def visit(case, arm):
                nonlocal drain_blocked
                row = await one(
                    root, tasks[case], arm, config, api, corpus, abstracts, index, frozen_id, run_id
                )
                drain_blocked |= bool((row.get("audit_shutdown") or {}).get("error"))
                return row

            try:
                if gate_path.exists():
                    gate = read(gate_path)
                else:
                    probe_rows = await asyncio.gather(*(visit(PROBES[0], a) for a in ARMS))
                    accepted = [qualified(row, tasks[PROBES[0]], corpus) for row in probe_rows]
                    gate = {
                        "case": PROBES[0],
                        "qualified": dict(zip(ARMS, accepted, strict=True)),
                        "overlap": overlap(api.rows),
                        "memory_before_index": memory,
                        "phase": run_id,
                    }
                    gate["parallel"] = (
                        all(accepted) and gate["overlap"] and not api.blocked and not drain_blocked
                    )
                    save_new(gate_path, gate)
                if not pilot_only and not api.blocked and not drain_blocked:
                    limit = 2 if gate["parallel"] else 1
                    slots = asyncio.Semaphore(limit)

                    async def guarded(case, arm):
                        async with slots:
                            if not api.blocked and not drain_blocked:
                                await visit(case, arm)

                    await asyncio.gather(
                        *(
                            guarded(case, arm)
                            for i, case in enumerate(tasks)
                            for arm in (ARMS if i % 2 == 0 else ARMS[::-1])
                        )
                    )
            finally:
                save_new(
                    root / f"workspace-http-{run_id}.json",
                    {
                        "at": utc_now_iso(),
                        "peak": api.peak,
                        "rows": api.rows,
                        "capacity": api.transitions,
                        "blocked": api.blocked,
                    },
                )
        if contract(root, suite, corpus, file_hash(index.cache_path), tasks, list(ARMS)) != plan:
            raise ValueError("implementation or evidence changed during the run")
    return {
        "name": name,
        "pilot": gate,
        "blocked": api.blocked or ("NATIVE_MODEL_AUDIT_UNSEALED" if drain_blocked else None),
        "http": dict(Counter(str(r["status"]) for r in api.rows)),
    }


def evidence_losses(row, task, corpus_by_id):
    shown = defaultdict(list)
    for receipt in row.get("evidence_deliveries", []):
        for key, spans in receipt["spans"].items():
            shown[key].extend(spans)
    answer = {p["doc_id"]: p for p in row["assessment"]["papers"] if not p["duplicate"]}
    losses = []
    for target in first_loss_by_target(row, task):
        key = target["doc_id"]
        source = corpus_by_id[key]
        revealed = [source["abstract"][a:b] for a, b in shown[key]]
        rationale_sets = task["expect"]["evidence"][key]
        complete = any(
            all(any(source["abstract_sentences"][i] in t for t in revealed) for i in r["sentences"])
            for r in rationale_sets
        )
        paper = answer.get(key) or {}
        failure = (
            "technical"
            if not row["assessment"]["valid_completion"]
            else "format"
            if not row["assessment"]["format_valid"]
            else "covered"
            if paper.get("stance_correct") and paper.get("exact_quote")
            else "wrong_direction"
            if paper.get("stance_correct") is False
            else "non_verbatim"
            if paper
            else "seen_not_selected"
            if shown[key]
            else "submitted_not_read"
            if target["observed"]["source_submission"]
            else "retrieved_not_submitted"
            if target["observed"]["retrieval"]
            else "not_observed"
        )
        losses.append(
            {
                **target,
                "any_abstract_delivered": bool(shown[key]),
                "complete_rationale_delivered": complete,
                "answer_quote_delivered": bool(
                    paper and any(paper["quote"] in t for t in revealed)
                ),
                "failure": failure,
            }
        )
    return losses


def score(name):
    root, suite, corpus = prepare_run(name)
    by_id = {r["doc_id"]: r for r in corpus["records"]}
    systems = (*ARMS, "kimi_code")
    per = defaultdict(list)
    missing, seen_sessions = [], set()
    for task in suite["tasks"]:
        case = task["case_id"]
        for arm in systems:
            path = root / ("kimi" if arm == "kimi_code" else arm) / f"{case}.json"
            if not path.is_file():
                missing.append([arm, case])
                continue
            record = read(path) | {"run_case_id": case}
            if arm == "kimi_code":
                sid = record.get("session_id")
                if sid is not None and sid in seen_sessions:
                    raise ValueError("Kimi session reused")
                seen_sessions.add(sid)
                record["session_isolation_valid"] = bool(sid)
                record["project_skill_isolation_valid"] = project_skill_isolation_valid(record)
            row = score_record(record, task, corpus)
            if arm in ARMS:
                row["target_readback"] = evidence_losses(row, task, by_id)
            per[arm].append(row)
    result = {"systems": {a: aggregate(per[a]) for a in systems}, "missing": missing}
    if not missing:
        result["comparisons"] = {
            f"{left}_vs_{right}": paired_summary(
                [{**r, "system": "product"} for r in per[left]]
                + [{**r, "system": "kimi_code"} for r in per[right]]
            )
            for left, right in (
                ("workspace", "kimi_code"),
                ("default", "kimi_code"),
                ("workspace", "default"),
            )
        }
    save_new(
        root / f"workspace-assessment-{uuid4().hex[:10]}.json", result | {"records": dict(per)}
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pilot", "run", "score"))
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    result = (
        score(args.name)
        if args.command == "score"
        else asyncio.run(run(args.name, pilot_only=args.command == "pilot"))
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "comparisons"}, ensure_ascii=False, indent=2
        ),
        flush=True,
    )
    return 2 if result.get("blocked") or result.get("missing") else 0


if __name__ == "__main__":
    raise SystemExit(main())
