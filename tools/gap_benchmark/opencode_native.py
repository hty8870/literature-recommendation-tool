"""Native model transport for the public SciFact OpenCode retest, not the deployed API."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field
from tools.benchmark.core import secret_leak
from tools.gap_benchmark.opencode_baseline import MODELS
from tools.kimi_chat_model import resolve_kimi_command

from citation_tool.jcs import sha256_json

ROUTES = {
    "deepseek-v4.1-flash": ("http://127.0.0.1:8339/go/v1", "/chat/completions", "openai"),
    "qwen3.8-flash": ("https://opencode.ai/zen", "/v1/messages", "anthropic"),
}
TOKEN_LIMITS = {"parent": 8192, "source": 4096, "rank": 14000, "reason": 4096, "review": 6144}


@dataclass(frozen=True)
class Route:
    model: str
    provider: str
    base_url: str
    path: str
    protocol: str
    api_key: str = field(repr=False)


def configured_route(model: str) -> Route:
    alias = MODELS[model]
    provider = alias.split("/", 1)[0]
    base_url, path, protocol = ROUTES[model]
    result = subprocess.run(
        [*resolve_kimi_command(), "provider", "list", "--json"],
        capture_output=True,
        check=False,
        timeout=20,
    )
    if result.returncode or len(result.stdout) > 5_000_000:
        raise ValueError("configured provider lookup failed")
    try:
        export = json.loads(result.stdout)
        model_config, provider_config = export["models"][alias], export["providers"][provider]
        if (
            model_config["provider"] != provider
            or model_config["model"] != model
            or (model_config.get("protocol") or provider_config["type"]) != protocol
            or (model_config.get("baseUrl") or provider_config["baseUrl"]).rstrip("/") != base_url
        ):
            raise ValueError("route or model identity changed")
        key = provider_config["apiKey"]
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError("missing provider authorization")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("configured OpenCode route is missing or changed") from None
    return Route(model, provider, base_url, path, protocol, key)


def chat_messages(messages: list[BaseMessage]) -> list[dict]:
    result = []
    for message in messages:
        if isinstance(message, AIMessage):
            row: dict = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                row["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["args"], ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            if continuation := message.additional_kwargs.get("opencode_continuation"):
                row["reasoning_content"] = continuation
        elif isinstance(message, ToolMessage):
            row = {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.text}
        else:
            row = {
                "role": "system" if isinstance(message, SystemMessage) else "user",
                "content": message.text,
            }
        result.append(row)
    return result


def anthropic_messages(messages: list[BaseMessage]) -> tuple[str, list[dict]]:
    instructions, result = [], []
    for message in messages:
        if isinstance(message, SystemMessage):
            instructions.append(message.text)
            continue
        role = "assistant" if isinstance(message, AIMessage) else "user"
        if isinstance(message, ToolMessage):
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.text,
                    **({"is_error": True} if message.status == "error" else {}),
                }
            ]
        elif isinstance(message, AIMessage) and (
            continuation := message.additional_kwargs.get("opencode_continuation")
        ):
            blocks = continuation
        else:
            blocks = [{"type": "text", "text": message.text}] if message.text else []
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": list(blocks)})
    return "\n\n".join(instructions), result


def reply(body: dict, protocol: str) -> tuple[AIMessage, int]:
    observed = body.get("model")
    response_id = body.get("id")
    if not isinstance(observed, str) or not isinstance(response_id, str) or not response_id:
        raise ValueError("provider response lacks model or request identity")
    tool_calls, invalid = [], []
    measure = body.get("usage") or {}
    if protocol == "anthropic":
        blocks = body["content"]
        text = "".join(block["text"] for block in blocks if block["type"] == "text")
        calls = [
            (block["name"], block["id"], block["input"])
            for block in blocks
            if block["type"] == "tool_use"
        ]
        continuation = [
            block
            for block in blocks
            if block["type"] in {"text", "thinking", "redacted_thinking", "tool_use"}
        ]
        hidden_blocks = sum(b["type"] in {"thinking", "redacted_thinking"} for b in continuation)
        finish = body.get("stop_reason")
        cache_read = measure.get("cache_read_input_tokens", 0) or 0
        cache_write = measure.get("cache_creation_input_tokens", 0) or 0
        prompt_tokens = (measure.get("input_tokens", 0) or 0) + cache_read + cache_write
        output_tokens = measure.get("output_tokens", 0) or 0
    else:
        choice = body["choices"][0]
        response = choice["message"]
        text = response.get("content") or ""
        calls = [
            (call["function"]["name"], call["id"], call["function"].get("arguments"))
            for call in response.get("tool_calls") or []
        ]
        continuation = response.get("reasoning_content")
        hidden_blocks = int(bool(continuation))
        finish = choice.get("finish_reason")
        prompt_tokens = measure.get("prompt_tokens", 0) or 0
        output_tokens = measure.get("completion_tokens", 0) or 0
        cache_read = (measure.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        cache_write = 0
    for name, call_id, args in calls:
        try:
            parsed = json.loads(args) if isinstance(args, str) else args
            if not isinstance(parsed, dict):
                raise ValueError("arguments are not an object")
            tool_calls.append({"name": name, "id": call_id, "args": parsed, "type": "tool_call"})
        except ValueError:
            invalid.append({"name": name, "id": call_id, "args": None, "error": "invalid JSON"})
    usage = (
        {
            "input_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens,
            "input_token_details": {"cache_read": cache_read, "cache_creation": cache_write},
        }
        if measure
        else None
    )
    return AIMessage(
        content=text,
        id=response_id,
        tool_calls=tool_calls,
        invalid_tool_calls=invalid,
        additional_kwargs={"opencode_continuation": continuation} if continuation else {},
        response_metadata={"model_name": observed, "finish_reason": finish},
        usage_metadata=usage,
    ), hidden_blocks


class ModelFailure(RuntimeError):
    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def _safe_text(value: str, key: str) -> str:
    return "[WITHHELD]" if secret_leak(value, [key]) else value


def visible_parent_context(messages: list[BaseMessage], key: str) -> dict:
    tool_results = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        text = _safe_text(message.text, key)
        try:
            content = json.loads(text)
        except ValueError:
            content = text
        tool_results.append(
            {
                "name": message.name,
                "tool_call_id": message.tool_call_id,
                "content": content,
            }
        )
    return {
        "system": [
            _safe_text(message.text, key)
            for message in messages
            if isinstance(message, SystemMessage)
        ],
        "user": [_safe_text(message.text, key) for message in messages if message.type == "human"],
        "tool_results": tool_results,
    }


class OpenCodeGateway:
    def __init__(self, config: Route, *, parent_max_tokens: int = TOKEN_LIMITS["parent"]) -> None:
        if parent_max_tokens < 1:
            raise ValueError("parent_max_tokens must be positive")
        self.config = config
        self.parent_max_tokens = parent_max_tokens
        self.session = "lit-scifact-" + uuid.uuid4().hex
        self.slots = asyncio.Semaphore(2)
        self.calls: list[dict] = []
        self.parent_turns: list[dict] = []
        self.exit_error: str | None = None
        headers = {"user-agent": "citation-benchmark/1.0"}
        if config.protocol == "openai":
            headers.update(
                {"authorization": f"Bearer {config.api_key}", "x-opencode-session": self.session}
            )
        else:
            headers.update({"x-api-key": config.api_key, "anthropic-version": "2023-06-01"})
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            follow_redirects=False,
            timeout=httpx.Timeout(260, connect=25),
            trust_env=config.protocol == "anthropic",
        )

    def model_for(self, stage: str, *, json_mode: bool | None = None) -> OpenCodeModel:
        return OpenCodeModel(
            gateway=self,
            model_name=self.config.model,
            stage=stage,
            json_mode=stage not in {"source", "parent"} if json_mode is None else json_mode,
        )

    def agent_model(self) -> OpenCodeModel:
        return self.model_for("parent")

    def reason_model(self) -> OpenCodeModel:
        return self.model_for("reason")

    def rerank_model(self) -> OpenCodeModel:
        return self.model_for("rank")

    def review_model(self) -> OpenCodeModel:
        return self.model_for("review")

    def citation_model(self) -> OpenCodeModel:
        return self.model_for("reason")

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        messages: list[BaseMessage],
        stage: str,
        *,
        tools: list[dict],
        tool_choice: Any,
        json_mode: bool,
        stop: list[str] | None,
    ) -> AIMessage:
        model, protocol = self.config.model, self.config.protocol
        max_tokens = self.parent_max_tokens if stage == "parent" else TOKEN_LIMITS[stage]
        payload: dict = {"model": model, "max_tokens": max_tokens}
        if protocol == "openai":
            payload["messages"] = chat_messages(messages)
            if tools:
                payload.update(tools=tools, tool_choice=tool_choice or "auto")
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            if stop:
                payload["stop"] = stop
        else:
            system, history = anthropic_messages(messages)
            payload["messages"] = history
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = [
                    {
                        "name": f["name"],
                        "description": f.get("description", ""),
                        "input_schema": f["parameters"],
                    }
                    for item in tools
                    for f in [item["function"]]
                ]
                if tool_choice not in {None, "auto"}:
                    raise ValueError("only native auto choice is supported for Messages")
            if stop:
                payload["stop_sequences"] = stop
        start = time.monotonic()
        row: dict = {"stage": stage, "status": "queued", "model_observed": []}
        self.calls.append(row)
        try:
            async with self.slots:
                served = await self.client.post(
                    self.config.base_url + self.config.path, json=payload
                )
            row["status_code"] = served.status_code
            if not served.is_success:
                raise ModelFailure("OPENCODE_HTTP_ERROR", status=served.status_code)
            try:
                message, hidden_blocks = reply(served.json(), protocol)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                raise ModelFailure("OPENCODE_BAD_RESPONSE") from None
            observed = message.response_metadata["model_name"]
            row["model_observed"] = [
                observed
                if re.fullmatch(r"[a-zA-Z0-9._/-]{1,120}", observed)
                and observed != self.config.api_key
                else "unverified"
            ]
            if observed != model:
                raise ModelFailure("OPENCODE_MODEL_MISMATCH")
            row.update(
                status="completed",
                response_id=_safe_text(message.id or "", self.config.api_key),
                finish=message.response_metadata["finish_reason"],
                usage=message.usage_metadata or {},
                reasoning_segments_present=hidden_blocks,
                tool_names=[call["name"] for call in message.tool_calls],
            )
            if stage == "parent":
                self.parent_turns.append(
                    {
                        **visible_parent_context(messages, self.config.api_key),
                        "visible_output": _safe_text(message.text, self.config.api_key),
                        "tool_calls": json.loads(
                            _safe_text(json.dumps(message.tool_calls), self.config.api_key)
                        )
                        if not secret_leak(json.dumps(message.tool_calls), [self.config.api_key])
                        else [],
                        "finish": row["finish"],
                        "response_id": row["response_id"],
                    }
                )
            return message
        except asyncio.CancelledError:
            row["status"] = "cancelled"
            raise
        except (httpx.HTTPError, ModelFailure) as exc:
            row.update(status="failed", error=getattr(exc, "code", type(exc).__name__))
            if isinstance(exc, ModelFailure):
                raise
            raise ModelFailure("OPENCODE_TRANSPORT_ERROR") from None
        finally:
            row["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)

    def audit(self) -> dict:
        calls = list(self.calls)
        responses = [item["response_id"] for item in calls if item.get("response_id")]
        models = sorted({model for item in calls for model in item["model_observed"]})
        usage = {
            key: sum((item.get("usage") or {}).get(key, 0) for item in calls)
            for key in ("input_tokens", "output_tokens")
        }
        return {
            "artifacts": calls,
            "artifact_errors": [],
            "artifact_count": len(calls),
            "model_calls": len(calls),
            "usage": usage,
            "served_models": models,
            "model_valid": bool(responses) and all(item["status"] == "completed" for item in calls),
            "session_isolation_valid": bool(responses) and len(responses) == len(set(responses)),
            "session_ids": [self.session],
        }


class OpenCodeModel(BaseChatModel):
    gateway: OpenCodeGateway = Field(exclude=True, repr=False)
    model_name: str
    stage: str
    json_mode: bool = False

    @property
    def _llm_type(self) -> str:
        return "opencode-benchmark-native"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(
            tools=[convert_to_openai_tool(tool) for tool in tools],
            tool_choice=tool_choice,
            **kwargs,
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("the benchmark transport is async-only")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        message = await self.gateway.request(
            messages,
            self.stage,
            tools=kwargs.get("tools") or [],
            tool_choice=kwargs.get("tool_choice"),
            json_mode=self.json_mode,
            stop=stop,
        )
        return ChatResult(
            generations=[ChatGeneration(message=message)],
            llm_output={"model_name": message.response_metadata["model_name"]},
        )


def trace_fingerprint(turn: dict) -> str:
    return sha256_json(
        {"system": turn["system"], "user": turn["user"], "tools": turn["tool_results"]}
    )
