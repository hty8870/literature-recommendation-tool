from __future__ import annotations

import json

import pytest
import tools.luna_egress as egress


def test_tunnel_is_single_project_loopback_and_disables_connection_sharing(monkeypatch):
    monkeypatch.setenv("CITATION_DEPLOY_SSH_TARGET", "citation-test")
    monkeypatch.delenv("CITATION_DEPLOY_SSH_IDENTITY_FILE", raising=False)
    command = egress.tunnel_command()
    assert command[-2] == "citation-test"
    assert command[command.index("-R") + 1] == "127.0.0.1:18768:127.0.0.1:7897"
    assert "ControlMaster=no" in command
    assert "ControlPath=none" in command
    assert "ExitOnForwardFailure=yes" in command
    assert "-N" not in command
    assert command[-1].startswith("exec python3 -u -c ")
    assert "CITATION_EGRESS_PID" in egress.REMOTE_SESSION
    assert "identity()!=birth" in egress.REMOTE_SESSION


def test_authentication_or_host_identity_failure_never_becomes_a_retry():
    assert egress.permanent_failure("Permission denied (publickey)")
    assert egress.permanent_failure("Host key verification failed")
    assert egress.permanent_failure("remote port forwarding failed for listen port")
    assert not egress.permanent_failure("Connection closed by remote host")


def test_cleanup_only_uses_verified_creation_bound_receipt(tmp_path, monkeypatch):
    path = tmp_path / "owner.json"
    receipt = {
        "pid": 1234,
        "start_ticks": "999",
        "purpose": "citation-openai-egress",
        "port": 18768,
    }
    path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(egress, "LEASE", path)
    calls = []

    def remote(script, request):
        calls.append((script, json.loads(request)))
        return json.dumps({"closed": True})

    monkeypatch.setattr(egress, "remote_python", remote)
    egress.cleanup_owned()
    assert calls[0][1] == {**receipt, "action": "close"}
    assert "pidfd_send_signal" in calls[0][0]
    assert not path.exists()


def test_unknown_receipt_does_not_touch_a_remote_process(tmp_path, monkeypatch):
    path = tmp_path / "owner.json"
    path.write_text(json.dumps({"purpose": "another-project", "port": 18768}), encoding="utf-8")
    monkeypatch.setattr(egress, "LEASE", path)
    monkeypatch.setattr(
        egress, "remote_python", lambda *args: pytest.fail("must not contact server")
    )
    with pytest.raises(ValueError):
        egress.cleanup_owned()
    assert path.exists()


def test_only_one_controller_can_hold_the_process_owned_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "LOCK", tmp_path / "egress.lock")
    with egress.single_owner():
        with pytest.raises(OSError), egress.single_owner():
            pytest.fail("second controller must not enter")
    with egress.single_owner():
        pass
