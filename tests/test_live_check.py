from __future__ import annotations

import pytest

from citation_tool.config import LOCAL_ONLY_ADAPTER_MODE
from citation_tool.entities import (
    CandidateDiagnostic,
    CandidateRanking,
    RecommendationReason,
    SearchSession,
    SearchStateResponse,
    WorkerStatus,
)
from citation_tool.live_check import DEMO_DOI, LiveCheckError, _verified_ranking


@pytest.fixture
def state():
    candidate = CandidateDiagnostic.model_construct(
        doi=DEMO_DOI,
        work_id="target",
        claim_id="claim",
        title="Published article",
        evidence_excerpt="Public abstract with evidence.",
        reason_evidence_quote="abstract with evidence",
        ranking_status="ranked",
        llm_rank=1,
        pool_version=3,
        ranking_version=2,
        reason=RecommendationReason.model_construct(
            status="generated", text="Relevant evidence.", served_model="actual-model"
        ),
    )
    return SearchStateResponse.model_construct(
        session=SearchSession.model_construct(search_state="completed"),
        adapter_mode="real_abstract_metadata_live_llm",
        workers=[
            WorkerStatus.model_construct(
                library_id="europepmc-online",
                worker_state="completed",
                source_agent_status="completed",
                candidates_seen=3,
                model_versions={"normalized_records": "0"},
            )
        ],
        candidate_diagnostics=[candidate],
        candidate_rankings=[
            CandidateRanking.model_construct(
                claim_id="claim",
                status="completed",
                ordered_work_ids=["target"],
                pool_version=3,
                ranking_version=2,
            )
        ],
        libraries=[],
    )


def test_live_check_accepts_final_real_rank_and_reason(state):
    target, count = _verified_ranking(state)
    assert target.doi == DEMO_DOI and count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("ranking_status", "disabled"),
        ("llm_rank", None),
        ("pool_version", 2),
        ("ranking_version", 1),
        ("reason_evidence_quote", "made up"),
    ],
)
def test_live_check_rejects_stale_ranking_and_unbacked_reasons(state, field, value):
    setattr(state.candidate_diagnostics[0], field, value)
    with pytest.raises(LiveCheckError, match="原文绑定"):
        _verified_ranking(state)


def test_live_check_rejects_fallback_and_online_worker_without_hits(state):
    with pytest.raises(LiveCheckError, match="仅本地备用"):
        _verified_ranking(state.model_copy(update={"adapter_mode": LOCAL_ONLY_ADAPTER_MODE}))
    state.workers[0].candidates_seen = 0
    with pytest.raises(LiveCheckError, match="没有召回候选"):
        _verified_ranking(state)
    state.workers[0].worker_state = "failed"
    state.workers[0].last_error = {"code": "SOURCE_TIMEOUT"}
    with pytest.raises(LiveCheckError, match="SOURCE_TIMEOUT"):
        _verified_ranking(state)


def test_live_check_rejects_incomplete_ranking_or_model_identity(state):
    state.candidate_rankings[0].ordered_work_ids.append("unseen work")
    with pytest.raises(LiveCheckError, match="完整 LLM 榜单"):
        _verified_ranking(state)
    state.candidate_rankings[0].ordered_work_ids.pop()
    state.candidate_diagnostics[0].reason.served_model = None
    with pytest.raises(LiveCheckError, match="原文绑定"):
        _verified_ranking(state)
