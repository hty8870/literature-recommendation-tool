from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import openai
import pytest
from pydantic import Field

from citation_tool.api import create_app
from citation_tool.config import LOCAL_ONLY_ADAPTER_MODE, Settings
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.online import EuropePMCOnlineAdapter
from test_agent import ScriptedChatModel, wait_agent
from test_agent_citation_preparation import search_call
from test_agent_source_ready import HeldRanking
from test_discovery import models, record
from test_integration import headers, wait_final
from test_online_sources import online_library, snapshot_library, snapshot_record


class FailedProvider(ScriptedChatModel):
    failure: str = Field(default="503")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.responses:
            return super()._generate(messages, stop, run_manager, **kwargs)
        self.received_messages.append(messages)
        request = httpx.Request("POST", "https://example.invalid/model")
        if self.failure == "connection":
            raise openai.APIConnectionError(request=request)
        if self.failure == "timeout":
            raise openai.APITimeoutError(request=request)
        if self.failure == "internal":
            raise RuntimeError("not a provider failure")
        response = httpx.Response(int(self.failure), request=request)
        raise openai.APIStatusError("provider unavailable", response=response, body=None)


def _app(tmp_path, agent_model, *, enabled=True, discovery=None, source_first=False):
    online = online_library()
    local = snapshot_library()
    sample = snapshot_library("abiotech-sample", "Teacher")
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(online, local, sample),
        library_mode="metadata",
        candidate_pipeline="agentic",
        demo_continuity=enabled,
        agent_source_evidence_first=source_first,
    )

    def not_online(*_):
        pytest.fail("must not call the online database on this path")

    return create_app(
        settings,
        adapters={
            online.library_id: EuropePMCOnlineAdapter(online, transport=not_online),
            local.library_id: MetadataCorpusAdapter(local, [snapshot_record()]),
            sample.library_id: MetadataCorpusAdapter(sample, [record(1), record(2)]),
        },
        agent_model=agent_model,
        discovery_models=discovery,
        citation_model=ScriptedChatModel(),
        citation_review_model=ScriptedChatModel(),
    )


@pytest.mark.parametrize(
    "failure,code",
    [
        ("503", "MODEL_UPSTREAM_ERROR"),
        ("402", "MODEL_INSUFFICIENT_BALANCE"),
        ("401", "MODEL_AUTHENTICATION_FAILED"),
        ("429", "MODEL_RATE_LIMITED"),
        ("connection", "MODEL_CONNECTION_ERROR"),
        ("timeout", "MODEL_TIMEOUT"),
        ("manual", "LOCAL_SEARCH_ONLY"),
        ("no-model", "LOCAL_SEARCH_ONLY"),
    ],
)
def test_local_delivery_stays_inside_selection_without_model_or_online_calls(
    tmp_path, live_server, failure, code
):
    model = None if failure == "no-model" else FailedProvider(failure=failure)
    manual = failure in {"manual", "no-model"}
    app = _app(tmp_path, model)
    app.state.citation_texts.schedule_preparation = lambda *_: pytest.fail("no citation generation")
    base = live_server(app)
    body = {
        "input_type": "keywords",
        "keywords": ["LFY2", "soybean"],
        "library_ids": ["europepmc-online", "europepmc-snapshot"],
        **({"local_only": True} if manual else {"prepare_citations": True}),
    }
    with httpx.Client(timeout=8) as client:
        ready = client.get(f"{base}/api/v1/libraries").raise_for_status().json()
        assert ready["demo_local_search_available"]
        assert ready["agent_available"] is (model is not None)
        sent = client.post(f"{base}/api/v1/agent/runs", json=body, headers=headers(key="once"))
        assert sent.status_code == 202, sent.text
        run = wait_agent(client, base, sent.json()["run_id"])
        assert run["status"] == "failed" and run["error"]["code"] == code
        assert not run["summary"] and not run["tool_calls"] and len(run["queries"]) == 1
        assert run["request"]["library_ids"] == body["library_ids"]
        query = run["latest_query_id"]
        state = wait_final(client, base, query)
        assert state["adapter_mode"] == LOCAL_ONLY_ADAPTER_MODE
        assert state["results"] == []
        assert [
            (w["library_id"], w["worker_state"], w["source_agent_status"]) for w in state["workers"]
        ] == [("europepmc-snapshot", "completed", "disabled")]
        [paper] = state["candidate_diagnostics"]
        assert paper["doi"] == "10.1007/s42994-024-00192-2"
        assert paper["library_ids"] == ["europepmc-snapshot"]
        assert paper["llm_rank"] is None and (paper["reason"] or {}).get("text") is None
        assert "abstract_metadata_only" in paper["rejection_reasons"]
        params = {
            "result_id": paper["result_id"],
            "result_version": paper["pool_version"],
            "result_kind": "candidate",
        }
        for action in ("citation-formats", "citation-suggestion"):
            reply = client.get(f"{base}/api/v1/searches/{query}/{action}", params=params)
            assert reply.status_code == 422 and reply.json()["code"] == "CITATION_LOCAL_SEARCH_ONLY"
        reply = client.post(
            f"{base}/api/v1/citation-texts",
            json={"query_id": query, **params, "citation_style": "vancouver", "language": "zh"},
        )
        assert reply.status_code == 422 and reply.json()["code"] == "CITATION_LOCAL_SEARCH_ONLY"
        replay = client.post(f"{base}/api/v1/agent/runs", json=body, headers=headers(key="once"))
        assert replay.status_code == 202 and replay.json()["latest_query_id"] == query
        conversation = client.get(f"{base}/api/v1/conversations/{run['conversation_id']}").json()
        assert [message["role"] for message in conversation["messages"]] == ["user"]
        assert len(conversation["artifacts"]) == 1
        if model:
            assert len(model.received_messages) == (0 if manual else 1)


@pytest.mark.parametrize("case", ["disabled", "online-only", "internal"])
def test_no_silent_expansion_or_fallback_on_internal_errors(tmp_path, live_server, case):
    parent = FailedProvider(failure="internal" if case == "internal" else "503")
    app = _app(tmp_path, parent, enabled=case != "disabled")
    base = live_server(app)
    body = {
        "input_type": "keywords",
        "keywords": ["LFY2"],
        "library_ids": ["europepmc-online" if case == "online-only" else "europepmc-snapshot"],
    }
    with httpx.Client(timeout=8) as client:
        if case != "internal":
            rejected = client.post(f"{base}/api/v1/agent/runs", json={**body, "local_only": True})
            assert rejected.status_code == 400
            assert rejected.json()["code"] == "LOCAL_SEARCH_UNAVAILABLE"
        sent = client.post(f"{base}/api/v1/agent/runs", json=body).raise_for_status().json()
        run = wait_agent(client, base, sent["run_id"])
        assert run["status"] == "failed" and run["queries"] == []
        assert run["error"]["code"] == (
            "AGENT_MODEL_ERROR" if case == "internal" else "MODEL_UPSTREAM_ERROR"
        )


async def test_provider_failure_does_not_cancel_already_accepted_ranking(tmp_path):
    rank = HeldRanking(role="ranking")
    initial = search_call("local-source", ["LFY2"])
    initial.tool_calls[0]["args"]["library_ids"] = ["abiotech-sample"]
    parent = FailedProvider(responses=[initial])
    app = _app(tmp_path, parent, discovery=models(ranking=rank), source_first=True)
    coordinator = app.state.search_service.coordinator
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        sent = await client.post(
            "/api/v1/agent/runs",
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
                "library_ids": ["abiotech-sample"],
            },
        )
        run_id = sent.raise_for_status().json()["run_id"]
        try:
            await asyncio.wait_for(rank.started.wait(), timeout=5)
            await asyncio.wait_for(app.state.agent_runner.wait_for_exit(run_id), timeout=5)
            run = (await client.get(f"/api/v1/agent/runs/{run_id}")).json()
            assert run["status"] == "failed" and len(run["queries"]) == 1
            query = run["latest_query_id"]
            state = (await client.get(f"/api/v1/searches/{query}")).json()
            assert state["is_live"] and len(state["candidate_diagnostics"]) == 2
            assert state["adapter_mode"] != LOCAL_ONLY_ADAPTER_MODE
        finally:
            rank.release.set()
        await coordinator.wait(query, timeout_s=5)
        final = (await client.get(f"/api/v1/searches/{query}")).json()
        assert final["session"]["search_state"] == "completed"
        assert all(
            item["reason"]["status"] == "generated" for item in final["candidate_diagnostics"]
        )
