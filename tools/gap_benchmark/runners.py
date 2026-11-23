from __future__ import annotations

import asyncio
import copy
import json
import re
import time

from tools.benchmark.core import file_hash, model_secrets, withhold_secrets
from tools.benchmark.runners import run_kimi
from tools.quality_benchmark.core import (
    EMBEDDING_CACHE_DIR,
    artifact_hash,
    project_skill_isolation_valid,
)
from tools.quality_benchmark.runners import run_product

from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import SearchCreate, utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.search import _build_claim
from citation_tool.semantic import EmbeddingIndex, SemanticMetadataAdapter

from .data import ROOT, RUNTIME, SUITE_ROOT, label, load_data, save_new
from .metrics import mean, retrieval_metrics


def implementation_hash() -> str:
    return sha256_json(
        {
            "existing_implementation": artifact_hash(
                SUITE_ROOT / "tasks.json", SUITE_ROOT / "protocol.md"
            ),
            "gap_evaluation": {
                path.name: file_hash(path)
                for path in sorted((ROOT / "tools/gap_benchmark").glob("*.py"))
            },
        }
    )


def engine_evaluation(name: str) -> dict:
    suite, corpus = load_data()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("invalid run name")
    output = SUITE_ROOT / "results" / name / "engine.json"
    if output.exists():
        raise FileExistsError(output)
    library = LibraryConfig("scifact", "SciFact original abstracts", "fast", 1, simulation=False)
    settings = Settings(embedding_cache_dir=EMBEDDING_CACHE_DIR)
    sparse = MetadataCorpusAdapter(library, corpus["records"], top_k=20)
    hybrid = SemanticMetadataAdapter(library, corpus["records"], settings=settings, top_k=20)
    started = time.monotonic()
    hybrid._index._load_matrix()
    index_seconds = time.monotonic() - started
    print(f"prebuilt embedding ready; seconds={index_seconds:.2f}", flush=True)
    results = []
    for position, claim in enumerate(suite["all_claims"], 1):
        query, _ = _build_claim(
            "gap-engine",
            f"sf-{claim['id']}",
            SearchCreate(input_type="abstract", abstract=claim["claim"]),
            utc_now_iso(),
            None,
        )
        for method, adapter in (("bm25", sparse), ("hybrid_rrf", hybrid)):
            first = adapter.search(query)
            second = adapter.search(query)

            def manifest(rows):
                return [[row.passage.work_id, row.display_score, row.score_version] for row in rows]

            ids = [row.passage.work_id.removeprefix("work:scifact:") for row in first]
            facet_count = int(adapter.model_versions.get("facet_count", "1"))
            native_limit = min(20, max(5, 2 * facet_count)) if method == "hybrid_rrf" else 5
            results.append(
                {
                    "claim_id": claim["id"],
                    "label": label(claim),
                    "evidence_documents": len(claim["evidence"]),
                    "method": method,
                    "recall_at_5": retrieval_metrics(ids, claim["evidence"], 5)["recall"],
                    "recall_at_20": retrieval_metrics(ids, claim["evidence"], 20)["recall"],
                    "ndcg_at_5": retrieval_metrics(ids, claim["evidence"], 5)["ndcg"],
                    "candidate_ids": ids,
                    "native_window": native_limit,
                    "native_candidate_ids": ids[:native_limit],
                    "identical_same_process_cached_repeat": manifest(first) == manifest(second),
                    "dense_status": adapter.model_versions.get("dense_status"),
                }
            )
        if position % 25 == 0:
            print(f"engine claims={position}/{len(suite['all_claims'])}", flush=True)
    result = {
        "schema": "scientific-quality-gap-engine-v1",
        "created_at": utc_now_iso(),
        "implementation_hash": implementation_hash(),
        "corpus_sha256": corpus["corpus_sha256"],
        "protocol_sha256": file_hash(SUITE_ROOT / "protocol.md"),
        "model": settings.embedding_model,
        "embedding_revision": hybrid._index.model_revision,
        "index_build_seconds": index_seconds,
        "vector_bytes": hybrid._index.cache_path.stat().st_size,
        "scope": "all 300 original dev claims; only official evidence positives contribute recall",
        "systems": {
            method: {
                "runs": len([row for row in results if row["method"] == method]),
                "positive_claims": sum(
                    row["recall_at_5"] is not None for row in results if row["method"] == method
                ),
                **{
                    metric: mean(row[metric] for row in results if row["method"] == method)
                    for metric in ("recall_at_5", "recall_at_20", "ndcg_at_5")
                },
                "zero_recall_at_5": sum(
                    row["recall_at_5"] == 0 for row in results if row["method"] == method
                ),
                "all_repeats_identical": all(
                    row["identical_same_process_cached_repeat"]
                    for row in results
                    if row["method"] == method
                ),
            }
            for method in ("bm25", "hybrid_rrf")
        },
        "cases": results,
    }
    save_new(output, result)
    return {"output": str(output.relative_to(ROOT)), "systems": result["systems"]}


def baseline_prompt(task: dict) -> str:
    return (
        "当前目录 corpus.json 含本次唯一可用的5,183篇真实论文题名与摘要。"
        "没有 DOI/年份/作者元数据或全文，不要补造。可自由选择文件、命令等检索方式，"
        "只读 corpus.json；不要读项目、其他会话、评分文件或修改配置。"
        "不限制检索轮次，不要创建辅助文件。\n\n"
        + task["baseline_query"]
        + "\n同源结构化输入（不含答案）："
        + json.dumps(task["request"], ensure_ascii=False)
    )


async def run_agents(name: str, concurrency: int, *, resume: bool = False) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("invalid run name")
    suite, corpus = load_data()
    secrets = model_secrets()
    output = SUITE_ROOT / "results" / name
    runtime = RUNTIME / name
    plan_path = output / "plan.json"
    frozen_hash = implementation_hash()
    if plan_path.exists():
        if not resume:
            raise FileExistsError("existing run requires --resume; originals are not overwritten")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan["implementation_hash"] != frozen_hash:
            raise ValueError("cannot resume after changing frozen implementation or evaluator")
    else:
        plan = {
            "schema": "scientific-quality-gap-agent-plan-v1",
            "created_at": utc_now_iso(),
            "run_name": name,
            "implementation_hash": frozen_hash,
            "protocol_sha256": file_hash(SUITE_ROOT / "protocol.md"),
            "tasks_sha256": file_hash(SUITE_ROOT / "tasks.json"),
            "corpus_sha256": corpus["corpus_sha256"],
            "model": "deepseek-flash",
            "tasks_per_system": len(suite["tasks"]),
            "gold": "unaltered external SciFact expert annotations; not supplied to subjects",
            "scope": "first frozen public challenge evaluation; not pretraining-unseen data",
            "product": "source React + hybrid + common score + LLM listwise; no runtime LTR",
            "kimi": "fresh CLI sessions; normal tools/skills; no project Skill or gold",
            "thinking": {
                "product": "disabled",
                "kimi": "existing alias configuration, exported per session",
            },
            "limits": {"timeout_seconds": 900, "steps": 96, "product_parent_tools": 32},
            "resource_policy": "retrieval count, cost and latency are not quality gates",
            "status": "frozen_before_agent_calls",
        }
        save_new(plan_path, plan)
    index = EmbeddingIndex(
        corpus["records"], model_name=Settings().embedding_model, cache_dir=EMBEDDING_CACHE_DIR
    )
    if not index.cache_path.is_file():
        raise ValueError("run the frozen engine evaluation to prebuild the corpus cache first")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(original: dict, system: str) -> dict:
        path = runtime / "records" / f"{original['case_id']}.{system}.json"
        if path.exists():
            if not resume:
                raise FileExistsError(path)
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous["implementation_hash"] != frozen_hash:
                raise ValueError("record implementation changed")
            return previous
        task = copy.deepcopy(original)
        task.pop("expect")
        async with semaphore:
            if system == "product":
                record = await run_product(
                    task,
                    name,
                    corpus,
                    runtime_root=RUNTIME,
                    library_override=LibraryConfig(
                        "scifact",
                        "SciFact 原始题名与摘要",
                        "fast",
                        1,
                        simulation=False,
                    ),
                )
            else:
                record = await asyncio.to_thread(
                    run_kimi,
                    task,
                    f"gap-{name}",
                    secrets,
                    corpus_payload=corpus,
                    prompt=baseline_prompt(task),
                )
        record["implementation_hash"] = frozen_hash
        record["split"] = "holdout"
        record, leaked = withhold_secrets(record, secrets)
        record["secret_leak"] = bool(record.get("secret_leak") or leaked)
        save_new(path, record)
        print(
            f"done {original['case_id']} {system}: completed={record['completed']} "
            f"model_valid={record['model_valid']} seconds={record['wall_time_ms'] / 1000:.1f}",
            flush=True,
        )
        return record

    records = await asyncio.gather(
        *(one(task, system) for task in suite["tasks"] for system in ("product", "kimi_code"))
    )
    for system, key in (("product", "agent_run_id"), ("kimi_code", "session_id")):
        selected = [row for row in records if row["system"] == system]
        ids = [row.get(key) for row in selected]
        for row in selected:
            row["session_isolation_valid"] = bool(row.get(key)) and ids.count(row[key]) == 1
            if system == "kimi_code":
                row["project_skill_isolation_valid"] = project_skill_isolation_valid(row)
    if implementation_hash() != frozen_hash:
        raise ValueError(
            "implementation changed during frozen run; records retained but run invalid"
        )
    save_new(output / "raw-records.json", records)
    return {"run_name": name, "records": len(records), "status": "agent_calls_finished"}
