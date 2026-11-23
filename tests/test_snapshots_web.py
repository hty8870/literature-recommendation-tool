from __future__ import annotations

import os

import httpx

from citation_tool.api import create_app
from citation_tool.config import Settings
from test_integration import headers, search_body, wait_final, wait_state


def test_checkpoint_is_idempotent_and_does_not_stop_search(tmp_path, live_server) -> None:
    """AT-SNP-002 and API-001: checkpoint is immutable and does not finalize."""
    base = live_server(create_app(Settings.for_tests(tmp_path)))
    with httpx.Client(timeout=10) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 2, "fixture-slow": 500}),
            headers=headers(),
        ).json()
        query_id = created["query_id"]
        wait_state(client, base, query_id, lambda state: len(state["results"]) >= 2)
        request_headers = headers(key="checkpoint-1")
        first = client.post(
            f"{base}/api/v1/searches/{query_id}/snapshots",
            json={"kind": "checkpoint"},
            headers=request_headers,
        )
        assert first.status_code == 201, first.text
        checkpoint = first.json()
        assert checkpoint["snapshot_kind"] == "checkpoint"
        assert checkpoint["is_partial"] is True
        assert all(item["provisional"] is True for item in checkpoint["items"])
        repeated = client.post(
            f"{base}/api/v1/searches/{query_id}/snapshots",
            json={"kind": "checkpoint"},
            headers=request_headers,
        )
        assert repeated.content == first.content
        conflict = client.post(
            f"{base}/api/v1/searches/{query_id}/snapshots",
            json={"kind": "final"},
            headers=request_headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"

        final = wait_final(client, base, query_id)
        assert final["session"]["latest_checkpoint_id"] == checkpoint["snapshot_id"]
        assert (
            client.get(
                f"{base}/api/v1/snapshots/{checkpoint['snapshot_id']}", headers=headers()
            ).content
            == first.content
        )
        after_final = client.post(
            f"{base}/api/v1/searches/{query_id}/snapshots",
            json={"kind": "checkpoint"},
            headers=headers(),
        )
        assert after_final.status_code == 409


def test_early_final_stops_workers_and_returns_same_snapshot(tmp_path, live_server) -> None:
    """AT-SNP-006: early finalization drains and preserves partial coverage."""
    base = live_server(create_app(Settings.for_tests(tmp_path)))
    with httpx.Client(timeout=10) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 200, "fixture-slow": 3000}),
            headers=headers(),
        ).json()
        query_id = created["query_id"]
        accepted = client.post(
            f"{base}/api/v1/searches/{query_id}/snapshots",
            json={"kind": "final"},
            headers=headers(key="final-1"),
        )
        assert accepted.status_code == 202
        assert accepted.json()["snapshot_id"] is None
        state = wait_final(client, base, query_id)
        assert state["session"]["search_state"] == "partial"
        assert state["coverage"]["pending_library_ids"] == []
        assert state["coverage"]["running_library_ids"] == []
        manifest = client.get(
            f"{base}/api/v1/snapshots/{state['session']['final_snapshot_id']}", headers=headers()
        ).json()
        assert manifest["termination_reason"] == "user_requested"
        assert manifest["is_partial"] is True
        repeated = client.post(
            f"{base}/api/v1/searches/{query_id}/snapshots",
            json={"kind": "final"},
            headers=headers(key="final-1"),
        )
        assert repeated.json()["snapshot_id"] == manifest["snapshot_id"]


def test_default_factory_serves_web_and_simulated_libraries(
    tmp_path, monkeypatch, live_server
) -> None:
    for name in tuple(os.environ):
        if name.startswith("CITATION_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CITATION_DB_PATH", str(tmp_path / "citation.sqlite3"))
    monkeypatch.setenv("CITATION_AUTH_DB_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("CITATION_MODEL_CONFIG", str(tmp_path / "missing-model.json"))

    app = create_app()
    assert app.state.settings.candidate_pipeline == "agentic"
    base = live_server(app)
    with httpx.Client(timeout=10) as client:
        health = client.get(f"{base}/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.json()["simulation"] is True
        assert health.json()["agent_available"] is False
        page = client.get(f"{base}/")
        assert page.status_code == 200
        assert 'src="./js/main.js"' in page.text
        assert client.get(f"{base}/js/main.js").status_code == 200
        libraries = client.get(f"{base}/api/v1/libraries")
        assert libraries.status_code == 200
        configured = libraries.json()["libraries"]
        assert {item["library_id"] for item in configured} == {"fixture-fast", "fixture-slow"}
        assert all(item["simulation"] for item in configured)
        assert all(item["display_name"].startswith("本地演示") for item in configured)
        assert 'id="library-list"' in page.text
        assert 'id="library-note" hidden' in page.text
        assert 'id="mode-menu"' in page.text
        assert 'id="manuscript-file"' in page.text
