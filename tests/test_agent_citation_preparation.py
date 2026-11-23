from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace

import httpx
import pytest
from langchain_core.messages import AIMessage

from citation_tool import agent as agent_module
from citation_tool.api import create_app
from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import AgentRun, CitationBatchOutcome, SearchCreate, utc_now_iso
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from test_agent import ScriptedChatModel, wait_agent
from test_discovery import models, record
from test_integration import headers


def new_app(tmp_path, responses, *, demo_continuity=False):
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(library,),
        candidate_pipeline="agentic",
        demo_continuity=demo_continuity,
    )
    return create_app(
        settings,
        adapters={"a": MetadataCorpusAdapter(library, [record(1), record(2)])},
        discovery_models=models(),
        agent_model=ScriptedChatModel(responses=responses),
    )


def search_call(identifier, keywords):
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": identifier,
                "name": "search_literature",
                "args": {"input_type": "keywords", "keywords": keywords},
            }
        ],
    )


def wait_for_preparation(client, base, run_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"{base}/api/v1/agent/runs/{run_id}", headers=headers())
        response.raise_for_status()
        run = response.json()
        if (run.get("citation_preparation") or {}).get("status") == "waiting":
            return run
        assert run["status"] in {"pending", "running"}, run
        time.sleep(0.01)
    raise AssertionError("never observed the final-answer preparation barrier")


def test_turn_keeps_both_boards_available_and_waits_for_both_batches(tmp_path, live_server):
    app = new_app(
        tmp_path,
        [
            search_call("s1", ["LFY2"]),
            search_call("s2", ["soybean"]),
            AIMessage(content="核查结果。"),
        ],
    )
    gates = [threading.Event(), threading.Event()]
    queries = []
    settled = [threading.Event(), threading.Event()]

    def prepare(tenant_id, query_id, model_config):
        assert tenant_id == "local" and model_config is None
        index = len(queries)
        queries.append(query_id)

        async def generate():
            assert await asyncio.to_thread(gates[index].wait, 12)
            settled[index].set()
            return CitationBatchOutcome(ready=1, unavailable=index)

        return asyncio.create_task(generate())

    app.state.citation_texts.schedule_preparation = prepare
    base = live_server(app)
    with httpx.Client(timeout=15) as client:
        try:
            response = client.post(
                f"{base}/api/v1/agent/runs",
                headers=headers(),
                json={"input_type": "keywords", "keywords": ["LFY2"], "prepare_citations": True},
            )
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            waiting = wait_for_preparation(client, base, run_id)
            assert waiting["status"] == "running" and waiting["summary"] is None
            assert len(queries) == 2
            assert set(queries) == {query["created"]["query_id"] for query in waiting["queries"]}
            path = f"{base}/api/v1/conversations/{waiting['conversation_id']}"
            for query_id in queries:
                state = client.get(f"{base}/api/v1/searches/{query_id}", headers=headers()).json()
                assert state["session"]["search_state"] == "completed"
                assert all(item["llm_rank"] for item in state["candidate_diagnostics"])
                assert state["session"]["final_snapshot_id"]
            assert [m["role"] for m in client.get(path, headers=headers()).json()["messages"]] == [
                "user"
            ]
            gates[0].set()
            assert settled[0].wait(2)
            assert (
                client.get(f"{base}/api/v1/agent/runs/{run_id}", headers=headers()).json()[
                    "summary"
                ]
                is None
            )
            gates[1].set()
            result = wait_agent(client, base, run_id)
            assert result["status"] == "completed" and result["summary"] == "核查结果。"
            assert result["citation_preparation"] == {
                "status": "partial",
                "ready": 2,
                "unavailable": 1,
                "unfinished_batches": 0,
            }
            messages = client.get(path, headers=headers()).json()["messages"]
            assert [m["role"] for m in messages] == ["user", "assistant"]
        finally:
            for gate in gates:
                gate.set()


def test_demo_publishes_answer_without_canceling_slow_citation_batch(
    tmp_path, live_server, monkeypatch
):
    app = new_app(
        tmp_path,
        [search_call("slow-citation", ["LFY2"]), AIMessage(content="已查到相关摘要。")],
        demo_continuity=True,
    )
    monkeypatch.setattr(agent_module, "DEMO_CITATION_WAIT_S", 0.05)
    release = threading.Event()
    settled = threading.Event()
    tasks = []

    def prepare(*_):
        async def generate():
            assert await asyncio.to_thread(release.wait, 8)
            settled.set()
            return CitationBatchOutcome(ready=1)

        task = asyncio.create_task(generate())
        tasks.append(task)
        return task

    app.state.citation_texts.schedule_preparation = prepare
    base = live_server(app)
    try:
        with httpx.Client(timeout=8) as client:
            sent = (
                client.post(
                    f"{base}/api/v1/agent/runs",
                    json={
                        "input_type": "keywords",
                        "keywords": ["LFY2"],
                        "prepare_citations": True,
                    },
                )
                .raise_for_status()
                .json()
            )
            result = wait_agent(client, base, sent["run_id"])
            assert result["status"] == "completed" and result["summary"] == "已查到相关摘要。"
            assert result["citation_preparation"] == {
                "status": "partial",
                "ready": 0,
                "unavailable": 0,
                "unfinished_batches": 1,
            }
            assert len(tasks) == 1 and not tasks[0].done()
            release.set()
            assert settled.wait(2)
    finally:
        release.set()


@pytest.mark.parametrize("enable", [False, True])
def test_no_tool_reply_does_not_schedule_or_wait(tmp_path, live_server, enable):
    app = new_app(tmp_path, [AIMessage(content="这个问题不需要重新检索。")])
    app.state.citation_texts.schedule_preparation = lambda *args: pytest.fail("no new query")
    base = live_server(app)
    with httpx.Client(timeout=15) as client:
        response = client.post(
            f"{base}/api/v1/agent/runs",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2"], "prepare_citations": enable},
        )
        response.raise_for_status()
        result = wait_agent(client, base, response.json()["run_id"])
        assert result["status"] == "completed"
        assert result["citation_preparation"] is None and result["queries"] == []


async def test_expired_budget_records_unfinished_without_canceling_batch(tmp_path):
    app = new_app(tmp_path, [])
    runner = app.state.agent_runner
    now = utc_now_iso()
    run = AgentRun(
        run_id="test-run",
        tenant_id="local",
        request=SearchCreate(input_type="keywords", keywords=["abc"]),
        status="running",
        provider="test",
        model="test",
        created_at=now,
        updated_at=now,
    )
    gate = asyncio.Event()
    batch = asyncio.create_task(gate.wait())
    try:
        await runner._wait_for_citations(run, {"q1": batch, "q2": None}, time.monotonic() - 1)
        assert run.citation_preparation.status == "partial"
        assert run.citation_preparation.unfinished_batches == 2
        assert not batch.done()
    finally:
        gate.set()
        await batch
