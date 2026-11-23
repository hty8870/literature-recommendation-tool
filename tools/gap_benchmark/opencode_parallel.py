"""Pilot one public SciFact case per model on a single E5 instance."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import re
import sys
import time
from functools import partial
from pathlib import Path
from uuid import uuid4

import httpx
from filelock import FileLock, Timeout
from tools import luna_product_retest as shared
from tools.benchmark.core import file_hash
from tools.gap_benchmark import opencode_native, opencode_parent_pair, opencode_source_first
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import score_record
from tools.gap_benchmark.opencode_baseline import MODELS, freeze
from tools.quality_benchmark.core import artifact_hash

from citation_tool.entities import ClaimContext
from citation_tool.jcs import sha256_json
from citation_tool.semantic import EmbeddingIndex

GIB = 1 << 30
START_RESERVE = 4 * GIB
CASE_RESERVE = 2 * GIB


class SharedEmbeddingIndex(EmbeddingIndex):
    def similarities(
        self, claim: ClaimContext, *, query_text: str | None = None
    ) -> dict[str, float]:
        with self._inference_lock:
            return super().similarities(claim, query_text=query_text)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("total_physical", ctypes.c_uint64),
        ("free_physical", ctypes.c_uint64),
        ("total_commit", ctypes.c_uint64),
        ("free_commit", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("free_virtual", ctypes.c_uint64),
        ("reserved", ctypes.c_uint64),
    ]


class PilotBlocked(RuntimeError):
    pass


def memory_status() -> dict[str, int]:
    if sys.platform != "win32":
        raise PilotBlocked("Windows commit information is required for this local pilot")
    status = _MemoryStatus(length=ctypes.sizeof(_MemoryStatus))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise PilotBlocked(f"memory preflight failed: Windows error {ctypes.get_last_error()}")
    return {
        "free_commit_bytes": int(status.free_commit),
        "free_physical_bytes": int(status.free_physical),
    }


def preflight(reserve: int = START_RESERVE) -> dict:
    memory = memory_status()
    return {
        **memory,
        "reserve_bytes": reserve,
        "admitted": min(memory.values()) >= reserve,
    }


def require_memory(reserve: int) -> dict:
    reading = preflight(reserve)
    if not reading["admitted"]:
        raise PilotBlocked(
            f"memory headroom below {reserve // GIB} GiB: "
            f"commit={reading['free_commit_bytes'] / GIB:.2f} GiB, "
            f"physical={reading['free_physical_bytes'] / GIB:.2f} GiB"
        )
    return reading


async def run_models(models, concurrency, launch):
    async def guarded(model):
        try:
            return await launch(model)
        except Exception as exc:
            return {"model": model, "status": "not_sealed", "error_type": type(exc).__name__}

    if concurrency == 1:
        return [await guarded(model) for model in models]
    return await asyncio.gather(*(guarded(model) for model in models))


def observed_overlap(records: list[dict]) -> float | None:
    if len(records) != 2 or records[0]["execution_id"] != records[1]["execution_id"]:
        return None
    start = max(row["started_s"] for row in records)
    end = min(row["ended_s"] for row in records)
    return round(max(0.0, end - start), 3)


def attach_transport_timing(
    gateway: opencode_native.OpenCodeGateway, *, origin: float, intervals: list[list[float]]
) -> None:
    key = "scifact_request_started_s"

    async def request_started(request: httpx.Request) -> None:
        request.extensions[key] = time.monotonic() - origin

    async def response_received(response: httpx.Response) -> None:
        started = response.request.extensions.get(key)
        if response.is_success and isinstance(started, float):
            intervals.append([started, time.monotonic() - origin])

    gateway.client.event_hooks["request"].append(request_started)
    gateway.client.event_hooks["response"].append(response_received)


def observed_transport_overlap(records: list[dict]) -> float | None:
    if len(records) != 2 or records[0]["execution_id"] != records[1]["execution_id"]:
        return None
    edges = []
    for lane, record in enumerate(records):
        for start, end in record["http_intervals_s"]:
            if not 0 <= start <= end or not math.isfinite(end):
                raise ValueError("invalid transport interval")
            edges.extend([(start, lane, 1), (end, lane, -1)])
    active = [0, 0]
    previous, overlap = 0.0, 0.0
    for instant, lane, delta in sorted(edges):
        if all(n > 0 for n in active):
            overlap += instant - previous
        active[lane] += delta
        previous = instant
    return round(overlap, 3)


def runner_paths() -> list[Path]:
    return [
        Path(__file__),
        Path(shared.__file__),
        Path(opencode_parent_pair.__file__),
        Path(opencode_source_first.__file__),
        Path(opencode_native.__file__),
        Path(__file__).with_name("opencode_product.py"),
        Path(__file__).with_name("metrics.py"),
    ]


def require_expansion_pilot(name: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", name):
        raise ValueError("invalid pilot name")
    root = RUNTIME / "opencode-parallel"
    path = (root / name / "assessment.json").resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise PilotBlocked("a verified parallel pilot is required before expansion")
    data = path.read_bytes()
    report = json.loads(data)
    plan = report["plan"]
    if (
        report.get("status") != "sealed"
        or plan.get("schema") != "scifact-opencode-shared-index-pilot-v1"
        or plan.get("case_concurrency") != 2
        or plan.get("models") != MODELS
        or plan.get("product_sha256") != artifact_hash()
        or plan.get("runner_sha256") != {p.name: file_hash(p) for p in runner_paths()}
    ):
        raise PilotBlocked("the parallel pilot does not match this implementation")
    gate = expansion_gate(
        report["models"], report["overlap_seconds"], report.get("transport_overlap_seconds")
    )
    if not gate["passed"] or report.get("expansion_gate") != gate:
        raise PilotBlocked("the parallel pilot did not pass the expansion gate")
    return {
        "name": name,
        "report_sha256": file_hash(path),
        "corpus_sha256": plan["corpus_sha256"],
        "e5_cache_sha256": plan["e5_cache_sha256"],
    }


def full_dense_common_score(queries: list[dict]) -> bool:
    candidates = [item for query in queries for item in query["candidate_diagnostics"]]
    return bool(candidates) and all(
        item.get("score_status") == "scoped"
        and (item.get("score_version") or "").endswith(":e5")
        and isinstance(cosine := (item.get("common_features") or {}).get("dense_cosine"), float)
        and math.isfinite(cosine)
        for item in candidates
    )


def expansion_gate(
    reports: dict[str, dict], overlap: float | None, transport_overlap: float | None
) -> dict:
    blockers = []
    if overlap is None or overlap <= 0:
        blockers.append({"code": "NO_OBSERVED_CASE_OVERLAP"})
    if transport_overlap is None or transport_overlap <= 0:
        blockers.append({"code": "NO_OBSERVED_TRANSPORT_OVERLAP"})
    for model in MODELS:
        report = reports.get(model)
        if report is None:
            blockers.append({"model": model, "code": "MODEL_LANE_MISSING"})
            continue
        ranking = report["ranking"]
        readings = (report["memory_on_admission"], report["memory_on_exit"])
        checks = {
            "DENSE_RETRIEVAL_UNVERIFIED": report["full_dense_path"],
            "DENSE_COMMON_SCORE_UNVERIFIED": report["full_dense_common_score"]
            and report["common_score_failures"] == 0,
            "RANKING_INCOMPLETE": ranking.get("all_completed", False)
            and ranking.get("queries_with_completed") == ranking.get("query_count"),
            "TERMINAL_ANSWER_INVALID": report["valid"] and report["format_valid"],
            "MEMORY_HEADROOM_UNVERIFIED": all(
                memory
                and min(memory.get(k, 0) for k in ("free_physical_bytes", "free_commit_bytes"))
                >= CASE_RESERVE
                for memory in readings
            ),
        }
        blockers.extend({"model": model, "code": key} for key, ok in checks.items() if not ok)
    return {"passed": not blockers, "blockers": blockers}


async def pilot(name: str, case_id: str, concurrency: int, *, resume: bool) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", name):
        raise ValueError("invalid pilot name")
    require_memory(START_RESERVE)
    root = RUNTIME / "opencode-parallel" / name
    lock = FileLock(root.parent / ".shared-e5.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise PilotBlocked("another shared-index pilot holds the lock") from None
    try:
        return await _pilot(root, case_id, concurrency, resume=resume)
    finally:
        lock.release()


async def _pilot(root: Path, case_id: str, concurrency: int, *, resume: bool) -> dict:
    suite, corpus = load_data()
    original = next(
        (t for t in suite["tasks"] if t["case_id"] == case_id and t["retriever"] == "hybrid"),
        None,
    )
    if original is None:
        raise ValueError("select a hybrid case from the existing development suite")
    task = {k: v for k, v in original.items() if k != "expect"}
    index, cache_hash = await asyncio.to_thread(
        opencode_parent_pair.ready_index, root, task, corpus, index_type=SharedEmbeddingIndex
    )
    code_paths = runner_paths()
    plan = {
        "schema": "scifact-opencode-shared-index-pilot-v1",
        "case": case_id,
        "models": MODELS,
        "case_concurrency": concurrency,
        "limits": opencode_native.TOKEN_LIMITS,
        "timeout_seconds": shared.AGENT_TIMEOUT_MS / 1000,
        "embedding_lanes": 1,
        "startup_reserve_bytes": START_RESERVE,
        "case_reserve_bytes": CASE_RESERVE,
        "product_sha256": artifact_hash(),
        "runner_sha256": {p.name: file_hash(p) for p in code_paths},
        "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "corpus_sha256": corpus["corpus_sha256"],
        "e5_cache_sha256": cache_hash,
        "scope": "development pilot; never merged into the frozen serial retest",
    }
    freeze(root / "plan.json", plan)
    fingerprint = sha256_json(plan)
    execution_id, origin = uuid4().hex, time.monotonic()

    async def launch(model: str) -> dict:
        folder = root / model
        path = folder / "product" / f"{case_id}.json"
        row = shared.load_reusable_record(path, resume=resume, implementation_hash=fingerprint)
        if row is not None:
            if row["expected_model"] != model or row["case_id"] != case_id:
                raise ValueError("stored record does not match the model and case")
            return row
        require_memory(CASE_RESERVE)
        route = await asyncio.to_thread(opencode_native.configured_route, model)
        memory = require_memory(CASE_RESERVE)
        started = time.monotonic() - origin
        audit: list[dict] = []
        http_intervals: list[list[float]] = []
        row = await opencode_source_first.one(
            task,
            original,
            root=folder,
            corpus=corpus,
            index=index,
            fingerprint=fingerprint,
            route=route,
            source_evidence_first=False,
            observe_adapter=partial(opencode_parent_pair.attach_retrieval_audit, events=audit),
            observe_gateway=partial(
                attach_transport_timing, origin=origin, intervals=http_intervals
            ),
        )
        ended = time.monotonic() - origin
        try:
            exit_memory = memory_status()
        except PilotBlocked:
            exit_memory = None
        row.update(
            execution_id=execution_id,
            started_s=started,
            ended_s=ended,
            http_intervals_s=http_intervals,
            memory_on_admission=memory,
            memory_on_exit=exit_memory,
            retrieval_audit=audit,
            parent_receipt_metrics=opencode_parent_pair.receipt_metrics(
                json.loads((folder / row["parent_trace_path"]).read_text(encoding="utf-8"))
            ),
        )
        save_new(path, row)
        print(
            json.dumps(
                {"sealed": model, "case_id": case_id, "duration_s": row["ended_s"] - started}
            ),
            flush=True,
        )
        return row

    results = await run_models(MODELS, concurrency, launch)
    if artifact_hash() != plan["product_sha256"] or any(
        file_hash(p) != plan["runner_sha256"][p.name] for p in code_paths
    ):
        raise ValueError("the implementation changed during the pilot; records retained")
    if failures := [r for r in results if r.get("status") == "not_sealed"]:
        return {"status": "partial", "not_sealed": failures}
    overlap = observed_overlap(results)
    transport_overlap = observed_transport_overlap(results)
    reports = {}
    for row in results:
        audit = row["retrieval_audit"]
        row_summary = opencode_parent_pair.arm_summary(score_record(row, original, corpus))
        reports[row["expected_model"]] = {
            **row_summary,
            "full_dense_path": bool(audit) and all(r["dense_status"] == "available" for r in audit),
            "full_dense_common_score": full_dense_common_score(row["queries"]),
            "memory_on_admission": row["memory_on_admission"],
            "memory_on_exit": row["memory_on_exit"],
        }
    summary = {
        "status": "sealed",
        "plan": plan,
        "overlap_seconds": overlap,
        "transport_overlap_seconds": transport_overlap,
        "same_invocation_wall_s": (
            round(max(r["ended_s"] for r in results) - min(r["started_s"] for r in results), 3)
            if overlap is not None
            else None
        ),
        "models": reports,
        "expansion_gate": expansion_gate(reports, overlap, transport_overlap),
    }
    freeze(root / "assessment.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="read memory only; no E5 or model calls"
    )
    parser.add_argument("--name")
    parser.add_argument("--case", default="sf-1086")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            result = preflight()
            print(json.dumps(result))
            return 0 if result["admitted"] else 2
        if not args.name:
            parser.error("--name is required for a live pilot")
        result = asyncio.run(pilot(args.name, args.case, args.concurrency, resume=args.resume))
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "sealed" else 2
    except PilotBlocked as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
