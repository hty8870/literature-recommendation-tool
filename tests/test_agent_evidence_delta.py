from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from citation_tool.agent_observations import ParentEvidenceHistory
from citation_tool.api import create_app
from citation_tool.config import LibraryConfig, Settings
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.runtime import settings_from_env
from test_agent import ScriptedChatModel
from test_discovery import models, record


def candidate(work: str, text: str, *, library: str = "a", status: str = "none") -> dict:
    return {
        "work_id": work,
        "claim_id": f"claim-{library}",
        "llm_rank": 1,
        "evidence_excerpt": text,
        "rejection_reasons": ["abstract_metadata_only"],
        "reason": {"text": "Generated comment."},
        "source_records": [
            {
                "library_id": library,
                "paper_id": f"{library}:{work}",
                "source_uri": f"https://example.org/{library}",
                "title": "A paper",
                "abstract": text,
                "publication_status": status,
                "doi": "10.1234/study",
                "year": 2022,
                "publication_types": ["journal-article"],
                "related_dois": {},
            }
        ],
    }


def receipt(*items: dict, library: str = "a", completed: bool = True) -> dict:
    return {
        "results": [],
        "simulation": False,
        "candidate_diagnostics": list(items),
        "coverage": {"completed_library_ids": [library] if completed else []},
    }


def test_metadata_delta_distinguishes_duplicate_from_new_abstract_retraction_and_version():
    history = ParentEvidenceHistory()
    first = receipt(candidate("work:one", "Original\ntext."))
    assert history.observe(first) is None
    other_copy = candidate("work:one", "Original\ntext.", library="b")
    other_copy["reason"] = {"text": "A different model comment."}
    other_copy["llm_rank"] = 7
    other_copy["pool_version"] = 3
    second = receipt(other_copy, library="b")
    initial = deepcopy(second)
    delta = history.observe(second)
    assert delta["status"] == "no_unseen_evidence"
    assert delta["newly_completed_library_ids"] == ["b"]
    assert delta["unchanged_work_count"] == 1
    assert delta["new_work_count"] == delta["changed_work_count"] == 0
    assert initial == second

    updated = candidate("work:one", "Original\ntext.", status="retracted")
    updated["source_records"] += candidate("work:one", "New abstract.")["source_records"]
    version = candidate("work:two", "Original\ntext.")
    third = history.observe(receipt(updated, version))
    assert third["new_work_count"] == third["changed_work_count"] == 1
    assert third["new_work_ids"] == ["work:two"]
    assert third["changed_work_ids"] == ["work:one"]
    assert third["unchanged_work_count"] == 0
    assert history.observe(receipt(updated))["status"] == "no_unseen_evidence"


def test_delta_distinguishes_no_results_no_coverage_and_nonabstract_receipts():
    h = ParentEvidenceHistory()
    item = candidate("work:one", "A distinct abstract.")
    assert h.observe({**receipt(item), "simulation": True}) is None
    assert h.observe({**receipt(item), "results": [{"citable": True}]}) is None
    assert h.observe(receipt(completed=False)) is None
    empty = h.observe(receipt(completed=False))
    assert empty["status"] == "no_candidates_returned"
    assert empty["newly_completed_library_ids"] == []
    assert "note" not in empty
    assert h.observe(receipt(item))["new_work_ids"] == ["work:one"]
    assert ParentEvidenceHistory().observe(receipt(item)) is None


def test_receipt_id_sample_is_capped_without_undercounting():
    history = ParentEvidenceHistory()
    history.observe(receipt(completed=False))
    entries = [candidate(f"work:{n}", "Exact original.") for n in range(25)]
    observed = history.observe(receipt(*entries))
    assert observed["new_work_count"] == 25
    assert observed["new_work_ids"] == [entry["work_id"] for entry in entries[:10]]


def test_delta_is_opt_in(monkeypatch):
    monkeypatch.delenv("CITATION_AGENT_PARENT_EVIDENCE_DELTA", raising=False)
    assert not settings_from_env().agent_parent_evidence_delta
    monkeypatch.setenv("CITATION_AGENT_PARENT_EVIDENCE_DELTA", "true")
    assert settings_from_env().agent_parent_evidence_delta


@pytest.mark.parametrize("enabled", [False, True])
async def test_real_search_tool_receipts_keep_all_queries_separate(enabled, tmp_path):
    library = LibraryConfig("papers", "Papers", "", 0, simulation=False)
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(library,),
        candidate_pipeline="agentic",
        agent_parent_evidence_delta=enabled,
        agent_evidence_view=True,
    )
    body = {"input_type": "keywords", "keywords": ["LFY2"], "library_ids": ["papers"]}
    papers = [record(1)]
    parent = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "search_literature", "id": "search-1", "args": body}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_literature",
                        "id": "search-2",
                        "args": {**body, "keywords": ["LFY2", "soybean"]},
                    }
                ],
            ),
            AIMessage(content="两次检索找到同一篇摘要；它不能替代全文核验。"),
        ]
    )
    app = create_app(
        settings,
        adapters={"papers": MetadataCorpusAdapter(library, papers)},
        agent_model=parent,
        discovery_models=models(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post("/api/v1/agent/runs", json=body)
        assert response.status_code == 202, response.text
        url = f"/api/v1/agent/runs/{response.json()['run_id']}"
        async with asyncio.timeout(8):
            while True:
                run = (await client.get(url)).json()
                if run["status"] in {"completed", "failed"}:
                    assert run["status"] == "completed", run["error"]
                    break
                await asyncio.sleep(0.01)
        observed = [
            json.loads(m.text) for m in parent.received_messages[-1] if isinstance(m, ToolMessage)
        ]
        assert len(observed) == 2
        assert "evidence_delta" not in observed[0]
        assert ("evidence_delta" in observed[1]) is enabled
        if enabled:
            assert observed[1]["evidence_delta"]["status"] == "no_unseen_evidence"
        ids = [r["query_id"] for r in observed]
        assert len(set(ids)) == 2
        for item in observed:
            assert item["candidate_diagnostics"][0]["evidence_excerpt"] == papers[0]["abstract"]
            assert item["results"] == []
            state = (await client.get(f"/api/v1/searches/{item['query_id']}")).json()
            assert "evidence_delta" not in state
            assert state["session"]["final_snapshot_id"] == item["final_snapshot_id"]
            assert state["candidate_diagnostics"][0]["llm_rank"] == 1
