from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from filelock import FileLock
from tools.gap_benchmark import opencode_parallel as parallel

from citation_tool.common_scoring import CommonScorer
from citation_tool.semantic import EmbeddingIndex


def test_shared_embedding_keeps_e5_for_common_scoring_and_serializes_search(tmp_path, monkeypatch):
    entered = threading.Event()
    dispatched = threading.Event()
    claim = object()

    def original(self, observed_claim, *, query_text):
        assert self is index and observed_claim is claim and query_text == "facet"
        entered.set()
        return {"work:1": 0.9}

    monkeypatch.setattr(EmbeddingIndex, "similarities", original)
    index = parallel.SharedEmbeddingIndex([], model_name="test", cache_dir=tmp_path)
    scorer = CommonScorer({"a": SimpleNamespace(_index=index, simulation=False)})
    assert scorer._dense is index and scorer.version.endswith(":e5")

    def search():
        dispatched.set()
        return index.similarities(claim, query_text="facet")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with index._inference_lock:
            pending = pool.submit(search)
            assert dispatched.wait(1)
            assert not entered.wait(0.05)
        assert pending.result(timeout=1) == {"work:1": 0.9}


async def test_remote_lanes_overlap_without_cancelling_other_lane():
    entered = []
    both_started = asyncio.Event()

    async def attempt(model):
        entered.append(model)
        if len(entered) == 2:
            both_started.set()
        await both_started.wait()
        if model == "a":
            raise ValueError("test transport failure")
        return {"model": model, "status": "done"}

    async with asyncio.timeout(1):
        records = await parallel.run_models(("a", "b"), 2, attempt)
    assert records == [
        {"model": "a", "status": "not_sealed", "error_type": "ValueError"},
        {"model": "b", "status": "done"},
    ]


async def test_serial_mode_does_not_dispatch_second_early():
    sequence = []

    async def attempt(model):
        sequence.append(("start", model))
        await asyncio.sleep(0)
        sequence.append(("end", model))
        return {"model": model}

    await parallel.run_models(("a", "b"), 1, attempt)
    assert sequence == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


async def test_commit_pressure_rejects_before_allocating_index(monkeypatch):
    monkeypatch.setattr(
        parallel,
        "memory_status",
        lambda: {"free_commit_bytes": parallel.GIB, "free_physical_bytes": 8 * parallel.GIB},
    )
    monkeypatch.setattr(parallel, "_pilot", lambda *a, **k: pytest.fail("started under pressure"))
    with pytest.raises(parallel.PilotBlocked, match="commit=1.00 GiB"):
        await parallel.pilot("memory-test", "sf-1086", 2, resume=False)


async def test_lock_applies_between_new_pilot_names(tmp_path, monkeypatch):
    monkeypatch.setattr(parallel, "RUNTIME", tmp_path)
    monkeypatch.setattr(parallel, "require_memory", lambda _: {})
    monkeypatch.setattr(
        parallel, "_pilot", lambda *a, **k: pytest.fail("started despite local lock")
    )
    with FileLock(tmp_path / "opencode-parallel" / ".shared-e5.lock"):
        with pytest.raises(parallel.PilotBlocked, match="holds the lock"):
            await parallel.pilot("different-pilot", "sf-1086", 2, resume=False)


def test_interval_overlap_is_not_inferred_across_resumed_executions():
    base = {"execution_id": "a", "started_s": 2, "ended_s": 12}
    simultaneous = {**base, "started_s": 4, "ended_s": 20}
    assert parallel.observed_overlap([base, simultaneous]) == 8
    assert parallel.observed_overlap([base, {**simultaneous, "execution_id": "b"}]) is None
    assert parallel.observed_overlap([base, {**simultaneous, "started_s": 13}]) == 0


def test_transport_overlap_does_not_count_nested_or_resumed_intervals_twice():
    first = {"execution_id": "a", "http_intervals_s": [[1, 5], [3, 8], [10, 12]]}
    second = {"execution_id": "a", "http_intervals_s": [[4, 6], [5, 10], [10.5, 11]]}
    assert parallel.observed_transport_overlap([first, second]) == 4.5
    assert parallel.observed_transport_overlap([first, {**second, "execution_id": "b"}]) is None
    assert parallel.observed_transport_overlap([first, {**second, "http_intervals_s": []}]) == 0


async def test_http_hooks_record_successful_request_intervals_only():
    def reply(request):
        return httpx.Response(200 if request.url.path == "/ok" else 503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        intervals = []
        parallel.attach_transport_timing(
            SimpleNamespace(client=client), origin=time.monotonic(), intervals=intervals
        )
        await client.get("https://model.invalid/ok")
        await client.get("https://model.invalid/failed")
        assert len(intervals) == 1
        start, end = intervals[0]
        assert 0 <= start <= end


def test_common_score_requires_actual_finite_dense_features():
    item = {
        "score_status": "scoped",
        "score_version": "constant:e5",
        "common_features": {"dense_cosine": 0.0},
    }
    assert parallel.full_dense_common_score([{"candidate_diagnostics": [item]}])
    assert not parallel.full_dense_common_score([])
    for bad in (
        {**item, "score_version": "constant:lexical"},
        {**item, "common_features": {"dense_cosine": None}},
        {**item, "common_features": {"dense_cosine": float("nan")}},
        {**item, "score_status": "failed"},
    ):
        assert not parallel.full_dense_common_score([{"candidate_diagnostics": [item, bad]}])


def test_expansion_gate_uses_verified_paths_not_science_scores():
    memory = {"free_physical_bytes": 6 * parallel.GIB, "free_commit_bytes": 3 * parallel.GIB}
    model_report = {
        "full_dense_path": True,
        "full_dense_common_score": True,
        "common_score_failures": 0,
        "valid": True,
        "format_valid": True,
        "strict_coverage": 0.0,
        "ranking": {"all_completed": True, "queries_with_completed": 1, "query_count": 1},
        "memory_on_admission": memory,
        "memory_on_exit": memory,
    }
    reports = {model: dict(model_report) for model in parallel.MODELS}
    assert parallel.expansion_gate(reports, 12.0, 5.0) == {"passed": True, "blockers": []}
    assert parallel.expansion_gate(reports, 12.0, 0)["blockers"] == [
        {"code": "NO_OBSERVED_TRANSPORT_OVERLAP"}
    ]
    model = next(iter(reports))
    reports[model].update(full_dense_common_score=False, format_valid=False, memory_on_exit=None)
    gate = parallel.expansion_gate(reports, None, None)
    assert not gate["passed"]
    assert gate["blockers"] == [
        {"code": "NO_OBSERVED_CASE_OVERLAP"},
        {"code": "NO_OBSERVED_TRANSPORT_OVERLAP"},
        {"model": model, "code": "DENSE_COMMON_SCORE_UNVERIFIED"},
        {"model": model, "code": "TERMINAL_ANSWER_INVALID"},
        {"model": model, "code": "MEMORY_HEADROOM_UNVERIFIED"},
    ]


async def test_dual_pilot_uses_one_index_separate_roots_and_retains_records(tmp_path, monkeypatch):
    task_id = "sf-fixture"
    task = {
        "case_id": task_id,
        "retriever": "hybrid",
        "expect": {"evidence": {}},
        "slice": "unlabeled",
        "family": "fixture",
    }
    model_index = object()
    embedding_loads, invocations = [], []
    ready = asyncio.Event()
    monkeypatch.setattr(parallel, "RUNTIME", tmp_path)
    monkeypatch.setattr(
        parallel,
        "memory_status",
        lambda: {"free_commit_bytes": 8 * parallel.GIB, "free_physical_bytes": 8 * parallel.GIB},
    )
    monkeypatch.setattr(
        parallel, "load_data", lambda: ({"tasks": [task]}, {"records": [], "corpus_sha256": "test"})
    )
    monkeypatch.setattr(parallel, "artifact_hash", lambda: "fixture")
    monkeypatch.setattr(parallel.opencode_native, "configured_route", lambda model: model)

    def prepare_index(root, observed, corpus, *, index_type):
        assert index_type is parallel.SharedEmbeddingIndex and "expect" not in observed
        embedding_loads.append(root)
        return model_index, "index-sha"

    monkeypatch.setattr(parallel.opencode_parent_pair, "ready_index", prepare_index)

    async def one(observed, original, *, root, corpus, index, fingerprint, route, **kwargs):
        assert index is model_index and "expect" not in observed
        assert not kwargs["source_evidence_first"] and root.name == route
        invocations.append(root)
        if len(invocations) == 2:
            ready.set()
        await ready.wait()
        root.mkdir(parents=True)
        (root / "trace.json").write_text("[]", encoding="utf-8")
        return {
            "schema": parallel.shared.SCHEMA,
            "system": "product",
            "case_id": task_id,
            "expected_model": route,
            "implementation_hash": fingerprint,
            "completed": True,
            "model_valid": True,
            "response_text": '{"papers": [], "conclusion": "No sources."}',
            "wall_time_ms": 1,
            "queries": [],
            "model_calls": [],
            "ranking_metrics": {},
            "parent_trace_path": "trace.json",
        }

    monkeypatch.setattr(parallel.opencode_source_first, "one", one)
    async with asyncio.timeout(5):
        report = await parallel.pilot("independent", task_id, 2, resume=False)
    assert len(embedding_loads) == 1 and len(set(invocations)) == 2
    assert report["status"] == "sealed" and report["overlap_seconds"] is not None
    assert report["plan"]["case_concurrency"] == 2
    assert {m for m, result in report["models"].items() if result["format_valid"]} == set(
        parallel.MODELS
    )
    records = [root / "product" / f"{task_id}.json" for root in invocations]
    previous = [p.read_bytes() for p in records]
    assert await parallel.pilot("independent", task_id, 2, resume=True) == report
    assert [p.read_bytes() for p in records] == previous
    assert len(invocations) == 2
