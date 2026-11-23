"""Single-stage causal screen on actual historical SciFact product candidate pools."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage
from tools import luna_product_retest as product
from tools.benchmark.core import file_hash, secret_leak
from tools.gap_benchmark.data import RUNTIME, save_new
from tools.gap_benchmark.metrics import aggregate, mean, score_record
from tools.gap_benchmark.opencode_assessment import _read
from tools.gap_benchmark.opencode_baseline import MODELS, freeze, prepare
from tools.gap_benchmark.opencode_native import OpenCodeGateway, configured_route

from citation_tool.agent import _system_prompt, _user_prompt, final_answer_text
from citation_tool.agent_observations import abstract_evidence_observation, hide_rank_rationale
from citation_tool.entities import SearchCreate
from citation_tool.jcs import sha256_json

HISTORICAL = RUNTIME / "luna-retests/scifact-luna-v12-20261119/assessment-1.json"
ARMS = ("legacy", "without_placeholder", "without_rationale", "raw_abstracts", "raw_without_skill")
NOTE = (
    "这是一轮固定候选的摘要判读。没有可用的额外检索；只用下面各候选的实际题名和摘要。"
    "摘要判读与全文引用资格无关；不能声称核验了全文。"
)
RAW_FIELDS = ("work_id", "title", "evidence_excerpt", "base_rank", "llm_rank")


def cohort(tasks: list[dict]) -> list[dict]:
    return [
        task
        for label, quota in (("multi_document", 4), ("support", 4), ("contradict", 4))
        for task in [row for row in tasks if row["slice"] == label][:quota]
    ]


def source_material(queries: list[dict]) -> list:
    return [
        [[entry[key] for key in RAW_FIELDS] for entry in query["candidate_diagnostics"]]
        for query in queries
    ]


def observation(queries: list[dict], arm: str) -> list[dict]:
    material = copy.deepcopy(queries)
    if arm == "without_placeholder":
        material = [abstract_evidence_observation(query, "separate") for query in material]
    elif arm == "without_rationale":
        material = [hide_rank_rationale(query) for query in material]
    elif arm in {"raw_abstracts", "raw_without_skill"}:
        material = [
            {
                "query_id": query["query_id"],
                "candidate_diagnostics": [
                    {key: item[key] for key in RAW_FIELDS}
                    for item in query["candidate_diagnostics"]
                ],
            }
            for query in material
        ]
    if source_material(queries) != source_material(material):
        raise ValueError("ablation changed source identity, text, order, or ranks")
    return material


def checked_pool(row: dict, sources: dict) -> list[dict]:
    result = row["queries"]
    if not result or not any(query["candidate_diagnostics"] for query in result):
        raise ValueError("empty recorded product pool")
    for query in result:
        if query.get("results"):
            raise ValueError("analysis is confined to abstract diagnostics")
        for item in query["candidate_diagnostics"]:
            source = sources[item["work_id"].removeprefix("work:scifact:")]
            if item["title"] != source["title"] or item["evidence_excerpt"] != source["abstract"]:
                raise ValueError("recorded abstract or title differs from the frozen source")
            if any(
                origin.get("abstract") != source["abstract"]
                for origin in item.get("source_records", [])
            ):
                raise ValueError("raw arm would hide a variant source")
    return result


def messages(task: dict, queries: list[dict], arm: str, root: Path) -> list:
    request = SearchCreate(**task["request"])
    service = SimpleNamespace(
        settings=product.product_settings(root, task), adapter_mode="metadata"
    )
    system = (
        "判断给定论文摘要对用户论断的证据方向，遵守用户指定的输出契约。"
        if arm == "raw_without_skill"
        else _system_prompt(request, service, reason_model_configured=True)
    )
    user = _user_prompt(
        request, extra_blocks=observation(queries, arm), user_message=task["baseline_query"]
    )
    return [SystemMessage(system), HumanMessage(f"{NOTE}\n\n{user}")]


def metrics(rows: list[dict], arms: tuple[str, ...] = ARMS) -> dict:
    total = {arm: aggregate([row for row in rows if row["arm"] == arm]) for arm in arms}
    grouped = {arm: {r["case_id"]: r for r in rows if r["arm"] == arm} for arm in arms}
    field = "correct_direction_evidence_coverage"
    contrasts = {}
    for before, after in (
        ("legacy", "without_placeholder"),
        ("legacy", "without_rationale"),
        ("legacy", "raw_abstracts"),
        ("raw_abstracts", "raw_without_skill"),
    ):
        if before not in grouped or after not in grouped:
            continue
        first, second = grouped[before], grouped[after]
        matched = sorted(first.keys() & second.keys())
        delta = [
            second[case]["assessment"][field] - first[case]["assessment"][field] for case in matched
        ]
        contrasts[f"{before} -> {after}"] = {
            "cases": len(matched),
            "delta": mean(delta),
            "wins": sum(d > 0 for d in delta),
            "ties": sum(d == 0 for d in delta),
            "losses": sum(d < 0 for d in delta),
            "per_case": dict(zip(matched, delta, strict=True)),
        }
    return {"systems": total, "contrasts": contrasts}


async def run(name: str, model: str, *, resume: bool, confirm_rationale: bool = False) -> dict:
    root, suite, corpus, run_plan = prepare(name, model)
    output = root / "fixed-pool"
    sources = {row["doc_id"]: row for row in corpus["records"]}
    historical = {r["case_id"]: r for r in _read(HISTORICAL)["records"] if r["system"] == "product"}
    if confirm_rationale:
        cases = {"sf-1187", "sf-5", "sf-922", "sf-793"}
        tasks = [task for task in suite["tasks"] if task["case_id"] in cases]
        arms = ("legacy", "without_rationale")
        expected = len(cases)
    else:
        tasks, arms, expected = cohort(suite["tasks"]), ARMS, 12
    if len({task["family"] for task in tasks}) != expected:
        raise ValueError("unexpected development cohort family count")
    pools = {task["case_id"]: checked_pool(historical[task["case_id"]], sources) for task in tasks}
    plan = {
        "schema": "scifact-opencode-presentation-screen-v1",
        "parent_plan": sha256_json(run_plan),
        "code": file_hash(Path(__file__)),
        "input_assessment": file_hash(HISTORICAL),
        "cases": [
            {"case": task["case_id"], "pool_hash": sha256_json(pools[task["case_id"]])}
            for task in tasks
        ],
        "arms": arms,
        "followup_rationale_only": confirm_rationale,
        "computation": "fresh single native response, same model and defaults, no retrieval tools",
        "scope": "reconstructed stored Luna product pools; not live E2E or an unseen holdout",
        "design": "same candidate IDs, order, full abstracts, ranks, and user task",
        "comparisons": {
            "without_placeholder": "remove only abstract relationship=insufficient placeholder",
            "without_rationale": "remove only model-generated reason.text",
            "raw_abstracts": "keep only title/id/abstract and the two ranks; composite projection",
            "raw_without_skill": "same raw projection but replace entire product system prompt",
        },
    }
    freeze(output / "plan.json", json.loads(json.dumps(plan)))
    route = await asyncio.to_thread(configured_route, model)
    records = []
    for original in tasks:
        case = original["case_id"]
        task = {k: v for k, v in original.items() if k != "expect"}
        queries = pools[case]
        ids = {
            item[0].removeprefix("work:scifact:")
            for batch in source_material(queries)
            for item in batch
        }
        for arm in sorted(
            arms, key=lambda item: hashlib.sha256(f"{case}:{item}".encode()).digest()
        ):
            path = output / "records" / f"{case}.{arm}.json"
            prompt = messages(task, queries, arm, output)
            prompt_hash = sha256_json([message.text for message in prompt])
            if path.exists():
                if not resume:
                    raise FileExistsError(path)
                record = _read(path)
                if record["inputs_sha256"] != prompt_hash:
                    raise ValueError("replay inputs changed")
            else:
                gateway = OpenCodeGateway(route)
                text, error = "", None
                start = time.monotonic()
                try:
                    response = await gateway.model_for("parent", json_mode=False).ainvoke(prompt)
                    text = final_answer_text(response) or ""
                    if not text or response.response_metadata.get("finish_reason") == "max_tokens":
                        error = "NO_TERMINAL_OUTPUT"
                except Exception as exc:
                    error = getattr(exc, "code", type(exc).__name__)
                finally:
                    await gateway.aclose()
                audit = gateway.audit()
                leaked = secret_leak(text, [route.api_key])
                record = {
                    "case_id": case,
                    "run_case_id": case,
                    "system": "replay",
                    "arm": arm,
                    "family": original["family"],
                    "inputs_sha256": prompt_hash,
                    "model_valid": audit["model_valid"],
                    "observed_model": audit["served_models"],
                    "completed": bool(text) and not error and not leaked,
                    "response_text": text if not leaked else "[WITHHELD]",
                    "error": error,
                    "secret_leak": leaked,
                    "wall_time_ms": round((time.monotonic() - start) * 1000, 3),
                    "call_metadata": audit["artifacts"],
                }
                save_new(path, record)
            assessed = score_record(record, original, corpus)
            assessment = assessed["assessment"]
            targets = original["expect"]["evidence"]
            off_pool = [row["doc_id"] for row in assessment["papers"] if row["doc_id"] not in ids]
            covered = {
                row["doc_id"]
                for row in assessment["papers"]
                if row["doc_id"] in ids and row["stance_correct"] and row["exact_quote"]
            }
            assessment["out_of_pool"] = off_pool
            assessment["correct_direction_evidence_coverage"] = (
                len(covered) / len(targets) if targets and assessment["valid_completion"] else 0.0
            )
            records.append(assessed)
            print(
                json.dumps(
                    {
                        "case": case,
                        "arm": arm,
                        "error": record["error"],
                        "completed": assessment["valid_completion"],
                        "format": assessment["format_valid"],
                        "coverage": assessment["correct_direction_evidence_coverage"],
                    }
                ),
                flush=True,
            )
    result = metrics(records, arms)
    save_new(output / "assessment.json", {**result, "plan": plan, "records": records})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--model", required=True, choices=MODELS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm-rationale", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(
        run(args.name, args.model, resume=args.resume, confirm_rationale=args.confirm_rationale)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
