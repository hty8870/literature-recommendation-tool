from __future__ import annotations

import asyncio

import httpx
import pytest

from citation_tool.config import LibraryConfig
from citation_tool.entities import CandidateDiagnostic
from citation_tool.listwise import ListwiseReranker
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.store import Store
from test_discovery import DiscoveryChatModel, models, record, setup
from test_integration import headers, wait_final
from test_units import make_claim


def pool(size):
    claim = make_claim()
    rows = [record(index) for index in range(1, size + 1)]
    adapter = MetadataCorpusAdapter(LibraryConfig("a", "A", "", 0, simulation=False), rows)
    items, evidence = [], {}
    for index, row in enumerate(rows, 1):
        source = adapter._candidate(row, claim, 20)
        work_id = source.passage.work_id
        evidence[work_id] = source
        items.append(
            CandidateDiagnostic(
                result_id=f"r-{index}",
                work_id=work_id,
                paper_id=source.passage.paper_id,
                claim_id=claim.claim_id,
                title=source.source.title,
                doi=source.source.doi,
                evidence_excerpt=source.context,
                library_ids=["a"],
                relationship="insufficient",
                scope_match="unknown",
                rejection_reasons=["abstract_metadata_only"],
                quality_note="test",
                candidate_score=100 - index,
                base_rank=index,
                score_version="common-test",
                score_scale_group="single-group",
            )
        )
    return claim, items, evidence


async def test_same_blind_payload_despite_score_rank_and_arrival_changes(tmp_path):
    claim, items, evidence = pool(2)
    model = DiscoveryChatModel(role="ranking", score_review_behavior="reverse")
    store = Store(tmp_path / "score-review.db")
    reranker = ListwiseReranker(models(ranking=model), store, asyncio.Semaphore(1))
    first = await reranker.rank(claim, items, evidence)
    assert first.score_review.status == "revised"
    assert first.score_review.served_model == "test-model"
    assert [item.work_id for item in first.items] == list(
        reversed(first.score_review.blind_ordered_work_ids)
    )
    assert first == await reranker.rank(claim, list(reversed(items)), evidence)
    assert len(model.inputs) == 2
    swapped = [
        item.model_copy(
            update={"candidate_score": other.candidate_score, "base_rank": other.base_rank}
        )
        for item, other in zip(reversed(items), items, strict=True)
    ]
    changed = await reranker.rank(claim, swapped, evidence)
    assert changed.input_hash != first.input_hash
    assert changed.score_review.blind_ordered_work_ids == first.score_review.blind_ordered_work_ids
    assert model.inputs[0] == model.inputs[2]
    assert model.inputs[1] != model.inputs[3]
    assert all(item.reason and item.evidence_quote for item in changed.items)


@pytest.mark.parametrize("case", ["single", "unscored", "mixed_scale", "disabled", "missing"])
async def test_reveal_is_skipped_when_not_useful_and_missing_scores_stay_null(tmp_path, case):
    claim, items, evidence = pool(1 if case == "single" else 2)
    if case == "unscored":
        items = [item.model_copy(update={"score_status": "failed"}) for item in items]
    if case == "mixed_scale":
        items[0] = items[0].model_copy(update={"score_scale_group": "another-group"})
    if case == "missing":
        items[0] = items[0].model_copy(update={"candidate_score": None, "base_rank": None})
    model = DiscoveryChatModel(role="ranking")
    reranker = ListwiseReranker(
        models(ranking=model),
        Store(tmp_path / "review.db"),
        asyncio.Semaphore(1),
        score_review_enabled=case != "disabled",
    )
    outcome = await reranker.rank(claim, items, evidence)
    if case == "missing":
        assert outcome.score_review.status == "kept"
        reveal = {item["doi"]: item for item in model.inputs[1]["candidates"]}
        assert reveal[items[0].doi]["candidate_score"] is None
        assert reveal[items[0].doi]["base_rank"] is None
        assert reveal[items[1].doi]["base_rank"] == 2
    else:
        assert outcome.score_review.status == "not_applicable"
        assert outcome.score_review.window_count == 0
        assert len(model.inputs) == 1


@pytest.mark.parametrize("behavior, expected", [("reverse", "revised"), ("invalid", "failed")])
def test_score_review_updates_only_order_or_keeps_valid_blind_fallback(
    tmp_path, live_server, behavior, expected
):
    model = DiscoveryChatModel(role="ranking", score_review_behavior=behavior)
    app, _ = setup(tmp_path, ranking=model)
    url = live_server(app)
    with httpx.Client(timeout=20) as client:
        created = client.post(
            f"{url}/api/v1/searches",
            headers=headers(),
            json={"input_type": "keywords", "keywords": ["LFY2", "soybean"]},
        ).json()
        state = wait_final(client, url, created["query_id"])
    board = state["candidate_rankings"][0]
    review = board["score_review"]
    blind = [record(2)["work_id"], record(1)["work_id"]]
    assert review["blind_ordered_work_ids"] == blind
    assert review["status"] == expected
    assert board["status"] == "completed"
    rows = state["candidate_diagnostics"]
    assert all(item["rank_delta"] == item["base_rank"] - item["llm_rank"] for item in rows)
    assert all(item["reason_evidence_quote"] in item["evidence_excerpt"] for item in rows)
    assert [item["work_id"] for item in rows] == board["ordered_work_ids"]
    history = app.state.store.get_candidate_ranking_history("local", created["query_id"])
    assert history[-1]["ranking"] == board
    if behavior == "invalid":
        assert state["session"]["search_state"] == "partial"
        assert review["failure_code"] == "RERANK_INVALID_PERMUTATION"
        assert not app.state.store.get_cached_ranking("local", board["input_hash"])
        assert board["ordered_work_ids"] == blind
        assert len(model.inputs) == 3
    else:
        assert state["session"]["search_state"] == "completed"
        assert board["ordered_work_ids"] == list(reversed(blind))
        assert len(model.inputs) == 2


async def test_large_pool_never_reveals_scores_before_all_blind_windows(tmp_path):
    claim, items, evidence = pool(26)
    model = DiscoveryChatModel(role="ranking")
    reranker = ListwiseReranker(
        models(ranking=model), Store(tmp_path / "large.db"), asyncio.Semaphore(1)
    )
    outcome = await reranker.rank(claim, items, evidence)
    assert outcome.window_count > 1
    assert outcome.score_review.window_count * 2 == outcome.window_count
    blind, review = model.inputs[: outcome.window_count], model.inputs[outcome.window_count :]
    assert all("current_order" not in item for item in blind)
    assert all("candidate_score" not in row for item in blind for row in item["candidates"])
    assert {row["doi"] for item in review for row in item["candidates"]} == {
        row.doi for row in items
    }
    assert all("current_order" in item for item in review)
    assert outcome.score_review.status == "kept"
    assert len({item.work_id for item in outcome.items}) == 26


async def test_aborted_review_does_not_publish_or_cache_an_order(tmp_path, monkeypatch):
    claim, items, evidence = pool(2)
    model = DiscoveryChatModel(role="ranking")
    store = Store(tmp_path / "cancel.db")
    reranker = ListwiseReranker(models(ranking=model), store, asyncio.Semaphore(1))
    entered = asyncio.Event()
    never = asyncio.Event()

    async def block_review(*args):
        entered.set()
        await never.wait()

    monkeypatch.setattr(reranker, "_review_order", block_review)
    task = asyncio.create_task(reranker.rank(claim, items, evidence))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(model.inputs) == 1
    with store._read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ranking_cache").fetchone()[0] == 0


def test_rollback_switch_disables_only_score_review(monkeypatch):
    from citation_tool.runtime import settings_from_env

    monkeypatch.setenv("CITATION_LISTWISE_SCORE_REVIEW", "0")
    settings = settings_from_env()
    assert settings.candidate_pipeline == "agentic"
    assert settings.listwise_score_review is False
