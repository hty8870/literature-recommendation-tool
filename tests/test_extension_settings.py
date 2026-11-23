from __future__ import annotations

from dataclasses import replace

import pytest

from citation_tool.config import Settings
from citation_tool.runtime import settings_from_env


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("manuscript_max_upload_bytes", 999),
        ("manuscript_max_upload_bytes", 50_000_001),
        ("manuscript_max_pages", 0),
        ("manuscript_max_pages", 301),
        ("manuscript_parse_timeout_s", 9),
        ("manuscript_parse_timeout_s", 901),
        ("manuscript_max_concurrent", 0),
        ("manuscript_max_concurrent", 3),
        ("citation_text_max_concurrent", 0),
        ("citation_text_max_concurrent", 5),
    ],
)
def test_unsafe_extension_capacity_fails_before_start(field, value):
    with pytest.raises(ValueError, match=field):
        replace(Settings(), **{field: value}).validate()


def test_extension_switch_and_limits_are_runtime_configurable(monkeypatch):
    monkeypatch.setenv("CITATION_MANUSCRIPT_UPLOAD_ENABLED", "false")
    monkeypatch.setenv("CITATION_MANUSCRIPT_MAX_BYTES", "1000000")
    monkeypatch.setenv("CITATION_MANUSCRIPT_MAX_PAGES", "10")
    monkeypatch.setenv("CITATION_MANUSCRIPT_PARSE_TIMEOUT_S", "60")
    monkeypatch.setenv("CITATION_MANUSCRIPT_MAX_CONCURRENT", "1")
    monkeypatch.setenv("CITATION_TEXT_MAX_CONCURRENT", "1")
    settings = settings_from_env()
    assert settings.manuscript_upload_enabled is False
    assert settings.manuscript_max_upload_bytes == 1_000_000
    assert settings.manuscript_max_pages == 10
    assert settings.manuscript_parse_timeout_s == 60
    assert settings.manuscript_max_concurrent == 1
    assert settings.citation_text_max_concurrent == 1


def test_unrecognized_extension_boolean_does_not_silently_enable_uploads(monkeypatch):
    monkeypatch.setenv("CITATION_MANUSCRIPT_UPLOAD_ENABLED", "enabled")
    with pytest.raises(ValueError, match="CITATION_MANUSCRIPT_UPLOAD_ENABLED"):
        settings_from_env()
