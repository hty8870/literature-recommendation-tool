from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import AIMessage

from citation_tool.api import create_app
from citation_tool.citation_texts import CitationTextError
from citation_tool.config import Settings
from citation_tool.reasons import DeepSeekModelGateway
from test_accounts import ORIGIN, SAMPLE_PASSPHRASE, client_for, finished, register
from test_agent import ScriptedChatModel

SETTINGS = "/api/v1/account/model-settings"
AGENT = "/api/v1/agent/runs"
DRAFT = {
    "api_type": "responses",
    "base_url": "https://api.example.com/v1/responses",
    "model": "lab-v1",
}
FAKE_KEY = "unit-test-marker-not-a-provider-key"
SEARCH = {"input_type": "keywords", "keywords": ["configuration check"]}


async def save(client, *, revision=None, model=DRAFT, key=FAKE_KEY):
    model = {**model, **({"api_key": key} if key is not None else {})} if model else None
    return await client.put(SETTINGS, json={"revision": revision, "model": model})


async def test_saved_model_used_by_clean_device_after_restart(tmp_path, monkeypatch):
    settings = Settings.for_tests(tmp_path)
    first = create_app(settings)
    async with first.router.lifespan_context(first), client_for(first) as owner:
        _, identity = await register(owner, "lab-owner")
        result = await save(owner)
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["model"] == {**DRAFT, "base_url": "https://api.example.com/v1"}
        assert result.headers["cache-control"] == "no-store"
        assert FAKE_KEY not in result.text
        assert (await owner.get("/api/v1/libraries")).json()["account_model_sync"]
        assert (await owner.post("/api/v1/account/logout")).status_code == 204

    received = []

    def agent_model(gateway):
        received.append(gateway.config)
        return ScriptedChatModel(responses=[AIMessage(content="Used account model")])

    monkeypatch.setattr(DeepSeekModelGateway, "agent_model", agent_model)
    app = create_app(settings)

    async def citation(*args, model_config, **kwargs):
        received.append(model_config)
        raise CitationTextError("TEST_REQUEST_RECEIVED", 409, "test inspected resolved model")

    monkeypatch.setattr(app.state.citation_texts, "start", citation)
    async with (
        app.router.lifespan_context(app),
        client_for(app) as device,
        client_for(app) as other,
        client_for(app) as guest,
    ):
        assert (await guest.get(SETTINGS)).status_code == 401
        assert (await guest.put(SETTINGS, json={})).status_code == 401
        assert (
            await device.post(
                "/api/v1/account/login",
                headers={"Origin": ORIGIN},
                json={
                    "username": "lab-owner",
                    "password": SAMPLE_PASSPHRASE,
                },
            )
        ).status_code == 200
        device.headers.update({"Origin": ORIGIN, "X-Workspace-Account": identity["account"]["id"]})
        assert (await device.get(SETTINGS)).json() == saved
        await register(other, "other-member")
        assert (await other.get(SETTINGS)).json() == {"revision": None, "model": None}
        assert (await other.post(AGENT, json=SEARCH)).json()["code"] == "MODEL_NOT_CONFIGURED"
        assert (await guest.post(AGENT, json=SEARCH)).json()["code"] == "MODEL_NOT_CONFIGURED"
        assert (
            await other.get(
                SETTINGS,
                headers={
                    "X-Workspace-Account": identity["account"]["id"],
                },
            )
        ).json()["code"] == "WORKSPACE_CHANGED"

        run = await finished(device, await device.post(AGENT, json=SEARCH))
        assert run["model"] == "lab-v1" and FAKE_KEY not in str(run)
        second = await device.post(
            f"/api/v1/conversations/{run['conversation_id']}/turns", json={"message": "continue"}
        )
        assert (await finished(device, second))["model"] == "lab-v1"
        result = await device.post(
            "/api/v1/citation-texts",
            json={
                "query_id": "q_test",
                "result_id": "res_test",
                "result_version": 1,
                "citation_style": "vancouver",
                "language": "zh",
            },
        )
        assert result.json()["code"] == "TEST_REQUEST_RECEIVED"
        assert [config.api_key == FAKE_KEY for config in received] == [True, True, True]
        assert {config.base_url for config in received} == {"https://api.example.com/v1"}

        with sqlite3.connect(settings.db_path) as conn:
            dump = "\n".join(conn.iterdump())
            assert FAKE_KEY not in dump
            assert conn.execute(
                "SELECT typeof(sealed_key) FROM user_account_models"
            ).fetchone() == ("blob",)
        assert (
            await device.post(
                "/api/v1/account/delete",
                json={
                    "current_password": SAMPLE_PASSPHRASE,
                },
            )
        ).status_code == 204
        with sqlite3.connect(settings.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM user_account_models").fetchone() == (0,)


async def test_updates_keep_key_only_for_same_endpoint_and_do_not_overwrite_other_devices(tmp_path):
    app = create_app(Settings.for_tests(tmp_path))
    async with app.router.lifespan_context(app), client_for(app) as owner:
        await register(owner, "profile-check")
        first = (await save(owner)).json()
        assert (await save(owner)).json()["code"] == "ACCOUNT_MODEL_CHANGED"
        bad_origin = await owner.put(
            SETTINGS,
            headers={"Origin": "https://elsewhere.example"},
            json={"revision": first["revision"], "model": None},
        )
        assert bad_origin.status_code == 403
        for model in (
            {**DRAFT, "base_url": "https://new.example.com/v1"},
            {**DRAFT, "base_url": "https://127.0.0.1/v1"},
            {**DRAFT, "api_type": "chat_completions", "base_url": "https://api.example.com/v1"},
        ):
            assert (
                await save(owner, revision=first["revision"], model=model, key=None)
            ).status_code == 400
        changed = await save(
            owner, revision=first["revision"], model={**DRAFT, "model": "lab-v2"}, key=None
        )
        assert changed.status_code == 200
        assert changed.json()["model"]["model"] == "lab-v2"
        assert (await save(owner, revision=first["revision"], model=None)).status_code == 409
        assert (await save(owner, revision=changed.json()["revision"], model=None)).json() == {
            "model": None,
            "revision": None,
        }
        assert (await owner.post(AGENT, json=SEARCH)).json()["code"] == "MODEL_NOT_CONFIGURED"


@pytest.mark.parametrize("damage", ["other-account", "changed-address", "missing-key"])
async def test_envelope_cannot_be_reused_and_does_not_fall_back(tmp_path, damage):
    settings = Settings.for_tests(tmp_path)
    app = create_app(settings, agent_model=ScriptedChatModel(responses=[]))
    async with app.router.lifespan_context(app), client_for(app) as alice, client_for(app) as bob:
        _, owner = await register(alice, "envelope-a")
        _, other = await register(bob, "envelope-b")
        assert (await save(alice)).status_code == 200
        denied = alice
        if damage == "missing-key":
            settings.db_path.with_name("app.db.account-model.key").unlink()
        else:
            with sqlite3.connect(settings.db_path) as conn:
                if damage == "changed-address":
                    conn.execute(
                        "UPDATE user_account_models SET base_url='https://else.example.com'"
                    )
                else:
                    conn.execute(
                        "UPDATE user_account_models SET account_id=? WHERE account_id=?",
                        (other["account"]["id"], owner["account"]["id"]),
                    )
                    denied = bob
        response = await denied.post(AGENT, json=SEARCH)
        assert (
            response.status_code == 503 and response.json()["code"] == "ACCOUNT_MODEL_UNAVAILABLE"
        )
        assert FAKE_KEY not in response.text
