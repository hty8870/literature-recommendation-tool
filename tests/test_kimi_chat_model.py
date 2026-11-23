from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from tools.kimi_chat_model import (
    PROTOCOL_VERSION,
    KimiChatModel,
    KimiLunaConfig,
    KimiLunaGateway,
    KimiModelIdentityError,
    KimiProtocolError,
    KimiTimeoutError,
    KimiToolUseForbidden,
    _escape_profile_template,
    _profile_text,
    _wire_message,
    build_discovery_components,
    parse_model_reply,
)

from citation_tool.config import Settings

FAKE_CLI = textwrap.dedent(
    """
    import json
    import os
    import sys
    import time
    import zipfile
    from pathlib import Path

    here = Path(__file__).resolve().parent
    config = json.loads((here / "fake_config.json").read_text(encoding="utf-8"))
    args = sys.argv[1:]
    with (here / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "invoke",
            "mode": "export" if args[:1] == ["export"] else "prompt",
            "argv": args,
            "cwd": os.getcwd(),
            "env_model_keys": sorted(
                key for key in os.environ if key.startswith("KIMI_MODEL_")
            ),
            "retry_step_limit": os.environ.get("KIMI_LOOP_MAX_STEPS_PER_TURN"),
            "env_credential_names": sorted(
                key for key in ("OPENAI_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY")
                if key in os.environ
            ),
        }, ensure_ascii=False) + "\\n")

    if args and args[0] == "export":
        archive = Path(args[args.index("-o") + 1])
        model = config.get("model", "gpt-6-luna")
        usage = config.get(
            "usage",
            {"inputOther": 10, "output": 5, "inputCacheRead": 0, "inputCacheCreation": 0},
        )
        wire = [
            {
                "type": "profile.bind",
                "modelAlias": config.get("alias", "OpenAI/gpt-6-luna"),
                "thinkingEffort": "max",
                "agentsMdPaths": [],
            },
            {
                "type": "llm.request",
                "model": model,
                "modelAlias": config.get("alias", "OpenAI/gpt-6-luna"),
                "thinkingEffort": "max",
                "systemPromptHash": "fake",
            },
            {"type": "context.append_loop_event", "event": {"type": "step.end", "usage": usage}},
        ]
        for _ in range(max(0, int(config.get("model_request_count", 1)) - 1)):
            wire.insert(2, dict(wire[1]))
        for name in config.get("cli_tool_calls", []):
            wire.append({
                "type": "context.append_loop_event",
                "event": {"type": "tool.call", "name": name, "args": {}, "toolCallId": "t1"},
            })
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                "agents/main/wire.jsonl",
                "\\n".join(json.dumps(record) for record in wire) + "\\n",
            )
        sys.exit(0)

    profile = None
    if "--agent-file" in args:
        profile = Path(args[args.index("--agent-file") + 1]).read_text(encoding="utf-8")
    with (here / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "kind": "prompt",
            "profile": profile,
            "prompt": args[args.index("-p") + 1] if "-p" in args else None,
        }, ensure_ascii=False) + "\\n")
    if config.get("sleep"):
        time.sleep(float(config["sleep"]))
    (here / "finished.txt").write_text("finished", encoding="utf-8")
    reply = config.get("reply", "")
    if config.get("replies"):
        index_path = here / "reply_index.txt"
        index = int(index_path.read_text(encoding="utf-8")) if index_path.exists() else 0
        reply = config["replies"][min(index, len(config["replies"]) - 1)]
        index_path.write_text(str(index + 1), encoding="utf-8")
    print(json.dumps({"role": "meta", "type": "system.version", "version": "fake"}))
    print(json.dumps({"role": "assistant", "content": reply}))
    print(json.dumps({
        "role": "meta",
        "type": "session.resume_hint",
        "session_id": config.get("session_id", "session_fake_1"),
    }))
    sys.exit(int(config.get("returncode", 0)))
    """
)

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current local time for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def make_fake_cli(tmp_path: Path, **config) -> tuple[str, ...]:
    script = tmp_path / "fake_kimi.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    values = {"reply": json.dumps({"content": "LUNA", "tool_calls": []})}
    values.update(config)
    (tmp_path / "fake_config.json").write_text(
        json.dumps(values, ensure_ascii=False), encoding="utf-8"
    )
    return (sys.executable, str(script))


def calls(tmp_path: Path) -> list[dict]:
    path = tmp_path / "calls.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def prompt_call(tmp_path: Path) -> dict:
    return next(
        item for item in calls(tmp_path) if item["kind"] == "invoke" and item["mode"] == "prompt"
    )


def path_exists(value: str) -> bool:
    return Path(value).exists()


def make_model(tmp_path: Path, command: tuple[str, ...], **overrides) -> KimiChatModel:
    values = {
        "model_id": "gpt-6-luna",
        "command": command,
        "timeout_s": 30.0,
        "artifact_root": tmp_path / "artifacts",
    }
    values.update(overrides)
    return KimiChatModel(**values)


def test_profile_has_no_tools_and_embeds_messages() -> None:
    envelope = {
        "protocol": PROTOCOL_VERSION,
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [WEATHER_TOOL],
        "tool_choice": "auto",
        "json_mode": False,
        "stop": [],
    }
    profile = _profile_text("abc123", envelope)
    assert profile.startswith("---\nname: citation-luna-abc123\n")
    assert "tools: []" in profile
    assert "subagents: []" in profile
    assert "${base_prompt}" not in profile
    assert '"content":"hello"' in profile
    assert "get_weather" in profile


def test_profile_template_variables_are_neutralised() -> None:
    text = _escape_profile_template("data ${cwd} and ${unknown_value}")
    assert "${cwd}" not in text
    assert "${unknown_value}" in text
    assert "$\u200b{cwd}" in text


def test_wire_messages_keep_roles_and_tool_links() -> None:
    assert _wire_message(SystemMessage(content="rules")) == {
        "role": "system",
        "content": "rules",
    }
    assert _wire_message(HumanMessage(content="question")) == {
        "role": "user",
        "content": "question",
    }
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "get_weather", "args": {"city": "Shanghai"}, "id": "c1"}],
    )
    assert _wire_message(ai)["tool_calls"] == [
        {"id": "c1", "name": "get_weather", "args": {"city": "Shanghai"}}
    ]
    tool = ToolMessage(content="sunny", tool_call_id="c1", name="get_weather")
    assert _wire_message(tool) == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "sunny",
        "name": "get_weather",
    }


def test_parse_reply_text_and_json_mode() -> None:
    content, calls = parse_model_reply('{"content":"LUNA","tool_calls":[]}')
    assert content == "LUNA"
    assert calls == []
    content, calls = parse_model_reply(
        '{"content":{"ok":true,"total":5},"tool_calls":[]}', json_mode=True
    )
    assert json.loads(content) == {"ok": True, "total": 5}
    assert calls == []


def test_parse_reply_rejects_bad_shapes_and_unoffered_tools() -> None:
    with pytest.raises(KimiProtocolError):
        parse_model_reply('{"content":broken')
    with pytest.raises(KimiProtocolError):
        parse_model_reply('{"content":"x","tool_calls":[],"extra":1}')
    with pytest.raises(KimiProtocolError):
        parse_model_reply('{"content":"","tool_calls":[]}')
    with pytest.raises(KimiProtocolError):
        parse_model_reply(
            '{"content":"","tool_calls":[{"name":"other","args":{}}]}',
            tool_names=["get_weather"],
        )


@pytest.mark.parametrize(
    "text, json_mode",
    [
        ("已检索到相关候选；全文尚待核查。", False),
        ('{"papers":[],"conclusion":"没有取得证据。"}', False),
        ('{"verdict":"supported","scope_ok":true}', True),
        ('[{"evidence_id":"ev-public"}]', True),
    ],
)
def test_direct_final_content_is_preserved_without_inventing_tool_calls(text, json_mode):
    content, tool_calls = parse_model_reply(
        text, tool_names=["search_literature"], json_mode=json_mode
    )
    assert content == text
    assert tool_calls == []
    with pytest.raises(KimiProtocolError):
        parse_model_reply(
            text, tool_names=["search_literature"], tool_choice="required", json_mode=json_mode
        )


@pytest.mark.parametrize(
    "text",
    ['{"content":broken', '{"tool_calls":[', '{"name":"search_literature","args":{}}'],
)
def test_direct_content_does_not_repair_malformed_or_unwrapped_tool_requests(text):
    with pytest.raises(KimiProtocolError):
        parse_model_reply(text, tool_names=["search_literature"])


async def test_plain_final_content_is_audited_as_normalized_not_synthesized(tmp_path):
    raw = "摘要报告花器官差异，但没有开花时间测量。"
    model = make_model(tmp_path, make_fake_cli(tmp_path, reply=raw))
    reply = await model.ainvoke([HumanMessage(content="解释已有来源，不需要新检索。")])
    assert reply.content == raw
    assert reply.tool_calls == []
    assert reply.response_metadata["wire_normalization"] == "plain-final-text-v1"
    audit = json.loads(next((tmp_path / "artifacts").glob("*.json")).read_text())
    assert audit["protocol"] == PROTOCOL_VERSION
    assert audit["wire_normalization"] == "plain-final-text-v1"
    assert audit["error"] is None


def test_parser_rejects_commentary_followed_by_tool_envelope():
    payload = "我会继续读取正文。" + json.dumps(
        {
            "content": "",
            "tool_calls": [
                {"name": "read_manuscript", "args": {"offset": 40, "limit": 8}},
            ],
        }
    )
    with pytest.raises(KimiProtocolError):
        parse_model_reply(payload, tool_names=["read_manuscript"])


@pytest.mark.parametrize(
    "payload",
    [
        '以下为格式示例：```json\\n{"tool_calls":[{"name":"get_weather","args":{}}]}\\n```',
        '准备读取。{"tool_calls":[{"name":"get_weather","args":{}}]} extra text',
        '准备读取。{"tool_calls":[',
        '准备读取。{"content":"","tool_calls":[{"name":"get_weather","args":{}}],"extra":true}',
    ],
)
def test_ambiguous_tool_shaped_text_is_not_presented_as_a_completed_answer(payload):
    with pytest.raises(KimiProtocolError):
        parse_model_reply(payload, tool_names=["get_weather"])


def test_tool_choice_contract() -> None:
    content, calls = parse_model_reply(
        '{"content":"","tool_calls":[{"name":"get_weather","args":{"city":"Shanghai"}}]}',
        tool_names=["get_weather"],
        tool_choice="required",
    )
    assert content == ""
    assert calls[0]["name"] == "get_weather"
    assert calls[0]["args"] == {"city": "Shanghai"}
    assert calls[0]["type"] == "tool_call"
    with pytest.raises(KimiProtocolError):
        parse_model_reply(
            '{"content":"no tool","tool_calls":[]}',
            tool_names=["get_weather"],
            tool_choice="required",
        )
    with pytest.raises(KimiProtocolError):
        parse_model_reply(
            '{"content":"","tool_calls":[{"name":"get_weather","args":{}}]}',
            tool_names=["get_weather", "get_time"],
            tool_choice="get_time",
        )


@pytest.mark.asyncio
async def test_async_text_call_validates_model_and_writes_metadata_only(tmp_path) -> None:
    command = make_fake_cli(tmp_path)
    model = make_model(tmp_path, command)
    message = await model.ainvoke(
        [
            SystemMessage(content="Be terse."),
            HumanMessage(content="PROMPT_SECRET_MARKER"),
        ]
    )
    assert message.content == "LUNA"
    assert message.tool_calls == []
    assert message.response_metadata["model_observed"] == ["gpt-6-luna"]
    assert message.response_metadata["model_valid"] is True
    assert message.response_metadata["session_id"] == "session_fake_1"
    assert message.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "input_token_details": {"cache_read": 0},
    }
    invocation = prompt_call(tmp_path)
    prompt_record = next(item for item in calls(tmp_path) if item["kind"] == "prompt")
    assert invocation["cwd"]
    assert not path_exists(invocation["cwd"])
    assert len(prompt_record["prompt"]) < 200
    artifacts = list((tmp_path / "artifacts").glob("*.json"))
    assert len(artifacts) == 1
    artifact_text = artifacts[0].read_text(encoding="utf-8")
    assert "PROMPT_SECRET_MARKER" not in artifact_text
    assert "LUNA" not in artifact_text
    artifact = json.loads(artifact_text)
    assert artifact["model_valid"] is True
    assert artifact["session_id"] == "session_fake_1"
    assert artifact["model_calls"] == 1
    assert artifact["usage"]["input_tokens"] == 10
    assert artifact["error"] is None


@pytest.mark.asyncio
async def test_langchain_usage_callback_records_gpt_6_luna(tmp_path) -> None:
    from tools.benchmark.core import BenchmarkUsageCallback

    callback = BenchmarkUsageCallback()
    model = make_model(tmp_path, make_fake_cli(tmp_path))
    model.callbacks = [callback]
    await model.ainvoke([HumanMessage(content="hello")])
    assert callback.totals.served_models == {"gpt-6-luna"}
    assert callback.totals.calls == 1
    assert callback.totals.input_tokens == 10
    assert callback.totals.output_tokens == 5


def test_sync_call_and_tool_choice_binding(tmp_path) -> None:
    command = make_fake_cli(
        tmp_path,
        reply=json.dumps(
            {
                "content": "",
                "tool_calls": [{"name": "get_weather", "args": {"city": "Shanghai"}}],
            }
        ),
    )
    model = make_model(tmp_path, command)
    bound = model.bind_tools([WEATHER_TOOL, TIME_TOOL], tool_choice="auto")
    message = bound.invoke([HumanMessage(content="Weather in Shanghai?")])
    assert message.tool_calls == [
        {
            "name": "get_weather",
            "args": {"city": "Shanghai"},
            "id": message.tool_calls[0]["id"],
            "type": "tool_call",
        }
    ]
    prompt_record = next(item for item in calls(tmp_path) if item["kind"] == "prompt")
    assert "get_weather" in prompt_record["profile"]
    assert "get_time" in prompt_record["profile"]


def test_structured_tool_schema_is_bound(tmp_path) -> None:
    class WeatherArgs(BaseModel):
        city: str

    def get_weather(city: str) -> str:
        return f"weather for {city}"

    tool = StructuredTool.from_function(
        func=get_weather,
        name="get_weather",
        description="Get the current weather for a city.",
        args_schema=WeatherArgs,
    )
    model = make_model(tmp_path, make_fake_cli(tmp_path))
    bound = model.bind_tools([tool], tool_choice="required")
    schema = bound.bound_tools[0]
    assert schema["function"]["name"] == "get_weather"
    assert schema["function"]["parameters"]["properties"]["city"]["type"] == "string"
    assert bound.tool_choice == "required"


def test_create_agent_executes_returned_tool_and_feeds_result_back(tmp_path) -> None:
    from langchain.agents import create_agent

    class WeatherArgs(BaseModel):
        city: str

    def get_weather(city: str) -> str:
        return f"sunny in {city}"

    tool = StructuredTool.from_function(
        func=get_weather,
        name="get_weather",
        description="Get the current weather for a city.",
        args_schema=WeatherArgs,
    )
    command = make_fake_cli(
        tmp_path,
        replies=[
            json.dumps(
                {
                    "content": "",
                    "tool_calls": [{"name": "get_weather", "args": {"city": "Shanghai"}}],
                }
            ),
            json.dumps({"content": "The weather is sunny.", "tool_calls": []}),
        ],
    )
    model = make_model(tmp_path, command)
    agent = create_agent(
        model=model,
        tools=[tool],
        system_prompt="Use the offered tool when needed.",
    )
    result = agent.invoke({"messages": [HumanMessage(content="Weather in Shanghai?")]})
    assert result["messages"][-1].content == "The weather is sunny."
    prompts = [item for item in calls(tmp_path) if item["kind"] == "prompt"]
    assert len(prompts) == 2
    profile = prompts[1]["profile"]
    envelope_text = profile.split("<envelope-", 1)[1].split(">", 1)[1].split("</envelope-", 1)[0]
    second = json.loads(envelope_text)
    roles = [message["role"] for message in second["messages"]]
    assert "tool" in roles
    tool_message = next(message for message in second["messages"] if message["role"] == "tool")
    assert "sunny in Shanghai" in tool_message["content"]
    assert any(
        message["role"] == "assistant" and message.get("tool_calls")
        for message in second["messages"]
    )


def test_json_mode_returns_serialized_content(tmp_path) -> None:
    command = make_fake_cli(
        tmp_path,
        reply=json.dumps({"content": {"ok": True, "total": 5}, "tool_calls": []}),
    )
    model = make_model(tmp_path, command, json_mode=True)
    message = model.invoke([HumanMessage(content="Return the object.")])
    assert json.loads(message.content) == {"ok": True, "total": 5}


def test_model_mismatch_fails_and_records_it(tmp_path) -> None:
    command = make_fake_cli(tmp_path, model="deepseek-flash")
    model = make_model(tmp_path, command)
    with pytest.raises(KimiModelIdentityError):
        model.invoke([HumanMessage(content="hello")])
    artifact = json.loads(next((tmp_path / "artifacts").glob("*.json")).read_text())
    assert artifact["model_valid"] is False
    assert artifact["model_observed"] == ["deepseek-flash"]
    assert artifact["error"]["code"] == "KIMI_MODEL_MISMATCH"
    assert artifact["session_id"] == "session_fake_1"


def test_real_cli_tool_call_is_rejected(tmp_path) -> None:
    command = make_fake_cli(tmp_path, cli_tool_calls=["Bash"])
    model = make_model(tmp_path, command)
    with pytest.raises(KimiToolUseForbidden):
        model.invoke([HumanMessage(content="hello")])
    artifact = json.loads(next((tmp_path / "artifacts").glob("*.json")).read_text())
    assert artifact["error"]["code"] == "KIMI_CLI_TOOL_USE_FORBIDDEN"
    assert artifact["error"]["details"]["tool_calls"] == ["Bash"]


def test_protocol_violation_is_reported(tmp_path) -> None:
    command = make_fake_cli(tmp_path, reply='{"content":broken')
    model = make_model(tmp_path, command)
    with pytest.raises(KimiProtocolError):
        model.invoke([HumanMessage(content="hello")])
    artifact = json.loads(next((tmp_path / "artifacts").glob("*.json")).read_text())
    assert artifact["error"]["code"] == "KIMI_PROTOCOL_INVALID"
    assert artifact["model_valid"] is True


def test_timeout_kills_only_the_spawned_cli(tmp_path) -> None:
    command = make_fake_cli(tmp_path, sleep=5)
    model = make_model(tmp_path, command, timeout_s=10.0)
    with pytest.raises(KimiTimeoutError):
        model.invoke([HumanMessage(content="hello")])
    assert not (tmp_path / "finished.txt").exists()
    artifact = json.loads(next((tmp_path / "artifacts").glob("*.json")).read_text())
    assert artifact["error"]["code"] == "KIMI_TIMEOUT"


@pytest.mark.asyncio
async def test_cancellation_kills_the_spawned_cli_and_cleans_up(tmp_path) -> None:
    command = make_fake_cli(tmp_path, sleep=10)
    model = make_model(tmp_path, command, timeout_s=30.0)
    task = asyncio.create_task(model.ainvoke([HumanMessage(content="hello")]))
    deadline = asyncio.get_running_loop().time() + 10
    while not any(item["kind"] == "prompt" for item in calls(tmp_path)):
        if task.done():
            await task
            pytest.fail("model call finished before the fake CLI reached its wait")
        if asyncio.get_running_loop().time() >= deadline:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pytest.fail("fake CLI did not signal that it was ready for cancellation")
        await asyncio.sleep(0.01)
    invocation = prompt_call(tmp_path)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "finished.txt").exists()
    assert not path_exists(invocation["cwd"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancellation_during_profile_preparation_cleans_late_workspace(
    tmp_path, monkeypatch, cancel_count
) -> None:
    command = make_fake_cli(tmp_path)
    model = make_model(tmp_path, command)
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    plans = []
    original = KimiChatModel._prepare

    def prepare(self, *args, **kwargs):
        plan = original(self, *args, **kwargs)
        plans.append(plan)
        entered.set()
        if not release.wait(10):
            raise TimeoutError("profile preparation test was not released")
        returned.set()
        return plan

    monkeypatch.setattr(KimiChatModel, "_prepare", prepare)
    task = asyncio.create_task(model.ainvoke([HumanMessage(content="public fixture")]))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(returned.wait, 10)
        assert not plans[0].workspace.exists()
        assert calls(tmp_path) == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        for plan in plans:
            plan.close()


def test_child_environment_does_not_receive_provider_secrets(tmp_path, monkeypatch) -> None:
    secret = "sk-" + "a" * 24
    monkeypatch.setenv("KIMI_MODEL_API_KEY", secret)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    command = make_fake_cli(tmp_path)
    model = make_model(tmp_path, command)
    model.invoke([HumanMessage(content="hello")])
    invocation = prompt_call(tmp_path)
    assert invocation["env_model_keys"] == []
    assert invocation["env_credential_names"] == []
    assert invocation["retry_step_limit"] == "16"
    text = (tmp_path / "calls.jsonl").read_text(encoding="utf-8")
    assert secret not in text


@pytest.mark.parametrize(
    "overrides",
    [{"model": "other"}, {"model_alias": "Other/gpt-6-luna"}],
)
def test_development_gateway_rejects_other_model_identities_before_cli(overrides):
    with pytest.raises(ValueError, match="requires OpenAI/gpt-6-luna"):
        KimiLunaConfig(**overrides).validate()
    fields = {"model_id" if k == "model" else k: v for k, v in overrides.items()}
    with pytest.raises(ValueError, match="requires OpenAI/gpt-6-luna"):
        KimiChatModel(**fields)


def test_exported_alias_must_match_as_well_as_the_actual_model(tmp_path):
    model = make_model(tmp_path, make_fake_cli(tmp_path, alias="Other/gpt-6-luna"))
    with pytest.raises(KimiModelIdentityError):
        model.invoke([HumanMessage(content="public fixture")])
    audit = json.loads(next((tmp_path / "artifacts").glob("*.json")).read_text())
    assert audit["model_valid"] is False
    assert audit["model_aliases"] == ["Other/gpt-6-luna"]


def test_nonserializable_schema_fails_before_creating_a_profile(tmp_path, monkeypatch):
    command = make_fake_cli(tmp_path)
    model = make_model(tmp_path, command)
    raw = {**WEATHER_TOOL, "non_serializable": object()}
    bound = model.bind_tools([raw])
    created = []
    monkeypatch.setattr(
        "tools.kimi_chat_model.tempfile.mkdtemp", lambda **kwargs: created.append(kwargs)
    )
    with pytest.raises(TypeError):
        bound.invoke([HumanMessage(content="public fixture")])
    assert created == []
    assert calls(tmp_path) == []


def test_explicit_empty_tool_list_removes_previously_bound_tools(tmp_path):
    model = make_model(tmp_path, make_fake_cli(tmp_path)).bind_tools([WEATHER_TOOL])
    envelope = model._messages_envelope([HumanMessage(content="no tools")], None, {"tools": []})
    assert envelope["tools"] == []
    with pytest.raises(KimiProtocolError):
        parse_model_reply(
            '{"tool_calls":[{"function":{"name":"get_weather","arguments":"{}"}}]}',
            tool_names=["get_weather"],
        )


@pytest.mark.asyncio
async def test_cancelled_export_reader_finishes_before_removing_its_archive(tmp_path, monkeypatch):
    model = make_model(tmp_path, make_fake_cli(tmp_path))
    entered, release = threading.Event(), threading.Event()
    paths = []
    original = KimiChatModel._read_export

    def read_export(self, archive, started_at):
        paths.append(archive)
        entered.set()
        assert release.wait(10)
        assert archive.is_file()
        return original(self, archive, started_at)

    monkeypatch.setattr(KimiChatModel, "_read_export", read_export)
    task = asyncio.create_task(model.ainvoke([HumanMessage(content="public fixture")]))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not paths[0].parent.exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_native_api_retries_are_counted_even_when_one_usage_report_is_missing(tmp_path):
    model = make_model(tmp_path, make_fake_cli(tmp_path, model_request_count=2))
    reply = await model.ainvoke([HumanMessage(content="Public request")])
    assert reply.response_metadata["model_calls"] == 2
    usage = reply.response_metadata["usage"]
    assert usage["model_calls"] == 2
    assert usage["usage_report_count"] == 1
    assert usage["usage_complete"] is False
    assert reply.usage_metadata["input_tokens"] == 10
    audit = json.loads(next((tmp_path / "artifacts").glob("*.json")).read_text())
    assert audit["model_calls"] == 2
    assert audit["usage"]["usage_complete"] is False
    assert audit["cli_retry_step_limit"] == 16
    assert audit["model_valid"] is True


async def test_format_repair_preserves_failures_usage_and_the_original_deadline(
    tmp_path, monkeypatch
):
    failed = (
        '我会读取。{"content":"","tool_calls":[{"name":"get_weather","args":{"city":"Shanghai"}}]}'
    )
    corrected = '{"content":"","tool_calls":[{"name":"get_weather","args":{"city":"Shanghai"}}]}'
    model = make_model(tmp_path, make_fake_cli(tmp_path, replies=[failed, corrected]))
    plans = []
    original = KimiChatModel._prepare

    def prepared(self, *args, **kwargs):
        plan = original(self, *args, **kwargs)
        plans.append(plan)
        return plan

    monkeypatch.setattr(KimiChatModel, "_prepare", prepared)
    reply = await model.bind_tools([WEATHER_TOOL]).ainvoke([HumanMessage(content="Public request")])
    assert reply.tool_calls[0]["args"] == {"city": "Shanghai"}
    assert reply.response_metadata["attempt_count"] == 2
    assert reply.response_metadata["model_calls"] == 2
    assert reply.usage_metadata["input_tokens"] == 20
    assert reply.usage_metadata["output_tokens"] == 10
    assert plans[0].deadline == plans[1].deadline
    assert all(not plan.workspace.exists() for plan in plans)
    audits = [json.loads(p.read_text()) for p in (tmp_path / "artifacts").glob("*.json")]
    assert len(audits) == 2
    assert sum(a["error"] is not None for a in audits) == 1
    assert all(a["model_valid"] for a in audits)
    assert sum(a["model_calls"] for a in audits) == 2
    assert all(failed not in json.dumps(a, ensure_ascii=False) for a in audits)


async def test_format_repair_is_one_attempt_not_an_unbounded_research_loop(tmp_path):
    model = make_model(tmp_path, make_fake_cli(tmp_path, reply='{"content":broken'))
    with pytest.raises(KimiProtocolError):
        await model.ainvoke([HumanMessage(content="Public request")])
    assert len([item for item in calls(tmp_path) if item["kind"] == "prompt"]) == 2
    audits = [json.loads(p.read_text()) for p in (tmp_path / "artifacts").glob("*.json")]
    assert len(audits) == 2 and all(a["error"] for a in audits)


async def test_format_repair_can_return_a_quoted_example_without_executing_it(tmp_path):
    quoted = '示例：{"tool_calls":[{"name":"get_weather","args":{"city":"Shanghai"}}]}'
    model = make_model(
        tmp_path,
        make_fake_cli(
            tmp_path,
            replies=[
                quoted,
                json.dumps({"content": quoted, "tool_calls": []}, ensure_ascii=False),
            ],
        ),
    )
    reply = await model.bind_tools([WEATHER_TOOL]).ainvoke(
        [HumanMessage(content="Explain the format")]
    )
    assert reply.content == quoted
    assert not reply.tool_calls


async def test_langchain_executes_the_repaired_tool_request_once_and_only_after_repair(tmp_path):
    from langchain.agents import create_agent

    executed = []

    def weather(city: str) -> str:
        executed.append(city)
        return "PUBLIC-SUNNY"

    tool = StructuredTool.from_function(
        func=weather, name="get_weather", description="Public weather"
    )
    action = {"content": "", "tool_calls": [{"name": "get_weather", "args": {"city": "Shanghai"}}]}
    model = make_model(
        tmp_path,
        make_fake_cli(
            tmp_path,
            replies=[
                "I will look it up." + json.dumps(action),
                json.dumps(action),
                json.dumps({"content": "PUBLIC-SUNNY", "tool_calls": []}),
            ],
        ),
    )
    result = await create_agent(model=model, tools=[tool]).ainvoke(
        {"messages": [HumanMessage(content="Public weather please")]}
    )
    assert result["messages"][-1].content == "PUBLIC-SUNNY"
    assert executed == ["Shanghai"]
    prompts = [item["profile"] for item in calls(tmp_path) if item["kind"] == "prompt"]
    assert len(prompts) == 3
    assert '"role":"tool"' not in prompts[1]
    assert '"role":"tool"' in prompts[2]
    assert "PUBLIC-SUNNY" in prompts[2]
    audits = [json.loads(p.read_text()) for p in (tmp_path / "artifacts").glob("*.json")]
    assert len(audits) == 3
    assert sum(a["error"] is not None for a in audits) == 1


def test_gateway_shares_one_semaphore_and_reports_gpt_6_luna(tmp_path) -> None:
    command = make_fake_cli(tmp_path)
    gateway = KimiLunaGateway(
        KimiLunaConfig(command=command, artifact_root=None, max_concurrency=1)
    )
    agent = gateway.agent_model()
    reason = gateway.reason_model()
    rerank = gateway.rerank_model()
    review = gateway.review_model()
    assert gateway.config.model == "gpt-6-luna"
    assert gateway.config.provider == "kimi-code-cli"
    assert agent._async_semaphore is reason._async_semaphore
    assert reason._async_semaphore is rerank._async_semaphore
    assert rerank._async_semaphore is review._async_semaphore
    assert reason.bind_tools([])._async_semaphore is reason._async_semaphore
    assert agent.json_mode is False
    assert reason.json_mode is True
    assert rerank.json_mode is True
    assert review.json_mode is True


def test_components_inject_into_create_app(tmp_path) -> None:
    import dataclasses

    from tools.kimi_chat_model import build_kimi_app

    command = make_fake_cli(tmp_path)
    gateway = KimiLunaGateway(
        KimiLunaConfig(command=command, artifact_root=None, max_concurrency=1)
    )
    reasons, agent_model, models = build_discovery_components(
        gateway, max_inflight=1, timeout_ms=5000
    )
    assert reasons.provider == "kimi-code-cli"
    assert reasons.gateway is gateway
    assert models.model_name == "gpt-6-luna"
    assert models.provider == "kimi-code-cli"
    invalid_config = tmp_path / "deepseek.local.json"
    invalid_config.write_text("not json", encoding="utf-8")
    settings = dataclasses.replace(Settings.for_tests(tmp_path), model_config_path=invalid_config)
    app = build_kimi_app(
        settings,
        gateway,
        max_inflight=1,
        timeout_ms=5000,
    )
    assert app.state.search_service.reason_provider == "kimi-code-cli"
    assert app.state.agent_runner.model_name == "gpt-6-luna"
    assert app.state.agent_runner.provider == "kimi-code-cli"
    assert app.state.agent_runner.available is True
    assert app.state.settings.allow_user_models is False
    assert app.state.search_service.coordinator.reasons._cache is app.state.store


def test_detached_empty_tool_list_normalization_preserves_content_without_creating_decisions():
    original = "第一篇与研究需求相关。\n仍需核对全文与条件。"
    malformed = json.dumps({"content": original}, ensure_ascii=False) + ', "tool_calls": [] }'
    content, calls = parse_model_reply(
        malformed, tool_names=["search_literature"], tool_choice=None, json_mode=False
    )
    assert content == original
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"content":"text"},"tool_calls":[{"name":"search_literature","args":{}}]}',
        '{"content":"text","other":"value"},"tool_calls":[]}',
        '{"content":null},"tool_calls":[]}',
        '{"content":"text"},"tool_calls":[],"extra":true}',
    ],
)
def test_wire_normalizer_never_guesses_nonempty_tools_or_discards_extra_fields(payload):
    with pytest.raises(KimiProtocolError):
        parse_model_reply(payload, tool_names=["search_literature"], json_mode=False)
