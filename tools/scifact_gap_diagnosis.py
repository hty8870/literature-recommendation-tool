"""Summarize the frozen SciFact scores and observed evidence paths without rescoring."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

ARMS = ("default", "workspace", "kimi_code")


def unique(rows, key):
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate diagnosis key: {key}")
    return result


def delivery_state(*, complete, partial):
    if complete:
        return "complete_rationale_observed"
    return "partial_text_observed" if partial else "not_observed"


def disposition(assessment, paper, target):
    if not assessment["valid_completion"]:
        return "technical_failure"
    if not assessment["format_valid"]:
        return "format_failure"
    if paper and paper["stance_correct"] and paper["exact_quote"]:
        return "covered"
    if paper and paper["stance_correct"] is False:
        return "abstained" if paper["stance"] == "INSUFFICIENT" else "opposite_direction"
    if paper:
        return "non_verbatim"
    if target is None:
        return "not_selected"
    return {
        "seen_not_selected": "text_observed_not_selected",
        "submitted_not_read": "submitted_text_not_observed",
        "retrieved_not_submitted": "retrieved_not_submitted",
        "not_observed": "retrieval_not_observed",
    }[target["failure"]]


def operation_row(row, paper, target=None, kimi_target=None):
    if target is not None:
        delivery = delivery_state(
            complete=target["complete_rationale_delivered"],
            partial=target["any_abstract_delivered"],
        )
    elif kimi_target is not None:
        delivery = delivery_state(
            complete=kimi_target["observed_full_set"],
            partial=bool(kimi_target["observed_sentence_ids"]),
        )
    else:
        raise ValueError("target has no readback record")
    return {
        "status": disposition(row["assessment"], paper, target),
        "evidence_delivery": delivery,
        "observed_product_stages": target["observed"] if target is not None else None,
        "answer_stance": paper["stance"] if paper else None,
        "verbatim_in_source": paper["exact_quote"] if paper else None,
        "answer_rank": paper["rank"] if paper else None,
        "technical_error": (row.get("error") or {}).get("code"),
    }


def diagnose(assessment, kimi_readback, *, allow_partial=False):
    missing = assessment["missing"]
    if missing and not allow_partial:
        raise ValueError("frozen comparison still incomplete; use --partial only for diagnosis")
    arms = {name: unique(assessment["records"][name], "run_case_id") for name in ARMS}
    common = set.intersection(*(set(rows) for rows in arms.values()))
    if not common or (not missing and any(set(rows) != common for rows in arms.values())):
        raise ValueError("no aligned frozen comparison")
    readbacks = unique(kimi_readback["rows"], "case")
    outcomes = {name: Counter() for name in ARMS}
    deliveries = {name: Counter() for name in ARMS}
    deltas = {name: Counter() for name in ARMS[:-1]}
    cases = []
    for case in sorted(common):
        rows = {name: arms[name][case] for name in ARMS}
        entries = {
            name: {p["doc_id"]: p for p in row["assessment"]["papers"] if not p["duplicate"]}
            for name, row in rows.items()
        }
        gold = readbacks[case]["gold"]
        targets = {name: unique(rows[name]["target_readback"], "doc_id") for name in ARMS[:-1]}
        if any(set(values) != set(gold) for values in targets.values()):
            raise ValueError(f"frozen target IDs disagree in {case}")
        documents = []
        for doc_id, kimi_target in sorted(gold.items()):
            per_arm = {
                name: operation_row(
                    rows[name],
                    entries[name].get(doc_id),
                    targets.get(name, {}).get(doc_id),
                    kimi_target if name == "kimi_code" else None,
                )
                for name in ARMS
            }
            for name in ARMS:
                item = per_arm[name]
                outcomes[name][item["status"]] += 1
                deliveries[name][item["evidence_delivery"]] += 1
                if name != "kimi_code":
                    ours = item["status"] == "covered"
                    theirs = per_arm["kimi_code"]["status"] == "covered"
                    if ours != theirs:
                        key = "product_only" if ours else "kimi_only:" + item["status"]
                        deltas[name][key] += 1
            documents.append({"doc_id": doc_id, "arms": per_arm})
        cases.append(
            {
                "case": case,
                "slice": rows["default"]["slice"],
                "family": rows["default"]["family"],
                "targets": documents,
            }
        )
    return {
        "complete": not missing,
        "paired_cases": len(common),
        "labeled_cases": sum(bool(case["targets"]) for case in cases),
        "gold_document_targets": sum(len(case["targets"]) for case in cases),
        "missing": missing,
        "outcomes": outcomes,
        "evidence_delivery": deliveries,
        "disagreement_with_kimi": deltas,
        "boundaries": (
            "Counts are target-level diagnostics, not macro scores. An official direction "
            "mismatch can be a defensible abstention. Product and Kimi readback are observed "
            "with different instruments; absence of a receipt is not proof of no reading."
        ),
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assessment", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--partial", action="store_true")
    args = parser.parse_args()
    scored = args.assessment.read_bytes()
    readback = args.assessment.with_name("kimi-readback.json").read_bytes()
    report = diagnose(json.loads(scored), json.loads(readback), allow_partial=args.partial)
    report["inputs"] = {
        "assessment_sha256": hashlib.sha256(scored).hexdigest(),
        "kimi_readback_sha256": hashlib.sha256(readback).hexdigest(),
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    main()
