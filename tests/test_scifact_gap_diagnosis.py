import pytest
from tools.scifact_gap_diagnosis import diagnose, disposition


def paper(stance, *, correct, exact=True):
    return {
        "doc_id": "209",
        "stance": stance,
        "stance_correct": correct,
        "exact_quote": exact,
        "duplicate": False,
        "rank": 1,
    }


def row(final, *, complete=True, target=True):
    record = {
        "run_case_id": "sf-probe",
        "slice": "contradict",
        "family": "fixture",
        "assessment": {"valid_completion": complete, "format_valid": True, "papers": [final]},
    }
    if target:
        record["target_readback"] = [
            {
                "doc_id": "209",
                "complete_rationale_delivered": True,
                "any_abstract_delivered": True,
                "observed": {"source_submission": True, "retrieval": True},
            }
        ]
    return record


def test_diagnosis_separates_abstention_from_reversed_direction_and_missing_evidence():
    scored = {
        "missing": [],
        "records": {
            "default": [row(paper("INSUFFICIENT", correct=False))],
            "workspace": [row(paper("SUPPORT", correct=False))],
            "kimi_code": [row(paper("CONTRADICT", correct=True), target=False)],
        },
    }
    readback = {
        "rows": [
            {
                "case": "sf-probe",
                "gold": {"209": {"observed_full_set": False, "observed_sentence_ids": []}},
            }
        ]
    }
    stats = diagnose(scored, readback)
    assert stats["outcomes"] == {
        "default": {"abstained": 1},
        "workspace": {"opposite_direction": 1},
        "kimi_code": {"covered": 1},
    }
    assert stats["evidence_delivery"]["kimi_code"] == {"not_observed": 1}
    assert stats["disagreement_with_kimi"]["default"] == {"kimi_only:abstained": 1}
    scored["missing"] = [["workspace", "sf-next"]]
    with pytest.raises(ValueError, match="incomplete"):
        diagnose(scored, readback)
    assert not diagnose(scored, readback, allow_partial=True)["complete"]


def test_technical_failure_overrides_a_valid_visible_answer_without_changing_judgment():
    final = paper("CONTRADICT", correct=True)
    incomplete = row(final, complete=False)["assessment"]
    assert disposition(incomplete, final, None) == "technical_failure"
    assert disposition(row(final)["assessment"], final | {"exact_quote": False}, None) == (
        "non_verbatim"
    )
