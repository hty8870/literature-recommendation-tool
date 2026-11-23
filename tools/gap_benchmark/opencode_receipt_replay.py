"""Compare one presentation change on receipts the Qwen parent actually received."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from filelock import FileLock
from langchain_core.messages import HumanMessage, SystemMessage
from tools.benchmark.core import file_hash
from tools.gap_benchmark import opencode_budget, opencode_native
from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import aggregate, mean
from tools.gap_benchmark.opencode_assessment import _read
from tools.gap_benchmark.opencode_baseline import freeze

from citation_tool.jcs import sha256_json

MODEL = opencode_budget.MODEL
ARMS = ("control", "trial")
CASES = {
    "format": ("sf-742", "sf-219", "sf-1086", "sf-1137"),
    "abstract-references": ("sf-589", "sf-179", "sf-1086", "sf-1137"),
}
OFFLINE_NOTE = (
    "下面是该问题实际返回的工具回执。本次不能再次调用工具；请依据这些资料按用户的要求交付。"
)
JSON_GUIDANCE = (
    "仅当用户明确要求 JSON 时，返回一个完整的合法 JSON 值，严格使用用户要求的字段与类型；"
    "不在 JSON 前后加叙述，不额外加键。引文及其他字符串内的双引号、换行和反斜线需按 JSON 转义；"
    "解码后的逐字引文必须仍是原文。理由、证据边界及其他说明放在用户允许的字段内。"
)
REFERENCE_GUIDANCE = (
    "摘要字段中的 abstract_ref 指向较早回执中同一 work_id 的逐字相同原文。"
    "按 query_id、work_id 与 field 找回原文；引用的是原文，不是引用标记。"
    "不同来源文字若有区别仍分别给出，名次、来源或出版状态以各自回执为准。"
)


def actual_observations(trace: list[dict], abstracts: dict[str, str]) -> tuple[dict, set[str]]:
    turn = next(t for t in reversed(trace) if t["tool_results"])
    seen = set()
    for tool in turn["tool_results"]:
        result = tool["content"]
        if not isinstance(result, dict) or "candidate_diagnostics" not in result:
            continue
        if tool["name"] != "search_literature" or result.get("simulation") is not False:
            raise ValueError("replay only supports actual public abstract search receipts")
        if result.get("results"):
            raise ValueError("replay does not support citable results or private evidence")
        for candidate in result["candidate_diagnostics"]:
            work = candidate["work_id"]
            source = abstracts[work]
            if candidate.get("evidence_excerpt") != source or any(
                s.get("abstract") != source for s in candidate.get("source_records", [])
            ):
                raise ValueError("receipt text differs from frozen SciFact")
            seen.add(work.removeprefix("work:scifact:"))
    if not seen:
        raise ValueError("no article evidence in actual parent input")
    return turn, seen


def same_abstract_refs(tool_results: list[dict]) -> tuple[list[dict], int]:
    projection = []
    observed: dict[tuple[str, str], dict[str, str]] = {}
    count = 0
    for tool in tool_results:
        result = tool["content"]
        if not isinstance(result, dict) or not result.get("candidate_diagnostics"):
            projection.append(tool)
            continue
        query = result["query_id"]
        candidates = []
        for item in result["candidate_diagnostics"]:
            work, text = item["work_id"], item["evidence_excerpt"]
            key = (work, text)
            current = {"query_id": query, "work_id": work, "field": "evidence_excerpt"}
            entry = dict(item)
            origin = observed.get(key, current)
            if origin != current:
                del entry["evidence_excerpt"]
                entry["abstract_ref"] = origin
                count += 1
            sources = []
            for source in item.get("source_records", []):
                if source.get("abstract") == text:
                    source = {k: v for k, v in source.items() if k != "abstract"}
                    source["abstract_ref"] = origin
                    count += 1
                sources.append(source)
            if "source_records" in entry:
                entry["source_records"] = sources
            candidates.append(entry)
            observed.setdefault(key, current)
        projection.append({**tool, "content": {**result, "candidate_diagnostics": candidates}})
    return projection, count


def messages(turn: dict, contrast: str, arm: str) -> tuple[list, int]:
    system = "\n\n".join(turn["system"])
    user = "\n\n".join(turn["user"])
    receipts, references = turn["tool_results"], 0
    if arm == "trial":
        if contrast == "format":
            system += f"\n\n{JSON_GUIDANCE}"
        else:
            receipts, references = same_abstract_refs(receipts)
            system += f"\n\n{REFERENCE_GUIDANCE}"
    material = json.dumps(receipts, ensure_ascii=False, separators=(",", ":"))
    return [
        SystemMessage(system),
        HumanMessage(f"{user}\n\n{OFFLINE_NOTE}\n\n{material}"),
    ], references


async def run(name: str, contrast: str, concurrency: int, *, resume: bool) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", name):
        raise ValueError("invalid replay name")
    path = RUNTIME / "opencode-receipt-replay" / name
    with FileLock(path / ".run.lock", timeout=0):
        return await _run(path, contrast, concurrency, resume=resume)


async def _run(path: Path, contrast: str, concurrency: int, *, resume: bool) -> dict:
    root = RUNTIME / "opencode-retests" / "opencode-flash-retest-v1" / MODEL
    suite, corpus = load_data()
    cases = CASES[contrast]
    originals = {t["case_id"]: t for t in suite["tasks"] if t["case_id"] in cases}
    if len({t["family"] for t in originals.values()}) != len(cases):
        raise ValueError("use distinct known development families")
    abstracts = {row["work_id"]: row["abstract"] for row in corpus["records"]}
    trace_hashes, prompts, input_hashes, ids = {}, {}, {}, {}
    contract = _read(root / "product-contract.json")
    if contract["model"] != MODEL:
        raise ValueError("replay uses an unexpected model contract")
    for case in cases:
        record = _read(root / "product" / f"{case}.json")
        if (
            record["secret_leak"]
            or record["gold_sent_to_model"]
            or record["expected_model"] != MODEL
            or record["model_observed"] != [MODEL]
            or record["implementation_hash"] != contract["implementation_hash"]
        ):
            raise ValueError("input is not a checked public replay record")
        trace = (root / record["parent_trace_path"]).resolve()
        if not trace.is_relative_to(root.resolve()):
            raise ValueError("parent trace is outside the frozen run")
        trace_hashes[case] = file_hash(trace)
        turn, ids[case] = actual_observations(_read(trace), abstracts)
        for arm in ARMS:
            prompt, refs = messages(turn, contrast, arm)
            prompts[case, arm] = (prompt, refs)
            input_hashes[f"{case}.{arm}"] = sha256_json([item.text for item in prompt])
    order = [
        (case, arm) for i, case in enumerate(cases) for arm in ARMS[:: 1 if i % 2 == 0 else -1]
    ]
    plan = {
        "schema": "scifact-opencode-actual-receipt-replay-v1",
        "model": MODEL,
        "case_concurrency": concurrency,
        "contrast": contrast,
        "order": [list(key) for key in order],
        "development_only": True,
        "gold_sent_to_model": False,
        "scope": "same visible prior user/system/receipts, no tools or hidden continuations",
        "max_tokens": opencode_native.TOKEN_LIMITS["parent"],
        "source_contract_sha256": file_hash(root / "product-contract.json"),
        "corpus_sha256": corpus["corpus_sha256"],
        "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "trace_sha256": trace_hashes,
        "input_sha256": input_hashes,
        "code_sha256": {
            name: file_hash(Path(__file__).with_name(name))
            for name in (
                Path(__file__).name,
                "opencode_budget.py",
                "opencode_native.py",
                "metrics.py",
            )
        },
    }
    freeze(path / "plan.json", plan)
    fingerprint = sha256_json(plan)
    route = await asyncio.to_thread(opencode_native.configured_route, MODEL)
    slots = asyncio.Semaphore(concurrency)
    throttled = asyncio.Event()

    async def run_arm(case, arm):
        key = f"{case}.{arm}"
        target = path / "records" / f"{key}.json"
        if target.is_file():
            if not resume:
                raise FileExistsError(target)
            row = _read(target)
            if (row["case_id"], row["arm"], row["plan_sha256"], row["input_sha256"]) != (
                case,
                arm,
                fingerprint,
                input_hashes[key],
            ):
                raise ValueError("replay record identity changed")
        else:
            async with slots:
                if throttled.is_set():
                    return None
                prompt, refs = prompts[case, arm]
                row = await opencode_budget.invoke(
                    route, prompt, case, plan["max_tokens"], input_hashes[key], fingerprint
                )
                row.update(
                    arm=arm, abstract_references=refs, prompt_chars=sum(len(m.text) for m in prompt)
                )
                if any(c.get("status_code") in {401, 403, 429} for c in row["call_metadata"]):
                    throttled.set()
                save_new(target, row)
        assessed = opencode_budget.score(row, originals[case], corpus, ids[case])
        print(
            json.dumps(
                {
                    "case": case,
                    "arm": arm,
                    "finish": row["finish"],
                    "error": row["error"],
                    "format": assessed["assessment"]["format_valid"],
                    "strict_visible_coverage": assessed["assessment"][
                        "in_pool_direction_evidence_coverage"
                    ],
                }
            ),
            flush=True,
        )
        return assessed

    rows = await asyncio.gather(*(run_arm(*key) for key in order))
    if any(row is None for row in rows):
        return {"status": "paused_after_provider_rejection", "missing": rows.count(None)}
    result = {
        "status": "sealed",
        "plan": plan,
        "systems": {
            arm: {
                **aggregate([r for r in rows if r["arm"] == arm]),
                "strict_visible_coverage": mean(
                    r["assessment"]["in_pool_direction_evidence_coverage"]
                    for r in rows
                    if r["arm"] == arm
                ),
                "off_receipt_cases": sum(
                    bool(r["assessment"]["out_of_pool_ids"]) for r in rows if r["arm"] == arm
                ),
            }
            for arm in ARMS
        },
        "cases": [
            {
                key: r[key]
                for key in (
                    "case_id",
                    "arm",
                    "wall_time_ms",
                    "finish",
                    "error",
                    "prompt_chars",
                    "abstract_references",
                    "assessment",
                )
            }
            for r in rows
        ],
    }
    freeze(path / "assessment.json", result)
    return {"status": "sealed", "systems": result["systems"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--contrast", required=True, choices=CASES)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args.name, args.contrast, args.concurrency, resume=args.resume))
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "sealed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
