from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
from langchain_core.messages import AIMessage

from citation_tool.api import create_app
from citation_tool.config import Settings
from test_agent import ScriptedChatModel, StubReasonGenerator
from test_manuscripts import _digital_pdf_bytes


async def finished(client, run_id, headers):
    for _ in range(1000):
        response = await client.get(f"/api/v1/agent/runs/{run_id}", headers=headers)
        response.raise_for_status()
        record = response.json()
        if record["status"] in {"completed", "failed", "cancelled"}:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("HTTP agent run did not finish")


async def test_real_conversation_routes_preserve_history_artifacts_and_idempotency(tmp_path):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "search-first",
                        "name": "search_literature",
                        "args": {
                            "input_type": "keywords",
                            "keywords": ["LFY2"],
                            "library_ids": ["fixture-fast"],
                        },
                    }
                ],
            ),
            AIMessage(content="已找到相关候选，建议先核对摘要。"),
            AIMessage(content="第一篇属于已展示的候选；本轮只解释限制，不重新检索。"),
        ]
    )
    app = create_app(
        Settings.for_tests(tmp_path), agent_model=model, reason_generator=StubReasonGenerator()
    )
    owner = {"X-Tenant-ID": "owner"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.local"
    ) as client:
        created = await client.post(
            "/api/v1/agent/runs",
            headers=owner,
            json={"input_type": "keywords", "keywords": ["LFY2"], "library_ids": ["fixture-fast"]},
        )
        assert created.status_code == 202, created.text
        first = await finished(client, created.json()["run_id"], owner)
        assert first["status"] == "completed", first
        conversation_id = first["conversation_id"]
        query_id = first["latest_query_id"]
        initial_search = (await client.get(f"/api/v1/searches/{query_id}", headers=owner)).json()
        body = {"message": "解释第一篇，不必重新检索。", "active_query_id": query_id}
        turn_headers = {**owner, "Idempotency-Key": "explain-once"}
        second = await client.post(
            f"/api/v1/conversations/{conversation_id}/turns", json=body, headers=turn_headers
        )
        assert second.status_code == 202, second.text
        result = await finished(client, second.json()["run_id"], owner)
        assert result["status"] == "completed", result
        assert result["queries"] == []
        replay = await client.post(
            f"/api/v1/conversations/{conversation_id}/turns", json=body, headers=turn_headers
        )
        assert replay.json()["run_id"] == result["run_id"]
        restored = (
            await client.get(f"/api/v1/conversations/{conversation_id}", headers=owner)
        ).json()
        assert len(restored["messages"]) == 4
        assert len(restored["runs"]) == 2
        assert restored["artifacts"][0]["query_id"] == query_id
        assert (
            await client.get(f"/api/v1/searches/{query_id}", headers=owner)
        ).json() == initial_search
        foreign = await client.get(
            f"/api/v1/conversations/{conversation_id}", headers={"X-Tenant-ID": "other"}
        )
        assert foreign.status_code == 404
        invalid = await client.post(
            f"/api/v1/conversations/{conversation_id}/turns",
            headers=owner,
            json={"message": "解释", "active_query_id": "q_not-owned-by-conversation"},
        )
        assert invalid.status_code == 409
        assert invalid.json()["code"] == "CONVERSATION_ACTIVE_QUERY_INVALID"
    await app.state.citation_texts.aclose()
    app.state.manuscripts.close()


async def test_upload_capability_can_be_disabled_without_fake_success(tmp_path):
    settings = replace(Settings.for_tests(tmp_path), manuscript_upload_enabled=False)
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.local"
    ) as client:
        assert (await client.get("/api/v1/libraries")).json()["manuscript_upload_enabled"] is False
        capability = await client.get("/api/v1/manuscripts/capability")
        assert capability.status_code == 200
        assert capability.json()["manuscript_upload_enabled"] is False
        assert capability.json()["max_upload_bytes"] == settings.manuscript_max_upload_bytes
        response = await client.post(
            "/api/v1/manuscripts",
            content=_digital_pdf_bytes(),
            headers={"Content-Type": "application/pdf", "X-Filename": "public-fixture.pdf"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "MANUSCRIPT_UPLOAD_DISABLED"
    await app.state.citation_texts.aclose()
    app.state.manuscripts.close()


async def test_citation_http_does_not_silently_use_default_when_user_models_disabled(tmp_path):
    app = create_app(replace(Settings.for_tests(tmp_path), allow_user_models=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.local"
    ) as client:
        response = await client.post(
            "/api/v1/citation-texts",
            json={
                "query_id": "q_1",
                "result_id": "res_1",
                "result_version": 1,
                "citation_style": "vancouver",
                "language": "zh",
                "model": {"provider": "openai", "model": "test", "api_key": "unit-test-value"},
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "USER_MODEL_DISABLED"
        foreign = await client.get("/api/v1/citation-texts/ct_unknown")
        assert foreign.status_code == 404
    await app.state.citation_texts.aclose()
    app.state.manuscripts.close()
