"""Paired native Luna test of hiding only the generated reason from the parent receipt."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from tools import scifact_workspace_retest as trial
from tools.benchmark.core import file_hash
from tools.gap_benchmark.metrics import aggregate, score_record
from tools.gap_benchmark.opencode_parallel import PilotBlocked
from tools.scifact_workspace_resume import (
    LOADED_INDEX_WAIT_S,
    PAIR_RESERVE,
    MemoryBackpressure,
    wait_for_memory,
)

CASES = ("sf-1029", "sf-528", "sf-873", "sf-1187", "sf-1137", "sf-437")
ARMS = ("control", "no_rank_reason")
PROTOCOL = trial.SUITE_ROOT / "rank-reason-parent-v1.md"


def inputs(name):
    root, suite, corpus = trial.prepare_run(name)
    by_id = {case["case_id"]: case for case in suite["tasks"]}
    tasks = [by_id[case] for case in CASES]
    return root, suite, corpus, tasks


def contract_for(root, suite, corpus, tasks, cache_hash):
    return trial.contract(root, suite, corpus, cache_hash, CASES, list(ARMS)) | {
        "schema": "scifact-rank-reason-parent-pair-v1",
        "paired_runner_sha256": file_hash(Path(__file__)),
        "protocol_sha256": file_hash(PROTOCOL),
        "scheduler_sha256": file_hash(Path(__file__).with_name("scifact_workspace_resume.py")),
        "cases": [task["case_id"] for task in tasks],
        "features": {"control": "default parent", "no_rank_reason": "omit only reason.text"},
        "probes": [],
        "primary": "frozen strict direction plus verbatim; five labeled development cases",
        "scope": "six previously inspected tasks, paired end to end; no new holdout",
    }


async def run(name):
    root, suite, corpus, tasks = inputs(name)
    contract_path = root / "rank-reason-contract.json"
    phase = uuid4().hex[:10]
    abstracts = {r["doc_id"]: r["abstract"] for r in corpus["records"]}
    case_by_id = {c["case_id"]: c for c in tasks}
    with FileLock(trial.RUNTIME / "opencode-parallel/.shared-e5.lock", timeout=0):
        await wait_for_memory(trial.START_RESERVE, "rank-reason E5 startup")
        index, cache_hash = await asyncio.to_thread(
            trial.ready_index, root, tasks[0], corpus, index_type=trial.SharedEmbeddingIndex
        )
        contract = contract_for(root, suite, corpus, tasks, cache_hash)
        if contract_path.exists():
            if trial.read(contract_path) != contract:
                raise ValueError("rank-reason contract changed; use a new run name")
        else:
            trial.save_new(contract_path, contract)
        digest = trial.sha256_json(contract)
        jobs = [
            (case, arm)
            for i, case in enumerate(CASES)
            for arm in (ARMS if i % 2 == 0 else tuple(reversed(ARMS)))
            if trial.product.load_reusable_record(
                root / arm / f"{case}.json", resume=True, implementation_hash=digest
            )
            is None
        ]
        config = await asyncio.to_thread(trial.authorized_config)
        restart = None
        batches = []
        async with trial.AuditedLunaClient(2) as api:
            try:
                while jobs and not api.blocked:
                    try:
                        reading = await wait_for_memory(
                            trial.CASE_RESERVE, "rank-reason next cases", wait_s=LOADED_INDEX_WAIT_S
                        )
                    except MemoryBackpressure:
                        restart = "loaded_index_memory_backpressure"
                        break
                    width = 2 if min(reading.values()) >= PAIR_RESERVE else 1
                    batch, jobs = jobs[:width], jobs[width:]
                    batches.append({"subjects": batch, "memory": reading})
                    rows = await asyncio.gather(
                        *(
                            trial.one(
                                root,
                                case_by_id[case],
                                arm,
                                config,
                                api,
                                corpus,
                                abstracts,
                                index,
                                digest,
                                phase,
                                hide_rank_rationale=arm == "no_rank_reason",
                            )
                            for case, arm in batch
                        ),
                        return_exceptions=True,
                    )
                    for row in rows:
                        if isinstance(row, PilotBlocked):
                            restart = "memory_before_case_dispatch"
                        elif isinstance(row, Exception):
                            raise row
                        elif row["audit_shutdown"]["error"]:
                            restart = "audit_unsealed"
                    if restart:
                        break
            finally:
                trial.save_new(
                    root / f"rank-reason-http-{phase}.json",
                    {
                        "peak": api.peak,
                        "overlap": trial.overlap(api.rows),
                        "renew": restart,
                        "blocked": api.blocked,
                        "capacity": api.transitions,
                        "rows": api.rows,
                        "batches": batches,
                    },
                )
        if contract_for(root, suite, corpus, tasks, file_hash(index.cache_path)) != contract:
            raise ValueError("rank-reason inputs changed during the batch")
    print(json.dumps({"restart": restart, "provider_blocked": api.blocked}, ensure_ascii=False))
    return 2 if api.blocked else 12 if restart else 0


def observed_reasons(row):
    searched = [x for x in row.get("evidence_deliveries", []) if x["tool"] == "search_literature"]
    return sum(x["rank_reason_texts"] for x in searched), len(searched)


def direction_conflicts(rows):
    return sum(
        p["stance_correct"] is False and p["stance"] != "INSUFFICIENT" and not p["duplicate"]
        for row in rows
        for p in row["assessment"]["papers"]
    )


def unjudged_direction(row):
    return {
        (p["doc_id"], p["stance"])
        for p in row["assessment"]["papers"]
        if not p["official_evidence"] and not p["duplicate"] and p["stance"] != "INSUFFICIENT"
    }


def score(name):
    root, _, corpus, tasks = inputs(name)
    contract = trial.read(root / "rank-reason-contract.json")
    digest = trial.sha256_json(contract)
    by_id = {r["doc_id"]: r for r in corpus["records"]}
    missing, cases = [], []
    for task in tasks:
        cid = task["case_id"]
        paths = {arm: root / arm / f"{cid}.json" for arm in ARMS}
        if any(not path.is_file() for path in paths.values()):
            missing.extend([cid, arm] for arm, path in paths.items() if not path.is_file())
            continue
        arms = {arm: score_record(trial.read(path), task, corpus) for arm, path in paths.items()}
        if any(row.get("implementation_hash") != digest for row in arms.values()):
            raise ValueError("paired record belongs to another contract")
        disclosure = {arm: observed_reasons(row) for arm, row in arms.items()}
        for row in arms.values():
            row["target_readback"] = trial.evidence_losses(row, task, by_id)
        cases.append({"case": cid, "arms": arms, "disclosure": disclosure})
    per_arm = {arm: [c["arms"][arm] for c in cases] for arm in ARMS}
    metrics = {arm: aggregate(rows) for arm, rows in per_arm.items()}
    off, on = (metrics[a] for a in ARMS)
    quality = "correct_direction_evidence_coverage"
    gains = set()
    to_review = []
    task_by_id = {task["case_id"]: task for task in tasks}
    for c in cases:
        before, after = (c["arms"][a] for a in ARMS)
        x, y = (r["assessment"][quality] for r in (before, after))
        if (
            x is not None
            and y > x
            and all(trial.qualified(r, task_by_id[c["case"]], corpus) for r in (before, after))
        ):
            gains.add(after["family"])
        to_review.extend(
            [c["case"], doc, sign]
            for doc, sign in sorted(unjudged_direction(after) - unjudged_direction(before))
        )
    gain = on[quality] - off[quality] if off[quality] is not None else None

    def not_lower(metric):
        return off[metric] is not None and on[metric] >= off[metric]

    guard = {
        "quality_gain": gain is not None and gain >= 0.05,
        "distinct_gaining_families": len(gains) >= 2,
        "same_retrieval_engine": all(
            r["dense_retrieval"] and r["dense_common_score"]
            for rows in per_arm.values()
            for r in rows
        ),
        "valid_completions": on["completed"] >= off["completed"],
        "legal_output_format": on["format_valid"] >= off["format_valid"],
        "original_rationales": not_lower("complete_rationale_coverage"),
        "final_recall": not_lower("final_recall_at_10"),
        "direction_conflicts": direction_conflicts(per_arm[ARMS[1]])
        <= direction_conflicts(per_arm[ARMS[0]]),
        "source_identity": on["unknown_ids"] <= off["unknown_ids"],
        "verbatim_quotes": on["invalid_quotes"] <= off["invalid_quotes"],
        "reason_text_removed": bool(cases)
        and all(
            c["disclosure"][ARMS[0]][0] > 0
            and c["disclosure"][ARMS[1]][0] == 0
            and c["disclosure"][ARMS[1]][1] > 0
            for c in cases
        ),
    }
    result = {
        "systems": metrics,
        "missing": missing,
        "main_metric_gain": gain,
        "gaining_families": sorted(gains),
        "guardrails": guard,
        "pending_unjudged_direction_review": to_review,
        "screen_passed": not missing and not to_review and all(guard.values()),
    }
    report = root / f"rank-reason-assessment-{uuid4().hex[:10]}.json"
    trial.save_new(report, result | {"cases": cases, "contract": contract})
    return result | {"report": str(report)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "score"))
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if args.command == "run":
        rc = asyncio.run(run(args.name))
        if rc:
            return rc
    report = score(args.name)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 2 if report["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
