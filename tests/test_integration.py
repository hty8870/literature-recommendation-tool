from __future__ import annotations

import json
import time
from dataclasses import replace

import httpx

from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.jcs import sha256_json
from citation_tool.retrieval import build_fixture_adapters
from conftest import read_events


class FailingAdapter:
    def __init__(self, library):
        self.library = library
        self.adapter_id = "failing-fixture-v1"
        self.simulation = True
        self.display_name = library.display_name
        self.index_snapshot = "failing-index-v1"
        self.model_versions = {"retrieval": "failing-adapter-v1"}

    def search(self, claim):
        raise RuntimeError("simulated local adapter failure")


class SlowOnceAdapter(FailingAdapter):
    def __init__(self, library):
        super().__init__(library)
        self.calls = 0

    def search(self, claim):
        self.calls += 1
        if self.calls == 1:
            time.sleep(0.35)
        return []


def search_body(delays: dict[str, int], soft_deadline_ms: int = 5000) -> dict:
    return {
        "input_type": "keywords",
        "keywords": ["LFY2", "soybean", "floral development"],
        "claim_scope": {
            "species": ["Glycine max"],
            "system": ["greenhouse", "field"],
            "method": ["expression profiling", "association analysis"],
        },
        "options": {
            "library_delays_ms": delays,
            "soft_deadline_ms": soft_deadline_ms,
        },
    }


def headers(tenant: str = "local", key: str | None = None) -> dict:
    value = {"X-Tenant-ID": tenant}
    if key:
        value["Idempotency-Key"] = key
    return value


def search_url(base: str, query_id: str) -> str:
    return f"{base}/api/v1/searches/{query_id}"


def events_url(base: str, query_id: str) -> str:
    return f"{base}/api/v1/searches/{query_id}/events"


def wait_state(
    client: httpx.Client,
    base: str,
    query_id: str,
    predicate,
    timeout: float = 15,
    tenant: str = "local",
    request_headers: dict[str, str] | None = None,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            search_url(base, query_id), headers=request_headers or headers(tenant=tenant)
        )
        assert response.status_code == 200, response.text
        state = response.json()
        if predicate(state):
            return state
        time.sleep(0.01)
    raise AssertionError(f"state predicate not met for {query_id}")


def wait_final(
    client: httpx.Client,
    base: str,
    query_id: str,
    tenant: str = "local",
    request_headers: dict[str, str] | None = None,
) -> dict:
    return wait_state(
        client,
        base,
        query_id,
        lambda state: (
            state["session"]["search_state"] in {"completed", "partial", "failed", "cancelled"}
        ),
        tenant=tenant,
        request_headers=request_headers,
    )


def test_stream_state_snapshot_evidence_and_restart(tmp_path, live_server) -> None:
    settings = Settings.for_tests(tmp_path)
    app = create_app(settings)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 2, "fixture-slow": 600}),
            headers=headers(key="flow"),
        )
        assert response.status_code == 202
        created = response.json()
        query_id = created["query_id"]
        assert created["search_state"] == "created"
        assert created["simulation"] is True
        assert created["adapter_mode"] == "simulation_fixture_model_unavailable"
        assert "fixture" in created["notice"] and "未配置模型" in created["notice"]
        assert created["claims"][0]["claim_type"] == "topic"
        assert created["claims"][0]["scope"]["species"] == ["Glycine max"]
        assert created["scope_source"] == "request"

        early = wait_state(
            client,
            base,
            query_id,
            lambda state: (
                len(
                    [
                        item
                        for item in state["results"]
                        if any(source["library_id"] == "fixture-fast" for source in item["sources"])
                    ]
                )
                >= 2
                and next(
                    worker for worker in state["workers"] if worker["library_id"] == "fixture-slow"
                )["worker_state"]
                == "running"
            ),
        )
        assert early["session"]["search_state"] == "running"
        assert early["as_of_sequence"] == early["latest_sequence"]
        assert early["tenant_note"].startswith("X-Tenant-ID")
        assert any(item["simulation"] for item in early["results"] if item["citable"])

        events = read_events(
            client,
            events_url(base, query_id),
            headers=headers(),
            until="search_finalized",
        )
        assert [event["id"] for event in events] == list(range(1, len(events) + 1))
        assert len({event["data"]["event_id"] for event in events}) == len(events)
        assert [event["event"] for event in events].count("search_finalized") == 1
        assert [event["event"] for event in events].count("worker_completed") == 2
        created_events = [event for event in events if event["event"] == "recommendation_created"]
        assert len(created_events) == 4

        fast_events = [
            event
            for event in created_events
            if any(
                source["library_id"] == "fixture-fast"
                for source in event["data"]["payload"]["recommendation"]["sources"]
            )
        ]
        slow_events = [event for event in created_events if event not in fast_events]
        assert len(fast_events) == len(slow_events) == 2
        completed = [event for event in events if event["event"] == "worker_completed"]
        fast_completed = next(
            event
            for event in completed
            if event["data"]["payload"]["worker"]["library_id"] == "fixture-fast"
        )
        slow_completed = next(
            event
            for event in completed
            if event["data"]["payload"]["worker"]["library_id"] == "fixture-slow"
        )
        assert max(event["id"] for event in fast_events) < min(event["id"] for event in slow_events)
        assert fast_completed["id"] < min(event["id"] for event in slow_events)
        assert slow_completed["id"] > max(event["id"] for event in slow_events)
        rank_events = [event for event in events if event["event"] == "recommendation_reranked"]
        assert len(rank_events) == 1
        changes = rank_events[0]["data"]["payload"]["changes"]
        assert changes
        assert all(change["current_rank"] >= 2 for change in changes)
        assert all(change["previous_rank"] > 0 for change in changes)
        assert (
            slow_completed["data"]["payload"]["worker"]["last_emitted_sequence"]
            >= rank_events[0]["id"]
        )
        for event in completed:
            worker = event["data"]["payload"]["worker"]
            assert worker["worker_state"] == "completed"
            assert worker["drained"] is True
            assert worker["last_emitted_sequence"] == worker["local_watermark"]
            assert worker["emitted_event_count"] == 2

        cursor = fast_events[0]["id"]
        resumed = read_events(
            client,
            events_url(base, query_id),
            headers={**headers(), "Last-Event-ID": str(cursor)},
            until="search_finalized",
        )
        assert [event["id"] for event in resumed] == [
            event["id"] for event in events if event["id"] > cursor
        ]

        conflict = client.get(
            events_url(base, query_id),
            params={"after_sequence": str(events[0]["id"])},
            headers={**headers(), "Last-Event-ID": str(events[1]["id"])},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "REPLAY_REQUEST_CONFLICT"

        state = wait_final(client, base, query_id)
        assert state["session"]["search_state"] == "completed"
        assert state["coverage"]["completed_library_ids"] == ["fixture-fast", "fixture-slow"]
        assert len(state["results"]) == 4
        assert state["as_of_sequence"] == state["latest_sequence"]
        assert state["session"]["finalize_cut"] <= state["as_of_sequence"]
        assert state["groups"] and state["groups"][0]["entries"][0]["rank"] == 1
        for recommendation in state["results"]:
            assert recommendation["citable"] is True
            assert recommendation["rankable"] is True
            assert recommendation["provisional"] is True
            assert recommendation["simulation"] is True
            assert recommendation["display_score"] is not None
            assert recommendation["evidence"][0]["source_trace_valid"] is True
            evidence = recommendation["evidence"][0]
            assert evidence["end_offset"] - evidence["start_offset"] == len(
                evidence["quote"].encode("utf-8")
            )

        snapshot_id = state["session"]["final_snapshot_id"]
        snapshot_response = client.get(f"{base}/api/v1/snapshots/{snapshot_id}", headers=headers())
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()
        assert snapshot["snapshot_kind"] == "final"
        assert snapshot["item_count"] == 4
        assert snapshot["is_partial"] is False
        assert snapshot["applied_through_sequence"] == state["session"]["finalize_cut"]
        assert [item["rank"] for item in snapshot["items"]] == [1, 2, 3, 4]
        assert all(item["provisional"] is False for item in snapshot["items"])
        scores = [item["display_score"] for item in snapshot["items"]]
        assert scores == sorted(scores, reverse=True)
        core = {key: value for key, value in snapshot.items() if key != "immutable_hash"}
        assert sha256_json(core) == snapshot["immutable_hash"]
        through_cut = [
            event["data"]
            for event in events
            if event["data"]["sequence"] <= snapshot["applied_through_sequence"]
        ]
        assert (
            sha256_json(
                [
                    [event["event_id"], event["event_type"], sha256_json(event["payload"])]
                    for event in through_cut
                ]
            )
            == snapshot["event_set_hash"]
        )
        bundle = json.loads(
            app.state.store.get_bundle("local", snapshot["evidence_bundle_id"])["manifest_json"]
        )
        assert sha256_json({"items": bundle["items"]}) == snapshot["evidence_bundle_hash"]
        assert snapshot["evidence_bundle_hash"] == bundle["bundle_hash"]
        assert (
            snapshot["evidence_bundle_id"] == bundle["bundle_id"] == f"eb_{bundle['bundle_hash']}"
        )

        evidence_id = snapshot["items"][0]["evidence_ids"][0]
        evidence_response = client.get(
            f"{base}/api/v1/snapshots/{snapshot_id}/evidence/{evidence_id}",
            headers=headers(),
        )
        assert evidence_response.status_code == 200
        evidence = evidence_response.json()
        assert evidence["text_offset_valid"] is True
        assert evidence["source_trace_valid"] is True
        assert evidence["locator"]["pdf_page"] >= 1
        assert evidence["end_offset"] - evidence["start_offset"] == len(
            evidence["quote"].encode("utf-8")
        )

        repeated = client.get(f"{base}/api/v1/snapshots/{snapshot_id}", headers=headers())
        assert repeated.content == snapshot_response.content
        assert repeated.headers["etag"] == f'"{snapshot["immutable_hash"]}"'
        assert client.patch(f"{base}/api/v1/snapshots/{snapshot_id}", json={}).status_code == 405
        assert client.put(f"{base}/api/v1/snapshots/{snapshot_id}", json={}).status_code == 405

        restarted_base = live_server(create_app(settings))
        restarted = client.get(search_url(restarted_base, query_id), headers=headers())
        assert restarted.status_code == 200
        assert restarted.json()["session"]["final_snapshot_id"] == snapshot_id
        restarted_snapshot = client.get(
            f"{restarted_base}/api/v1/snapshots/{snapshot_id}", headers=headers()
        )
        assert restarted_snapshot.content == snapshot_response.content


def test_idempotency_replay_conflict_and_gap(tmp_path, live_server) -> None:
    settings = Settings.for_tests(tmp_path, retention=6)
    base = live_server(create_app(settings))
    body = search_body({"fixture-fast": 1, "fixture-slow": 1})
    with httpx.Client(timeout=20) as client:
        first = client.post(f"{base}/api/v1/searches", json=body, headers=headers(key="idem"))
        second = client.post(f"{base}/api/v1/searches", json=body, headers=headers(key="idem"))
        assert first.status_code == second.status_code == 202
        query_id = first.json()["query_id"]
        assert second.json()["query_id"] == query_id
        changed = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}, soft_deadline_ms=4000),
            headers=headers(key="idem"),
        )
        assert changed.status_code == 409
        assert changed.json()["code"] == "IDEMPOTENCY_KEY_CONFLICT"
        other_tenant = client.post(
            f"{base}/api/v1/searches", json=body, headers=headers(tenant="other", key="idem")
        )
        assert other_tenant.status_code == 202
        assert other_tenant.json()["query_id"] != query_id
        wait_final(client, base, query_id)
        wait_final(client, base, other_tenant.json()["query_id"], tenant="other")
        state = client.get(search_url(base, query_id), headers=headers()).json()
        assert state["latest_sequence"] > 6
        gap = client.get(
            events_url(base, query_id),
            params={"after_sequence": "0"},
            headers=headers(),
        )
        assert gap.status_code == 410
        assert gap.json()["code"] == "REPLAY_GAP"
        conflict = client.get(
            events_url(base, query_id),
            params={"after_sequence": str(state["latest_sequence"] - 1)},
            headers={**headers(), "Last-Event-ID": str(state["latest_sequence"])},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "REPLAY_REQUEST_CONFLICT"
        ahead = client.get(
            events_url(base, query_id),
            params={"after_sequence": str(state["latest_sequence"] + 1)},
            headers=headers(),
        )
        assert ahead.status_code == 409
        assert (
            read_events(
                client,
                events_url(base, query_id),
                params={"after_sequence": str(state["latest_sequence"])},
                headers=headers(),
            )
            == []
        )


def test_failed_library_produces_partial_snapshot(tmp_path, live_server) -> None:
    settings = Settings.for_tests(tmp_path)
    adapters = build_fixture_adapters(settings)
    adapters["fixture-fast"] = FailingAdapter(settings.library("fixture-fast"))
    base = live_server(create_app(settings, adapters=adapters))
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 1, "fixture-slow": 2}),
            headers=headers(),
        )
        query_id = response.json()["query_id"]
        state = wait_final(client, base, query_id)
        assert state["session"]["search_state"] == "partial"
        assert state["coverage"]["failed_library_ids"] == ["fixture-fast"]
        assert state["coverage"]["completed_library_ids"] == ["fixture-slow"]
        snapshot = client.get(
            f"{base}/api/v1/snapshots/{state['session']['final_snapshot_id']}",
            headers=headers(),
        ).json()
        assert snapshot["is_partial"] is True
        assert snapshot["item_count"] == 2
        assert snapshot["termination_reason"] == "coordinator_failed"
        events = read_events(
            client,
            events_url(base, query_id),
            headers=headers(),
            until="search_finalized",
        )
        failed = [event for event in events if event["event"] == "worker_failed"]
        assert len(failed) == 1
        assert failed[0]["data"]["payload"]["worker"]["last_error"]["code"] == "INTERNAL_ERROR"


def test_soft_deadline_marks_worker_timed_out(tmp_path, live_server) -> None:
    settings = Settings.for_tests(tmp_path)
    base = live_server(create_app(settings))
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 2, "fixture-slow": 250}, soft_deadline_ms=100),
            headers=headers(),
        )
        query_id = response.json()["query_id"]
        state = wait_final(client, base, query_id)
        assert state["session"]["search_state"] == "partial"
        assert state["coverage"]["timed_out_library_ids"] == ["fixture-slow"]
        assert state["coverage"]["completed_library_ids"] == ["fixture-fast"]
        snapshot = client.get(
            f"{base}/api/v1/snapshots/{state['session']['final_snapshot_id']}",
            headers=headers(),
        ).json()
        assert snapshot["is_partial"] is True
        assert snapshot["item_count"] == 2
        assert snapshot["termination_reason"] == "deadline_reached"
        events = read_events(
            client,
            events_url(base, query_id),
            headers=headers(),
            until="search_finalized",
        )
        timed_out = [event for event in events if event["event"] == "worker_timed_out"]
        assert len(timed_out) == 1
        assert timed_out[0]["data"]["payload"]["worker"]["worker_state"] == "timed_out"


def test_adapter_search_timeout_finalizes_and_releases_lock_after_thread_exit(
    tmp_path, live_server
) -> None:
    library = Settings().library("fixture-fast")
    adapter = SlowOnceAdapter(library)
    settings = replace(
        Settings.for_tests(tmp_path),
        libraries=(library,),
        default_soft_deadline_ms=100,
    )
    base = live_server(create_app(settings, adapters={library.library_id: adapter}))
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            json=search_body({library.library_id: 0}, soft_deadline_ms=100),
            headers=headers(),
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "partial"
        assert state["coverage"]["timed_out_library_ids"] == [library.library_id]
        assert state["workers"][0]["candidates_seen"] == 0
        assert state["session"]["final_snapshot_id"]

        time.sleep(0.4)
        created = client.post(
            f"{base}/api/v1/searches",
            json=search_body({library.library_id: 0}, soft_deadline_ms=5000),
            headers=headers(),
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "completed"
        assert state["coverage"]["completed_library_ids"] == [library.library_id]
        assert adapter.calls == 2


def test_full_text_and_unknown_library_are_rejected(live_server, tmp_path) -> None:
    base = live_server(create_app(Settings.for_tests(tmp_path)))
    with httpx.Client(timeout=10) as client:
        full_text = client.post(
            f"{base}/api/v1/searches",
            json={"input_type": "full_text", "manuscript_id": "m1"},
            headers=headers(),
        )
        assert full_text.status_code == 400
        assert full_text.json()["code"] == "VALIDATION_ERROR"
        unknown = client.post(
            f"{base}/api/v1/searches",
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
                "library_ids": ["does-not-exist"],
            },
            headers=headers(),
        )
        assert unknown.status_code == 400
        assert unknown.json()["code"] == "VALIDATION_ERROR"
        empty = client.post(
            f"{base}/api/v1/searches",
            json={"input_type": "keywords", "keywords": ["LFY2"], "library_ids": []},
            headers=headers(),
        )
        assert empty.status_code == 400
        duplicate = client.post(
            f"{base}/api/v1/searches",
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
                "library_ids": ["fixture-fast", "fixture-fast"],
            },
            headers=headers(),
        )
        assert duplicate.status_code == 400


def test_missing_claim_scope_uses_labeled_fixture_demo_scope(tmp_path, live_server) -> None:
    settings = Settings.for_tests(tmp_path)
    base = live_server(create_app(settings))
    body = {
        "input_type": "keywords",
        "keywords": ["LFY2", "soybean"],
        "options": {
            "library_delays_ms": {"fixture-fast": 1, "fixture-slow": 1},
            "soft_deadline_ms": 5000,
        },
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{base}/api/v1/searches", json=body, headers=headers())
        created = response.json()
        query_id = created["query_id"]
        assert created["scope_source"] == "m0_fixture_demo_default"
        assert created["claims"][0]["scope"]["species"] == ["Glycine max"]
        assert "仅演示" in created["notice"]
        state = wait_final(client, base, query_id)
        assert state["scope_source"] == "m0_fixture_demo_default"
        assert state["session"]["search_state"] == "completed"
        assert len(state["results"]) == 4
        assert all(item["citable"] for item in state["results"])
        snapshot = client.get(
            f"{base}/api/v1/snapshots/{state['session']['final_snapshot_id']}",
            headers=headers(),
        ).json()
        assert snapshot["item_count"] == 4
        assert snapshot["is_partial"] is False


def test_production_default_without_scope_has_no_citable_results(tmp_path, live_server) -> None:
    settings = replace(Settings.for_tests(tmp_path), fixture_demo_scope_default=False)
    base = live_server(create_app(settings))
    body = {
        "input_type": "keywords",
        "keywords": ["LFY2", "soybean"],
        "options": {
            "library_delays_ms": {"fixture-fast": 1, "fixture-slow": 1},
            "soft_deadline_ms": 5000,
        },
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{base}/api/v1/searches", json=body, headers=headers())
        created = response.json()
        assert created["scope_source"] == "unknown"
        assert "unknown" in created["notice"]
        query_id = created["query_id"]
        state = wait_final(client, base, query_id)
        assert state["scope_source"] == "unknown"
        assert state["session"]["search_state"] == "completed"
        assert state["results"] == []
        snapshot = client.get(
            f"{base}/api/v1/snapshots/{state['session']['final_snapshot_id']}",
            headers=headers(),
        ).json()
        assert snapshot["item_count"] == 0


def test_cancel_is_idempotent_and_creates_final_cancelled_snapshot(tmp_path, live_server) -> None:
    settings = Settings.for_tests(tmp_path)
    base = live_server(create_app(settings))
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 400, "fixture-slow": 400}),
            headers=headers(),
        )
        query_id = response.json()["query_id"]
        wait_state(
            client,
            base,
            query_id,
            lambda state: (
                state["session"]["search_state"] == "running"
                and len(
                    [worker for worker in state["workers"] if worker["worker_state"] == "running"]
                )
                >= 1
            ),
        )
        cancelled = client.post(f"{base}/api/v1/searches/{query_id}/cancel", headers=headers())
        assert cancelled.status_code == 202
        assert cancelled.json()["cancel_snapshot_id"] is None
        state = wait_final(client, base, query_id)
        assert state["session"]["search_state"] == "cancelled"
        assert set(state["coverage"]["cancelled_library_ids"]) == {
            "fixture-fast",
            "fixture-slow",
        }
        snapshot_id = state["session"]["final_snapshot_id"]
        snapshot = client.get(f"{base}/api/v1/snapshots/{snapshot_id}", headers=headers()).json()
        assert snapshot["termination_reason"] == "cancelled"
        assert snapshot["is_partial"] is True
        assert snapshot["item_count"] == 0
        repeated = client.post(f"{base}/api/v1/searches/{query_id}/cancel", headers=headers())
        assert repeated.status_code == 202
        assert repeated.json()["cancel_snapshot_id"] == snapshot_id
        assert repeated.json()["session"]["search_state"] == "cancelled"
