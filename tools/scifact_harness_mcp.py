"""Local, public-data benchmark adapter for swapping only the parent Agent loop."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import tempfile
from collections import Counter
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

import uvicorn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from tools.benchmark.core import secret_leak
from tools.benchmark.runners import KIMI_MAX_STEPS_PER_TURN, _parse_kimi_export, _parse_stream
from tools.kimi_chat_model import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_ALIAS,
    _child_env,
    _escape_profile_template,
    resolve_kimi_command,
)

from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.mcp import McpHandler

NAMESPACE = "scifacttrial"
PREFIX = f"mcp__{NAMESPACE}__"
PARENT_TOOLS = frozenset({"search_literature", "read_literature", "Read", "Grep", "Glob"})
MAX_REQUEST = 128 * 1024


class HarnessError(RuntimeError):
    pass


class ParentTools(McpHandler):
    def __init__(self, tools, *, search_limit: int):
        self.tools = {tool.name: tool for tool in tools}
        if len(tools) != len(self.tools) or not self.tools or self.tools.keys() - PARENT_TOOLS:
            raise ValueError("external harness tool set outside the literature allowlist")
        self.lock = asyncio.Lock()
        self.search_limit = search_limit
        self.searches = 0
        self.actions = []
        self.messages = []
        self.path = f"/mcp/{uuid4().hex}"
        self.app = Starlette(routes=[Route(self.path, self.post, methods=["POST"])])

    def _tools(self):
        return [
            {
                "name": spec["name"],
                "description": spec["description"],
                "inputSchema": spec["parameters"],
            }
            for tool in self.tools.values()
            for spec in [convert_to_openai_tool(tool)["function"]]
        ]

    async def _call(self, tenant_id, name, arguments):
        if not isinstance(name, str) or name not in self.tools:
            raise ValueError("tool not registered for this case")
        async with self.lock:
            if name == "search_literature":
                self.searches += 1
            try:
                if name == "search_literature" and self.searches > self.search_limit:
                    raise ValueError("benchmark search limit reached")
                result = await self.tools[name].ainvoke(arguments)
                if not isinstance(result, dict):
                    raise TypeError("expected a structured literature receipt")
            except (TypeError, ValueError) as exc:
                result = {"error": {"code": "TOOL_REQUEST_REJECTED", "type": type(exc).__name__}}
            call = {"name": name, "args": arguments, "id": uuid4().hex, "type": "tool_call"}
            self.messages += [
                AIMessage(content="", tool_calls=[call]),
                ToolMessage(
                    content=json.dumps(result, ensure_ascii=False),
                    name=name,
                    tool_call_id=call["id"],
                ),
            ]
            self.actions.append((name, arguments, result))
            return result

    async def post(self, request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST:
                return Response(status_code=413)
        try:
            message = json.loads(body)
        except (ValueError, UnicodeError):
            return Response(status_code=400)
        if not isinstance(message, dict) or not isinstance(message.get("params", {}), dict):
            return Response(status_code=400)
        result = await self.handle("already-scoped", message)
        if result is None:
            return Response(status_code=202)
        if "result" in result and "content" in result["result"]:
            result["result"].pop("structuredContent", None)
        return JSONResponse(result)


class LocalServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()


@asynccontextmanager
async def serve(bridge: ParentTools):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.setblocking(False)
        server = LocalServer(
            uvicorn.Config(
                bridge.app,
                lifespan="off",
                ws="none",
                access_log=False,
                log_config=None,
                proxy_headers=False,
                timeout_graceful_shutdown=5,
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise HarnessError("benchmark MCP exited during startup")
                    await asyncio.sleep(0.02)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}{bridge.path}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 8)


async def process(command, work: Path, env: dict[str, str], seconds: float):
    child = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(work),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(seconds):
            stdout, _ = await child.communicate()
    except (TimeoutError, asyncio.CancelledError):
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        await child.wait()
        raise
    return child.returncode, stdout.decode("utf-8", errors="replace")


def tool_trace_agrees(trace, actions) -> bool:
    expected = Counter((PREFIX + name, sha256_json(args)) for name, args, _ in actions)
    parsed = []
    try:
        for call in trace:
            args = call["arguments"]
            args = json.loads(args) if isinstance(args, str) else args
            if not isinstance(args, dict):
                return False
            parsed.append((call["name"], sha256_json(args)))
    except (ValueError, KeyError, TypeError):
        return False
    return Counter(parsed) == expected


def profile(prompt: str, names) -> str:
    entries = "\n".join(f"  - {PREFIX}{name}" for name in names)
    return (
        "---\nname: literature-trial\ndescription: Run the literature research task.\n"
        f"subagents: []\ntools:\n{entries}\n---\n"
        + _escape_profile_template(prompt)
        + "\nTool names have an MCP namespace; use only the tools actually supplied above."
    )


class KimiParent:
    def __init__(self, tools, system_prompt: str, *, search_limit: int = 32):
        self.bridge = ParentTools(tools, search_limit=search_limit)
        self.system_prompt = system_prompt
        self.session_id = None
        self.audit = {}
        self.messages_count = 0
        self.error = None

    async def ainvoke(self, state, config=None):
        messages = state["messages"]
        if not messages or not isinstance(messages[-1], HumanMessage):
            raise HarnessError("benchmark expects a user message")
        if self.session_id is None and len(messages) != 1:
            raise HarnessError("the prototype accepts single-turn public benchmark tasks only")
        prompt = messages[-1].content
        if not isinstance(prompt, str) or len(prompt.encode("utf-16-le")) > 40000:
            raise HarnessError("benchmark user prompt exceeds CLI envelope")
        checkpoint = len(self.bridge.messages)
        try:
            final = await self._run(prompt)
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, HarnessError) else type(exc).__name__
            raise
        return {"messages": [*messages, *self.bridge.messages[checkpoint:], final]}

    async def _run(self, prompt: str) -> AIMessage:
        if self.messages_count:
            raise HarnessError("second turn is not included in this single-turn harness trial")
        self.messages_count += 1
        command = resolve_kimi_command()
        env = _child_env()
        no_proxy = ",".join(filter(None, [env.get("NO_PROXY"), "localhost", "127.0.0.1"]))
        env.update(
            KIMI_CODE_TRUST_WORKSPACE="1",
            KIMI_LOOP_MAX_STEPS_PER_TURN=str(KIMI_MAX_STEPS_PER_TURN),
            NO_PROXY=no_proxy,
        )
        stamp = utc_now_iso()
        with tempfile.TemporaryDirectory(prefix="scifact-harness-") as temporary:
            work = Path(temporary)
            profile_path = work / "profile.md"
            profile_path.write_text(
                profile(self.system_prompt, self.bridge.tools), encoding="utf-8"
            )
            skills = work / "skills"
            skills.mkdir()
            config = work / ".kimi-code" / "mcp.json"
            config.parent.mkdir()
            async with serve(self.bridge) as url:
                config.write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                NAMESPACE: {
                                    "url": url,
                                    "enabledTools": list(self.bridge.tools),
                                    "startupTimeoutMs": 8000,
                                    "toolTimeoutMs": 600000,
                                    "deferred": False,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                status, stream = await process(
                    [
                        *command,
                        "-m",
                        DEFAULT_MODEL_ALIAS,
                        "--agent-file",
                        str(profile_path),
                        "--skills-dir",
                        str(skills),
                        "--output-format",
                        "stream-json",
                        "-p",
                        prompt,
                    ],
                    work,
                    env,
                    880,
                )
            parsed = _parse_stream(stream)
            session = parsed["session_id"]
            if secret_leak(stream, []):
                raise HarnessError("potential secret withheld")
            if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", session):
                raise HarnessError(f"Kimi process status {status}; no valid session receipt")
            self.session_id = session
            archive = work / "audit.zip"
            exported, _ = await process(
                [*command, "export", session, "--no-include-global-log", "-o", str(archive)],
                work,
                env,
                12,
            )
            if exported or not archive.is_file():
                raise HarnessError("Kimi session export failed")
            trace = await asyncio.to_thread(_parse_kimi_export, archive, stamp)
            matched = tool_trace_agrees(trace["tool_calls"], self.bridge.actions)
            self.audit = {
                "session_id": session,
                "process_status": status,
                "observed_models": trace["model_observed"],
                "aliases": trace["model_aliases"],
                "reasoning_efforts": trace["thinking_efforts"],
                "model_requests": trace["model_request_count"],
                "usage": trace["usage"],
                "usage_report_count": trace["usage_report_count"],
                "system_prompt_sha256": sha256_json(self.system_prompt),
                "user_prompt_sha256": sha256_json(prompt),
                "tool_schema_sha256": sha256_json(self.bridge._tools()),
                "tools_match_receipts": matched,
                "tool_names": [call["name"] for call in trace["tool_calls"]],
                "cli_version": parsed["cli_version"],
            }
            if (
                trace["model_observed"] != [DEFAULT_MODEL]
                or trace["model_aliases"] != [DEFAULT_MODEL_ALIAS]
                or trace["usage_report_count"] < 1
                or not matched
            ):
                raise HarnessError("Kimi model or tool audit mismatch")
            if status or not parsed["response_text"]:
                raise HarnessError(f"Kimi process status {status}; no verified final answer")
            return AIMessage(
                content=parsed["response_text"], response_metadata={"model_name": DEFAULT_MODEL}
            )
