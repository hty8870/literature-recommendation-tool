from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from citation_tool import model_egress
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.model_egress import PublicModelTransport, normalize_model_base
from citation_tool.reasons import DeepSeekModelGateway, UserModelSelection

TEST_KEY = "fixture-credential-not-a-real-key"  # noqa: S105
ENDPOINT = "https://provider.example.net/v1"


class FixtureBytes(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        yield self.content


def fixture_transport(handler):
    def streamed(request):
        result = handler(request)
        return httpx.Response(
            result.status_code, headers=result.headers, stream=FixtureBytes(result.content)
        )

    return httpx.MockTransport(streamed)


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example.net/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://169.254.169.254/v1",
        "https://10.0.0.1/v1",
        "https://[::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https://provider.example.net:444/v1",
        "https://user:pw@provider.example.net/v1",
        "https://provider.example.net/v1?debug=1",
        "https://provider.example.net/v1#x",
        "https://provider.example.net/%2e%2e/v1",
        "https://provider.example.net/v1/responses",
    ],
)
def test_custom_invalid_endpoint_values_are_rejected(url):
    with pytest.raises(ValueError):
        normalize_model_base(url, "chat_completions")


def config_for(protocol, url=ENDPOINT):
    return UserModelSelection(
        api_type=protocol, base_url=url, model="acceptance-model", api_key=TEST_KEY
    ).to_model_config()


def test_endpoint_identity_uses_normalized_protocol_and_address():
    normalized = config_for(
        "chat_completions", "https://PROVIDER.example.net:443/v1/chat/completions/"
    )
    assert normalized.base_url == ENDPOINT
    assert normalized.cache_fields == config_for("chat_completions").cache_fields
    assert normalized.cache_fields != config_for("responses").cache_fields
    assert (
        normalized.cache_fields
        != config_for("chat_completions", "https://other.example.net/v1").cache_fields
    )
    assert TEST_KEY not in repr(normalized)


async def test_dns_mixed_answers_never_reach_transport(monkeypatch):
    async def mixed(*_args, **_kwargs):
        return [
            (None, None, None, None, ("8.8.8.8", 443)),
            (None, None, None, None, ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", mixed)
    transport = PublicModelTransport(ENDPOINT, "responses", timeout_s=2, api_key=TEST_KEY)
    with pytest.raises(httpx.ConnectError, match="only to public"):
        await transport.handle_async_request(
            httpx.Request("POST", ENDPOINT + "/responses", json={})
        )


@pytest.mark.parametrize("mode", ["redirect", "auth_error", "oversized", "secret_echo"])
async def test_egress_bounds_redirects_and_upstream_diagnostics(monkeypatch, mode):
    calls = []

    async def resolve(host):
        assert host == "provider.example.net"
        return ["8.8.8.8"]

    def reply(request):
        calls.append(request)
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://127.0.0.1/"}, text=TEST_KEY)
        if mode == "auth_error":
            return httpx.Response(401, text=TEST_KEY)
        if mode == "oversized":
            return httpx.Response(200, content=b"a" * 128)
        return httpx.Response(200, text=TEST_KEY)

    monkeypatch.setattr(model_egress, "_resolve_public", resolve)
    monkeypatch.setattr(
        model_egress.httpx, "AsyncHTTPTransport", lambda **_: fixture_transport(reply)
    )
    monkeypatch.setattr(model_egress, "_RESPONSE_LIMIT", 64)
    async with httpx.AsyncClient(
        transport=PublicModelTransport(ENDPOINT, "responses", timeout_s=2, api_key=TEST_KEY),
        follow_redirects=True,
        headers={"OpenAI-Project": "fixture-project", "Cookie": "fixture-cookie=discard"},
    ) as client:
        if mode in {"oversized", "secret_echo"}:
            with pytest.raises(httpx.ReadError):
                await client.post(ENDPOINT + "/responses", json={})
        else:
            result = await client.post(ENDPOINT + "/responses", json={})
            assert result.status_code == (502 if mode == "redirect" else 401)
            assert TEST_KEY not in result.text
    assert len(calls) == 1
    assert calls[0].url.host == "8.8.8.8"
    assert calls[0].headers["Host"] == "provider.example.net"
    assert calls[0].extensions["sni_hostname"] == "provider.example.net"
    assert "openai-project" not in calls[0].headers
    assert "cookie" not in calls[0].headers


def chat_reply(text):
    return {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "model": "acceptance-model",
        "created": 1,
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
    }


def response_reply(text):
    return {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 1,
        "model": "acceptance-model",
        "status": "completed",
        "output": [
            {
                "id": "msg_fixture",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
    }


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_custom_gateway_uses_real_sdk_protocol_for_reason_ranking_and_citations(
    monkeypatch, protocol
):
    requests = []

    async def resolve(_host):
        return ["8.8.8.8"]

    def wire(request):
        requests.append((request, json.loads(request.content)))
        payload = (
            response_reply('{"hello":"world"}')
            if protocol == "responses"
            else chat_reply('{"hello":"world"}')
        )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(model_egress, "_resolve_public", resolve)
    monkeypatch.setattr(
        model_egress.httpx, "AsyncHTTPTransport", lambda **_: fixture_transport(wire)
    )
    gateway = DeepSeekModelGateway(config_for(protocol), max_inflight=2, timeout_ms=2000)
    for factory in [
        gateway.agent_model,
        gateway.reason_model,
        gateway.rerank_model,
        gateway.citation_model,
        gateway.review_model,
    ]:
        reply = await factory().ainvoke([HumanMessage(content="Return JSON.")])
        assert json.loads(reply.text) == {"hello": "world"}
    for index, (request, body) in enumerate(requests):
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        assert body["model"] == "acceptance-model"
        if protocol == "responses":
            assert request.url.path == "/v1/responses" and body["store"] is False
            if index > 0:
                assert body["text"]["format"]["type"] == "json_object"
        else:
            assert request.url.path == "/v1/chat/completions"
            if index > 0:
                assert body["response_format"]["type"] == "json_object"


async def test_invalid_endpoint_causes_no_agent_run(tmp_path):
    app = create_app(Settings.for_tests(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        reply = await client.post(
            "/api/v1/agent/runs",
            json={
                "input_type": "keywords",
                "keywords": ["plants"],
                "model": {
                    "api_type": "responses",
                    "base_url": "https://127.0.0.1",
                    "model": "test",
                    "api_key": TEST_KEY,
                },
            },
        )
        assert reply.status_code == 400 and reply.json()["code"] == "MODEL_CONFIG_INVALID"
        assert TEST_KEY not in reply.text


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_custom_model_tool_call_and_result_roundtrip(monkeypatch, protocol):
    wire_calls = []
    binding = {
        "type": "function",
        "function": {
            "name": "read_marker",
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
        },
    }

    async def resolve(_host):
        return ["8.8.8.8"]

    def wire(request):
        wire_calls.append(json.loads(request.content))
        if protocol == "responses":
            response = response_reply("Fixture roundtrip complete.")
            if len(wire_calls) == 1:
                response["output"] = [
                    {
                        "id": "fc_fixture",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_fixture",
                        "name": "read_marker",
                        "arguments": '{"label":"a"}',
                    }
                ]
        else:
            response = chat_reply("Fixture roundtrip complete.")
            if len(wire_calls) == 1:
                response["choices"][0].update(
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_fixture",
                                    "type": "function",
                                    "function": {
                                        "name": "read_marker",
                                        "arguments": '{"label":"a"}',
                                    },
                                }
                            ],
                        },
                    }
                )
        return httpx.Response(200, json=response)

    monkeypatch.setattr(model_egress, "_resolve_public", resolve)
    monkeypatch.setattr(
        model_egress.httpx, "AsyncHTTPTransport", lambda **_: fixture_transport(wire)
    )
    model = DeepSeekModelGateway(
        config_for(protocol), max_inflight=2, timeout_ms=2000
    ).agent_model()
    bound = model.bind_tools([binding])
    question = HumanMessage(content="Read the fixture marker.")
    call = await bound.ainvoke([question])
    assert call.tool_calls[0]["name"] == "read_marker"
    assert call.tool_calls[0]["args"] == {"label": "a"}
    reply = await bound.ainvoke(
        [question, call, ToolMessage("marker-a", tool_call_id="call_fixture")]
    )
    assert reply.text == "Fixture roundtrip complete."
    result = wire_calls[1]["input" if protocol == "responses" else "messages"][-1]
    assert (
        (result["call_id"], result["output"]) == ("call_fixture", "marker-a")
        if protocol == "responses"
        else (
            result["tool_call_id"],
            result["content"],
        )
        == ("call_fixture", "marker-a")
    )
