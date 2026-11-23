from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

from citation_tool.config import LibraryConfig
from citation_tool.entities import CandidateDiagnostic
from citation_tool.listwise import ListwiseReranker, RerankFailure
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.store import Store
from test_agent import ScriptedChatModel
from test_discovery import models, record
from test_units import make_claim


class EvidenceAwareScriptedModel(ScriptedChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        payload = json.loads(messages[1].content)
        candidates = {item["work_id"]: item for item in payload["candidates"]}
        message = result.generations[0].message
        output = json.loads(message.content)
        for item in output.get("items", []):
            source = candidates.get(item["work_id"])
            if item.get("evidence_id") == "fixture-first-abstract" and source is not None:
                item["evidence_id"] = next(
                    key for key in source["evidence_options"] if key.startswith("abstract:")
                )
        message.content = json.dumps(output, ensure_ascii=False)
        return result


def window(tmp_path, responses):
    claim = make_claim()
    first = record(1)
    second = record(2, "AP1 expression in soybean floral meristems was measured.")
    second["title"] = "AP1 expression in soybean"
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    adapter = MetadataCorpusAdapter(library, [first, second])
    candidates = [adapter._candidate(row, claim, 20) for row in (first, second)]
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
    model = EvidenceAwareScriptedModel(responses=responses)
    reranker = ListwiseReranker(
        models(ranking=model), Store(tmp_path / "rank.db"), asyncio.Semaphore(1)
    )
    return (
        reranker,
        model,
        claim,
        {item.work_id: item for item in items},
        {item.passage.work_id: item for item in candidates},
    )


def decision(identifier, reason="该研究报告来源对象的表达观察，可作背景；尚无全文证据。"):
    return {
        "work_id": identifier,
        "reason": reason,
        "evidence_id": "fixture-first-abstract",
        "alignment_score": 1,
        "missing_constraints": [],
    }


def response(items, **extra):
    return AIMessage(content=json.dumps({"items": items, **extra}, ensure_ascii=False))


async def test_schema_retry_includes_exact_error_and_rejected_response(tmp_path):
    rejected = response([decision("c1"), decision("c0")], type="json")
    reranker, model, claim, subset, evidence = window(
        tmp_path, [rejected, response([decision("c1"), decision("c0")])]
    )
    draft, _ = await reranker._window(
        claim, [reranker._input(item) for item in subset.values()], subset, evidence
    )
    assert [item.work_id for item in draft.items] == list(reversed(subset))
    correction = model.received_messages[1]
    assert any(
        isinstance(item, AIMessage) and item.content == rejected.content for item in correction
    )
    assert '"path": ["type"]' in correction[-1].content
    assert "extra_forbidden" in correction[-1].content
    assert "input" not in correction[-1].content


async def test_reason_recovery_preserves_order_and_only_regenerates_the_failed_item(tmp_path):
    invalid = decision("c1", "本篇证明LFY2调控AP1并促进大豆花器官发育。")
    fixed = decision("c0", "该文测量大豆AP1表达，只提供背景；未直接验证目标对象。")
    reranker, model, claim, subset, evidence = window(
        tmp_path, [response([invalid, decision("c0")]), response([fixed])]
    )
    draft, _ = await reranker._window(
        claim, [reranker._input(item) for item in subset.values()], subset, evidence
    )
    assert [item.work_id for item in draft.items] == list(reversed(subset))
    assert draft.items[0].reason == fixed["reason"]
    assert len(model.received_messages) == 2
    recovery = json.loads(model.received_messages[1][1].content)
    assert len(recovery["candidates"]) == 1
    assert recovery["candidates"][0]["title"] == "AP1 expression in soybean"
    assert recovery["validation_feedback"]["identifiers_absent_from_source"] == ["lfy2"]
    assert "只放在 missing_constraints" in recovery["task"]


async def test_failed_reason_recovery_never_regenerates_or_invents_the_global_order(tmp_path):
    invalid = "本篇证明LFY2调控AP1并促进大豆花器官发育。"
    reranker, model, claim, subset, evidence = window(
        tmp_path,
        [
            response([decision("c1", invalid), decision("c0")]),
            response([decision("c0", invalid)]),
            response([decision("c0", invalid)]),
        ],
    )
    with pytest.raises(RerankFailure) as raised:
        await reranker._window(
            claim, [reranker._input(item) for item in subset.values()], subset, evidence
        )
    assert raised.value.code == "MODEL_UNGROUNDED_OUTPUT"
    assert len(model.received_messages) == 3
    assert all(
        len(json.loads(batch[1].content)["candidates"]) == 1
        for batch in model.received_messages[1:]
    )


async def test_transient_window_references_are_repaired_not_shown_to_users(tmp_path):
    invalid = decision("c1", "该研究与c0相同，报告了来源对象的表达观察，可作背景。")
    fixed = decision("c0", "该文测量大豆AP1表达，属于背景，仍需核查全文。")
    reranker, model, claim, subset, evidence = window(
        tmp_path, [response([invalid, decision("c0")]), response([fixed])]
    )
    draft, _ = await reranker._window(
        claim, [reranker._input(item) for item in subset.values()], subset, evidence
    )
    assert draft.items[0].reason == fixed["reason"]
    recovery = json.loads(model.received_messages[1][1].content)
    assert recovery["validation_feedback"]["failure_code"] == "RERANK_TRANSIENT_REFERENCE"


async def test_species_recovery_gets_exact_source_gap_without_weakening_the_guard(tmp_path):
    invalid = decision("c1", "该研究在拟南芥中验证AP1表达，可以提供目标研究背景。")
    fixed = decision("c0", "该文测量大豆AP1表达，仅可提供背景，未验证目标对象。")
    reranker, model, claim, subset, evidence = window(
        tmp_path, [response([invalid, decision("c0")]), response([fixed])]
    )
    draft, _ = await reranker._window(
        claim, [reranker._input(item) for item in subset.values()], subset, evidence
    )
    assert draft.items[0].reason == fixed["reason"]
    recovery = json.loads(model.received_messages[1][1].content)
    assert recovery["validation_feedback"]["species_absent_from_source"] == ["拟南芥"]
    assert "未写明物种" in recovery["task"]


async def test_identifier_recovery_suggests_exact_source_forms_without_authorizing_aliases(
    tmp_path,
):
    from citation_tool.reasons import reason_entities_grounded

    invalid = decision("c1", "该研究测量GmAP1表达，可为FT-AP1通路提供背景。")
    fixed = decision("c0", "该文测量大豆GmAP1表达，仅提供背景，没有目标对象的功能证据。")
    reranker, model, claim, subset, evidence = window(
        tmp_path, [response([invalid, decision("c0")]), response([fixed])]
    )
    identifier = list(subset)[1]
    source = record(2, "GmAP1 expression in soybean floral meristems was measured.")
    source["title"] = "GmAP1 expression in soybean"
    library = LibraryConfig("a", "A", "", 0, simulation=False)
    evidence[identifier] = MetadataCorpusAdapter(library, [source])._candidate(source, claim, 20)
    subset[identifier] = subset[identifier].model_copy(
        update={
            "title": source["title"],
            "evidence_excerpt": source["abstract"],
        }
    )
    assert not reason_entities_grounded(invalid["reason"], evidence[identifier], claim)
    draft, _ = await reranker._window(
        claim, [reranker._input(item) for item in subset.values()], subset, evidence
    )
    assert draft.items[0].reason == fixed["reason"]
    recovery = json.loads(model.received_messages[1][1].content)
    assert recovery["validation_feedback"]["source_identifier_suggestions"] == {"ap1": ["gmap1"]}
    assert "不得省略前缀" in recovery["task"]
    assert not reason_entities_grounded(invalid["reason"], evidence[identifier], claim)


@pytest.mark.parametrize(
    "raw, normalized", [("Fe2 O3", "Fe2O3"), ("H2 O", "H2O"), ("g-C3 N4", "g-C3N4")]
)
def test_source_identifier_normalization_precedes_casefolding(raw, normalized):
    from citation_tool.reasons import (
        _source_identifiers,
        _ungrounded_identifiers,
        reason_entities_grounded,
    )
    from test_units import make_candidate

    source = make_candidate(raw_text=f"Catalyst {raw} was measured.", quote=raw)
    assert _source_identifiers(raw) == _source_identifiers(normalized)
    assert not _ungrounded_identifiers(f"该研究测量{normalized}催化材料，仍需核查全文。", source)
    assert reason_entities_grounded(f"该研究测量{normalized}催化材料，仍需核查全文。", source)
    assert not reason_entities_grounded("该研究证实Xx9具有催化作用。", source)


async def test_failed_schema_reports_field_paths_without_raw_model_content(tmp_path):
    invalid = response([decision("c1"), decision("c0")], type="not allowed")
    reranker, _, claim, subset, evidence = window(tmp_path, [invalid, invalid])
    with pytest.raises(RerankFailure) as raised:
        await reranker._window(
            claim, [reranker._input(item) for item in subset.values()], subset, evidence
        )
    assert raised.value.code == "MODEL_INVALID_OUTPUT"
    assert raised.value.details == {
        "schema_errors": [{"path": ["type"], "type": "extra_forbidden"}]
    }
