from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field
from tools.benchmark.core import BenchmarkUsageCallback, model_secrets, withhold_secrets

from citation_tool.config import Settings
from citation_tool.reasons import load_model_config

from .data import ROOT, RUNTIME, SUITE_ROOT, load_data, save_new
from .metrics import paired_summary, score_record


class PaperAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["A", "B"]
    index: int = Field(ge=0)
    actual_stance: Literal["SUPPORT", "CONTRADICT", "INSUFFICIENT", "UNCLEAR"]
    issue: Literal["none", "wrong_direction", "scope_overclaim", "unrelated", "quote_misuse"]
    output_evidence: str
    source_evidence: str
    explanation: str


class PairAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    papers: list[PaperAudit]
    summary: str


def audit_payload(task: dict, records: dict, corpus: dict) -> tuple[dict, dict]:
    by_id = {row["doc_id"]: row for row in corpus["records"]}
    order = ("product", "kimi_code")
    if int(hashlib.sha256(task["case_id"].encode()).hexdigest(), 16) % 2:
        order = tuple(reversed(order))
    mapping = dict(zip(("A", "B"), order, strict=True))
    payload = {
        "claim": task["claim"],
        "official_evidence": task["expect"]["evidence"],
        "outputs": {},
    }
    for side, system in mapping.items():
        assessment = records[system]["assessment"]
        payload["outputs"][side] = {
            "format_valid": assessment["format_valid"],
            "conclusion": assessment["conclusion"],
            "papers": [
                {
                    "index": index,
                    "doc_id": paper["doc_id"],
                    "claimed_stance": paper["stance"],
                    "quote": paper["quote"],
                    "reason": paper["reason"],
                    "title": by_id.get(paper["doc_id"], {}).get("title"),
                    "abstract": by_id.get(paper["doc_id"], {}).get("abstract"),
                }
                for index, paper in enumerate(assessment["papers"])
            ],
        }
    return payload, mapping


def validate_audit(audit: PairAudit, payload: dict) -> None:
    expected = {
        (side, paper["index"]): paper
        for side, output in payload["outputs"].items()
        for paper in output["papers"]
    }
    actual = [(paper.side, paper.index) for paper in audit.papers]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError("audit must cover every actual output paper exactly once")
    for item in audit.papers:
        paper = expected[(item.side, item.index)]
        if item.source_evidence and item.source_evidence not in (paper["abstract"] or ""):
            raise ValueError("audit source quote is not verbatim")
        visible = "\n".join(str(paper[key] or "") for key in ("quote", "reason", "claimed_stance"))
        if item.issue != "none" and (
            not item.output_evidence or item.output_evidence not in visible
        ):
            raise ValueError("audit issue lacks verbatim same-side output evidence")
        if item.actual_stance in {"SUPPORT", "CONTRADICT"} and not item.source_evidence:
            raise ValueError("audit scientific direction lacks actual source evidence")


async def audit_records(name: str, concurrency: int) -> dict:
    suite, corpus = load_data()
    output = SUITE_ROOT / "results" / name
    records = json.loads((output / "scored-records.json").read_text(encoding="utf-8"))
    if (output / "semantic-audits.json").exists():
        raise FileExistsError("semantic audits already exist")
    config = load_model_config(Settings().model_config_path)
    if (
        config is None
        or config.model != "deepseek-flash"
        or config.base_url != "https://api.deepseek.com"
    ):
        raise ValueError("audit requires official deepseek-flash")
    callback = BenchmarkUsageCallback()
    model = ChatOpenAI(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=0,
        timeout=75,
        max_retries=4,
        max_completion_tokens=6000,
        extra_body={"thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}},
        callbacks=[callback],
    )
    grouped = {task["case_id"]: {} for task in suite["tasks"]}
    for row in records:
        grouped[row["case_id"]][row["system"]] = row
    semaphore = asyncio.Semaphore(concurrency)
    secrets = model_secrets()

    async def one(task):
        path = RUNTIME / name / "audits" / f"{task['case_id']}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        payload, mapping = audit_payload(task, grouped[task["case_id"]], corpus)
        messages = [
            SystemMessage(
                content=(
                    "你是匿名科学摘要证据审核员。只根据所给论断与逐篇摘要，判断最终论文选择的科学关系。"
                    "官方标签由外部专家标注，不能因系统身份改变；未标注文献不自动当无关。"
                    "SUPPORT/CONTRADICT须直接涉及论断对象、方向和适用范围，仅主题相关为INSUFFICIENT。"
                    "原文与quote逐字相符不等于它蕴涵reason，检查夸大因果、物种、比较方向和研究阶段。"
                    "没有摘要或无法确定则UNCLEAR。不要因谨慎披露未核验全文扣分，不评价费用和轮次。"
                    "每侧每篇正好一项，index为给定零起始编号。issue非none须output_evidence逐字引述该篇"
                    "quote/reason/claimed_stance中的具体越界文字；source_evidence必须是本篇原摘要逐字片段。"
                    "issue为none时output_evidence可空；肯定支持/反驳必须有source_evidence。"
                    "不要改写被测答案。只输出JSON：{papers:[{side:A|B,index:0,"
                    "actual_stance:SUPPORT|CONTRADICT|INSUFFICIENT|UNCLEAR,"
                    "issue:none|wrong_direction|scope_overclaim|unrelated|quote_misuse,"
                    "output_evidence:字符串,source_evidence:字符串,explanation:中文}],summary:中文}。"
                )
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        async with semaphore:
            error = None
            for attempt in (1, 2):
                response = None
                try:
                    response = await model.ainvoke(messages)
                    audit = PairAudit.model_validate_json(str(response.content))
                    validate_audit(audit, payload)
                    result = {
                        "case_id": task["case_id"],
                        "status": "completed",
                        "mapping": mapping,
                        "input_sha256": hashlib.sha256(
                            json.dumps(payload, sort_keys=True).encode()
                        ).hexdigest(),
                        "audit": audit.model_dump(),
                        "attempts": attempt,
                    }
                    break
                except Exception as exc:
                    error = type(exc).__name__
                    if response is not None:
                        messages.append(AIMessage(content=str(response.content)))
                    messages.append(
                        HumanMessage(
                            content=(
                                f"前次未通过{error}。请覆盖每个(side,index)一次，不漏项不重复；"
                                "证据必须逐字属于正确侧及本篇，不能为了格式改变科学判断。只返回完整JSON。"
                            )
                        )
                    )
            else:
                result = {
                    "case_id": task["case_id"],
                    "status": "failed",
                    "error": error,
                    "mapping": mapping,
                }
        result, leaked = withhold_secrets(result, secrets)
        if leaked:
            raise ValueError("secret scanner rejected audit")
        save_new(path, result)
        print(f"audited {task['case_id']}: {result['status']}", flush=True)
        return result

    audits = await asyncio.gather(*(one(task) for task in suite["tasks"]))
    result = {
        "model": config.model,
        "judgments": audits,
        "usage": callback.totals.as_dict(),
        "scope": "same-model supplementary source audit, not independent human gold",
    }
    save_new(output / "semantic-audits.json", result)
    return {"completed": sum(row["status"] == "completed" for row in audits), "total": len(audits)}


def evaluate(name: str) -> dict:
    suite, corpus = load_data()
    output = SUITE_ROOT / "results" / name
    records = json.loads((output / "raw-records.json").read_text(encoding="utf-8"))
    tasks = {task["case_id"]: task for task in suite["tasks"]}
    expected = {(case_id, system) for case_id in tasks for system in ("product", "kimi_code")}
    actual = [(row["case_id"], row["system"]) for row in records]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("evaluation requires every frozen task/system exactly once")
    scored = [score_record(row, tasks[row["case_id"]], corpus) for row in records]
    summary = paired_summary(scored)
    save_new(output / "scored-records.json", scored)
    save_new(output / "summary.json", summary)
    lines = [
        "# 外部研究证据能力空洞对照：SciFact v1",
        "",
        "本轮使用外部专家原始标注，而非项目Agent自出题。公开旧数据可能存在预训练污染。",
        "仅测试固定5,183篇题录/摘要；不是全文可引用资格、真实全库召回或普遍优效证明。",
        "双方新运行官方deepseek-flash；只按相同最终papers交付排序，不混入产品所有候选卡片。",
        "",
        "## 全部冻结任务",
        "",
        "| 系统 | 有效完成 | JSON有效 | 证据召回@5 | 证据召回@10 | "
        "正确方向且原文真实的覆盖 | 方向错误 | 假/改写原文 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system, stats in summary["systems"].items():
        lines.append(
            f"| {system} | {stats['completed']}/{stats['runs']} "
            f"| {stats['format_valid']}/{stats['runs']} "
            f"| {stats['final_recall_at_5']:.3f} | {stats['final_recall_at_10']:.3f} "
            f"| {stats['correct_direction_evidence_coverage']:.3f} "
            f"| {stats['wrong_official_stances']} "
            f"| {stats['invalid_quotes']} |"
        )
    paired = summary["paired"]
    lines.extend(
        [
            "",
            "- 预登记实用优势判据是否达到："
            f"**{paired['preregistered_practical_advantage_observed']}**。",
            f"- 正确方向证据覆盖配对均差（产品−Kimi）：{paired['case_mean_difference']:.3f}。",
            f"- 独立正目标研究家族：{paired['positive_families']}；产品胜/平/Kimi胜："
            f"{paired['product_win_families']}/{paired['tie_families']}/{paired['kimi_win_families']}。",
            "- 按家族重采样的描述性95%区间："
            f"{paired['descriptive_cluster_bootstrap_95_interval']}；"
            "小样本区间不作总体优效结论。",
            "",
            "## 能力切片",
            "",
            "| 切片 | 产品 正确方向证据覆盖 | Kimi 正确方向证据覆盖 |",
            "|---|---:|---:|",
        ]
    )
    for slice_name, systems in summary["slices"].items():
        values = [
            systems[name]["correct_direction_evidence_coverage"]
            for name in ("product", "kimi_code")
        ]
        rendered = [
            f"{value:.3f}" if value is not None else "不计召回；另查未判证据" for value in values
        ]
        lines.append(f"| {slice_name} | {rendered[0]} | {rendered[1]} |")
    lines.extend(
        [
            "",
            "## 逐案（全部保留）",
            "",
            "| 案例 | 切片 | 产品覆盖 | Kimi覆盖 |",
            "|---|---|---:|---:|",
        ]
    )
    for case_id, task in tasks.items():
        values = [
            next(row for row in scored if row["case_id"] == case_id and row["system"] == name)[
                "assessment"
            ]["correct_direction_evidence_coverage"]
            for name in ("product", "kimi_code")
        ]
        rendered = [f"{value:.3f}" if value is not None else "—" for value in values]
        lines.append(f"| {case_id} | {task['slice']} | {rendered[0]} | {rendered[1]} |")
    lines.extend(
        [
            "",
            "## 指标解释",
            "",
            "- 未标注候选不自动算错或算幻觉，需查看同模型辅助原文复核；这也限制了精确率结论。",
            "- 证据覆盖同时要求官方目标、方向正确、逐字原文，不能被速度或成本抵消。",
            "- 原文完整rationale覆盖更严格，但不是唯一合法证据方式，单列不当硬门槛。",
            "- 证据缺口题不把空结果当作现实世界不存在研究的证明。",
            "- 若任一JSON格式失败，原文仍保留，格式故障与科学方向错误分开。",
            "- 工程榜单／理由成功率、按阶段召回和原始输出见scored-records.json；"
            "引擎全部300题见engine.json，不把直接检索器冒充通用Agent。",
            "",
        ]
    )
    path = output / "report.md"
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    return {"report": str(path.relative_to(ROOT)), **summary}
