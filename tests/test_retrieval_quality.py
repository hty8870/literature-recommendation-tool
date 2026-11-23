from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx

from citation_tool.api import create_app
from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import EvidenceScope, SearchCreated
from citation_tool.evidence import build_recommendation
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.reasons import reason_entities_grounded
from citation_tool.retrieval import build_fixture_adapters
from test_agent import StubReasonGenerator
from test_integration import headers, wait_final
from test_units import make_candidate, make_claim


def test_semantic_query_preserves_positive_sentences_after_a_negation() -> None:
    from citation_tool.relevance import semantic_query_text

    claim = make_claim(
        text=(
            "We study grain number per panicle rather than grain width. "
            "We need evidence for E3 partner binding and proteasome-dependent protein turnover."
        )
    ).model_copy(update={"input_type": "abstract", "query_terms": []})
    query = semantic_query_text(claim)
    assert "grain number per panicle" in query
    assert "E3 partner binding" in query
    assert "proteasome-dependent protein turnover" in query
    assert "grain width" not in query


def test_method_identifiers_do_not_turn_multi_object_queries_into_impossible_and() -> None:
    def method_record(doi: str, title: str, abstract: str) -> dict:
        return {
            "work_id": f"work:{doi}",
            "paper_id": f"paper:{doi}",
            "title": title,
            "abstract": abstract,
            "doi": doi,
            "year": 2025,
            "authors": "Example",
            "pub_types": ["research-article"],
            "source_metadata_url": "https://example.org/article",
            "simulation": False,
        }

    records = [
        method_record(
            "10.1007/kraken",
            "Improved metagenomic analysis with Kraken 2.",
            "Kraken 2 reduces memory and adds translated search.",
        ),
        method_record(
            "10.1007/centrifuge",
            "Centrifuge metagenomic classification.",
            "A BWT and FM-index classifier for metagenomic reads.",
        ),
        method_record("10.1007/unrelated", "Cell biology", "A different topic entirely."),
    ]
    adapter = MetadataCorpusAdapter(
        LibraryConfig("methods", "Methods", "fast", 0, simulation=False), records
    )
    candidates = adapter.search(make_claim(text="Kraken2 Centrifuge metagenomic classifier"))
    assert {item.passage.work_id for item in candidates} == {
        "work:10.1007/kraken",
        "work:10.1007/centrifuge",
    }
    exact = adapter.search(make_claim(text="Kraken2"))
    assert [item.passage.work_id for item in exact] == ["work:10.1007/kraken"]


def test_sparse_retrieval_preserves_identifiers_synonyms_and_negation() -> None:
    settings = Settings()
    adapter = build_fixture_adapters(settings)["fixture-fast"]
    positive = adapter.search(make_claim(text="LFY2 Glycine max floral transition"))
    assert {item.passage.work_id for item in positive} == {
        "work:10.5555/demo-fast-001",
        "work:10.5555/demo-fast-002",
    }
    assert [item.display_score for item in positive] == sorted(
        (item.display_score for item in positive), reverse=True
    )
    repeated = adapter.search(make_claim(text="LFY2 Glycine max floral transition"))
    assert [(item.passage.work_id, item.display_score) for item in positive] == [
        (item.passage.work_id, item.display_score) for item in repeated
    ]
    assert (
        adapter.search(
            make_claim(
                text="nitrate transport Oryza sativa roots without measuring LFY2 or flowering"
            )
        )
        == []
    )
    assert adapter.search(make_claim(text="不要大豆，只找拟南芥开花")) == []
    assert adapter.search(make_claim(text="LFY1 without LFY2")) == []
    exact = adapter.search(make_claim(text="10.5555/demo-fast-001 flowering"))
    assert len(exact) == 1
    assert exact[0].passage.work_id == "work:10.5555/demo-fast-001"


def test_scope_aliases_and_unrequested_dimensions_are_not_hard_filters() -> None:
    candidate = make_candidate()
    result = build_recommendation(
        tenant_id="local",
        query_id="q_test",
        claim=make_claim(scope=EvidenceScope(species=["大豆"])),
        candidate=candidate,
    )
    assert result.evidence[0].scope_match == "match"
    assert result.citable is True
    missing = build_recommendation(
        tenant_id="local",
        query_id="q_test",
        claim=make_claim(scope=EvidenceScope(species=["Glycine max"])),
        candidate=make_candidate(scope=EvidenceScope(method=["expression profiling"])),
    )
    assert missing.evidence[0].scope_match == "unknown"
    assert missing.citable is False
    assert missing.rejection_reasons == ["evidence_scope_missing"]
    conflicting = build_recommendation(
        tenant_id="local",
        query_id="q_test",
        claim=make_claim(scope=EvidenceScope(species=["大豆"], system=["田间"])),
        candidate=make_candidate(
            scope=EvidenceScope(species=["Glycine max"], system=["greenhouse"])
        ),
    )
    assert conflicting.evidence[0].scope_match == "mismatch"
    assert conflicting.citable is False


def test_reason_cannot_import_entities_from_another_source() -> None:
    candidate = make_candidate()
    assert reason_entities_grounded("LFY2 表达证据与目标组织相关，仍需核查原文范围。", candidate)
    assert not reason_entities_grounded("AP1 与 LFY2 的共表达已在本研究得到验证。", candidate)
    assert not reason_entities_grounded("该水稻实验支持氮利用机制。", candidate)
    family_source = replace(candidate, context="LFY2 regulates AP1a and AP1b expression.")
    assert reason_entities_grounded("LFY2 可能影响 AP1 家族表达。", family_source)
    assert not reason_entities_grounded("LFY2 可能影响 AP2 家族表达。", family_source)
    normalized_source = replace(
        candidate,
        context=(
            "LEAFY (LFY) includes LFY2. CRISPR/Cas9-mediated editing was performed "
            "in SFT1, SFT2 and SFT3 using A0- and B0-series soyasaponins."
        ),
    )
    assert reason_entities_grounded(
        "LEAFY2 可用 Cas9 在 SFT1-3 中编辑；A0 与 B0 是不同系列。",
        normalized_source,
    )
    assert not reason_entities_grounded("SFT1-4 均有相同表型。", normalized_source)
    modification_source = replace(candidate, context="The m 5C and m 6A modifications interact.")
    assert reason_entities_grounded("m5C 与 m6A 的相互作用值得核查。", modification_source)


def test_repeated_empty_searches_finalize_with_independent_snapshots(tmp_path, live_server) -> None:
    settings = replace(Settings.for_tests(tmp_path), fixture_demo_scope_default=False)
    base = live_server(create_app(settings))
    body = {
        "input_type": "keywords",
        "keywords": ["LFY2"],
        "options": {
            "library_delays_ms": {"fixture-fast": 1, "fixture-slow": 1},
            "soft_deadline_ms": 5000,
        },
    }
    with httpx.Client(timeout=20) as client:
        states = []
        for _ in range(2):
            created = client.post(f"{base}/api/v1/searches", json=body, headers=headers()).json()
            state = wait_final(client, base, created["query_id"])
            assert state["session"]["search_state"] == "completed"
            assert state["results"] == []
            assert len(state["candidate_diagnostics"]) == 4
            assert all(
                diagnostic["rejection_reasons"] == ["evidence_scope_missing"]
                for diagnostic in state["candidate_diagnostics"]
            )
            snapshot = client.get(
                f"{base}/api/v1/snapshots/{state['session']['final_snapshot_id']}",
                headers=headers(),
            ).json()
            assert snapshot["item_count"] == 0
            states.append((state, snapshot))
        assert states[0][0]["session"]["final_snapshot_id"] == states[0][1]["snapshot_id"]
        assert states[0][0]["session"]["query_id"] != states[1][0]["session"]["query_id"]
        assert states[0][1]["snapshot_id"] != states[1][1]["snapshot_id"]
        assert states[0][1]["evidence_bundle_id"] == states[1][1]["evidence_bundle_id"]


def test_real_abstracts_are_candidates_not_full_text_citations(tmp_path, live_server) -> None:
    records = [
        {
            "work_id": "work:10.1007/example-original",
            "paper_id": "paper:example-original",
            "title": "FHBDSR-Net measures diseased spikelets in wheat",
            "abstract": "We measured Fusarium diseased spikelets using FHBDSR-Net in wheat.",
            "doi": "10.1007/example-original",
            "year": 2025,
            "pub_types": ["research-article"],
            "source_metadata_url": "https://europepmc.org/article/MED/example-original",
        },
        {
            "work_id": "work:10.1007/example-correction",
            "paper_id": "paper:example-correction",
            "title": "Correction: FHBDSR-Net measures diseased spikelets in wheat",
            "abstract": "This corrects the original article.",
            "doi": "10.1007/example-correction",
            "year": 2025,
            "pub_types": ["Published Erratum"],
            "source_metadata_url": "https://europepmc.org/article/MED/example-correction",
        },
    ]
    library = LibraryConfig("abiotech-real", "aBiotech 真实摘要", "fast", 1, simulation=False)
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(library,),
        fixture_demo_scope_default=False,
    )
    adapter = MetadataCorpusAdapter(library, records)
    app = create_app(
        settings,
        adapters={library.library_id: adapter},
        reason_generator=StubReasonGenerator(),
    )
    base = live_server(app)
    body = {
        "input_type": "keywords",
        "keywords": ["FHBDSR-Net", "Fusarium", "wheat"],
        "options": {"library_delays_ms": {library.library_id: 1}},
    }
    with httpx.Client(timeout=20) as client:
        created = client.post(f"{base}/api/v1/searches", json=body, headers=headers()).json()
        assert created["simulation"] is False
        assert created["adapter_mode"] == "real_abstract_metadata_live_llm"
        tool_result = asyncio.run(
            app.state.search_service.tool_result("local", SearchCreated.model_validate(created))
        )
        assert tool_result["simulation"] is False
        assert tool_result["reason_model_configured"] is True
        assert tool_result["candidate_diagnostics"]
        assert client.get(f"{base}/healthz").json()["simulation"] is False
        assert client.get(f"{base}/api/v1/libraries").json()["simulation"] is False
        state = wait_final(client, base, created["query_id"])
        assert state["results"] == []
        assert len(state["candidate_diagnostics"]) == 2
        original = next(
            item
            for item in state["candidate_diagnostics"]
            if item["work_id"] == "work:10.1007/example-original"
        )
        correction = next(
            item
            for item in state["candidate_diagnostics"]
            if item["work_id"] == "work:10.1007/example-correction"
        )
        assert original["reason"]["status"] == "generated"
        assert original["evidence_excerpt"] == records[0]["abstract"]
        assert original["source_uri"] == records[0]["source_metadata_url"]
        assert "source_trace_invalid" in original["rejection_reasons"]
        assert correction["reason"] is None
        assert "source_ineligible" in correction["rejection_reasons"]
        assert correction["publication_types"] == ["published erratum"]


def test_corpus_v3_normalizes_real_metadata_and_rejects_incomplete_records() -> None:
    from tools.quality_corpus_expand_v3 import normalize_record

    item = {
        "doi": "HTTPS://DOI.ORG/10.1007/EXAMPLE-V3.",
        "pmid": "12345",
        "title": "A <i>real</i> title",
        "abstractText": "A real abstract. " * 30,
        "pubYear": "2026",
        "authorString": "Wang L, Liu H",
        "pubTypeList": {"pubType": ["research-article"]},
    }
    record = normalize_record(item)
    assert record is not None
    assert record["work_id"] == "work:10.1007/example-v3"
    assert record["doi"] == "10.1007/example-v3"
    assert record["title"] == "A real title"
    assert record["source_metadata_url"] == "https://europepmc.org/article/MED/12345"
    assert record["retraction_status"] == "unknown"
    assert record["simulation"] is False
    assert normalize_record({**item, "abstractText": "too short"}) is None
    assert normalize_record({**item, "doi": "not-a-doi"}) is None


def test_reused_bm25_index_matches_recomputed_scores() -> None:
    from citation_tool.relevance import BM25Index, score_records

    records = [
        (
            "work:a",
            "LFY2 soybean flowering",
            "LFY2 controls floral organ identity in soybean.",
            "10.1007/a",
        ),
        ("work:b", "Rice nitrogen use", "Nitrate transport affects rice roots.", "10.1007/b"),
        ("work:c", "Unrelated review", "A general review of cell biology.", "10.1007/c"),
    ]
    claim = make_claim(text="LFY2 soybean floral development")
    expected = score_records(claim, records)
    index = BM25Index(records)
    assert index.score(claim) == expected
    assert index.score(claim) == expected


def test_reason_cache_is_reused_across_http_queries(tmp_path, live_server) -> None:
    from langchain_core.messages import AIMessage

    from citation_tool.reasons import DeepSeekModelGateway, DeepSeekReasonGenerator, ModelConfig
    from citation_tool.store import Store
    from test_agent import ScriptedChatModel
    from test_governance_semantic import record

    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content=(
                    '{"reason":"该文涉及目标基因及组织，可进一步核查其适用范围。",'
                    '"alignment_score":3,"missing_constraints":[]}'
                )
            )
        ]
    )
    gateway = DeepSeekModelGateway(
        ModelConfig(api_key="unit-test-placeholder"), max_inflight=1, timeout_ms=1000
    )
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(LibraryConfig("meta", "真实摘要", "fast", 0, simulation=False),),
        fixture_demo_scope_default=False,
    )
    store = Store(settings.db_path)
    reasons = DeepSeekReasonGenerator(
        gateway, max_inflight=1, timeout_ms=1000, model=model, cache=store
    )
    adapter = MetadataCorpusAdapter(settings.libraries[0], [record()])
    base = live_server(
        create_app(
            settings,
            adapters={"meta": adapter},
            reason_generator=reasons,
        )
    )
    body = {"input_type": "keywords", "keywords": ["LFY1", "soybean"]}
    with httpx.Client(timeout=20) as client:
        first = wait_final(
            client,
            base,
            client.post(f"{base}/api/v1/searches", json=body, headers=headers(key="one")).json()[
                "query_id"
            ],
        )
        second = wait_final(
            client,
            base,
            client.post(f"{base}/api/v1/searches", json=body, headers=headers(key="two")).json()[
                "query_id"
            ],
        )
    first_candidate = first["candidate_diagnostics"][0]
    second_candidate = second["candidate_diagnostics"][0]
    assert first_candidate["reason"]["input_hash"] == second_candidate["reason"]["input_hash"]
    assert first_candidate["reason"]["alignment_score"] == 3
    assert first_candidate["candidate_score"] == second_candidate["candidate_score"]
    assert first_candidate["reason"]["generated_at"] == second_candidate["reason"]["generated_at"]
    assert model.responses == []
