from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest
from tools import update_linux as update


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("CITATION_"):
            monkeypatch.delenv(name)
    return update.Deployment(tmp_path, wait=15)


def test_staged_code_keeps_runtime_paths_at_checkout(deployment, monkeypatch):
    monkeypatch.setenv("CITATION_DB_PATH", "db/demo.sqlite")
    env = update.service_environment(deployment.repo, deployment.releases)
    assert Path(env["CITATION_DB_PATH"]) == deployment.repo / "db/demo.sqlite"
    assert Path(env["CITATION_MODEL_CONFIG"]) == (
        deployment.repo / "data/runtime/deepseek.local.json"
    )
    monkeypatch.setenv("CITATION_DB_PATH", str(deployment.releases / "lost.sqlite"))
    with pytest.raises(update.UpdateError, match="独立数据目录"):
        update.service_environment(deployment.repo, deployment.releases)


def test_server_detection_distinguishes_checks_and_unrelated_processes():
    assert update.is_server_command(["uv", "run", "--frozen", "citation-tool"])
    assert update.is_server_command(["uvicorn", "citation_tool.api:create_app", "--factory"])
    assert not update.is_server_command(["citation-tool", "--check"])
    assert not update.is_server_command(["citation-tool", "--check-live"])
    assert not update.is_server_command(["python", "-m", "http.server", "8765"])


def test_real_worker_uses_a_free_port_and_only_stops_its_own_instance(deployment, monkeypatch):
    monkeypatch.setenv("CITATION_LIBRARY_MODE", "fixture")
    deployment.env = update.service_environment(deployment.repo, deployment.releases)
    monkeypatch.setattr(update, "interpreter", lambda _: Path(sys.executable))
    version = "a" * 40
    (deployment.releases / version).mkdir()
    with socket.create_server((update.HOST, 0)) as unrelated:
        port = unrelated.getsockname()[1]
        with pytest.raises(update.UpdateError, match="已被占用"):
            update.listening_socket([port])
        item = deployment.start(version, port, fallback=True)
        try:
            assert item.port != port
            assert update.healthy(item.port)
            assert deployment.running() == item
        finally:
            deployment.stop(item)
        assert deployment.running() is None
        assert unrelated.fileno() != -1
        assert unrelated.getsockname()[1] == port


def test_unhealthy_new_version_restores_previous_ready_version(deployment, monkeypatch):
    previous = update.Instance("a" * 40, 18800, "1" * 32, deployment.profile)
    target = "b" * 40
    events = []
    monkeypatch.setattr(deployment, "running", lambda: previous)
    monkeypatch.setattr(update, "healthy", lambda _: False)
    monkeypatch.setattr(update, "locked", lambda _: False)
    monkeypatch.setattr(deployment, "target", lambda _: target)
    monkeypatch.setattr(deployment, "prepare", lambda rev: events.append(("prepare", rev)))
    monkeypatch.setattr(deployment, "stop", lambda instance: events.append(("stop", instance)))

    def start(revision, port, *, fallback=False):
        assert port == previous.port and not fallback
        events.append(("start", revision))
        if revision == target:
            raise update.UpdateError("launch failed")
        return previous

    monkeypatch.setattr(deployment, "start", start)
    with pytest.raises(update.UpdateError, match="launch failed"):
        deployment.update(None)
    assert events == [
        ("prepare", target),
        ("stop", previous),
        ("start", target),
        ("start", previous.revision),
    ]
    assert update.Instance.read(deployment.state / "last-good.json") == previous


def test_failed_isolated_preflight_never_stops_existing_instance(deployment, monkeypatch):
    previous = update.Instance("a" * 40, 18800, "1" * 32, deployment.profile)
    monkeypatch.setattr(deployment, "running", lambda: previous)
    monkeypatch.setattr(update, "healthy", lambda _: False)
    monkeypatch.setattr(deployment, "target", lambda _: "b" * 40)
    monkeypatch.setattr(deployment, "stop", lambda _: pytest.fail("must not stop old service"))

    def fail(_):
        raise update.UpdateError("preflight failed")

    monkeypatch.setattr(deployment, "prepare", fail)
    with pytest.raises(update.UpdateError, match="preflight failed"):
        deployment.update(None)


def test_live_check_runs_before_switch_even_on_the_same_revision(deployment, monkeypatch):
    deployment.check_live = True
    previous = update.Instance("a" * 40, 18800, "1" * 32, deployment.profile)
    release = deployment.releases / previous.revision
    release.mkdir()
    (release / ".prepared").touch()
    monkeypatch.setattr(deployment, "running", lambda: previous)
    monkeypatch.setattr(update, "healthy", lambda port: port == previous.port)
    monkeypatch.setattr(deployment, "target", lambda _: previous.revision)
    monkeypatch.setattr(deployment, "stop", lambda _: pytest.fail("must not stop old service"))

    def fail_probe(name, argv, *, cwd, env, timeout):
        assert name == "真实演示预检"
        assert "'--check-live'" in argv[-1]
        assert cwd == release and env == deployment.env
        assert timeout > 900
        raise update.UpdateError("NO_VALID_RANKING")

    monkeypatch.setattr(deployment, "command", fail_probe)
    with pytest.raises(update.UpdateError, match="NO_VALID_RANKING"):
        deployment.update(None)


def test_changed_profile_does_not_stop_live_instance(deployment, monkeypatch):
    previous = update.Instance("a" * 40, 18800, "1" * 32, "0" * 64)
    monkeypatch.setattr(deployment, "running", lambda: previous)
    monkeypatch.setattr(deployment, "stop", lambda _: pytest.fail("must not stop old service"))
    with pytest.raises(update.UpdateError, match="部署环境"):
        deployment.update(None)
