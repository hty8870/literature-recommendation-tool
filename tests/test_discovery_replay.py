from __future__ import annotations

import httpx
import pytest
from tools import discovery_pool_replay as replay

from test_discovery import setup
from test_integration import headers, wait_final


def test_stored_pool_replay_reads_exact_sources_without_modifying_the_original(
    tmp_path, live_server, monkeypatch
):
    app, _ = setup(tmp_path)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2", "soybean"]},
        ).json()
        state = wait_final(client, base, created["query_id"])
    database = app.state.store.db_path
    original = database.read_bytes()
    monkeypatch.setattr(replay, "RUNTIME", tmp_path.resolve())
    claim, items, evidence = replay.read_pool(database, "local", created["query_id"])
    assert claim.query_id == created["query_id"]
    assert {item.work_id for item in items} == {
        item["work_id"] for item in state["candidate_diagnostics"]
    }
    for item in items:
        assert evidence[item.work_id].context == item.evidence_excerpt
        assert evidence[item.work_id].source.title == item.title
    with pytest.raises(ValueError, match="search not found"):
        replay.read_pool(database, "other", created["query_id"])
    with pytest.raises(ValueError, match="exactly one"):
        replay.read_pool(database, "local", created["query_id"], "missing-claim")
    assert database.read_bytes() == original
    monkeypatch.setattr(replay, "RUNTIME", tmp_path / "not-the-source-root")
    with pytest.raises(ValueError, match="inside data/runtime"):
        replay.read_pool(database, "local", created["query_id"])
