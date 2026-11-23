from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.benchmark.core import file_hash
from tools.gap_benchmark.data import ROOT, SUITE_ROOT, load_data, save_new
from tools.quality_benchmark.core import EMBEDDING_CACHE_DIR

from citation_tool.config import LibraryConfig, Settings
from citation_tool.entities import SearchCreate, utc_now_iso
from citation_tool.relevance import record_allowed, semantic_query_text
from citation_tool.search import _build_claim
from citation_tool.semantic import SemanticMetadataAdapter


def probe(run_name: str, output: Path) -> dict:
    suite, corpus = load_data()
    path = SUITE_ROOT / "results" / run_name / "engine.json"
    measured = json.loads(path.read_text(encoding="utf-8"))
    methods = {
        method: {row["claim_id"]: row for row in measured["cases"] if row["method"] == method}
        for method in ("bm25", "hybrid_rrf")
    }
    chosen = {
        claim_id
        for claim_id, row in methods["hybrid_rrf"].items()
        if row["recall_at_20"] is not None
        and (
            row["recall_at_20"] < 1 or row["recall_at_5"] < methods["bm25"][claim_id]["recall_at_5"]
        )
    }
    records = {row["work_id"]: row for row in corpus["records"]}
    adapter = SemanticMetadataAdapter(
        LibraryConfig("scifact", "SciFact", "fast", 1, simulation=False),
        corpus["records"],
        settings=Settings(embedding_cache_dir=EMBEDDING_CACHE_DIR),
        top_k=20,
    )
    rows = []
    for claim in suite["all_claims"]:
        if claim["id"] not in chosen:
            continue
        query, _ = _build_claim(
            "gap-probe",
            f"sf-{claim['id']}",
            SearchCreate(input_type="abstract", abstract=claim["claim"]),
            utc_now_iso(),
            None,
        )
        lexical = adapter._lexical_index.score(query)
        lexical_ids = sorted(lexical, key=lambda key: (-lexical[key].score, key))
        dense = adapter._index.similarities(query)
        dense_ids = sorted(dense, key=lambda key: (-dense[key], key))
        fused = adapter.search(query)
        fused_ids = [row.passage.work_id for row in fused]
        for doc_id, evidence in claim["evidence"].items():
            work_id = f"work:scifact:{doc_id}"
            source = records[work_id]
            rows.append(
                {
                    "claim_id": claim["id"],
                    "claim": claim["claim"],
                    "effective_semantic_query": semantic_query_text(query),
                    "doc_id": doc_id,
                    "title": source["title"],
                    "official_rationale": [
                        source["abstract_sentences"][index]
                        for index in sorted(
                            {index for item in evidence for index in item["sentences"]}
                        )
                    ],
                    "sparse_rank_before_top20": lexical_ids.index(work_id) + 1
                    if work_id in lexical
                    else None,
                    "dense_rank_before_threshold": dense_ids.index(work_id) + 1,
                    "dense_cosine": dense[work_id],
                    "dense_passes_current_threshold": dense[work_id] >= 0.80,
                    "passes_exclusion_rules": record_allowed(
                        query, source["title"], source["abstract"], source["doi"]
                    ),
                    "fused_rank_within20": fused_ids.index(work_id) + 1
                    if work_id in fused_ids
                    else None,
                }
            )
    result = {
        "scope": "post-hoc diagnosis only; no product or frozen benchmark changes",
        "selection": "all top20 misses and all top5 regressions against BM25",
        "engine_sha256": file_hash(path),
        "corpus_sha256": corpus["corpus_sha256"],
        "rows": rows,
    }
    resolved = output.resolve()
    if not resolved.is_relative_to(ROOT / "data/runtime"):
        raise ValueError("probe output must stay in ignored runtime data")
    save_new(resolved, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose every frozen SciFact retrieval miss")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.run_name, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
