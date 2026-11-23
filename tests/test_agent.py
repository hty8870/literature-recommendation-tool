from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import replace

import httpx
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from citation_tool.agent import final_answer_text
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.entities import (
    ClaimContext,
    EvaluationAnchor,
    EvidenceScope,
    RecommendationReason,
    utc_now_iso,
)
from citation_tool.evidence import Candidate
from citation_tool.reasons import (
    DeepSeekModelGateway,
    DeepSeekReasonGenerator,
    ModelConfig,
)
from test_integration import headers, search_body, wait_final


class StubReasonGenerator:
    provider = "test-double"
    supports_alignment = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate(self, claim: ClaimContext, candidate: Candidate) -> RecommendationReason:
        self.calls.append(candidate.quote)
        return RecommendationReason(
            status="generated",
            text=f"测试模型针对“{candidate.quote}”生成的独立理由，仍需核查原文。",
            provider=self.provider,
            model="stub-reason-v1",
            prompt_version="test-reason-v1",
            input_hash="test-input-hash",
            generated_at=utc_now_iso(),
            alignment_score=3,
        )


class ScriptedChatModel(BaseChatModel):
    responses: list[AIMessage] = Field(default_factory=list)
    received_messages: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-test"

    def bind_tools(self, tools: Sequence, **kwargs) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        self.received_messages.append(messages)
        if not self.responses:
            raise AssertionError("Agent made more model calls than the test script allowed")
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def test_injected_reason_generator_reports_model_available(tmp_path) -> None:
    config = ModelConfig(api_key="unit-test-placeholder")  # noqa: S106
    gateway = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=5000)
    reasons = DeepSeekReasonGenerator(
        gateway, max_inflight=1, timeout_ms=5000, model=ScriptedChatModel()
    )
    app = create_app(
        Settings.for_tests(tmp_path), reason_generator=reasons, agent_model=ScriptedChatModel()
    )
    assert app.state.search_service.reason_provider == "deepseek"
    assert app.state.search_service.adapter_mode.endswith("_live_llm")


def wait_agent(client: httpx.Client, base: str, run_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"{base}/api/v1/agent/runs/{run_id}", headers=headers())
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.01)
    raise AssertionError("agent did not finish")


def test_agent_chooses_query_and_tool_returns_ranked_real_reason_calls(
    tmp_path, live_server
) -> None:
    reasons = StubReasonGenerator()
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-search-1",
                        "name": "search_literature",
                        "args": {
                            "input_type": "keywords",
                            "keywords": ["LFY2", "AP1"],
                            "topic_context": ["LFY2", "AP1", "soybean floral development"],
                            "library_ids": ["fixture-fast", "fixture-slow"],
                            "claim_scope": {
                                "species": ["Glycine max"],
                                "system": ["greenhouse", "field"],
                                "method": ["expression profiling", "association analysis"],
                            },
                            "options": {
                                "library_delays_ms": {"fixture-fast": 1, "fixture-slow": 2},
                                "soft_deadline_ms": 5000,
                            },
                        },
                    }
                ],
            ),
            AIMessage(content="查询已覆盖两个库，当前结果足够，不需要重查。"),
        ]
    )
    app = create_app(Settings.for_tests(tmp_path), reason_generator=reasons, agent_model=model)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=headers(key="agent-flow"),
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        run = wait_agent(client, base, run_id)
        assert run["status"] == "completed", run["error"]
        assert run["queries"] and run["latest_query_id"]
        assert run["tool_calls"][0]["name"] == "search_literature"
        assert run["tool_calls"][0]["arguments"]["keywords"] == ["LFY2", "AP1"]
        assert "不需要重查" in run["summary"]
        query_id = run["latest_query_id"]
        state = wait_final(client, base, query_id)
        assert len(reasons.calls) == 4, state["results"]
        assert len(state["results"]) == 4
        for result in state["results"]:
            reason = result["reason"]
            assert reason["status"] == "generated"
            assert reason["provider"] == "test-double"
            assert "测试模型针对" in reason["text"]
            assert reason["text"] != "支持当前目标论述；提供背景，范围一致。"
        assert state["groups"][0]["entries"][0]["rank"] == 1

        replay = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=headers(key="agent-flow"),
        )
        assert replay.json()["run_id"] == run_id
        conflict = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 2, "fixture-slow": 1}),
            headers=headers(key="agent-flow"),
        )
        assert conflict.status_code == 409
        assert (
            client.get(
                f"{base}/api/v1/agent/runs/{run_id}", headers=headers(tenant="other")
            ).status_code
            == 404
        )


def test_final_answer_text_uses_terminal_blocks_and_keeps_unphased_text() -> None:
    message = AIMessage(
        content=[
            {"type": "text", "phase": "commentary", "text": "Checking the sources."},
            {"type": "text", "phase": "final_answer", "text": '{"papers":[]}'},
        ],
        response_metadata={"object": "response", "status": "completed"},
    )
    assert final_answer_text(message) == '{"papers":[]}'
    assert final_answer_text(AIMessage(content="A complete answer.")) == "A complete answer."
    assert final_answer_text(message.model_copy(update={"content": message.content[:1]})) is None
    incomplete = {"object": "response", "status": "incomplete"}
    assert final_answer_text(message.model_copy(update={"response_metadata": incomplete})) is None


@pytest.mark.parametrize("finish", ["length", "max_tokens", "model_context_window_exceeded"])
def test_final_answer_text_refuses_truncated_output_even_with_text(finish) -> None:
    message = AIMessage(content='{"papers":[]}', response_metadata={"finish_reason": finish})
    assert final_answer_text(message) is None


@pytest.mark.parametrize("has_final,finish", [(False, None), (True, None), (True, "max_tokens")])
def test_only_final_phase_is_committed_to_conversation(tmp_path, live_server, has_final, finish):
    content = [{"type": "text", "phase": "commentary", "text": "正在检查文献。"}]
    if has_final:
        content.append({"type": "text", "phase": "final_answer", "text": "请明确研究范围。"})
    message = AIMessage(
        content=content, response_metadata={"finish_reason": finish} if finish else {}
    )
    base = live_server(
        create_app(
            Settings.for_tests(tmp_path),
            agent_model=ScriptedChatModel(responses=[message]),
        )
    )
    with httpx.Client(timeout=10) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1}),
            headers=headers(),
        ).json()
        run = wait_agent(client, base, created["run_id"])
        conversation = client.get(
            f"{base}/api/v1/conversations/{run['conversation_id']}", headers=headers()
        ).json()
    delivered = has_final and finish is None
    assert run["status"] == ("completed" if delivered else "failed")
    assert run["summary"] == ("请明确研究范围。" if delivered else None)
    messages = [row["text"] for row in conversation["messages"] if row["role"] == "assistant"]
    assert messages == ([run["summary"]] if delivered else [])
    if not delivered:
        assert run["error"]["code"] == "AGENT_NO_FINAL_ANSWER"


def test_agent_can_decide_not_to_call_the_tool(tmp_path, live_server) -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="先明确研究物种和论述范围，再检索。")])
    base = live_server(create_app(Settings.for_tests(tmp_path), agent_model=model))
    with httpx.Client(timeout=10) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=headers(),
        )
        run = wait_agent(client, base, created.json()["run_id"])
        assert run["status"] == "completed"
        assert run["queries"] == []
        assert run["tool_calls"] == []
        assert "先明确" in run["summary"]


def test_missing_model_is_explicit_and_reasons_are_never_templated(tmp_path, live_server) -> None:
    base = live_server(create_app(Settings.for_tests(tmp_path)))
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=headers(),
        )
        assert response.status_code == 503
        assert response.json()["code"] == "MODEL_NOT_CONFIGURED"
        created = client.post(
            f"{base}/api/v1/searches",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=headers(),
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert len(state["results"]) == 4
        for result in state["results"]:
            assert result["reason"]["status"] == "unavailable"
            assert result["reason"]["failure_code"] == "MODEL_NOT_CONFIGURED"
            assert result["reason"]["text"] is None


async def test_deepseek_reason_parser_and_invalid_output_are_honest() -> None:
    from test_units import make_candidate, make_claim

    config = ModelConfig(api_key="unit-test-not-a-secret")
    assert "unit-test-not-a-secret" not in repr(config)
    gateway = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=1000)
    valid = ScriptedChatModel(
        responses=[
            AIMessage(content='{"reason":"该片段涉及目标基因与开花组织，可用于核查论述范围。"}')
        ]
    )
    generator = DeepSeekReasonGenerator(gateway, max_inflight=1, timeout_ms=1000, model=valid)
    generated = await generator.generate(make_claim(), make_candidate())
    assert generated.status == "generated"
    assert generated.model == "deepseek-flash"
    assert "目标基因" in (generated.text or "")

    invalid = ScriptedChatModel(
        responses=[AIMessage(content="not JSON"), AIMessage(content="still not JSON")]
    )
    generator = DeepSeekReasonGenerator(gateway, max_inflight=1, timeout_ms=1000, model=invalid)
    failed = await generator.generate(make_claim(), make_candidate())
    assert failed.status == "failed"
    assert failed.failure_code == "MODEL_INVALID_OUTPUT"
    assert failed.text is None

    recoverable = ScriptedChatModel(
        responses=[
            AIMessage(content="not JSON"),
            AIMessage(content='{"reason":"该片段涉及目标基因与开花组织，可用于核查论述范围。"}'),
        ]
    )
    generator = DeepSeekReasonGenerator(gateway, max_inflight=1, timeout_ms=1000, model=recoverable)
    recovered = await generator.generate(make_claim(), make_candidate())
    assert recovered.status == "generated"
    assert recoverable.responses == []

    corrected = ScriptedChatModel(
        responses=[
            AIMessage(content='{"reason":"AP1 共表达已经在本研究得到验证。"}'),
            AIMessage(content='{"reason":"LFY2 表达定位与目标组织相关，可进一步核查其范围。"}'),
        ]
    )
    generator = DeepSeekReasonGenerator(gateway, max_inflight=1, timeout_ms=1000, model=corrected)
    generated = await generator.generate(make_claim(), make_candidate())
    assert generated.status == "generated"
    assert "LFY2" in (generated.text or "")
    assert corrected.responses == []

    twice_invalid = ScriptedChatModel(
        responses=[
            AIMessage(content='{"reason":"AP1 共表达已经在本研究得到验证。"}'),
            AIMessage(content='{"reason":"AP2 调控已经在本研究得到验证。"}'),
        ]
    )
    generator = DeepSeekReasonGenerator(
        gateway, max_inflight=1, timeout_ms=1000, model=twice_invalid
    )
    rejected = await generator.generate(make_claim(), make_candidate())
    assert rejected.status == "failed"
    assert rejected.failure_code == "MODEL_UNGROUNDED_OUTPUT"
    assert rejected.text is None


async def test_reason_requires_source_support_for_double_blind_design() -> None:
    from citation_tool.reasons import reason_entities_grounded
    from test_units import make_candidate, make_claim

    candidate = make_candidate(
        raw_text="A randomized placebo-controlled trial; blinding was not reported.",
        quote="A randomized placebo-controlled trial; blinding was not reported.",
    )
    assert reason_entities_grounded("摘要未报告双盲设计，需核查全文。", candidate)
    assert not reason_entities_grounded("该研究为双盲随机安慰剂对照试验。", candidate)
    assert not reason_entities_grounded("This was a double-blind trial.", candidate)
    explicit = make_candidate(
        raw_text="A randomized, double-blind, placebo-controlled trial.",
        quote="A randomized, double-blind, placebo-controlled trial.",
    )
    assert reason_entities_grounded("该研究为双盲随机安慰剂对照试验。", explicit)
    assert not reason_entities_grounded("该研究为开放标签试验。", candidate)
    assert reason_entities_grounded("摘要未报告开放标签与随机化设计。", candidate)
    explicit_open = make_candidate(
        raw_text="An open-label randomized trial.",
        quote="An open-label randomized trial.",
    )
    assert reason_entities_grounded("该研究为开放标签随机试验。", explicit_open)
    negative_source = make_candidate(
        raw_text="This was not a double-blind trial.",
        quote="This was not a double-blind trial.",
    )
    assert not reason_entities_grounded("This was a double-blind trial.", negative_source)
    positive_with_other_negation = make_candidate(
        raw_text="No participants were excluded from this double-blind trial.",
        quote="No participants were excluded from this double-blind trial.",
    )
    assert reason_entities_grounded("This was a double-blind trial.", positive_with_other_negation)

    model = ScriptedChatModel(
        responses=[
            AIMessage(content='{"reason":"该研究为双盲随机安慰剂对照试验。"}'),
            AIMessage(content='{"reason":"该研究为随机安慰剂对照试验，摘要未报告盲法。"}'),
        ]
    )
    gateway = DeepSeekModelGateway(
        ModelConfig(api_key="unit-test-not-a-secret"), max_inflight=1, timeout_ms=1000
    )
    generator = DeepSeekReasonGenerator(gateway, max_inflight=1, timeout_ms=1000, model=model)
    corrected = await generator.generate(make_claim(), candidate)
    assert corrected.status == "generated"
    assert "双盲" not in (corrected.text or "")
    assert model.responses == []


async def test_reason_uses_original_evaluation_anchor_and_binds_publication_year() -> None:
    from test_units import make_candidate, make_claim

    anchor = EvaluationAnchor(
        claim_text="Find the 2017 original trial, not a later follow-up.",
        scope=EvidenceScope(species=["Homo sapiens"]),
    )
    claim = make_claim(text="a rewritten recall query").model_copy(
        update={"evaluation_anchor": anchor}
    )
    candidate = replace(
        make_candidate(),
        source=make_candidate().source.model_copy(
            update={"year": 2017, "authors": "Original Trial Authors"}
        ),
    )
    model = ScriptedChatModel(
        responses=[
            AIMessage(content='{"reason":"该候选与原始研究需求相关，可核查其直接证据与年份范围。"}')
            for _ in range(3)
        ]
    )
    gateway = DeepSeekModelGateway(
        ModelConfig(api_key="unit-test-not-a-secret"), max_inflight=1, timeout_ms=1000
    )
    generator = DeepSeekReasonGenerator(gateway, max_inflight=1, timeout_ms=1000, model=model)
    original = await generator.generate(claim, candidate)
    rewritten = await generator.generate(
        claim.model_copy(update={"claim_text": "another recall query"}), candidate
    )
    later = await generator.generate(
        claim,
        replace(candidate, source=candidate.source.model_copy(update={"year": 2021})),
    )
    assert original.input_hash == rewritten.input_hash
    assert later.input_hash != original.input_hash
    payloads = [json.loads(messages[1].content) for messages in model.received_messages]
    assert [payload["year"] for payload in payloads] == [2017, 2017, 2021]
    assert all(payload["authors"] == "Original Trial Authors" for payload in payloads)
    assert all(payload["claim"] == anchor.claim_text for payload in payloads)
    assert all(payload["scope"]["species"] == ["Homo sapiens"] for payload in payloads)


def test_final_design_claims_are_bound_to_their_own_sources() -> None:
    from citation_tool.reasons import unlinked_design_violations

    sources = [
        {"doi": "a", "source_text": "An open-label study."},
        {"doi": "b", "source_text": "A small dose-escalation study."},
    ]
    assert unlinked_design_violations(
        "研究A（DOI a）为开放标签。两项研究均为开放标签。DOI b", sources
    ) == ["open_label"]
    assert (
        unlinked_design_violations(
            "DOI a：该研究为开放标签试验；DOI b：该研究未报告开放标签。", sources
        )
        == []
    )
    assert unlinked_design_violations("两项研究均为开放标签。DOI a；DOI b", sources) == [
        "open_label"
    ]
    for honest_limit in (
        "摘要未报告随机化，也未报告盲法（包括双盲）；因此不能被描述为随机研究或双盲研究。",
        "两篇摘要均未报告随机化、盲法或对照设计。",
        "两篇研究均不是开放标签；双盲设计未报告。",
        "SpaCET 的真实数据以 matched double-blind histopathology annotations 作为真值；"
        "这不是该研究采用双盲设计的证据。",
        "上一版回答中的“随机化/双盲”表述属错误，现予删除。",
        "上一版提到“随机化/盲法/对照设计细节需核查全文”，这一表述属于来源未直接支持的属性。",
        "不应以任何形式暗示其存在或需要“核查”其随机化。",
    ):
        assert unlinked_design_violations(honest_limit, sources) == []


def test_model_configuration_rejects_plaintext_remote_endpoints(tmp_path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ModelConfig(api_key="dummy", base_url="http://models.example.com").validate()
    app = create_app(Settings.for_tests(tmp_path))
    assert app.state.agent_runner.available is False


def tool_call(call_id: str, keywords: list[str], library_ids: list[str]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "search_literature",
                "args": {
                    "input_type": "keywords",
                    "keywords": keywords,
                    "topic_context": keywords,
                    "library_ids": library_ids,
                    "claim_scope": {
                        "species": ["Glycine max"],
                        "system": ["greenhouse", "field"],
                        "method": ["expression profiling", "association analysis"],
                    },
                    "options": {
                        "library_delays_ms": {library_id: 1 for library_id in library_ids},
                        "soft_deadline_ms": 5000,
                    },
                },
            }
        ],
    )


def test_agent_can_narrow_a_query_and_keep_both_result_sets(tmp_path, live_server) -> None:
    model = ScriptedChatModel(
        responses=[
            tool_call("call-broad", ["flowering"], ["fixture-fast", "fixture-slow"]),
            tool_call("call-narrow", ["LFY2", "AP1"], ["fixture-fast"]),
            AIMessage(content="第一次范围过宽，第二次已缩小到目标基因，足够回答。"),
        ]
    )
    base = live_server(
        create_app(
            Settings.for_tests(tmp_path),
            reason_generator=StubReasonGenerator(),
            agent_model=model,
        )
    )
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{base}/api/v1/agent/runs",
            json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
            headers=headers(),
        )
        run = wait_agent(client, base, response.json()["run_id"])
        assert run["status"] == "completed", run["error"]
        assert len(run["queries"]) == 2
        for query in run["queries"]:
            claim = query["created"]["claims"][0]
            assert claim["evaluation_anchor"]["claim_text"] == "LFY2 soybean floral development"
            assert claim["evaluation_anchor"]["scope"]["species"] == ["Glycine max"]
            assert claim["claim_text"] != claim["evaluation_anchor"]["claim_text"]
        first_id, second_id = [query["created"]["query_id"] for query in run["queries"]]
        assert first_id != second_id
        assert run["latest_query_id"] == second_id
        first = wait_final(client, base, first_id)
        second = wait_final(client, base, second_id)
        assert len(first["results"]) == 4
        assert len(second["results"]) == 2
        assert first["results"][0]["claim"]["query_id"] == first_id
        assert second["results"][0]["claim"]["query_id"] == second_id
        assert [entry["rank"] for entry in second["groups"][0]["entries"]] == [1, 2]
        assert all(item["reason"]["status"] == "generated" for item in second["results"])


async def test_search_belongs_to_parent_before_any_worker_is_started(tmp_path, monkeypatch):
    from citation_tool.agent import LiteratureSearchTool
    from test_conversations import make_run

    app = create_app(Settings.for_tests(tmp_path))
    store = app.state.store
    run, _ = store.create_conversation_agent_run(make_run(), None, create_conversation=True)
    assert store.save_agent_run(run.model_copy(update={"status": "running"}))
    coordinator = app.state.coordinator
    start = coordinator.start
    observed = []

    def verify_owner(tenant, query_id):
        persisted = store.get_agent_run(tenant, run.run_id)
        observed.append(query_id in {query.created.query_id for query in persisted.queries})
        return start(tenant, query_id)

    monkeypatch.setattr(coordinator, "start", verify_owner)
    search = LiteratureSearchTool(
        app.state.search_service, run.tenant_id, run.run_id, run.request
    ).as_tool()
    result = await search.ainvoke({"input_type": "keywords", "keywords": ["LFY2"]})
    assert result["search_state"] == "completed"
    assert observed == [True]


async def test_unexpected_worker_error_drains_other_workers_before_finishing(tmp_path, monkeypatch):
    from citation_tool.entities import SearchCreate

    app = create_app(Settings.for_tests(tmp_path))
    coordinator = app.state.coordinator
    ready, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker(tenant, query, claims, library, options):
        if library == "fixture-fast":
            await ready.wait()
            raise RuntimeError("test unexpected worker error")
        ready.set()
        try:
            await release.wait()
        finally:
            drained.set()

    monkeypatch.setattr(coordinator, "_run_library", worker)
    created, _ = app.state.search_service.create(
        "local", SearchCreate(input_type="keywords", keywords=["LFY2"]), None
    )
    try:
        assert await coordinator.wait(created.query_id, timeout_s=3)
        assert drained.is_set()
        state = app.state.search_service.state("local", created.query_id)
        assert state.session.search_state == "failed"
        assert state.coverage.running_library_ids == []
    finally:
        release.set()
        await asyncio.wait_for(drained.wait(), 2)


async def test_cancellation_cannot_be_overwritten_by_a_late_running_save(
    tmp_path, monkeypatch
) -> None:
    app = create_app(Settings.for_tests(tmp_path), agent_model=ScriptedChatModel())
    store = app.state.store
    original = store.save_agent_run
    started, release, saved = threading.Event(), threading.Event(), threading.Event()

    def save(run):
        if run.status != "running":
            return original(run)
        started.set()
        try:
            assert release.wait(timeout=5)
            return original(run)
        finally:
            saved.set()

    monkeypatch.setattr(store, "save_agent_run", save)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        receipt = await client.post(
            "/api/v1/agent/runs", json={"input_type": "keywords", "keywords": ["LFY2"]}
        )
        path = f"/api/v1/agent/runs/{receipt.raise_for_status().json()['run_id']}"
        try:
            assert await asyncio.to_thread(started.wait, 2)
            cancelled = await client.post(path + "/cancel")
            assert cancelled.raise_for_status().json()["status"] == "cancelled"
        finally:
            release.set()
        assert await asyncio.to_thread(saved.wait, 2)
        persisted = (await client.get(path)).raise_for_status().json()
        assert persisted["status"] == "cancelled"
        assert persisted["error"]["code"] == "AGENT_CANCELLED"
        conversation = (
            (await client.get(f"/api/v1/conversations/{persisted['conversation_id']}"))
            .raise_for_status()
            .json()
        )
        assert conversation["active_run_id"] is None and conversation["revision"] == 1


def test_authentication_failures_have_a_specific_public_code() -> None:
    from citation_tool.agent import _agent_error_code
    from citation_tool.reasons import _failure_code

    error = type("OpenAIAuthenticationError", (Exception,), {})()
    assert _agent_error_code(error) == "MODEL_AUTHENTICATION_FAILED"
    assert _failure_code(error) == "MODEL_AUTHENTICATION_FAILED"


@pytest.mark.parametrize(
    ("exception_name", "expected"),
    [("OpenAIConnectionError", "MODEL_CONNECTION_ERROR"), ("OpenAITimeoutError", "MODEL_TIMEOUT")],
)
def test_wrapped_provider_errors_are_distinct_from_agent_timeout(exception_name, expected) -> None:
    from citation_tool.agent import _agent_error_code

    error = RuntimeError()
    error.__cause__ = type(exception_name, (Exception,), {})()
    assert _agent_error_code(error) == expected
    assert _agent_error_code(TimeoutError()) == "AGENT_TIMEOUT"


def test_agent_defaults_prioritize_quality_with_runaway_safety_limits(tmp_path) -> None:
    settings = Settings.for_tests(tmp_path)
    settings.validate()
    assert settings.agent_timeout_ms == 900_000
    assert settings.agent_max_tool_calls == 32
    assert settings.agent_recursion_limit == 96


def test_user_model_http_path_is_scoped_and_never_persists_the_key(
    tmp_path, live_server, monkeypatch
) -> None:
    from citation_tool.agent import DeepSeekReasonGenerator as production_reasons

    model = ScriptedChatModel(
        responses=[
            tool_call("custom-call", ["LFY2", "AP1"], ["fixture-fast", "fixture-slow"]),
            AIMessage(content="自有模型已完成检索。"),
        ]
    )
    observed = []

    class Gateway:
        def __init__(self, config, **_kwargs) -> None:
            observed.append(config)
            self.config = config

        def agent_model(self):
            return model

    monkeypatch.setattr("citation_tool.agent.DeepSeekModelGateway", Gateway)
    monkeypatch.setattr(
        "citation_tool.agent.DeepSeekReasonGenerator",
        lambda gateway, **kwargs: production_reasons(
            gateway,
            **kwargs,
            model=ScriptedChatModel(
                responses=[
                    AIMessage(content='{"reason":"该文涉及目标基因和组织，可进一步核对原文。"}')
                    for _ in range(4)
                ]
            ),
        ),
    )
    app = create_app(Settings.for_tests(tmp_path))
    base = live_server(app)
    selection = {
        "provider": "moonshot",
        "model": "unit-test-model",
        "api_key": "unit-test-user-key",
    }
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/agent/runs",
            json={**search_body({"fixture-fast": 1, "fixture-slow": 1}), "model": selection},
            headers=headers(key="custom-model"),
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        run = wait_agent(client, base, run_id)
        assert run["status"] == "completed", run["error"]
        assert run["provider"] == "moonshot"
        assert run["model"] == "unit-test-model"
        assert "unit-test-user-key" not in created.text
        assert "unit-test-user-key" not in json.dumps(run)
        state = wait_final(client, base, run["latest_query_id"])
        assert len(state["results"]) == 4
        assert all(
            item["reason"]["provider"] == "moonshot" and item["reason"]["status"] == "generated"
            for item in state["results"]
        )
        replay = client.post(
            f"{base}/api/v1/agent/runs",
            json={**search_body({"fixture-fast": 1, "fixture-slow": 1}), "model": selection},
            headers=headers(key="custom-model"),
        )
        assert replay.json()["run_id"] == run_id
        conflict = client.post(
            f"{base}/api/v1/agent/runs",
            json={
                **search_body({"fixture-fast": 1, "fixture-slow": 1}),
                "model": {**selection, "provider": "openai"},
            },
            headers=headers(key="custom-model"),
        )
        assert conflict.status_code == 409
    assert [config.provider for config in observed] == ["moonshot"]
    assert all(config.api_key == "unit-test-user-key" for config in observed)
    assert b"unit-test-user-key" not in Settings.for_tests(tmp_path).db_path.read_bytes()


def test_user_model_contract_rejects_custom_hosts_and_can_be_disabled(
    tmp_path, live_server
) -> None:
    from citation_tool.agent import AgentRunCreate
    from citation_tool.reasons import UserModelSelection

    with pytest.raises(ValueError):
        AgentRunCreate.model_validate(
            {
                "input_type": "keywords",
                "keywords": ["LFY2"],
                "model": {"provider": "custom", "model": "test", "api_key": "dummy"},
            }
        )
    selection = UserModelSelection.model_validate(
        {"provider": "deepseek", "model": "deepseek-flash", "api_key": "dummy"}
    )
    assert selection.to_model_config().base_url == "https://api.deepseek.com"
    body = AgentRunCreate.model_validate(
        {
            "input_type": "keywords",
            "keywords": ["LFY2"],
            "model": {"provider": "deepseek", "model": "deepseek-flash", "api_key": "dummy"},
        }
    )
    assert "model" not in body.model_dump()
    assert "dummy" not in repr(body)
    settings = replace(Settings.for_tests(tmp_path), allow_user_models=False)
    base = live_server(create_app(settings, agent_model=ScriptedChatModel()))
    with httpx.Client(timeout=10) as client:
        response = client.post(
            f"{base}/api/v1/agent/runs",
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
                "model": {"provider": "openai", "model": "test", "api_key": "dummy"},
            },
            headers=headers(),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "USER_MODEL_DISABLED"


def test_model_retry_budget_and_exception_diagnostics_are_bounded() -> None:
    from citation_tool.agent import _exception_chain

    config = ModelConfig(api_key="unit-test-placeholder")
    gateway = DeepSeekModelGateway(config, max_inflight=1, timeout_ms=1000)
    assert gateway.agent_model().max_retries == 4
    assert gateway.reason_model().max_retries == 4
    first = ValueError("outer")
    second = RuntimeError("inner")
    first.__cause__ = second
    second.__context__ = first
    assert _exception_chain(first) == ["ValueError", "RuntimeError"]


def test_candidate_alignment_rewards_evidence_coverage_without_faking_fallback() -> None:
    from citation_tool.candidates import candidate_scoring
    from test_units import make_candidate

    candidate = replace(
        make_candidate(),
        display_score=94.0465,
        score_version="rrf-test",
        score_scale_group="corpus-test",
    )
    candidate = replace(
        candidate,
        source=candidate.source.model_copy(update={"source_role": "metadata_api"}),
    )
    reason = RecommendationReason(
        status="generated",
        text="覆盖对象与比较条件。",
        provider="deepseek",
        model="deepseek-flash",
        prompt_version="candidate-alignment-v1",
        input_hash="test-input",
        generated_at=utc_now_iso(),
        alignment_score=3,
    )
    aligned = candidate_scoring(candidate, reason, provider="deepseek", supports_alignment=True)
    background = candidate_scoring(
        candidate,
        reason.model_copy(update={"alignment_score": 0}),
        provider="deepseek",
        supports_alignment=True,
    )
    assert aligned[0] == 100
    assert background[0] == 0
    assert "candidate-alignment-v6-common-score" in aligned[2]
    assert "corpus-test" not in aligned[2]
    fallback = candidate_scoring(
        candidate,
        reason.model_copy(update={"status": "failed", "alignment_score": None}),
        provider="deepseek",
        supports_alignment=True,
    )
    assert fallback == (None, None, None)


async def test_alignment_reason_cache_is_tenant_scoped_and_reproducible(tmp_path) -> None:
    from citation_tool.store import Store
    from test_units import make_candidate, make_claim

    store = Store(tmp_path / "reason-cache.db")
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content=(
                    '{"reason":"该文覆盖目标对象与研究范围，可进一步核查原文。",'
                    '"alignment_score":3,"missing_constraints":[]}'
                )
            ),
            AIMessage(
                content=(
                    '{"reason":"该文覆盖目标对象与研究范围，可进一步核查原文。",'
                    '"alignment_score":3,"missing_constraints":[]}'
                )
            ),
        ]
    )
    gateway = DeepSeekModelGateway(
        ModelConfig(api_key="unit-test-placeholder"), max_inflight=1, timeout_ms=1000
    )
    generator = DeepSeekReasonGenerator(
        gateway, max_inflight=1, timeout_ms=1000, model=model, cache=store
    )
    claim = make_claim()
    first = await generator.generate(claim, make_candidate())
    second = await generator.generate(claim, make_candidate())
    assert first.status == "generated"
    assert first.alignment_score == 3
    assert first == second
    assert len(model.responses) == 1
    other = await generator.generate(
        claim.model_copy(update={"tenant_id": "other"}), make_candidate()
    )
    assert other.status == "generated"
    assert other.input_hash != first.input_hash
    assert model.responses == []


async def test_reason_cache_does_not_merge_distinct_publication_versions(tmp_path) -> None:
    from citation_tool.store import Store
    from test_units import make_candidate, make_claim

    store = Store(tmp_path / "version-cache.db")
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content=(
                    '{"reason":"该文覆盖目标研究对象，可进一步核查全文。",'
                    '"alignment_score":3,"missing_constraints":[]}'
                )
            )
            for _ in range(2)
        ]
    )
    gateway = DeepSeekModelGateway(
        ModelConfig(api_key="unit-test-placeholder"), max_inflight=1, timeout_ms=1000
    )
    generator = DeepSeekReasonGenerator(
        gateway, max_inflight=1, timeout_ms=1000, model=model, cache=store
    )
    claim = make_claim()
    journal = await generator.generate(claim, make_candidate())
    preprint_candidate = replace(
        make_candidate(),
        passage=make_candidate().passage.model_copy(
            update={"work_id": "work:10.1101/example-preprint", "paper_id": "preprint"}
        ),
    )
    preprint = await generator.generate(claim, preprint_candidate)
    assert journal.alignment_score == preprint.alignment_score == 3
    assert journal.input_hash != preprint.input_hash
    assert model.responses == []


async def test_public_agent_capacity_returns_429_without_starting_another_run(
    tmp_path, live_server
) -> None:
    import sqlite3

    settings = replace(Settings.for_tests(tmp_path), agent_max_concurrent_runs=1)
    app = create_app(settings, agent_model=ScriptedChatModel())
    runner = app.state.agent_runner
    existing = asyncio.create_task(asyncio.sleep(30))
    runner._tasks["existing"] = existing
    base = live_server(app)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{base}/api/v1/agent/runs",
                json=search_body({"fixture-fast": 1, "fixture-slow": 1}),
                headers=headers(),
            )
        assert response.status_code == 429
        assert response.json()["code"] == "AGENT_CAPACITY_EXCEEDED"
        with sqlite3.connect(settings.db_path) as conn:
            status, error = conn.execute(
                "SELECT json_extract(payload_json, '$.status'), "
                "json_extract(payload_json, '$.error.code') FROM agent_runs"
            ).fetchone()
        assert status == "failed"
        assert error == "AGENT_CAPACITY_EXCEEDED"
    finally:
        existing.cancel()


async def test_agent_response_design_grounding_repairs_or_rejects(tmp_path, live_server) -> None:
    async def run(answers):
        model = ScriptedChatModel(
            responses=[
                tool_call("grounding-call", ["LFY2"], ["fixture-fast"]),
                *answers,
            ]
        )
        base = live_server(create_app(Settings.for_tests(tmp_path), agent_model=model))
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{base}/api/v1/agent/runs",
                json=search_body({"fixture-fast": 1}),
                headers=headers(),
            )
            run_id = response.json()["run_id"]
            for _ in range(300):
                run = (await client.get(f"{base}/api/v1/agent/runs/{run_id}")).json()
                if run["status"] in {"completed", "failed", "cancelled"}:
                    return run
                await asyncio.sleep(0.01)
        raise AssertionError("grounded Agent response did not finish")

    corrected = await run(
        [
            AIMessage(content="DOI 10.5555/demo-fast-001：该研究为开放标签试验。"),
            AIMessage(content="DOI 10.5555/demo-fast-001：摘要未报告开放标签，需核查全文。"),
        ]
    )
    assert corrected["status"] == "completed"
    assert corrected["response_grounding"] == "source-dois-v8-contextual-design"
    assert "未报告开放标签" in corrected["summary"]

    rejected = await run(
        [
            AIMessage(content="DOI 10.5555/demo-fast-001：该研究为开放标签试验。"),
            AIMessage(content="DOI 10.5555/demo-fast-001：该研究为开放标签试验。"),
        ]
    )
    assert rejected["status"] == "failed"
    assert rejected["error"]["code"] == "AGENT_UNGROUNDED_OUTPUT"
    assert rejected["error"]["design_attributes"] == ["open_label"]
    assert "开放标签" in rejected["summary"]
