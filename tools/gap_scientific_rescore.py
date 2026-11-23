from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tools.benchmark.core import file_hash
from tools.gap_benchmark.data import SUITE_ROOT, load_data, save_new
from tools.gap_benchmark.metrics import PaperAnswer, paired_summary, score_record


class ScientificAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    papers: list[PaperAnswer]
    conclusion: str = Field(min_length=1)


def score_scientifically(record: dict, task: dict, corpus: dict) -> dict:
    strict = score_record(record, task, corpus)
    assessment = copy.deepcopy(strict["assessment"])
    original_contract = {key: assessment[key] for key in ("format_valid", "format_errors")}
    scientific_parsed = assessment["format_valid"]
    projection = None
    if assessment["format_errors"] == [{"path": ["papers"], "type": "too_long"}]:
        text = (record.get("response_text") or "").strip()
        if text.startswith("```json\n") and text.endswith("\n```"):
            text = text[8:-4].strip()
        elif text.startswith("```\n") and text.endswith("\n```"):
            text = text[4:-4].strip()
        try:
            parsed = ScientificAnswer.model_validate_json(text)
        except ValidationError:
            parsed = None
        if parsed is not None:
            all_rows = []
            for start in range(0, len(parsed.papers), 10):
                payload = {
                    "papers": [paper.model_dump() for paper in parsed.papers[start : start + 10]],
                    "conclusion": parsed.conclusion,
                }
                projected = {**record, "response_text": json.dumps(payload, ensure_ascii=False)}
                chunk = score_record(projected, task, corpus)["assessment"]
                if start == 0:
                    assessment = chunk
                for paper in chunk["papers"]:
                    all_rows.append({**paper, "rank": paper["rank"] + start})
            seen = set()
            for paper in all_rows:
                paper["duplicate"] = paper["doc_id"] in seen
                seen.add(paper["doc_id"])
            assessment["papers"] = all_rows
            for field, predicate in (
                (
                    "wrong_official_stances",
                    lambda row: row["stance_correct"] is False and not row["duplicate"],
                ),
                ("invalid_quotes", lambda row: not row["exact_quote"]),
                ("unknown_ids", lambda row: not row["known_id"]),
                ("duplicate_ids", lambda row: row["duplicate"]),
            ):
                assessment[field] = sum(predicate(row) for row in all_rows)
            assessment["unjudged_requires_review"] = [
                row["doc_id"] for row in all_rows if not row["official_evidence"]
            ]
            scientific_parsed = True
            projection = {
                "rule": "first 10 objects for recall/coverage; all actual objects for audit",
                "actual_paper_count": len(parsed.papers),
                "recall_cutoff": 10,
                "subject_response_changed": False,
                "agent_calls_repeated": False,
            }
    assessment.update(original_contract)
    assessment["scientific_parsed"] = scientific_parsed
    assessment["opposite_official_stances"] = sum(
        row["stance_correct"] is False and row["stance"] != "INSUFFICIENT" and not row["duplicate"]
        for row in assessment["papers"]
    )
    assessment["official_evidence_marked_insufficient"] = sum(
        row["official_evidence"] and row["stance"] == "INSUFFICIENT" and not row["duplicate"]
        for row in assessment["papers"]
    )
    return {
        **strict,
        "assessment": assessment,
        "original_strict_assessment": strict["assessment"],
        "scoring_projection": projection,
    }


def rescore(source_name: str, name: str) -> dict:
    if not all(re.fullmatch(r"[a-zA-Z0-9_-]+", value) for value in (source_name, name)):
        raise ValueError("run names must not contain paths")
    suite, corpus = load_data()
    source = SUITE_ROOT / "results" / source_name
    output = SUITE_ROOT / "results" / name
    if output.exists():
        raise FileExistsError("scientific rescore must write to a new result directory")
    raw_path = source / "raw-records.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    tasks = {task["case_id"]: task for task in suite["tasks"]}
    expected = {(case, system) for case in tasks for system in ("product", "kimi_code")}
    keys = [(row["case_id"], row["system"]) for row in raw]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("scientific rescore requires the whole unchanged paired run")
    records = [score_scientifically(row, tasks[row["case_id"]], corpus) for row in raw]
    summary = paired_summary(records)
    strict_records = [score_record(row, tasks[row["case_id"]], corpus) for row in raw]
    strict_summary = paired_summary(strict_records)
    comparable = {
        case
        for case in tasks
        if all(row["assessment"]["scientific_parsed"] for row in records if row["case_id"] == case)
    }
    conditional = (
        paired_summary([row for row in records if row["case_id"] in comparable])
        if comparable
        else None
    )
    plan = {
        "schema": "scientific-quality-gap-scoring-clarification-v2",
        "source_run": source_name,
        "source_plan_sha256": file_hash(source / "plan.json"),
        "source_records_sha256": file_hash(raw_path),
        "scoring_tool_sha256": file_hash(Path(__file__)),
        "clarification_sha256": file_hash(SUITE_ROOT / "scoring-clarification.md"),
        "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
        "corpus_sha256": corpus["corpus_sha256"],
        "agent_calls_repeated": False,
        "status": "post-hoc evaluation repair; not a new independent holdout",
        "unmeasurable_cases": sorted(set(tasks) - comparable),
        "normalized_cases": [
            row["case_id"] + ":" + row["system"] for row in records if row["scoring_projection"]
        ],
    }
    save_new(output / "plan.json", plan)
    save_new(output / "scored-records.json", records)
    save_new(
        output / "summary.json",
        {**summary, "strict_original": strict_summary, "paired_measurable_cases": conditional},
    )
    lines = [
        "# 研究证据能力空洞：科学口径与严格契约分开报告",
        "",
        "这是已保存原始输出的确定性重评分，不重跑任何被测Agent，不改任务或外部金标。",
        f"- 来源运行：`{source_name}`；全部56份原始回答保留。",
        "- 唯一修正：超出10篇的合法论文对象仍可量测前10篇科学结果；超限契约失败不被抹掉。",
        "- 原文与方向错误保留全部对象，未标注论文不自动当幻觉。",
        "",
        "| 系统 | 完成 | 严格格式 | 科学可量测 | Recall@5 | Recall@10 | 正确方向原文覆盖@10 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    details = []
    for system, stats in summary["systems"].items():
        selected = [row for row in records if row["system"] == system]
        measurable = sum(row["assessment"]["scientific_parsed"] for row in selected)
        lines.append(
            f"| {system} | {stats['completed']}/28 | {stats['format_valid']}/28 | {measurable}/28 "
            f"| {stats['final_recall_at_5']:.3f} | {stats['final_recall_at_10']:.3f} "
            f"| {stats['correct_direction_evidence_coverage']:.3f} |"
        )
        opposite = sum(row["assessment"]["opposite_official_stances"] for row in selected)
        insufficient = sum(
            row["assessment"]["official_evidence_marked_insufficient"] for row in selected
        )
        details.append(
            f"- {system}：明确支持/反驳方向反转 {opposite}；"
            f"官方有证据但标 INSUFFICIENT {insufficient}；"
            f"非逐字原文 {stats['invalid_quotes']}；重复ID {stats['duplicate_ids']}。"
        )
    lines.extend(["", *details])
    paired = summary["paired"]
    lines.extend(
        [
            "",
            "## 配对结果与边界",
            "",
            f"- 正确方向原文覆盖配对均差（产品−Kimi）：{paired['case_mean_difference']:.3f}。",
            f"- 产品胜/平/Kimi胜研究家族：{paired['product_win_families']}/"
            f"{paired['tie_families']}/{paired['kimi_win_families']}。",
            f"- 家族重采样描述性95%区间：{paired['descriptive_cluster_bootstrap_95_interval']}。",
            "- 原预登记实用优势规则："
            f"{paired['preregistered_practical_advantage_observed']}；不是总体优效检验。",
            f"- 无法自动量测科学内容的案：{plan['unmeasurable_cases']}；"
            "不声称它们已证实科学能力失败。",
            f"- 触发篇数投影：{plan['normalized_cases']}；双方全部案例按相同规则处理。",
            "- 严格原始指标、可量测配对子集和全部逐案细节见summary.json/scored-records.json。",
            "- 外部SciFact公开旧数据可能存在预训练污染，官方证据标注非穷尽；只测固定摘要库。",
            "- 语义审查来自来源运行，超限论文新增可量测部分不能假装已被旧评审覆盖。",
            "",
        ]
    )
    with (output / "report.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    return {"report": str(output / "report.md"), **summary}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Separate scientific scoring from paper-count contracts"
    )
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    print(json.dumps(rescore(args.source_name, args.name), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
