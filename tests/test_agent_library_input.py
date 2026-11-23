from __future__ import annotations

import httpx
import pytest

from citation_tool.api import create_app
from citation_tool.config import Settings
from test_agent import ScriptedChatModel


@pytest.mark.parametrize("libraries", [[], ["not-configured"], ["fixture-fast", "fixture-fast"]])
async def test_invalid_agent_libraries_fail_before_model_call(tmp_path, libraries):
    model = ScriptedChatModel()
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test.local"
    ) as client:
        response = await client.post(
            "/api/v1/agent/runs",
            json={"input_type": "keywords", "keywords": ["LFY2"], "library_ids": libraries},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert model.received_messages == []
