"""Luna mechanism screen: independent adjudication of omitted or uncertain candidates."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from tools import luna_native_product_retest as native
from tools.benchmark.core import file_hash, secret_leak
from tools.gap_benchmark import parent_metrics, parent_retest, parent_splits
from tools.gap_benchmark.data import save_new
from tools.gap_benchmark.metrics import FinalAnswer, parse_answer
from tools.luna_credentials import MODEL, authorized_config

from citation_tool.agent import final_answer_text
from citation_tool.jcs import sha256_json

PROMPT = (
    "独立核查本次提供的逐篇摘要，不知道其他模型意见。只列确实与论断中的对象、比较、"
    "结局和方向相符、有直接原文依据的新增支持或反驳；相反结果与没有测量、没有显著性不同。"
    "仅主题相似、不同对象、代理终点、背景叙述或只支持复合论断的一部分不算完整论断的方向。"
    "不使用外部资料；不要为了列论文而降低标准，没有直接方向就交空数组。"
    '返回唯一 JSON：{"papers":[{"doc_id":"数字 ID","stance":"SUPPORT 或 CONTRADICT",'
    '"quote":"同篇摘要中逐字连续片段","reason":"依据和适用范围"}],"conclusion":"简短说明"}。'
)


def review_pool(observation: dict, baseline: FinalAnswer) -> dict[str, dict]:
    decided = {paper.doc_id for paper in baseline.papers if paper.stance != "INSUFFICIENT"}
    docs = {}
    for row in observation["candidate_diagnostics"]:
        doc = row["work_id"].removeprefix("work:scifact:")
        if doc not in decided:
            docs[doc] = {"doc_id": doc, "title": row["title"], "abstract": row["evidence_excerpt"]}
    return docs


def merge_evidence(
    baseline: FinalAnswer, review: FinalAnswer, allowed: dict[str, dict]
) -> FinalAnswer:
    proposals = {paper.doc_id: paper for paper in review.papers}
    if len(proposals) != len(review.papers) or any(
        paper.doc_id not in allowed
        or paper.stance == "INSUFFICIENT"
        or len(re.findall(r"\w+", paper.quote)) < 4
        or paper.quote not in allowed[paper.doc_id]["abstract"]
        for paper in review.papers
    ):
        raise ValueError("invalid evidence proposal")
    papers = [
        proposals.pop(paper.doc_id, paper) if paper.stance == "INSUFFICIENT" else paper
        for paper in baseline.papers
    ]
    papers += list(proposals.values())[: 10 - len(papers)]
    return FinalAnswer(papers=papers, conclusion=baseline.conclusion)


async def _review(record: dict, subject: dict, path: Path, config) -> tuple[dict, dict]:
    baseline = parse_answer(record["response_text"])[0]
    if not record["completed"] or not record["model_valid"] or baseline is None:
        return record, {"status": "no_valid_baseline", "native_response_ids": []}
    docs = review_pool(subject["observation"], baseline)
    if not docs:
        return record, {"status": "not_applicable", "native_response_ids": []}
    audit = native.NativeModelAudit(path, native.NativeModelSlots())
    gateway = native.NativeLunaGateway(config, audit)
    merged = None
    reply = ""
    status = "completed"
    try:
        async with asyncio.timeout(900):
            response = await gateway.agent_model().ainvoke(
                [
                    SystemMessage(PROMPT),
                    HumanMessage(
                        json.dumps(
                            {
                                "claim": subject["request"]["abstract"],
                                "candidates": list(docs.values()),
                            },
                            ensure_ascii=False,
                        )
                    ),
                ],
                response_format={"type": "json_object"},
            )
            reply = final_answer_text(response) or ""
            review = parse_answer(reply)[0]
            if review is None or secret_leak(reply, []):
                raise ValueError("invalid reviewer delivery")
            merged = merge_evidence(baseline, review, docs)
    except Exception as exc:
        status = type(exc).__name__
    finally:
        await gateway.aclose()
    calls = native.collect_model_audit(path)
    if merged is not None and calls["model_valid"]:
        alternate = {**record, "response_text": merged.model_dump_json()}
    else:
        status = "model_unverified" if status == "completed" else status
        alternate = record
    details = {
        "status": status,
        "reviewed_docs": list(docs),
        "native_response_ids": calls["response_ids"],
        "models": calls["served_models"],
        "usage": calls["usage"],
        "final_review": reply if not secret_leak(reply, []) else "",
        "alternate_evidence_list": alternate["response_text"] if status == "completed" else None,
    }
    return alternate, details


async def run(output: Path) -> dict:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    manifest = parent_splits.load_splits()
    stage = parent_splits.ROOT / "runs/direction-vs-strength-v1/train"
    plan = parent_retest._read(stage.parent / "plan.json")
    assessment = parent_retest._read(stage / "assessment.json")
    cases = manifest["cohorts"]["train"]
    if assessment["plan_sha256"] != sha256_json(plan) or assessment["cohort"] != "train":
        raise ValueError("wrong baseline contract")
    files = {
        case: (stage / "subject" / f"{case}.json", stage / "records" / case / "legacy.json")
        for case in cases
    }
    contract = {
        "protocol": file_hash(parent_retest.SUITE_ROOT / "parent-secondlook-v1.md"),
        "runner": file_hash(Path(__file__)),
        "source_plan": sha256_json(plan),
        "baseline_inputs": {case: [file_hash(p) for p in files[case]] for case in cases},
        "model": MODEL,
        "store": False,
    }
    save_new(output / "plan.json", contract)
    _, corpus, claims, _ = parent_splits.load_sources()
    if corpus["corpus_sha256"] != manifest["corpus_sha256"]:
        raise ValueError("wrong corpus")
    sources = {row["doc_id"]: row for row in corpus["records"]}
    members = {row["case_id"]: row for row in manifest["members"]}
    config = await asyncio.to_thread(authorized_config)
    rows, failures, calls = [], 0, 0
    used = set()
    for case in cases:
        subject, record = (parent_retest._read(path) for path in files[case])
        if record["subject_sha256"] != sha256_json(subject) or record["plan_sha256"] != sha256_json(
            plan
        ):
            raise ValueError("baseline was changed")
        alternate, detail = await _review(record, subject, output / "model-calls" / case, config)
        ids = [*record["native_response_ids"], *detail["native_response_ids"]]
        if len(set(ids)) != len(ids) or used.intersection(ids):
            raise ValueError("reused response ID")
        used.update(ids)
        failures += detail["status"] not in {"completed", "not_applicable"}
        calls += detail.get("usage", {}).get("model_calls", 0)
        member = members[case]
        gold = claims[member["claim_id"]]["evidence"]
        scores = {
            arm: parent_metrics.score(row, gold, sources, set(row["supplied"]))
            for arm, row in (("legacy", record), ("secondlook", alternate))
        }
        row = {
            "case_id": case,
            "family": member["family"],
            "stratum": member["stratum"],
            "arms": scores,
        }
        await asyncio.to_thread(
            save_new, output / "records" / f"{case}.json", {**row, "review": detail}
        )
        rows.append(row)
        print(
            json.dumps(
                {
                    "case": case,
                    "status": detail["status"],
                    "delta": (
                        None
                        if not gold
                        else scores["secondlook"]["directional_coverage"]
                        - scores["legacy"]["directional_coverage"]
                    ),
                }
            ),
            flush=True,
        )
    report = parent_metrics.compare(rows, ("legacy", "secondlook"))
    report.update(
        development_only=True,
        extra_model_calls=calls,
        review_failures=failures,
        merits_validation=not failures and parent_metrics.select_variant(report) == "secondlook",
    )
    save_new(output / "assessment.json", report)
    return {key: value for key, value in report.items() if key not in {"cases", "strata"}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.output_directory)), indent=2))


if __name__ == "__main__":
    main()
