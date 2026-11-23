"""Exercise a real loopback HTTP server and its streaming protocol."""

import json
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from citation_tool.api import create_app
from citation_tool.config import Settings


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="citation-tool-smoke-") as temporary:
        settings = Settings.for_tests(Path(temporary))
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        thread = threading.Thread(target=server.run, name="m0-smoke-server", daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline or not thread.is_alive():
                    raise RuntimeError("Local server did not start")
                time.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=base, timeout=10) as client:
                health = client.get("/healthz")
                health.raise_for_status()
                assert health.json()["simulation"] is True

                ids = [library.library_id for library in settings.libraries]
                delays = {ids[0]: 100, ids[1]: 600}
                request = {
                    "input_type": "keywords",
                    "keywords": ["LFY2", "soybean floral development"],
                    "library_ids": ids,
                    "options": {
                        "library_delays_ms": delays,
                        "soft_deadline_ms": 5_000,
                    },
                }
                headers = {"X-Tenant-ID": "smoke", "Idempotency-Key": "smoke-search-v1"}
                created = client.post("/api/v1/searches", json=request, headers=headers)
                created.raise_for_status()
                assert created.status_code == 202
                query_id = created.json()["query_id"]
                replayed = client.post("/api/v1/searches", json=request, headers=headers)
                assert replayed.json()["query_id"] == query_id

                events = []
                first_result_time = None
                start = time.monotonic()
                with client.stream(
                    "GET",
                    f"/api/v1/searches/{query_id}/events",
                    headers={"Accept": "text/event-stream", "X-Tenant-ID": "smoke"},
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    assert response.headers["x-accel-buffering"] == "no"
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            events.append(event)
                            if (
                                event["event_type"] == "recommendation_created"
                                and first_result_time is None
                            ):
                                first_result_time = time.monotonic() - start
                                first_library = event["payload"]["recommendation"]["sources"][0][
                                    "library_id"
                                ]
                                assert first_library == ids[0], (
                                    "Slow library blocked the first result"
                                )
                assert first_result_time is not None
                assert events[-1]["event_type"] == "search_finalized"
                sequence = [event["sequence"] for event in events]
                assert sequence == list(range(1, len(events) + 1)), "Public sequence has a gap"

                snapshot_id = events[-1]["payload"]["snapshot_id"]
                snapshot_response = client.get(
                    f"/api/v1/snapshots/{snapshot_id}", headers={"X-Tenant-ID": "smoke"}
                )
                snapshot_response.raise_for_status()
                snapshot = snapshot_response.json()
                repeated = client.get(
                    f"/api/v1/snapshots/{snapshot_id}", headers={"X-Tenant-ID": "smoke"}
                ).content
                assert repeated == snapshot_response.content, "Snapshot is not immutable"
                assert snapshot["immutable_hash"] == snapshot_response.headers["etag"].strip('"')

                replayed_events = []
                with client.stream(
                    "GET",
                    f"/api/v1/searches/{query_id}/events",
                    params={"after_sequence": sequence[-2]},
                    headers={"X-Tenant-ID": "smoke", "Last-Event-ID": str(sequence[-2])},
                ) as response:
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            replayed_events.append(json.loads(line[6:]))
                assert [event["sequence"] for event in replayed_events] == [sequence[-1]]

                state = client.get(f"/api/v1/searches/{query_id}", headers={"X-Tenant-ID": "smoke"})
                state.raise_for_status()
                body = state.json()
                assert body["as_of_sequence"] == body["latest_sequence"] == sequence[-1]
                assert body["coverage"]["total_library_count"] == len(ids)
                print(
                    "PASS: real HTTP + SSE; first result "
                    f"{first_result_time:.3f}s before slow library; "
                    f"{len(events)} ordered events; replay and immutable snapshot verified."
                )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("Local server did not stop cleanly")


if __name__ == "__main__":
    main()
