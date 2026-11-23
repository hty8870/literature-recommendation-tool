from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import (
    ClaimContext,
    EvidenceScope,
    Passage,
    RecommendationSource,
    SourceLocator,
    stable_id,
    utc_now_iso,
)
from citation_tool.evidence import Candidate, build_recommendation, sha256_text
from citation_tool.jcs import canonical_json, sha256_json

TEST_SCOPE = EvidenceScope(species=["Glycine max"], method=["expression profiling"])


def make_claim(
    *, claim_id: str = "clm_test", scope: EvidenceScope | None = None, text: str = "LFY2 soybean"
) -> ClaimContext:
    now = utc_now_iso()
    claim_scope = scope or EvidenceScope(**TEST_SCOPE.model_dump())
    return ClaimContext(
        tenant_id="local",
        claim_id=claim_id,
        query_id="q_test",
        anchor_hash=sha256_json({"text": text}),
        claim_rule_version="claim-rule-v1",
        input_type="keywords",
        claim_type="topic",
        claim_text=text,
        topic_context=text.split(),
        claim_index=0,
        scope=claim_scope,
        claim_hash=sha256_json({"text": text, "scope": claim_scope.model_dump()}),
        created_at=now,
    )


def make_candidate(
    *,
    raw_text: str = "Simulation: LFY2 expression was observed in soybean floral meristems.",
    quote: str = "LFY2 expression was observed in soybean floral meristems",
    reading_order_confidence: float = 0.99,
    metadata_verified: bool = True,
    scope: EvidenceScope | None = None,
) -> Candidate:
    now = utc_now_iso()
    passage = Passage(
        passage_id="paper:p1#p1",
        work_id="work:w1",
        paper_id="paper:p1",
        library_id="fixture-fast",
        raw_text=raw_text,
        normalized_text=raw_text,
        section_path=["Results"],
        paragraph_index=0,
        reading_order_index=4,
        page_start=1,
        page_end=1,
        printed_page_start="1",
        printed_page_end="1",
        created_at=now,
        updated_at=now,
    )
    source = RecommendationSource(
        paper_id="paper:p1",
        library_id="fixture-fast",
        source_role="publisher_pdf",
        entitlement_status="allowed",
        metadata_verified=metadata_verified,
        simulation=True,
        adapter_id="fixture-local-v1",
        display_name="本地演示快库",
    )
    locator = SourceLocator(
        pdf_page=1,
        printed_page="1",
        section_path=["Results"],
        paragraph_index=0,
        reading_order_index=4,
        reading_order_confidence=reading_order_confidence,
        source_uri="fixture://paper:p1",
    )
    return Candidate(
        passage=passage,
        quote=quote,
        display_score=88.5,
        evidence_role="background",
        locator=locator,
        source=source,
        scope=scope or EvidenceScope(**TEST_SCOPE.model_dump()),
        context=raw_text,
    )


def test_jcs_sorts_utf16_and_rejects_non_finite() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"\ue000": 1, "\U00010000": 2, "a": 3}) == (
        '{"a":3,"\U00010000":2,"\ue000":1}'
    )
    assert canonical_json({"x": "中文", "y": 'a"b\n'}) == '{"x":"中文","y":"a\\"b\\n"}'
    assert canonical_json([1e20, 1e-7, 1e-6, 1.0, -0.0]) == (
        "[100000000000000000000,1e-7,0.000001,1,0]"
    )
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json([float("inf")])
    assert sha256_json({"a": 1}) == sha256_json({"a": 1})
    assert sha256_json({"a": 1}) != sha256_json({"a": 2})


def test_evidence_uses_exact_utf8_offsets() -> None:
    raw = "前言：LFY2 单倍型 B 与大豆开花提前相关，后文。"
    quote = "LFY2 单倍型 B 与大豆开花提前相关"
    candidate = make_candidate(raw_text=raw, quote=quote)
    result = build_recommendation(
        tenant_id="local", query_id="q_test", claim=make_claim(), candidate=candidate
    )
    evidence = result.evidence[0]
    encoded = raw.encode("utf-8")
    assert evidence.text_offset_valid is True
    assert encoded[evidence.start_offset : evidence.end_offset].decode("utf-8") == quote
    assert evidence.end_offset - evidence.start_offset == len(quote.encode("utf-8"))
    assert evidence.quote_sha256 == sha256_text(quote)
    assert evidence.raw_text_sha256 == sha256_text(raw)
    assert evidence.source_trace_valid is True
    assert result.citable is True
    assert result.rankable is True
    assert result.score_scale_group == "ssg:fixture-bootstrap-v1"
    assert result.rankable_group == "ssg:fixture-bootstrap-v1"


def test_evidence_rejects_tampering_low_quality_and_bad_metadata() -> None:
    claim = make_claim()
    tampered = make_candidate(quote="not present in the passage")
    result = build_recommendation(
        tenant_id="local", query_id="q_test", claim=claim, candidate=tampered
    )
    assert result.evidence[0].text_offset_valid is False
    assert result.evidence[0].source_trace_valid is False
    assert result.citable is False
    assert result.rankable is False

    low_quality = make_candidate(reading_order_confidence=0.2)
    result = build_recommendation(
        tenant_id="local", query_id="q_test", claim=claim, candidate=low_quality
    )
    assert result.evidence[0].reading_order_valid is False
    assert result.citable is False

    unverified = make_candidate(metadata_verified=False)
    result = build_recommendation(
        tenant_id="local", query_id="q_test", claim=claim, candidate=unverified
    )
    assert result.citable is False


def test_scope_mismatch_is_insufficient_and_claim_identity_is_independent() -> None:
    claim = make_claim(scope=EvidenceScope(species=["Glycine max"]))
    mismatched = make_candidate(scope=EvidenceScope(species=["Arabidopsis thaliana"]))
    result = build_recommendation(
        tenant_id="local", query_id="q_test", claim=claim, candidate=mismatched
    )
    assert result.evidence[0].scope_match == "mismatch"
    assert result.relationship == "insufficient"
    assert result.citable is False

    candidate = make_candidate()
    first = build_recommendation(
        tenant_id="local",
        query_id="q_test",
        claim=make_claim(claim_id="clm_a"),
        candidate=candidate,
    )
    second = build_recommendation(
        tenant_id="local",
        query_id="q_test",
        claim=make_claim(claim_id="clm_b"),
        candidate=candidate,
    )
    assert first.result_id != second.result_id
    assert first.evidence[0].evidence_id != second.evidence[0].evidence_id
    assert first.citable == second.citable is True
    assert first.rankable == second.rankable is True
    assert first.score_scale_group == second.score_scale_group


def test_config_validation_and_test_defaults(tmp_path: Path) -> None:
    settings = Settings.for_tests(tmp_path)
    assert settings.db_path == tmp_path / "app.db"
    assert settings.default_tenant == "local"
    assert settings.fixture_demo_scope_default is True
    assert Settings().fixture_demo_scope_default is False
    assert not hasattr(settings, "api_key")
    duplicate = LibraryConfig("dup", "one", "fast", 1)
    with pytest.raises(ValueError):
        replace(settings, libraries=(duplicate, duplicate)).validate()
    with pytest.raises(ValueError):
        replace(settings, libraries=(LibraryConfig("bad", "bad", "fast", -1),)).validate()
    with pytest.raises(ValueError):
        replace(settings, sse_poll_ms=0).validate()


def test_stable_id_namespaces_are_different() -> None:
    assert stable_id("clm_", "local", "q1", "a", "v1") != stable_id(
        "clm_", "local", "q2", "a", "v1"
    )
