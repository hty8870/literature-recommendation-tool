from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

from citation_tool.api import create_app
from citation_tool.candidates import merge_candidates, retrieval_fingerprint
from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import CandidateDiagnostic
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.publication import canonical_work_id, normalize_doi, publication_status
from citation_tool.relevance import (
    LEXICAL_JOINED_FALLBACK_VERSION,
    QueryRelevance,
    lexical_query_fallback,
    query_plan,
    semantic_query_facets,
)
from citation_tool.semantic import (
    EmbeddingIndex,
    SemanticMetadataAdapter,
    hybrid_candidate_window,
    reciprocal_rank_fusion,
)
from conftest import require_runtime_file
from test_agent import StubReasonGenerator
from test_integration import headers, wait_final
from test_units import make_claim


def record(doi="10.1007/example-1", **updates):
    value = {
        "work_id": f"work:{doi.upper()}",
        "paper_id": "paper:one",
        "title": "LFY1 regulates soybean floral organ identity",
        "abstract": (
            "Single and double lfy1 lfy2 mutants establish homeotic floral organ identity "
            "in soybean."
        ),
        "doi": doi,
        "year": 2025,
        "authors": "Example A, Example B",
        "pub_types": ["research-article"],
        "source_metadata_url": "https://europepmc.org/article/MED/example",
        "simulation": False,
    }
    value.update(updates)
    return value


def diagnostic(library="one", score=75.0):
    value = CandidateDiagnostic(
        result_id="res_same",
        work_id="work:10.1007/example-1",
        paper_id="paper:one",
        claim_id="clm_one",
        title="Example",
        doi="10.1007/example-1",
        library_ids=[library],
        publication_status="corrected",
        related_dois={
            "corrects": [],
            "corrected_by": ["10.1007/example-correction"],
            "retracted_by": [],
        },
        relationship="insufficient",
        scope_match="unknown",
        rejection_reasons=["source_trace_invalid"],
        evidence_excerpt="Same abstract",
        quality_note="测试候选，不是可引用全文证据。",
        candidate_score=score,
        score_version="test-score",
        score_scale_group="same-corpus",
        source_uri=f"https://example.org/{library}",
    )
    return value.model_copy(
        update={"retrieval_fingerprint": retrieval_fingerprint(value, "claim-hash")}
    )


def test_missing_alignment_does_not_replace_valid_score_with_zero_or_failure():
    scored = diagnostic("one", 100)
    failed = diagnostic("two", None).model_copy(update={"score_status": "failed"})
    merged = merge_candidates(scored, failed, "claim-hash")
    assert merged.candidate_score == 100
    assert merged.score_status == "scoped"
    assert merge_candidates(failed, failed, "claim-hash").score_status == "failed"


def test_doi_normalization_and_source_merge_do_not_depend_on_arrival_order():
    assert (
        normalize_doi("HTTPS://DOI.ORG/10.1007/S42994-024-00192-2.") == "10.1007/s42994-024-00192-2"
    )
    assert canonical_work_id(record(doi="doi:10.1007/EXAMPLE-1")) == "work:10.1007/example-1"
    one = merge_candidates(diagnostic("one", 70), diagnostic("two", 90), "claim-hash")
    two = merge_candidates(diagnostic("two", 90), diagnostic("one", 70), "claim-hash")
    assert one.library_ids == ["one", "two"]
    assert one.candidate_score == two.candidate_score == 90
    assert one.source_uris == two.source_uris
    assert one.retrieval_fingerprint == two.retrieval_fingerprint
    assert one.publication_status == "corrected"
    assert one.related_dois["corrected_by"] == ["10.1007/example-correction"]


def test_metadata_propagates_version_unknown_and_retraction_status():
    unknown = MetadataCorpusAdapter(
        LibraryConfig("meta", "Metadata", "fast", 0, simulation=False), [record()]
    ).search(make_claim(text="LFY1 soybean floral"))
    assert unknown[0].retraction_status == "unknown"
    corrected = MetadataCorpusAdapter(
        LibraryConfig("meta", "Metadata", "fast", 0, simulation=False),
        [
            record(
                related_dois={"corrected_by": ["10.1007/correction"]},
                retraction_status="none",
                status_verified_at="2026-11-15",
            )
        ],
    ).search(make_claim(text="LFY1 soybean floral"))
    assert corrected[0].source.publication_status == "corrected"
    retracted = record(pub_types=["Retracted Publication"], retraction_status="none")
    assert publication_status(retracted) == "retracted"
    candidate = MetadataCorpusAdapter(
        LibraryConfig("meta", "Metadata", "fast", 0, simulation=False), [retracted]
    ).search(make_claim(text="LFY1 soybean floral"))[0]
    assert candidate.retraction_status == "retracted"
    assert candidate.document_type_eligible is False


def test_rrf_is_deterministic_and_dense_can_add_hard_constraint_safe_candidate():
    matches = reciprocal_rank_fusion({}, {"work:a": 0.9, "work:b": 0.91})
    assert list(matches) == ["work:a", "work:b"]
    assert matches["work:b"].score > matches["work:a"].score

    class FakeIndex:
        model_revision = "fake-index-v1"

        def similarities(self, claim):
            return {"work:10.1007/example-1": 0.9, "work:10.1007/other": 0.4}

    adapter = SemanticMetadataAdapter(
        LibraryConfig("meta", "Metadata", "fast", 0, simulation=False),
        [record(), record(doi="10.1007/other", paper_id="paper:other")],
        settings=Settings(),
        index=FakeIndex(),
        dense_threshold=0.8,
    )
    first = adapter.search(make_claim(text="homeotic floral organ identity"))
    second = adapter.search(make_claim(text="homeotic floral organ identity"))
    assert [(item.passage.work_id, item.display_score) for item in first] == [
        (item.passage.work_id, item.display_score) for item in second
    ]
    assert first[0].passage.work_id == "work:10.1007/example-1"


def test_agentic_window_keeps_bm25_leader_behind_six_fused_matches(monkeypatch):
    records = [record(doi=f"10.1007/window-{i}", paper_id=f"paper:window-{i}") for i in range(8)]
    ids = [canonical_work_id(item) for item in records]

    class DenseOnlyForOtherMatches:
        model_revision = "window-test"

        def similarities(self, claim):
            return {work_id: 0.96 - i / 100 for i, work_id in enumerate(ids[1:7])}

    def candidates(settings, **kwargs):
        adapter = SemanticMetadataAdapter(
            LibraryConfig("meta", "Metadata", "fast", 0, simulation=False),
            records,
            settings=settings,
            index=DenseOnlyForOtherMatches(),
            **kwargs,
        )
        monkeypatch.setattr(
            adapter._lexical_index,
            "score",
            lambda claim: {work_id: QueryRelevance(8 - i, (), ()) for i, work_id in enumerate(ids)},
        )
        return [
            (item.passage.work_id, item.display_score)
            for item in adapter.search(make_claim(text="floral organ identity"))
        ]

    agentic = Settings()
    pointwise = replace(agentic, candidate_pipeline="pointwise")
    wide = candidates(agentic, top_k=hybrid_candidate_window(agentic))
    legacy = candidates(pointwise, top_k=hybrid_candidate_window(pointwise))
    assert len(wide) == 8
    assert wide[:5] == candidates(agentic) == legacy
    assert wide[6][0] == ids[0]
    assert candidates(agentic, top_k=7) == wide[:7]


def test_joined_lexical_fallback_requires_missing_literal_and_attested_word():
    claim = make_claim(text="Birth-weight and pre-term timing near LFY-2")
    raw = query_plan(claim)
    fixed = lexical_query_fallback(raw, {"birthweight": 5, "pre-term": 1, "preterm": 4, "lfy2": 1})
    assert set(raw.terms) - set(fixed.terms) == {"birth-weight"}
    assert set(fixed.terms) - set(raw.terms) == {"birthweight"}
    assert replace(fixed, terms=raw.terms) == raw
    assert lexical_query_fallback(raw, {"birth-weight": 1, "birthweight": 5}) is raw
    assert lexical_query_fallback(raw, {}) is raw
    excluded = query_plan(make_claim(text="breast cancer, excluding birth-weight."))
    assert lexical_query_fallback(excluded, {"birthweight": 1}) is excluded


def test_hybrid_joined_fallback_does_not_change_query_text_or_doi_filter():
    class NoDenseHits:
        model_revision = "fixed-orthography"

        def similarities(self, claim):
            return {}

    library = LibraryConfig("meta", "Metadata", "fast", 0, simulation=False)
    records = [record(title="Birthweight", abstract="Birthweight was reported.")]
    claim = make_claim(text="Birth-weight")
    base = Settings(candidate_pipeline="pointwise")
    legacy = SemanticMetadataAdapter(library, records, settings=base, index=NoDenseHits())
    current = SemanticMetadataAdapter(
        library, records, settings=replace(base, candidate_pipeline="agentic"), index=NoDenseHits()
    )
    assert legacy.search(claim) == []
    matches = current.search(claim)
    assert [item.passage.work_id for item in matches] == ["work:10.1007/example-1"]
    assert matches[0].context == records[0]["abstract"]
    assert current.model_versions["lexical_query"] == LEXICAL_JOINED_FALLBACK_VERSION
    assert LEXICAL_JOINED_FALLBACK_VERSION in matches[0].score_version
    assert current.search(make_claim(text="Birth-weight 10.1007/wrong-id")) == []


def test_topic_facets_add_recall_without_score_inflation_or_negative_anchors():
    class FacetIndex:
        model_revision = "facet-test-v1"

        def __init__(self):
            self.queries = []

        def similarities(self, claim, *, query_text=None):
            self.queries.append(query_text)
            if query_text is None:
                return {"work:10.1007/example-1": 0.85, "work:10.1007/other": 0.95}
            return {"work:10.1007/example-1": 0.99}

    claim = make_claim(text="long research context").model_copy(
        update={
            "topic_context": [
                "cell-type-specific expression state with direct spatial application",
                "Do not infer clinical validation from a computational model",
            ]
        }
    )
    facets = semantic_query_facets(claim)
    assert len(facets) == 2
    assert facets[1] == "cell-type-specific expression state with direct spatial application"
    index = FacetIndex()
    adapter = SemanticMetadataAdapter(
        LibraryConfig("meta", "Metadata", "fast", 0, simulation=False),
        [record(), record(doi="10.1007/other", paper_id="paper:other")],
        settings=Settings(),
        index=index,
    )
    candidates = adapter.search(claim)
    assert candidates[0].passage.work_id == "work:10.1007/example-1"
    assert candidates[0].display_score <= 100
    assert candidates[0].display_score > candidates[1].display_score
    assert candidates[0].score_version.endswith(":facets-v1")
    assert adapter.model_versions["facet_count"] == "2"
    assert index.queries == [None, facets[1]]


@pytest.mark.parametrize("dense_failure", [False, True])
def test_hybrid_cache_replays_query_specific_scoring_metadata(dense_failure):
    class Index:
        model_revision = "stable-replay-v1"

        def __init__(self):
            self.calls = 0

        def similarities(self, claim, *, query_text=None):
            self.calls += 1
            if dense_failure and claim.claim_text.startswith("Different query"):
                raise RuntimeError("exercise the sparse fallback")
            return {"work:10.1007/example-1": 0.98}

    index = Index()
    adapter = SemanticMetadataAdapter(
        LibraryConfig("meta", "Metadata", "fast", 0, simulation=False),
        [record()],
        settings=Settings(),
        index=index,
    )
    first = make_claim(text="LFY2 soybean").model_copy(update={"topic_context": []})
    other = make_claim(text="Different query about soybean").model_copy(
        update={"topic_context": ["Independent anatomical observations in matched samples"]}
    )

    def run(claim):
        return (
            [
                (row.passage.work_id, row.display_score, row.score_version, row.score_scale_group)
                for row in adapter.search(claim)
            ],
            dict(adapter.model_versions),
        )

    expected = run(first)
    other_result = run(other)
    assert expected[1]["facet_count"] == "1"
    assert other_result[1]["facet_count"] == ("0" if dense_failure else "2")
    calls = index.calls
    assert run(first) == expected
    assert index.calls == calls


def test_cross_library_stream_merge_and_repeat_fingerprint(tmp_path, live_server):
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(
            LibraryConfig("meta-a", "元数据甲库", "fast", 15, simulation=False),
            LibraryConfig("meta-b", "元数据乙库", "fast", 60, simulation=False),
        ),
        fixture_demo_scope_default=False,
    )
    records_a = [record(paper_id="paper:source-a")]
    records_b = [
        record(
            doi="HTTPS://DOI.ORG/10.1007/EXAMPLE-1",
            paper_id="paper:source-b",
            source_metadata_url="https://publisher.example/article/1",
        )
    ]
    adapters = {
        "meta-a": MetadataCorpusAdapter(settings.libraries[0], records_a),
        "meta-b": MetadataCorpusAdapter(settings.libraries[1], records_b),
    }
    base = live_server(
        create_app(settings, adapters=adapters, reason_generator=StubReasonGenerator())
    )
    body = {
        "input_type": "keywords",
        "keywords": ["LFY1", "soybean", "floral"],
        "library_ids": ["meta-a", "meta-b"],
        "options": {"library_delays_ms": {"meta-a": 15, "meta-b": 60}},
    }
    with httpx.Client(timeout=20) as client:
        states = []
        for _ in range(2):
            created = client.post(f"{base}/api/v1/searches", json=body, headers=headers()).json()
            state = wait_final(client, base, created["query_id"])
            states.append(state)
            assert state["results"] == []
            assert len(state["candidate_diagnostics"]) == 1
            candidate = state["candidate_diagnostics"][0]
            assert candidate["work_id"] == "work:10.1007/example-1"
            assert candidate["library_ids"] == ["meta-a", "meta-b"]
            assert candidate["source_uris"] == {
                "meta-a": "https://europepmc.org/article/MED/example",
                "meta-b": "https://publisher.example/article/1",
            }
            events = []
            with client.stream(
                "GET", f"{base}/api/v1/searches/{created['query_id']}/events", headers=headers()
            ) as response:
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        events.append(line.removeprefix("event:").strip())
            assert events.count("candidate_diagnostic") == 2
            assert "recommendation_created" not in events
        assert (
            states[0]["candidate_diagnostics"][0]["retrieval_fingerprint"]
            == states[1]["candidate_diagnostics"][0]["retrieval_fingerprint"]
        )
        assert states[0]["session"]["query_id"] != states[1]["session"]["query_id"]
        created = client.post(
            f"{base}/api/v1/searches",
            json={
                **body,
                "options": {"library_delays_ms": {"meta-a": 60, "meta-b": 15}},
            },
            headers=headers(),
        ).json()
        reversed_state = wait_final(client, base, created["query_id"])
        assert (
            reversed_state["candidate_diagnostics"][0]["retrieval_fingerprint"]
            == states[0]["candidate_diagnostics"][0]["retrieval_fingerprint"]
        )
        snapshot = client.get(
            f"{base}/api/v1/snapshots/{states[0]['session']['final_snapshot_id']}",
            headers=headers(),
        ).json()
        assert snapshot["item_count"] == 0


def test_incomparable_candidate_scores_are_never_maxed():
    other = diagnostic("two", 90).model_copy(update={"score_version": "other-score"})
    merged = merge_candidates(diagnostic("one", 70), other, "claim-hash")
    assert merged.candidate_score is None
    assert merged.score_version is None
    assert merged.score_scale_group is None
    assert merged.score_status == "incomparable"
    assert merged.library_ids == ["one", "two"]
    third = merge_candidates(merged, diagnostic("three", 80), "claim-hash")
    assert third.candidate_score is None
    assert third.score_version is None
    assert third.score_scale_group is None
    assert third.score_status == "incomparable"
    assert third.library_ids == ["one", "three", "two"]


def test_deployed_model_metadata_uses_portable_paths():
    from tools.deploy_citation import ROOT, model_metadata

    require_runtime_file(
        ROOT
        / "data/runtime/embedding-models/models--intfloat--multilingual-e5-small"
        / "files_metadata.json"
    )
    metadata = json.loads(model_metadata())
    assert metadata
    assert all(key.startswith("snapshots/") and "\\" not in key for key in metadata)
    assert any(key.endswith("/onnx/model.onnx") for key in metadata)


def test_semantic_worker_persists_actual_dense_status(tmp_path, live_server):
    class FakeIndex:
        model_revision = "fake-index-v1"

        def similarities(self, claim):
            return {"work:10.1007/example-1": 0.9}

    library = LibraryConfig("meta", "Metadata", "fast", 0, simulation=False)
    settings = replace(
        Settings.for_tests(tmp_path), libraries=(library,), fixture_demo_scope_default=False
    )
    adapter = SemanticMetadataAdapter(library, [record()], settings=settings, index=FakeIndex())
    base = live_server(
        create_app(
            settings,
            adapters={library.library_id: adapter},
            reason_generator=StubReasonGenerator(),
        )
    )
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            json={"input_type": "keywords", "keywords": ["LFY1", "soybean"]},
            headers=headers(),
        ).json()
        state = wait_final(client, base, created["query_id"])
    versions = state["workers"][0]["model_versions"]
    assert versions["dense_status"] == "available"
    assert versions["embedding_revision"] == "fake-index-v1"


def test_concurrent_embedding_indexes_register_custom_model_once(tmp_path, monkeypatch):
    from fastembed import TextEmbedding

    from citation_tool import semantic

    registered = []
    initialized = []

    def register(**kwargs):
        registered.append(kwargs["model"])

    def initialize(model, **kwargs):
        initialized.append(kwargs["model_name"])

    monkeypatch.setattr(semantic, "_CUSTOM_MODELS_REGISTERED", set())
    monkeypatch.setattr(TextEmbedding, "add_custom_model", register)
    monkeypatch.setattr(TextEmbedding, "__init__", initialize)
    indexes = [
        EmbeddingIndex([record()], model_name="intfloat/multilingual-e5-small", cache_dir=tmp_path)
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda index: index._ensure_model(), indexes))
    assert registered == ["intfloat/multilingual-e5-small"]
    assert len(initialized) == 2


def test_embedding_cache_binds_snapshot_revision_and_reuses_documents(tmp_path):
    model_root = tmp_path / "models--acme--test"
    model_file = model_root / "snapshots" / "rev-one" / "onnx" / "model.onnx"
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"fixture model")
    (model_root / "refs").mkdir()
    (model_root / "refs" / "main").write_text("rev-one", encoding="utf-8")
    calls = []

    class FakeModel:
        def embed(self, texts, batch_size):
            calls.append(list(texts))
            return ([1.0, 0.0] for _ in texts)

    index = EmbeddingIndex(
        [record()], model_name="acme/test", cache_dir=tmp_path, model_loader=FakeModel
    )
    assert index.similarities(make_claim()) == {"work:10.1007/example-1": 1.0}
    assert index.model_revision == "rev-one"
    assert len(calls) == 2
    cached = EmbeddingIndex(
        [record()], model_name="acme/test", cache_dir=tmp_path, model_loader=FakeModel
    )
    assert cached.similarities(make_claim()) == {"work:10.1007/example-1": 1.0}
    assert len(calls) == 3


def test_failed_remote_acceptance_restores_only_the_previous_service_and_new_route(monkeypatch):
    from tools import deploy_citation

    calls = []
    monkeypatch.setattr(
        deploy_citation,
        "remote",
        lambda command, payload=None, timeout=600: calls.append(command),
    )

    def fail_after_restart(context):
        context.update(
            previous_root="/opt/citation-tool/releases/previous",
            unit_backup="/opt/citation-tool/releases/test/previous.service",
            caddy_backup="/etc/caddy/Caddyfile.bak-citation-test",
            restart_attempted=True,
        )
        raise ValueError("simulated failed acceptance")

    monkeypatch.setattr(deploy_citation, "deploy", fail_after_restart)
    with pytest.raises(RuntimeError, match="rollback attempted"):
        deploy_citation.main()
    assert calls == [
        "sudo cp /opt/citation-tool/releases/test/previous.service "
        "/etc/systemd/system/citation-tool.service "
        "&& sudo systemctl daemon-reload && sudo systemctl restart citation-tool",
        "sudo ln -sfn /opt/citation-tool/releases/previous /opt/citation-tool/current",
        "sudo cp /etc/caddy/Caddyfile.bak-citation-test /etc/caddy/Caddyfile "
        "&& sudo caddy validate --config /etc/caddy/Caddyfile "
        "&& sudo systemctl reload caddy",
    ]


def test_release_dedup_only_links_identical_dependency_files(tmp_path, monkeypatch):
    import sys

    from tools.deploy_citation import RELEASE_DEDUP_SCRIPT

    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    for root in (old_root, new_root):
        (root / ".venv/lib").mkdir(parents=True)
        (root / ".venv/lib/same.txt").write_text("identical", encoding="utf-8")
        (root / ".venv/lib/different.txt").write_text(
            "old" if root == old_root else "new", encoding="utf-8"
        )
    original = (old_root / ".venv/lib/different.txt").read_bytes()
    monkeypatch.setattr(sys, "argv", ["python", str(new_root)])
    exec(RELEASE_DEDUP_SCRIPT, {})  # noqa: S102 - static internal script under test
    assert (new_root / ".venv/lib/same.txt").samefile(old_root / ".venv/lib/same.txt")
    assert not (new_root / ".venv/lib/different.txt").samefile(old_root / ".venv/lib/different.txt")
    assert (new_root / ".venv/lib/different.txt").read_text(encoding="utf-8") == "new"
    assert (old_root / ".venv/lib/different.txt").read_bytes() == original
