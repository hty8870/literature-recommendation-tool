"""Run the DSH parent loop over the existing native Luna transport via private stdio."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from tools.benchmark.core import file_hash, secret_leak
from tools.gap_benchmark.data import RUNTIME, save_new
from tools.luna_credentials import MODEL, authorized_config
from tools.luna_native_product_retest import NativeModelAudit, NativeModelSlots, collect_model_audit
from tools.parallel_luna import ParallelLunaGateway
from tools.scifact_workspace_retest import AuditedLunaClient

from citation_tool.agent import final_answer_text

DRIVER = Path(__file__).with_name("scifact_dsh_driver.mjs")
LIMIT = 1_000_000
CHILD_ENV = {"APPDATA", "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}


class BridgeError(RuntimeError):
    pass


def text_blocks(text):
    return [{"type": "text", "text": text}]


def contents(message, *, tool_turn):
    if not tool_turn:
        text = final_answer_text(message)
        if not text:
            raise BridgeError("INVALID_NATIVE_FINAL_RESPONSE")
        return text_blocks(text)
    if isinstance(message.content, str):
        return text_blocks(message.content) if message.content else []
    return [
        {"type": "text", "text": part["text"]}
        for part in message.content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    ]


def normalize_projection(messages):
    normalized = []
    for message in messages:
        content = []
        for block in message["content"]:
            if block["type"] == "tool-result":
                content.append({**block, "isError": bool(block.get("isError"))})
            else:
                content.append(block)
        normalized.append({"role": message["role"], "content": content})
    return normalized


class DshParent:
    def __init__(
        self, model, tools, system: str, prompt: str, *, max_model_calls: int, max_tool_calls=32
    ):
        self.system = system
        self.prompt = prompt
        self.tools = {tool.name: tool for tool in tools}
        self.schemas = [convert_to_openai_tool(tool)["function"] for tool in tools]
        self.model = model.bind_tools(tools)
        self.history = [HumanMessage(content=prompt)]
        self.projected = [{"role": "user", "content": text_blocks(prompt)}]
        self.pending = {}
        self.completed = False
        self.model_calls = 0
        self.tool_calls = 0
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.child = None
        self.turns = []

    async def model_request(self, payload):
        received_specs = sorted(payload.get("tools", []), key=lambda spec: spec["name"])
        expected_specs = sorted(self.schemas, key=lambda spec: spec["name"])
        if (
            payload.get("model") != MODEL
            or payload.get("system") != self.system
            or received_specs != expected_specs
        ):
            raise BridgeError("UNEXPECTED_MODEL_PROMPT_OR_TOOL_SCHEMAS")
        if self.completed or self.pending or self.model_calls >= self.max_model_calls:
            raise BridgeError("MODEL_REQUEST_NOT_ADMITTED")
        if normalize_projection(payload.get("messages", [])) != self.projected:
            raise BridgeError("MODEL_HISTORY_DIFFERS_FROM_DSH_VIEW")
        self.model_calls += 1
        message = await self.model.ainvoke([SystemMessage(content=self.system), *self.history])
        if (
            not isinstance(message, AIMessage)
            or message.invalid_tool_calls
            or message.response_metadata.get("model_name") != MODEL
            or message.response_metadata.get("status") not in (None, "completed")
        ):
            raise BridgeError("UNVERIFIED_PROVIDER_RESPONSE")
        calls = message.tool_calls
        blocks = contents(message, tool_turn=bool(calls))
        for call in calls:
            identity, name = call.get("id"), call.get("name")
            if not identity or identity in self.pending or name not in self.tools:
                raise BridgeError("UNEXPECTED_PROVIDER_TOOL_CALL")
            self.pending[identity] = call
            blocks.append(
                {
                    "type": "tool-call",
                    "id": identity,
                    "name": name,
                    "arguments": json.dumps(
                        call["args"], ensure_ascii=False, separators=(",", ":")
                    ),
                }
            )
        self.history.append(message)
        self.projected.append({"role": "assistant", "content": blocks})
        self.completed = not calls
        return {"blocks": blocks, "finish": "tool-calls" if calls else "stop"}

    async def tool_request(self, payload):
        if self.tool_calls >= self.max_tool_calls:
            raise BridgeError("DSH_TOOL_CALL_BUDGET_EXHAUSTED")
        identity = payload.get("callId")
        call = self.pending.pop(identity, None)
        if (
            call is None
            or payload.get("name") != call["name"]
            or payload.get("args") != call["args"]
        ):
            raise BridgeError("UNASSOCIATED_TOOL_CALL")
        result = await self.tools[call["name"]].ainvoke(call["args"])
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        self.history.append(ToolMessage(content=content, name=call["name"], tool_call_id=identity))
        self.projected.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": identity,
                        "content": text_blocks(content),
                        "isError": False,
                    }
                ],
            }
        )
        self.tool_calls += 1
        return content

    async def send(self, value):
        encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > LIMIT:
            raise BridgeError("OVERSIZED_BRIDGE_MESSAGE")
        self.child.stdin.write(encoded)
        await self.child.stdin.drain()

    async def receive_turn(self):
        while raw := await self.child.stdout.readline():
            data = json.loads(raw)
            kind = data.get("kind")
            if kind == "done":
                if (
                    not self.completed
                    or self.pending
                    or data.get("turnEnd") != "completed"
                    or data.get("calledTools") != self.tool_calls
                    or data.get("answer") != final_answer_text(self.history[-1])
                    or (self.turns and data.get("sessionId") != self.turns[0]["sessionId"])
                ):
                    raise BridgeError("UNVERIFIED_DSH_TERMINAL_STATE")
                self.turns.append(data)
                return data
            if kind not in {"model", "tool"} or not isinstance(data.get("id"), int):
                raise BridgeError("INVALID_DSH_EVENT")
            handler = self.model_request if kind == "model" else self.tool_request
            value = await handler(data["payload"])
            await self.send({"id": data["id"], "ok": True, "result": value})
        raise BridgeError("DSH_STREAM_CLOSED_WITHOUT_TERMINAL_STATE")

    async def start(self):
        if self.child is not None or self.turns:
            raise BridgeError("DSH_SESSION_ALREADY_STARTED")
        node = shutil.which("node")
        if not node:
            raise BridgeError("NODE_NOT_AVAILABLE")
        env = {key: value for key, value in os.environ.items() if key.upper() in CHILD_ENV}
        self.child = await asyncio.create_subprocess_exec(
            node,
            str(DRIVER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=LIMIT,
        )
        await self.send({"system": self.system, "prompt": self.prompt, "tools": self.schemas})
        return await self.receive_turn()

    async def followup(self, message):
        if self.child is None or not self.turns or not self.completed or self.pending:
            raise BridgeError("DSH_SESSION_NOT_READY_FOR_FOLLOWUP")
        if not isinstance(message, HumanMessage) or not isinstance(message.content, str):
            raise BridgeError("UNSUPPORTED_DSH_FOLLOWUP_MESSAGE")
        self.completed = False
        self.history.append(message)
        self.projected.append({"role": "user", "content": text_blocks(message.content)})
        await self.send({"kind": "followup", "prompt": message.content})
        return await self.receive_turn()

    async def aclose(self):
        child = self.child
        if child is None:
            return True
        try:
            if child.returncode is None and self.turns and self.completed:
                try:
                    async with asyncio.timeout(5):
                        await self.send({"kind": "close"})
                        return await child.wait() == 0
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    return False
            return child.returncode == 0
        finally:
            self.child = None
            if child.returncode is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                await child.wait()

    async def drive(self):
        try:
            result = await self.start()
        finally:
            closed = await self.aclose()
        if not closed:
            raise BridgeError("DSH_DID_NOT_EXIT_NORMALLY")
        return result


class ProbeClient(AuditedLunaClient):
    def __init__(self, expected):
        super().__init__(1)
        self.expected = expected
        self.receipt_reached_provider = False

    async def send(self, request, **kwargs):
        body = json.loads(request.content)
        sent = [x for x in body.get("input", []) if x.get("type") == "function_call_output"]
        for value in sent:
            output = value.get("output")
            if output == self.expected or (
                isinstance(output, list)
                and any(part.get("text") == self.expected for part in output)
            ):
                self.receipt_reached_provider = True
        return await super().send(request, **kwargs)


async def probe(directory):
    config = await asyncio.to_thread(authorized_config)
    marker = uuid4().hex
    material = {"marker": marker}

    async def read_marker(path: str) -> dict:
        return material if path == "public.txt" else {"error": {"code": "NOT_FOUND"}}

    tool = StructuredTool.from_function(
        coroutine=read_marker, name="read_virtual", description="Read one authorized public marker."
    )
    audit = NativeModelAudit(directory / "native-audit", NativeModelSlots())
    async with ProbeClient(json.dumps(material, ensure_ascii=False)) as client:
        gateway = ParallelLunaGateway(config, client, audit)
        parent = DshParent(
            gateway.agent_model(),
            [tool],
            "Use only the authorized tool to answer this public test.",
            "Use read_virtual on public.txt. Return only its marker value, without any formatting.",
            max_model_calls=4,
        )
        async with asyncio.timeout(115):
            result = await parent.drive()
        await audit.slots.wait_idle()
        audit.seal()
        native = collect_model_audit(directory / "native-audit")
    if (
        not native["model_valid"]
        or not client.receipt_reached_provider
        or result["answer"] != marker
        or parent.tool_calls != 1
        or parent.model_calls < 2
        or len(client.rows) != parent.model_calls
        or any(row["status"] != 200 for row in client.rows)
    ):
        raise BridgeError("REAL_TOOL_RECEIPT_PREFLIGHT_NOT_VERIFIED")
    if secret_leak(result["answer"], [config.api_key]):
        raise BridgeError("POTENTIAL_SECRET_WITHHELD")
    return {
        "passed": True,
        "dsh_version": result["version"],
        "dsh_session_id": result["sessionId"],
        "actual_models": native["served_models"],
        "model_requests": parent.model_calls,
        "tool_calls": parent.tool_calls,
        "actual_tool_output_in_provider_request": True,
        "verbatim_terminal_marker": True,
        "provider_store": False,
        "transport": "DSH stdio to native Python Responses; Node has no provider credential",
        "driver_sha256": file_hash(DRIVER),
        "bridge_sha256": file_hash(Path(__file__)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if not args.name.replace("-", "").isascii() or not args.name.replace("-", "").isalnum():
        parser.error("name may contain only letters, digits and hyphens")
    directory = RUNTIME / "luna-retests" / args.name
    directory.mkdir(parents=True, exist_ok=False)
    try:
        report = asyncio.run(probe(directory))
    except Exception as exc:  # noqa: BLE001 - persist a safe failure code, not provider diagnostics
        report = {
            "passed": False,
            "error": str(exc) if isinstance(exc, BridgeError) else type(exc).__name__,
        }
    save_new(directory / "dsh-preflight.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
