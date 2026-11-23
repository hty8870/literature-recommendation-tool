from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path

from citation_tool.config import PROJECT_ROOT, LibraryConfig, Settings
from citation_tool.entities import utc_now_iso
from citation_tool.jcs import sha256_json
from citation_tool.metadata_retrieval import MetadataCorpusAdapter
from citation_tool.search import _build_claim
from citation_tool.semantic import SemanticMetadataAdapter

from .core import (
    EMBEDDING_CACHE_DIR,
    ROOT,
    expanded_tasks,
    load_corpus,
    load_suite,
    product_request,
)


def evaluate(
    split: str,
    *,
    retriever: str = "sparse",
    dense_threshold: float = 0.80,
    repeat: int = 3,
    corpus_path: Path | None = None,
    suite_path: Path | None = None,
) -> dict:
    suite = load_suite(suite_path)
    if corpus_path is not None:
        resolved = corpus_path.resolve()
        suite["corpus_path"] = str(
            resolved.relative_to(PROJECT_ROOT)
            if resolved.is_relative_to(PROJECT_ROOT)
            else resolved
        )
    corpus = load_corpus(suite)
    library = LibraryConfig("abiotech-real", "aBiotech", "fast", 1, simulation=False)
    if retriever == "hybrid":
        adapter = SemanticMetadataAdapter(
            library,
            corpus["records"],
            settings=Settings(embedding_cache_dir=EMBEDDING_CACHE_DIR),
            dense_threshold=dense_threshold,
        )
    else:
        adapter = MetadataCorpusAdapter(library, corpus["records"])
    cases = []
    seen = set()
    for task in expanded_tasks(suite, split):
        if task["case_id"] in seen:
            continue
        seen.add(task["case_id"])
        claim, _ = _build_claim(
            "quality-retrieval",
            f"direct-{task['run_case_id']}",
            product_request(task),
            utc_now_iso(),
            None,
        )
        repeats = []
        search_ms = []
        for _ in range(repeat):
            started = time.perf_counter()
            candidates = adapter.search(claim)
            search_ms.append(round((time.perf_counter() - started) * 1000, 3))
            repeats.append(
                [
                    {
                        "work_id": candidate.passage.work_id,
                        "score": candidate.display_score,
                        "status": candidate.retraction_status,
                    }
                    for candidate in candidates
                ]
            )
        ids = [item["work_id"] for item in repeats[0]]
        required = task["expect"].get("required_ids", [])
        ranks = {
            work_id: ids.index(work_id) + 1 if work_id in ids else None for work_id in required
        }
        ideal = sum(1 / math.log2(index + 1) for index in range(1, len(required) + 1))
        ndcg = (
            sum(
                1 / math.log2(rank + 1) for rank in ranks.values() if rank is not None and rank <= 5
            )
            / ideal
            if ideal
            else None
        )
        cases.append(
            {
                "case_id": task["case_id"],
                "split": task["split"],
                "candidate_count": len(ids),
                "candidate_ids": ids,
                "required_ranks": ranks,
                "hit_at_5": bool(required)
                and all(rank is not None and rank <= 5 for rank in ranks.values()),
                "ndcg_at_5": ndcg,
                "zero_answer": not required,
                "zero_answer_noise_ids": ids if not required else [],
                "reproducible": all(run == repeats[0] for run in repeats[1:]),
                "retrieval_fingerprint": sha256_json(
                    {"claim_hash": claim.claim_hash, "ordered_candidates": repeats[0]}
                ),
                "repeat_count": repeat,
                "search_ms": search_ms,
            }
        )
    metrics = [case["ndcg_at_5"] for case in cases if case["ndcg_at_5"] is not None]
    latencies = [duration for case in cases for duration in case["search_ms"]]
    vector_path = adapter._index.cache_path if retriever == "hybrid" else None
    return {
        "suite_id": suite["suite_id"],
        "corpus_sha256": corpus["corpus_sha256"],
        "split": split,
        "stage": "metadata_candidate_retrieval_before_agent_or_eligibility",
        "retriever": adapter.model_versions,
        "base_top_k": 5,
        "top_k": max((case["candidate_count"] for case in cases), default=0),
        "candidate_pool_rule": (
            "Base top_k=5. With multiple semantic facets, "
            "min(20, max(base_top_k, 2 * facet_count))."
        ),
        "case_count": len(cases),
        "hit_at_5_mean": statistics.fmean(
            case["hit_at_5"] for case in cases if case["ndcg_at_5"] is not None
        )
        if metrics
        else None,
        "ndcg_at_5_mean": statistics.fmean(metrics) if metrics else None,
        "repeat_reproducibility": all(case["reproducible"] for case in cases),
        "corpus_bytes": (ROOT / suite["corpus_path"]).stat().st_size,
        "vector_cache_bytes": vector_path.stat().st_size
        if vector_path and vector_path.is_file()
        else None,
        "median_search_ms": statistics.median(latencies) if latencies else None,
        "p95_search_ms": sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)]
        if latencies
        else None,
        "first_search_ms": latencies[0] if latencies else None,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--split", choices=["all", "tuning", "development", "holdout"], default="tuning"
    )
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--retriever", choices=["sparse", "hybrid"], default="sparse")
    parser.add_argument("--dense-threshold", type=float, default=0.80)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(
        args.split,
        retriever=args.retriever,
        dense_threshold=args.dense_threshold,
        repeat=args.repeat,
        corpus_path=args.corpus,
        suite_path=args.suite,
    )
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite evaluation: {args.output}")
    args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
