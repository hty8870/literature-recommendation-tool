"""Development-only same-tool SciFact trial: Kimi Code in the parent loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from filelock import FileLock
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from tools import scifact_harness_mcp as external
from tools import scifact_workspace_retest as reference
from tools.benchmark.core import file_hash
from tools.gap_benchmark.metrics import aggregate, score_record
from tools.quality_benchmark.core import project_skill_isolation_valid

from citation_tool import agent as agent_module
from citation_tool.jcs import sha256_json

REFERENCE_NAME = "scifact-workspace-20261121"
EXTRA_CASES = ("sf-528", "sf-1137")


def inputs(name):
    if name == REFERENCE_NAME:
        raise ValueError("choose a new evaluation name")
    root, suite, corpus = reference.prepare_run(name)
    control, _, _ = reference.prepare_run(REFERENCE_NAME)
    plan = reference.read(control / "workspace-contract.json")
    if (
        reference.native.product_implementation_hash(control, corpus)
        != plan["native_implementation"]
    ):
        raise ValueError("run from the verified frozen product used by the control")
    cases = [
        task
        for task in suite["tasks"]
        if task["slice"] == "multi_document" or task["case_id"] in EXTRA_CASES
    ]
    if len(cases) != 6 or {t["case_id"] for t in cases} - set(plan["cases"]):
        raise ValueError("frozen trial case identities changed")
    control_id = sha256_json(plan)
    snapshots = {}
    for task in cases:
        case = task["case_id"]
        files = {arm: control / arm / f"{case}.json" for arm in ("default", "workspace", "kimi")}
        if reference.read(files["workspace"])["implementation_hash"] != control_id:
            raise ValueError("workspace control subject differs from its frozen setup")
        snapshots[case] = {arm: file_hash(path) for arm, path in files.items()}
    contract = {
        "schema": "scifact-kimi-parent-trial-v1",
        "controls": REFERENCE_NAME,
        "control_contract": control_id,
        "control_records": snapshots,
        "scripts": {
            "runner": file_hash(Path(__file__)),
            "bridge": file_hash(Path(external.__file__)),
        },
        "model": reference.MODEL,
        "comparison": (
            "Same frozen search, rank, product prompt, tools and final checks; "
            "Kimi replaces the parent loop via local MCP. CLI envelope and effort differ."
        ),
        "case_selection": (
            "All four multi-document cases plus support sf-528 and contradict sf-1137; "
            "previously inspected, for development only."
        ),
        "data_boundary": "Public titles/abstracts, no gold, host tools or private data.",
        "provider_concurrency": "One subject; two native requests while CLI waits for tools.",
        "note": "Exact control records reused; full search, not an independent six-case suite.",
    }
    path = root / "harness-contract.json"
    if path.exists():
        if reference.read(path) != contract:
            raise ValueError("pilot or controls changed; use a new run name")
    else:
        reference.save_new(path, contract)
    return root, control, corpus, cases, plan, contract


async def probe():
    token = uuid4().hex

    async def read_probe(path: str) -> dict:
        return {"marker": token} if path == "public.txt" else {"error": {"code": "NOT_FOUND"}}

    tool = StructuredTool.from_function(
        coroutine=read_probe,
        name="Read",
        description="Read public.txt, the one permitted test resource.",
    )
    parent = external.KimiParent(
        [tool], "Read the permitted test resource and follow the user's output requirement."
    )
    async with asyncio.timeout(80):
        result = await parent.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=(
                            "Use the Read MCP tool on public.txt. "
                            "Return only the marker text it contains."
                        )
                    )
                ]
            }
        )
    if result["messages"][-1].content != token or len(parent.bridge.actions) != 1:
        raise ValueError("MCP preflight did not return the server-only marker")
    return {"passed": True, **parent.audit}


async def ensure_probe(root):
    path = root / "harness-preflight.json"
    digest = file_hash(Path(external.__file__))
    with FileLock(root / ".harness-preflight.lock", timeout=0):
        if path.is_file():
            proof = reference.read(path)
            if not proof.get("passed") or proof.get("bridge_sha256") != digest:
                raise ValueError("MCP preflight predates this bridge; choose a new run name")
            return proof
        proof = {**await probe(), "bridge_sha256": digest}
        reference.save_new(path, proof)
        return proof


async def case_run(root, case, config, api, corpus, index, contract_id):
    runtime = reference.product.new_attempt_dir(root, f"kimi-parent-{case['case_id']}")
    abstracts = {row["doc_id"]: row["abstract"] for row in corpus["records"]}
    audit = reference.EvidenceAudit(runtime / "model-calls", abstracts)
    gateway = reference.ParallelLunaGateway(config, api, audit)
    created, retrieval = [], []

    def parent_factory(*, model, tools, system_prompt, middleware):
        parent = external.KimiParent(
            tools, system_prompt, search_limit=reference.product.AGENT_MAX_TOOL_CALLS
        )
        created.append(parent)
        return parent

    def build(settings, adapter, gateway, callback):
        reference.attach_retrieval_audit(adapter, retrieval)
        settings = replace(settings, agent_research_workspace=True, agent_research_context=True)
        return reference.native.build_product_app(settings, adapter, gateway, callback)

    subject = reference.SUBJECT.set(f"kimi-parent/{case['case_id']}")
    try:
        with patch.object(agent_module, "create_agent", parent_factory):
            row = await reference.product.run_case(
                {key: value for key, value in case.items() if key != "expect"},
                root=root,
                runtime=runtime,
                corpus=corpus,
                index=index,
                implementation_hash=contract_id,
                shared_async=asyncio.Semaphore(2),
                shared_sync=threading.BoundedSemaphore(2),
                gateway_override=gateway,
                app_builder=build,
                audit_collector=reference.native.collect_model_audit,
            )
        drain_error, wait_ms = await reference.settle_case(
            audit, drain_seconds=reference.product.MODEL_TIMEOUT_S + 10
        )
    finally:
        reference.SUBJECT.reset(subject)
    native = reference.native.collect_model_audit(runtime / "model-calls")
    row = reference.native._native_record(row, native, runtime)
    parent = created[0] if len(created) == 1 else None
    if parent:
        for tool, _, receipt in parent.bridge.actions:
            audit.observe(tool, receipt)
    parent_audit = parent.audit if parent else {}
    session = parent_audit.get("session_id")
    valid = bool(
        parent_audit.get("tools_match_receipts") and session and not (parent and parent.error)
    )
    model_valid = bool(valid and row["model_valid"] and not drain_error)
    row.update(
        arm="kimi_parent",
        parent_harness="kimi-code-native-mcp",
        transport="kimi-code-cli-mcp-parent+openai-responses-native-tools",
        transport_boundary=(
            "Kimi Code drives the parent via native MCP; downstream stays on OpenAI Responses. "
            "The CLI envelope and reasoning effort can differ from the control."
        ),
        parent_model_request_count=parent_audit.get("model_requests"),
        parent_audit=parent_audit,
        parent_error=parent.error if parent else "parent not initialized",
        model_valid=model_valid,
        completed=row["completed"] and model_valid,
        session_isolation_valid=bool(row["session_isolation_valid"] and valid),
        audit_shutdown={"error": drain_error, "wait_ms": wait_ms},
        evidence_deliveries=audit.deliveries,
        evidence_delivery_boundary="MCP receipt sent; not proof of later prompt retention",
        retrieval_audit=retrieval,
        dense_retrieval=bool(retrieval)
        and all(e["dense_status"] == "available" for e in retrieval),
        dense_common_score=reference.full_dense_common_score(row["queries"]),
    )
    if row["secret_leak"] or config.api_key in row["visible_output"]:
        raise ValueError("potential key exposure; output not saved")
    reference.save_new(root / "kimi-parent" / f"{case['case_id']}.json", row)
    return row


async def run(name):
    root, _, corpus, cases, control, contract = inputs(name)
    lock = reference.RUNTIME / "opencode-parallel/.shared-e5.lock"
    with FileLock(lock, timeout=0):
        await ensure_probe(root)
        reference.require_memory(reference.START_RESERVE)
        index, cache_hash = await asyncio.to_thread(
            reference.ready_index, root, cases[0], corpus, index_type=reference.SharedEmbeddingIndex
        )
        if cache_hash != control["e5_cache_sha256"]:
            raise ValueError("E5 index differs from the control")
        config = await asyncio.to_thread(reference.authorized_config)
        async with reference.AuditedLunaClient(2) as api:
            try:
                sessions = set()
                for case in cases:
                    path = root / "kimi-parent" / f"{case['case_id']}.json"
                    if path.exists():
                        row = reference.read(path)
                        if row.get("implementation_hash") != sha256_json(contract):
                            raise ValueError("cannot reuse another implementation's subject")
                    else:
                        reference.require_memory(reference.CASE_RESERVE)
                        row = await case_run(
                            root, case, config, api, corpus, index, sha256_json(contract)
                        )
                    parent = row["parent_audit"]
                    session = parent.get("session_id")
                    if session and session in sessions:
                        raise ValueError("external Agent session reused across subjects")
                    sessions.add(session)
                    print(
                        json.dumps(
                            {
                                "case": case["case_id"],
                                "completed": row["completed"],
                                "parent_error": row["parent_error"],
                                "error": row["error"],
                                "native_drain_error": row["audit_shutdown"]["error"],
                                "mcp_tools": parent.get("tool_names", []),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    if (
                        api.blocked
                        or row["audit_shutdown"]["error"]
                        or not parent.get("tools_match_receipts")
                    ):
                        break
            finally:
                reference.save_new(
                    root / f"harness-http-{uuid4().hex[:10]}.json",
                    {
                        "rows": api.rows,
                        "peak": api.peak,
                        "blocked": api.blocked,
                    },
                )


def score(name):
    root, control, corpus, cases, _, contract = inputs(name)
    missing, assessed = [], []
    by_id = {r["doc_id"]: r for r in corpus["records"]}
    session_ids = set()
    for task in cases:
        case = task["case_id"]
        path = root / "kimi-parent" / f"{case}.json"
        if not path.is_file():
            missing.append(case)
            continue
        arms = {
            arm: reference.read(control / arm / f"{case}.json")
            for arm in ("default", "workspace", "kimi")
        }
        arms["kimi"]["project_skill_isolation_valid"] = project_skill_isolation_valid(arms["kimi"])
        candidate = reference.read(path)
        if candidate["implementation_hash"] != sha256_json(contract):
            raise ValueError("external Subject differs from its registered implementation")
        session = candidate["parent_audit"].get("session_id")
        if session is not None and session in session_ids:
            raise ValueError("external Agent sessions not isolated")
        session_ids.add(session)
        arms["kimi_parent"] = candidate
        rows = {arm: score_record(row, task, corpus) for arm, row in arms.items()}
        for arm in ("default", "workspace", "kimi_parent"):
            rows[arm]["target_readback"] = reference.evidence_losses(rows[arm], task, by_id)
        assessed.append({"case": case, "slice": task["slice"], "arms": rows})
    arm_names = ("default", "workspace", "kimi", "kimi_parent")
    summaries = {arm: aggregate([r["arms"][arm] for r in assessed]) for arm in arm_names}
    multi = {
        arm: aggregate([r["arms"][arm] for r in assessed if r["slice"] == "multi_document"])
        for arm in arm_names
    }
    result = {"summaries": summaries, "multi_document": multi, "missing": missing}
    report = root / f"harness-assessment-{uuid4().hex[:10]}.json"
    reference.save_new(report, {**result, "contract": contract, "cases": assessed})
    return {**result, "report": str(report)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("probe", "run", "score"))
    parser.add_argument("--name")
    args = parser.parse_args()
    if args.command == "probe":
        if args.name:
            root, *_ = inputs(args.name)
            result = asyncio.run(ensure_probe(root))
        else:
            result = asyncio.run(probe())
    elif not args.name:
        parser.error("--name is required")
    else:
        if args.command == "run":
            asyncio.run(run(args.name))
        result = score(args.name)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 2 if result.get("missing") else 0


if __name__ == "__main__":
    raise SystemExit(main())
