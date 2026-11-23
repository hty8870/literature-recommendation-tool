from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import Field

from citation_tool.agent_observations import hide_rank_rationale
from citation_tool.api import create_app
from citation_tool.config import LibraryConfig, Settings
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from test_agent import ScriptedChatModel
from test_discovery import DiscoveryChatModel, models, record


class HeldRanking(DiscoveryChatModel):
    started: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)
    release: asyncio.Event = Field(default_factory=asyncio.Event, exclude=True)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.started.set()
        await self.release.wait()
        return self._generate(messages, stop=stop, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_first,prepare_citations", [(True, False), (False, False), (True, True)]
)
async def test_parent_sources_ready_keeps_real_rank_pipeline(
    tmp_path, source_first, prepare_citations
):
    library = LibraryConfig("study", "Study", "", 0, simulation=False)
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(library,),
        candidate_pipeline="agentic",
        agent_source_evidence_first=source_first,
    )
    rank = HeldRanking(role="ranking")
    items = [record(1), record(2)]
    discovery = replace(models(ranking=rank), timeout_s=10)
    body = {"input_type": "keywords", "keywords": ["LFY2"], "library_ids": ["study"]}
    parent = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_literature", "id": "research", "args": body},
                ],
            ),
            AIMessage(content="这些摘要提供相关方向，仍需核查全文。"),
        ]
    )
    app = create_app(
        settings,
        adapters={"study": MetadataCorpusAdapter(library, items)},
        agent_model=parent,
        discovery_models=discovery,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        request = await client.post(
            "/api/v1/agent/runs", json={**body, "prepare_citations": prepare_citations}
        )
        assert request.status_code == 202, request.text
        run_id = request.json()["run_id"]
        run_path = f"/api/v1/agent/runs/{run_id}"

        async def completed() -> dict:
            async with asyncio.timeout(7):
                while True:
                    run = (await client.get(run_path)).json()
                    if run["status"] in {"completed", "failed"}:
                        assert run["status"] == "completed", run["error"]
                        return run
                    await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(rank.started.wait(), timeout=7)
            if source_first and not prepare_citations:
                run = await completed()
                state = (await client.get(f"/api/v1/searches/{run['latest_query_id']}")).json()
                assert state["is_live"]
                assert not state["session"]["final_snapshot_id"]
                assert all(row["llm_rank"] is None for row in state["candidate_diagnostics"])
                tool_receipt = next(
                    json.loads(msg.text)
                    for msg in parent.received_messages[-1]
                    if isinstance(msg, ToolMessage)
                )
                assert tool_receipt["delivery_stage"] == "sources_ready"
                assert not tool_receipt["complete"]
                assert tool_receipt["results"] == []
                assert {
                    row["evidence_excerpt"] for row in tool_receipt["candidate_diagnostics"]
                } == {item["abstract"] for item in items}
                assert all(
                    "abstract_metadata_only" in row["rejection_reasons"]
                    for row in tool_receipt["candidate_diagnostics"]
                )
            else:
                assert (await client.get(run_path)).json()["status"] == "running"
                assert len(parent.received_messages) == 1
        finally:
            rank.release.set()
        run = await completed()
        query = run["latest_query_id"]
        await app.state.search_service.coordinator.wait(query, timeout_s=7)
        state = (await client.get(f"/api/v1/searches/{query}")).json()
        assert state["session"]["search_state"] == "completed"
        assert state["session"]["final_snapshot_id"]
        assert sorted(c["llm_rank"] for c in state["candidate_diagnostics"]) == [1, 2]
        assert all(c["reason"]["status"] == "generated" for c in state["candidate_diagnostics"])
        assert state["results"] == []


@pytest.mark.parametrize("use_refs", [False, True])
async def test_abstract_references_only_change_parent_context(tmp_path, use_refs):
    library = LibraryConfig("study", "Study", "", 0, simulation=False)
    paper = record(1)
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(library,),
        candidate_pipeline="agentic",
        agent_parent_abstract_references=use_refs,
        agent_evidence_view=True,
    )
    body = {"input_type": "keywords", "keywords": ["LFY2"], "library_ids": ["study"]}
    parent = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "search_literature", "id": call, "args": body}],
            )
            for call in ("first", "other")
        ]
        + [AIMessage(content="摘要仅报告相关性。")]
    )
    app = create_app(
        settings,
        adapters={"study": MetadataCorpusAdapter(library, [paper])},
        agent_model=parent,
        discovery_models=models(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        sent = await client.post("/api/v1/agent/runs", json=body)
        assert sent.status_code == 202, sent.text
        run_id = sent.json()["run_id"]
        async with asyncio.timeout(7):
            while True:
                run = (await client.get(f"/api/v1/agent/runs/{run_id}")).json()
                if run["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
        assert run["status"] == "completed", run["error"]
        first, other = [
            json.loads(msg.text)
            for msg in parent.received_messages[-1]
            if isinstance(msg, ToolMessage)
        ]
        one = first["candidate_diagnostics"][0]
        two = other["candidate_diagnostics"][0]
        assert first["query_id"] != other["query_id"]
        assert one["evidence_excerpt"] == paper["abstract"]
        if use_refs:
            ref = {
                "query_id": first["query_id"],
                "work_id": one["work_id"],
                "field": "evidence_excerpt",
            }
            assert "evidence_excerpt" not in two and two["abstract_ref"] == ref
            assert two["source_records"][0]["abstract_ref"] == ref
        else:
            assert two["evidence_excerpt"] == paper["abstract"] and "abstract_ref" not in two
        assert not two["citation_check"]["citable"] and two["llm_rank"] == 1
        for view in (first, other):
            state = (await client.get(f"/api/v1/searches/{view['query_id']}")).json()
            candidate = state["candidate_diagnostics"][0]
            assert candidate["evidence_excerpt"] == paper["abstract"]
            assert candidate["source_records"][0]["abstract"] == paper["abstract"]
            assert "abstract_ref" not in candidate and state["session"]["final_snapshot_id"]


def test_source_first_environment_switch_is_opt_in(monkeypatch):
    from citation_tool.runtime import settings_from_env

    monkeypatch.delenv("CITATION_AGENT_SOURCE_EVIDENCE_FIRST", raising=False)
    assert not settings_from_env().agent_source_evidence_first
    monkeypatch.setenv("CITATION_AGENT_SOURCE_EVIDENCE_FIRST", "true")
    assert settings_from_env().agent_source_evidence_first


def test_parent_only_rationale_projection_leaves_original_evidence_and_rank():
    candidate = {
        "work_id": "work:fixture",
        "llm_rank": 2,
        "evidence_excerpt": "Exact abstract text.",
        "reason_evidence_quote": "Exact abstract text.",
        "relationship": "insufficient",
        "reason": {"status": "generated", "text": "Generated ranking explanation."},
    }
    receipt = {
        "results": [{"reason": {"text": "a non-candidate reason"}}],
        "candidate_diagnostics": [candidate],
    }
    projected = hide_rank_rationale(receipt)
    assert projected["candidate_diagnostics"] == [{**candidate, "reason": {"status": "generated"}}]
    assert projected["results"] == receipt["results"]
    assert candidate["reason"]["text"] == "Generated ranking explanation."


def test_parent_rationale_switch_is_independent(monkeypatch):
    from citation_tool.runtime import settings_from_env

    monkeypatch.delenv("CITATION_AGENT_PARENT_HIDE_RANK_RATIONALE", raising=False)
    monkeypatch.delenv("CITATION_AGENT_SOURCE_EVIDENCE_FIRST", raising=False)
    assert not settings_from_env().agent_parent_hide_rank_rationale
    monkeypatch.setenv("CITATION_AGENT_PARENT_HIDE_RANK_RATIONALE", "true")
    cfg = settings_from_env()
    assert cfg.agent_parent_hide_rank_rationale and not cfg.agent_source_evidence_first
