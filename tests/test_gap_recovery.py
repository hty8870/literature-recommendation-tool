from __future__ import annotations

from types import SimpleNamespace

import pytest
import tools.gap_baseline_repair as recovery
from tools.gap_benchmark.metrics import parse_answer
from tools.kimi_evaluation import DEFAULT_ALIAS, DEFAULT_EXPECTED_MODEL, actual_model_matches


def test_future_kimi_evaluation_requires_real_luna_model_not_just_alias():
    assert DEFAULT_ALIAS == "OpenAI/gpt-6-luna"
    assert DEFAULT_EXPECTED_MODEL == "gpt-6-luna"
    assert actual_model_matches(["gpt-6-luna"], DEFAULT_EXPECTED_MODEL)
    assert not actual_model_matches([], DEFAULT_EXPECTED_MODEL)
    assert not actual_model_matches(["deepseek-flash"], DEFAULT_EXPECTED_MODEL)
    assert not actual_model_matches(["gpt-6-luna", "deepseek-flash"], DEFAULT_EXPECTED_MODEL)


def test_recovery_model_pin_is_process_only_and_keeps_other_environment(monkeypatch):
    environment = {"EXISTING_RUNTIME_SETTING": "keep", "KIMI_CODE_HOME": "unchanged-home"}
    monkeypatch.setattr(recovery.os, "environ", environment)
    monkeypatch.setattr(recovery, "model_secrets", lambda: ["fixture-not-a-secret"])
    monkeypatch.setattr(
        recovery,
        "load_model_config",
        lambda _: SimpleNamespace(
            model="deepseek-flash",
            api_key="fixture-not-a-secret",
            base_url="https://api.deepseek.com",
        ),
    )
    recovery.pin_process_model()
    assert environment["KIMI_MODEL_NAME"] == "deepseek-flash"
    assert environment["KIMI_MODEL_API_KEY"] == "fixture-not-a-secret"
    assert environment["KIMI_MODEL_THINKING_EFFORT"] == "max"
    assert environment["KIMI_MODEL_PROVIDER_TYPE"] == "openai"
    assert environment["EXISTING_RUNTIME_SETTING"] == "keep"
    assert environment["KIMI_CODE_HOME"] == "unchanged-home"


def test_recovery_does_not_silently_use_another_model_when_configuration_missing(monkeypatch):
    monkeypatch.setattr(recovery, "model_secrets", lambda: [])
    monkeypatch.setattr(recovery, "load_model_config", lambda _: None)
    with pytest.raises(ValueError, match="not configured"):
        recovery.pin_process_model()


def test_original_strict_contract_keeps_overlong_list_failure_visible():
    import json

    text = json.dumps(
        {
            "papers": [
                {
                    "doc_id": str(index),
                    "stance": "INSUFFICIENT",
                    "quote": "actual text",
                    "reason": "only a type-check fixture",
                }
                for index in range(11)
            ],
            "conclusion": "no scientific assertions in this fixture",
        }
    )
    answer, errors = parse_answer(text)
    assert answer is None
    assert errors == [{"path": ["papers"], "type": "too_long"}]
