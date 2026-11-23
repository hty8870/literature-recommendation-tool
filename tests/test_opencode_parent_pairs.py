import asyncio
import json
from types import SimpleNamespace

import pytest
from tools.gap_benchmark import opencode_parallel as parallel
from tools.gap_benchmark import opencode_parent_pair as pairs

MODEL = "qwen3.8-flash"
CASES = ("sf-1086",)


def test_exposure_requires_exact_observed_source_and_keeps_first_phase():
    def search(q, doc, excerpt, *, early=False, simulation=False, sources=None):
        return {
            "name": "search_literature",
            "content": {
                "query_id": q,
                "simulation": simulation,
                "delivery_stage": "sources_ready" if early else None,
                "candidate_diagnostics": [
                    {
                        "work_id": f"work:scifact:{doc}",
                        "evidence_excerpt": excerpt,
                        "source_records": sources or [],
                    }
                ],
            },
        }

    one = "Original first abstract."
    two = "Original second abstract."
    trace = [
        {"tool_results": []},
        {"tool_results": [search("pending", "1", "Original first", early=True)]},
        {
            "tool_results": [
                search("finished", "1", one),
                search("before_rank", "2", "", early=True, sources=[{"abstract": two}]),
                search("fixture", "3", "Third abstract.", early=True, simulation=True),
            ]
        },
        {"tool_results": [search("later", "1", one, early=True)]},
    ]
    corpus = {f"work:scifact:{n}": text for n, text in [(1, one), (2, two), (3, "Third abstract.")]}
    report = pairs.target_exposure(trace, {"1", "2", "3"}, corpus)
    assert report == {
        "labeled_targets": 3,
        "exact_abstracts_observed": 2,
        "first_seen_in_sources_ready": 1,
        "targets": [
            {
                "doc_id": "1",
                "first_observed_request": 3,
                "query_id": "finished",
                "sources_ready": False,
            },
            {
                "doc_id": "2",
                "first_observed_request": 3,
                "query_id": "before_rank",
                "sources_ready": True,
            },
        ],
    }


async def test_parallel_pairs_require_a_sealed_compatible_pilot_before_index(tmp_path, monkeypatch):
    monkeypatch.setattr(pairs, "RUNTIME", tmp_path)
    monkeypatch.setattr(parallel, "RUNTIME", tmp_path)
    monkeypatch.setattr(
        pairs, "ready_index", lambda *a, **k: pytest.fail("E5 loaded before admission")
    )
    with pytest.raises(ValueError, match="verified pilot name"):
        await pairs.run("denied", MODEL, "source-ready", CASES, resume=False, concurrency=2)
    with pytest.raises(parallel.PilotBlocked, match="verified parallel pilot"):
        await pairs.run(
            "denied",
            MODEL,
            "source-ready",
            CASES,
            resume=False,
            concurrency=2,
            pilot_name="missing",
        )

    path = tmp_path / "opencode-parallel" / "old" / "assessment.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "sealed", "plan": {}}), encoding="utf-8")
    with pytest.raises(parallel.PilotBlocked, match="does not match"):
        await pairs.run(
            "denied", MODEL, "source-ready", CASES, resume=False, concurrency=2, pilot_name="old"
        )


async def test_overlapping_pairs_share_one_e5_and_retain_failed_arm(tmp_path, monkeypatch):
    task = {
        "case_id": CASES[0],
        "family": "fixture",
        "slice": "unlabeled",
        "expect": {"evidence": {}},
    }
    identity = object()
    proof = {"name": "pilot", "report_sha256": "p", "corpus_sha256": "c", "e5_cache_sha256": "e5"}
    memory_checks, initialized, roots, slots = [], [], [], []
    started = asyncio.Event()
    monkeypatch.setattr(pairs, "RUNTIME", tmp_path)
    monkeypatch.setattr(parallel, "require_expansion_pilot", lambda name: proof)
    monkeypatch.setattr(parallel, "require_memory", memory_checks.append)
    monkeypatch.setattr(
        pairs, "load_data", lambda: ({"tasks": [task]}, {"corpus_sha256": "c", "records": []})
    )
    monkeypatch.setattr(pairs, "artifact_hash", lambda: "product")
    monkeypatch.setattr(
        pairs.opencode_native,
        "configured_route",
        lambda model: SimpleNamespace(provider="test", protocol="test"),
    )

    def ready(root, given, corpus, *, index_type):
        assert index_type is parallel.SharedEmbeddingIndex
        initialized.append(root)
        return identity, "e5"

    async def one(observed, original, *, root, index, fingerprint, request_slots, **kwargs):
        assert index is identity and "expect" not in observed
        roots.append(root)
        slots.append(request_slots)
        if len(roots) == 2:
            started.set()
        await started.wait()
        root.mkdir(parents=True, exist_ok=True)
        (root / "trace.json").write_text("[]", encoding="utf-8")
        ok = root.name == "trial"
        return {
            "system": "product",
            "schema": pairs.shared.SCHEMA,
            "case_id": CASES[0],
            "expected_model": MODEL,
            "implementation_hash": fingerprint,
            "completed": ok,
            "model_valid": ok,
            "response_text": '{"papers": [], "conclusion": "No evidence."}' if ok else "",
            "error": None if ok else {"code": "AGENT_TIMEOUT"},
            "wall_time_ms": 2,
            "queries": [],
            "model_calls": [],
            "ranking_metrics": {},
            "parent_trace_path": "trace.json",
        }

    monkeypatch.setattr(pairs, "ready_index", ready)
    monkeypatch.setattr(pairs.opencode_source_first, "one", one)
    kwargs = {"concurrency": 2, "pilot_name": "pilot"}
    async with asyncio.timeout(4):
        result = await pairs.run("test", MODEL, "source-ready", CASES, resume=False, **kwargs)
    assert len(initialized) == 1 and len(set(roots)) == 2 and slots[0] is slots[1]
    assert memory_checks == [
        parallel.START_RESERVE,
        parallel.CASE_RESERVE,
        parallel.CASE_RESERVE,
        parallel.CASE_RESERVE,
    ]
    assert result["status"] == "sealed"
    assert result["plan"]["pilot_proof"] == proof
    assert result["pairs"][0]["overlap_seconds"] >= 0
    assert result["pairs"][0]["control"]["terminal_error"] == "AGENT_TIMEOUT"
    frozen = {root: (root / "product" / f"{CASES[0]}.json").read_bytes() for root in roots}
    rerun = await pairs.run("test", MODEL, "source-ready", CASES, resume=True, **kwargs)
    assert rerun == result and len(roots) == 2
    assert all(
        (root / "product" / f"{CASES[0]}.json").read_bytes() == previous
        for root, previous in frozen.items()
    )
