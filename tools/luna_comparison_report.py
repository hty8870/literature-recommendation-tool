"""Render a self-contained native Luna comparison without repricing or rewriting results."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tools.gap_benchmark.data import RUNTIME, SUITE_ROOT

METRICS = (
    ("有效完成", "completed"),
    ("最终证据召回@5", "final_recall_at_5"),
    ("最终证据召回@10", "final_recall_at_10"),
    ("最终 nDCG@5", "final_ndcg_at_5"),
    ("方向正确且逐字真实的证据覆盖", "correct_direction_evidence_coverage"),
    ("完整 rationale 原句覆盖", "complete_rationale_coverage"),
    ("与官方方向不符", "wrong_official_stances"),
    ("不精确/改写引文", "invalid_quotes"),
    ("语料外 ID", "unknown_ids"),
    ("重复 ID", "duplicate_ids"),
)


def number(value) -> str:
    return "—" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)


def render(name: str, assessment: dict, contract: dict, engineering: dict) -> str:
    systems = assessment["systems"]
    paired = assessment["paired"]["paired"]
    lines = [
        f"# 原生 Luna 研究证据对照：{name}",
        "",
        "## 结论",
        "",
        "预登记实用优势判据："
        + ("达到。" if paired["preregistered_practical_advantage_observed"] else "未达到。"),
        "本报告只按相同最终 papers 交付评分，不把各查询候选并集冒充最终答案。"
        "工程成功、命中文献、方向、逐字引用与证据完整度分别报告，速度和成本不抵消科学错误。",
        "",
        "## 冻结条件",
        "",
        f"- 模型：{contract['expected_model']}；产品传输：{contract['transport']}。",
        f"- 实现哈希：`{contract['implementation_hash']}`。",
        f"- 固定语料 {contract['corpus_records']} 篇，{contract['tasks']} 案；"
        "外部专家原始金标不变。",
        "- Kimi 使用原始、逐文件核对后的独立会话基线；复用不是重新运行。",
        "- 已看过的开发题，不是新留出；公开旧数据不能宣称预训练未见。",
        "- 产品原生 Responses 与 Kimi 正常工具/Skill 的传输、提示和思考设置未被隔离，"
        "不能把差异全部归因于检索架构或 LTR。",
        f"- 思考设置：{contract.get('thinking_configuration', '未登记')}。",
        "",
        "## 全部任务结果",
        "",
        "| 指标 | 产品 | Kimi Code |",
        "|---|---:|---:|",
    ]
    for label, key in METRICS:
        values = [systems[system][key] for system in ("product", "kimi_code")]
        if key == "completed":
            shown = [
                f"{value}/{systems[system]['runs']}"
                for value, system in zip(values, ("product", "kimi_code"), strict=True)
            ]
        else:
            shown = [number(value) for value in values]
        lines.append(f"| {label} | {shown[0]} | {shown[1]} |")
    lines += [
        "",
        "## 工程链与不确定性",
        "",
        f"- 产品模型身份核验：{engineering['model_valid']}/{engineering['cases']}。",
        f"- 理由：{engineering['reason_generated']}/{engineering['reason_total']}；"
        f"榜单：{engineering['ranking_completed']}/{engineering['ranking_total']}。",
        f"- 观察到的同时模型调用最高值：{engineering['max_observed_model_inflight']}。",
        f"- 逻辑模型调用：{engineering['logical_model_calls']}，不是精确物理 HTTP 尝试数。",
        f"- 用量边界：{engineering['usage_measurement']}",
        "- 不使用旧 DeepSeek 费率给 Luna 编造美元费用。",
        f"- 独立正目标家族 {paired['positive_families']}；产品胜/平/Kimi胜："
        f"{paired['product_win_families']}/{paired['tie_families']}/{paired['kimi_win_families']}。",
        f"- 家族均差：{number(paired['family_mean_difference'])}；描述性 95% 重采样区间："
        f"{paired['descriptive_cluster_bootstrap_95_interval']}。小样本不作普遍优效结论。",
        "",
        "## 各能力切片",
        "",
        "| 切片 | 产品方向证据覆盖 | Kimi方向证据覆盖 |",
        "|---|---:|---:|",
    ]
    for key, group in assessment["paired"]["slices"].items():
        values = [
            number(group[system]["correct_direction_evidence_coverage"])
            for system in ("product", "kimi_code")
        ]
        lines.append(f"| {key} | {values[0]} | {values[1]} |")
    records = {(row["system"], row["case_id"]): row for row in assessment["records"]}
    cases = list(dict.fromkeys(row["case_id"] for row in assessment["records"]))
    lines += [
        "",
        "## 逐案（不删除失败）",
        "",
        "| 案例 | 产品证据覆盖 | Kimi证据覆盖 | 产品完整链 | Kimi完整链 |",
        "|---|---:|---:|---:|---:|",
    ]
    for case in cases:
        values = [records[(system, case)]["assessment"] for system in ("product", "kimi_code")]
        shown = [
            number(row.get(key))
            for key in ("correct_direction_evidence_coverage", "complete_rationale_coverage")
            for row in values
        ]
        lines.append(f"| {case} | " + " | ".join(shown) + " |")
    lines += [
        "",
        "## 指标边界与诚实保留",
        "",
        "- 完整 rationale 是套内所有句、套间任一套即可；逐字前缀也参与该辅助指标，"
        "不能将所有漏前缀自动解释成科学错误，更不能据分数删除原始金标。",
        "- 部分支持与整句支持可能发生标签分歧，例如 sf-528 的免疫优势表位限定；"
        "保留官方扣分，同时说明原文缺失限定，不能为对齐而强制 SUPPORT。",
        "- 未标注文献不自动视为无关或幻觉；本比较不支持完整精确率结论。",
        "- 这里只测试固定题名/摘要，不是全文可引用资格、实时全库最优或人工专家核验。",
        "- 原始回答、逐案查询与模型响应身份在运行目录；不在本报告导出隐藏推理、认证凭据或私稿。",
        "",
        f"原协议：`{SUITE_ROOT / 'protocol.md'}`。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--assessment", default="assessment-1.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", args.name):
        raise ValueError("invalid run name")
    if not re.fullmatch(r"assessment-[0-9]+\.json", args.assessment):
        raise ValueError("invalid assessment filename")
    root = RUNTIME / "luna-retests" / args.name
    assessment = json.loads((root / args.assessment).read_text(encoding="utf-8"))
    contract = json.loads((root / "product-contract.json").read_text(encoding="utf-8"))
    summaries = sorted(root.glob("native-product-summary-*.json"))
    engineering = json.loads(summaries[-1].read_text(encoding="utf-8"))
    if not assessment["complete_paired_data"] or engineering["implementation_changed_during_run"]:
        raise ValueError("incomplete or changed run cannot be rendered as a complete comparison")
    text = render(args.name, assessment, contract, engineering)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(text)
    print(json.dumps({"report": str(args.output), "complete_paired_data": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
