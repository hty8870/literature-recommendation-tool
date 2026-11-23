from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from citation_tool.api import create_app
from citation_tool.candidate_pool import CandidatePool
from citation_tool.common_scoring import CommonScorer
from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import CandidateDiagnostic, stable_id
from citation_tool.jcs import sha256_json
from citation_tool.listwise import DiscoveryModels, ListwiseReranker, RankingDraft, RerankFailure
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.retrieval import build_fixture_adapters
from citation_tool.source_agent import SourceAgent
from citation_tool.store import Store
from test_agent import ScriptedChatModel
from test_integration import headers, wait_final
from test_units import make_claim


def record(number: int, abstract: str | None = None) -> dict:
    return {
        "work_id": f"work:10.1234/discovery-{number}",
        "paper_id": f"paper-{number}",
        "title": f"LFY2 soybean floral study {number}",
        "abstract": abstract
        or (
            f"LFY2 expression in soybean floral meristems. Study number {number} "
            "reports an association, not a causal experiment."
        ),
        "doi": f"10.1234/discovery-{number}",
        "authors": "Source Authors",
        "year": 2024,
        "source_metadata_url": f"https://example.org/paper/{number}",
        "simulation": False,
    }


class DiscoveryChatModel(BaseChatModel):
    role: str
    inputs: list[dict] = Field(default_factory=list)
    malformed: bool = False
    score_review_behavior: str = "keep"
    block_content: bool = False
    delay: float = 0

    @property
    def _llm_type(self):
        return "discovery-contract-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        payload = json.loads(messages[1].content)
        self.inputs.append(payload)
        if self.role == "source":
            if any(isinstance(message, ToolMessage) for message in messages):
                output = AIMessage(content="已提交当前库真实候选；无需机械重查。")
            elif payload["initial_candidates"]:
                output = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "submit",
                            "name": "submit_candidates",
                            "args": {
                                "candidate_ids": [
                                    item["candidate_id"] for item in payload["initial_candidates"]
                                ]
                            },
                        }
                    ],
                )
            else:
                output = AIMessage(content="当前库未返回可提交的候选。")
        elif "current_order" in payload:
            order = payload["current_order"]
            if self.score_review_behavior == "reverse":
                order = list(reversed(order))
            elif self.score_review_behavior == "invalid":
                order = [order[0], order[0], "outside-window"]
            output = AIMessage(
                content=json.dumps({"ordered_work_ids": order}),
                response_metadata={"model_name": "test-model"},
            )
        else:
            entries = sorted(
                payload["candidates"],
                key=lambda item: str(item.get("doi") or item.get("title") or item["work_id"]),
                reverse=True,
            )
            items = [
                {
                    "work_id": item["work_id"],
                    "reason": (
                        "该候选摘要涉及目标对象，可进一步核查研究范围与方法；"
                        "仅摘要信息，不能据此宣称已核验全文。"
                    ),
                    "evidence_id": next(
                        key for key in item["evidence_options"] if key.startswith("abstract:")
                    ),
                    "alignment_score": 2,
                    "missing_constraints": [],
                }
                for item in entries
            ]
            if self.malformed:
                items = items + items[:1]
            output = AIMessage(
                content=json.dumps({"items": items}, ensure_ascii=False),
                response_metadata={"model_name": "test-model"},
            )
        if self.block_content:
            output.content = [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "private-marker"}],
                },
                {"type": "text", "text": output.content},
            ]
        return ChatResult(generations=[ChatGeneration(message=output)])


def models(source=None, ranking=None) -> DiscoveryModels:
    return DiscoveryModels(
        source or DiscoveryChatModel(role="source"),
        ranking or DiscoveryChatModel(role="ranking"),
        "deepseek",
        "test-model",
        2,
    )


def setup(tmp_path, *, two_sources=False, ranking=None, source=None):
    libraries = (LibraryConfig("a", "A", "", 0, simulation=False),)
    if two_sources:
        libraries += (LibraryConfig("b", "B", "", 0, simulation=False),)
    settings = replace(
        Settings.for_tests(tmp_path), libraries=libraries, candidate_pipeline="agentic"
    )
    adapters = {
        library.library_id: MetadataCorpusAdapter(library, [record(1), record(2)])
        for library in libraries
    }
    discovery = models(source=source, ranking=ranking)
    app = create_app(settings, adapters=adapters, discovery_models=discovery)
    return app, discovery


class UnavailableSourceModel(DiscoveryChatModel):
    failure_kind: str = "model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.failure_kind == "timeout":
            raise TimeoutError("controlled source timeout")
        if self.failure_kind == "permission":
            from citation_tool.retrieval import SourceSearchError

            raise SourceSearchError("SOURCE_FORBIDDEN", "controlled source denial")
        raise RuntimeError("controlled decision service outage")


def test_source_model_failure_preserves_real_recall_without_fabricating_approval(
    tmp_path, live_server
):
    app, discovery = setup(tmp_path, source=UnavailableSourceModel(role="source"))
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={
                "input_type": "keywords",
                "keywords": ["LFY2", "soybean"],
            },
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "partial"
        assert not state["results"]
        assert len(state["candidate_diagnostics"]) == 2
        assert {row["work_id"] for row in state["candidate_diagnostics"]} == {
            record(1)["work_id"],
            record(2)["work_id"],
        }
        assert all(row["reason"]["status"] == "generated" for row in state["candidate_diagnostics"])
        assert state["candidate_rankings"][0]["status"] == "completed"
        worker = state["workers"][0]
        assert worker["worker_state"] == "failed"
        assert worker["source_agent_status"] == "failed"
        preserved = worker["last_error"]["details"]["recall_preservation"]
        assert preserved["submitted"] == 2 and preserved["agent_approved"] is False
        assert worker["source_actions"] == []
        assert worker["source_candidates_submitted"] == 2
        snapshot = client.get(
            f"{base}/api/v1/snapshots/{state['session']['final_snapshot_id']}", headers=headers()
        ).json()
        assert snapshot["items"] == []
    assert discovery.ranking_model.inputs


class PartlyUnavailableSourceModel(DiscoveryChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            raise RuntimeError("controlled outage after one actual submission")
        payload = json.loads(messages[1].content)
        identifier = payload["initial_candidates"][0]["candidate_id"]
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "first-only",
                                "name": "submit_candidates",
                                "args": {"candidate_ids": [identifier]},
                            }
                        ],
                    )
                )
            ]
        )


def test_preservation_does_not_resubmit_the_already_approved_record(tmp_path, live_server):
    app, _ = setup(tmp_path, source=PartlyUnavailableSourceModel(role="source"))
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
            },
        ).json()
        state = wait_final(client, base, created["query_id"])
        worker = state["workers"][0]
        assert len(state["candidate_diagnostics"]) == 2
        assert worker["source_candidates_submitted"] == 2
        assert len(worker["source_actions"]) == 1
        assert worker["source_actions"][0]["tool"] == "submit_candidates"
        assert worker["last_error"]["details"]["recall_preservation"]["submitted"] == 1
        assert state["candidate_rankings"][0]["pool_version"] == 2


@pytest.mark.parametrize("kind", ["timeout", "permission"])
def test_source_timeout_or_access_denial_never_triggers_recall_preservation(
    tmp_path, live_server, kind
):
    app, _ = setup(tmp_path, source=UnavailableSourceModel(role="source", failure_kind=kind))
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
            },
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert not state["candidate_diagnostics"]
        assert not state["results"]
        assert not state["workers"][0]["source_candidates_submitted"]


def test_successful_source_decision_can_still_choose_to_submit_nothing(tmp_path, live_server):
    source = ScriptedChatModel(responses=[AIMessage(content="当前候选不适合提交。")])
    app, _ = setup(tmp_path, source=source)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
            },
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["workers"][0]["source_agent_status"] == "completed"
        assert state["workers"][0]["source_candidates_submitted"] == 0
        assert not state["candidate_diagnostics"]


@pytest.mark.parametrize("block_content", [False, True])
def test_discovery_http_ranks_by_llm_and_preserves_both_sources(
    tmp_path, live_server, block_content
):
    app, discovery = setup(
        tmp_path,
        two_sources=True,
        source=DiscoveryChatModel(role="source", block_content=block_content),
        ranking=DiscoveryChatModel(role="ranking", block_content=block_content),
    )
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2", "soybean"]},
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "completed"
        assert not state["results"]
        items = state["candidate_diagnostics"]
        assert [item["work_id"] for item in items] == [record(2)["work_id"], record(1)["work_id"]]
        assert [item["llm_rank"] for item in items] == [1, 2]
        for item in items:
            assert item["library_ids"] == ["a", "b"]
            assert len(item["source_records"]) == 2
            assert len(item["retrieval_traces"]) == 2
            assert item["rank_delta"] == item["base_rank"] - item["llm_rank"]
            assert item["reason"]["status"] == "generated"
            assert item["reason_evidence_quote"] in item["evidence_excerpt"]
            assert all(
                annotation["quote"]
                == (item["title"] if annotation["field"] == "title" else item["evidence_excerpt"])[
                    annotation["start"] : annotation["end"]
                ]
                for annotation in item["match_annotations"]
            )
        assert all(worker["source_agent_status"] == "completed" for worker in state["workers"])
        assert all(worker["source_candidates_submitted"] == 2 for worker in state["workers"])
        ranking = state["candidate_rankings"][0]
        assert (
            ranking["status"] == "completed" and ranking["pool_version"] == items[0]["pool_version"]
        )
        history = app.state.store.get_candidate_ranking_history("local", created["query_id"])
        assert history and history[-1]["ranking"] == ranking
        assert ranking["phase"] is None
        events = app.state.store.list_events("local", created["query_id"], 0)
        phases = [
            event["payload"]["phase"]
            for event in events
            if event["event_type"] == "candidate_ranking_progress"
            and event["payload"]["pool_version"] == ranking["pool_version"]
        ]
        assert phases == ["blind", "score_review"]
        assert not app.state.store.get_candidate_ranking_history("other", created["query_id"])
        snapshot = app.state.store.get_snapshot("local", state["session"]["final_snapshot_id"])
        assert not json.loads(snapshot["manifest_json"])["items"]
    blind = [
        payload for payload in discovery.ranking_model.inputs if "current_order" not in payload
    ]
    reviewed = [payload for payload in discovery.ranking_model.inputs if "current_order" in payload]
    assert blind and reviewed
    prohibited = {
        "candidate_score",
        "base_rank",
        "blind_rank",
        "retrieval_scores",
        "common_features",
    }
    assert all(
        not (prohibited & item.keys()) for payload in blind for item in payload["candidates"]
    )
    final = {item["work_id"]: item for item in items}
    observed = {row["doi"]: row for row in reviewed[-1]["candidates"]}
    for work_id, item in final.items():
        row = observed[item["doi"]]
        assert row["candidate_score"] == item["candidate_score"]
        assert row["base_rank"] == item["base_rank"]
        assert (
            row["blind_rank"]
            == ranking["score_review"]["blind_ordered_work_ids"].index(work_id) + 1
        )
    assert ranking["score_review"]["status"] == "kept"


def test_invalid_llm_permutation_never_becomes_a_completed_ranking(tmp_path, live_server):
    app, _ = setup(tmp_path, ranking=DiscoveryChatModel(role="ranking", malformed=True))
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2"]},
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "partial"
        assert state["candidate_rankings"][0]["failure_code"] == "RERANK_INVALID_PERMUTATION"
        assert all(
            item["llm_rank"] is None and item["ranking_status"] == "failed"
            for item in state["candidate_diagnostics"]
        )
        assert all(item["reason"]["text"] is None for item in state["candidate_diagnostics"])


async def test_source_agent_can_submit_then_search_and_reject_unknown_id(tmp_path):
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    adapter = MetadataCorpusAdapter(library, [record(1), record(2)])
    claim = make_claim()
    first = adapter._candidate(record(1), claim, 20)
    second = adapter._candidate(record(2), claim, 30)

    def identifier(candidate):
        return stable_id(
            "sc_",
            "a",
            candidate.passage.paper_id,
            sha256_json([candidate.source.model_dump(), candidate.context]),
        )

    def call(name, args, number):
        return AIMessage(content="", tool_calls=[{"id": str(number), "name": name, "args": args}])

    model = ScriptedChatModel(
        responses=[
            call("submit_candidates", {"candidate_ids": ["other-library-id"]}, 1),
            call("submit_candidates", {"candidate_ids": [identifier(first)]}, 2),
            call("search_source", {"query": "another focused floral method"}, 3),
            call("submit_candidates", {"candidate_ids": [identifier(second)]}, 4),
            AIMessage(content="两个真实候选已提交。"),
        ]
    )
    events = []

    async def search(recall):
        assert recall.evaluation_anchor == claim.evaluation_anchor
        events.append("search")
        return [second]

    async def submit(candidate, recall):
        events.append(candidate.passage.paper_id)

    trace = await SourceAgent(model, Settings.for_tests(tmp_path), asyncio.Semaphore(1)).run(
        claim, "a", [first], search, submit
    )
    assert events == ["paper-1", "search", "paper-2"]
    assert trace["submitted_count"] == 2
    assert any(
        "CANDIDATE_NOT_FOUND" in str(message.content)
        for batch in model.received_messages
        for message in batch
        if isinstance(message, ToolMessage)
    )


def test_common_score_background_ignores_larger_simulated_libraries(tmp_path) -> None:
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    metadata = {"a": MetadataCorpusAdapter(library, [record(1)])}
    simulated = build_fixture_adapters(Settings.for_tests(tmp_path))

    real_background = CommonScorer(metadata).group
    assert real_background != CommonScorer({}).group
    assert CommonScorer({**simulated, **metadata}).group == real_background


async def test_common_score_does_not_depend_on_candidate_pool(tmp_path):
    app, _ = setup(tmp_path)
    scorer = app.state.coordinator._scorer
    claim = make_claim()
    pool = CandidatePool(claim, app.state.store, scorer, None, asyncio.Semaphore(1))
    diagnostic = CandidateDiagnostic(
        result_id="r",
        work_id=record(1)["work_id"],
        paper_id="p",
        claim_id=claim.claim_id,
        title=record(1)["title"],
        evidence_excerpt=record(1)["abstract"],
        library_ids=["a"],
        relationship="insufficient",
        scope_match="unknown",
        rejection_reasons=["abstract_metadata_only"],
        quality_note="test",
    )
    before = scorer.score(claim, diagnostic)
    scorer.score(
        claim, diagnostic.model_copy(update={"work_id": "new", "title": "irrelevant document"})
    )
    after = scorer.score(claim, diagnostic)
    assert before.candidate_score == after.candidate_score
    assert before.common_features == after.common_features
    assert before.score_scale_group == after.score_scale_group
    await pool.cancel()


async def test_listwise_validation_rejects_cross_paper_quotes_and_identifiers(tmp_path):
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    adapter = MetadataCorpusAdapter(library, [record(1)])
    claim = make_claim()
    candidate = adapter._candidate(record(1), claim, 20)
    item = CandidateDiagnostic(
        result_id="r",
        work_id=record(1)["work_id"],
        paper_id="p",
        claim_id=claim.claim_id,
        title=record(1)["title"],
        evidence_excerpt=record(1)["abstract"],
        library_ids=["a"],
        relationship="insufficient",
        scope_match="unknown",
        rejection_reasons=["abstract_metadata_only"],
        quality_note="test",
    )
    payload = {
        "items": [
            {
                "work_id": item.work_id,
                "reason": "该论文直接报告目标研究对象的相关信息。",
                "evidence_quote": "an invented evidence quote",
                "alignment_score": 2,
            }
        ]
    }
    with pytest.raises(RerankFailure, match="INVALID_EVIDENCE"):
        ListwiseReranker._validate(
            RankingDraft.model_validate(payload), {item.work_id: item}, {item.work_id: candidate}
        )
    payload["items"][0]["evidence_quote"] = item.evidence_excerpt[:30]
    payload["items"][0]["reason"] = "该文已经证明AP99调控全部组织中的表达。"
    with pytest.raises(RerankFailure, match="UNGROUNDED"):
        ListwiseReranker._validate(
            RankingDraft.model_validate(payload), {item.work_id: item}, {item.work_id: candidate}
        )


def test_ranking_cache_reuses_identical_evidence_but_not_another_tenant(tmp_path, live_server):
    app, discovery = setup(tmp_path)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        states = []
        counts = []
        for tenant in ("local", "local", "other"):
            created = client.post(
                f"{base}/api/v1/searches",
                headers=headers(tenant),
                json={"input_type": "keywords", "keywords": ["LFY2", "soybean"]},
            ).json()
            states.append(wait_final(client, base, created["query_id"], tenant=tenant))
            counts.append(len(discovery.ranking_model.inputs))
        assert counts[1] == counts[0] and counts[2] > counts[1]
        assert (
            states[0]["candidate_rankings"][0]["input_hash"]
            == states[1]["candidate_rankings"][0]["input_hash"]
        )
        assert (
            states[0]["candidate_rankings"][0]["input_hash"]
            != states[2]["candidate_rankings"][0]["input_hash"]
        )
        assert (
            client.get(
                f"{base}/api/v1/searches/{states[0]['session']['query_id']}/candidate-rankings",
                headers=headers("other"),
            ).status_code
            == 404
        )


def test_fast_source_publishes_rank_before_slow_source_finishes(tmp_path, live_server):
    app, _ = setup(
        tmp_path, two_sources=True, ranking=DiscoveryChatModel(role="ranking", delay=0.08)
    )
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={
                "input_type": "keywords",
                "keywords": ["LFY2"],
                "options": {"library_delays_ms": {"b": 500}},
            },
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "completed"
        events = app.state.store.list_events("local", created["query_id"], 0)
        first_rank = next(
            event["sequence"] for event in events if event["event_type"] == "candidate_reranked"
        )
        slow_complete = next(
            event["sequence"]
            for event in events
            if event["event_type"] == "worker_completed"
            and event["payload"]["worker"]["library_id"] == "b"
        )
        assert first_rank < slow_complete
        versions = [
            event["payload"]["ranking"]["pool_version"]
            for event in events
            if event["event_type"] == "candidate_reranked"
        ]
        assert versions == sorted(set(versions))
        assert versions[-1] == state["candidate_rankings"][0]["pool_version"]
        replay = client.get(
            f"{base}/api/v1/searches/{created['query_id']}/events", headers=headers()
        ).text
        assert "event: candidate_pool_updated" in replay and "event: candidate_reranked" in replay


def test_different_source_abstracts_are_preserved_not_concatenated(tmp_path, live_server):
    app, _ = setup(tmp_path, two_sources=True)
    library = app.state.settings.library("b")
    richer = record(
        1,
        "LFY2 expression in soybean floral meristems. This longer source record reports "
        "additional experimental detail but does not establish causality. The supplied "
        "abstract is a separate source version and must not be spliced with any other abstract.",
    )
    app.state.coordinator.adapters["b"] = MetadataCorpusAdapter(library, [richer])
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2"]},
        ).json()
        state = wait_final(client, base, created["query_id"])
        item = next(
            item
            for item in state["candidate_diagnostics"]
            if item["work_id"] == record(1)["work_id"]
        )
        assert item["evidence_excerpt"] == richer["abstract"]
        assert {entry["abstract"] for entry in item["source_records"]} == {
            record(1)["abstract"],
            richer["abstract"],
        }
        assert item["evidence_excerpt"] != record(1)["abstract"] + richer["abstract"]
        assert item["score_status"] == "scoped"


def test_cancel_interrupts_source_agent_without_fabricated_completion(tmp_path, live_server):
    app, discovery = setup(tmp_path)
    discovery.source_model.delay = 0.4
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2"]},
        ).json()
        time.sleep(0.08)
        client.post(f"{base}/api/v1/searches/{created['query_id']}/cancel", headers=headers())
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "cancelled"
        assert state["workers"][0]["worker_state"] == "cancelled"
        assert state["workers"][0]["source_agent_status"] != "completed"


def test_model_unavailable_is_not_silently_a_source_agent_success(tmp_path, live_server):
    app, _ = setup(tmp_path)
    app.state.coordinator.discovery_models = None
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2"]},
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "partial"
        assert state["workers"][0]["source_agent_status"] == "unavailable"
        assert not state["candidate_diagnostics"]


async def test_source_database_error_is_observable_and_same_query_can_recover(tmp_path):
    from citation_tool.retrieval import SourceSearchError

    library = LibraryConfig("a", "A", "", 0, simulation=False)
    adapter = MetadataCorpusAdapter(library, [record(1)])
    claim = make_claim()
    candidate = adapter._candidate(record(1), claim, 20)
    identifier = stable_id(
        "sc_",
        "a",
        candidate.passage.paper_id,
        sha256_json([candidate.source.model_dump(), candidate.context]),
    )
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "first",
                        "name": "search_source",
                        "args": {"query": "focused retry query"},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "retry",
                        "name": "search_source",
                        "args": {"query": "focused retry query"},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "submit",
                        "name": "submit_candidates",
                        "args": {"candidate_ids": [identifier]},
                    }
                ],
            ),
            AIMessage(content="重试已恢复并提交真实候选。"),
        ]
    )
    calls = []

    async def search(recall):
        calls.append(recall.claim_text)
        if len(calls) == 1:
            raise SourceSearchError("SOURCE_TIMEOUT", "数据库本次请求超时。")
        return [candidate]

    submitted = []

    async def submit(item, recall):
        submitted.append(item.passage.work_id)

    result = await SourceAgent(model, Settings.for_tests(tmp_path), asyncio.Semaphore(1)).run(
        claim,
        "a",
        [],
        search,
        submit,
    )
    assert calls == ["focused retry query", "focused retry query"]
    assert submitted == [candidate.passage.work_id]
    assert result["last_source_error"] is None
    assert any(action.get("error") == "SOURCE_TIMEOUT" for action in result["actions"])


async def test_listwise_repairs_only_invalid_reason_without_changing_valid_order(tmp_path):
    class RepairModel(DiscoveryChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            payload = json.loads(messages[1].content)
            if len(payload["candidates"]) > 1:
                content = json.loads(result.generations[0].message.content)
                content["items"][0]["reason"] = "该研究已经明确证明AP99调控所有组织。"
                result.generations[0].message.content = json.dumps(content, ensure_ascii=False)
            return result

    library = LibraryConfig("a", "A", "", 0, simulation=False)
    adapter = MetadataCorpusAdapter(library, [record(1), record(2)])
    claim = make_claim()
    candidates = [adapter._candidate(record(number), claim, 20) for number in (1, 2)]
    items = [
        CandidateDiagnostic(
            result_id=f"r-{index}",
            work_id=candidate.passage.work_id,
            paper_id=candidate.passage.paper_id,
            claim_id=claim.claim_id,
            title=candidate.source.title,
            evidence_excerpt=candidate.context,
            library_ids=["a"],
            relationship="insufficient",
            scope_match="unknown",
            rejection_reasons=["abstract_metadata_only"],
            quality_note="test",
        )
        for index, candidate in enumerate(candidates)
    ]
    model = RepairModel(role="ranking")
    reranker = ListwiseReranker(
        models(ranking=model), Store(tmp_path / "repair.db"), asyncio.Semaphore(1)
    )
    outcome = await reranker.rank(
        claim, items, {candidate.passage.work_id: candidate for candidate in candidates}
    )
    assert [item.work_id for item in outcome.items] == [record(2)["work_id"], record(1)["work_id"]]
    assert len(model.inputs) == 2
    assert len(model.inputs[0]["candidates"]) == 2 and len(model.inputs[1]["candidates"]) == 1


def test_source_decision_never_serializes_hidden_reasoning_blocks():
    from citation_tool.source_agent import decision_text

    assert (
        decision_text(
            [
                {"type": "reasoning", "text": "hidden internal reasoning"},
                {"type": "text", "text": "已提交真实候选。"},
            ]
        )
        == "已提交真实候选。"
    )


def test_discovery_monitoring_does_not_confuse_a_good_answer_with_a_failed_board():
    from tools.quality_benchmark.monitoring import discovery_metrics, required_flow

    query = {
        "candidate_rankings": [{"claim_id": "c", "status": "failed"}],
        "candidate_diagnostics": [
            {
                "claim_id": "c",
                "work_id": "w",
                "ranking_status": "failed",
                "reason": {"status": "failed"},
            }
        ],
        "workers": [{"source_available_work_ids": ["w", "not-submitted"]}],
    }
    metrics = discovery_metrics([query])
    assert metrics["ranked_candidates"] == 0 and metrics["failed_rankings"] == 1
    flow = required_flow({"expect": {"required_ids": ["w", "not-submitted"]}}, [query])
    assert flow["source_required_not_submitted"] == ["not-submitted"]
    assert flow["submitted_required"] == ["w"]
    assert flow["ranked_required"] == [] and flow["all_required_ranked"] is False
    assert discovery_metrics([{"candidate_rankings": [], "candidate_diagnostics": []}]) is None


def test_initial_database_failure_can_be_recovered_by_the_source_agent(
    tmp_path, live_server, monkeypatch
):
    from citation_tool.retrieval import SourceSearchError

    app, discovery = setup(tmp_path)
    adapter = app.state.coordinator.adapters["a"]
    original = adapter.search
    attempts = []

    def flaky_search(claim):
        attempts.append(claim.claim_text)
        if len(attempts) == 1:
            raise SourceSearchError("SOURCE_TIMEOUT", "临时数据库超时。")
        return original(claim)

    monkeypatch.setattr(adapter, "search", flaky_search)
    candidate = adapter._candidate(record(1), make_claim(), 20)
    identifier = stable_id(
        "sc_",
        "a",
        candidate.passage.paper_id,
        sha256_json([candidate.source.model_dump(), candidate.context]),
    )
    source = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "retry",
                        "name": "search_source",
                        "args": {"query": "LFY2", "keywords": ["LFY2"]},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "submit",
                        "name": "submit_candidates",
                        "args": {"candidate_ids": [identifier]},
                    }
                ],
            ),
            AIMessage(content="数据库恢复，已提交真实候选。"),
        ]
    )
    app.state.coordinator.discovery_models = models(source=source, ranking=discovery.ranking_model)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2"]},
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["session"]["search_state"] == "completed"
        assert state["workers"][0]["last_error"] is None
        assert state["workers"][0]["source_available_work_ids"]
        assert state["candidate_diagnostics"][0]["ranking_status"] == "ranked"
        assert len(attempts) == 2


def test_evidence_ids_resolve_exact_source_characters_and_reject_fakes():
    from citation_tool.listwise import evidence_options, resolve_evidence

    item = CandidateDiagnostic(
        result_id="r",
        work_id="w",
        paper_id="p",
        claim_id="c",
        title="A genuine source title",
        evidence_excerpt="This source contains a non-breaking\u00a0space and original Case.",
        library_ids=["a"],
        relationship="insufficient",
        scope_match="unknown",
        rejection_reasons=["abstract_metadata_only"],
        quality_note="test",
    )
    identifier = next(key for key in evidence_options(item) if key.startswith("abstract:"))
    payload = {
        "items": [
            {
                "work_id": "w",
                "reason": "该篇涉及实际研究对象，可进一步核查范围。",
                "evidence_id": identifier,
                "alignment_score": 2,
            }
        ]
    }
    resolved = resolve_evidence(RankingDraft.model_validate(payload), {"w": item})
    assert resolved.items[0].evidence_quote == item.evidence_excerpt
    assert "\u00a0" in resolved.items[0].evidence_quote
    payload["items"][0]["evidence_id"] = "invented"
    with pytest.raises(RerankFailure, match="INVALID_EVIDENCE"):
        resolve_evidence(RankingDraft.model_validate(payload), {"w": item})
    payload["items"][0]["evidence_id"] = identifier
    payload["items"][0]["evidence_quote"] = item.evidence_excerpt.replace("\u00a0", " ")
    with pytest.raises(RerankFailure, match="INVALID_EVIDENCE"):
        resolve_evidence(RankingDraft.model_validate(payload), {"w": item})


async def test_design_review_distinguishes_search_terms_from_scientific_assertions():
    from citation_tool.design_review import DesignReviewer, review_inputs

    summary = "建议独立检索 randomized overall survival。"
    targets, _ = review_inputs(summary, [], ["randomized"])
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content=json.dumps(
                    {
                        "checks": [
                            {
                                "check_id": targets[0]["check_id"],
                                "kind": "non_assertion",
                                "category": "search_instruction",
                                "evidence_ids": [],
                            }
                        ]
                    }
                )
            )
        ]
    )
    violations, audit = await DesignReviewer(model, "test").review(summary, [], ["randomized"], 2)
    assert not violations and audit["status"] == "checked"
    assert audit["target_count"] == 1


def test_design_review_cannot_borrow_another_trials_actual_design_evidence():
    from citation_tool.design_review import DesignReviewDraft, review_inputs, validate_review

    sources = [
        {
            "work_id": "a",
            "doi": "10.1234/a",
            "title": "Trial A",
            "source_text": "Dose escalation study; no design detail.",
        },
        {
            "work_id": "b",
            "doi": "10.1234/b",
            "title": "Trial B",
            "source_text": "A randomized trial with explicit allocation.",
        },
    ]
    targets, facts = review_inputs("DOI 10.1234/a：该研究采用随机化设计。", sources, ["randomized"])
    draft = DesignReviewDraft.model_validate(
        {
            "checks": [
                {
                    "check_id": "d0",
                    "kind": "supported",
                    "evidence_ids": [next(iter(facts))],
                }
            ]
        }
    )
    with pytest.raises(ValueError, match="another paper"):
        validate_review(draft, targets, facts, sources)
    with pytest.raises(ValueError, match="coverage"):
        validate_review(DesignReviewDraft(checks=[]), targets, facts, sources)
    draft.checks[0].evidence_ids = ["fabricated"]
    with pytest.raises(ValueError, match="unknown design evidence"):
        validate_review(draft, targets, facts, sources)


def test_reason_species_translations_reuse_existing_scope_aliases():
    from citation_tool.reasons import reason_entities_grounded
    from test_units import make_candidate

    arabidopsis = make_candidate(
        raw_text="The study measured flowering in Arabidopsis thaliana.",
        quote="The study measured flowering in Arabidopsis thaliana.",
    )
    assert reason_entities_grounded("该研究在拟南芥中测量开花表型，仍需核查全文。", arabidopsis)
    assert not reason_entities_grounded("该研究在水稻中验证了开花表型。", arabidopsis)
    rice = make_candidate(
        raw_text="The study measured flowering in rice.",
        quote="The study measured flowering in rice.",
    )
    assert reason_entities_grounded("该研究采用水稻材料，需核查适用范围。", rice)
    assert reason_entities_grounded("该研究涉及Oryza sativa开花表型。", rice)


def test_empty_original_scope_cannot_be_replaced_by_an_agents_recall_scope():
    from citation_tool.common_scoring import evaluation_claim
    from citation_tool.entities import EvaluationAnchor, EvidenceScope

    claim = make_claim().model_copy(
        update={
            "evaluation_anchor": EvaluationAnchor(claim_text="original question", scope=None),
            "scope": EvidenceScope(system=["invented greenhouse"], method=["invented assay"]),
        }
    )
    assert evaluation_claim(claim).scope == EvidenceScope()


async def test_source_repeated_records_are_compact_and_report_real_novelty(tmp_path):
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    adapter = MetadataCorpusAdapter(library, [record(1)])
    claim = make_claim()
    candidate = adapter._candidate(record(1), claim, 20)
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "again", "name": "search_source", "args": {"query": "another wording"}}
                ],
            ),
            AIMessage(content="此次没有新增记录，已有候选已知，无须无目的重复。"),
        ]
    )

    async def search(recall):
        return [candidate]

    async def submit(item, recall):
        raise AssertionError("The test source chose not to submit")

    trace = await SourceAgent(model, Settings.for_tests(tmp_path), asyncio.Semaphore(1)).run(
        claim, "a", [candidate], search, submit
    )
    returned = next(
        message for message in model.received_messages[-1] if isinstance(message, ToolMessage)
    )
    payload = json.loads(returned.content)
    assert payload["new_work_count"] == 0 and payload["new_record_count"] == 0
    assert payload["unique_works_seen"] == 1
    assert payload["candidates"][0]["already_seen"] is True
    assert "abstract" not in payload["candidates"][0]
    assert trace["actions"][0]["new_work_count"] == 0


def test_claim_target_names_are_allowed_only_in_explicit_negative_comparisons():
    from citation_tool.reasons import reason_entities_grounded
    from test_units import make_candidate

    candidate = make_candidate(
        raw_text="LFY1 expression was measured.", quote="LFY1 expression was measured."
    )
    candidate = replace(
        candidate, source=candidate.source.model_copy(update={"title": "LFY1 study"})
    )
    claim = make_claim(text="Find LFY2 studies")
    assert reason_entities_grounded("该文研究LFY1而非LFY2，不符合指定核心对象。", candidate, claim)
    assert reason_entities_grounded("本篇未对LFY2进行验证，只能提供背景。", candidate, claim)
    assert not reason_entities_grounded("本篇证明LFY2促进表达并调控开花。", candidate, claim)
    assert not reason_entities_grounded("本篇未涉及LFY2。LFY2促进表达。", candidate, claim)
    assert not reason_entities_grounded("本篇未涉及AP99，只能提供背景。", candidate, claim)


def test_compound_mutant_notation_requires_known_source_components():
    from citation_tool.reasons import reason_entities_grounded
    from test_units import make_candidate

    candidate = make_candidate(
        raw_text="LFY1 and LFY2 double mutants were infertile.",
        quote="LFY1 and LFY2 double mutants were infertile.",
    )
    assert reason_entities_grounded("lfy1lfy2双突变体出现不育表型，仍需核查全文。", candidate)
    assert not reason_entities_grounded("lfy1lfy99双突变体出现不育表型。", candidate)
    assert not reason_entities_grounded("该研究发现lfy1lfy2蛋白调控花发育。", candidate)


@pytest.mark.parametrize("invalid", [False, True])
def test_retrieval_trace_only_updates_do_not_invalidate_the_pool_or_lose_ranks(
    tmp_path, live_server, monkeypatch, invalid
):
    ranking = DiscoveryChatModel(role="ranking", malformed=invalid, delay=0.15)
    app, _ = setup(tmp_path, ranking=ranking)
    adapter = app.state.coordinator.adapters["a"]
    original = adapter.search

    def delayed_recall(claim):
        if "repeated" in claim.claim_text:
            time.sleep(0.45)
        return original(claim)

    monkeypatch.setattr(adapter, "search", delayed_recall)
    identifiers = []
    for number in (1, 2):
        candidate = adapter._candidate(record(number), make_claim(), 20)
        identifiers.append(
            stable_id(
                "sc_",
                "a",
                candidate.passage.paper_id,
                sha256_json([candidate.source.model_dump(), candidate.context]),
            )
        )
    source = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "submit-first",
                        "name": "submit_candidates",
                        "args": {"candidate_ids": identifiers},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "recall",
                        "name": "search_source",
                        "args": {"query": "LFY2 soybean repeated", "keywords": ["LFY2", "soybean"]},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "submit-trace",
                        "name": "submit_candidates",
                        "args": {"candidate_ids": identifiers},
                    }
                ],
            ),
            AIMessage(content="同一来源的新查询轨迹已保留，无新文献。"),
        ]
    )
    app.state.coordinator.discovery_models = models(source=source, ranking=ranking)
    base = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{base}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2"]},
        ).json()
        state = wait_final(client, base, created["query_id"])
        assert state["candidate_rankings"][0]["pool_version"] == 2
        assert all(len(item["retrieval_traces"]) == 2 for item in state["candidate_diagnostics"])
        events = app.state.store.list_events("local", created["query_id"], 0)
        assert sum(event["event_type"] == "candidate_provenance_updated" for event in events) == 2
        assert state["session"]["search_state"] == ("partial" if invalid else "completed")
        if not invalid:
            assert all(
                item["ranking_status"] == "ranked" for item in state["candidate_diagnostics"]
            )


@pytest.mark.parametrize(
    "text",
    [
        "这里的材料并非大豆LFY2，不能作为大豆LFY2的直接证据。",
        "本篇未直接研究LFY2，不能替代直接证据。",
        "摘要未提及LFY2，也未分析LFY2，只能作为背景。",
        "本篇与用户所需的大豆LFY2直接研究不符。",
        "该工作与LFY2花器官发育无直接关系，摘要未提及LFY2。",
        "这是一篇非LFY2直接研究，仅为背景。",
        "本篇不能替代针对LFY2的直接研究。",
    ],
)
def test_natural_query_comparisons_do_not_become_false_source_assertions(text):
    from citation_tool.reasons import reason_entities_grounded
    from test_units import make_candidate

    candidate = make_candidate(
        raw_text="AP1 expression was measured in soybean.",
        quote="AP1 expression was measured in soybean.",
    )
    candidate = replace(
        candidate, source=candidate.source.model_copy(update={"title": "AP1 source study"})
    )
    assert reason_entities_grounded(text, candidate, make_claim(text="Find LFY2 soybean studies"))
    assert not reason_entities_grounded(
        "本篇未研究LFY2，但本篇证实LFY2调控AP1。", candidate, make_claim(text="Find LFY2 studies")
    )
    assert not reason_entities_grounded(
        "这一反应并非LFY2介导，而是其他机制。", candidate, make_claim(text="Find LFY2 studies")
    )


async def test_listwise_large_pool_uses_short_window_ids_and_restores_real_ids(tmp_path):
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    records = [record(number) for number in range(1, 27)]
    adapter = MetadataCorpusAdapter(library, records)
    claim = make_claim()
    candidates = [adapter._candidate(row, claim, 20) for row in records]
    items = [
        CandidateDiagnostic(
            result_id=f"r-{number}",
            work_id=candidate.passage.work_id,
            paper_id=candidate.passage.paper_id,
            claim_id=claim.claim_id,
            title=candidate.source.title,
            doi=candidate.source.doi,
            evidence_excerpt=candidate.context,
            library_ids=["a"],
            relationship="insufficient",
            scope_match="unknown",
            rejection_reasons=["abstract_metadata_only"],
            quality_note="test",
        )
        for number, candidate in enumerate(candidates)
    ]
    model = DiscoveryChatModel(role="ranking")
    outcome = await ListwiseReranker(
        models(ranking=model), Store(tmp_path / "short-id.db"), asyncio.Semaphore(1)
    ).rank(
        claim,
        items,
        {candidate.passage.work_id: candidate for candidate in candidates},
    )
    ranked = outcome.items
    assert outcome.window_count > 1 and len(ranked) == 26
    assert {item.work_id for item in ranked} == {
        candidate.passage.work_id for candidate in candidates
    }
    assert all(item.work_id.startswith("work:") for item in ranked)
    for payload in model.inputs:
        assert [item["work_id"] for item in payload["candidates"]] == [
            f"c{index}" for index in range(len(payload["candidates"]))
        ]


def test_insufficient_model_balance_is_not_a_generic_upstream_error():
    from citation_tool.agent import _agent_error_code
    from citation_tool.reasons import _failure_code

    error = type("APIStatusError", (Exception,), {"status_code": 402})()
    assert _failure_code(error) == "MODEL_INSUFFICIENT_BALANCE"
    assert _agent_error_code(error) == "MODEL_INSUFFICIENT_BALANCE"
