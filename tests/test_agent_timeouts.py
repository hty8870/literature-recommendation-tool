from dataclasses import replace
from types import SimpleNamespace

import pytest

from citation_tool.agent import AgentRunner
from citation_tool.config import Settings
from citation_tool.runtime import settings_from_env


@pytest.mark.parametrize(
    ("kind", "expected"), [("keywords", 900_000), ("abstract", 900_000), ("full_text", 1_800_000)]
)
def test_full_text_has_its_own_envelope_without_changing_abstract_benchmarks(kind, expected):
    runner = SimpleNamespace(settings=Settings())
    assert AgentRunner._timeout_ms(runner, SimpleNamespace(input_type=kind)) == expected


def test_full_text_envelope_is_explicitly_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("CITATION_AGENT_FULL_TEXT_TIMEOUT_MS", "2400000")
    assert settings_from_env().agent_full_text_timeout_ms == 2_400_000
    with pytest.raises(ValueError, match="agent_full_text_timeout_ms"):
        replace(Settings(), agent_full_text_timeout_ms=3_600_001).validate()
