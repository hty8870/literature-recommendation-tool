import json
from copy import deepcopy

import pytest
from tools.gap_benchmark.opencode_receipt_replay import (
    JSON_GUIDANCE,
    actual_observations,
    messages,
    same_abstract_refs,
)


def receipt(query, text="Actual sentence.  Exact spacing.", *, publication_status="unknown"):
    return {
        "name": "search_literature",
        "tool_call_id": f"call_{query}",
        "content": {
            "query_id": query,
            "simulation": False,
            "results": [],
            "candidate_diagnostics": [
                {
                    "work_id": "work:scifact:1",
                    "evidence_excerpt": text,
                    "llm_rank": 2,
                    "source_records": [
                        {"abstract": text, "publication_status": publication_status},
                    ],
                }
            ],
        },
    }


def test_format_contrast_does_not_change_any_evidence_or_user_requirement():
    turn = {
        "system": ["Actual instructions"],
        "user": ["Actual question"],
        "tool_results": [receipt("first")],
        "visible_output": "Not evidence and not replayed",
    }
    control, _ = messages(turn, "format", "control")
    trial, _ = messages(turn, "format", "trial")
    assert control[1] == trial[1]
    assert trial[0].text == f"{control[0].text}\n\n{JSON_GUIDANCE}"
    assert turn["visible_output"] not in trial[1].text


def test_only_same_work_exact_abstracts_are_referenced_and_status_is_preserved():
    receipts = [
        receipt("one"),
        receipt("two", publication_status="retracted"),
        receipt("three", "Actual sentence. Exact spacing."),
        receipt("four"),
    ]
    receipts[3]["content"]["candidate_diagnostics"][0]["work_id"] = "work:scifact:2"
    originals = deepcopy(receipts)
    compressed, references = same_abstract_refs(receipts)
    assert receipts == originals and references == 5
    candidates = [r["content"]["candidate_diagnostics"][0] for r in compressed]
    first, second, third, other_work = candidates
    ref = {"query_id": "one", "work_id": first["work_id"], "field": "evidence_excerpt"}
    assert second["abstract_ref"] == ref and "evidence_excerpt" not in second
    assert second["llm_rank"] == 2
    assert second["source_records"] == [{"abstract_ref": ref, "publication_status": "retracted"}]
    assert third["evidence_excerpt"] == "Actual sentence. Exact spacing."
    assert other_work["evidence_excerpt"] == first["evidence_excerpt"]
    for original, shortened in zip(receipts, compressed, strict=True):
        restored = deepcopy(shortened)
        item = restored["content"]["candidate_diagnostics"][0]
        for field, row in [
            ("evidence_excerpt", item),
            *(("abstract", s) for s in item["source_records"]),
        ]:
            ref = row.pop("abstract_ref", None)
            if ref:
                source = next(r for r in compressed if r["content"]["query_id"] == ref["query_id"])
                candidate = next(
                    i
                    for i in source["content"]["candidate_diagnostics"]
                    if i["work_id"] == ref["work_id"]
                )
                row[field] = candidate[ref["field"]]
        assert restored == original


def test_only_actual_verified_abstracts_can_be_used_as_replay_input():
    tool = receipt("one")
    turn = {"system": [], "user": [], "tool_results": [tool]}
    assert actual_observations([turn], {"work:scifact:1": "Actual sentence.  Exact spacing."}) == (
        turn,
        {"1"},
    )
    control, _ = messages(turn, "abstract-references", "control")
    assert json.loads(control[1].text.split("\n\n")[-1]) == [tool]
    with pytest.raises(ValueError, match="differs from frozen"):
        actual_observations([turn], {"work:scifact:1": "Edited."})
    tool["content"]["results"] = ["not public abstract evidence"]
    with pytest.raises(ValueError, match="citable results"):
        actual_observations([turn], {"work:scifact:1": "Actual sentence.  Exact spacing."})
