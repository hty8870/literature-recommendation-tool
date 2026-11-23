import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from citation_tool.agent import TOOL_DESCRIPTION, LiteratureSearchTool
from citation_tool.agent_observations import (
    ABSTRACT_EVIDENCE_GUIDANCE,
    EVIDENCE_VIEW_VERSION,
    abstract_evidence_observation,
    evidence_observation,
)
from citation_tool.config import Settings
from citation_tool.entities import SearchCreate, SearchCreated
from citation_tool.runtime import settings_from_env


def _payload():
    representative = "Complete abstract with  two spaces.\nMethods, results and limitations."
    variant = "Complete and genuinely different source text."
    sources = [
        {"library_id": library, "title": "Paper", "paper_id": f"paper-{index}", "abstract": text}
        for index, (library, text) in enumerate(
            (("alpha", representative), ("beta", variant), ("gamma", variant))
        )
    ]
    candidates = [
        {
            "result_id": f"res_{index}",
            "work_id": f"work:{index}",
            "paper_id": f"paper:{index}",
            "claim_id": "clm_1",
            "evidence_excerpt": representative,
            "source_records": sources,
            "llm_rank": index + 1,
            "publication_status": "retracted" if index == 11 else "unknown",
            "ranking_status": "ineligible" if index == 11 else "ranked",
            "relationship": "insufficient",
            "scope_match": "unknown",
            "rejection_reasons": ["abstract_metadata_only"],
            "reason": {"status": "generated", "text": "Matched study limit", "input_hash": "omit"},
            "reason_evidence_quote": "Complete abstract with  two spaces.",
            "retrieval_traces": [{"query": "internal trace " * 120}],
        }
        for index in range(15)
    ]
    return {
        "query_id": "q_tool",
        "complete": True,
        "coverage": {"failed_library_ids": ["gamma"]},
        "candidate_diagnostics": candidates,
        "candidate_rankings": [{"claim_id": "clm_1", "status": "completed", "ranking_version": 1}],
        "workers": [{"library_id": "gamma", "worker_state": "failed", "source_actions": ["noise"]}],
        "results": [{"result_id": "res_verified", "evidence": [{"quote": "actual full text"}]}],
        "final_snapshot_id": "snap_original",
        "simulation": False,
        "diagnostic_note": "old",
    }


def test_observation_carries_all_candidates_and_distinct_source_bytes_with_no_mutation():
    old = _payload()
    saved = deepcopy(old)
    outcome = evidence_observation(old)
    assert old == saved
    assert outcome["evidence_view_version"] == EVIDENCE_VIEW_VERSION
    assert outcome["results"] == old["results"]
    assert outcome["coverage"] == old["coverage"]
    assert outcome["final_snapshot_id"] == old["final_snapshot_id"]
    original, compact = old["candidate_diagnostics"], outcome["candidate_diagnostics"]
    assert len(compact) == len(original) == 15
    for before, after in zip(original, compact, strict=True):
        for key in (
            "result_id",
            "work_id",
            "llm_rank",
            "claim_id",
            "ranking_status",
            "publication_status",
            "evidence_excerpt",
            "reason_evidence_quote",
        ):
            assert after[key] == before[key]
        assert "relationship" not in after
        assert after["citation_check"] == {
            "citable": False,
            "scope_match": "unknown",
            "exclusions": ["abstract_metadata_only"],
        }
        related = after["source_records"]
        assert related[0]["abstract_ref"] == "evidence_excerpt"
        assert related[1]["abstract"] == before["source_records"][1]["abstract"]
        assert related[2]["abstract_ref"] == "source_records[1].abstract"
        assert [record["paper_id"] for record in related] == [
            record["paper_id"] for record in before["source_records"]
        ]
        assert "retrieval_traces" not in after
    assert outcome["workers"] == [{"library_id": "gamma", "worker_state": "failed"}]
    assert len(json.dumps(outcome)) < len(json.dumps(old))


def test_preserves_other_diagnostic_relationships_and_no_candidate_response():
    payload = _payload()
    row = payload["candidate_diagnostics"][0]
    row["relationship"] = "supports"
    row["rejection_reasons"] = ["source_trace_invalid"]
    assert evidence_observation(payload)["candidate_diagnostics"][0]["relationship"] == "supports"
    payload["candidate_diagnostics"] = []
    assert evidence_observation(payload) is payload


@pytest.mark.parametrize("enabled", [True, False])
async def test_agent_tool_projects_only_return_value_not_store_or_public_result(enabled):
    request = SearchCreate(input_type="keywords", keywords=["floral development"])
    settings = Settings(agent_evidence_view=enabled)
    payload = _payload()
    attached = []

    class Service:
        def create(self, tenant, body, *_args, **_kwargs):
            assert (tenant, body.keywords) == ("current-tenant", request.keywords)
            return SearchCreated(
                query_id="q_tool",
                search_state="running",
                claims=[],
                events_url="/events",
                adapter_mode="metadata",
                scope_source="unknown",
                simulation=False,
                notice="test",
            ), False

        async def tool_result(self, tenant, created, **_kwargs):
            assert (tenant, created.query_id) == ("current-tenant", "q_tool")
            return payload

    service = Service()
    service.settings = settings
    tool = LiteratureSearchTool(
        SimpleNamespace(attach_agent_query=lambda *args: attached.append(args)),
        service,
        "current-tenant",
        "agt_tool",
        request,
    ).as_tool()
    seen = await tool.ainvoke(request.model_dump(exclude_none=True))
    assert bool(seen.get("evidence_view_version")) is enabled
    assert (tool.description != TOOL_DESCRIPTION) is enabled
    assert attached[0][:2] == ("current-tenant", "agt_tool")
    assert "relationship" in payload["candidate_diagnostics"][0]


def test_evidence_view_has_environment_rollback(monkeypatch):
    monkeypatch.setenv("CITATION_AGENT_EVIDENCE_VIEW", "0")
    assert settings_from_env().agent_evidence_view is False


def test_abstract_view_changes_only_the_unassessed_direction_and_optional_guidance():
    payload = _payload()
    payload["candidate_diagnostics"][1]["rejection_reasons"] = ["scope_mismatch"]
    payload["candidate_diagnostics"][2]["relationship"] = "support"
    snapshot = deepcopy(payload)
    assert abstract_evidence_observation(payload, "legacy") is payload

    expected = deepcopy(payload)
    for row in expected["candidate_diagnostics"]:
        if (
            row["relationship"] == "insufficient"
            and "abstract_metadata_only" in row["rejection_reasons"]
        ):
            del row["relationship"]
    assert abstract_evidence_observation(payload, "separate") == expected
    assert abstract_evidence_observation(payload, "scoped") == {
        **expected,
        "diagnostic_note": f"old\n{ABSTRACT_EVIDENCE_GUIDANCE}",
    }
    assert payload == snapshot
    no_candidates = {**expected, "candidate_diagnostics": []}
    assert abstract_evidence_observation(no_candidates, "scoped") is no_candidates


@pytest.mark.parametrize("mode", ["separate", "scoped"])
async def test_abstract_view_is_used_only_in_the_parent_tool(mode):
    payload = _payload()
    snapshot = deepcopy(payload)
    request = SearchCreate(input_type="keywords", keywords=["floral development"])

    class Service:
        settings = Settings(agent_abstract_evidence_mode=mode)

        def create(self, *_args, **_kwargs):
            return SearchCreated(
                query_id="q_tool",
                search_state="running",
                claims=[],
                events_url="/events",
                adapter_mode="metadata",
                scope_source="unknown",
                simulation=False,
                notice="test",
            ), False

        async def tool_result(self, *_args, **_kwargs):
            return payload

    tool = LiteratureSearchTool(
        SimpleNamespace(attach_agent_query=lambda *args: None),
        Service(),
        "current-tenant",
        "agt_tool",
        request,
    ).as_tool()
    seen = await tool.ainvoke(request.model_dump(exclude_none=True))
    assert seen == abstract_evidence_observation(snapshot, mode)
    assert payload == snapshot


def test_abstract_evidence_mode_configuration_is_explicit(monkeypatch):
    monkeypatch.setenv("CITATION_AGENT_ABSTRACT_EVIDENCE_MODE", "scoped")
    assert settings_from_env().agent_abstract_evidence_mode == "scoped"
    with pytest.raises(ValueError, match="cannot be combined"):
        Settings(agent_evidence_view=True, agent_abstract_evidence_mode="scoped").validate()
    with pytest.raises(ValueError, match="must be legacy"):
        Settings(agent_abstract_evidence_mode="unknown").validate()
