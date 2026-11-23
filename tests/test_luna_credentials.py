from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import tools.luna_credentials as credentials
from langchain_core.messages import AIMessage, HumanMessage

from citation_tool.reasons import (
    DeepSeekModelGateway,
    ModelConfig,
    load_model_config,
    validate_openai_proxy,
)


def export_fixture():
    return {
        "providers": {
            "OpenAI": {
                "type": "openai_responses",
                "baseUrl": "https://api.openai.com/v1",
                "apiKey": "unit-test-credential",
            },
            "unrelated": {"apiKey": "do-not-export-this"},
        },
        "models": {"OpenAI/gpt-6-luna": {"provider": "OpenAI", "model": "gpt-6-luna"}},
    }


def stub_export(monkeypatch, value):
    monkeypatch.setattr(credentials, "resolve_kimi_command", lambda: ("fake-kimi",))
    monkeypatch.setattr(
        credentials.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(value).encode(), stderr=b""
        ),
    )


def test_authorized_export_keeps_only_official_luna_and_masks_credential(monkeypatch):
    stub_export(monkeypatch, export_fixture())
    config = credentials.authorized_config()
    assert config.provider == "openai"
    assert config.api_type == "responses"
    assert config.model == "gpt-6-luna"
    assert config.api_key not in repr(config)
    assert config.api_key not in json.dumps(credentials.metadata(config))
    assert "do-not-export-this" not in json.dumps(credentials.metadata(config))


@pytest.mark.parametrize(
    "url", ["https://evil.example/v1", "http://api.openai.com/v1", "https://api.openai.com/v1?x=y"]
)
def test_authorized_export_rejects_endpoint_rerouting(monkeypatch, url):
    value = export_fixture()
    value["providers"]["OpenAI"]["baseUrl"] = url
    stub_export(monkeypatch, value)
    with pytest.raises(ValueError, match="invalid"):
        credentials.authorized_config()


def test_responses_gateway_maps_native_json_tools_and_privacy_controls():
    config = ModelConfig(
        api_key="unit-test-credential",  # noqa: S106
        provider="openai",
        model="gpt-6-luna",
        base_url="https://api.openai.com/v1",
        api_type="responses",
    )
    config.validate()
    gateway = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=180_000)
    payload = gateway.reason_model()._get_request_payload([HumanMessage("Return JSON.")])
    assert payload["text"]["format"] == {"type": "json_object"}
    assert payload["max_output_tokens"] == 350 + 8192
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "temperature" not in payload
    assert "messages" not in payload and payload["input"]
    assert "response_format" not in payload
    assert "api_key" not in payload


@pytest.mark.parametrize(
    "value",
    [
        "https://127.0.0.1:18768",
        "http://evil.example:18768",
        "http://127.0.0.1:0",
        "http://user@127.0.0.1:18768",
        "http://127.0.0.1:18768/private",
    ],
)
def test_model_proxy_is_loopback_only(value):
    with pytest.raises(ValueError):
        validate_openai_proxy(value)
    assert validate_openai_proxy("http://127.0.0.1:18768") == "http://127.0.0.1:18768"


def test_openai_proxy_is_scoped_to_that_provider(monkeypatch):
    monkeypatch.setenv("CITATION_OPENAI_PROXY", "http://127.0.0.1:18768")
    config = ModelConfig(
        api_key="unit-test-credential",
        provider="openai",
        model="gpt-6-luna",  # noqa: S106
        base_url="https://api.openai.com/v1",
        api_type="responses",
    )
    gateway = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=5000)
    assert gateway.agent_model().openai_proxy == "http://127.0.0.1:18768"
    assert gateway.reason_model().openai_proxy == "http://127.0.0.1:18768"
    monkeypatch.setenv("CITATION_OPENAI_PROXY", "http://not-loopback.example:80")
    deepseek = DeepSeekModelGateway(
        ModelConfig(api_key="unit-test-credential"),
        max_inflight=1,
        timeout_ms=5000,  # noqa: S106
    )
    assert deepseek.agent_model().openai_proxy is None
    with pytest.raises(ValueError):
        DeepSeekModelGateway(config, max_inflight=1, timeout_ms=5000)


async def test_reason_accepts_native_visible_text_blocks_without_reasoning_leak():
    from citation_tool.reasons import DeepSeekReasonGenerator
    from test_agent import ScriptedChatModel
    from test_units import make_candidate, make_claim

    text = "该候选摘要涉及研究主题，可供后续核查表达与方法，但不能据此认定已有全文证据或因果关系。"
    message = AIMessage(
        content=[
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private-marker"}]},
            {"type": "text", "text": json.dumps({"reason": text, "alignment_score": 2})},
        ],
        response_metadata={"model_name": "gpt-6-luna"},
    )
    gateway = DeepSeekModelGateway(
        ModelConfig(api_key="unit-test-credential"),
        max_inflight=1,
        timeout_ms=5000,  # noqa: S106
    )
    generator = DeepSeekReasonGenerator(
        gateway,
        max_inflight=1,
        timeout_ms=5000,
        model=ScriptedChatModel(responses=[message]),
    )
    reason = await generator.generate(make_claim(), make_candidate())
    assert reason.status == "generated"
    assert reason.text == text
    assert "private-marker" not in reason.model_dump_json()


def test_user_openai_selection_uses_native_responses_without_sampling_parameters():
    from citation_tool.reasons import UserModelSelection

    selection = UserModelSelection(provider="openai", model="gpt-6-luna", api_key="unit-fixture")
    config = selection.to_model_config()
    assert config.api_type == "responses"
    client = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=5000).agent_model()
    payload = client._get_request_payload([HumanMessage("Confirm.")])
    assert "temperature" not in payload
    assert payload["store"] is False


def test_response_configuration_is_loadable_and_other_providers_cannot_opt_in(tmp_path):
    path = tmp_path / "model-fixture.json"
    path.write_text(
        json.dumps(
            {
                "api_key": "unit-test-credential",
                "base_url": "https://api.openai.com/v1",
                "provider": "openai",
                "model": "gpt-6-luna",
                "api_type": "responses",
            }
        ),
        encoding="utf-8",
    )
    assert load_model_config(path).api_type == "responses"
    with pytest.raises(ValueError, match="requires the OpenAI"):
        ModelConfig(api_key="unit-test-credential", api_type="responses").validate()
