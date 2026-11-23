"""Explicitly authorized Luna provisioning through Kimi's credential export and systemd."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from tools.kimi_chat_model import resolve_kimi_command

from citation_tool.reasons import DeepSeekModelGateway, ModelConfig

ALIAS = "OpenAI/gpt-6-luna"
MODEL = "gpt-6-luna"


def authorized_config() -> ModelConfig:
    result = subprocess.run(
        [*resolve_kimi_command(), "provider", "list", "--json"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode or len(result.stdout) > 5_000_000:
        raise ValueError("authorized provider export failed")
    try:
        exported = json.loads(result.stdout)
        model = exported["models"][ALIAS]
        provider = exported["providers"]["OpenAI"]
        endpoint = urlparse(provider["baseUrl"])
        key = provider["apiKey"]
    except (KeyError, TypeError, ValueError):
        raise ValueError("authorized Luna configuration is incomplete") from None
    if (
        model.get("provider") != "OpenAI"
        or model.get("model") != MODEL
        or provider.get("type") != "openai_responses"
        or endpoint.scheme != "https"
        or endpoint.hostname != "api.openai.com"
        or endpoint.port not in {None, 443}
        or endpoint.path.rstrip("/") not in {"", "/v1"}
        or any((endpoint.username, endpoint.password, endpoint.query, endpoint.fragment))
        or not isinstance(key, str)
        or not key.strip()
    ):
        raise ValueError("authorized Luna identity, official endpoint or credential is invalid")
    if model.get("baseUrl") or model.get("protocol") not in {None, "openai_responses"}:
        raise ValueError("unexpected model routing override")
    config = ModelConfig(
        api_key=key,
        provider="openai",
        model=MODEL,
        base_url="https://api.openai.com/v1",
        api_type="responses",
    )
    config.validate()
    return config


def metadata(config: ModelConfig) -> dict:
    return {
        "provider": config.provider,
        "model": config.model,
        "api_type": config.api_type,
        "endpoint": config.base_url,
        "credential_present": bool(config.api_key),
    }


def _text(message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(
        block.get("text", "")
        for block in message.content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


async def probe(config: ModelConfig) -> dict:
    gateway = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=180_000)
    tool = {
        "name": "deployment_ping",
        "description": "Return the exact supplied public connectivity marker.",
        "parameters": {
            "type": "object",
            "properties": {"marker": {"type": "string"}},
            "required": ["marker"],
            "additionalProperties": False,
        },
    }
    human = HumanMessage("Call deployment_ping once with marker LUNA_NATIVE_OK.")
    model = gateway.agent_model()
    call = await model.bind_tools([tool], tool_choice="deployment_ping").ainvoke([human])
    if len(call.tool_calls) != 1 or call.tool_calls[0]["name"] != "deployment_ping":
        raise ValueError("native tool request missing")
    action = call.tool_calls[0]
    if action["args"] != {"marker": "LUNA_NATIVE_OK"}:
        raise ValueError("native tool arguments differ")
    reply = await model.ainvoke(
        [human, call, ToolMessage(content="LUNA_NATIVE_OK", tool_call_id=action["id"])]
    )
    if "LUNA_NATIVE_OK" not in _text(reply):
        raise ValueError("native tool result not consumed")
    structured = await gateway.reason_model().ainvoke(
        [
            SystemMessage('Return a JSON object with exactly {"status":"ok"}.'),
            HumanMessage("Confirm this public connectivity test in JSON."),
        ]
    )
    if json.loads(_text(structured)) != {"status": "ok"}:
        raise ValueError("native JSON output differs")
    observed = [
        item.response_metadata.get("model_name") or item.response_metadata.get("model")
        for item in (call, reply, structured)
    ]
    if observed != [MODEL, MODEL, MODEL]:
        raise ValueError("native response model identity differs")
    return {
        **metadata(config),
        "status": "passed",
        "observed_models": observed,
        "native_tool_roundtrip": True,
        "json_mode": True,
        "provider_storage_requested": False,
        "boundary": "Native protocol connectivity only, not a scientific quality evaluation.",
    }


def provision(config: ModelConfig) -> dict:
    from tools.deploy_citation import remote

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    credential_path = f"/etc/credstore.encrypted/citation-luna-{stamp}.json"
    payload = json.dumps(
        {
            "provider": config.provider,
            "base_url": config.base_url,
            "model": config.model,
            "api_type": config.api_type,
            "api_key": config.api_key,
        }
    ).encode()
    remote("sudo install -d -m 0700 /etc/credstore.encrypted")
    remote(
        f"sudo test ! -e {credential_path} && sudo systemd-creds encrypt "
        f"--name=model.local.json --with-key=host - {credential_path} "
        f"&& sudo chmod 0600 {credential_path}",
        payload,
    )
    return {**metadata(config), "encrypted_credential": credential_path, "activated": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "probe", "provision"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        config = authorized_config()
        result = (
            asyncio.run(probe(config))
            if args.action == "probe"
            else provision(config)
            if args.action == "provision"
            else metadata(config)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
