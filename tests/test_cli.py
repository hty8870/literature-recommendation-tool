from __future__ import annotations

import os

import pytest
import uvicorn

from citation_tool.cli import main


def test_start_uses_the_same_single_worker_app_factory(monkeypatch):
    invocations = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **opts: invocations.append((app, opts)))
    assert main([]) == 0
    assert invocations == [(
        "citation_tool.api:create_app",
        {"factory": True, "host": "127.0.0.1", "port": 8765, "workers": 1},
    )]


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
