"""Audit observed parent feedback and terminal citation visibility without writing results."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from tools.gap_benchmark.data import RUNTIME, load_data
from tools.gap_benchmark.metrics import score_record
from tools.gap_benchmark.opencode_assessment import _read
from tools.gap_benchmark.opencode_baseline import MODELS
from tools.gap_benchmark.opencode_parallel import observed_transport_overlap
from tools.gap_benchmark.opencode_parent_pair import restore_receipt_abstracts, target_exposure

from citation_tool.jcs import sha256_json


def _receipts(turn: dict) -> Iterator[dict]:
    for tool in turn["tool_results"]:
        receipt = tool["content"]
        if (
            tool["name"] == "search_literature"
            and isinstance(receipt, dict)
            and receipt.get("simulation") is False
            and isinstance(receipt.get("query_id"), str)
            and isinstance(receipt.get("candidate_diagnostics"), list)
        ):
            yield receipt


def _requested_searches(turn: dict) -> int:
    return sum(call["name"] == "search_literature" for call in turn["tool_calls"])


def followups(trace: list[dict], exposure: dict) -> dict:
    first_full = None
    labeled = exposure["labeled_targets"]
    if labeled > 0 and exposure["exact_abstracts_observed"] == labeled:
        first_full = max(row["first_observed_request"] for row in exposure["targets"])
    seen: set[str] = set()
    feedback_responses = []
    for ordinal, turn in enumerate(trace, 1):
        updates: Counter[str] = Counter()
        for receipt in _receipts(turn):
            delta = receipt.get("evidence_delta")
            if isinstance(delta, dict) and receipt["query_id"] not in seen:
                seen.add(receipt["query_id"])
                updates[delta["status"]] += 1
        if updates:
            feedback_responses.append(
                {
                    "parent_request": ordinal,
                    "first_feedback_receipts_by_status": dict(updates),
                    "search_requests_in_response": _requested_searches(turn),
                    "finish": turn["finish"],
                }
            )
    return {
        "first_parent_request_with_all_exact_target_abstracts": first_full,
        "search_requests_since_all_targets_visible": sum(
            _requested_searches(turn) for turn in trace[first_full - 1 :]
        )
        if first_full is not None
        else None,
        "responses_to_first_feedback": feedback_responses,
    }


def terminal_quotes(trace: list[dict], response_text: str, assessment: dict) -> dict:
    if not assessment["valid_completion"] or not assessment["format_valid"]:
        return {"status": "no_legal_answer"}
    if (
        not trace
        or trace[-1]["tool_calls"]
        or trace[-1]["finish"] not in {"end_turn", "stop"}
        or trace[-1]["visible_output"].strip() != response_text.strip()
    ):
        return {"status": "terminal_request_not_matched"}
    originals: dict[tuple[str, str], str] = {}
    shown: dict[str, set[str]] = {}
    for receipt in _receipts(trace[-1]):
        restored, _ = restore_receipt_abstracts(receipt, originals)
        for item in restored["candidate_diagnostics"]:
            spans = [
                item.get("evidence_excerpt"),
                *(s.get("abstract") for s in item.get("source_records", [])),
            ]
            shown.setdefault(item["work_id"], set()).update(s for s in spans if isinstance(s, str))
    missing = [
        paper["doc_id"]
        for paper in assessment["papers"]
        if not any(
            paper["quote"] in text for text in shown.get(f"work:scifact:{paper['doc_id']}", ())
        )
    ]
    return {"status": "checked", "citations": len(assessment["papers"]), "missing_doc_ids": missing}


def _arm(
    root: Path, case: dict, plan: dict, arm: str, corpus: dict, abstracts: dict
) -> tuple[dict, dict]:
    case_id = case["case_id"]
    path = root / arm / "product" / f"{case_id}.json"
    if not path.is_file():
        raise ValueError(f"{case_id}/{arm}: product record not sealed")
    row = _read(path)
    if (
        row["case_id"] != case_id
        or row["arm"] != arm
        or row["expected_model"] != plan["model"]
        or row["implementation_hash"] != sha256_json(plan)
        or row["gold_sent_to_model"] is not False
    ):
        raise ValueError(f"{case_id}/{arm}: record does not match experiment plan")
    trace_path = (root / arm / row["parent_trace_path"]).resolve()
    if not trace_path.is_relative_to((root / arm).resolve()):
        raise ValueError("parent trace outside experiment arm")
    trace = _read(trace_path)
    observed = target_exposure(trace, set(case["expect"]["evidence"]), abstracts)
    if observed != row["evidence_exposure"]:
        raise ValueError(f"{case_id}/{arm}: stored evidence exposure differs from actual trace")
    assessed = score_record(row, case, corpus)["assessment"]
    return row, {
        "target_exposure": observed,
        **followups(trace, observed),
        "terminal_quotes": terminal_quotes(trace, row["response_text"], assessed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", required=True, choices=MODELS)
    parser.add_argument("--cases", nargs="+", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", args.name):
        parser.error("invalid run name")
    root = RUNTIME / "opencode-parent-pairs" / args.name / args.model
    plan = _read(root / "plan.json")
    arms = ("control", "trial")
    if plan["model"] != args.model or any(
        [case, arm] not in plan["order"] for case in args.cases for arm in arms
    ):
        parser.error("cases or model outside experiment plan")
    suite, corpus = load_data()
    cases = {row["case_id"]: row for row in suite["tasks"]}
    abstracts = {row["work_id"]: row["abstract"] for row in corpus["records"]}
    for case in args.cases:
        try:
            control, trial = (_arm(root, cases[case], plan, arm, corpus, abstracts) for arm in arms)
        except ValueError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "case": case,
                    "transport_overlap_seconds": observed_transport_overlap([control[0], trial[0]]),
                    "control": control[1],
                    "trial": trial[1],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
