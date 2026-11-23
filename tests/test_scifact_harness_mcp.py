import json

import httpx
import pytest
from langchain_core.tools import StructuredTool
from tools.scifact_harness_mcp import PREFIX, ParentTools, profile, serve, tool_trace_agrees


def public_tool(name, calls):
    async def read(path: str):
        calls.append(path)
        return {"text": "public material", "path": path}

    return StructuredTool.from_function(
        coroutine=read, name=name, description="Read public material"
    )


def request(method, **params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


async def test_loopback_bridge_exposes_only_scoped_tools_and_exact_receipts():
    calls = []
    bridge = ParentTools([public_tool("Read", calls)], search_limit=2)
    async with serve(bridge) as url, httpx.AsyncClient(trust_env=False) as client:
        assert url.startswith("http://127.0.0.1:")
        assert (await client.post(url + "-other", json=request("tools/list"))).status_code == 404
        spec = (await client.post(url, json=request("tools/list"))).json()["result"]["tools"]
        assert [tool["name"] for tool in spec] == ["Read"]
        assert spec[0]["inputSchema"]["required"] == ["path"]
        denied = await client.post(url, json=request("tools/call", name="Bash"))
        assert denied.json()["error"]["code"] == -32602
        assert not calls
        args = {"path": "paper.txt"}
        receipt = await client.post(url, json=request("tools/call", name="Read", arguments=args))
        returned = receipt.json()["result"]["content"][0]["text"]
        assert json.loads(returned) == {"text": "public material", **args}
        assert json.loads(bridge.messages[-1].content) == json.loads(returned)
        assert calls == ["paper.txt"]
    assert tool_trace_agrees([{"name": PREFIX + "Read", "arguments": args}], bridge.actions)
    assert not tool_trace_agrees([{"name": "Read", "arguments": args}], bridge.actions)
    assert not tool_trace_agrees([{"name": PREFIX + "Read", "arguments": {}}], bridge.actions)


async def test_search_admission_is_counted_before_dispatch_and_cannot_be_bypassed():
    calls = []
    bridge = ParentTools([public_tool("search_literature", calls)], search_limit=1)
    args = {"path": "paper.txt"}
    assert (await bridge._call("case", "search_literature", args))["path"] == "paper.txt"
    assert (await bridge._call("case", "search_literature", args))["error"]["code"] == (
        "TOOL_REQUEST_REJECTED"
    )
    assert calls == ["paper.txt"]


def test_cli_profile_neither_expands_the_default_prompt_nor_allows_host_tools():
    body = profile("Literal ${base_prompt} and ${plugin_sections}", ["Read"])
    assert f"tools:\n  - {PREFIX}Read\n" in body
    assert "subagents: []" in body
    assert "${base_prompt}" not in body
    assert "${plugin_sections}" not in body
    with pytest.raises(ValueError, match="allowlist"):
        ParentTools([public_tool("Bash", [])], search_limit=1)
