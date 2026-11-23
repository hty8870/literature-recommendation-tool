"""Verify the deployed anonymous HTTPS UI and MCP without credentials."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from citation_tool.agent import AGENT_RESPONSE_GROUNDING_VERSION
from citation_tool.common_scoring import COMMON_SCORE_VERSION
from citation_tool.listwise import LISTWISE_PROMPT_VERSION

TARGET_DOI = "10.1007/s42994-024-00192-2"


def discovery_contract_issues(state: dict) -> list[str]:
    issues = []
    workers = state.get("workers", [])
    expected_libraries = {"europepmc-online", "europepmc-snapshot", "abiotech-sample"}
    if (
        len(workers) != 3
        or {worker.get("library_id") for worker in workers} != expected_libraries
        or any(
            worker.get("worker_state") != "completed"
            or worker.get("source_agent_status") != "completed"
            for worker in workers
        )
    ):
        issues.append("three_source_agents_not_completed")
    rankings = {}
    for ranking in state.get("candidate_rankings", []):
        key = ranking["claim_id"]
        previous = rankings.get(key)
        if previous is None or (ranking["pool_version"], ranking["ranking_version"]) > (
            previous["pool_version"],
            previous["ranking_version"],
        ):
            rankings[key] = ranking
    candidates = [
        item
        for item in state.get("candidate_diagnostics", [])
        if item.get("ranking_status") != "ineligible"
    ]
    if not candidates or not rankings:
        issues.append("candidate_pool_or_ranking_missing")
    groups = {item.get("score_scale_group") for item in candidates}
    if len(groups) != 1 or None in groups:
        issues.append("common_score_group_missing_or_mixed")
    for claim_id in {item["claim_id"] for item in candidates}:
        ranking = rankings.get(claim_id, {})
        eligible = [item for item in candidates if item["claim_id"] == claim_id]
        ordered = ranking.get("ordered_work_ids", [])
        if (
            ranking.get("status") != "completed"
            or ranking.get("prompt_version") != LISTWISE_PROMPT_VERSION
            or len(ordered) != len(set(ordered))
            or set(ordered) != {item["work_id"] for item in eligible}
        ):
            issues.append(f"invalid_complete_ranking:{claim_id}")
        for item in eligible:
            work_id = item["work_id"]
            reason = item.get("reason") or {}
            quote = item.get("reason_evidence_quote")
            base_rank, llm_rank = item.get("base_rank"), item.get("llm_rank")
            if (
                item.get("ranking_status") != "ranked"
                or not isinstance(base_rank, int)
                or not isinstance(llm_rank, int)
                or work_id not in ordered
                or ordered.index(work_id) + 1 != llm_rank
                or item.get("pool_version") != ranking.get("pool_version")
                or item.get("rank_delta") != base_rank - llm_rank
                or not str(item.get("score_version", "")).startswith(COMMON_SCORE_VERSION)
                or reason.get("status") != "generated"
                or not quote
                or not (
                    quote in (item.get("title") or "")
                    or quote in (item.get("evidence_excerpt") or "")
                )
            ):
                issues.append(f"invalid_rank_or_grounded_reason:{work_id}")
    return issues


def discovery_stream_completed(state: dict, event_types: list[str]) -> bool:
    return (
        state.get("session", {}).get("search_state") == "completed"
        and any(
            item.get("doi") == TARGET_DOI
            and (item.get("reason") or {}).get("status") == "generated"
            for item in state.get("candidate_diagnostics", [])
        )
        and {"candidate_pool_updated", "candidate_reranked", "worker_completed"}.issubset(
            event_types
        )
        and event_types[-1:] == ["search_finalized"]
    )


def rpc(client: httpx.Client, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post(
        "/mcp", json=body, headers={"Accept": "application/json, text/event-stream"}
    )
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        raise AssertionError(f"MCP {method} failed: {result['error']['code']}")
    return result["result"]


def verify(
    base_url: str,
    output: Path | None,
    *,
    trust_env: bool = True,
    expected_model: str | None = None,
) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query:
        raise ValueError("public acceptance requires an HTTPS URL without embedded credentials")
    base_url = base_url.rstrip("/")
    checks = {
        "anonymous_ui": False,
        "minimal_result_shell": False,
        "anonymous_api": False,
        "three_source_registry": False,
        "model_settings_available": False,
        "evaluation_anchor_and_response_version": False,
        "web_agent_answer": False,
        "web_candidate_stream": False,
        "mcp_initialize": False,
        "mcp_tools": False,
        "real_target_and_reason": False,
        "source_agents_common_score_and_llm_ranking": False,
        "ranking_evidence_and_saved_replay": False,
        "online_source_participated": False,
        "dense_available": False,
        "immutable_empty_snapshot": False,
    }
    with httpx.Client(
        base_url=base_url, timeout=300, follow_redirects=False, trust_env=trust_env
    ) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        home = client.get("/")
        checks["anonymous_ui"] = (
            home.status_code == 200
            and health.json().get("authentication_required") is False
            and 'id="main-content"' in home.text
        )
        checks["minimal_result_shell"] = all(
            marker not in home.text
            for marker in (
                'id="search-query"',
                'id="session-badges"',
                'id="session-button"',
                'id="session-dialog"',
                'id="source-kind-label"',
            )
        )
        libraries = client.get("/api/v1/libraries")
        library_items = libraries.json().get("libraries", [])
        by_id = {item.get("library_id"): item for item in library_items}
        checks["anonymous_api"] = (
            libraries.status_code == 200
            and libraries.json().get("simulation") is False
            and libraries.json().get("agent_available") is True
            and client.post("/login", json={"token": "not-used"}).status_code == 404
        )
        checks["three_source_registry"] = (
            by_id.get("europepmc-online", {}).get("source_type") == "remote_api"
            and by_id.get("europepmc-snapshot", {}).get("record_count") == 11002
            and by_id.get("abiotech-sample", {}).get("record_count") == 50
        )
        checks["model_settings_available"] = 'id="model-dialog"' in home.text and {
            item.get("provider") for item in libraries.json().get("user_model_providers", [])
        } == {"deepseek", "moonshot", "openai"}
        initialized = rpc(client, "initialize")
        checks["mcp_initialize"] = initialized.get("protocolVersion") == "2025-03-26"
        tools = rpc(client, "tools/list")
        checks["mcp_tools"] = {tool["name"] for tool in tools.get("tools", [])} == {
            "search_literature",
            "get_search_state",
            "get_snapshot",
            "get_snapshot_evidence",
        }
        agent_response = client.post(
            "/api/v1/agent/runs",
            json={
                "input_type": "keywords",
                "keywords": ["LFY2", "soybean", "floral organ"],
                "library_ids": [item["library_id"] for item in library_items],
            },
        )
        agent_response.raise_for_status()
        agent_run = agent_response.json()
        deadline = time.monotonic() + 900
        while agent_run.get("status") in {"pending", "running"} and time.monotonic() < deadline:
            time.sleep(0.5)
            agent_response = client.get(f"/api/v1/agent/runs/{agent_run['run_id']}")
            agent_response.raise_for_status()
            agent_run = agent_response.json()
        agent_text = agent_run.get("summary") or ""
        queries = agent_run.get("queries") or []
        anchor = queries[0]["created"]["claims"][0].get("evaluation_anchor") if queries else None
        checks["evaluation_anchor_and_response_version"] = (
            bool(anchor)
            and anchor.get("claim_text") == "LFY2 soybean floral organ"
            and agent_run.get("response_grounding") == AGENT_RESPONSE_GROUNDING_VERSION
        )
        checks["web_agent_answer"] = (
            agent_run.get("status") == "completed"
            and bool(agent_text.strip())
            and not any(
                phrase in agent_text
                for phrase in (
                    "reason_model_configured=false",
                    "未配置理由模型",
                    "未配置可用模型",
                    "未配置模型",
                )
            )
        )
        web_query_id = agent_run.get("latest_query_id")
        web_state_response = client.get(f"/api/v1/searches/{web_query_id}")
        web_state_response.raise_for_status()
        web_state = web_state_response.json()
        event_types = []
        with client.stream("GET", f"/api/v1/searches/{web_query_id}/events") as stream:
            stream.raise_for_status()
            for line in stream.iter_lines():
                if line.startswith("event:"):
                    event_types.append(line.removeprefix("event:").strip())
        checks["web_candidate_stream"] = discovery_stream_completed(web_state, event_types)
        result = rpc(
            client,
            "tools/call",
            {
                "name": "search_literature",
                "arguments": {
                    "input_type": "keywords",
                    "keywords": ["LFY2", "soybean", "floral organ"],
                },
            },
        )["structuredContent"]
        target = next(
            (
                candidate
                for candidate in result.get("candidate_diagnostics", [])
                if candidate.get("doi") == TARGET_DOI
            ),
            None,
        )
        checks["real_target_and_reason"] = bool(
            result.get("simulation") is False
            and result.get("reason_model_configured") is True
            and target
            and (target.get("reason") or {}).get("status") == "generated"
            and target.get("reason", {}).get("text")
        )
        if expected_model:
            checks["actual_default_model"] = (
                health.json().get("reason_model") == expected_model
                and agent_run.get("served_model") == expected_model
                and (target or {}).get("reason", {}).get("served_model") == expected_model
            )
        replay_path = f"/api/v1/searches/{result['query_id']}/candidate-rankings"
        first_rankings = client.get(replay_path)
        second_rankings = client.get(replay_path)
        first_reason = (target or {}).get("reason") or {}
        checks["ranking_evidence_and_saved_replay"] = (
            first_rankings.status_code == 200
            and first_rankings.content == second_rankings.content
            and bool(first_reason.get("input_hash"))
            and bool((target or {}).get("reason_evidence_quote"))
        )
        checks["online_source_participated"] = bool(
            target and "europepmc-online" in target.get("source_uris", {})
        )
        state = rpc(
            client,
            "tools/call",
            {"name": "get_search_state", "arguments": {"query_id": result["query_id"]}},
        )["structuredContent"]
        ranking_issues = discovery_contract_issues(state)
        checks["source_agents_common_score_and_llm_ranking"] = not ranking_issues
        dense = [
            worker.get("model_versions", {}).get("dense_status")
            for worker in state["workers"]
            if worker.get("library_id") == "europepmc-snapshot"
        ]
        checks["dense_available"] = len(dense) == 1 and dense == ["available"]
        snapshot_id = result["final_snapshot_id"]
        first = client.get(f"/api/v1/snapshots/{snapshot_id}")
        second = client.get(f"/api/v1/snapshots/{snapshot_id}")
        snapshot = first.json()
        checks["immutable_empty_snapshot"] = (
            first.status_code == 200
            and first.content == second.content
            and snapshot.get("item_count") == 0
            and snapshot.get("immutable_hash") == first.headers.get("etag", "").strip('"')
        )
        summary = {
            "schema": "citation-public-acceptance-v4-source-agents",
            "base_url": base_url,
            "network_mode": "environment" if trust_env else "direct",
            "expected_model": expected_model,
            "served_agent_model": agent_run.get("served_model"),
            "served_reason_model": (target or {}).get("reason", {}).get("served_model"),
            "protocol": initialized.get("protocolVersion"),
            "authentication_required": False,
            "query_id": result["query_id"],
            "replayed_rankings_query_id": result["query_id"],
            "ranking_contract_issues": ranking_issues,
            "agent_run_id": agent_run.get("run_id"),
            "web_query_id": web_query_id,
            "web_event_count": len(event_types),
            "index_snapshots": sorted(
                {worker.get("index_snapshot") for worker in state["workers"]}
            ),
            "snapshot_id": snapshot_id,
            "target_doi": TARGET_DOI,
            "candidate_count": len(result.get("candidate_diagnostics", [])),
            "checks": checks,
            "passed": all(checks.values()),
            "privacy_boundary": "anonymous public instance; do not submit private manuscripts",
            "evidence_boundary": (
                "real title/abstract candidates; no full-text citation eligibility"
            ),
        }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise FileExistsError(f"refusing to overwrite acceptance artifact: {output}")
        output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://biodataagent.com/literature")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--direct", action="store_true", help="Do not use environment proxies")
    parser.add_argument("--expected-model")
    args = parser.parse_args()
    summary = verify(
        args.base_url,
        args.output,
        trust_env=not args.direct,
        expected_model=args.expected_model,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
