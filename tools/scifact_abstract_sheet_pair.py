"""Paired Luna readback of real, frozen SciFact result artifacts; no retrieval scores."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from tools import scifact_workspace_retest as trial
from tools.benchmark.core import file_hash, secret_leak
from tools.gap_benchmark.data import load_data
from tools.gap_benchmark.metrics import aggregate, score_record
from tools.luna_native_product_retest import build_product_app, collect_model_audit
from tools.quality_benchmark.core import artifact_hash, output_details

from citation_tool.agent import AgentRunCreate
from citation_tool.entities import AgentRun, utc_now_iso
from citation_tool.retrieval import SourceSearchError

BATCH = trial.HISTORY.with_name("scifact-workspace-20261121")
SOURCES = {
    "sf-873": (BATCH, "workspace"),
    "sf-1029": (BATCH, "default"),
    "sf-452": (trial.HISTORY, "product"),
    "sf-179": (trial.HISTORY, "product"),
}
SCOPE = "仅用本会话已给出的既有榜单及所载原始摘要回答；不要发起新的检索或查阅库中其他论文。"


class ArtifactOnlyAdapter:
    simulation = False
    index_snapshot = "settled-artifact-only"
    model_versions = {}

    def __init__(self, library):
        self.library = library

    def search(self, claim):
        raise SourceSearchError("ARTIFACT_ONLY", "No new retrieval in this readback comparison")


def backup(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as opened,
        closing(sqlite3.connect(destination)) as copy,
    ):
        opened.backup(copy)


def prepare(task, arm, root, config, api, abstracts, *, show_older):
    case = task["case_id"]
    folder = root / "runs" / case / arm
    folder.mkdir(parents=True, exist_ok=False)
    source_root, directory = SOURCES[case]
    reference = source_root / directory / f"{case}.json"
    source = trial.read(reference)
    query_ids = [q["query_id"] for q in source["queries"] if q["candidate_diagnostics"]]
    db = (source_root / source["db_path"]).resolve(strict=True)
    if not db.is_relative_to(source_root.resolve()) or not source["completed"] or not query_ids:
        raise ValueError("replay requires genuine completed public benchmark artifacts")
    settings = replace(
        trial.product.product_settings(folder, task),
        embedding_enabled=False,
        agent_research_workspace=True,
        agent_research_context=True,
        agent_workspace_abstracts=show_older or arm == "on",
    )
    backup(db, settings.db_path)
    audit = trial.EvidenceAudit(folder / "model-calls", abstracts)
    gateway = trial.ParallelLunaGateway(config, api, audit)
    adapter = ArtifactOnlyAdapter(settings.libraries[0])
    app, _, _ = build_product_app(settings, adapter, gateway, None)
    tenant = f"luna-product-{case}"
    states = [app.state.search_service.state(tenant, q) for q in query_ids]
    if any(state is None or state.is_live for state in states):
        raise ValueError("replay artifact is missing or has not settled")
    pool_hash = trial.sha256_json([output_details(state) for state in states])
    now = utc_now_iso()
    pending = AgentRun(
        run_id=f"agt_{uuid4().hex}",
        tenant_id=tenant,
        request=AgentRunCreate.model_validate(task["request"]),
        status="pending",
        provider="openai",
        model=trial.MODEL,
        user_message=task["baseline_query"] + "\n" + SCOPE,
        active_query_id=query_ids[-1],
        prior_query_ids=query_ids,
        created_at=now,
        updated_at=now,
    )
    app.state.store.create_agent_run(pending, None)
    return folder, app, audit, pending, pool_hash, file_hash(reference)


async def one(root, task, arm, config, api, abstracts, corpus, by_id, *, show_older):
    folder, app, audit, pending, pool_hash, origin = await asyncio.to_thread(
        prepare, task, arm, root, config, api, abstracts, show_older=show_older
    )
    earlier = [q for q in pending.prior_query_ids if q != pending.active_query_id]
    blocks = (
        [
            {
                "earlier_same_question_abstracts": [f"searches/{q}/abstracts.txt" for q in earlier],
                "notice": "同一问题的其他独立榜单可按需回查，分数不能跨榜比较。",
            }
        ]
        if earlier and show_older and arm == "on"
        else []
    )
    key = trial.SUBJECT.set(f"{task['case_id']}/{arm}")
    started = time.monotonic()
    try:
        async with app.router.lifespan_context(app):
            await app.state.agent_runner._execute(pending, context_blocks=blocks)
            run = app.state.store.get_agent_run(pending.tenant_id, pending.run_id)
        await audit.slots.wait_idle()
        audit.seal()
    finally:
        trial.SUBJECT.reset(key)
    if run is None:
        raise ValueError("no saved AgentRun")
    models = collect_model_audit(folder / "model-calls")
    answer = run.summary or ""
    leak = secret_leak(answer, [config.api_key])
    scope_ok = not run.queries and all(c.name in {"Read", "Grep", "Glob"} for c in run.tool_calls)
    record = {
        "case_id": task["case_id"],
        "run_case_id": task["case_id"],
        "arm": arm,
        "system": "product",
        "pool_sha256": pool_hash,
        "previous_record_sha256": origin,
        "model_observed": models["served_models"],
        "model_valid": models["model_valid"],
        "completed": run.status == "completed" and scope_ok,
        "scope_valid": scope_ok,
        "error": (run.error or {}).get("code"),
        "secret_leak": leak,
        "response_text": answer if not leak else "",
        "wall_time_ms": (time.monotonic() - started) * 1000,
        "queries": [],
        "evidence_deliveries": audit.deliveries,
        "tool_actions": [call.name for call in run.tool_calls],
    }
    row = score_record(record, task, corpus)
    row["target_readback"] = trial.evidence_losses(row, task, by_id)
    trial.save_new(folder / "record.json", row)
    print(
        json.dumps(
            {
                "case": record["case_id"],
                "arm": arm,
                "completed": record["completed"],
                "tools": record["tool_actions"],
                "coverage": row["assessment"]["correct_direction_evidence_coverage"],
            }
        ),
        flush=True,
    )
    return row


async def run(name, *, show_older):
    suite, corpus = load_data()
    tasks = [t for t in suite["tasks"] if t["case_id"] in SOURCES]
    if show_older:
        tasks = [
            t
            for t in tasks
            if len(
                trial.read(
                    SOURCES[t["case_id"]][0] / SOURCES[t["case_id"]][1] / f"{t['case_id']}.json"
                )["queries"]
            )
            > 1
        ]
    by_id = {r["doc_id"]: r for r in corpus["records"]}
    abstracts = {key: row["abstract"] for key, row in by_id.items()}
    root = trial.RUNTIME / "workspace-abstract-sheet" / name
    root.mkdir(parents=True, exist_ok=False)
    plan = {
        "code": artifact_hash(),
        "runner": file_hash(Path(__file__)),
        "cases": [task["case_id"] for task in tasks],
        "corpus_sha256": corpus["corpus_sha256"],
        "task_sha256": file_hash(trial.SUITE_ROOT / "tasks.json"),
        "scope": SCOPE,
        "model": trial.MODEL,
        "default_reasoning": True,
        "store": False,
        "only_delta": (
            "disclose earlier same-question artifact paths; batch reading enabled in both arms"
            if show_older
            else "CITATION_AGENT_WORKSPACE_ABSTRACTS"
        ),
        "pairs": {
            case: file_hash(folder / directory / f"{case}.json")
            for case, (folder, directory) in SOURCES.items()
        },
        "primary": "original labeled direction + exact quote; both arms get same settled artifacts",
        "boundary": "development readback, no new corpus retrieval, not an end-to-end result",
    }
    trial.save_new(root / "plan.json", plan)
    config = await asyncio.to_thread(trial.authorized_config)
    systems = defaultdict(list)
    async with trial.AuditedLunaClient(2) as api:
        try:
            for task in tasks:
                if api.blocked:
                    break
                arms = ("off", "on") if len(systems["off"]) % 2 == 0 else ("on", "off")
                a, b = await asyncio.gather(
                    *(
                        one(
                            root,
                            task,
                            arm,
                            config,
                            api,
                            abstracts,
                            corpus,
                            by_id,
                            show_older=show_older,
                        )
                        for arm in arms
                    )
                )
                if (
                    a["pool_sha256"] != b["pool_sha256"]
                    or a["previous_record_sha256"] != b["previous_record_sha256"]
                ):
                    raise ValueError("paired views did not start from identical result artifacts")
                for row in (a, b):
                    systems[row["arm"]].append(row)
        finally:
            trial.save_new(root / "http.json", {"peak": api.peak, "rows": api.rows})
    if artifact_hash() != plan["code"] or file_hash(Path(__file__)) != plan["runner"]:
        raise ValueError("product changed during comparison")
    result = {
        "systems": {arm: aggregate(systems[arm]) for arm in ("off", "on")},
        "blocked": api.blocked,
        "intended_per_arm": len(tasks),
        "http_status": dict(Counter(str(r["status"]) for r in api.rows)),
        "readback": {
            arm: [
                {
                    "case": row["case_id"],
                    "tools": row["tool_actions"],
                    "sheet_receipts": sum(
                        r["kind"] == "candidate_abstracts" for r in row["evidence_deliveries"]
                    ),
                    "targets": row["target_readback"],
                }
                for row in systems[arm]
            ]
            for arm in ("off", "on")
        },
    }
    trial.save_new(root / "assessment.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--show-older", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", args.name):
        parser.error("invalid --name")
    result = asyncio.run(run(args.name, show_older=args.show_older))
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "readback"}, ensure_ascii=False, indent=2
        )
    )
    return 2 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
