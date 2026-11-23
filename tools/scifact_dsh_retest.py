"""Fresh, same-tools SciFact pairs replacing only the product's parent loop."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from filelock import FileLock
from langchain_core.messages import HumanMessage
from tools import scifact_workspace_retest as trial
from tools.benchmark.core import file_hash
from tools.gap_benchmark.metrics import aggregate, score_record
from tools.gap_benchmark.opencode_parallel import PilotBlocked
from tools.scifact_dsh_bridge import DRIVER, BridgeError, DshParent
from tools.scifact_rank_reason_retest import CASES, direction_conflicts, inputs, unjudged_direction
from tools.scifact_workspace_resume import (
    LOADED_INDEX_WAIT_S,
    PAIR_RESERVE,
    wait_for_memory,
)

from citation_tool import agent as product_agent

ARMS = ("control", "dsh")
PROTOCOL = trial.SUITE_ROOT / "parent-loop-dsh-v1.md"
PREFLIGHT = trial.RUNTIME / "luna-retests/scifact-dsh-luna-preflight-20261121-b/dsh-preflight.json"
ACTIVE_GRAPHS = ContextVar("scifact_dsh_graphs", default=None)


class DshGraph:
    def __init__(self, *, model, tools, system_prompt):
        if {tool.name for tool in tools} != {"search_literature"}:
            raise BridgeError("UNEXPECTED_DEFAULT_PRODUCT_TOOL_SET")
        self.model, self.tools, self.system = model, tools, system_prompt
        self.parent = None
        self.error = None
        self.closed_normally = False

    async def ainvoke(self, state, config=None):
        messages = state.get("messages", [])
        try:
            if not messages or not isinstance(messages[-1], HumanMessage):
                raise BridgeError("EXPECTED_USER_OR_GROUNDING_CORRECTION")
            if self.parent is None:
                if len(messages) != 1 or not isinstance(messages[0].content, str):
                    raise BridgeError("EXPECTED_FRESH_PRODUCT_CONVERSATION")
                self.parent = DshParent(
                    self.model,
                    self.tools,
                    self.system,
                    messages[0].content,
                    max_model_calls=config["recursion_limit"],
                    max_tool_calls=trial.product.AGENT_MAX_TOOL_CALLS,
                )
                self.parent.history[0] = messages[0]
                await self.parent.start()
            else:
                if len(self.parent.turns) != 1 or messages[:-1] != self.parent.history:
                    raise BridgeError("PRODUCT_DSH_FOLLOWUP_PREFIX_CHANGED")
                await self.parent.followup(messages[-1])
            return {"messages": list(self.parent.history)}
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, BridgeError) else type(exc).__name__
            raise

    async def aclose(self):
        if self.parent is not None:
            self.closed_normally = await self.parent.aclose()

    def trace(self):
        parent = self.parent
        turns = parent.turns if parent else []
        return {
            "dsh_session": turns[0]["sessionId"] if turns else None,
            "dsh_version": turns[0]["version"] if turns else None,
            "tool_names": [tool.name for tool in self.tools],
            "turns_completed": len(turns),
            "model_requests": parent.model_calls if parent else 0,
            "tool_calls": parent.tool_calls if parent else 0,
            "error": self.error,
            "closed_normally": self.closed_normally,
        }


def routed_factory(original):
    def create(**kwargs):
        graphs = ACTIVE_GRAPHS.get()
        if graphs is None:
            return original(**kwargs)
        if graphs:
            raise BridgeError("MORE_THAN_ONE_DSH_PARENT_IN_SUBJECT")
        graph = DshGraph(
            model=kwargs["model"], tools=kwargs["tools"], system_prompt=kwargs["system_prompt"]
        )
        graphs.append(graph)
        return graph

    return create


def contract_for(root, suite, corpus, tasks, cache_hash):
    proof = trial.read(PREFLIGHT)
    if (
        not proof["passed"]
        or proof["driver_sha256"] != file_hash(DRIVER)
        or proof["bridge_sha256"] != file_hash(DRIVER.with_name("scifact_dsh_bridge.py"))
    ):
        raise ValueError("the real DSH preflight does not match this bridge")
    return trial.contract(root, suite, corpus, cache_hash, CASES, list(ARMS)) | {
        "schema": "scifact-dsh-parent-pair-v1",
        "paired_runner_sha256": file_hash(Path(__file__)),
        "registered_protocol_sha256": file_hash(PROTOCOL),
        "bridge_sha256": proof["bridge_sha256"],
        "driver_sha256": proof["driver_sha256"],
        "validated_preflight_sha256": file_hash(PREFLIGHT),
        "cases": [task["case_id"] for task in tasks],
        "features": {"control": "default product parent", "dsh": "replace parent loop only"},
        "probes": [CASES[0]],
        "primary": "strict official direction plus verbatim, five labeled development claims",
        "scope": "same native transport and product tools; fresh per-case DSH sessions; no holdout",
    }


async def subject(root, task, arm, *inputs):
    graphs = [] if arm == "dsh" else None
    token = ACTIVE_GRAPHS.set(graphs)
    try:
        row = await trial.one(root, task, arm, *inputs)
    finally:
        ACTIVE_GRAPHS.reset(token)
        if graphs:
            await graphs[0].aclose()
    if arm == "dsh":
        proof = graphs[0].trace() if graphs else {"error": "DSH_PARENT_NOT_CREATED"}
        trial.save_new(root / "dsh-traces" / f"{task['case_id']}.json", proof)
    return row


def trace_valid(proof):
    return bool(
        proof.get("dsh_session")
        and proof.get("dsh_version") == "0.1.0-rc.6"
        and proof.get("tool_calls", 0) > 0
        and proof.get("turns_completed") in (1, 2)
        and proof.get("tool_names") == ["search_literature"]
        and proof.get("closed_normally")
        and not proof.get("error")
    )


async def run(name):
    root, suite, corpus, tasks = inputs(name)
    manifest = root / "dsh-contract.json"
    phase = uuid4().hex[:10]
    case_by_id = {c["case_id"]: c for c in tasks}
    abstracts = {r["doc_id"]: r["abstract"] for r in corpus["records"]}
    gate_file = root / "dsh-first-pair.json"
    blocked, recycle = False, False
    with FileLock(trial.RUNTIME / "opencode-parallel/.shared-e5.lock", timeout=0):
        await wait_for_memory(trial.START_RESERVE, "DSH-pair E5 startup")
        index, cache_hash = await asyncio.to_thread(
            trial.ready_index, root, tasks[0], corpus, index_type=trial.SharedEmbeddingIndex
        )
        contract = contract_for(root, suite, corpus, tasks, cache_hash)
        if manifest.is_file():
            if trial.read(manifest) != contract:
                raise ValueError("the frozen DSH contract has changed")
        else:
            trial.save_new(manifest, contract)
        digest = trial.sha256_json(contract)
        config = await asyncio.to_thread(trial.authorized_config)
        batches = []
        async with trial.AuditedLunaClient(2) as api:
            factory = routed_factory(product_agent.create_agent)
            try:
                with patch.object(product_agent, "create_agent", factory):
                    for i, case in enumerate(CASES):
                        if api.blocked:
                            break
                        task = case_by_id[case]
                        order = ARMS if i % 2 == 0 else ARMS[::-1]
                        surrounding = (config, api, corpus, abstracts, index, digest, phase)
                        todo = [
                            arm
                            for arm in order
                            if trial.product.load_reusable_record(
                                root / arm / f"{case}.json", resume=True, implementation_hash=digest
                            )
                            is None
                        ]
                        try:
                            while todo:
                                reading = await wait_for_memory(
                                    trial.CASE_RESERVE,
                                    "DSH-pair admission",
                                    wait_s=LOADED_INDEX_WAIT_S,
                                )
                                width = 2 if min(reading.values()) >= PAIR_RESERVE else 1
                                batch, todo = todo[:width], todo[width:]
                                batches.append({"case": case, "arms": batch, "memory": reading})
                                fetched = await asyncio.gather(
                                    *(subject(root, task, arm, *surrounding) for arm in batch),
                                    return_exceptions=True,
                                )
                                for row in fetched:
                                    if isinstance(row, Exception):
                                        raise row
                                    if row["audit_shutdown"]["error"]:
                                        raise PilotBlocked(
                                            "model audit must drain in a new process"
                                        )
                        except PilotBlocked:
                            recycle = True
                            break
                        if i == 0:
                            qualified = {
                                arm: trial.qualified(
                                    trial.read(root / arm / f"{case}.json"), task, corpus
                                )
                                for arm in ARMS
                            }
                            seen = root / "dsh-traces" / f"{case}.json"
                            gate = {
                                "case": case,
                                "qualified": qualified,
                                "verified_dsh_trace": seen.is_file()
                                and trace_valid(trial.read(seen)),
                            }
                            gate["passed"] = all(qualified.values()) and gate["verified_dsh_trace"]
                            if gate_file.is_file():
                                if trial.read(gate_file) != gate:
                                    raise ValueError("saved DSH gate no longer matches the case")
                            else:
                                trial.save_new(gate_file, gate)
                            print(json.dumps(gate), flush=True)
                            if not gate["passed"]:
                                blocked = True
                                break
            finally:
                trial.save_new(
                    root / f"dsh-http-{phase}.json",
                    {
                        "rows": api.rows,
                        "peak": api.peak,
                        "overlap": trial.overlap(api.rows),
                        "provider_blocked": api.blocked,
                        "recycle": recycle,
                        "batches": batches,
                    },
                )
        if contract_for(root, suite, corpus, tasks, file_hash(index.cache_path)) != contract:
            raise ValueError("DSH inputs changed during the batch")
    return 2 if api.blocked or blocked else 12 if recycle else 0


def score(name):
    root, _, corpus, tasks = inputs(name)
    contract = trial.read(root / "dsh-contract.json")
    identity = trial.sha256_json(contract)
    source = {r["doc_id"]: r for r in corpus["records"]}
    missing, pairs, unjudged, sessions, gaining = [], [], [], set(), set()
    quality = "correct_direction_evidence_coverage"
    for task in tasks:
        case = task["case_id"]
        paths = {arm: root / arm / f"{case}.json" for arm in ARMS}
        if any(not f.is_file() for f in paths.values()):
            missing.extend([case, arm] for arm, f in paths.items() if not f.is_file())
            continue
        raw = {arm: trial.read(path) for arm, path in paths.items()}
        if any(row["implementation_hash"] != identity for row in raw.values()):
            raise ValueError("wrong DSH product or case fingerprint")
        path = root / "dsh-traces" / f"{case}.json"
        trace = trial.read(path) if path.is_file() else {}
        sid = trace.get("dsh_session")
        if sid is not None:
            if sid in sessions:
                raise ValueError("reused DSH session across cases")
            sessions.add(sid)
        if not trace_valid(trace):
            raw["dsh"] = raw["dsh"] | {"completed": False, "session_isolation_valid": False}
        arms = {arm: score_record(row, task, corpus) for arm, row in raw.items()}
        for row in arms.values():
            row["target_readback"] = trial.evidence_losses(row, task, source)
        off, on = (arms[a] for a in ARMS)
        x, y = (row["assessment"][quality] for row in (off, on))
        if x is not None and y > x and all(trial.qualified(row, task, corpus) for row in (off, on)):
            gaining.add(task["family"])
        unjudged.extend(
            [case, doc, sign]
            for doc, sign in sorted(unjudged_direction(on) - unjudged_direction(off))
        )
        pairs.append({"case": case, "arms": arms, "trace": trace})
    records = {a: [p["arms"][a] for p in pairs] for a in ARMS}
    summaries = {a: aggregate(rows) for a, rows in records.items()}
    off, on = (summaries[a] for a in ARMS)
    gain = on[quality] - off[quality] if off[quality] is not None else None
    guard = {
        "quality_gain": gain is not None and gain >= 0.05,
        "two_families": len(gaining) >= 2,
        "completed": on["completed"] >= off["completed"],
        "recall": off["final_recall_at_10"] is not None
        and on["final_recall_at_10"] >= off["final_recall_at_10"],
        "complete_rationales": off["complete_rationale_coverage"] is not None
        and on["complete_rationale_coverage"] >= off["complete_rationale_coverage"],
        "opposite_direction": direction_conflicts(records["dsh"])
        <= direction_conflicts(records["control"]),
        "quotes_and_identity": all(on[k] <= off[k] for k in ("invalid_quotes", "unknown_ids")),
    }
    result = {
        "missing": missing,
        "systems": summaries,
        "guidance": guard,
        "gain": gain,
        "gaining_families": sorted(gaining),
        "new_unjudged_directions": unjudged,
        "screen_passed": not missing and not unjudged and all(guard.values()),
    }
    path = root / f"dsh-assessment-{uuid4().hex[:10]}.json"
    trial.save_new(path, result | {"pairs": pairs, "contract": contract})
    return result | {"report": str(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "score"))
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if args.operation == "run":
        code = asyncio.run(run(args.name))
        if code:
            return code
    report = score(args.name)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 2 if report["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
