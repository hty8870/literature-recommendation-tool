from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from langchain_core.messages import AIMessage
from tools import luna_product_retest as benchmark

from citation_tool.api import create_app
from citation_tool.config import Settings
from test_agent import ScriptedChatModel, tool_call


@pytest.mark.parametrize("stall", [None, "submission", "poll"])
async def test_benchmark_timeout_cancels_and_waits_for_case_queries(tmp_path, monkeypatch, stall):
    real_transport = httpx.ASGITransport

    class DelayedResponse(real_transport):
        async def handle_async_request(self, request):
            if stall == "poll" and request.method == "GET":
                await asyncio.Event().wait()
            response = await super().handle_async_request(request)
            if stall == "submission" and request.url.path == "/api/v1/agent/runs":
                try:
                    await asyncio.Event().wait()
                finally:
                    await response.aclose()
            return response

    monkeypatch.setattr(benchmark.httpx, "ASGITransport", DelayedResponse)
    monkeypatch.setattr(benchmark, "AGENT_TIMEOUT_MS", 750)
    monkeypatch.setattr(benchmark, "AGENT_POLL_GRACE_S", 0)
    monkeypatch.setattr(benchmark, "product_settings", lambda path, _: Settings.for_tests(path))
    monkeypatch.setattr(benchmark, "build_product_adapter", lambda *_: None)
    search = tool_call("benchmark-search", ["LFY2", "soybean"], ["fixture-fast", "fixture-slow"])
    search.tool_calls[0]["args"]["options"]["library_delays_ms"] = {
        "fixture-fast": 0,
        "fixture-slow": 1500,
    }
    model = ScriptedChatModel(responses=[search, AIMessage(content="完成。")])
    app = None

    def build(settings, *_):
        nonlocal app
        app = create_app(settings, agent_model=model)
        return app, None, None

    runtime = tmp_path / "attempt"
    runtime.mkdir()
    task = {
        "case_id": "fixture-timeout",
        "run_case_id": "fixture-timeout",
        "repetition": 1,
        "slice": "fixture",
        "family": "fixture",
        "request": {"input_type": "keywords", "keywords": ["LFY2", "soybean"]},
        "baseline_query": "LFY2 soybean",
    }
    async with asyncio.timeout(5):
        record = await benchmark.run_case(
            task,
            root=tmp_path,
            runtime=runtime,
            corpus={},
            index=None,
            implementation_hash="fixture",
            shared_async=asyncio.Semaphore(2),
            shared_sync=threading.BoundedSemaphore(2),
            gateway_override=object(),
            app_builder=build,
        )
    assert record["completed"] is False
    assert record["error"]["code"] == "BENCHMARK_TIMEOUT"
    assert "cleanup_error" not in record
    assert record["missing_query_states"] == []
    assert len(record["queries"]) == 1
    query = record["queries"][0]
    assert query["search_state"] == "cancelled"
    assert query["final_snapshot_id"]
    assert not app.state.agent_runner.has_live_runs([record["agent_run_id"]])
    assert not app.state.coordinator.has_live_searches([query["query_id"]])
    app.state.coordinator.request_cancel(query["query_id"])
    assert query["query_id"] not in app.state.coordinator._cancel_requested
