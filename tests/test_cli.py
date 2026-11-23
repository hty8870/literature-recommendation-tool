from __future__ import annotations

import os

import pytest
import uvicorn

from citation_tool.cli import main


def test_start_uses_the_same_single_worker_app_factory(monkeypatch):
    invocations = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **opts: invocations.append((app, opts)))
    assert main([]) == 0
    assert invocations == [
        (
            "citation_tool.api:create_app",
            {"factory": True, "host": "127.0.0.1", "port": 8765, "workers": 1},
        )
    ]


def test_startup_check_does_not_open_deployed_databases(tmp_path, monkeypatch, capsys):
    for name in tuple(os.environ):
        if name.startswith("CITATION_"):
            monkeypatch.delenv(name)
    app_db, auth_db = (tmp_path / name for name in ("app.db", "auth.db"))
    for path in (app_db, auth_db):
        path.write_bytes(b"not to be opened by the startup check")
    monkeypatch.setenv("CITATION_DB_PATH", str(app_db))
    monkeypatch.setenv("CITATION_AUTH_DB_PATH", str(auth_db))
    monkeypatch.setenv("CITATION_MODEL_CONFIG", str(tmp_path / "missing-model.json"))
    monkeypatch.setenv("CITATION_LIBRARY_MODE", "fixture")
    monkeypatch.setenv("CITATION_AUTH_REQUIRED", "1")
    monkeypatch.setenv("CITATION_ACCESS_TOKEN_SHA256", "a" * 64)
    assert main(["--check"]) == 0
    assert "Sources: fixture-fast, fixture-slow" in capsys.readouterr().out
    for path in (app_db, auth_db):
        assert path.read_bytes() == b"not to be opened by the startup check"
    monkeypatch.setenv("CITATION_LIBRARY_MODE", "invalid")
    with pytest.raises(SystemExit) as error:
        main(["--check"])
    assert error.value.code == 1
    assert "library_mode must be fixture or metadata" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("variable", "value", "reason"),
    [
        ("CITATION_LIBRARY_MODE", "fixture", "模拟库"),
        ("CITATION_CANDIDATE_PIPELINE", "pointwise", "列表重排"),
        ("CITATION_EUROPEPMC_ONLINE", "0", "在线库未开启"),
        ("CITATION_AUTH_REQUIRED", "1", "访问令牌"),
        ("CITATION_DEMO_CONTINUITY", "1", "未加载默认模型"),
    ],
)
def test_live_check_fails_closed_on_non_demo_profiles(
    tmp_path, monkeypatch, capsys, variable, value, reason
):
    from citation_tool import live_check

    async def unexpected_probe(*args):
        pytest.fail("the live probe should not call external services for invalid profiles")

    for name in tuple(os.environ):
        if name.startswith("CITATION_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CITATION_MODEL_CONFIG", str(tmp_path / "missing-model.json"))
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(live_check, "_run_probe", unexpected_probe)
    with pytest.raises(SystemExit) as error:
        main(["--check-live"])
    assert error.value.code == 1
    assert reason in capsys.readouterr().err


def test_wrong_model_config_type_has_actionable_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "broken.json"
    path.write_text('{"api_key": null}', encoding="utf-8")
    monkeypatch.setenv("CITATION_MODEL_CONFIG", str(path))
    monkeypatch.setenv("CITATION_LIBRARY_MODE", "fixture")
    with pytest.raises(SystemExit) as error:
        main(["--check"])
    assert error.value.code == 1
    assert "model credential fields must be strings" in capsys.readouterr().err
