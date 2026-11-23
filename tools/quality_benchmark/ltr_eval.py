"""Small deterministic linear LTR evaluated with leave-one-case-out on tuning tasks only."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from citation_tool.config import Settings
from citation_tool.entities import utc_now_iso
from citation_tool.publication import canonical_work_id, is_correction
from citation_tool.relevance import record_allowed, score_records
from citation_tool.search import _build_claim
from citation_tool.semantic import EmbeddingIndex, reciprocal_rank_fusion

from .core import EMBEDDING_CACHE_DIR, expanded_tasks, load_corpus, load_suite, product_request

FEATURE_VERSION = "sparse-cosine-rank-rrf-type-v1"
FEATURE_NAMES = [
    "sparse_score",
    "cosine",
    "sparse_reciprocal_rank",
    "dense_reciprocal_rank",
    "rrf_score",
    "topic_coverage",
    "identifier_coverage",
    "review",
    "correction",
]


def feature_rows(claim, records, index):
    tuples = [
        (canonical_work_id(record), record["title"], record["abstract"], record["doi"])
        for record in records
    ]
    sparse = score_records(claim, tuples)
    cosine = index.similarities(claim)
    sparse_order = sorted(sparse, key=lambda key: (-sparse[key].score, key))[:20]
    dense_order = sorted(
        (
            work_id
            for work_id, title, text, doi in tuples
            if cosine[work_id] >= 0.80 and record_allowed(claim, title, text, doi)
        ),
        key=lambda key: (-cosine[key], key),
    )[:20]
    fused = reciprocal_rank_fusion(sparse, {key: cosine[key] for key in dense_order})
    sparse_ranks = {key: rank for rank, key in enumerate(sparse_order, 1)}
    dense_ranks = {key: rank for rank, key in enumerate(dense_order, 1)}
    by_id = {canonical_work_id(record): record for record in records}
    rows = {}
    for work_id in sorted(set(sparse_order) | set(dense_order)):
        lexical = sparse.get(work_id)
        identifiers, topics = (
            _query_coverage(claim, lexical.matched_identifiers, lexical.matched_topics)
            if lexical
            else (0.0, 0.0)
        )
        record = by_id[work_id]
        features = [
            (lexical.score / 100) if lexical else 0.0,
            max(0.0, min(1.0, cosine[work_id])),
            1 / sparse_ranks[work_id] if work_id in sparse_ranks else 0.0,
            1 / dense_ranks[work_id] if work_id in dense_ranks else 0.0,
            fused[work_id].score / 100,
            topics,
            identifiers,
            float(any("review" in str(value).casefold() for value in record.get("pub_types", []))),
            float(is_correction(record)),
        ]
        rows[work_id] = features
    return rows, fused


def _query_coverage(claim, identifiers, topics):
    from citation_tool.relevance import _query_concepts

    wanted_identifiers, wanted_topics, _, _ = _query_concepts(claim)
    return (
        len(topics) / len(wanted_topics) if wanted_topics else 0.0,
        len(identifiers) / len(wanted_identifiers) if wanted_identifiers else 0.0,
    )


def fit(examples, *, l2=0.1, iterations=300):
    weights = [0.0] * len(FEATURE_NAMES)
    if not examples:
        return weights
    for _ in range(iterations):
        gradient = [weight * l2 for weight in weights]
        for features in examples:
            margin = sum(
                weight * feature for weight, feature in zip(weights, features, strict=True)
            )
            scale = 1 / (1 + math.exp(max(-60, min(60, margin))))
            for index, feature in enumerate(features):
                gradient[index] -= scale * feature / len(examples)
        weights = [weight - 0.1 * change for weight, change in zip(weights, gradient, strict=True)]
    return weights


def pairs(rows, required):
    positive = next((work_id for work_id in required if work_id in rows), None)
    if positive is None:
        return []
    return [
        [left - right for left, right in zip(rows[positive], rows[other], strict=True)]
        for other in rows
        if other != positive
    ]


def evaluate():
    suite = load_suite()
    corpus = load_corpus(suite)
    records = corpus["records"]
    index = EmbeddingIndex(
        records,
        model_name=Settings().embedding_model,
        cache_dir=EMBEDDING_CACHE_DIR,
    )
    cases = []
    for task in expanded_tasks(suite, "tuning"):
        if any(item["case_id"] == task["case_id"] for item in cases):
            continue
        claim, _ = _build_claim(
            "quality-ltr", f"ltr-{task['case_id']}", product_request(task), utc_now_iso(), None
        )
        rows, fused = feature_rows(claim, records, index)
        cases.append(
            {
                "case_id": task["case_id"],
                "required": task["expect"].get("required_ids", []),
                "rows": rows,
                "rrf": fused,
            }
        )
    folds = []
    for held_out in cases:
        examples = [
            pair
            for case in cases
            if case["case_id"] != held_out["case_id"]
            for pair in pairs(case["rows"], case["required"])
        ]
        weights = fit(examples)
        if not held_out["required"]:
            folds.append({"case_id": held_out["case_id"], "zero_answer": True})
            continue
        target = held_out["required"][0]
        if target not in held_out["rows"]:
            ltr_rank = None
        else:
            order = sorted(
                held_out["rows"],
                key=lambda work_id: (
                    -sum(
                        weight * feature
                        for weight, feature in zip(weights, held_out["rows"][work_id], strict=True)
                    ),
                    work_id,
                ),
            )
            ltr_rank = order.index(target) + 1
        baseline_order = sorted(
            held_out["rrf"], key=lambda work_id: (-held_out["rrf"][work_id].score, work_id)
        )
        baseline_rank = baseline_order.index(target) + 1 if target in baseline_order else None
        folds.append(
            {
                "case_id": held_out["case_id"],
                "target": target,
                "ltr_rank": ltr_rank,
                "rrf_rank": baseline_rank,
                "ltr_hit_at_5": ltr_rank is not None and ltr_rank <= 5,
                "rrf_hit_at_5": baseline_rank is not None and baseline_rank <= 5,
            }
        )
    final_examples = [pair for case in cases for pair in pairs(case["rows"], case["required"])]
    scored = [fold for fold in folds if not fold.get("zero_answer")]
    ltr_mean = statistics.fmean(fold["ltr_hit_at_5"] for fold in scored)
    rrf_mean = statistics.fmean(fold["rrf_hit_at_5"] for fold in scored)
    adopted = ltr_mean > rrf_mean and all(
        fold["ltr_hit_at_5"] for fold in scored if fold["rrf_hit_at_5"]
    )
    return {
        "suite_id": suite["suite_id"],
        "corpus_sha256": corpus["corpus_sha256"],
        "split": "tuning",
        "holdout_used": False,
        "method": "leave-one-case-out pairwise logistic linear LTR",
        "feature_version": FEATURE_VERSION,
        "feature_names": FEATURE_NAMES,
        "seed": 0,
        "iterations": 300,
        "l2": 0.1,
        "learning_rate": 0.1,
        "training_pair_count": len(final_examples),
        "ltr_hit_at_5_mean": ltr_mean,
        "rrf_hit_at_5_mean": rrf_mean,
        "folds": folds,
        "final_reference_weights": fit(final_examples),
        "adopted": adopted,
        "decision": (
            "No deployment gain over RRF is established; keep the simpler deterministic RRF."
            if not adopted
            else "Candidate LTR requires independent holdout validation before deployment."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = evaluate()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "folds"},
            ensure_ascii=False,
            indent=2,
        )
    )
