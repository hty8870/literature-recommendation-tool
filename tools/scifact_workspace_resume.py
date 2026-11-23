"""Resume the unchanged SciFact subjects with host-memory backpressure."""

from __future__ import annotations

import argparse
import asyncio
import json
import runpy
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from tools import scifact_workspace_retest as trial
from tools.benchmark.core import file_hash
from tools.gap_benchmark.opencode_parallel import PilotBlocked, memory_status

GIB = 1 << 30
PAIR_RESERVE = 3 * GIB
LOADED_INDEX_WAIT_S = 75


class MemoryBackpressure(PilotBlocked):
    def __init__(self, stage, reading):
        super().__init__(f"memory guard blocked {stage}; frozen checkpoints preserved")
        self.reading = reading


async def wait_for_memory(reserve, stage, *, wait_s=1200):
    stop = time.monotonic() + wait_s
    reported = False
    while True:
        reading = memory_status()
        if min(reading.values()) >= reserve:
            return reading
        if not reported:
            print(json.dumps({"waiting_for_memory": stage, "headroom": reading}), flush=True)
            reported = True
        remaining = stop - time.monotonic()
        if remaining <= 0:
            raise MemoryBackpressure(stage, reading)
        await asyncio.sleep(min(15, remaining))


async def resume(name, *, after_kimi):
    root, suite, corpus = trial.prepare_run(name)
    frozen = trial.read(root / "workspace-contract.json")
    tasks = {t["case_id"]: t for t in suite["tasks"] if t["case_id"] in frozen["cases"]}
    arms = frozen["arms"]
    if after_kimi:
        until = time.monotonic() + 5400
        print(json.dumps({"waiting_for": "baseline-summary.json", "name": name}), flush=True)
        while not (root / "baseline-summary.json").is_file():
            if time.monotonic() >= until:
                raise RuntimeError("Kimi has not released the batch; frozen checkpoints preserved")
            await asyncio.sleep(20)
    key = uuid4().hex[:10]
    abstracts = {r["doc_id"]: r["abstract"] for r in corpus["records"]}
    pilot = trial.read(root / "workspace-pilot.json")
    lock = trial.RUNTIME / "opencode-parallel/.shared-e5.lock"
    with FileLock(lock, timeout=0):
        await wait_for_memory(trial.START_RESERVE, "E5 startup")
        index, cache_hash = await asyncio.to_thread(
            trial.ready_index,
            root,
            tasks[pilot["case"]],
            corpus,
            index_type=trial.SharedEmbeddingIndex,
        )
        if trial.contract(root, suite, corpus, cache_hash, tasks, arms) != frozen:
            raise ValueError("the frozen product or benchmark changed; refusing to mix records")
        implementation = trial.sha256_json(frozen)
        config = await asyncio.to_thread(trial.authorized_config)
        harness = Path(__file__).with_name("scifact_workspace_retest.py").resolve()
        subject = runpy.run_path(str(harness))["one"]
        phase = {
            "after_kimi": after_kimi,
            "scheduler_sha256": file_hash(Path(__file__)),
            "subject_harness_sha256": file_hash(harness),
            "audit_drain_seconds": trial.product.MODEL_TIMEOUT_S + 10,
            "requires_process_restart": False,
            "batches": [],
        }
        async with trial.AuditedLunaClient(2) as api:

            async def run_subject(case, arm):
                return await subject(
                    root,
                    tasks[case],
                    arm,
                    config,
                    api,
                    corpus,
                    abstracts,
                    index,
                    implementation,
                    key,
                )

            try:
                jobs = [
                    (case, arm)
                    for i, case in enumerate(tasks)
                    for arm in (arms if i % 2 == 0 else arms[::-1])
                    if not (root / arm / f"{case}.json").exists()
                ]
                while jobs and not api.blocked:
                    try:
                        reading = await wait_for_memory(
                            trial.CASE_RESERVE, "next isolated case", wait_s=LOADED_INDEX_WAIT_S
                        )
                    except MemoryBackpressure as exc:
                        phase.update(
                            requires_process_restart=True,
                            restart_reason="loaded_index_memory_backpressure",
                            headroom=exc.reading,
                            pending_cases=len(jobs),
                        )
                        break
                    width = 2 if pilot["parallel"] and min(reading.values()) >= PAIR_RESERVE else 1
                    batch, jobs = jobs[:width], jobs[width:]
                    phase["batches"].append({"jobs": batch, "memory": reading})
                    results = await asyncio.gather(
                        *(run_subject(case, arm) for case, arm in batch), return_exceptions=True
                    )
                    for (case, arm), result in zip(batch, results, strict=True):
                        if isinstance(result, Exception):
                            if isinstance(result, PilotBlocked):
                                jobs.append((case, arm))
                            else:
                                raise result
                        elif (result.get("audit_shutdown") or {}).get("error"):
                            phase["requires_process_restart"] = True
                    if phase["requires_process_restart"]:
                        break
            finally:
                trial.save_new(
                    root / f"workspace-http-{key}.json",
                    phase
                    | {
                        "at": trial.utc_now_iso(),
                        "peak": api.peak,
                        "rows": api.rows,
                        "capacity": api.transitions,
                        "blocked": api.blocked,
                    },
                )
        if trial.contract(root, suite, corpus, file_hash(index.cache_path), tasks, arms) != frozen:
            raise ValueError("the frozen product or benchmark changed during execution")
    print(
        json.dumps(
            {
                "phase": key,
                "blocked": api.blocked,
                "requires_process_restart": phase["requires_process_restart"],
                "restart_reason": phase.get("restart_reason"),
                "http": dict(Counter(str(row["status"]) for row in api.rows)),
            }
        ),
        flush=True,
    )
    if api.blocked:
        return 2
    return 12 if phase["requires_process_restart"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--after-kimi", action="store_true")
    args = parser.parse_args()
    return asyncio.run(resume(args.name, after_kimi=args.after_kimi))


if __name__ == "__main__":
    raise SystemExit(main())
