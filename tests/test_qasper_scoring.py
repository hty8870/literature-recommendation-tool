import json

from tools.qasper_scoring import parse_answer, score


def answer(*pairs, text="correct value"):
    return json.dumps(
        {
            "status": "answered",
            "answer": text,
            "evidence": [{"passage_id": pid, "quote": quote} for pid, quote in pairs],
        }
    )


def test_wrong_locator_whitespace_and_later_duplicate_cannot_earn_evidence():
    source = "The correct value is four."
    result = score(
        answer(("P0001", "   "), ("P0001", source), ("P0009", source)),
        stratum="extractive",
        passages={"P0001": source},
        references=[{"answer": "correct value", "paragraph_ids": ["P0001"]}],
    )
    assert result["format_valid"]
    assert result["reference_token_f1"] == 1
    assert result["evidence_paragraph_f1"] == 0
    assert result["scientific_pass"] is None
    assert [r["error"] for r in result["evidence_items"]] == [
        "quote_too_short",
        "duplicate",
        "unknown_passage",
    ]


def test_evidence_and_answer_must_use_same_reference_for_joint_diagnostic():
    result = score(
        answer(("P0002", "This is paragraph two."), text="first answer"),
        stratum="abstractive",
        passages={"P0002": "This is paragraph two."},
        references=[
            {"answer": "first answer", "paragraph_ids": ["P0001"]},
            {"answer": "second answer", "paragraph_ids": ["P0002"]},
        ],
    )
    assert result["reference_token_f1"] == result["evidence_paragraph_f1"] == 1
    assert result["same_reference_min_f1"] == 0.5


def test_disputed_answerability_stays_undetermined():
    none = '{"status":"insufficient","answer":"","evidence":[]}'
    assert score(none, stratum="mixed", passages={}, references=[])["answerability_match"] is None
    assert (
        score(none, stratum="unanswerable", passages={}, references=[])["answerability_match"]
        is True
    )
    assert (
        parse_answer(none.replace('"insufficient",', '"insufficient","status":"answered",')) is None
    )
