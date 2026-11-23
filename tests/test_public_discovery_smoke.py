from __future__ import annotations

import copy

import pytest
from tools.public_smoke import TARGET_DOI, discovery_contract_issues, discovery_stream_completed

from citation_tool.common_scoring import COMMON_SCORE_VERSION
from citation_tool.listwise import LISTWISE_PROMPT_VERSION


def valid_state():
    return {
        "workers": [
            {"library_id": library, "worker_state": "completed", "source_agent_status": "completed"}
            for library in ("europepmc-online", "europepmc-snapshot", "abiotech-sample")
        ],
        "candidate_rankings": [
            {
                "claim_id": "claim",
                "pool_version": 2,
                "ranking_version": 3,
                "status": "completed",
                "prompt_version": LISTWISE_PROMPT_VERSION,
                "ordered_work_ids": ["work:target"],
            }
        ],
        "candidate_diagnostics": [
            {
                "claim_id": "claim",
                "work_id": "work:target",
                "title": "Actual title",
                "evidence_excerpt": "Actual abstract evidence.",
                "reason_evidence_quote": "Actual abstract",
                "ranking_status": "ranked",
                "pool_version": 2,
                "base_rank": 1,
                "llm_rank": 1,
                "rank_delta": 0,
                "score_version": COMMON_SCORE_VERSION + ":e5",
                "score_scale_group": "fixed-background",
                "reason": {"status": "generated"},
            }
        ],
    }


def test_public_smoke_requires_source_agents_and_grounded_current_ranking():
    assert discovery_contract_issues(valid_state()) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("llm_rank", None),
        ("rank_delta", 2),
        ("reason_evidence_quote", "invented quote"),
        ("score_version", "legacy-alignment"),
        ("pool_version", 1),
    ],
)
def test_public_smoke_does_not_accept_partial_or_legacy_ranking(field, value):
    state = valid_state()
    state["candidate_diagnostics"][0][field] = value
    assert any(issue.startswith("invalid_rank") for issue in discovery_contract_issues(state))


def test_historical_pending_manifest_does_not_hide_completed_current_version():
    state = valid_state()
    previous = copy.deepcopy(state["candidate_rankings"][0])
    previous.update(pool_version=1, ranking_version=1, status="pending", ordered_work_ids=[])
    state["candidate_rankings"].append(previous)
    assert discovery_contract_issues(state) == []


def test_public_smoke_rejects_duplicate_ids_and_mixed_score_scales():
    state = valid_state()
    state["candidate_rankings"][0]["ordered_work_ids"] *= 2
    assert "invalid_complete_ranking:claim" in discovery_contract_issues(state)
    state = valid_state()
    state["candidate_diagnostics"][0]["score_scale_group"] = None
    assert "common_score_group_missing_or_mixed" in discovery_contract_issues(state)


def test_worker_count_alone_is_not_three_source_completion():
    state = valid_state()
    state["workers"][0]["library_id"] = "abiotech-sample"
    assert "three_source_agents_not_completed" in discovery_contract_issues(state)


def test_public_stream_requires_new_pool_and_rerank_events_before_finalization():
    state = valid_state()
    state["session"] = {"search_state": "completed"}
    state["candidate_diagnostics"][0]["doi"] = TARGET_DOI
    events = [
        "candidate_pool_updated",
        "worker_completed",
        "candidate_reranked",
        "search_finalized",
    ]
    assert discovery_stream_completed(state, events)
    assert not discovery_stream_completed(
        state, ["candidate_diagnostic", "worker_completed", "search_finalized"]
    )
    assert not discovery_stream_completed(state, events[:-1])
