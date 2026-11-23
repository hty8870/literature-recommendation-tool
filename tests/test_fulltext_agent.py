from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

import citation_tool.manuscript_tools as manuscript_tools_module
from citation_tool.agent import (
    AgentPurgeBusy,
    LiteratureSearchTool,
    _restrict_library_ids,
)
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.entities import SearchCreate
from citation_tool.manuscript_tools import (
    build_manuscript_context,
    build_manuscript_tools,
)
from citation_tool.manuscripts import ManuscriptService
from citation_tool.search import SearchRequestError
from test_agent import ScriptedChatModel, wait_agent
from test_integration import headers, search_body, wait_final
from test_manuscripts import _docx_bytes, _upload_and_wait


@pytest.fixture
def manuscripts(tmp_path):
    root = tmp_path / "manuscripts"
    root.mkdir()
    service = ManuscriptService(
        root / "manuscripts.sqlite",
        root / "storage",
        parse_concurrency=1,
    )
    try:
        yield service
    finally:
        service.close()


def ready_manuscript(service: ManuscriptService, tenant: str = "local") -> dict:
    status = _upload_and_wait(service, _docx_bytes(), tenant=tenant)
    assert status["parse_status"] == "ready", status
    return status


def fulltext_body(manuscript_id: str, passage_index: int | None = None) -> dict:
    body = {
        "input_type": "full_text",
        "manuscript_id": manuscript_id,
        "topic_context": ["PCR", "Introduction"],
        "library_ids": ["fixture-fast"],
        "options": {"library_delays_ms": {"fixture-fast": 1}, "soft_deadline_ms": 5000},
    }
    if passage_index is not None:
        body["manuscript_passage_index"] = passage_index
    return body


def test_fulltext_search_uses_one_real_passage_not_the_whole_manuscript(
    tmp_path, live_server, manuscripts
) -> None:
    status = ready_manuscript(manuscripts)
    manuscript_id = status["manuscript_id"]
    passage = manuscripts.passages("local", manuscript_id, offset=1, limit=1)["passages"][0]
    app = create_app(Settings.for_tests(tmp_path))
    app.state.search_service.manuscripts = manuscripts
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/searches",
            json=fulltext_body(manuscript_id, 1),
            headers=headers(key="fulltext-one"),
        )
        assert response.status_code == 202, response.text
        created = response.json()
        claim = created["claims"][0]
        assert claim["input_type"] == "full_text"
        assert claim["claim_text"] == passage["text"].strip()
        assert claim["claim_rule_version"] == "claim-rule-v3-manuscript-passage"
        assert claim["manuscript_anchor"]["manuscript_id"] == manuscript_id
        assert claim["manuscript_anchor"]["content_sha256"] == status["sha256"]
        assert claim["manuscript_anchor"]["passage_index"] == 1
        assert claim["manuscript_anchor"]["page"] == passage["page"]
        assert claim["manuscript_anchor"]["char_start"] == passage["char_start"]
        assert claim["manuscript_anchor"]["char_end"] == passage["char_end"]
        assert claim["manuscript_anchor"]["quote"] == passage["text"].strip()
        assert claim["evaluation_anchor"]["claim_text"] == passage["text"].strip()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["claim_count"] == 1
        assert state["session"]["input_type"] == "full_text"

        missing_index = client.post(
            f"{base}/api/v1/searches",
            json=fulltext_body(manuscript_id),
            headers=headers(key="fulltext-no-index"),
        )
        assert missing_index.status_code == 400, missing_index.text
        assert "manuscript_passage_index" in missing_index.text

        missing_passage = client.post(
            f"{base}/api/v1/searches",
            json=fulltext_body(manuscript_id, 9999),
            headers=headers(key="fulltext-missing-passage"),
        )
        assert missing_passage.status_code == 400, missing_passage.text
        assert "MANUSCRIPT_PASSAGE_NOT_FOUND" in missing_passage.text

    other = ready_manuscript(manuscripts, tenant="other-tenant")
    with httpx.Client(timeout=20) as client:
        cross_tenant = client.post(
            f"{base}/api/v1/searches",
            json=fulltext_body(other["manuscript_id"], 0),
            headers=headers(key="fulltext-cross-tenant"),
        )
        assert cross_tenant.status_code == 400, cross_tenant.text
        assert "MANUSCRIPT_NOT_FOUND" in cross_tenant.text

    class NotReady:
        def get(self, tenant_id, manuscript_id, *, catalog_limit=0):
            return {
                "manuscript_id": manuscript_id,
                "parse_status": "parsing",
                "error": None,
            }

    app.state.search_service.manuscripts = NotReady()
    with httpx.Client(timeout=20) as client:
        not_ready = client.post(
            f"{base}/api/v1/searches",
            json=fulltext_body(manuscript_id, 1),
            headers=headers(key="fulltext-not-ready"),
        )
        assert not_ready.status_code == 400, not_ready.text
        assert "MANUSCRIPT_NOT_READY" in not_ready.text

    with pytest.raises(ValueError):
        SearchCreate.model_validate(
            {
                "input_type": "keywords",
                "keywords": ["LFY2"],
                "manuscript_passage_index": 0,
            }
        )


def test_agent_fulltext_origin_gets_catalog_and_can_read_without_search(
    tmp_path, live_server, manuscripts
) -> None:
    status = ready_manuscript(manuscripts)
    manuscript_id = status["manuscript_id"]
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "read-catalog",
                        "name": "read_manuscript",
                        "args": {"offset": 0, "limit": 2},
                    }
                ],
            ),
            AIMessage(content="已读取前两段；没有执行新的文献检索。"),
        ]
    )
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    app.state.search_service.manuscripts = manuscripts
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json=fulltext_body(manuscript_id),
            headers=headers(key="fulltext-agent-read"),
        )
        assert created.status_code == 202, created.text
        run = wait_agent(client, base, created.json()["run_id"])
        assert run["status"] == "completed", run["error"]
        assert run["queries"] == []
        assert run["tool_calls"][0]["name"] == "read_manuscript"
        assert run["tool_calls"][0]["status"] == "completed"
        assert run["manuscript_reads"]
        assert run["manuscript_reads"][0]["passage_indices"]
        assert "text" not in run["manuscript_reads"][0]
        prompt = "\n".join(str(message.content) for message in model.received_messages[0])
        assert manuscript_id in prompt
        assert '"catalog"' in prompt
        assert "legal_manuscript_ids" in prompt
        assert "read_manuscript" in prompt
        assert not any(isinstance(message, ToolMessage) for message in model.received_messages[0])
        passages = manuscripts.passages("local", manuscript_id, offset=0, limit=5)["passages"]
        sentinels = [
            passage["text"].strip() for passage in passages if len(passage["text"].strip()) >= 15
        ]
        assert sentinels
        assert all(text not in json.dumps(run, ensure_ascii=False) for text in sentinels)


async def test_follow_up_keeps_conversation_manuscript_without_copying_full_text(
    tmp_path, manuscripts
) -> None:
    status = ready_manuscript(manuscripts)
    manuscript_id = status["manuscript_id"]
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="先说明目录；本轮不检索。"),
            AIMessage(content="继续解释原有稿件；本轮同样不检索。"),
        ]
    )
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    app.state.search_service.manuscripts = manuscripts
    runner = app.state.agent_runner
    origin = SearchCreate.model_validate(fulltext_body(manuscript_id))
    first = runner.start("local", origin, None)
    await runner._tasks[first.run_id]
    assert first.conversation_id is not None
    follow_up = runner.start(
        "local",
        SearchCreate.model_validate(
            {
                "input_type": "keywords",
                "keywords": ["PCR"],
                "library_ids": ["fixture-fast"],
            }
        ),
        None,
        conversation_id=first.conversation_id,
        message="继续解释前两段，不要重新检索。",
    )
    await runner._tasks[follow_up.run_id]
    second = app.state.store.get_agent_run("local", follow_up.run_id)
    assert second.status == "completed"
    assert second.queries == []
    assert second.context_manifest["manuscript"]["manuscript_id"] == manuscript_id
    prompt = "\n".join(str(message.content) for message in model.received_messages[1])
    assert manuscript_id in prompt
    assert "legal_manuscript_ids" in prompt
    conversation = app.state.store.get_conversation("local", first.conversation_id)
    passages = manuscripts.passages("local", manuscript_id, offset=0, limit=5)["passages"]
    sentinels = [
        passage["text"].strip() for passage in passages if len(passage["text"].strip()) >= 15
    ]
    assert sentinels
    assert all(text not in conversation.model_dump_json() for text in sentinels)


def test_agent_can_choose_a_passage_and_search_it(tmp_path, live_server, manuscripts) -> None:
    status = ready_manuscript(manuscripts)
    manuscript_id = status["manuscript_id"]
    passage = manuscripts.passages("local", manuscript_id, offset=1, limit=1)["passages"][0]
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "fulltext-search",
                        "name": "search_literature",
                        "args": {
                            "input_type": "full_text",
                            "manuscript_id": manuscript_id,
                            "manuscript_passage_index": 1,
                            "topic_context": ["PCR"],
                            "library_ids": ["fixture-fast"],
                            "options": {
                                "library_delays_ms": {"fixture-fast": 1},
                                "soft_deadline_ms": 5000,
                            },
                        },
                    }
                ],
            ),
            AIMessage(content="已针对选定段落完成候选检索；结论仍需核查原文。"),
        ]
    )
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    app.state.search_service.manuscripts = manuscripts
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json=fulltext_body(manuscript_id),
            headers=headers(key="fulltext-agent-search"),
        )
        assert created.status_code == 202, created.text
        run = wait_agent(client, base, created.json()["run_id"])
        assert run["status"] == "completed", run["error"]
        assert len(run["queries"]) == 1
        assert run["tool_calls"][0]["name"] == "search_literature"
        assert run["tool_calls"][0]["arguments"]["manuscript_passage_index"] == 1
        wait_final(client, base, run["latest_query_id"])
        claim = run["queries"][0]["created"]["claims"][0]
        assert claim["input_type"] == "full_text"
        assert claim["claim_text"] == passage["text"].strip()
        assert claim["manuscript_anchor"]["passage_index"] == 1
        assert "[本次所选真实段落]" in claim["evaluation_anchor"]["claim_text"]
        assert passage["text"].strip() in claim["evaluation_anchor"]["claim_text"]


async def test_read_manuscript_tool_is_bounded_and_conversation_scoped(
    tmp_path, manuscripts, monkeypatch
) -> None:
    doc_a = ready_manuscript(manuscripts)["manuscript_id"]
    doc_b = ready_manuscript(manuscripts)["manuscript_id"]
    other_tenant = ready_manuscript(manuscripts, tenant="other-tenant")["manuscript_id"]

    tools = build_manuscript_tools(
        manuscripts,
        tenant_id="local",
        current_manuscript_id=doc_b,
        document_ids=[doc_b],
    )
    assert tools
    foreign = await tools[0].ainvoke({"manuscript_id": doc_a, "offset": 0, "limit": 1})
    assert foreign["error"]["code"] == "MANUSCRIPT_NOT_ALLOWED"
    assert "passages" not in foreign

    allowed = await tools[0].ainvoke({})
    assert allowed["manuscript_id"] == doc_b
    assert allowed["passages"]
    assert allowed["read_manifest"]["legal_manuscript_ids"] == [doc_b]

    cross_tools = build_manuscript_tools(
        manuscripts,
        tenant_id="local",
        current_manuscript_id=other_tenant,
        document_ids=[other_tenant],
    )
    cross = await cross_tools[0].ainvoke({"manuscript_id": other_tenant})
    assert cross["error"]["code"] == "MANUSCRIPT_NOT_FOUND"

    monkeypatch.setattr(manuscript_tools_module, "MAX_CALL_CHARS", 10)
    capped = build_manuscript_tools(
        manuscripts,
        tenant_id="local",
        current_manuscript_id=doc_b,
        document_ids=[doc_b],
    )
    limited = await capped[0].ainvoke({"offset": 0, "limit": 2})
    assert limited["passages"]
    assert all(len(item["text"]) <= 10 for item in limited["passages"])
    assert any(item["text_truncated"] for item in limited["passages"])

    monkeypatch.setattr(manuscript_tools_module, "MAX_CALLS", 1)
    one_call = build_manuscript_tools(
        manuscripts,
        tenant_id="local",
        current_manuscript_id=doc_b,
        document_ids=[doc_b],
    )
    assert "passages" in await one_call[0].ainvoke({})
    exhausted = await one_call[0].ainvoke({})
    assert exhausted["error"]["code"] == "MANUSCRIPT_READ_BUDGET_EXCEEDED"

    context = build_manuscript_context(
        manuscripts,
        tenant_id="local",
        current_manuscript_id=doc_b,
        document_ids=[doc_b],
    )
    assert context is not None
    assert context["catalog"]
    assert len(context["catalog"]) <= 20
    assert all("text" not in entry for entry in context["catalog"])
    assert context["legal_manuscript_ids"] == [doc_b]


def test_literature_tool_rejects_libraries_outside_the_origin_run(tmp_path, live_server) -> None:
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "bad-libraries",
                        "name": "search_literature",
                        "args": {
                            "input_type": "keywords",
                            "keywords": ["LFY2"],
                            "topic_context": ["LFY2"],
                            "library_ids": ["fixture-fast", "fixture-slow"],
                        },
                    }
                ],
            ),
            AIMessage(content="没有创建越界查询；保留 origin 选库。"),
        ]
    )
    settings = Settings.for_tests(tmp_path)
    app = create_app(settings, agent_model=model)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json={
                **search_body({"fixture-fast": 1, "fixture-slow": 1}),
                "library_ids": ["fixture-fast"],
            },
            headers=headers(key="origin-library-guard"),
        )
        assert created.status_code == 202, created.text
        run = wait_agent(client, base, created.json()["run_id"])
        assert run["status"] == "completed", run["error"]
        assert run["queries"] == []
        assert run["tool_calls"][0]["name"] == "search_literature"
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0] == 0

    origin = SearchCreate.model_validate(
        {
            "input_type": "keywords",
            "keywords": ["LFY2"],
            "library_ids": ["fixture-fast"],
        }
    )
    assert _restrict_library_ids(origin, ["fixture-fast"]).library_ids == ["fixture-fast"]
    assert _restrict_library_ids(
        origin.model_copy(update={"library_ids": None}), ["fixture-fast"]
    ).library_ids == ["fixture-fast"]

    class NeverCreate:
        def create(self, *args, **kwargs):
            raise AssertionError("invalid library selection must not create a query")

    NeverCreate.settings = settings

    async def reject_invalid() -> None:
        tool = LiteratureSearchTool(
            NeverCreate(),
            "local",
            "guard-run",
            origin,
            allowed_library_ids=["fixture-fast"],
        ).as_tool()
        for bad in (
            [],
            ["fixture-fast", "fixture-fast"],
            ["fixture-slow"],
        ):
            result = await tool.ainvoke(
                {
                    "input_type": "keywords",
                    "keywords": ["LFY2"],
                    "library_ids": bad,
                }
            )
            assert result["error"]["code"] == "VALIDATION_ERROR", result
        fulltext_origin = SearchCreate.model_validate(
            {
                "input_type": "full_text",
                "manuscript_id": "ms_allowed",
                "topic_context": ["PCR"],
                "library_ids": ["fixture-fast"],
            }
        )
        fulltext_tool = LiteratureSearchTool(
            NeverCreate(),
            "local",
            "guard-fulltext",
            fulltext_origin,
            allowed_library_ids=["fixture-fast"],
            allowed_manuscript_ids=["ms_allowed"],
        ).as_tool()
        foreign_document = await fulltext_tool.ainvoke(
            {
                "input_type": "full_text",
                "manuscript_id": "ms_other",
                "manuscript_passage_index": 0,
                "library_ids": ["fixture-fast"],
            }
        )
        assert foreign_document["error"]["code"] == "MANUSCRIPT_NOT_ALLOWED"

    asyncio.run(reject_invalid())


async def test_purge_manuscript_cancels_and_deletes_only_associated_data(
    tmp_path, manuscripts
) -> None:
    local_status = ready_manuscript(manuscripts)
    local_doc = local_status["manuscript_id"]
    other_status = ready_manuscript(manuscripts, tenant="other-tenant")
    other_doc = other_status["manuscript_id"]

    model = ScriptedChatModel(responses=[AIMessage(content="仅解释目录，不检索。")])
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    service = app.state.search_service
    service.manuscripts = manuscripts
    runner = app.state.agent_runner

    local_search = SearchCreate.model_validate(fulltext_body(local_doc, 1))
    created, _ = service.create("local", local_search, None)
    checkpoint = app.state.store.create_checkpoint("local", created.query_id)
    await service.coordinator.wait(created.query_id, timeout_s=10)
    snapshot_id = checkpoint["snapshot_id"]
    assert app.state.store.get_snapshot("local", snapshot_id) is not None

    unrelated = SearchCreate.model_validate(
        {
            "input_type": "keywords",
            "keywords": ["LFY2"],
            "library_ids": ["fixture-fast"],
            "options": {"library_delays_ms": {"fixture-fast": 1}, "soft_deadline_ms": 5000},
        }
    )
    unrelated_created, _ = service.create("local", unrelated, None)
    await service.coordinator.wait(unrelated_created.query_id, timeout_s=10)

    other_search = SearchCreate.model_validate(fulltext_body(other_doc, 1))
    other_created, _ = service.create("other-tenant", other_search, None)
    await service.coordinator.wait(other_created.query_id, timeout_s=10)

    run = runner.start("local", local_search, None)
    await runner._tasks[run.run_id]
    conversation_id = run.conversation_id
    assert conversation_id is not None
    assert app.state.store.get_conversation("local", conversation_id) is not None

    runner._purging.add(("local", local_doc))
    with pytest.raises(AgentPurgeBusy):
        runner.start("local", local_search, None)
    runner._purging.discard(("local", local_doc))
    service.purging_documents.add(("local", local_doc))
    with pytest.raises(SearchRequestError):
        service.create("local", local_search, None)
    service.purging_documents.discard(("local", local_doc))

    receipt = await runner.purge_manuscript("local", local_doc)
    assert receipt.status == "completed"
    assert receipt.counts["searches"] >= 1
    assert receipt.counts["snapshots"] >= 1
    assert receipt.counts["agent_runs"] >= 1
    assert receipt.counts["conversations"] >= 1
    assert receipt.counts["conversation_messages"] >= 1
    assert receipt.limitations["external_model_logs"] == "not_covered"
    receipt_json = receipt.model_dump_json()
    assert local_doc not in receipt_json
    assert local_status["sha256"] not in receipt_json
    assert app.state.store.get_purge_receipt("local", receipt.receipt_id) == receipt

    assert service.state("local", created.query_id) is None
    assert app.state.store.get_snapshot("local", snapshot_id) is None
    assert app.state.store.get_agent_run("local", run.run_id) is None
    assert app.state.store.get_conversation("local", conversation_id) is None
    assert service.state("local", unrelated_created.query_id) is not None
    assert service.state("other-tenant", other_created.query_id) is not None
    assert ("local", local_doc) not in runner._purging
    assert ("local", local_doc) not in service.purging_documents


async def test_purge_cancels_an_in_flight_fulltext_query_before_deleting(
    tmp_path, manuscripts
) -> None:
    status = ready_manuscript(manuscripts)
    manuscript_id = status["manuscript_id"]
    app = create_app(
        Settings.for_tests(tmp_path),
        agent_model=ScriptedChatModel(responses=[AIMessage(content="unused")]),
    )
    service = app.state.search_service
    service.manuscripts = manuscripts
    body = SearchCreate.model_validate(
        {
            **fulltext_body(manuscript_id, 1),
            "options": {
                "library_delays_ms": {"fixture-fast": 5000},
                "soft_deadline_ms": 10000,
            },
        }
    )
    created, _ = service.create("local", body, None)
    live_state = service.state("local", created.query_id)
    assert live_state is not None and live_state.is_live is True
    receipt = await app.state.agent_runner.purge_manuscript("local", manuscript_id, timeout_s=5.0)
    assert receipt.counts["searches"] == 1
    assert service.state("local", created.query_id) is None
