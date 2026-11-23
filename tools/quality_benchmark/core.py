from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from tools.benchmark.core import RUNTIME_ROOT, file_hash, sha256_json

from citation_tool.candidates import candidate_sort_key
from citation_tool.entities import SearchCreate

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "benchmark/quality/tasks-v3.json"
PROTOCOL_PATH = ROOT / "benchmark/quality/protocol-v3.md"
RESULTS_ROOT = ROOT / "benchmark/quality/results"
QUALITY_RUNTIME = RUNTIME_ROOT / "quality"
EMBEDDING_CACHE_DIR = ROOT / "data/runtime/embedding-models"
EXTERNAL_SUITE_ID = "astra-independent-abstract-v1"
EXTERNAL_SUITE_IDS = {
    EXTERNAL_SUITE_ID,
    "astra-independent-abstract-v2",
    "astra-independent-abstract-v3",
    "astra-independent-abstract-v4",
}
METADATA_INSTRUCTION = (
    "这是真实题录与摘要上的候选发现，不是全文证据核验。"
    "请给相关候选、DOI 和摘要范围内的理由；没有全文定位就不能声称可直接引用。"
)
KIMI_CONTEXT = (
    "当前目录 corpus.json 含真实论文的题录与摘要及近邻干扰论文，"
    "没有提供全文或全文定位。只在该库内做候选发现，按摘要说明理由与限制，"
    "不要把摘要当成已核查全文，也不要用外部论文补齐结果。"
    "不要修改任何项目、配置或凭据，不要创建辅助文件。"
)


def is_external_suite(suite: dict[str, Any]) -> bool:
    return suite.get("suite_id") in EXTERNAL_SUITE_IDS


def load_suite(path: Path | None = None) -> dict[str, Any]:
    suite = json.loads((path or SUITE_PATH).read_text(encoding="utf-8"))
    suite_id = suite.get("suite_id")
    if (
        suite_id
        not in {
            "abiotech-abstract-ood-v1",
            "abiotech-abstract-ood-v2",
            "abiotech-multidomain-abstract-v3",
        }
        | EXTERNAL_SUITE_IDS
    ):
        raise ValueError("unsupported quality suite")
    external = is_external_suite(suite)
    tasks = suite.get("tasks", [])
    task_ids = [task["case_id"] for task in tasks]
    if len(tasks) < 16 or len(set(task_ids)) != len(tasks):
        raise ValueError("quality suite requires at least 16 unique tasks")
    if external and len(tasks) != 16:
        raise ValueError("the independent external suite requires exactly 16 tasks")
    for task in tasks:
        SearchCreate.model_validate(task["request"])
        if not task["baseline_query"].strip() or not task["expect"].get("gold_note"):
            raise ValueError(f"incomplete quality task: {task['case_id']}")
        if external and not task["expect"].get("evidence_anchors"):
            raise ValueError(f"external task lacks evidence anchors: {task['case_id']}")
    if not set(suite["stability_case_ids"]).issubset(task_ids):
        raise ValueError("stability task does not exist")
    if external and len(suite["stability_case_ids"]) != 4:
        raise ValueError("external suite requires four stability tasks")
    if len(suite["tuning_case_ids"]) != len(set(suite["tuning_case_ids"])):
        raise ValueError("duplicate tuning task IDs")
    if external:
        if suite["tuning_case_ids"] or suite.get("development_case_ids"):
            raise ValueError("independent external tasks must all remain holdout")
        targets = [work_id for task in tasks for work_id in task["expect"].get("required_ids", [])]
        if (
            len(set(targets)) < 12
            or max((targets.count(item) for item in set(targets)), default=0) > 2
        ):
            raise ValueError("external suite needs at least 12 targets with at most two uses each")
        if sum(not task["expect"].get("required_ids") for task in tasks) != 2:
            raise ValueError("external suite requires exactly two evidence-gap tasks")
        if not any(len(task["expect"].get("required_ids", [])) > 1 for task in tasks):
            raise ValueError("external suite requires a multi-target comparison")
        if {task["request"]["input_type"] for task in tasks} != {"keywords", "abstract"}:
            raise ValueError("external suite requires both input types")
    return suite


def expanded_tasks(suite: dict[str, Any], selection: str = "all") -> list[dict[str, Any]]:
    if selection not in {"all", "tuning", "development", "holdout"}:
        raise ValueError("selection must be all, tuning, development or holdout")
    tuning = set(suite["tuning_case_ids"])
    development = set(suite.get("development_case_ids", []))
    external = is_external_suite(suite)
    if not external and (
        len(tuning) < 8 or not tuning.issubset({task["case_id"] for task in suite["tasks"]})
    ):
        raise ValueError("quality suite requires at least eight existing tuning tasks")
    if tuning & development or (external and (tuning or development)):
        raise ValueError("tuning and development tasks must be disjoint and external-only holdout")
    tasks = [
        task
        for task in suite["tasks"]
        if selection == "all"
        or (selection == "tuning" and task["case_id"] in tuning)
        or (selection == "development" and task["case_id"] in development)
        or (selection == "holdout" and task["case_id"] not in tuning | development)
    ]
    expanded = []
    for task in tasks:
        repetitions = suite["repetitions"] if task["case_id"] in suite["stability_case_ids"] else 1
        for repetition in range(1, repetitions + 1):
            item = copy.deepcopy(task)
            item["repetition"] = repetition
            item["run_case_id"] = (
                task["case_id"] if repetition == 1 else f"{task['case_id']}__r{repetition}"
            )
            item["split"] = (
                "tuning"
                if task["case_id"] in tuning
                else "development"
                if task["case_id"] in development
                else "holdout"
            )
            item["suite_id"] = suite["suite_id"]
            item["retriever"] = suite.get("retriever", "sparse")
            item["dense_threshold"] = suite.get("dense_threshold", 0.80)
            expanded.append(item)
    return expanded


def load_corpus(suite: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / suite["corpus_path"]
    corpus = json.loads(path.read_text(encoding="utf-8"))
    records = corpus.get("records", [])
    if (
        len(records) < 50
        or len({record["work_id"] for record in records}) != len(records)
        or not all(record.get("abstract") and not record["simulation"] for record in records)
    ):
        raise ValueError("quality corpus must contain at least 50 unique real abstracts")
    if suite.get("core_work_ids"):
        if not set(suite["core_work_ids"]).issubset({record["work_id"] for record in records}):
            raise ValueError("core papers are missing from the quality corpus")
    corpus["corpus_sha256"] = sha256_json(records)
    return corpus


def validate_gold(tasks: list[dict[str, Any]], corpus: dict[str, Any]) -> None:
    records = {record["work_id"]: record for record in corpus["records"]}
    for task in tasks:
        expect = task["expect"]
        ids = set(expect.get("required_ids", []))
        ids.update(expect.get("background_ids", []))
        ids.update(expect.get("forbidden_ids", []))
        if unknown := ids - records.keys():
            raise ValueError(f"unknown gold IDs in {task['case_id']}: {sorted(unknown)}")
        if ids_required_disjoint(expect):
            raise ValueError(f"inconsistent gold roles in {task['case_id']}")
        if "evidence_anchors" in expect:
            _validate_evidence_anchors(task, ids, records)


def _validate_evidence_anchors(
    task: dict[str, Any], gold_ids: set[str], records: dict[str, dict[str, Any]]
) -> None:
    anchors = task["expect"]["evidence_anchors"]
    anchored_ids = set()
    for anchor in anchors:
        work_id = anchor.get("work_id")
        quote = anchor.get("quote", "").strip()
        if work_id not in gold_ids or not anchor.get("supports", "").strip():
            raise ValueError(f"irrelevant or unexplained evidence anchor in {task['case_id']}")
        if len(quote) < 24:
            raise ValueError(f"evidence anchor is too short in {task['case_id']}")
        record = records[work_id]
        if quote not in record["title"] and quote not in record["abstract"]:
            raise ValueError(f"non-verbatim evidence anchor in {task['case_id']}: {work_id}")
        anchored_ids.add(work_id)
    if anchored_ids != gold_ids:
        raise ValueError(f"not every gold role has an evidence anchor in {task['case_id']}")
    query_text = json.dumps(
        {"query": task["baseline_query"], "request": task["request"]}, ensure_ascii=False
    ).casefold()
    for work_id in task["expect"].get("required_ids", []):
        if (
            work_id.casefold() in query_text
            or work_id.removeprefix("work:").casefold() in query_text
        ):
            raise ValueError(f"target identifier leaked into external query: {task['case_id']}")
    abstract = task["request"].get("abstract")
    if abstract:
        normalized = " ".join(abstract.casefold().split())
        for work_id in task["expect"].get("required_ids", []):
            target = " ".join(records[work_id]["abstract"].casefold().split())
            if normalized in target or target in normalized:
                raise ValueError(f"target abstract copied into external query: {task['case_id']}")


def ids_required_disjoint(expect: dict[str, Any]) -> bool:
    required = set(expect.get("required_ids", []))
    forbidden = set(expect.get("forbidden_ids", []))
    background = set(expect.get("background_ids", []))
    return bool(required & (forbidden | background))


def product_request(task: dict[str, Any]) -> SearchCreate:
    payload = copy.deepcopy(task["request"])
    payload["claim_scope"] = {"free_text": METADATA_INSTRUCTION}
    payload["topic_context"] = [task["baseline_query"]]
    return SearchCreate.model_validate(payload)


def kimi_prompt(task: dict[str, Any]) -> str:
    return (
        f"{KIMI_CONTEXT}\n\n{task['baseline_query']}\n"
        "与上述需求同源的结构化输入（不含答案）："
        + json.dumps(task["request"], ensure_ascii=False)
        + f"\n{METADATA_INSTRUCTION}\n以候选质量、约束覆盖和证据忠实度为先；"
        "可自主补查，但不要进行没有新信息目的的机械重复。"
    )


def evaluation_records(task: dict[str, Any], corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expect = task["expect"]
    ids = set(expect.get("required_ids", []))
    ids.update(expect.get("background_ids", []))
    ids.update(expect.get("forbidden_ids", []))
    return [record for record in corpus if record["work_id"] in ids]


def output_details(state) -> dict[str, Any]:
    candidates = [
        {
            **item.model_dump(mode="json"),
            "evidence_level": "abstract_metadata_only",
        }
        for item in sorted(state.candidate_diagnostics, key=candidate_sort_key)
    ]
    return {
        "query_id": state.session.query_id,
        "input_type": state.session.input_type,
        "search_state": state.session.search_state,
        "coverage": state.coverage.model_dump(mode="json"),
        "requested_library_ids": state.session.requested_library_ids,
        "results": [],
        "candidate_diagnostics": candidates,
        "candidate_rankings": [item.model_dump(mode="json") for item in state.candidate_rankings],
        "workers": [item.model_dump(mode="json") for item in state.workers],
        "final_snapshot_id": state.session.final_snapshot_id,
    }


def visible_output(run: dict[str, Any], queries: list[dict[str, Any]]) -> str:
    sections = ["[Agent 最终摘要]", run.get("summary") or ""]
    for query in queries:
        cards = []
        for item in query["candidate_diagnostics"]:
            reason = item.get("reason") or {}
            cards.append(
                json.dumps(
                    {
                        "work_id": item["work_id"],
                        "title": item["title"],
                        "doi": item["doi"],
                        "candidate_score": item["candidate_score"],
                        "base_rank": item.get("base_rank"),
                        "llm_rank": item.get("llm_rank"),
                        "ranking_status": item.get("ranking_status", "disabled"),
                        "publication_types": item["publication_types"],
                        "reason": reason.get("text"),
                        "reason_status": reason.get("status"),
                        "abstract": item["evidence_excerpt"],
                        "source": item["source_uri"],
                        "citable": False,
                        "evidence_level": "abstract_metadata_only",
                    },
                    ensure_ascii=False,
                )
            )
        sections.append(
            f"[待核查候选 query_id={query['query_id']}；不是可引用推荐]\n"
            + ("\n".join(cards) if cards else "没有候选。")
        )
    return "\n\n".join(sections)


def project_skill_isolation_valid(record: dict[str, Any]) -> bool:
    paths = [str(path).casefold().replace("\\", "/") for path in record.get("agents_md_paths", [])]
    return not any(
        "recommend-literature" in path
        or "build-minimal-frontend" in path
        or "文献推荐工具/agents.md" in path
        for path in paths
    )


def usage_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    from tools.benchmark.core import merge_usage

    return merge_usage([record["usage"] for record in records])


def artifact_hash(suite_path: Path = SUITE_PATH, protocol_path: Path = PROTOCOL_PATH) -> str:
    paths = [
        suite_path,
        protocol_path,
        ROOT / "tools/quality_corpus.py",
        ROOT / "tools/quality_corpus_expand.py",
        ROOT / "tools/quality_corpus_expand_v3.py",
        ROOT / "tools/quality_suite_v2.py",
        ROOT / "tools/quality_suite_v3.py",
        ROOT / "tools/benchmark/core.py",
        ROOT / "tools/benchmark/runners.py",
        ROOT / "tools/benchmark/evaluation.py",
        *sorted((ROOT / "tools/quality_benchmark").glob("*.py")),
        *sorted((ROOT / "src/citation_tool").glob("*.py")),
        ROOT / ".agents/skills/recommend-literature/SKILL.md",
        ROOT / "tools/discovery_retest.py",
    ]
    return sha256_json(
        {
            str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): file_hash(path)
            for path in paths
        }
    )
