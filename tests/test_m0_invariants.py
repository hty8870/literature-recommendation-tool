"""Independent product invariants, not assertions tailored to fixture success."""

from citation_tool.config import LibraryConfig
from citation_tool.entities import ClaimContext
from citation_tool.evidence import build_recommendation
from citation_tool.retrieval import FixtureLibraryAdapter


def test_unknown_scope_is_not_citable_or_rankable() -> None:
    """AT-EVD-006: missing scope cannot silently become a supporting match."""
    claim = ClaimContext(
        tenant_id="test",
        query_id="query-unknown-scope",
        claim_id="claim-unknown-scope",
        anchor_hash="test-anchor",
        claim_rule_version="test-v1",
        input_type="keywords",
        claim_type="topic",
        claim_text="soybean flowering",
        topic_context=["soybean flowering"],
        claim_index=0,
        claim_hash="test-claim",
        created_at="2026-11-14T00:00:00Z",
    )
    adapter = FixtureLibraryAdapter(
        LibraryConfig(
            library_id="fixture-test",
            display_name="fixture",
            fixture_set="fast",
            delay_ms=0,
        )
    )
    recommendation = build_recommendation(
        tenant_id=claim.tenant_id,
        query_id=claim.query_id,
        claim=claim,
        candidate=adapter.search(claim)[0],
    )

    assert recommendation.evidence[0].scope_match == "unknown"
    assert not recommendation.citable
    assert not recommendation.rankable
    assert recommendation.display_score is None
