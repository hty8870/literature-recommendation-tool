from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def iter_sse(response: httpx.Response):
    fields: dict[str, str] = {}
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                payload = json.loads("\n".join(data_lines))
                yield {
                    "id": int(fields["id"]),
                    "event": fields.get("event", "message"),
                    "data": payload,
                }
            fields, data_lines = {}, []
        elif line.startswith(":"):
            continue
        elif line.startswith("id:"):
            fields["id"] = line[3:].strip()
        elif line.startswith("event:"):
            fields["event"] = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield {
            "id": int(fields["id"]),
            "event": fields.get("event", "message"),
            "data": json.loads("\n".join(data_lines)),
        }


def read_events(
    client: httpx.Client,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    events: list[dict] = []
    with client.stream("GET", url, headers=headers, params=params) as response:
        if response.status_code != 200:
            response.read()
            raise AssertionError(f"SSE request failed: {response.status_code} {response.text}")
        for event in iter_sse(response):
            events.append(event)
            if until and event["event"] == until:
                break
            if limit and len(events) >= limit:
                break
    return events


@pytest.fixture
def live_server():
    resources: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start(app) -> str:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        resources.append((server, thread))
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("uvicorn thread exited before startup")
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn startup timed out")
            time.sleep(0.005)
        return f"http://127.0.0.1:{port}"

    yield start
    for server, _ in resources:
        server.should_exit = True
    for _, thread in resources:
        thread.join(timeout=10)
