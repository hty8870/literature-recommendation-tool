from tools.gap_benchmark.opencode_followup_audit import followups, terminal_quotes


def _receipt(query, candidates=(), status=None):
    body = {"simulation": False, "query_id": query, "candidate_diagnostics": list(candidates)}
    if status is not None:
        body["evidence_delta"] = {"status": status}
    return {"name": "search_literature", "content": body}


def _turn(*receipts, searches=0, output=""):
    return {
        "tool_results": list(receipts),
        "tool_calls": [{"name": "search_literature"}] * searches,
        "visible_output": output,
        "finish": "tool_use" if searches else "end_turn",
    }


def test_followups_count_first_observed_feedback_but_all_subsequent_requests():
    duplicate = _receipt("q2", status="no_unseen_evidence")
    absent = _receipt("q3", status="no_candidates_returned")
    trace = [_turn(searches=1), _turn(duplicate, searches=2), _turn(duplicate, absent)]
    exposure = {
        "labeled_targets": 2,
        "exact_abstracts_observed": 2,
        "targets": [{"first_observed_request": 2}, {"first_observed_request": 3}],
    }
    assert followups(trace, exposure) == {
        "first_parent_request_with_all_exact_target_abstracts": 3,
        "search_requests_since_all_targets_visible": 0,
        "responses_to_first_feedback": [
            {
                "parent_request": 2,
                "first_feedback_receipts_by_status": {"no_unseen_evidence": 1},
                "search_requests_in_response": 2,
                "finish": "tool_use",
            },
            {
                "parent_request": 3,
                "first_feedback_receipts_by_status": {"no_candidates_returned": 1},
                "search_requests_in_response": 0,
                "finish": "end_turn",
            },
        ],
    }
    exposure["exact_abstracts_observed"] = 1
    exposure["targets"] = exposure["targets"][:1]
    assert followups(trace, exposure)["search_requests_since_all_targets_visible"] is None


def test_terminal_quotes_bind_same_work_and_current_request():
    work1 = {"work_id": "work:scifact:1", "evidence_excerpt": "Exact earlier text."}
    work2 = {"work_id": "work:scifact:2", "evidence_excerpt": "Exact earlier text."}
    work3 = {"work_id": "work:scifact:3", "evidence_excerpt": "Another exact sentence."}
    reference = {"query_id": "q3", "work_id": work3["work_id"], "field": "evidence_excerpt"}
    trace = [
        _turn(_receipt("q1", [work1]), searches=1),
        _turn(
            _receipt("q2", [work2]),
            _receipt("q3", [work3]),
            _receipt("q4", [{"work_id": work3["work_id"], "abstract_ref": reference}]),
            output="Final answer",
        ),
    ]
    assessed = {
        "valid_completion": True,
        "format_valid": True,
        "papers": [
            {"doc_id": "1", "quote": "Exact earlier text."},
            {"doc_id": "3", "quote": "Another exact sentence."},
        ],
    }
    assert terminal_quotes(trace, "Final answer", assessed) == {
        "status": "checked",
        "citations": 2,
        "missing_doc_ids": ["1"],
    }
    assert terminal_quotes(trace, "Different", assessed)["status"] == "terminal_request_not_matched"
    trace[-1]["finish"] = "max_tokens"
    assert (
        terminal_quotes(trace, "Final answer", assessed)["status"] == "terminal_request_not_matched"
    )
    assessed["valid_completion"] = False
    assert terminal_quotes(trace, "Final answer", assessed) == {"status": "no_legal_answer"}
