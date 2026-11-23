from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
from langchain_core.messages import AIMessage

from citation_tool.api import create_app
from citation_tool.citation_sources import EuropePmcSourceResolver
from citation_tool.config import Settings
from citation_tool.manuscripts import DOCX_CONTENT_TYPE
from citation_tool.retrieval import FixtureLibraryAdapter, FixtureRecord, build_fixture_adapters
from test_agent import ScriptedChatModel as ScriptedAgentModel
from test_citation_sources import DOI, PMCID, TITLE, FakeTransport
from test_citation_texts import ScriptedCitationModel
from test_manuscripts import _docx_bytes

OWNER = "cascade-owner"
OTHER = "cascade-other"
PUBLIC_TEXT = (
    "Herbicide resistance and fitness were measured in backcross progenies of transgenic and "
    "wild soybean under greenhouse conditions. The BC1F1 plants showed stable expression of "
    "the exogenous EPSPS protein and measurable differences in seed dormancy and growth."
)
PUBLIC_QUOTE = (
    "Herbicide resistance and fitness were measured in backcross progenies of transgenic and "
    "wild soybean under greenhouse conditions."
)


class PublicSourceFixtureAdapter(FixtureLibraryAdapter):
    def search(self, claim):
        record = FixtureRecord(
            work_id=f"work:{DOI}",
            paper_id=f"paper:europepmc:PMC{PMCID}",
            title=TITLE,
            year=2026,
            doi=DOI,
            raw_text=PUBLIC_TEXT,
            quote=PUBLIC_QUOTE,
            section_path=("Results",),
            page=3,
            reading_order=1,
            display_score=80.0,
        )
        candidate = self._candidate(record, claim, record.display_score)
        return [
            replace(
                candidate,
                relationship="insufficient",
                source=candidate.source.model_copy(
                    update={
                        "authors": "Zhang L, Wang X",
                        "simulation": False,
                    }
                ),
                locator=candidate.locator.model_copy(
                    update={"source_uri": f"https://europepmc.org/article/PMC/{PMCID}"}
                ),
            )
        ]


class DelayedCitationModel:
    provider = "test"
    model_name = "delayed-citation"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()

    async def ainvoke(self, messages):
        if "审校器" in str(messages[0].content):
            return AIMessage(
                content=json.dumps(
                    {
                        "verdict": "supported",
                        "issues": [],
                        "scope_ok": True,
                        "secondary_ok": True,
                        "unsupported_claims": [],
                    }
                )
            )

        payload = json.loads(str(messages[1].content))
        evidence_id = payload["evidence"][0]["evidence_id"]
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await asyncio.shield(self.release.wait())
        self.returned.set()
        return AIMessage(
            content=json.dumps(
                {
                    "citation_text": "研究显示，回交大豆在除草剂抗性和适合度方面存在差异。",
                    "facts": [
                        {"statement": "回交大豆表现出相关差异。", "evidence_ids": [evidence_id]}
                    ],
                    "scope_limits": [],
                    "review_secondary": False,
                },
                ensure_ascii=False,
            )
        )


def _docx() -> bytes:
    body = f"<w:p><w:r><w:t>{PUBLIC_QUOTE}</w:t></w:r></w:p>"
    return _docx_bytes(body=body)


def _full_text_body(manuscript_id: str, passage_index: int, delay_ms: int) -> dict:
    return {
        "input_type": "full_text",
        "manuscript_id": manuscript_id,
        "manuscript_passage_index": passage_index,
        "topic_context": ["herbicide resistance", "backcross soybean"],
        "library_ids": ["fixture-fast"],
        "options": {
            "library_delays_ms": {"fixture-fast": delay_ms},
            "soft_deadline_ms": 5000,
        },
    }


def _headers(tenant: str) -> dict[str, str]:
    return {"X-Tenant-ID": tenant}


async def _wait_for(client, path, tenant, predicate, *, timeout_s: float = 15.0):
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = None
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(path, headers=_headers(tenant))
        assert response.status_code == 200, response.text
        last = response.json()
        if predicate(last):
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}; last response: {last!r}")


async def _upload_manuscript(client, tenant: str, filename: str) -> tuple[str, int]:
    response = await client.post(
        "/api/v1/manuscripts",
        content=_docx(),
        headers={
            **_headers(tenant),
            "Content-Type": DOCX_CONTENT_TYPE,
            "X-Filename": filename,
        },
    )
    assert response.status_code == 202, response.text
    manuscript_id = response.json()["manuscript_id"]
    record = await _wait_for(
        client,
        f"/api/v1/manuscripts/{manuscript_id}",
        tenant,
        lambda item: item["parse_status"] in {"ready", "failed"},
    )
    assert record["parse_status"] == "ready", record
    passages = await client.get(
        f"/api/v1/manuscripts/{manuscript_id}/passages", headers=_headers(tenant)
    )
    assert passages.status_code == 200, passages.text
    assert passages.json()["passages"]
    return manuscript_id, passages.json()["passages"][0]["passage_index"]


async def _wait_for_agent_query(client, run_id: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 10
    last = None
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/api/v1/agent/runs/{run_id}", headers=_headers(OWNER))
        assert response.status_code == 200, response.text
        last = response.json()
        if last["queries"]:
            return last
        assert last["status"] not in {"completed", "failed", "cancelled"}, last
        await asyncio.sleep(0.01)
    raise AssertionError(f"agent did not attach its search query: {last!r}")


async def _wait_for_agent(client, run_id: str) -> dict:
    return await _wait_for(
        client,
        f"/api/v1/agent/runs/{run_id}",
        OWNER,
        lambda item: item["status"] in {"completed", "failed", "cancelled"},
    )


async def _create_checkpoint(client, query_id: str, tenant: str) -> str:
    response = await client.post(
        f"/api/v1/searches/{query_id}/snapshots",
        json={"kind": "checkpoint"},
        headers=_headers(tenant),
    )
    assert response.status_code == 201, response.text
    manifest = response.json()
    snapshot_id = manifest["snapshot_id"]
    fetched = await client.get(f"/api/v1/snapshots/{snapshot_id}", headers=_headers(tenant))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["query_id"] == query_id
    return snapshot_id


async def _create_search(client, tenant: str, body: dict) -> tuple[str, str]:
    response = await client.post("/api/v1/searches", json=body, headers=_headers(tenant))
    assert response.status_code == 202, response.text
    query_id = response.json()["query_id"]
    snapshot_id = await _create_checkpoint(client, query_id, tenant)
    return query_id, snapshot_id


async def _wait_for_search(client, query_id: str, tenant: str) -> dict:
    return await _wait_for(
        client,
        f"/api/v1/searches/{query_id}",
        tenant,
        lambda item: (
            item["session"]["search_state"] in {"completed", "partial", "failed", "cancelled"}
        ),
    )


async def _create_ready_citation(
    client, query_id: str, candidate: dict, tenant: str, style: str
) -> str:
    response = await client.post(
        "/api/v1/citation-texts",
        json={
            "query_id": query_id,
            "result_id": candidate["result_id"],
            "result_version": candidate["pool_version"],
            "result_kind": "candidate",
            "citation_style": style,
            "language": "zh",
        },
        headers=_headers(tenant),
    )
    assert response.status_code == 202, response.text
    citation_text_id = response.json()["citation_text_id"]
    record = await _wait_for(
        client,
        f"/api/v1/citation-texts/{citation_text_id}",
        tenant,
        lambda item: item["status"] in {"ready", "failed", "cancelled"},
    )
    assert record["status"] == "ready", record
    assert record["citation_text"]
    return citation_text_id


async def _assert_events_available(client, query_id: str, tenant: str) -> None:
    response = await client.get(f"/api/v1/searches/{query_id}/events", headers=_headers(tenant))
    assert response.status_code == 200, response.text
    assert "data:" in response.text


async def _cancel_background_work(app) -> None:
    agent_tasks = list(app.state.agent_runner._tasks.values())
    for task in agent_tasks:
        if not task.done():
            task.cancel()
    if agent_tasks:
        await asyncio.gather(*agent_tasks, return_exceptions=True)
    search_tasks = list(app.state.coordinator._tasks)
    for task in search_tasks:
        if not task.done():
            task.cancel()
    if search_tasks:
        await asyncio.gather(*search_tasks, return_exceptions=True)


async def test_delete_manuscript_api_purges_associated_graph_and_blocks_late_citation(
    tmp_path,
):
    """FND-059: Purge manuscript-linked resources through the unified API route."""
    settings = Settings.for_tests(tmp_path)
    adapters = build_fixture_adapters(settings)
    adapters["fixture-fast"] = PublicSourceFixtureAdapter(settings.library("fixture-fast"))
    source = EuropePmcSourceResolver(transport=FakeTransport(), timeout_ms=5000, max_evidence=3)
    agent_model = ScriptedAgentModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "cascade-fulltext-search",
                        "name": "search_literature",
                        "args": {},
                    }
                ],
            ),
            AIMessage(content="已针对稿件段落创建检索记录，结果仍需作者核查。"),
        ]
    )
    citation_model = ScriptedCitationModel()
    app = create_app(
        settings,
        adapters=adapters,
        agent_model=agent_model,
        citation_model=citation_model,
        citation_review_model=citation_model,
        citation_resolver=source,
    )
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test.local"
            ) as client:
                owner_manuscript_id, owner_passage_index = await _upload_manuscript(
                    client, OWNER, "owner.docx"
                )
                other_manuscript_id, other_passage_index = await _upload_manuscript(
                    client, OTHER, "other.docx"
                )

                owner_body = _full_text_body(
                    owner_manuscript_id, owner_passage_index, delay_ms=3000
                )
                tool_args = _full_text_body(owner_manuscript_id, owner_passage_index, 3000)
                agent_model.responses[0] = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "cascade-fulltext-search",
                            "name": "search_literature",
                            "args": tool_args,
                        }
                    ],
                )
                created_run = await client.post(
                    "/api/v1/agent/runs", json=owner_body, headers=_headers(OWNER)
                )
                assert created_run.status_code == 202, created_run.text
                run_id = created_run.json()["run_id"]
                conversation_id = created_run.json()["conversation_id"]
                run_with_query = await _wait_for_agent_query(client, run_id)
                assert len(run_with_query["queries"]) == 1
                owner_query_id = run_with_query["queries"][0]["created"]["query_id"]
                owner_snapshot_id = await _create_checkpoint(client, owner_query_id, OWNER)

                unrelated_query_id, unrelated_snapshot_id = await _create_search(
                    client,
                    OWNER,
                    {
                        "input_type": "keywords",
                        "keywords": ["herbicide resistance", "backcross soybean"],
                        "library_ids": ["fixture-fast"],
                        "options": {
                            "library_delays_ms": {"fixture-fast": 1800},
                            "soft_deadline_ms": 5000,
                        },
                    },
                )
                other_query_id, other_snapshot_id = await _create_search(
                    client,
                    OTHER,
                    _full_text_body(other_manuscript_id, other_passage_index, 1800),
                )

                completed_run = await _wait_for_agent(client, run_id)
                assert completed_run["status"] == "completed", completed_run
                assert completed_run["queries"]
                owner_state = await _wait_for_search(client, owner_query_id, OWNER)
                unrelated_state = await _wait_for_search(client, unrelated_query_id, OWNER)
                other_state = await _wait_for_search(client, other_query_id, OTHER)
                assert owner_state["candidate_diagnostics"]
                assert unrelated_state["candidate_diagnostics"]
                assert other_state["candidate_diagnostics"]

                conversation_response = await client.get(
                    f"/api/v1/conversations/{conversation_id}", headers=_headers(OWNER)
                )
                assert conversation_response.status_code == 200, conversation_response.text
                conversation = conversation_response.json()
                assert conversation["messages"]
                assert any(
                    artifact["query_id"] == owner_query_id for artifact in conversation["artifacts"]
                )
                await _assert_events_available(client, owner_query_id, OWNER)
                await _assert_events_available(client, unrelated_query_id, OWNER)
                await _assert_events_available(client, other_query_id, OTHER)

                owner_candidate = owner_state["candidate_diagnostics"][0]
                unrelated_candidate = unrelated_state["candidate_diagnostics"][0]
                other_candidate = other_state["candidate_diagnostics"][0]
                ready_citation_id = await _create_ready_citation(
                    client, owner_query_id, owner_candidate, OWNER, "apa"
                )
                unrelated_citation_id = await _create_ready_citation(
                    client, unrelated_query_id, unrelated_candidate, OWNER, "apa"
                )
                other_citation_id = await _create_ready_citation(
                    client, other_query_id, other_candidate, OTHER, "apa"
                )

                delayed_model = DelayedCitationModel()
                app.state.citation_texts._model = delayed_model
                inflight_response = await client.post(
                    "/api/v1/citation-texts",
                    json={
                        "query_id": owner_query_id,
                        "result_id": owner_candidate["result_id"],
                        "result_version": owner_candidate["pool_version"],
                        "result_kind": "candidate",
                        "citation_style": "vancouver",
                        "language": "zh",
                    },
                    headers=_headers(OWNER),
                )
                assert inflight_response.status_code == 202, inflight_response.text
                inflight_citation_id = inflight_response.json()["citation_text_id"]
                await asyncio.wait_for(delayed_model.started.wait(), timeout=5)
                inflight_record = await client.get(
                    f"/api/v1/citation-texts/{inflight_citation_id}",
                    headers=_headers(OWNER),
                )
                assert inflight_record.status_code == 200, inflight_record.text
                assert inflight_record.json()["status"] == "generating"

                delete_task = asyncio.create_task(
                    client.delete(
                        f"/api/v1/manuscripts/{owner_manuscript_id}",
                        headers=_headers(OWNER),
                    )
                )
                try:
                    await asyncio.wait_for(delayed_model.cancelled.wait(), timeout=5)
                finally:
                    delayed_model.release.set()
                deleted = await asyncio.wait_for(delete_task, timeout=15)
                assert deleted.status_code == 204, deleted.text
                await asyncio.wait_for(delayed_model.returned.wait(), timeout=5)
                await asyncio.sleep(0.05)

                assert (
                    await client.get(
                        f"/api/v1/manuscripts/{owner_manuscript_id}",
                        headers=_headers(OWNER),
                    )
                ).status_code == 404
                assert (
                    await client.get(
                        f"/api/v1/manuscripts/{owner_manuscript_id}/passages",
                        headers=_headers(OWNER),
                    )
                ).status_code == 404
                for path in (
                    f"/api/v1/searches/{owner_query_id}",
                    f"/api/v1/searches/{owner_query_id}/events",
                    f"/api/v1/agent/runs/{run_id}",
                    f"/api/v1/conversations/{conversation_id}",
                    f"/api/v1/snapshots/{owner_snapshot_id}",
                    f"/api/v1/citation-texts/{ready_citation_id}",
                    f"/api/v1/citation-texts/{inflight_citation_id}",
                ):
                    response = await client.get(path, headers=_headers(OWNER))
                    assert response.status_code == 404, (path, response.text)

                retry = await client.post(
                    "/api/v1/citation-texts",
                    json={
                        "query_id": owner_query_id,
                        "result_id": owner_candidate["result_id"],
                        "result_version": owner_candidate["pool_version"],
                        "result_kind": "candidate",
                        "citation_style": "vancouver",
                        "language": "zh",
                    },
                    headers=_headers(OWNER),
                )
                assert retry.status_code == 410, retry.text
                assert retry.json()["code"] == "CITATION_QUERY_PURGED"

                for query_id, snapshot_id, citation_id, tenant in (
                    (
                        unrelated_query_id,
                        unrelated_snapshot_id,
                        unrelated_citation_id,
                        OWNER,
                    ),
                    (other_query_id, other_snapshot_id, other_citation_id, OTHER),
                ):
                    assert (
                        await client.get(f"/api/v1/searches/{query_id}", headers=_headers(tenant))
                    ).status_code == 200
                    assert (
                        await client.get(
                            f"/api/v1/snapshots/{snapshot_id}", headers=_headers(tenant)
                        )
                    ).status_code == 200
                    assert (
                        await client.get(
                            f"/api/v1/searches/{query_id}/events", headers=_headers(tenant)
                        )
                    ).status_code == 200
                    retained_citation = await client.get(
                        f"/api/v1/citation-texts/{citation_id}", headers=_headers(tenant)
                    )
                    assert retained_citation.status_code == 200, retained_citation.text
                    assert retained_citation.json()["status"] == "ready"
                assert (
                    await client.get(
                        f"/api/v1/manuscripts/{other_manuscript_id}",
                        headers=_headers(OTHER),
                    )
                ).status_code == 200
        finally:
            await _cancel_background_work(app)
