from __future__ import annotations

import json
from dataclasses import replace

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.research_context import compact_working_messages
from citation_tool.research_workspace import (
    ResearchWorkspace,
    VirtualDocument,
    WorkspaceError,
    text_lines,
)
from test_agent import ScriptedChatModel, StubReasonGenerator, tool_call, wait_agent
from test_integration import headers, search_body


async def test_virtual_tools_use_exact_unicode_ranges_regex_and_versioned_cursor():
    path = "manuscripts/ms_a/text.txt"
    original = "条件 ✅ " + "a" * 150 + "\n对照 浅色；结果 0.752。"
    doc = VirtualDocument.build(path, "manuscript", text_lines(original, {"passage_index": 3}))
    current = {path: doc}
    budget = {"calls": 0, "chars": 0, "catalog_chars": 0}

    def resolve(name):
        if name not in current:
            raise WorkspaceError("WORKSPACE_NOT_FOUND", "Unavailable")
        return current[name]

    tools = {
        t.name: t
        for t in ResearchWorkspace(
            lambda _: current, resolve, manuscript_budget=budget, disclose=lambda *_: None
        ).tools()
    }
    assert (await tools["Glob"].ainvoke({"path": "manuscripts"}))["paths"] == [path]
    hits = await tools["Grep"].ainvoke({"path": path, "pattern": r"浅色|0\.75[0-9]"})
    hit = hits["lines"][0]
    assert hit["line"] == 2 and hit["match_columns"] == [3, 5]
    assert original[hit["anchor"]["start"] : hit["anchor"]["end"]] == hit["text"]
    assert (await tools["Grep"].ainvoke({"path": path, "pattern": r"(a)\1"}))["error"][
        "code"
    ] == "WORKSPACE_PATTERN_INVALID"
    page = await tools["Read"].ainvoke({"path": path, "max_chars": 100})
    tail = await tools["Read"].ainvoke(page["next_arguments"])
    assert page["lines"][0]["text"] + tail["lines"][0]["text"] == original.splitlines()[0]
    assert budget["chars"] >= len(original.splitlines()[0])
    current[path] = VirtualDocument.build(path, "manuscript", text_lines("changed"))
    stale = await tools["Read"].ainvoke(page["next_arguments"])
    assert stale["error"]["code"] == "WORKSPACE_VERSION_CHANGED"
    for forbidden in ("../text.txt", "C:/Windows/hosts", "manuscripts/ms_foreign/text.txt"):
        assert "error" in await tools["Read"].ainvoke({"path": forbidden})


def _call(name, arguments, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id}])


def test_compaction_only_changes_old_working_view_and_preserves_tool_pairing():
    path = "manuscripts/ms_a/text.txt"
    content = json.dumps(
        {
            "path": path,
            "kind": "manuscript",
            "version": "a" * 64,
            "lines": [{"line": 8, "start_column": 4, "text": "long " * 5000}],
        }
    )
    base = HumanMessage("Keep exact evidence and the current constraints.")
    first = _call("Read", {"path": path}, "old_read")
    old = ToolMessage(content=content, name="Read", tool_call_id="old_read")
    newest = _call("Read", {"path": path}, "new_read")
    recent = ToolMessage(content="the latest result", name="Read", tool_call_id="new_read")
    history = [base, first, old, newest, recent]
    view, changes = compact_working_messages(history, budget=900)
    assert len(changes) == 1 and changes[0]["tool_call_id"] == "old_read"
    assert view[0] == base and view[1] == first and view[-2:] == [newest, recent]
    assert view[2].tool_call_id == first.tool_calls[0]["id"]
    replacement = json.loads(view[2].content)
    assert replacement["compacted"] and replacement["version"] == "a" * 64
    assert replacement["read_from"] == {"line_offset": 8, "column_offset": 4}
    assert history[2].content == content


def test_search_returns_artifact_and_followup_reads_the_existing_rank(tmp_path, live_server):
    model = ScriptedChatModel(
        responses=[
            tool_call("first-search", ["LFY2"], ["fixture-fast"]),
            AIMessage(content="第一轮保存榜单。"),
        ]
    )
    app = create_app(
        replace(
            Settings.for_tests(tmp_path), agent_research_workspace=True, agent_research_context=True
        ),
        reason_generator=StubReasonGenerator(),
        agent_model=model,
    )
    base = live_server(app)
    with httpx.Client(timeout=15) as client:
        response = client.post(
            f"{base}/api/v1/agent/runs",
            json={**search_body({"fixture-fast": 1}), "message": "请找植物开花研究。"},
            headers=headers(),
        )
        assert response.status_code == 202, response.text
        pending = response.json()
        result = wait_agent(client, base, pending["run_id"])
        assert result["status"] == "completed", result["error"]
        tool = next(m for m in model.received_messages[-1] if isinstance(m, ToolMessage))
        brief = json.loads(tool.content)
        assert brief["query_id"] == result["latest_query_id"] and "results" not in brief
        assert "candidate_diagnostics" not in brief
        model.responses.extend(
            [
                _call("Read", {"path": brief["ranking_path"], "n_lines": 12}, "read-rank"),
                _call("Read", {"path": "turns/0001.txt"}, "read-turn"),
                AIMessage(content="看过旧榜单，没有重查。"),
            ]
        )
        response = client.post(
            f"{base}/api/v1/conversations/{pending['conversation_id']}/turns",
            json={"message": "现在看上次的实际榜单。"},
            headers=headers(),
        )
        assert response.status_code == 202, response.text
        follow = wait_agent(client, base, response.json()["run_id"])
        assert follow["status"] == "completed", follow["error"]
        assert follow["queries"] == []
        history = follow["context_manifest"]["research_context"]
        assert history["history_messages_included"] == 2
        inputs = model.received_messages[-1]
        assert any(
            isinstance(m, HumanMessage) and m.content == "请找植物开花研究。" for m in inputs
        )
        assert any(isinstance(m, AIMessage) and m.content == "第一轮保存榜单。" for m in inputs)
        reads = [
            json.loads(m.content) for m in inputs if isinstance(m, ToolMessage) and m.name == "Read"
        ]
        assert reads[0]["kind"] == "settled_ranking" and reads[0]["lines"]
        assert reads[0]["provenance"]["query_id"] == brief["query_id"]
        assert reads[1]["kind"] == "conversation_history_not_evidence"
