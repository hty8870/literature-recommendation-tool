from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from citation_tool.api import create_app
from citation_tool.auth import token_hash
from citation_tool.config import Settings
from test_agent import StubReasonGenerator
from test_integration import headers, search_body, wait_final

TOKEN = "unit-test-token-not-a-real-secret"  # noqa: S105


def auth_settings(tmp_path) -> Settings:
    return replace(
        Settings.for_tests(tmp_path),
        auth_required=True,
        auth_db_path=tmp_path / "auth.db",
    )


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_authentication_cookie_and_mcp_end_to_end(tmp_path, live_server, monkeypatch) -> None:
    monkeypatch.setenv("CITATION_ACCESS_TOKEN_SHA256", token_hash(TOKEN))
    app = create_app(auth_settings(tmp_path), reason_generator=StubReasonGenerator())
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        assert client.get(f"{base}/healthz").status_code == 200
        assert client.get(f"{base}/healthz").json()["authentication_required"] is True
        assert client.get(f"{base}/api/v1/libraries").status_code == 401
        assert client.post(f"{base}/mcp", json=rpc("initialize")).status_code == 401
        assert client.post(f"{base}/login", json={"token": "wrong"}).status_code == 401
        login = client.post(f"{base}/login", json={"token": TOKEN})
        assert login.status_code == 200
        assert login.json()["tenant_id"] == "local"
        assert "HttpOnly" in login.headers["set-cookie"]

        created = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers={**headers(tenant="ignored-tenant"), "Idempotency-Key": "cookie-search"},
        )
        assert created.status_code == 202
        query_id = created.json()["query_id"]
        state = wait_final(client, base, query_id)
        assert state["session"]["tenant_id"] == "local"
        assert len(state["results"]) == 4

        initialized = client.post(f"{base}/mcp", json=rpc("initialize"), headers=bearer())
        assert initialized.status_code == 200
        assert initialized.json()["result"]["protocolVersion"] == "2025-03-26"
        tools = client.post(f"{base}/mcp", json=rpc("tools/list"), headers=bearer())
        names = {tool["name"] for tool in tools.json()["result"]["tools"]}
        assert names == {
            "search_literature",
            "get_search_state",
            "get_snapshot",
            "get_snapshot_evidence",
        }
        response = client.post(
            f"{base}/mcp",
            json=rpc(
                "tools/call",
                {
                    "name": "search_literature",
                    "arguments": search_body({"fixture-fast": 1, "fixture-slow": 1}),
                },
            ),
            headers=bearer(),
        )
        result = response.json()["result"]["structuredContent"]
        assert result["simulation"] is True
        assert len(result["results"]) == 4
        assert result["final_snapshot_id"]
        queried = client.post(
            f"{base}/mcp",
            json=rpc(
                "tools/call",
                {"name": "get_search_state", "arguments": {"query_id": result["query_id"]}},
            ),
            headers=bearer(),
        )
        assert (
            queried.json()["result"]["structuredContent"]["session"]["query_id"]
            == result["query_id"]
        )
        snapshot = client.post(
            f"{base}/mcp",
            json=rpc(
                "tools/call",
                {
                    "name": "get_snapshot",
                    "arguments": {"snapshot_id": result["final_snapshot_id"]},
                },
            ),
            headers=bearer(),
        )
        manifest = snapshot.json()["result"]["structuredContent"]
        assert manifest["item_count"] == 4
        evidence_id = manifest["items"][0]["evidence_ids"][0]
        evidence = client.post(
            f"{base}/mcp",
            json=rpc(
                "tools/call",
                {
                    "name": "get_snapshot_evidence",
                    "arguments": {
                        "snapshot_id": result["final_snapshot_id"],
                        "evidence_id": evidence_id,
                    },
                },
            ),
            headers=bearer(),
        )
        assert evidence.json()["result"]["structuredContent"]["source_trace_valid"] is True
        unknown = client.post(
            f"{base}/mcp",
            json=rpc("tools/call", {"name": "not_a_tool", "arguments": {}}),
            headers=bearer(),
        )
        assert unknown.json()["error"]["code"] == -32602
        assert client.post(f"{base}/logout").status_code == 204
        assert client.get(f"{base}/api/v1/libraries").status_code == 401


def test_auth_isolation_and_rate_limit(tmp_path, live_server, monkeypatch) -> None:
    monkeypatch.setenv("CITATION_ACCESS_TOKEN_SHA256", token_hash(TOKEN))
    app = create_app(auth_settings(tmp_path), reason_generator=StubReasonGenerator())
    app.state.auth.register_token("other-token", "other")
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        for _ in range(5):
            assert client.post(f"{base}/login", json={"token": "bad"}).status_code == 401
        limited = client.post(f"{base}/login", json={"token": TOKEN})
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        created = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=bearer(),
        )
        query_id = created.json()["query_id"]
        wait_final(client, base, query_id, request_headers=bearer())
        assert (
            client.get(
                f"{base}/api/v1/searches/{query_id}", headers=bearer("other-token")
            ).status_code
            == 404
        )
        other = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=bearer("other-token"),
        )
        assert other.status_code == 202
        assert other.json()["query_id"] != query_id


def test_production_auth_fails_closed_without_token_hash(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CITATION_ACCESS_TOKEN_SHA256", raising=False)
    with pytest.raises(ValueError, match="CITATION_ACCESS_TOKEN_SHA256"):
        create_app(auth_settings(tmp_path))


def test_anonymous_mode_needs_no_token_for_api_or_mcp(tmp_path, live_server) -> None:
    app = create_app(Settings.for_tests(tmp_path), reason_generator=StubReasonGenerator())
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        health = client.get(f"{base}/healthz")
        assert health.status_code == 200
        assert health.json()["authentication_required"] is False
        assert client.get(f"{base}/api/v1/libraries").status_code == 200
        assert client.post(f"{base}/login", json={"token": "not-used"}).status_code == 404
        initialized = client.post(f"{base}/mcp", json=rpc("initialize"))
        assert initialized.status_code == 200
        assert initialized.json()["result"]["protocolVersion"] == "2025-03-26"
