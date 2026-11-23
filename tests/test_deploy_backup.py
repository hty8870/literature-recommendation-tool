from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import tools.deploy_citation as deployment
from tools.deploy_citation import DATABASE_BACKUP_SCRIPT


@pytest.mark.parametrize("target", [None, "-Fconfig", "user@host additional-arg"])
def test_deployment_requires_an_explicit_ssh_destination(monkeypatch, target):
    if target is None:
        monkeypatch.delenv("CITATION_DEPLOY_SSH_TARGET", raising=False)
    else:
        monkeypatch.setenv("CITATION_DEPLOY_SSH_TARGET", target)
    with pytest.raises(ValueError, match="CITATION_DEPLOY_SSH_TARGET"):
        deployment.ssh_command()


def test_deployment_can_use_an_ssh_destination_and_explicit_identity(monkeypatch, tmp_path):
    identity = tmp_path / "test identity"
    monkeypatch.setenv("CITATION_DEPLOY_SSH_TARGET", "user@example.invalid")
    monkeypatch.setenv("CITATION_DEPLOY_SSH_IDENTITY_FILE", str(identity))
    assert deployment.ssh_command(connect_timeout=15) == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-i",
        str(identity),
        "user@example.invalid",
    ]


def run_backup(source: Path, target: Path):
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", DATABASE_BACKUP_SCRIPT, str(source), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_deployment_snapshot_preserves_committed_wal_data_without_changing_source(tmp_path):
    source, target = tmp_path / "app.db", tmp_path / "app-before.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('committed')")
        connection.commit()
        result = run_backup(source, target)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["integrity"] == "ok"
        assert connection.execute("SELECT value FROM records").fetchall() == [("committed",)]
    with sqlite3.connect(target) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert backup.execute("SELECT value FROM records").fetchall() == [("committed",)]


def test_deployment_snapshot_refuses_overwrite_and_missing_source(tmp_path):
    source, target = tmp_path / "missing.db", tmp_path / "existing.sqlite"
    target.write_bytes(b"preserve this existing backup")
    assert run_backup(source, target).returncode != 0
    assert target.read_bytes() == b"preserve this existing backup"
    absent_target = tmp_path / "must-not-be-created.sqlite"
    assert run_backup(source, absent_target).returncode != 0
    assert not absent_target.exists()
    assert not source.exists()


@pytest.mark.parametrize("restart_attempted", [False, True])
def test_failed_deployment_does_not_restart_users_when_restart_was_not_attempted(
    monkeypatch, restart_attempted
):
    commands = []

    def fail(context):
        context.update(
            {
                "unit_backup": "/opt/citation-tool/releases/test/previous.service",
                "previous_root": "/opt/citation-tool/releases/old",
                "restart_attempted": restart_attempted,
            }
        )
        raise RuntimeError("active users appeared before the restart")

    monkeypatch.setattr(deployment, "deploy", fail)
    monkeypatch.setattr(deployment, "remote", lambda command: commands.append(command))
    with pytest.raises(RuntimeError, match="deployment failed"):
        deployment.main()
    assert any("previous.service" in command for command in commands)
    assert (
        any("systemctl restart citation-tool" in command for command in commands)
        is restart_attempted
    )


def test_stage_only_mode_is_explicit_and_does_not_activate(monkeypatch):
    seen = []
    monkeypatch.setattr(deployment, "deploy", lambda context: seen.append(dict(context)))
    deployment.main(stage_only=True)
    assert seen == [{"stage_only": True}]


def test_staged_manifest_does_not_claim_active_or_model_acceptance(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deployment, "remote", lambda command, payload=None: calls.append((command, payload))
    )
    deployment.record_staged_release(
        "/opt/citation-tool/releases/test", "large-hash", "sample-hash", 11002
    )
    manifest = json.loads(calls[0][1])
    assert manifest["status"] == "staged_not_active"
    assert manifest["corpus_records"] == 11002
    assert manifest["sample_records"] == 50
    assert manifest["manuscript_upload_enabled"] is False
    assert manifest["model_acceptance"] == "not_run"
    assert len(calls) == 2
    assert calls[1][0] == "sudo tee -a /opt/OPS_NOTES.md >/dev/null"
    assert not any("systemctl" in command or "ln -s" in command for command, _ in calls)


def test_staged_activation_rejects_unsafe_paths_before_any_remote_change(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("remote operation must not start")

    monkeypatch.setattr(deployment, "remote", unexpected)
    with pytest.raises(ValueError, match="invalid staged"):
        deployment.activate_staged_release({}, "../active")
    with pytest.raises(ValueError, match="provisioned"):
        deployment.activate_staged_release(
            {"model_credential": "/not-authorized/model.json"}, "20261117-050212"
        )


def test_clone_mode_preserves_flags_and_does_not_rebuild_dependencies(monkeypatch):
    calls = []
    monkeypatch.setattr(deployment, "clone_current_release", lambda: "20261117-180000")
    monkeypatch.setattr(deployment, "deploy", lambda context: pytest.fail("must not reinstall"))
    monkeypatch.setattr(
        deployment,
        "activate_staged_release",
        lambda context, release: calls.append((context, release)),
    )
    credential = "/etc/credstore.encrypted/citation-luna-20261117-065959.json"
    deployment.main(clone_current=True, model_credential=credential, enable_uploads=True)
    assert calls == [
        (
            {"stage_only": False, "model_credential": credential, "enable_uploads": True},
            "20261117-180000",
        )
    ]


def test_staged_activation_uses_explicit_credential_and_does_not_restage(monkeypatch):
    calls = []
    monkeypatch.setattr(
        deployment,
        "activate_staged_release",
        lambda context, release: calls.append((context, release)),
    )
    monkeypatch.setattr(deployment, "deploy", lambda context: pytest.fail("must not install twice"))
    path = "/etc/credstore.encrypted/citation-luna-20261117-120000.json"
    deployment.main(staged_release="20261117-050212", model_credential=path)
    assert calls == [({"stage_only": False, "model_credential": path}, "20261117-050212")]
