from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]


def run(base: str, output: Path) -> None:
    parsed = urlparse(base)
    if parsed.hostname != "127.0.0.1" or parsed.scheme != "http":
        raise ValueError("this acceptance command only targets an explicit loopback server")
    output = output.resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        raise ValueError("use a new acceptance path inside the project")
    serial = 0
    checks = []
    with httpx.Client(
        base_url=base.rstrip("/"),
        timeout=450,
        headers={
            "X-Tenant-ID": "mcp-acceptance-v18",
            "MCP-Protocol-Version": "2025-03-26",
            "Accept": "application/json, text/event-stream",
        },
    ) as client:

        def rpc(method, params):
            nonlocal serial
            serial += 1
            response = client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": serial, "method": method, "params": params}
            )
            response.raise_for_status()
            body = response.json()
            assert "error" not in body, body.get("error")
            result = body["result"]
            return result.get("structuredContent", result)

        initialized = rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "local-discovery-acceptance", "version": "1"},
            },
        )
        assert initialized["protocolVersion"] == "2025-03-26"
        tools = rpc("tools/list", {})
        assert {tool["name"] for tool in tools["tools"]} >= {
            "search_literature",
            "get_search_state",
            "get_snapshot",
        }
        checks.append("anonymous_mcp_initialize_and_tools")
        result = rpc(
            "tools/call",
            {
                "name": "search_literature",
                "arguments": {
                    "input_type": "keywords",
                    "keywords": ["LFY2", "soybean"],
                },
            },
        )
        state = rpc(
            "tools/call",
            {"name": "get_search_state", "arguments": {"query_id": result["query_id"]}},
        )
        workers = [
            {
                key: worker.get(key)
                for key in ("library_id", "worker_state", "source_agent_status", "last_error")
            }
            for worker in state["workers"]
        ]
        assert state["session"]["search_state"] == "completed", workers
        assert len(workers) == 3 and all(
            worker["worker_state"] == "completed" for worker in workers
        ), workers
        assert all(worker["source_agent_status"] == "completed" for worker in workers)
        checks.append("three_real_source_agents_completed")
        candidates = state["candidate_diagnostics"]
        target = next(
            item for item in candidates if item.get("doi") == "10.1007/s42994-024-00192-2"
        )
        assert len(target["library_ids"]) == 3
        assert target["ranking_status"] == "ranked" and target["reason"]["status"] == "generated"
        assert (
            target["reason_evidence_quote"] in target["evidence_excerpt"]
            or target["reason_evidence_quote"] in target["title"]
        )
        assert target["rank_delta"] == target["base_rank"] - target["llm_rank"]
        assert (
            len(
                {
                    item["score_scale_group"]
                    for item in candidates
                    if item["candidate_score"] is not None
                }
            )
            == 1
        )
        assert all(ranking["status"] == "completed" for ranking in state["candidate_rankings"])
        checks.append("common_score_llm_rank_evidence_and_merged_sources")
        assert not state["results"]
        first = rpc(
            "tools/call",
            {
                "name": "get_snapshot",
                "arguments": {"snapshot_id": state["session"]["final_snapshot_id"]},
            },
        )
        second = rpc(
            "tools/call",
            {
                "name": "get_snapshot",
                "arguments": {"snapshot_id": state["session"]["final_snapshot_id"]},
            },
        )
        assert first["items"] == [] and first["immutable_hash"] == second["immutable_hash"]
        checks.append("metadata_not_citable_and_snapshot_immutable")
        report = {
            "status": "passed",
            "base_url": base,
            "query_id": result["query_id"],
            "checks": checks,
            "workers": workers,
            "candidate_count": len(candidates),
            "target": {
                key: target.get(key)
                for key in (
                    "doi",
                    "llm_rank",
                    "base_rank",
                    "candidate_score",
                    "rank_delta",
                    "library_ids",
                )
            },
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real anonymous MCP acceptance for the source-agent pipeline"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8876")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/runtime/discovery-mcp-acceptance-v18.json"
    )
    args = parser.parse_args()
    run(args.base_url, args.output)
