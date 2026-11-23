"""Score OpenCode SciFact retests with the unchanged evaluator and trace first losses."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from tools.benchmark.core import elapsed_ms
from tools.gap_benchmark.data import save_new
from tools.gap_benchmark.metrics import aggregate, mean, paired_summary, parse_answer, score_record
from tools.gap_benchmark.opencode_baseline import MODELS, prepare
from tools.luna_gap_retest import first_loss_by_target

from citation_tool.jcs import sha256_json

LOSS_ORDER = (
    "invalid_run",
    "invalid_format",
    "omitted",
    "insufficient",
    "opposite",
    "inexact_quote",
    "covered",
)


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def product_event_trace(root: Path, record: dict) -> list[dict]:
    database = record.get("db_path")
    if not database or not (root / database).is_file():
        return []
    path = (root / database).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("trace database outside the run")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        events = conn.execute(
            """SELECT query_id, event_type, occurred_at, payload_json FROM events
               WHERE event_type IN ('candidate_pool_updated', 'candidate_ranking_progress',
                   'candidate_reranked', 'worker_completed', 'worker_failed', 'search_finalized')
               ORDER BY query_id, sequence"""
        ).fetchall()
    stages: dict[str, dict] = {}
    for query, event, time, body in events:
        stage = stages.setdefault(query, {"query_id": query, "first_work_seen_s": {}})
        seconds = round(elapsed_ms(record["started_at"], time) / 1000, 3)
        if event == "candidate_pool_updated":
            for item in json.loads(body).get("candidates", []):
                stage["first_work_seen_s"].setdefault(item["work_id"], seconds)
        if event == "candidate_ranking_progress":
            stage.setdefault("first_" + json.loads(body)["phase"] + "_s", seconds)
        stage.setdefault("first_" + event + "_s", seconds)
        stage["last_" + event + "_s"] = seconds
    return sorted(
        stages.values(),
        key=lambda item: min(
            item.get("first_candidate_pool_updated_s", float("inf")),
            item.get("first_worker_completed_s", float("inf")),
            item.get("first_search_finalized_s", float("inf")),
        ),
    )


def _candidate_entries(turn: dict) -> list[dict]:
    return [
        entry
        for tool in turn.get("tool_results", [])
        if isinstance(receipt := tool.get("content"), dict)
        for entry in receipt.get("candidate_diagnostics", [])
    ]


def candidate_receipts(turn: dict, doc: str, source: dict) -> list[dict]:
    observed = []
    for tool in turn.get("tool_results", []):
        receipt = tool.get("content")
        if not isinstance(receipt, dict):
            continue
        for entry in receipt.get("candidate_diagnostics", []):
            if entry.get("work_id") != f"work:scifact:{doc}":
                continue
            origin = entry.get("evidence_excerpt") or ""
            original_abstracts = [
                origin,
                *(field.get("abstract") for field in entry.get("source_records", [])),
            ]
            advice = entry.get("reason") or {}
            observed.append(
                {
                    "query_id": receipt.get("query_id"),
                    "abstract_exact_and_complete": source["abstract"] in original_abstracts,
                    "relationship": entry.get("relationship"),
                    "eligibility_rejections": entry.get("rejection_reasons", []),
                    "scope_match": entry.get("scope_match"),
                    "llm_rank": entry.get("llm_rank"),
                    "base_rank": entry.get("base_rank"),
                    "reason_status": advice.get("status"),
                    "reason": advice.get("text"),
                    "reason_quote": entry.get("reason_evidence_quote"),
                }
            )
    return observed


def family_outcomes(rows: list[dict]) -> dict:
    weights = Counter(row["case_id"] for row in rows)
    if not weights:
        return {"positive_cases": 0, "counts": {}, "weighted_shares": {}}
    return {
        "positive_cases": len(weights),
        "counts": dict(Counter(row["outcome"] for row in rows)),
        "weighted_shares": {
            label: sum(1 / weights[row["case_id"]] for row in rows if row["outcome"] == label)
            / len(weights)
            for label in LOSS_ORDER
        },
    }


def extra_fields_only(text: str) -> tuple[str, list[list]] | None:
    _, errors = parse_answer(text)
    if not errors or any(row["type"] != "extra_forbidden" for row in errors):
        return None
    content = text.strip()
    if content.startswith("```json\n") and content.endswith("\n```"):
        content = content[8:-4].strip()
    elif content.startswith("```\n") and content.endswith("\n```"):
        content = content[4:-4].strip()
    document = json.loads(content)
    paths = [row["path"] for row in errors]
    for path in paths:
        if len(path) == 1:
            document.pop(path[0])
        elif len(path) == 3 and path[0] == "papers":
            document["papers"][path[1]].pop(path[2])
        else:
            return None
    filtered = json.dumps(document, ensure_ascii=False)
    return (filtered, paths) if parse_answer(filtered)[0] is not None else None


def _outcome(papers: list[dict], assessment: dict) -> str:
    if not assessment["valid_completion"]:
        return "invalid_run"
    if not assessment["format_valid"]:
        return "invalid_format"
    if not papers:
        return "omitted"
    if any(p["stance_correct"] and p["exact_quote"] for p in papers):
        return "covered"
    if any(p["stance_correct"] for p in papers):
        return "inexact_quote"
    if any(p["stance"] == "INSUFFICIENT" for p in papers):
        return "insufficient"
    return "opposite"


def traced_targets(root: Path, record: dict, task: dict, sources: dict) -> list[dict]:
    assess = record["assessment"]
    product = record["system"] == "product"
    trace = _read(root / record["parent_trace_path"]) if record.get("parent_trace_path") else []
    last_turn = trace[-1] if trace else {}
    terminal_text = (record.get("response_text") or "").strip()
    final_turn = next(
        (
            turn
            for turn in reversed(trace)
            if terminal_text
            and not turn["tool_calls"]
            and turn.get("visible_output", "").strip() == terminal_text
        ),
        last_turn,
    )
    stage_rows = {r["doc_id"]: r for r in first_loss_by_target(record, task)} if product else {}
    previous = []
    for turn in trace:
        if turn["tool_calls"]:
            continue
        parsed, _ = parse_answer(turn["visible_output"])
        if parsed is not None:
            previous.append({paper.doc_id: paper.stance for paper in parsed.papers})
    rows = []
    for doc, labels in task["expect"]["evidence"].items():
        source = sources[doc]
        papers = [row for row in assess["papers"] if doc == row["doc_id"]]
        statements = [
            source["abstract_sentences"][idx]
            for idx in sorted({index for group in labels for index in group["sentences"]})
        ]
        if product:
            history = {
                sha256_json(receipt): receipt
                for turn in trace
                for receipt in candidate_receipts(turn, doc, source)
            }
            details = {
                "stage_union": stage_rows[doc]["observed"],
                "parent_receipts": candidate_receipts(final_turn, doc, source),
                "parent_receipts_ever": list(history.values()),
                "parent_observed_count_last_turn": len(_candidate_entries(final_turn)),
                "visible_parent_label_sequence": [item.get(doc) for item in previous],
                "final_guard_reviews": len(record.get("design_reviews") or []),
            }
        else:
            observations = record.get("tool_observations", [])
            matching = [
                event
                for event in observations
                if not event["error"]
                and (
                    doc in event["corpus_ids_mentioned"]
                    or any(stmt in event["output"] for stmt in statements)
                )
            ]
            details = {
                "tool_observation_count": len(observations),
                "baseline_receipts": [
                    {
                        "tool_call_id": item["tool_call_id"],
                        "exact_annotated_sentence_count": sum(
                            s in item["output"] for s in statements
                        ),
                        "doc_id_seen": doc in item["corpus_ids_mentioned"],
                        "visible_final_quote_seen": any(
                            p["quote"] in item["output"] for p in papers
                        ),
                    }
                    for item in matching
                ],
            }
        rows.append(
            {
                "system": record["system"],
                "case_id": task["case_id"],
                "slice": task["slice"],
                "doc_id": doc,
                "title": source["title"],
                "claim": task["claim"],
                "gold": sorted({entry["label"] for entry in labels}),
                "annotations": statements,
                "outcome": _outcome(papers, assess),
                "final_entries": papers,
                **details,
            }
        )
    return rows


def evaluate(name: str, model: str, *, preview: bool) -> dict:
    root, suite, corpus, plan = prepare(name, model)
    sources = {row["doc_id"]: row for row in corpus["records"]}
    targets, records, missing = [], [], []
    sensitivity, extra_paths = [], []
    failures = Counter()
    sessions = {"kimi_code": set(), "product": set()}
    for task in suite["tasks"]:
        case = task["case_id"]
        for folder, system in (("product", "product"), ("kimi", "kimi_code")):
            path = root / folder / f"{case}.json"
            if not path.is_file():
                missing.append([system, case])
                continue
            row = _read(path)
            if row["system"] != system or row["case_id"] != case or row["run_case_id"] != case:
                raise ValueError("record identity differs")
            if system == "kimi_code" and row["plan_sha256"] != sha256_json(plan):
                raise ValueError("baseline plan changed")
            ident = row.get("agent_run_id") if system == "product" else row.get("session_id")
            row["session_isolation_valid"] = bool(
                ident and ident not in sessions[system] and row.get("session_isolation_valid", True)
            )
            if ident:
                if ident in sessions[system]:
                    raise ValueError("same session used for multiple cases")
                sessions[system].add(ident)
            scored = score_record(row, task, corpus)
            if system == "product":
                scored["event_trace"] = product_event_trace(root, row)
            targets.extend(traced_targets(root, scored, task, sources))
            records.append(scored)
            only_extras = extra_fields_only(row.get("response_text") or "")
            if only_extras is not None and scored["assessment"]["valid_completion"]:
                filtered, paths = only_extras
                sensitivity.append(score_record({**row, "response_text": filtered}, task, corpus))
                extra_paths.append({"system": system, "case_id": case, "removed": paths})
            else:
                sensitivity.append(scored)
            if (
                not scored["assessment"]["valid_completion"]
                or not scored["assessment"]["format_valid"]
            ):
                reason = (scored.get("error") or {}).get("code") or scored.get(
                    "observability_error"
                )
                if not reason:
                    reason = (
                        "model_or_run_invalid"
                        if not scored["completed"] or not scored["model_valid"]
                        else "format"
                    )
                failures[f"{system}:{reason}"] += 1
    by_system = {
        system: aggregate([row for row in records if row["system"] == system])
        for system in ("product", "kimi_code")
    }
    case_counts = Counter(row["case_id"] for row in records)
    paired_cases = {case for case, count in case_counts.items() if count == 2}
    paired_records = [row for row in records if row["case_id"] in paired_cases]
    result = {
        "schema": "scifact-opencode-development-assessment-v1",
        "model": model,
        "complete_cohort": not missing,
        "missing": missing,
        "systems": by_system,
        "paired": paired_summary(paired_records) if paired_records else None,
        "matched_cases": len(paired_cases),
        "extra_fields_only_sensitivity": {
            "note": "Removes only unknown JSON keys; does not change papers or scores upstream.",
            "cases": extra_paths,
            "matched_systems": {
                system: aggregate(
                    [
                        row
                        for row in sensitivity
                        if row["system"] == system and row["case_id"] in paired_cases
                    ]
                )
                for system in by_system
            },
        },
        "loss": {
            system: family_outcomes([row for row in targets if row["system"] == system])
            for system in by_system
        },
        "failure_codes": dict(failures),
        "case_scores": [
            {
                "system": row["system"],
                "case_id": row["case_id"],
                "slice": row["slice"],
                "valid_completion": row["assessment"]["valid_completion"],
                "format_valid": row["assessment"]["format_valid"],
                "correct_direction_evidence_coverage": row["assessment"][
                    "correct_direction_evidence_coverage"
                ],
                "final_recall_at_10": row["assessment"]["retrieval_at_10"]["recall"],
                "product_stages": row["assessment"]["product_stages"],
                "ranking_metrics": row.get("ranking_metrics"),
                "event_trace": row.get("event_trace"),
                "complete_rationale_coverage": row["assessment"]["complete_rationale_coverage"],
            }
            for row in records
        ],
        "targets": targets,
    }
    if not preview:
        if missing:
            raise ValueError("both arms must finish; use --preview for incomplete data")
        save_new(root / "assessment.json", result)
    return {
        key: value
        for key, value in result.items()
        if key not in {"targets", "case_scores", "paired", "missing"}
    } | {
        "missing_count": len(missing),
        "positive_parent_union": mean(
            row["assessment"]["product_stages"].get("available_recall_union")
            for row in records
            if row["system"] == "product"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", required=True, choices=MODELS)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate(args.name, args.model, preview=args.preview), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
