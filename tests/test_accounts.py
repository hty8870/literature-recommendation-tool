from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import httpx
from langchain_core.messages import AIMessage

from citation_tool.accounts import ACCOUNT_COOKIE, token_hash
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.manuscripts import DOCX_CONTENT_TYPE
from test_agent import ScriptedChatModel, StubReasonGenerator, tool_call
from test_manuscripts import _docx_bytes

SAMPLE_PASSPHRASE = "browser acceptance fixture only!"  # noqa: S105
ORIGIN = "https://literature.example.test"
API = "/api/v1"


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


async def register(client, username):
    response = await client.post(
        f"{API}/account/register",
        headers={"Origin": ORIGIN},
        json={"username": username, "password": SAMPLE_PASSPHRASE},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["recovery_code"]) == 32
    client.headers.update({"Origin": ORIGIN, "X-Workspace-Account": body["account"]["id"]})
    return response, body


async def finished(client, started):
    assert started.status_code == 202, started.text
    run = started.json()
    async with asyncio.timeout(10):
        while run["status"] in ("pending", "running"):
            await asyncio.sleep(0.03)
            fresh = await client.get(f"{API}/agent/runs/{run['run_id']}")
            assert fresh.status_code == 200, fresh.text
            run = fresh.json()
    assert run["status"] == "completed", run.get("error")
    return run


async def test_private_history_and_artifacts_with_two_accounts_and_guest(tmp_path):
    model = ScriptedChatModel(
        responses=[
            tool_call("private", ["LFY2"], ["fixture-fast"]),
            AIMessage(content="A 的榜单只保存在当前会话。"),
            AIMessage(content="B 的独立会话。"),
            AIMessage(content="公共工作区仍无需登录。"),
            AIMessage(content="A 的独立第二个会话。"),
        ]
    )
    app = create_app(
        Settings.for_tests(tmp_path), agent_model=model, reason_generator=StubReasonGenerator()
    )
    async with client_for(app) as alice, client_for(app) as bob, client_for(app) as guest:
        _, alice_body = await register(alice, "Alice")
        await register(bob, "Bob")
        assert alice_body["account"]["username"] == "alice"
        assert (await guest.get(f"{API}/conversations")).json()["code"] == "ACCOUNT_REQUIRED"
        payload = {
            "input_type": "keywords",
            "keywords": ["private history check"],
            "library_ids": ["fixture-fast"],
        }
        arun = await finished(alice, await alice.post(f"{API}/agent/runs", json=payload))
        brun = await finished(bob, await bob.post(f"{API}/agent/runs", json=payload))
        grun = await finished(guest, await guest.post(f"{API}/agent/runs", json=payload))
        a_second = await finished(alice, await alice.post(f"{API}/agent/runs", json=payload))
        conversation_id = arun["conversation_id"]
        conversation_url = f"{API}/conversations/{conversation_id}"
        query_id = arun["latest_query_id"]
        search_url = f"{API}/searches/{query_id}"
        state = (await alice.get(search_url)).json()
        snapshot_id = state["session"]["final_snapshot_id"]
        assert snapshot_id
        snapshot_path = f"{API}/snapshots/{snapshot_id}"
        result = state["results"][0]
        citation_path = (
            f"{search_url}/citation-suggestion?result_id={result['result_id']}"
            f"&result_version={result['result_version']}&result_kind=recommendation"
        )
        protected = [
            conversation_url,
            search_url,
            search_url + "/events",
            snapshot_path,
            f"{API}/agent/runs/{arun['run_id']}",
            citation_path,
        ]
        for url in protected:
            response = await alice.get(url)
            assert response.status_code == 200, (url, response.text[:500])
            assert (await bob.get(url)).status_code == 404
            assert (await guest.get(url)).status_code == 404
        assert (
            await guest.get(conversation_url, headers={"X-Tenant-ID": arun["tenant_id"]})
        ).status_code == 400
        assert (
            await guest.get(
                conversation_url, headers={"X-Workspace-Account": alice_body["account"]["id"]}
            )
        ).status_code == 401
        assert (
            await alice.get(conversation_url, headers={"X-Tenant-ID": "somebody-else"})
        ).status_code == 200
        assert (await bob.delete(conversation_url)).status_code == 404
        assert (
            await alice.delete(conversation_url, headers={"Origin": "https://other.example"})
        ).status_code == 403
        assert (await alice.patch(conversation_url, json={"title": "  自己的  历史 "})).json()[
            "title"
        ] == "自己的 历史"
        first = (await alice.get(f"{API}/conversations", params={"limit": 1})).json()
        after = (
            await alice.get(f"{API}/conversations", params={"before": first["next_cursor"]})
        ).json()
        assert [row["conversation_id"] for row in first["items"] + after["items"]] == [
            a_second["conversation_id"],
            conversation_id,
        ]
        assert (await alice.delete(conversation_url)).status_code == 204
        for url in protected:
            assert (await alice.get(url)).status_code == 404
        assert len((await alice.get(f"{API}/conversations")).json()["items"]) == 1
        assert (await bob.get(f"{API}/conversations/{brun['conversation_id']}")).status_code == 200
        assert (
            await guest.get(f"{API}/conversations/{grun['conversation_id']}")
        ).status_code == 200
        mcp = await guest.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert mcp.status_code == 200 and "result" in mcp.json()
        upload = await alice.post(
            f"{API}/manuscripts",
            content=_docx_bytes(),
            headers={
                "X-Filename": "public-acceptance.docx",
                "Content-Type": DOCX_CONTENT_TYPE,
            },
        )
        assert upload.status_code == 202, upload.text
        document_id = upload.json()["manuscript_id"]
        document_dir = app.state.manuscripts._manuscript_dir(arun["tenant_id"], document_id)
        assert document_dir.is_dir()
        assert (await bob.get(f"{API}/manuscripts/{document_id}")).status_code == 404
        refused = await alice.post(f"{API}/account/delete", json={"current_password": "wrong"})
        assert refused.status_code == 401
        deleted = await alice.post(
            f"{API}/account/delete", json={"current_password": SAMPLE_PASSPHRASE}
        )
        assert deleted.status_code == 204, deleted.text
        assert not document_dir.exists()
        assert (await alice.get(f"{API}/account/session")).json()["account"] is None
        with sqlite3.connect(app.state.settings.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE tenant_id=?", (arun["tenant_id"],)
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM user_accounts WHERE username='alice'"
                ).fetchone()[0]
                == 0
            )
        assert (await bob.get(f"{API}/conversations/{brun['conversation_id']}")).status_code == 200
        assert (
            await guest.get(f"{API}/conversations/{grun['conversation_id']}")
        ).status_code == 200


async def test_account_lifecycle_and_no_plaintext_persistence(tmp_path):
    settings = replace(Settings.for_tests(tmp_path), base_path="/literature")
    app = create_app(settings)
    paths = "/literature" + API + "/account"
    async with client_for(app) as owner, client_for(app) as device, client_for(app) as recovered:
        denied = await owner.post(
            paths + "/register", json={"username": "owner", "password": SAMPLE_PASSPHRASE}
        )
        assert denied.status_code == 403
        reply = await owner.post(
            paths + "/register",
            headers={"Origin": ORIGIN},
            json={"username": "owner", "password": SAMPLE_PASSPHRASE},
        )
        assert reply.status_code == 201, reply.text
        cookie = reply.headers["set-cookie"].lower()
        assert all(
            s in cookie
            for s in ("httponly", "secure", "samesite=lax", "path=/literature", "max-age=")
        )
        assert reply.headers["cache-control"] == "no-store"
        raw_session = owner.cookies[ACCOUNT_COOKIE]
        registration = reply.json()
        owner.headers.update(
            {"Origin": ORIGIN, "X-Workspace-Account": registration["account"]["id"]}
        )
        assert (await owner.patch(paths + "/profile", json={"display_name": "论文工作台"})).json()[
            "account"
        ]["display_name"] == "论文工作台"
        login_data = {"username": "OWNER", "password": SAMPLE_PASSPHRASE}
        signed = await device.post(paths + "/login", headers={"Origin": ORIGIN}, json=login_data)
        assert signed.json()["account"]["display_name"] == "论文工作台"
        assert "recovery_code" not in signed.json()
        same_origin = {"Origin": ORIGIN}
        rotated = await owner.post(
            paths + "/password",
            json={
                "current_password": SAMPLE_PASSPHRASE,
                "new_password": "second passphrase fixture!",
            },
        )
        assert rotated.status_code == 200, rotated.text
        assert owner.cookies[ACCOUNT_COOKIE] != raw_session
        assert (await device.get(paths + "/session")).json()["account"] is None
        recovery = {
            "username": "owner",
            "password": "renewed passphrase fixture!",
            "recovery_code": rotated.json()["recovery_code"],
        }
        assert (
            await recovered.post(paths + "/recover", headers=same_origin, json=recovery)
        ).status_code == 200
        assert (
            await device.post(paths + "/recover", headers=same_origin, json=recovery)
        ).status_code == 401
        assert (await owner.get(paths + "/session")).json()["account"] is None
        assert (await recovered.post(paths + "/logout", headers=same_origin)).status_code == 204
        assert (await recovered.get(paths + "/session")).json()["account"] is None
        with sqlite3.connect(settings.db_path) as conn:
            dump = "\n".join(conn.iterdump())
        for secret in (
            raw_session,
            SAMPLE_PASSPHRASE,
            recovery["recovery_code"],
            registration["recovery_code"],
        ):
            assert secret not in dump
        assert "scrypt-15-8-3$" in dump
        assert token_hash(raw_session) not in dump  # Rotated sessions are removed, not reused.


async def test_account_rate_limiter_and_insecure_remote_are_explicit(tmp_path, monkeypatch):
    from citation_tool import accounts

    monkeypatch.setattr(accounts, "_derive", lambda *_: b"x" * 64)
    app = create_app(Settings.for_tests(tmp_path))
    async with client_for(app) as client:
        for _ in range(10):
            response = await client.post(
                f"{API}/account/login",
                headers={"Origin": ORIGIN},
                json={"username": "not-found", "password": "nope"},
            )
            assert response.json()["code"] == "ACCOUNT_CREDENTIALS_INVALID"
        assert (
            await client.post(
                f"{API}/account/login",
                headers={"Origin": ORIGIN},
                json={"username": "not-found", "password": "nope"},
            )
        ).status_code == 429
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://not-secure.example"
    ) as remote:
        assert (await remote.get(f"{API}/account/session")).json()[
            "code"
        ] == "ACCOUNT_HTTPS_REQUIRED"


async def test_account_survives_restart_then_expires_without_anonymous_downgrade(tmp_path):
    settings = Settings.for_tests(tmp_path)
    app = create_app(settings)
    async with client_for(app) as owner:
        _, identity = await register(owner, "restart-check")
        token = owner.cookies[ACCOUNT_COOKIE]
        account_id = identity["account"]["id"]

    restarted = create_app(settings)
    async with client_for(restarted) as client:
        client.cookies.set(ACCOUNT_COOKIE, token)
        assert (await client.get(f"{API}/account/session")).json()["account"]["id"] == account_id
        client.headers["X-Workspace-Account"] = account_id
        assert (await client.get(f"{API}/conversations")).status_code == 200
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute("UPDATE user_account_sessions SET expires_at=0")
        assert (await client.get(f"{API}/account/session")).json() == {"account": None}
        denied = await client.post(
            f"{API}/searches",
            headers={"Origin": ORIGIN},
            json={
                "input_type": "keywords",
                "keywords": ["must not create a guest search"],
            },
        )
        assert denied.status_code == 401 and denied.json()["code"] == "ACCOUNT_SESSION_EXPIRED"
        with sqlite3.connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 0
