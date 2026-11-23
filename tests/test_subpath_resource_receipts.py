from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI

from citation_tool.api import create_app
from citation_tool.citation_sources import EuropePmcSourceResolver
from citation_tool.config import Settings
from citation_tool.manuscripts import DOCX_CONTENT_TYPE
from citation_tool.retrieval import build_fixture_adapters
from test_citation_sources import FakeTransport
from test_citation_texts import ScriptedCitationModel
from test_manuscript_cascade_api import PublicSourceFixtureAdapter, _docx


def get_until(client, path, headers, predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        record = response.json()
        if predicate(record):
            return record
        time.sleep(0.01)
    raise AssertionError(f"resource did not reach expected state: {path}")


@pytest.mark.parametrize(
    ("mount", "configured_base"),
    [("", ""), ("/literature", ""), ("/literature", "/literature"), ("/proxy/literature", "")],
)
def test_upload_and_citation_receipts_follow_current_http_mount(
    tmp_path, live_server, mount, configured_base
):
    settings = replace(Settings.for_tests(tmp_path), base_path=configured_base)
    adapters = build_fixture_adapters(settings)
    adapters["fixture-fast"] = PublicSourceFixtureAdapter(settings.library("fixture-fast"))
    model = ScriptedCitationModel()
    app = create_app(
        settings,
        adapters=adapters,
        citation_model=model,
        citation_review_model=model,
        citation_resolver=EuropePmcSourceResolver(
            transport=FakeTransport(), timeout_ms=5000, max_evidence=3
        ),
    )
    outer = FastAPI()
    if mount:
        outer.mount(mount, app)
    else:
        outer = app
    base = live_server(outer)
    headers = {"X-Tenant-ID": "receipt-owner", "X-Forwarded-Prefix": "/untrusted"}
    try:
        with httpx.Client(base_url=base, timeout=20) as client:
            upload = client.post(
                f"{mount}/api/v1/manuscripts",
                content=_docx(),
                headers={**headers, "X-Filename": "public.docx", "Content-Type": DOCX_CONTENT_TYPE},
            )
            assert upload.status_code == 202, upload.text
            manuscript_id = upload.json()["manuscript_id"]
            manuscript_path = f"{mount}/api/v1/manuscripts/{manuscript_id}"
            assert upload.json()["status_url"] == manuscript_path
            assert upload.headers["Location"] == manuscript_path
            followed = client.get(upload.headers["Location"], headers=headers)
            assert followed.status_code == 200
            assert followed.json()["manuscript_id"] == manuscript_id
            manuscript = get_until(
                client,
                upload.json()["status_url"],
                headers,
                lambda record: record["parse_status"] in {"ready", "failed"},
            )
            assert manuscript["parse_status"] == "ready", manuscript
            assert manuscript["passages_url"] == f"{manuscript_path}/passages"
            passages = client.get(manuscript["passages_url"], headers=headers)
            assert passages.status_code == 200
            assert passages.json()["manuscript_id"] == manuscript_id
            assert passages.json()["passages"]

            search = client.post(
                f"{mount}/api/v1/searches",
                headers=headers,
                json={
                    "input_type": "keywords",
                    "keywords": ["herbicide resistance", "backcross soybean"],
                    "library_ids": ["fixture-fast"],
                    "options": {"library_delays_ms": {"fixture-fast": 0}, "soft_deadline_ms": 5000},
                },
            )
            assert search.status_code == 202, search.text
            query_id = search.json()["query_id"]
            state = get_until(
                client,
                f"{mount}/api/v1/searches/{query_id}",
                headers,
                lambda record: (
                    record["session"]["search_state"] in {"completed", "partial", "failed"}
                ),
            )
            candidate = state["candidate_diagnostics"][0]
            request = {
                "query_id": query_id,
                "result_id": candidate["result_id"],
                "result_version": candidate["pool_version"],
                "result_kind": "candidate",
                "citation_style": "vancouver",
                "language": "zh",
            }
            target = {key: request[key] for key in ("result_id", "result_version", "result_kind")}
            suggestion_path = f"{mount}/api/v1/searches/{query_id}/citation-suggestion"
            formats_path = f"{mount}/api/v1/searches/{query_id}/citation-formats"
            idle = client.get(suggestion_path, params=target, headers=headers)
            assert idle.status_code == 200 and idle.json() == {"preparing": False, "record": None}
            formatted = client.get(formats_path, params=target, headers=headers)
            assert formatted.status_code == 200, formatted.text
            assert all(option.get("text") for option in formatted.json()["formats"])
            assert not model.received
            assert (
                client.get(
                    formats_path, params=target, headers={"X-Tenant-ID": "other-owner"}
                ).status_code
                == 404
            )
            citation_headers = {**headers, "Idempotency-Key": "receipt-citation"}
            first = client.post(
                f"{mount}/api/v1/citation-texts", json=request, headers=citation_headers
            )
            assert first.status_code == 202, first.text
            citation_id = first.json()["citation_text_id"]
            citation_path = f"{mount}/api/v1/citation-texts/{citation_id}"
            assert first.json()["status_url"] == citation_path
            followed = client.get(first.json()["status_url"], headers=headers)
            assert followed.status_code == 200, followed.text
            assert followed.json()["citation_text_id"] == citation_id
            citation = get_until(
                client,
                citation_path,
                headers,
                lambda record: record["status"] in {"ready", "failed", "cancelled"},
            )
            assert citation["status"] == "ready", citation
            readback = client.get(suggestion_path, params=target, headers=headers)
            assert readback.status_code == 200, readback.text
            assert readback.json()["record"]["citation_text_id"] == citation_id
            replay = client.post(
                f"{mount}/api/v1/citation-texts", json=request, headers=citation_headers
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["cached"] is True
            assert replay.json()["status_url"] == citation_path
            assert (
                client.get(replay.json()["status_url"], headers=headers).json()["citation_text_id"]
                == citation_id
            )
            foreign = client.get(citation_path, headers={"X-Tenant-ID": "other-owner"})
            assert foreign.status_code == 404
    finally:
        asyncio.run(app.state.citation_texts.aclose())
        app.state.manuscripts.close()
