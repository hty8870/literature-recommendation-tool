from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
from langchain_core.messages import AIMessage

from citation_tool import project_schema
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.learning_web import DeepSeekWebSearch
from citation_tool.store import SCHEMA, Store
from test_accounts import API, ORIGIN, client_for, finished, register
from test_agent import ScriptedChatModel, StubReasonGenerator

ARXIV = "arxiv-online"
VERSION = "arxiv-online-metadata-v1"
ATOM_SAMPLE = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2101.02415v2</id><title>Machine learning model stability</title>
<summary>Machine learning model stability is assessed in this example.</summary>
<published>2021-01-01T00:00:00Z</published><author><name>A. Example</name></author>
</entry></feed>"""


def choose(name, **args):
    return AIMessage(content="", tool_calls=[{"id": f"call_{name}", "name": name, "args": args}])


async def project(client, name):
    result = await client.post(f"{API}/projects", json={"name": name})
    assert result.status_code == 201, result.text
    return result.json()


async def edit(client, data, **fields):
    return await client.patch(
        f"{API}/projects/{data['project_id']}", json={"revision": data["revision"], **fields}
    )


async def message(client, text, *, project_id=None, key=None, **kwargs):
    return await client.post(
        f"{API}/agent/runs",
        headers={"Idempotency-Key": key} if key else {},
        json={
            "input_type": "keywords",
            "keywords": [text],
            **({"project_id": project_id} if project_id else {}),
            **kwargs,
        },
    )


def test_project_migration_is_additive(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO conversations(tenant_id,conversation_id,title,created_at,updated_at) "
            "VALUES('legacy','old','保留', 'old-time', 'old-time')"
        )
    store = Store(path)
    assert Store(path).get_conversation("legacy", "old").title == "保留"
    assert store.list_conversations("legacy", project_id="other")[0]["project_id"] is None


async def test_shared_source_defaults_version_and_archive(tmp_path):
    model = ScriptedChatModel(responses=[AIMessage(content="完成。") for _ in range(5)])
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    async with client_for(app) as alice, client_for(app) as bob, client_for(app) as guest:
        await register(alice, "workspaceowner")
        await register(bob, "unrelated")
        first = await project(alice, "论文项目")
        other = await project(alice, "另一个项目")
        project_id = first["project_id"]
        assert first["share_sources"] is True
        assert (await guest.get(f"{API}/projects")).status_code == 401
        assert (await bob.get(f"{API}/projects/{project_id}")).status_code == 404
        assert (await message(guest, "test", project_id=project_id)).status_code == 401
        assert (await message(bob, "test", project_id=project_id)).status_code == 404
        assert (await edit(alice, first, library_ids=[])).status_code == 400
        stale = first
        first = (await edit(alice, first, library_ids=["fixture-fast"])).json()
        assert (await edit(alice, stale, library_ids=["fixture-slow"])).status_code == 409
        turn = await finished(
            alice, await message(alice, "A", project_id=project_id, key="group-a")
        )
        assert turn["request"]["library_ids"] == ["fixture-fast"]
        first = (await edit(alice, first, library_ids=["fixture-slow"])).json()
        replay = await message(alice, "A", project_id=project_id, key="group-a")
        assert replay.json()["run_id"] == turn["run_id"]
        assert (
            await message(alice, "A", project_id=other["project_id"], key="group-a")
        ).status_code == 409
        second = await finished(alice, await message(alice, "B", project_id=project_id))
        assert second["request"]["library_ids"] == ["fixture-slow"]
        conv = f"{API}/conversations/{turn['conversation_id']}"
        continued = await finished(
            alice, await alice.post(f"{conv}/turns", json={"message": "follow"})
        )
        assert continued["request"]["library_ids"] == ["fixture-slow"]
        first = (await edit(alice, first, share_sources=False)).json()
        independent = await finished(alice, await message(alice, "C", project_id=project_id))
        assert independent["request"]["library_ids"] == ["fixture-fast", "fixture-slow"]
        uncategorized = await finished(alice, await message(alice, "Miscellaneous"))
        assert (await alice.get(f"{API}/conversations/{uncategorized['conversation_id']}")).json()[
            "project_id"
        ] is None
        assert {
            row["project_id"]
            for row in (await alice.get(f"{API}/conversations?project_id={project_id}")).json()[
                "items"
            ]
        } == {project_id}
        assert len((await alice.get(f"{API}/conversations?project_id=other")).json()["items"]) == 1
        assert (await edit(alice, first, archived=True)).json()["archived"] is True
        assert (await alice.post(f"{conv}/turns", json={"message": "archived"})).json()[
            "code"
        ] == "PROJECT_ARCHIVED"
        assert (await alice.get(conv)).json()["project_id"] == project_id


async def test_learning_actual_read_approval_probe_availability_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(project_schema, "SCAN_DELAY_S", 0)
    settings = replace(Settings.for_tests(tmp_path), learning_enabled=True)
    model = ScriptedChatModel(responses=[AIMessage(content="我已记录这个检索需求。")])
    app = create_app(settings, agent_model=model, reason_generator=StubReasonGenerator())
    worker = app.state.learning
    requests = []

    def transport(url):
        requests.append(parse_qs(urlsplit(url).query))
        return ATOM_SAMPLE

    worker.adapters[ARXIV]._transport = transport
    worker.adapters[ARXIV]._next_at = 0
    external = []

    async def web_handler(request):
        external.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/fetch"):
            return httpx.Response(200, json={"title": "arXiv API", "content": "Public API manual"})
        return httpx.Response(200, json={"results": []})

    worker.web = DeepSeekWebSearch("unit-only-fake-key", transport=httpx.MockTransport(web_handler))
    async with client_for(app) as alice, client_for(app) as bob, client_for(app) as guest:
        await register(alice, "learningowner")
        await register(bob, "lookupother")
        selected = await project(alice, "仅本项目")
        p_id = selected["project_id"]
        assert await worker.run_one() is False
        selected = (await edit(alice, selected, learning_enabled=True)).json()
        run = await finished(
            alice, await message(alice, "请增加 arXiv 检索，私有项目标记。", project_id=p_id)
        )
        conv_id = run["conversation_id"]
        path = f"sessionlog/{conv_id}/0001.txt"
        evidence = [{"path": path, "line": 1}]
        model.responses.extend(
            [
                choose(
                    "propose_source",
                    connector_id=ARXIV,
                    reason="explicit_request",
                    evidence=evidence,
                ),
                choose("Read", path=path),
                choose("WebSearch", connector_id=ARXIV),
                choose("FetchURL", document_id="http://127.0.0.1/not-authorized"),
                choose("FetchURL", document_id=f"{ARXIV}:docs"),
                choose(
                    "propose_source",
                    connector_id=ARXIV,
                    reason="explicit_request",
                    evidence=evidence,
                ),
                AIMessage(content="已拟定申请。"),
            ]
        )
        assert await worker.run_one() is True
        assert external == [
            (
                "/v1/tools/search",
                {
                    "query": "arxiv metadata search API documentation terms of use",
                    "count": 6,
                    "need_content": False,
                },
            ),
            ("/v1/tools/fetch", {"url": "https://info.arxiv.org/help/api/user-manual.html"}),
        ]
        assert "PROPOSAL_EVIDENCE_NOT_READ" in str(model.received_messages[-1])
        assert "WEB_LOOKUP_NOT_AVAILABLE" in str(model.received_messages[-1])
        assert not requests
        assert await worker.run_one() is False
        inbox = await alice.get(f"{API}/source-requests")
        (item,) = inbox.json()["items"]
        assert item["status"] == "pending"
        assert item["conversation_id"] == conv_id
        assert "私有项目标记" not in inbox.text
        assert (await bob.get(f"{API}/source-requests")).json()["items"] == []
        endpoint = f"{API}/source-requests/{item['application_id']}/decision"
        decision = {"decision": "approve", "connector_version": VERSION}
        assert (await guest.post(endpoint, json=decision)).status_code == 401
        assert (await bob.post(endpoint, json=decision)).status_code == 404
        assert (
            await alice.post(endpoint, json=decision, headers={"Origin": "https://foreign.example"})
        ).status_code == 403
        assert (
            await alice.post(endpoint, json={**decision, "connector_version": "forged"})
        ).status_code == 409
        assert (
            await message(alice, "machine learning", project_id=p_id, library_ids=[ARXIV])
        ).status_code == 400
        assert (await alice.post(endpoint, json=decision, headers={"Origin": ORIGIN})).json()[
            "status"
        ] == "queued"
        assert (await alice.post(endpoint, json=decision)).json()["status"] == "queued"
        model.responses.extend(
            [
                choose("stage_verified_connector"),
                choose("probe_approved_connector"),
                choose("stage_verified_connector"),
                AIMessage(content="题录已通过探测。"),
            ]
        )
        assert await worker.run_one() is True
        (item,) = (await alice.get(f"{API}/source-requests")).json()["items"]
        assert item["status"] == "ready" and item["probe"]["records"] == 1
        assert len(requests) == 1
        selected = (await alice.get(f"{API}/projects/{p_id}")).json()
        assert ARXIV in selected["available_library_ids"] and ARXIV not in selected["library_ids"]
        assert (
            ARXIV not in (await alice.get(f"{API}/projects/other")).json()["available_library_ids"]
        )
        assert ARXIV not in [
            library["library_id"]
            for library in (await alice.get(f"{API}/libraries")).json()["libraries"]
        ]
        assert (
            await alice.post(
                f"{API}/searches",
                json={
                    "input_type": "keywords",
                    "keywords": ["machine learning"],
                    "library_ids": [ARXIV],
                },
            )
        ).status_code == 400
        assert (await alice.delete(f"{API}/conversations/{conv_id}")).status_code == 204
        (item,) = (await alice.get(f"{API}/source-requests")).json()["items"]
        assert item["status"] == "ready" and item["conversation_id"] is None
        assert (await alice.post(endpoint, json={**decision, "decision": "close"})).json()[
            "closed"
        ] is True
        assert (await alice.get(f"{API}/source-requests")).json()["items"] == []
