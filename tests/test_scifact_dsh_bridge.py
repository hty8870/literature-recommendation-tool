import os
import shutil
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from tools.scifact_dsh_bridge import BridgeError, DshParent


class FixtureModel:
    calls = 0

    def bind_tools(self, tools):
        assert [t.name for t in tools] == ["read_virtual"]
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        metadata = {"model_name": "gpt-6-luna", "status": "completed"}
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_virtual", "args": {"path": "public.txt"}, "id": "call_fixture"}
                ],
                response_metadata=metadata,
            )
        if self.calls == 3:
            assert messages[-2].content == "public fixture output"
            assert isinstance(messages[-1], HumanMessage)
            assert messages[-1].content == "Please correct your answer."
            return AIMessage(content="corrected fixture output", response_metadata=metadata)
        assert self.calls == 2 and isinstance(messages[-1], ToolMessage)
        assert messages[-1].content == '{"marker": "public fixture output"}'
        return AIMessage(content="public fixture output", response_metadata=metadata)


def fixture():
    async def read_marker(path: str) -> dict:
        assert path == "public.txt"
        return {"marker": "public fixture output"}

    tool = StructuredTool.from_function(
        coroutine=read_marker, name="read_virtual", description="Read the public test input."
    )
    model = FixtureModel()
    parent = DshParent(
        model,
        [tool],
        "Only the supplied tool is available.",
        "Read public.txt and return its marker.",
        max_model_calls=3,
    )
    return model, parent


async def test_changed_dsh_model_input_is_rejected_before_provider_dispatch():
    model, parent = fixture()
    with pytest.raises(BridgeError, match="MODEL_HISTORY_DIFFERS"):
        await parent.model_request(
            {
                "system": parent.system,
                "model": "gpt-6-luna",
                "tools": parent.schemas,
                "messages": [],
            }
        )
    assert model.calls == 0
    with pytest.raises(BridgeError, match="UNASSOCIATED_TOOL_CALL"):
        await parent.tool_request({"name": "Bash", "callId": "unknown", "args": {}})
    assert parent.tool_calls == 0


async def test_actual_dsh_parent_loop_roundtrip_with_network_free_fixture():
    manifest = (
        Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@deepseek-ai/dsh/package.json"
    )
    if not shutil.which("node") or not manifest.is_file():
        pytest.skip("optional installed DSH 0.1.0-rc.6 is not available")
    model, parent = fixture()
    result = await parent.drive()
    assert result["turnEnd"] == "completed"
    assert result["answer"] == "public fixture output"
    assert parent.tool_calls == 1
    assert model.calls == 2


async def test_dsh_keeps_same_session_for_product_grounding_correction():
    manifest = (
        Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@deepseek-ai/dsh/package.json"
    )
    if not shutil.which("node") or not manifest.is_file():
        pytest.skip("optional installed DSH 0.1.0-rc.6 is not available")
    model, parent = fixture()
    try:
        first = await parent.start()
        second = await parent.followup(HumanMessage(content="Please correct your answer."))
        assert first["sessionId"] == second["sessionId"]
        assert second["answer"] == "corrected fixture output"
        assert parent.tool_calls == 1 and model.calls == 3
    finally:
        assert await parent.aclose()
