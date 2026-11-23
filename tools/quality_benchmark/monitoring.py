from __future__ import annotations

from typing import Any


def discovery_metrics(queries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not any(query.get("candidate_rankings") for query in queries):
        return None
    candidates = [item for query in queries for item in query["candidate_diagnostics"]]
    rankings = [ranking for query in queries for ranking in query.get("candidate_rankings", [])]
    eligible = [item for item in candidates if item.get("ranking_status") != "ineligible"]
    violations = []
    for query in queries:
        for ranking in query.get("candidate_rankings", []):
            if ranking["status"] != "completed":
                continue
            items = [
                item
                for item in query["candidate_diagnostics"]
                if item["claim_id"] == ranking["claim_id"]
                and item.get("ranking_status") != "ineligible"
            ]
            items.sort(key=lambda item: item.get("llm_rank") or 10_000)
            expected = [item["work_id"] for item in items]
            if ranking["ordered_work_ids"] != expected or len(expected) != len(set(expected)):
                violations.append("RANK_ORDER_MISMATCH")
            if [item.get("llm_rank") for item in items] != list(range(1, len(items) + 1)):
                violations.append("RANK_NOT_CONTIGUOUS")
            for item in items:
                if item.get("pool_version") != ranking["pool_version"]:
                    violations.append("RANK_POOL_MISMATCH")
                base_rank = item.get("base_rank")
                if base_rank is not None and item.get("rank_delta") != base_rank - item["llm_rank"]:
                    violations.append("RANK_DELTA_MISMATCH")
                reason = item.get("reason") or {}
                quote = item.get("reason_evidence_quote")
                if (
                    reason.get("status") != "generated"
                    or not quote
                    or (
                        quote not in item["evidence_excerpt"]
                        and quote not in (item.get("title") or "")
                    )
                ):
                    violations.append("RANK_REASON_EVIDENCE_MISSING")
    return {
        "scope": (
            "final_candidate_pool_of_each_tool_query; not scientific quality or all live versions"
        ),
        "query_rankings": len(rankings),
        "completed_rankings": sum(item["status"] == "completed" for item in rankings),
        "failed_rankings": sum(item["status"] == "failed" for item in rankings),
        "eligible_candidates": len(eligible),
        "ranked_candidates": sum(item.get("ranking_status") == "ranked" for item in eligible),
        "generated_reasons": sum(
            (item.get("reason") or {}).get("status") == "generated" for item in eligible
        ),
        "rank_contract_violations": sorted(set(violations)),
    }


def required_flow(task: dict[str, Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    required = set(task["expect"].get("required_ids", []))
    available = {
        work_id
        for query in queries
        for worker in query.get("workers", [])
        for work_id in worker.get("source_available_work_ids", [])
    }
    submitted = {item["work_id"] for query in queries for item in query["candidate_diagnostics"]}
    ranked = {
        item["work_id"]
        for query in queries
        for item in query["candidate_diagnostics"]
        if item.get("ranking_status") == "ranked"
    }
    exposed = any(
        "source_available_work_ids" in worker
        for query in queries
        for worker in query.get("workers", [])
    )
    return {
        "source_required_seen": sorted(required & available) if exposed else None,
        "source_required_not_submitted": sorted((required & available) - submitted)
        if exposed
        else None,
        "submitted_required": sorted(required & submitted),
        "ranked_required": sorted(required & ranked),
        "all_required_ranked": required <= ranked if required else None,
    }


def monitoring_report(records: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    lookup = {task["run_case_id"]: task for task in tasks}
    rows = []
    for record in records:
        if record["system"] != "product":
            continue
        queries = record.get("queries", [])
        metrics = discovery_metrics(queries)
        if metrics is not None:
            rows.append(
                {
                    "run_case_id": record["run_case_id"],
                    **metrics,
                    **required_flow(lookup[record["run_case_id"]], queries),
                }
            )
    return {
        "available": bool(rows),
        "totals": {
            key: sum(row[key] for row in rows)
            for key in (
                "query_rankings",
                "completed_rankings",
                "failed_rankings",
                "eligible_candidates",
                "ranked_candidates",
                "generated_reasons",
            )
        },
        "contract_violation_runs": [
            row["run_case_id"] for row in rows if row["rank_contract_violations"]
        ],
        "cases": rows,
    }
