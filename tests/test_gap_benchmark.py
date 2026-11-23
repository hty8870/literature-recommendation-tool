from __future__ import annotations

import copy
import json

import pytest
from tools.gap_benchmark.data import normalized_corpus
from tools.gap_benchmark.evaluation import PairAudit, audit_payload, validate_audit
from tools.gap_benchmark.metrics import (
    paired_summary,
    parse_answer,
    retrieval_metrics,
    score_record,
)
from tools.gap_scientific_rescore import score_scientifically


@pytest.fixture
def corpus():
    return normalized_corpus(
        [
            {
                "doc_id": 7,
                "title": "Example scientific title",
                "abstract": [
                    "Treatment reduced the endpoint.",
                    "No long-term follow-up was performed.",
                ],
            },
            {
                "doc_id": 9,
                "title": "Example adjacent study",
                "abstract": ["A related biomarker changed."],
            },
        ]
    )


@pytest.fixture
def task():
    return {
        "case_id": "sf-1",
        "run_case_id": "sf-1",
        "slice": "contradict",
        "family": "one",
        "claim": "Treatment increased the endpoint.",
        "expect": {"evidence": {"7": [{"label": "CONTRADICT", "sentences": [0]}]}},
    }


def record(text, system="product"):
    return {
        "case_id": "sf-1",
        "run_case_id": "sf-1",
        "system": system,
        "completed": True,
        "model_valid": True,
        "secret_leak": False,
        "response_text": text,
        "wall_time_ms": 1000,
        "queries": [],
    }


def answer(doc_id="7", stance="CONTRADICT", quote="Treatment reduced the endpoint."):
    import json

    return json.dumps(
        {
            "papers": [
                {
                    "doc_id": doc_id,
                    "stance": stance,
                    "quote": quote,
                    "reason": "摘要反驳待核查的方向。",
                }
            ],
            "conclusion": "仅摘要级判断。",
        }
    )


def test_real_corpus_adapter_does_not_fabricate_bibliography(corpus):
    row = corpus["records"][0]
    assert row["doi"] is None and row["year"] is None and row["authors"] is None
    assert row["work_id"] == "work:scifact:7"
    assert (
        row["abstract"] == "Treatment reduced the endpoint. No long-term follow-up was performed."
    )


def test_json_parser_only_accepts_actual_final_object():
    parsed, errors = parse_answer("```json\n" + answer("work:scifact:7") + "\n```")
    assert not errors and parsed.papers[0].doc_id == "7"
    assert parse_answer("I found it: " + answer())[0] is None
    assert parse_answer(answer("not-an-id"))[0] is None


def test_retrieval_deduplicates_and_never_scores_nei_as_zero():
    assert retrieval_metrics(["7", "7"], {"7": [{}], "9": [{}]}, 5)["recall"] == 0.5
    assert retrieval_metrics(["7"], {}, 5)["recall"] is None
    assert retrieval_metrics(["7"], {}, 5)["ndcg"] is None


def test_title_hit_does_not_hide_wrong_direction_or_fabricated_quote(corpus, task):
    correct = score_record(record(answer()), task, corpus)["assessment"]
    wrong = score_record(record(answer(stance="SUPPORT")), task, corpus)["assessment"]
    invented = score_record(
        record(answer(quote="Treatment increased the endpoint.")), task, corpus
    )["assessment"]
    assert correct["retrieval_at_5"]["recall"] == 1
    assert correct["correct_direction_evidence_coverage"] == 1
    assert correct["complete_rationale_coverage"] == 1
    assert wrong["retrieval_at_5"]["recall"] == 1
    assert wrong["correct_direction_evidence_coverage"] == 0
    assert wrong["wrong_official_stances"] == 1
    assert invented["correct_direction_evidence_coverage"] == 0
    assert invented["invalid_quotes"] == 1


def test_card_hits_never_get_substituted_for_final_papers(corpus, task):
    raw = record('{"papers":[],"conclusion":"没有证据"}')
    raw["queries"] = [
        {
            "workers": [{"source_available_work_ids": ["work:scifact:7"]}],
            "candidate_diagnostics": [{"work_id": "work:scifact:7", "base_rank": 1, "llm_rank": 1}],
        }
    ]
    assessed = score_record(raw, task, corpus)["assessment"]
    assert assessed["retrieval_at_5"]["recall"] == 0
    assert assessed["product_stages"]["available_recall_union"] == 1
    assert assessed["product_stages"]["best_single_query_llm_recall_at_10"] == 1


def test_unannotated_paper_requires_review_not_automatic_hallucination(corpus, task):
    assessment = score_record(
        record(answer("9", "INSUFFICIENT", "A related biomarker changed.")), task, corpus
    )["assessment"]
    assert assessment["wrong_official_stances"] == 0
    assert assessment["invalid_quotes"] == 0
    assert assessment["unjudged_requires_review"] == ["9"]


def test_partial_failed_run_is_not_a_success(corpus, task):
    raw = record(answer()) | {"completed": False}
    assessed = score_record(raw, task, corpus)["assessment"]
    assert not assessed["valid_completion"]
    assert assessed["correct_direction_evidence_coverage"] == 0
    assert assessed["retrieval_at_5"]["recall"] == 0
    assert assessed["partial_output_recall_at_10"] == 1


def test_cluster_pairing_does_not_make_one_family_two_independent_wins(corpus, task):
    records = []
    for case in ("sf-1", "sf-2"):
        for system, text in (
            ("product", answer()),
            ("kimi_code", '{"papers":[],"conclusion":"未知"}'),
        ):
            row = score_record(record(text, system), task, corpus)
            row["run_case_id"] = case
            records.append(row)
    summary = paired_summary(records)
    assert summary["paired"]["positive_families"] == 1
    assert summary["paired"]["case_mean_difference"] == 1
    assert not summary["paired"]["preregistered_practical_advantage_observed"]
    assert paired_summary(records) == summary
    with pytest.raises(ValueError, match="both systems"):
        paired_summary(records[:-1])


def test_semantic_audit_requires_complete_same_side_verbatim_evidence(corpus, task):
    rows = {
        system: score_record(record(answer(), system), task, corpus)
        for system in ("product", "kimi_code")
    }
    payload, _ = audit_payload(task, rows, corpus)
    audit = PairAudit.model_validate(
        {
            "papers": [
                {
                    "side": side,
                    "index": 0,
                    "actual_stance": "CONTRADICT",
                    "issue": "none",
                    "output_evidence": "",
                    "source_evidence": "Treatment reduced the endpoint.",
                    "explanation": "方向有据",
                }
                for side in ("A", "B")
            ],
            "summary": "一致",
        }
    )
    validate_audit(audit, payload)
    wrong = copy.deepcopy(audit)
    wrong.papers.pop()
    with pytest.raises(ValueError, match="every actual output"):
        validate_audit(wrong, payload)
    wrong = copy.deepcopy(audit)
    wrong.papers[0].source_evidence = "made up source"
    with pytest.raises(ValueError, match="not verbatim"):
        validate_audit(wrong, payload)
    wrong = copy.deepcopy(audit)
    wrong.papers[0].issue = "scope_overclaim"
    with pytest.raises(ValueError, match="same-side output evidence"):
        validate_audit(wrong, payload)


def test_scientific_rescore_preserves_overflow_contract_and_original_answer(corpus, task):
    payload = json.loads(answer())
    background = json.loads(answer("9", "INSUFFICIENT", "A related biomarker changed."))["papers"][
        0
    ]
    payload["papers"].extend(copy.deepcopy(background) for _ in range(10))
    text = json.dumps(payload)
    assessed = score_scientifically(record(text), task, corpus)
    assert assessed["response_text"] == text
    assert assessed["original_strict_assessment"]["correct_direction_evidence_coverage"] == 0
    assert assessed["assessment"]["correct_direction_evidence_coverage"] == 1
    assert not assessed["assessment"]["format_valid"]
    assert assessed["assessment"]["scientific_parsed"]
    assert len(assessed["assessment"]["papers"]) == 11
    assert assessed["assessment"]["duplicate_ids"] == 9
    assert assessed["scoring_projection"]["agent_calls_repeated"] is False


def test_scientific_rescore_cannot_gain_recall_from_an_eleventh_paper(corpus, task):
    payload = json.loads(answer())
    background = json.loads(answer("9", "INSUFFICIENT", "A related biomarker changed."))["papers"][
        0
    ]
    payload["papers"] = [copy.deepcopy(background) for _ in range(10)] + payload["papers"]
    assessed = score_scientifically(record(json.dumps(payload)), task, corpus)["assessment"]
    assert assessed["retrieval_at_10"]["recall"] == 0
    assert assessed["correct_direction_evidence_coverage"] == 0
    assert assessed["papers"][-1]["official_evidence"]


def test_scientific_rescore_separates_conservative_abstention_from_reversed_direction(corpus, task):
    uncertain = score_scientifically(record(answer(stance="INSUFFICIENT")), task, corpus)[
        "assessment"
    ]
    reversed_direction = score_scientifically(record(answer(stance="SUPPORT")), task, corpus)[
        "assessment"
    ]
    assert uncertain["official_evidence_marked_insufficient"] == 1
    assert uncertain["opposite_official_stances"] == 0
    assert reversed_direction["opposite_official_stances"] == 1
    assert reversed_direction["official_evidence_marked_insufficient"] == 0
