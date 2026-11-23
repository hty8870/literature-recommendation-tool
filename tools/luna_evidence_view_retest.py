"""Frozen same-model A/B for the parent Agent's candidate evidence view."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import replace
from functools import partial
from pathlib import Path

from tools import luna_native_product_retest as native
from tools import luna_product_retest as product
from tools.benchmark.core import ROOT, file_hash
from tools.gap_benchmark.data import make_task, save_new
from tools.gap_benchmark.metrics import aggregate, score_record
from tools.luna_credentials import authorized_config
from tools.luna_gap_retest import first_loss_by_target, prepare_run

from citation_tool.agent_observations import EVIDENCE_VIEW_VERSION, evidence_observation
from citation_tool.jcs import sha256_json

V3 = "scientific-gap-luna-native-dev-20261118-v3-window10-doi"
V3_HASH = "fe707fc22a80b647d4106c6d0f6bade71dc43afe42841b65d77e584cb86f6ff0"
DEVELOPMENT_IDS = ("sf-452", "sf-179", "sf-873", "sf-1029")
SEED = "scifact-evidence-combination-v1-20261118"
SLICES = (("multi_document", 4), ("support", 1), ("contradict", 1))
ARMS = ("legacy", "evidence")


def _family_docs(claim: dict) -> set[int]:
    return {int(item) for item in (*claim["cited_doc_ids"], *claim["evidence"])}


def select_challenge(suite: dict, corpus: dict) -> list[dict]:
    old = {row["claim_id"] for row in suite["tasks"]}
    used = set().union(*(_family_docs(row) for row in suite["all_claims"] if row["id"] in old))
    ranked = sorted(
        suite["all_claims"], key=lambda row: hashlib.sha256(f"{SEED}:{row['id']}".encode()).digest()
    )
    selected = []
    for group, quota in SLICES:
        taken = 0
        for row in ranked:
            count = len(row["evidence"])
            labels = {item["label"] for rows in row["evidence"].values() for item in rows}
            eligible = (
                count >= 2
                if group == "multi_document"
                else count == 1 and labels == {group.upper()}
            )
            docs = _family_docs(row)
            if not eligible or row["id"] in old or not docs or docs & used:
                continue
            selected.append(
                make_task(row, group, "new-family-" + sha256_json(sorted(docs))[:12], corpus)
            )
            used.update(docs)
            taken += 1
            if taken == quota:
                break
        if taken != quota:
            raise ValueError(f"Not enough distinct {group} families: {taken}/{quota}")
    return selected


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_path(root: Path, case: str) -> Path:
    return root.parent / V3 / "product" / f"{case}.json"


def prepare(name: str) -> tuple[Path, dict, dict, list[dict], list[dict]]:
    root, suite, corpus = prepare_run(name)
    product_contract = native.product_contract(root, suite, corpus)
    product.ensure_product_contract(root, product_contract)
    old = {task["case_id"]: task for task in suite["tasks"]}
    development = [old[case] for case in DEVELOPMENT_IDS]
    challenge = select_challenge(suite, corpus)
    controls = {}
    for case in DEVELOPMENT_IDS:
        path = _legacy_path(root, case)
        record = _read(path)
        if record["implementation_hash"] != V3_HASH or not record["model_valid"]:
            raise ValueError(f"History mismatch: {case}")
        controls[case] = file_hash(path)
    plan = {
        "schema": "parent-evidence-view-ab-v1",
        "runner_sha256": file_hash(Path(__file__)),
        "implementation_hash": product_contract["implementation_hash"],
        "evidence_version": EVIDENCE_VIEW_VERSION,
        "actual_model": native.MODEL,
        "corpus_sha256": corpus["corpus_sha256"],
        "seed": SEED,
        "new_cases": [row["case_id"] for row in challenge],
        "challenge_sha256": sha256_json(challenge),
        "old_baseline_exact_sha256": controls,
        "scope": (
            "Same OpenAI Responses and full application, changing only the evidence-view switch. "
            "Old cases are development; new families are a small uninspected end-to-end challenge "
            "from public SciFact dev, not a new corpus or proof of model generalization."
        ),
        "default_gate": (
            "Enable only if new multi-document correct-direction verbatim evidence improves, "
            "new-cases completion and Recall@10 do not drop, and wrong official directions, "
            "invalid quotes, unknown or duplicate IDs do not increase. Otherwise leave opt-in."
        ),
    }
    path = root / "evidence-view-plan.json"
    if path.exists():
        if _read(path) != plan:
            raise ValueError("The evidence-view plan or a frozen artifact changed")
    else:
        save_new(path, plan)
    return root, plan, corpus, development, challenge


def _paired_jobs(development: list[dict], challenge: list[dict]):
    for index, task in enumerate(challenge):
        arms = ARMS if index % 2 == 0 else ARMS[::-1]
        for arm in arms:
            yield "challenge", arm, task
    for task in development:
        yield "development", "evidence", task


def _destination(root: Path, split: str, arm: str, case: str) -> Path:
    return root / "evidence-view" / split / arm / f"{case}.json"


def _builder(settings, adapter, gateway, callback, *, enabled: bool):
    return native.build_product_app(
        replace(settings, agent_evidence_view=enabled), adapter, gateway, callback
    )


async def run(name: str, resume: bool) -> None:
    root, plan, corpus, development, challenge = prepare(name)
    frozen = _read(ROOT / "snapshot-manifest.json")
    if frozen["name"] != name or frozen["implementation_hash"] != plan["implementation_hash"]:
        raise ValueError("Run in the matching frozen code snapshot")
    config = await asyncio.to_thread(authorized_config)
    settings = product.product_settings(root / "_preflight", challenge[0])
    index = await asyncio.to_thread(product.require_prebuilt_index, corpus, settings)
    slots = native.NativeModelSlots()
    for split, arm, task in _paired_jobs(development, challenge):
        case = task["case_id"]
        path = _destination(root, split, arm, case)
        run_hash = sha256_json({"plan": plan, "split": split, "arm": arm})
        if product.load_reusable_record(path, resume=resume, implementation_hash=run_hash):
            continue
        runtime = product.new_attempt_dir(root, f"evidence-view-{split}-{arm}-{case}")
        record = await native.run_case(
            task,
            root=root,
            runtime=runtime,
            corpus=corpus,
            index=index,
            implementation_hash=run_hash,
            config=config,
            slots=slots,
            app_builder=partial(_builder, enabled=arm == "evidence"),
        )
        save_new(path, record)
        print(
            json.dumps(
                {
                    "split": split,
                    "arm": arm,
                    "case": case,
                    "completed": record["completed"],
                    "model_verified": record["model_valid"],
                    "error": record["error"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if (record["error"] or {}).get("code") == "NATIVE_MODEL_CLEANUP_FAILED":
            raise RuntimeError("Unfinished model work preserved; not advancing to the next case")
        await slots.wait_idle()
    if native.product_implementation_hash(root, corpus) != plan["implementation_hash"]:
        raise ValueError("Frozen implementation changed during the run")


def evaluate(name: str) -> dict:
    root, plan, corpus, development, challenge = prepare(name)
    result = {"plan": plan, "metrics": {}, "case_comparisons": [], "records": []}
    for split, tasks in (("challenge", challenge), ("development", development)):
        views = {arm: [] for arm in ARMS}
        for task in tasks:
            case = task["case_id"]
            row = {"split": split, "case": case, "slice": task["slice"]}
            for arm in ARMS:
                path = (
                    _legacy_path(root, case)
                    if split == "development" and arm == "legacy"
                    else _destination(root, split, arm, case)
                )
                record = _read(path)
                expected_hash = (
                    V3_HASH
                    if split == "development" and arm == "legacy"
                    else sha256_json({"plan": plan, "split": split, "arm": arm})
                )
                if record["case_id"] != case or record["implementation_hash"] != expected_hash:
                    raise ValueError(f"Result does not match its frozen plan: {path.name}")
                assessed = score_record(record, task, corpus)
                assessed["first_loss_by_target"] = first_loss_by_target(assessed, task)
                views[arm].append(assessed)
                result["records"].append({**assessed, "cohort": split, "arm": arm})
                row[arm] = {
                    key: assessed["assessment"][key]
                    for key in (
                        "correct_direction_evidence_coverage",
                        "complete_rationale_coverage",
                        "wrong_official_stances",
                        "valid_completion",
                    )
                }
            result["case_comparisons"].append(row)
        result["metrics"][split] = {arm: aggregate(rows) for arm, rows in views.items()}
        if split == "challenge":
            result["metrics"]["new_multi_document"] = {
                arm: aggregate([row for row in rows if row["slice"] == "multi_document"])
                for arm, rows in views.items()
            }
    fresh = result["metrics"]["challenge"]
    multi = result["metrics"]["new_multi_document"]
    result["default_gate_passed"] = (
        multi["evidence"]["correct_direction_evidence_coverage"]
        > multi["legacy"]["correct_direction_evidence_coverage"]
        and all(
            fresh["evidence"][key] >= fresh["legacy"][key]
            for key in ("completed", "final_recall_at_10")
        )
        and all(
            fresh["evidence"][key] <= fresh["legacy"][key]
            for key in (
                "wrong_official_stances",
                "invalid_quotes",
                "unknown_ids",
                "duplicate_ids",
            )
        )
    )
    old_rows = [
        row for row in result["records"] if row["case_id"] == "sf-452" and row["arm"] == "legacy"
    ]
    query = old_rows[0]["queries"][0]
    compact = evidence_observation(query)
    result["same_sf452_projection"] = {
        "candidates": len(query["candidate_diagnostics"]),
        "detailed_candidate_chars": len(
            json.dumps(query["candidate_diagnostics"], ensure_ascii=False)
        ),
        "evidence_candidate_chars": len(
            json.dumps(compact["candidate_diagnostics"], ensure_ascii=False)
        ),
        "not_provider_token_usage": True,
    }
    save_new(root / "evidence-view-assessment.json", result)
    return {key: value for key, value in result.items() if key != "records"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "evaluate"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.action == "run":
        asyncio.run(run(args.name, args.resume))
    else:
        data = prepare(args.name)[1] if args.action == "prepare" else evaluate(args.name)
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
