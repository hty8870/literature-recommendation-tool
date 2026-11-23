"""Run one parent-tool change through the real SciFact product on a single E5 index."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from functools import partial
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout
from tools import luna_product_retest as shared
from tools.benchmark.core import file_hash
from tools.gap_benchmark import opencode_native, opencode_product, opencode_source_first
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import aggregate, score_record
from tools.gap_benchmark.opencode_assessment import _read
from tools.gap_benchmark.opencode_baseline import MODELS, freeze
from tools.quality_benchmark.core import artifact_hash

from citation_tool.agent_observations import ParentEvidenceHistory
from citation_tool.entities import SearchCreate, utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.search import _build_claim
from citation_tool.semantic import EmbeddingIndex

CONTRASTS = {
    "delta": {
        "control": {"source_evidence_first": True, "evidence_delta": False},
        "trial": {"source_evidence_first": True, "evidence_delta": True},
    },
    "source-ready": {
        "control": {"source_evidence_first": False, "evidence_delta": False},
        "trial": {"source_evidence_first": True, "evidence_delta": False},
    },
    "abstract-references": {
        "control": {"source_evidence_first": True, "abstract_references": False},
        "trial": {"source_evidence_first": True, "abstract_references": True},
    },
    "parent-budget": {
        "control": {"source_evidence_first": True, "parent_max_tokens": 8192},
        "trial": {"source_evidence_first": True, "parent_max_tokens": 16384},
    },
}
DEVELOPMENT_IDS = (
    "sf-1086",
    "sf-873",
    "sf-1029",
    "sf-179",
    "sf-589",
    "sf-1137",
    "sf-1187",
    "sf-452",
)


def restore_receipt_abstracts(receipt: dict, known: dict[tuple[str, str], str]) -> tuple[dict, int]:
    candidates = []
    references = 0

    def resolve(row: dict, field: str, work: str) -> dict:
        nonlocal references
        pointer = row.get("abstract_ref")
        if not isinstance(pointer, dict):
            return row
        if pointer.get("work_id") != work or pointer.get("field") != "evidence_excerpt":
            raise ValueError("abstract pointer does not preserve the evidence identity")
        identifier = (pointer["query_id"], work)
        if identifier not in known:
            raise ValueError("abstract pointer has no visible original")
        if field in row:
            raise ValueError("reference and original both supplied")
        references += 1
        return {k: v for k, v in row.items() if k != "abstract_ref"} | {field: known[identifier]}

    for candidate in receipt["candidate_diagnostics"]:
        work = candidate["work_id"]
        if isinstance(text := candidate.get("evidence_excerpt"), str):
            known[receipt["query_id"], work] = text
        entry = resolve(candidate, "evidence_excerpt", work)
        if "source_records" in entry:
            entry = {
                **entry,
                "source_records": [resolve(s, "abstract", work) for s in entry["source_records"]],
            }
        candidates.append(entry)
    return {**receipt, "candidate_diagnostics": candidates}, references


def receipt_metrics(trace: list[dict]) -> dict:
    history = ParentEvidenceHistory()
    viewed, works = set(), set()
    observed = Counter()
    abstracts: dict[tuple[str, str], str] = {}
    for turn in trace:
        for tool in turn["tool_results"]:
            row = tool["content"]
            if (
                tool["name"] != "search_literature"
                or not isinstance(row, dict)
                or "query_id" not in row
                or row["query_id"] in viewed
            ):
                continue
            viewed.add(row["query_id"])
            works.update(c["work_id"] for c in row["candidate_diagnostics"])
            observed["early_receipts"] += row.get("delivery_stage") == "sources_ready"
            if row.get("evidence_delta"):
                observed["observed_delta_receipts"] += 1
            restored, references = restore_receipt_abstracts(row, abstracts)
            observed["abstract_reference_count"] += references
            delta = history.observe(restored)
            if delta is not None:
                observed[delta["status"]] += 1
    return {
        "distinct_parent_receipts": len(viewed),
        "distinct_works": len(works),
        "repeated_without_new_evidence": observed["no_unseen_evidence"],
        "empty_followups": observed["no_candidates_returned"],
        "early_receipts": observed["early_receipts"],
        "observed_delta_receipts": observed["observed_delta_receipts"],
        "abstract_reference_count": observed["abstract_reference_count"],
    }


def target_exposure(trace: list[dict], expected_ids: set[str], abstracts: dict[str, str]) -> dict:
    expected = {f"work:scifact:{doc_id}": doc_id for doc_id in expected_ids}
    first: dict[str, dict] = {}
    for ordinal, turn in enumerate(trace, 1):
        visible_originals: dict[tuple[str, str], str] = {}
        for tool in turn["tool_results"]:
            receipt = tool["content"]
            if (
                tool["name"] != "search_literature"
                or not isinstance(receipt, dict)
                or receipt.get("simulation") is not False
                or "query_id" not in receipt
                or "candidate_diagnostics" not in receipt
            ):
                continue
            restored, _ = restore_receipt_abstracts(receipt, visible_originals)
            for candidate in restored["candidate_diagnostics"]:
                work = candidate["work_id"]
                if work not in expected or work in first:
                    continue
                abstract = abstracts[work]
                if not abstract or (
                    abstract != candidate.get("evidence_excerpt")
                    and all(
                        abstract != doc.get("abstract")
                        for doc in candidate.get("source_records", [])
                    )
                ):
                    continue
                first[work] = {
                    "doc_id": expected[work],
                    "first_observed_request": ordinal,
                    "query_id": receipt["query_id"],
                    "sources_ready": receipt.get("delivery_stage") == "sources_ready",
                }
    return {
        "labeled_targets": len(expected),
        "exact_abstracts_observed": len(first),
        "first_seen_in_sources_ready": sum(r["sources_ready"] for r in first.values()),
        "targets": [first[work] for work in sorted(first)],
    }


def attach_retrieval_audit(adapter, events: list[dict]) -> None:
    search = adapter.search

    def observed(claim):
        context = {"query_id": claim.query_id, "recall_hash": claim.claim_hash}
        try:
            items = search(claim)
        except Exception as exc:
            events.append({**context, "dense_status": "search_failed", "error": type(exc).__name__})
            raise
        events.append(
            {
                **context,
                "dense_status": adapter.model_versions.get("dense_status", "not_reported"),
                "facet_count": adapter.model_versions.get("facet_count"),
                "returned": len(items),
            }
        )
        return items

    adapter.search = observed


def ready_index(root, task, corpus, *, index_type: type[EmbeddingIndex] = EmbeddingIndex):
    settings = shared.product_settings(root, task)
    index = shared.require_prebuilt_index(corpus, settings, index_type=index_type)
    original_cache = (index.cache_path, file_hash(index.cache_path))
    adapter = shared.build_product_adapter(settings, corpus, index, task)
    request = SearchCreate(**task["request"])
    claim, _ = _build_claim("benchmark-preflight", "q_preflight", request, utc_now_iso(), None)
    adapter.search(claim)
    if adapter.model_versions.get("dense_status") != "available":
        raise ValueError("actual E5 preflight failed; no model cases started")
    if (index.cache_path, file_hash(index.cache_path)) != original_cache:
        raise ValueError("preflight changed the frozen embedding cache")
    return index, original_cache[1]


def arm_summary(row: dict) -> dict:
    failed_scores = sum(
        item.get("score_status") == "failed"
        for query in row["queries"]
        for item in query["candidate_diagnostics"]
    )
    audit = row["retrieval_audit"]
    observed = sum(call["dense_status"] == "available" for call in audit)
    assessed = row["assessment"]
    return {
        "valid": assessed["valid_completion"],
        "format_valid": assessed["format_valid"],
        "strict_coverage": assessed["correct_direction_evidence_coverage"],
        "complete_rationale": assessed["complete_rationale_coverage"],
        "invalid_quotes": assessed["invalid_quotes"],
        "wrong_directions": assessed["wrong_official_stances"],
        "parent_coverage_at_delivery": row.get("parent_coverage_at_delivery"),
        "parent_seconds": round(row["wall_time_ms"] / 1000, 3),
        "terminal_error": (row.get("error") or {}).get("code"),
        "final_parent_finish": next(
            (
                call.get("finish")
                for call in reversed(row["model_calls"])
                if call["stage"] == "parent"
            ),
            None,
        ),
        "dense_calls": observed,
        "source_calls": len(audit),
        "common_score_failures": failed_scores,
        "ranking": row["ranking_metrics"],
        "progress": row["parent_receipt_metrics"],
        "evidence_exposure": row.get("evidence_exposure"),
    }


async def run(
    name: str,
    model: str,
    contrast: str,
    cases: tuple[str, ...],
    *,
    resume: bool,
    concurrency: int = 1,
    pilot_name: str | None = None,
) -> dict:
    if concurrency == 1 and pilot_name is None:
        return await _run_pairs(name, model, contrast, cases, resume=resume)
    if concurrency != 2 or not pilot_name:
        raise ValueError("parallel pairs require two lanes and a verified pilot name")
    from tools.gap_benchmark import opencode_parallel as parallel

    lock = RUNTIME / "opencode-parallel" / ".shared-e5.lock"
    with FileLock(lock, timeout=0):
        proof = parallel.require_expansion_pilot(pilot_name)
        parallel.require_memory(parallel.START_RESERVE)
        return await _run_pairs(
            name,
            model,
            contrast,
            cases,
            resume=resume,
            index_type=parallel.SharedEmbeddingIndex,
            pilot_proof=proof,
            admit=partial(parallel.require_memory, parallel.CASE_RESERVE),
        )


async def _run_pairs(
    name: str,
    model: str,
    contrast: str,
    cases: tuple[str, ...],
    *,
    resume: bool,
    index_type: type[EmbeddingIndex] = EmbeddingIndex,
    pilot_proof: dict | None = None,
    admit: Callable[[], object] | None = None,
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", name):
        raise ValueError("invalid run name")
    if not cases or len(cases) != len(set(cases)) or set(cases) - set(DEVELOPMENT_IDS):
        raise ValueError("use distinct previously inspected cases")
    if contrast == "parent-budget" and model != "qwen3.8-flash":
        raise ValueError("the parent budget contrast was registered for Qwen")
    suite, corpus = load_data()
    abstracts = {record["work_id"]: record["abstract"] for record in corpus["records"]}
    originals = {t["case_id"]: t for t in suite["tasks"] if t["case_id"] in cases}
    if len({t["family"] for t in originals.values()}) != len(cases):
        raise ValueError("selected cases must represent distinct development families")
    root = RUNTIME / "opencode-parent-pairs" / name / model
    if pilot_proof and pilot_proof["corpus_sha256"] != corpus["corpus_sha256"]:
        raise ValueError("parallel pilot used a different corpus")
    index, cache_hash = await asyncio.to_thread(
        ready_index,
        root,
        originals[cases[0]],
        corpus,
        index_type=index_type,
    )
    if pilot_proof and pilot_proof["e5_cache_sha256"] != cache_hash:
        raise ValueError("parallel pilot used a different E5 cache")
    if admit is not None:
        admit()
    concurrency = 2 if pilot_proof else 1
    request_slots = asyncio.Semaphore(2) if pilot_proof else None
    route = await asyncio.to_thread(opencode_native.configured_route, model)
    arms = CONTRASTS[contrast]
    order = [
        [case, arm]
        for case in cases
        for arm in sorted(arms, key=lambda a: hashlib.sha256(f"{case}:{a}".encode()).digest())
    ]
    code_paths = [
        Path(__file__),
        Path(opencode_product.__file__),
        Path(opencode_source_first.__file__),
        Path(opencode_native.__file__),
        Path(shared.__file__),
        Path(__file__).with_name("metrics.py"),
    ]
    code_paths.append(Path(__file__).with_name("opencode_parallel.py"))
    from tools.gap_benchmark.opencode_parallel import (
        attach_transport_timing,
        observed_overlap,
        observed_transport_overlap,
    )

    plan = {
        "schema": "scifact-opencode-parent-single-variable-v2",
        "model": model,
        "case_concurrency": concurrency,
        "model_request_lanes": 2,
        "pilot_proof": pilot_proof,
        "provider": route.provider,
        "protocol": route.protocol,
        "arms": arms,
        "order": order,
        "scope": "known development cases; live product, not an independent holdout",
        "engineering_boundary": "keep delivery status, background ranking and model audit separate",
        "corpus_sha256": corpus["corpus_sha256"],
        "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "e5_cache_sha256": cache_hash,
        "terminal_time_limit_seconds": shared.AGENT_TIMEOUT_MS / 1000,
        "output_limits": opencode_native.TOKEN_LIMITS,
        "product_sha256": artifact_hash(),
        "runner_sha256": {p.name: file_hash(p) for p in code_paths},
    }
    freeze(root / "plan.json", plan)
    fingerprint = sha256_json(plan)
    execution, origin = uuid4().hex, time.monotonic()
    case_slots, halted = asyncio.Semaphore(concurrency), asyncio.Event()

    async def evaluate_arm(case: str, arm: str) -> dict | None:
        path = root / arm / "product" / f"{case}.json"
        row = shared.load_reusable_record(path, resume=resume, implementation_hash=fingerprint)
        if row is None:
            async with case_slots:
                if halted.is_set():
                    return None
                if admit is not None:
                    admit()
                original = originals[case]
                audit: list[dict] = []
                http_intervals: list[list[float]] = []
                started = time.monotonic() - origin
                row = await opencode_source_first.one(
                    {k: v for k, v in original.items() if k != "expect"},
                    original,
                    root=root / arm,
                    corpus=corpus,
                    index=index,
                    fingerprint=fingerprint,
                    route=route,
                    request_slots=request_slots,
                    observe_adapter=partial(attach_retrieval_audit, events=audit),
                    observe_gateway=partial(
                        attach_transport_timing, origin=origin, intervals=http_intervals
                    ),
                    **arms[arm],
                )
                trace = _read(root / arm / row["parent_trace_path"])
                row.update(
                    arm=arm,
                    execution_id=execution,
                    started_s=started,
                    ended_s=time.monotonic() - origin,
                    http_intervals_s=http_intervals,
                    retrieval_audit=audit,
                    parent_receipt_metrics=receipt_metrics(trace),
                    evidence_exposure=target_exposure(
                        trace, set(original["expect"]["evidence"]), abstracts
                    ),
                )
                save_new(path, row)
                if any(c.get("status_code") in {401, 403, 429} for c in row["model_calls"]):
                    halted.set()
        if (row["case_id"], row["arm"], row["expected_model"]) != (case, arm, model):
            raise ValueError("record identity differs from registered case and arm")
        scored = score_record(row, originals[case], corpus)
        print(json.dumps({"case": case, "arm": arm, **arm_summary(scored)}), flush=True)
        return scored

    outcomes = (
        await asyncio.gather(*(evaluate_arm(*key) for key in order), return_exceptions=True)
        if concurrency == 2
        else [await evaluate_arm(*key) for key in order]
    )
    if artifact_hash() != plan["product_sha256"] or any(
        file_hash(p) != plan["runner_sha256"][p.name] for p in code_paths
    ):
        raise ValueError("working implementation changed during the paired run")
    not_sealed = [
        {
            "case": case,
            "arm": arm,
            "error": type(row).__name__ if row is not None else "admission_stopped",
        }
        for (case, arm), row in zip(order, outcomes, strict=True)
        if row is None or isinstance(row, BaseException)
    ]
    if not_sealed:
        return {"status": "partial", "plan": plan, "not_sealed": not_sealed}
    grouped = dict(zip((tuple(key) for key in order), outcomes, strict=True))
    result = {
        "status": "sealed",
        "plan": plan,
        "systems": {arm: aggregate([grouped[case, arm] for case in cases]) for arm in arms},
        "pairs": [
            {
                "case": case,
                "overlap_seconds": observed_overlap([grouped[case, arm] for arm in arms]),
                "transport_overlap_seconds": observed_transport_overlap(
                    [grouped[case, arm] for arm in arms]
                ),
                **{arm: arm_summary(grouped[case, arm]) for arm in arms},
            }
            for case in cases
        ],
    }
    freeze(root / "assessment.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--contrast", choices=CONTRASTS, required=True)
    parser.add_argument("--cases", nargs="+", choices=DEVELOPMENT_IDS, required=True)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument("--pilot", help="verified shared-index pilot name for two-lane pairs")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    from tools.gap_benchmark.opencode_parallel import PilotBlocked

    try:
        result = asyncio.run(
            run(
                args.name,
                args.model,
                args.contrast,
                tuple(args.cases),
                resume=args.resume,
                concurrency=args.concurrency,
                pilot_name=args.pilot,
            )
        )
    except (PilotBlocked, Timeout) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "sealed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
