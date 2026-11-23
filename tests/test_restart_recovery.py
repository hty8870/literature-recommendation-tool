from __future__ import annotations

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.entities import (
    AgentQuery,
    CandidateDiagnostic,
    CandidateRanking,
    CitationPreparationSummary,
    SearchCreated,
    utc_now_iso,
)
from conftest import read_events
from test_agent import ScriptedChatModel, wait_agent
from test_conversations import make_run
from test_integration import search_body, wait_final


def test_restart_settles_searches_and_unlocks_conversation(tmp_path, monkeypatch) -> None:
    settings = Settings.for_tests(tmp_path)
    previous = create_app(settings)
    store = previous.state.store
    with TestClient(previous) as client:
        request = search_body({"fixture-fast": 0, "fixture-slow": 0})
        finished_id = (
            client.post("/api/v1/searches", json=request).raise_for_status().json()["query_id"]
        )
        finished = wait_final(client, "", finished_id)["session"]
        snapshot_path = f"/api/v1/snapshots/{finished['final_snapshot_id']}"
        finished_snapshot = client.get(snapshot_path).raise_for_status().content

        monkeypatch.setattr(previous.state.coordinator, "start", lambda *_: None)
        receipt = client.post("/api/v1/searches", json=request).raise_for_status().json()
        query = SearchCreated.model_validate(receipt)
        query_path = f"/api/v1/searches/{query.query_id}"
        store.append_state_event("local", query.query_id, "running")
        previous_state = client.get(query_path).raise_for_status().json()
        cursor = previous_state["latest_sequence"]
        accepted = client.post("/api/v1/searches", json=request).raise_for_status().json()

        run, _ = store.create_conversation_agent_run(
            make_run(), "orphaned-turn", create_conversation=True
        )
        run.status = "running"
        run.citation_preparation = CitationPreparationSummary(status="waiting")
        store.save_agent_run(run)
        store.attach_agent_query(
            "local",
            run.run_id,
            AgentQuery(created=query, requested_library_ids=["fixture-fast", "fixture-slow"]),
        )

    model = ScriptedChatModel(responses=[AIMessage(content="本轮可以继续。")])
    with TestClient(create_app(settings, agent_model=model)) as client:
        assert client.get(snapshot_path).raise_for_status().content == finished_snapshot
        state = client.get(query_path).raise_for_status().json()
        assert state["session"]["search_state"] == "failed"
        assert state["session"]["final_snapshot_id"] is None
        assert set(state["coverage"]["failed_library_ids"]) == {"fixture-fast", "fixture-slow"}
        assert all(
            worker["last_error"]["code"] == "SEARCH_INTERRUPTED" for worker in state["workers"]
        )
        assert (
            client.get(f"/api/v1/searches/{accepted['query_id']}").json()["session"]["search_state"]
            == "failed"
        )
        events = read_events(client, f"{query_path}/events", headers={"Last-Event-ID": str(cursor)})
        assert [item["id"] for item in events] == [cursor + 1, cursor + 2, cursor + 3]
        assert [item["event"] for item in events] == [
            "worker_failed",
            "worker_failed",
            "search_state_changed",
        ]
        assert events[-1]["data"]["payload"]["error"]["code"] == "SEARCH_INTERRUPTED"
        recovered_through = events[-1]["id"]

        conversation_path = f"/api/v1/conversations/{run.conversation_id}"
        conversation = client.get(conversation_path).raise_for_status().json()
        assert conversation["active_run_id"] is None
        assert [item["role"] for item in conversation["messages"]] == ["user"]
        assert conversation["artifacts"][0]["query_id"] == query.query_id
        recovered = conversation["runs"][0]
        assert recovered["status"] == "failed"
        assert recovered["error"]["code"] == "AGENT_INTERRUPTED"
        assert recovered["citation_preparation"]["status"] == "partial"
        assert recovered["conversation_revision"] == conversation["revision"]
        next_run = (
            client.post(
                conversation_path + "/turns",
                json={"message": "继续", "expected_revision": conversation["revision"]},
            )
            .raise_for_status()
            .json()
        )
        assert wait_agent(client, "", next_run["run_id"])["status"] == "completed"

    with TestClient(create_app(settings)) as client:
        assert client.get(query_path).json()["latest_sequence"] == recovered_through
        assert client.get(snapshot_path).raise_for_status().content == finished_snapshot


def test_restart_finishes_only_the_uncommitted_ranking(tmp_path, monkeypatch) -> None:
    settings = Settings.for_tests(tmp_path)
    previous = create_app(settings)
    store = previous.state.store
    monkeypatch.setattr(previous.state.coordinator, "start", lambda *_: None)
    with TestClient(previous) as client:
        receipt = (
            client.post(
                "/api/v1/searches", json=search_body({"fixture-fast": 0, "fixture-slow": 0})
            )
            .raise_for_status()
            .json()
        )
        query_id = receipt["query_id"]
        claim_id = receipt["claims"][0]["claim_id"]
        store.append_state_event("local", query_id, "running")
        candidate = CandidateDiagnostic(
            result_id="candidate-one",
            work_id="work:interrupted",
            paper_id="paper:interrupted",
            claim_id=claim_id,
            library_ids=["fixture-fast"],
            relationship="insufficient",
            scope_match="unknown",
            rejection_reasons=["abstract_metadata_only"],
            evidence_excerpt="Saved source text.",
            candidate_score=40,
            base_rank=1,
            pool_version=1,
            ranking_status="pending",
            quality_note="test",
        )
        ranking = CandidateRanking(
            claim_id=claim_id,
            pool_version=1,
            pool_hash="pool",
            ranking_version=0,
            status="pending",
            created_at=utc_now_iso(),
        ).model_dump(mode="json")
        excluded = candidate.model_copy(
            update={
                "result_id": "ineligible",
                "work_id": "work:ineligible",
                "ranking_status": "ineligible",
                "base_rank": None,
            }
        )
        assert store.commit_discovery_pool("local", query_id, ranking, [candidate, excluded])
        assert store.commit_ranking_phase("local", query_id, claim_id, 1, "blind")

    with TestClient(create_app(settings)) as client:
        path = f"/api/v1/searches/{query_id}"
        result = client.get(path).raise_for_status().json()
        ranking = result["candidate_rankings"][0]
        assert ranking["pool_hash"] == "pool" and ranking["pool_version"] == 1
        assert ranking["status"] == "failed" and ranking["phase"] is None
        assert ranking["failure_code"] == "SEARCH_INTERRUPTED"
        candidates = {item["result_id"]: item for item in result["candidate_diagnostics"]}
        delivered = candidates[candidate.result_id]
        assert delivered["evidence_excerpt"] == candidate.evidence_excerpt
        assert delivered["base_rank"] == 1 and delivered["llm_rank"] is None
        assert delivered["reason"] is None and delivered["ranking_status"] == "failed"
        assert candidates[excluded.result_id] == excluded.model_dump(mode="json")
        history = client.get(path + "/candidate-rankings").raise_for_status().json()["history"]
        assert len(history) == 1 and history[0]["ranking"] == ranking
        assert history[0]["candidates"] == [delivered, candidates[excluded.result_id]]
