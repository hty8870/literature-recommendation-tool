from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import citation_tool.store as store_module
from citation_tool import conversations as conversation_context
from citation_tool.agent import (
    AgentRunCreate,
    _effective_evaluation_anchor,
    _mentioned_source_texts,
    _unprovided_dois,
)
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.conversations import build_conversation_context
from citation_tool.entities import (
    AgentRun,
    EvaluationAnchor,
    EvidenceScope,
    SearchCreate,
    utc_now_iso,
)
from citation_tool.store import (
    ConversationBusy,
    ConversationCapacity,
    ConversationConflict,
    ConversationNotFound,
    ConversationQueryNotFound,
    Store,
)
from test_agent import ScriptedChatModel, StubReasonGenerator, tool_call, wait_agent
from test_integration import headers, search_body, wait_final

FIXTURE_DOI = "10.5555/demo-fast-001"


def turn_server(app) -> FastAPI:
    """Test-only adapter for the exact runner.start conversation interface.

    The production HTTP surface is wired by the parent; this keeps these tests
    independent of that integration without editing api.py.
    """

    wrapper = FastAPI()

    @wrapper.post("/api/v1/agent/turns", status_code=202)
    async def start_turn(body: AgentRunCreate, request: Request):
        return app.state.agent_runner.start(
            "local",
            body.search_create(),
            request.headers.get("Idempotency-Key"),
            conversation_id=body.conversation_id,
            message=body.message,
            active_query_id=body.active_query_id,
            expected_revision=body.expected_revision,
        )

    @wrapper.get("/api/v1/agent/runs/{run_id}")
    async def get_run(run_id: str):
        run = app.state.store.get_agent_run("local", run_id)
        if run is None:
            raise HTTPException(status_code=404)
        return run

    @wrapper.get("/api/v1/searches/{query_id}")
    async def get_search(query_id: str):
        state = app.state.search_service.state("local", query_id)
        if state is None:
            raise HTTPException(status_code=404)
        return state

    return wrapper


def make_request(keywords: list[str] | None = None) -> SearchCreate:
    return SearchCreate.model_validate(
        {
            "input_type": "keywords",
            "keywords": keywords or ["LFY2", "soybean"],
            "topic_context": ["LFY2", "soybean", "floral development"],
        }
    )


def make_run(
    tenant: str = "local",
    *,
    conversation_id: str | None = None,
    message: str = "LFY2 soybean floral development",
    active_query_id: str | None = None,
    anchor: EvaluationAnchor | None = None,
    run_id: str | None = None,
) -> AgentRun:
    now = utc_now_iso()
    return AgentRun(
        run_id=run_id or f"agt_{uuid.uuid4().hex}",
        tenant_id=tenant,
        request=make_request(),
        status="pending",
        provider="test-provider",
        model="scripted-test",
        conversation_id=conversation_id,
        user_message=message,
        active_query_id=active_query_id,
        evaluation_anchor=anchor,
        created_at=now,
        updated_at=now,
    )


def finish(store: Store, run: AgentRun, status: str, summary: str | None = None):
    run = run.model_copy(
        update={
            "status": status,
            "summary": summary,
            "error": (
                {"code": "AGENT_CANCELLED", "message": "用户取消。"}
                if status == "cancelled"
                else None
            ),
            "updated_at": utc_now_iso(),
        }
    )
    store.save_agent_run(run)
    return store.finish_conversation_run(run)


def test_first_turn_creates_conversation_and_replays_idempotently(tmp_path, live_server) -> None:
    model = ScriptedChatModel(
        responses=[
            tool_call("first-search", ["LFY2"], ["fixture-fast"]),
            AIMessage(content="已找到一条候选；详情在独立榜单中。"),
        ]
    )
    app = create_app(
        Settings.for_tests(tmp_path),
        reason_generator=StubReasonGenerator(),
        agent_model=model,
    )
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1}),
            headers=headers(key="conversation-first"),
        )
        assert created.status_code == 202, created.text
        body = created.json()
        assert body["conversation_id"].startswith("cnv_")
        assert body["turn_index"] == 1
        assert body["conversation_revision"] == 0
        run = wait_agent(client, base, body["run_id"])
        assert run["status"] == "completed", run["error"]
        assert run["conversation_id"] == body["conversation_id"]
        assert run["conversation_revision"] == 1
        wait_final(client, base, run["latest_query_id"])

        conversation = app.state.store.get_conversation("local", body["conversation_id"])
        assert conversation is not None
        assert conversation.revision == 1
        assert conversation.active_run_id is None
        assert [message.role for message in conversation.messages] == ["user", "assistant"]
        assert conversation.messages[1].text == run["summary"]
        assert len(conversation.turns) == 1
        assert conversation.turns[0].status == "completed"
        assert conversation.turns[0].query_ids == [run["latest_query_id"]]
        assert [artifact.query_id for artifact in conversation.artifacts] == [
            run["latest_query_id"]
        ]
        assert app.state.store.get_conversation("other", body["conversation_id"]) is None

        replay = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1}),
            headers=headers(key="conversation-first"),
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["run_id"] == run["run_id"]
        assert replay.json()["conversation_id"] == body["conversation_id"]
        replayed_conversation = app.state.store.get_conversation("local", body["conversation_id"])
        assert len(replayed_conversation.messages) == 2
        assert len(replayed_conversation.artifacts) == 1


def test_follow_up_uses_prior_artifacts_without_pretending_they_are_new_tools(
    tmp_path, live_server
) -> None:
    model = ScriptedChatModel(
        responses=[
            tool_call("broad", ["flowering"], ["fixture-fast", "fixture-slow"]),
            tool_call("narrow", ["LFY2"], ["fixture-fast"]),
            AIMessage(content="第一轮给了两个独立结果集。"),
            AIMessage(content="第一篇范围较宽；本轮只保留田间条件，没有执行新的检索。"),
        ]
    )
    app = create_app(
        Settings.for_tests(tmp_path),
        reason_generator=StubReasonGenerator(),
        agent_model=model,
    )
    base = live_server(turn_server(app))
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/turns",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=headers(key="conversation-turn-1"),
        )
        first_run = wait_agent(client, base, created.json()["run_id"])
        assert first_run["status"] == "completed", first_run["error"]
        for query in first_run["queries"]:
            wait_final(client, base, query["created"]["query_id"])
        conversation = app.state.store.get_conversation("local", first_run["conversation_id"])
        first_query, second_query = [item.query_id for item in conversation.artifacts]
        assert conversation.latest_turn_index == 1
        assert conversation.revision == 1

        before = len(model.received_messages)
        follow_up = client.post(
            f"{base}/api/v1/agent/turns",
            json={
                **search_body({"fixture-fast": 1}),
                "conversation_id": first_run["conversation_id"],
                "message": "第一篇不对，只找田间",
                "active_query_id": first_query,
                "claim_scope": {
                    "species": ["Glycine max"],
                    "system": ["field"],
                },
            },
            headers=headers(key="conversation-turn-2"),
        )
        assert follow_up.status_code == 202, follow_up.text
        second = follow_up.json()
        assert second["turn_index"] == 2
        assert second["conversation_revision"] == 1
        assert second["active_query_id"] == first_query
        assert second["prior_query_ids"] == [first_query, second_query]
        assert second["queries"] == []
        anchor = second["evaluation_anchor"]
        assert "第一篇不对，只找田间" in anchor["claim_text"]
        assert "LFY2 soybean floral development" in anchor["claim_text"]
        assert anchor["scope"]["system"] == ["field"]
        assert anchor["scope"]["species"] == ["Glycine max"]
        assert anchor["scope"]["method"] == ["expression profiling", "association analysis"]
        second_run = wait_agent(client, base, second["run_id"])
        assert second_run["status"] == "completed", second_run["error"]
        assert second_run["conversation_revision"] == 2
        assert second_run["queries"] == []
        assert second_run["prior_query_ids"] == [first_query, second_query]

        second_calls = model.received_messages[before:]
        assert second_calls
        first_call = second_calls[0]
        assert isinstance(first_call[-1], HumanMessage)
        assert not any(isinstance(message, ToolMessage) for message in first_call)
        assert not any(getattr(message, "tool_calls", None) for message in first_call)
        prompt = "\n".join(str(message.content) for message in first_call)
        assert first_query in prompt and second_query in prompt
        assert "不是本轮工具回执" in prompt
        assert '"model_visible"' in prompt

        updated = app.state.store.get_conversation("local", first_run["conversation_id"])
        assert updated.revision == 2
        assert updated.latest_turn_index == 2
        assert updated.active_run_id is None
        assert [message.role for message in updated.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert len(updated.artifacts) == 2
        assert updated.turns[1].query_ids == []
        assert updated.turns[1].status == "completed"
        assert updated.last_active_query_id == first_query


def test_conversation_cas_busy_idempotency_cancel_and_tenant_isolation(tmp_path) -> None:
    store = Store(tmp_path / "conversations-cas.db")
    first = make_run()
    first, replayed = store.create_conversation_agent_run(
        first, "stable-key", create_conversation=True
    )
    assert replayed is False
    conversation_id = first.conversation_id
    assert conversation_id is not None
    assert first.turn_index == 1
    assert first.conversation_revision == 0

    busy = make_run(conversation_id=conversation_id)
    with pytest.raises(ConversationBusy):
        store.create_conversation_agent_run(busy, "busy-key")
    other_tenant = make_run(tenant="other", conversation_id=conversation_id)
    with pytest.raises(ConversationNotFound):
        store.create_conversation_agent_run(other_tenant, "other-key")

    conversation = finish(store, first, "completed", "第一轮答复")
    assert conversation.revision == 1
    assert conversation.active_run_id is None
    assert [message.role for message in conversation.messages] == ["user", "assistant"]

    stale = make_run(conversation_id=conversation_id, message="第二轮")
    with pytest.raises(ConversationConflict):
        store.create_conversation_agent_run(stale, "stale-key", expected_revision=0)
    with pytest.raises(ConversationQueryNotFound):
        store.create_conversation_agent_run(
            make_run(conversation_id=conversation_id, active_query_id="q_not_here"),
            "bad-query-key",
            expected_revision=1,
        )
    second, replayed = store.create_conversation_agent_run(stale, "second-key", expected_revision=1)
    assert replayed is False
    assert second.turn_index == 2

    cancelled = finish(store, second, "cancelled")
    assert cancelled.revision == 2
    assert cancelled.active_run_id is None
    assert cancelled.turns[-1].status == "cancelled"
    assert cancelled.turns[-1].assistant_message_seq is None
    assert cancelled.messages[-1].role == "user"
    assert len(cancelled.messages) == 3

    replay = make_run(
        conversation_id=conversation_id,
        message=first.user_message,
    )
    replayed_run, replayed = store.create_conversation_agent_run(replay, "stable-key")
    assert replayed is True
    assert replayed_run.run_id == first.run_id
    assert replayed_run.status == "completed"
    after_replay = store.get_conversation("local", conversation_id)
    assert len(after_replay.messages) == 3
    assert after_replay.revision == 2

    third = make_run(conversation_id=conversation_id, message="继续")
    third, replayed = store.create_conversation_agent_run(third, "third-key")
    assert replayed is False
    assert third.turn_index == 3
    assert store.get_conversation("other", conversation_id) is None


def test_stale_active_run_is_reconciled_as_failed_and_not_answered(tmp_path) -> None:
    store = Store(tmp_path / "conversations-stale.db")
    first, _ = store.create_conversation_agent_run(
        make_run(), "stale-first", create_conversation=True
    )
    first = first.model_copy(update={"updated_at": "2000-01-01T00:00:00.000Z"})
    store.save_agent_run(first)
    second, replayed = store.create_conversation_agent_run(
        make_run(conversation_id=first.conversation_id, message="继续"),
        "stale-second",
    )
    assert replayed is False
    conversation = store.get_conversation("local", first.conversation_id)
    assert conversation.turns[0].status == "failed"
    assert conversation.turns[0].error["code"] == "AGENT_INTERRUPTED"
    assert conversation.turns[0].assistant_message_seq is None
    assert conversation.turns[1].status == "pending"
    assert conversation.turns[1].turn_index == 2
    assert second.turn_index == 2
    assert store.get_agent_run("local", first.run_id).status == "failed"


def test_conversation_capacity_is_explicit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_module, "MAX_CONVERSATION_TURNS", 1)
    store = Store(tmp_path / "conversations-capacity.db")
    first, _ = store.create_conversation_agent_run(
        make_run(), "capacity-1", create_conversation=True
    )
    finish(store, first, "completed", "完成")
    with pytest.raises(ConversationCapacity):
        store.create_conversation_agent_run(
            make_run(conversation_id=first.conversation_id), "capacity-2"
        )


def test_failed_turn_is_visible_and_can_be_continued(tmp_path, live_server) -> None:
    class FailingChatModel(ScriptedChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("scripted model failure")

    app = create_app(Settings.for_tests(tmp_path), agent_model=FailingChatModel())
    base = live_server(turn_server(app))
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/turns",
            json=search_body({"fixture-fast": 1}),
            headers=headers(key="failed-turn"),
        )
        first = wait_agent(client, base, created.json()["run_id"])
        assert first["status"] == "failed"
        conversation = app.state.store.get_conversation("local", first["conversation_id"])
        assert conversation.revision == 1
        assert conversation.active_run_id is None
        assert conversation.turns[0].status == "failed"
        assert conversation.turns[0].assistant_message_seq is None
        assert conversation.turns[0].error["code"] == "AGENT_MODEL_ERROR"
        assert [message.role for message in conversation.messages] == ["user"]

        model = ScriptedChatModel(responses=[AIMessage(content="可以继续；上一轮没有成功答复。")])
        app.state.agent_runner.model = model
        follow_up = client.post(
            f"{base}/api/v1/agent/turns",
            json={
                **search_body({"fixture-fast": 1}),
                "conversation_id": first["conversation_id"],
                "message": "换一种说法继续找",
            },
            headers=headers(key="continued-turn"),
        )
        second = wait_agent(client, base, follow_up.json()["run_id"])
        assert second["status"] == "completed", second["error"]
        prompt = "\n".join(str(message.content) for message in model.received_messages[0])
        assert '"status": "failed"' in prompt
        assert '"error_code": "AGENT_MODEL_ERROR"' in prompt
        assert "换一种说法继续找" in prompt
        updated = app.state.store.get_conversation("local", first["conversation_id"])
        assert updated.revision == 2
        assert [message.role for message in updated.messages] == [
            "user",
            "user",
            "assistant",
        ]
        assert updated.turns[1].status == "completed"


async def test_runner_cancel_finalizes_the_turn_and_allows_continuation(tmp_path) -> None:
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="取消前不应被当作答复。"),
            AIMessage(content="取消后继续成功。"),
        ]
    )
    app = create_app(Settings.for_tests(tmp_path), agent_model=model)
    runner = app.state.agent_runner
    first = runner.start("local", make_request(), None)
    cancelled = await runner.cancel(first)
    assert cancelled.status == "cancelled"
    conversation = app.state.store.get_conversation("local", first.conversation_id)
    assert conversation.revision == 1
    assert conversation.active_run_id is None
    assert conversation.turns[0].status == "cancelled"
    assert conversation.turns[0].assistant_message_seq is None
    assert [message.role for message in conversation.messages] == ["user"]

    second = runner.start(
        "local",
        make_request(),
        None,
        conversation_id=first.conversation_id,
        message="取消后继续",
    )
    task = runner._tasks[second.run_id]
    await task
    conversation = app.state.store.get_conversation("local", first.conversation_id)
    assert conversation.revision == 2
    assert conversation.active_run_id is None
    assert conversation.turns[1].status == "completed"
    assert [message.role for message in conversation.messages] == [
        "user",
        "user",
        "assistant",
    ]


def test_prior_artifact_sources_ground_doi_checks_without_a_new_query(
    tmp_path, live_server
) -> None:
    model = ScriptedChatModel(
        responses=[
            tool_call("prior-source", ["LFY2"], ["fixture-fast"]),
            AIMessage(content="已返回一条题录。"),
        ]
    )
    app = create_app(
        Settings.for_tests(tmp_path),
        reason_generator=StubReasonGenerator(),
        agent_model=model,
    )
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1}),
            headers=headers(key="prior-source"),
        )
        first = wait_agent(client, base, created.json()["run_id"])
        wait_final(client, base, first["latest_query_id"])
        conversation = app.state.store.get_conversation("local", first["conversation_id"])

    run = make_run(
        conversation_id=first["conversation_id"],
        message="第一篇的 DOI 是什么？",
        anchor=conversation.latest_anchor,
        run_id="agt_prior_context",
    )
    run.prior_query_ids = [artifact.query_id for artifact in conversation.artifacts]
    service = app.state.search_service
    assert _unprovided_dois(run, service, f"DOI {FIXTURE_DOI}") == []
    mentioned = _mentioned_source_texts(run, service, f"DOI {FIXTURE_DOI}")
    assert mentioned
    assert any(source["doi"] == FIXTURE_DOI for source in mentioned)
    memory_only = run.model_copy(update={"prior_query_ids": []})
    assert _unprovided_dois(memory_only, service, f"DOI {FIXTURE_DOI}") == [FIXTURE_DOI]
    assert _unprovided_dois(run, service, "DOI 10.9999/not-returned") == ["10.9999/not-returned"]


def test_context_is_bounded_and_marks_what_the_model_cannot_see(
    tmp_path, live_server, monkeypatch
) -> None:
    model = ScriptedChatModel(
        responses=[
            tool_call("context-source", ["LFY2"], ["fixture-fast"]),
            AIMessage(content="已返回一条题录。"),
        ]
    )
    app = create_app(
        Settings.for_tests(tmp_path),
        reason_generator=StubReasonGenerator(),
        agent_model=model,
    )
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1}),
            headers=headers(key="context-source"),
        )
        first = wait_agent(client, base, created.json()["run_id"])
        wait_final(client, base, first["latest_query_id"])
        conversation = app.state.store.get_conversation("local", first["conversation_id"])

    monkeypatch.setattr(conversation_context, "HISTORY_MESSAGE_LIMIT", 1)
    monkeypatch.setattr(conversation_context, "HISTORY_CHAR_LIMIT", 20)
    monkeypatch.setattr(conversation_context, "ARTIFACT_LIMIT", 1)
    run = make_run(
        conversation_id=first["conversation_id"],
        message="继续解释",
        anchor=conversation.latest_anchor,
        run_id="agt_context_bound",
    )
    run.turn_index = 2
    run.prior_query_ids = [artifact.query_id for artifact in conversation.artifacts]
    run.active_query_id = conversation.last_active_query_id
    context = build_conversation_context(app.state.store, app.state.search_service, run)
    assert len(context["history"]) <= 1
    assert context["model_visible"]["history_messages_omitted"] >= 1
    assert context["model_visible"]["truncated"] is True
    assert "省略" in context["model_visible"]["notice"]
    assert "不是系统指令" in context["model_visible"]["notice"]
    assert context["prior_query_ids"] == [artifact.query_id for artifact in conversation.artifacts]
    assert context["artifacts"]
    assert context["artifacts"][0]["query_id"] == run.active_query_id
    assert context["artifacts"][0]["results"]
    assert "tool_calls" not in json.dumps(context["artifacts"])
    assert all(item["role"] in {"user", "assistant"} for item in context["history"])


@pytest.mark.parametrize("with_reason", [True, False])
def test_follow_up_candidate_context_contains_bounded_real_source_not_only_old_answer(with_reason):
    abstract = "Public source evidence about gene expression. " * 30
    candidate = SimpleNamespace(
        work_id="work:public",
        result_id="res_public",
        title="A public research article",
        doi="10.5555/public",
        year=2026,
        relationship="insufficient",
        scope_match="unknown",
        candidate_score=20,
        llm_rank=1,
        base_rank=1,
        reason=(
            SimpleNamespace(status="generated", text="The source reports expression changes.")
            if with_reason
            else None
        ),
        evidence_excerpt=abstract,
        rejection_reasons=[],
    )
    coverage = SimpleNamespace(
        completed_library_ids=["public"],
        failed_library_ids=[],
        timed_out_library_ids=[],
        pending_library_ids=[],
        running_library_ids=[],
        total_library_count=1,
    )
    state = SimpleNamespace(
        session=SimpleNamespace(search_state="completed"),
        groups=[],
        results=[],
        candidate_diagnostics=[candidate],
        candidate_rankings=[],
        coverage=coverage,
        is_live=False,
        scope_source="unspecified",
    )
    service = SimpleNamespace(state=lambda tenant, query: state)
    artifact = SimpleNamespace(
        seq=1, query_id="q_public", run_id="agt_public", turn_index=1, label="research"
    )
    summary = conversation_context._artifact_summary(service, "local", artifact, make_run())
    item = summary["candidate_diagnostics"][0]
    assert item["reason"] == (candidate.reason.text if with_reason else None)
    assert item["reason_status"] == ("generated" if with_reason else None)
    assert item["abstract_excerpt"] == abstract[: conversation_context.EVIDENCE_CHAR_LIMIT] + "…"
    assert item["abstract_truncated"] is True
    assert item["citable"] is False


def test_effective_anchor_keeps_existing_conditions_and_marks_the_revision() -> None:
    previous = EvaluationAnchor(
        claim_text="LFY2 soybean floral development",
        scope=EvidenceScope(
            species=["Glycine max"],
            system=["greenhouse", "field"],
            method=["expression profiling"],
        ),
    )
    request = SearchCreate.model_validate(
        {
            "input_type": "keywords",
            "keywords": ["LFY2"],
            "topic_context": ["field trial"],
            "claim_scope": {
                "species": ["Glycine max"],
                "system": ["field"],
            },
        }
    )
    anchor = _effective_evaluation_anchor(previous, request, "第一篇不对，只找田间")
    assert anchor is not None
    assert "[会话既有研究需求]" in anchor.claim_text
    assert "[本轮修订]" in anchor.claim_text
    assert "LFY2 soybean floral development" in anchor.claim_text
    assert "第一篇不对，只找田间" in anchor.claim_text
    assert anchor.claim_text != "第一篇不对，只找田间"
    assert anchor.scope.system == ["field"]
    assert anchor.scope.species == ["Glycine max"]
    assert anchor.scope.method == ["expression profiling"]
    third = _effective_evaluation_anchor(anchor, request, "再只看田间")
    assert third.claim_text.count("[会话既有研究需求]") == 1
    assert "第一篇不对，只找田间" in third.claim_text
    assert "再只看田间" in third.claim_text
    first = _effective_evaluation_anchor(None, request, "第一篇需求")
    assert first.claim_text == "LFY2\nfield trial"
    assert first.scope.system == ["field"]


def test_reaffirmed_original_request_updates_the_latest_revision() -> None:
    original = "只找独立复现实验。"
    intervening = "改看首发研究，排除独立复现实验。"

    def revise(previous, text):
        request = SearchCreate(input_type="abstract", abstract=text)
        return _effective_evaluation_anchor(previous, request, text)

    first = revise(None, original)
    second = revise(first, intervening)
    third = revise(second, original)
    assert third.claim_text.rsplit("[本轮修订]", 1)[-1].strip() == original
    assert intervening in third.claim_text
    assert revise(third, original) == third


def test_explicit_user_revision_is_last_after_structured_request() -> None:
    previous = EvaluationAnchor(claim_text="研究范围：首发研究")
    request = SearchCreate(input_type="abstract", abstract="首发研究和独立复现实验")
    updated = _effective_evaluation_anchor(previous, request, "不要首发研究，只看独立复现实验")
    assert updated.claim_text.endswith("不要首发研究，只看独立复现实验")


def test_agent_run_create_keeps_conversation_fields_out_of_the_search_request() -> None:
    body = AgentRunCreate.model_validate(
        {
            **search_body({"fixture-fast": 1}),
            "conversation_id": "cnv_example",
            "message": "只找田间",
            "active_query_id": "q_example",
            "expected_revision": 2,
        }
    )
    search = body.search_create()
    assert search.keywords == ["LFY2", "soybean", "floral development"]
    dumped = search.model_dump()
    assert "conversation_id" not in dumped
    assert "message" not in dumped
    assert "active_query_id" not in dumped
    assert "expected_revision" not in dumped
    with pytest.raises(ValueError):
        AgentRunCreate.model_validate({**search_body({"fixture-fast": 1}), "expected_revision": -1})
