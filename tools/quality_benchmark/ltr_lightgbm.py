"""Train a small LambdaMART and compare it with the frozen RRF without leakage.

Training: v3 tuning and Astra v1 (both development data). Evaluation: Astra v2,
which is also reported as development evidence after its earlier agent runs.
The fixed hyperparameters are not selected by scanning evaluation outcomes.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

from citation_tool.config import Settings
from citation_tool.entities import utc_now_iso
from citation_tool.search import _build_claim
from citation_tool.semantic import EMBEDDING_CACHE_VERSION, SEMANTIC_QUERY_VERSION, EmbeddingIndex

from .core import (
    EMBEDDING_CACHE_DIR,
    ROOT,
    expanded_tasks,
    load_corpus,
    load_suite,
    product_request,
)
from .ltr_features import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    artifact_payload,
    hybrid_feature_set,
)

PARAMETERS = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "eval_at": [5],
    "n_estimators": 40,
    "learning_rate": 0.05,
    "num_leaves": 7,
    "max_depth": 3,
    "min_child_samples": 5,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "random_state": 42,
    "deterministic": True,
    "force_col_wise": True,
    "verbosity": -1,
    "n_jobs": 1,
}
TRAINING_SUITES = (
    ("benchmark/quality/tasks-v3.json", "tuning"),
    ("benchmark/external/astra-v1/tasks.json", "all"),
)
EVALUATION_SUITE = "benchmark/external/astra-v2/tasks.json"
CACHE_PATH = ROOT / "data/runtime/ltr/feature-cache-v1.json"
MODEL_PATH = ROOT / "data/runtime/ltr/lambdarank-v1.json"


def build_cases(suite_path: str, selection: str) -> list[dict[str, Any]]:
    path = ROOT / suite_path
    suite = load_suite(path)
    corpus = load_corpus(suite)
    index = EmbeddingIndex(
        corpus["records"],
        model_name=Settings().embedding_model,
        cache_dir=EMBEDDING_CACHE_DIR,
    )
    cached = _read_cache()
    cases = []
    for task in expanded_tasks(suite, selection):
        if any(case["case_id"] == task["case_id"] for case in cases):
            continue
        claim, _ = _build_claim(
            "ltr-training",
            f"ltr-{suite['suite_id']}-{task['case_id']}",
            product_request(task),
            utc_now_iso(),
            None,
        )
        cache_key = f"{suite['suite_id']}:{task['case_id']}"
        feature_set = None
        if cached["corpus_sha256"] == corpus["corpus_sha256"]:
            saved = cached["cases"].get(cache_key)
            if saved and saved["claim_hash"] == claim.claim_hash:
                feature_set = saved
        if feature_set is None:
            print(f"features {cache_key}", flush=True)
            features = hybrid_feature_set(
                claim,
                corpus["records"],
                index.similarities(claim),
            )
            feature_set = {
                "claim_hash": claim.claim_hash,
                "features": features.features,
                "fused": {
                    work_id: relevance.score for work_id, relevance in features.fused.items()
                },
            }
            cached = {
                "feature_version": FEATURE_VERSION,
                "embedding_cache_version": EMBEDDING_CACHE_VERSION,
                "semantic_query_version": SEMANTIC_QUERY_VERSION,
                "corpus_sha256": corpus["corpus_sha256"],
                "cases": {**cached["cases"], cache_key: feature_set},
            }
            _write_cache(cached)
        cases.append(
            {
                "suite_id": suite["suite_id"],
                "case_id": task["case_id"],
                "required": task["expect"].get("required_ids", []),
                "features": feature_set["features"],
                "rrf_scores": feature_set["fused"],
            }
        )
    return cases


def _read_cache() -> dict[str, Any]:
    empty = {
        "feature_version": FEATURE_VERSION,
        "embedding_cache_version": EMBEDDING_CACHE_VERSION,
        "semantic_query_version": SEMANTIC_QUERY_VERSION,
        "corpus_sha256": "",
        "cases": {},
    }
    if not CACHE_PATH.is_file():
        return empty
    cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    version_keys = {"feature_version", "embedding_cache_version", "semantic_query_version"}
    if any(cached.get(key) != empty[key] for key in version_keys):
        raise ValueError("stale LTR feature cache; move it aside before rebuilding")
    if not isinstance(cached.get("cases"), dict):
        raise ValueError("invalid LTR feature cache")
    return cached


def _write_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(CACHE_PATH)


def fit(cases: list[dict[str, Any]], parameters=PARAMETERS):
    import lightgbm
    import numpy as np

    features = []
    labels = []
    groups = []
    for case in sorted(cases, key=lambda item: (item["suite_id"], item["case_id"])):
        if not case["required"]:
            continue
        work_ids = sorted(case["features"])
        features.extend(case["features"][work_id] for work_id in work_ids)
        labels.extend(int(work_id in case["required"]) for work_id in work_ids)
        groups.append(len(work_ids))
    if not groups or not any(labels):
        raise ValueError("LTR training requires positive grouped examples")
    dataset = lightgbm.Dataset(
        np.asarray(features, dtype=np.float64),
        label=np.asarray(labels, dtype=np.int32),
        group=groups,
        feature_name=FEATURE_NAMES,
    )
    parameters = {key: value for key, value in parameters.items() if key != "n_estimators"}
    return lightgbm.train(
        parameters,
        dataset,
        num_boost_round=parameters.get("n_estimators", PARAMETERS["n_estimators"]),
    )


def rank(case: dict[str, Any], ranker) -> dict[str, float]:
    work_ids = sorted(case["features"])
    scores = ranker.predict([case["features"][work_id] for work_id in work_ids], num_threads=1)
    return dict(zip(work_ids, map(float, scores), strict=True))


def measure(
    cases: list[dict[str, Any]],
    *,
    training_cases: list[dict[str, Any]],
    cross_validate: bool,
    parameters=PARAMETERS,
) -> dict:
    evaluations = []
    for case in cases:
        if cross_validate:
            fold_train = [
                other
                for other in training_cases
                if (other["suite_id"], other["case_id"]) != (case["suite_id"], case["case_id"])
            ]
            model = fit(fold_train, parameters)
        else:
            model = fit(training_cases, parameters)
        learned = rank(case, model)
        rrf_order = sorted(case["rrf_scores"], key=lambda key: (-case["rrf_scores"][key], key))
        ltr_order = sorted(learned, key=lambda key: (-learned[key], key))
        required = case["required"]
        rrf_ranks = {work_id: _position(rrf_order, work_id) for work_id in required}
        ltr_ranks = {work_id: _position(ltr_order, work_id) for work_id in required}
        evaluations.append(
            {
                "suite_id": case["suite_id"],
                "case_id": case["case_id"],
                "required": required,
                "zero_answer": not required,
                "rrf_ranks": rrf_ranks,
                "ltr_ranks": ltr_ranks,
                "rrf_hit_at_5": bool(required) and _within_five(rrf_ranks.values()),
                "ltr_hit_at_5": bool(required) and _within_five(ltr_ranks.values()),
                "rrf_ndcg_at_5": _ndcg(rrf_ranks) if required else None,
                "ltr_ndcg_at_5": _ndcg(ltr_ranks) if required else None,
                "candidate_count": len(ltr_order),
            }
        )
    scored = [case for case in evaluations if not case["zero_answer"]]
    return {
        "case_count": len(evaluations),
        "positive_case_count": len(scored),
        "rrf_hit_at_5_mean": statistics.fmean(case["rrf_hit_at_5"] for case in scored),
        "ltr_hit_at_5_mean": statistics.fmean(case["ltr_hit_at_5"] for case in scored),
        "rrf_ndcg_at_5_mean": statistics.fmean(case["rrf_ndcg_at_5"] for case in scored),
        "ltr_ndcg_at_5_mean": statistics.fmean(case["ltr_ndcg_at_5"] for case in scored),
        "regressions": [
            case["case_id"] for case in scored if case["rrf_hit_at_5"] and not case["ltr_hit_at_5"]
        ],
        "cases": evaluations,
    }


def _position(order: list[str], work_id: str) -> int | None:
    return order.index(work_id) + 1 if work_id in order else None


def _within_five(ranks) -> bool:
    return all(rank is not None and rank <= 5 for rank in ranks)


def _ndcg(ranks: dict[str, int | None]) -> float:
    ideal = sum(1 / math.log2(position + 1) for position in range(1, len(ranks) + 1))
    return (
        sum(1 / math.log2(rank + 1) for rank in ranks.values() if rank is not None and rank <= 5)
        / ideal
    )


def train(output: Path, model_output: Path, parameters=PARAMETERS) -> dict[str, Any]:
    training = [case for suite, split in TRAINING_SUITES for case in build_cases(suite, split)]
    evaluation = build_cases(EVALUATION_SUITE, "all")
    cv = measure(training, training_cases=training, cross_validate=True, parameters=parameters)
    held_out = measure(
        evaluation, training_cases=training, cross_validate=False, parameters=parameters
    )
    adopted = (
        not cv["regressions"]
        and not held_out["regressions"]
        and cv["ltr_ndcg_at_5_mean"] >= cv["rrf_ndcg_at_5_mean"]
        and held_out["ltr_ndcg_at_5_mean"] >= held_out["rrf_ndcg_at_5_mean"]
        and held_out["ltr_ndcg_at_5_mean"] > held_out["rrf_ndcg_at_5_mean"]
    )
    final_model = fit(training, parameters)
    payload = artifact_payload(
        final_model.model_to_string(),
        training={
            "parameters": parameters,
            "feature_version": FEATURE_VERSION,
            "training_suites": [suite for suite, _ in TRAINING_SUITES],
            "evaluation_suite": EVALUATION_SUITE,
            "evidence_status": "development_only_not_unseen_independent_confirmation",
            "training_case_count": len(training),
            "positive_training_case_count": sum(bool(case["required"]) for case in training),
            "candidate_policy": "union of BM25 and E5 top20; no new retrieval",
            "score_interpretation": "query-local margin only",
            "adopted": adopted,
            "sources": [
                "https://lightgbm.readthedocs.io/en/latest/pythonapi/lightgbm.LGBMRanker.html",
                "https://aclanthology.org/anthology-files/anthology-files/pdf/nllp/2025.nllp-1.10.pdf",
            ],
        },
    )
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if adopted:
        if model_output.exists():
            raise FileExistsError(f"refusing to overwrite model: {model_output}")
        model_output.parent.mkdir(parents=True, exist_ok=True)
        model_output.write_text(serialized, encoding="utf-8")
    result = {
        "schema": "citation-ltr-lightgbm-evaluation-v1",
        "created_at": utc_now_iso(),
        "method": "LightGBM LambdaMART grouped by query; leave-one-case-out development CV",
        "parameters": parameters,
        "feature_version": FEATURE_VERSION,
        "feature_names": FEATURE_NAMES,
        "training_suites": list(TRAINING_SUITES),
        "evaluation_suite": EVALUATION_SUITE,
        "evaluation_evidence": "development, not unseen independent confirmation",
        "model_sha256": payload["model_sha256"],
        "artifact_bytes": len(serialized.encode("utf-8")),
        "cross_validation": cv,
        "evaluation": held_out,
        "adopted": adopted,
        "decision": (
            "Eligible for runtime wiring and full agent regression; "
            "not an independent quality claim."
            if adopted
            else "Keep RRF: no measured gain or a regression violates the adoption gate."
        ),
    }
    if output.exists():
        raise FileExistsError(f"refusing to overwrite evaluation: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-output", type=Path, default=MODEL_PATH)
    parser.add_argument("--monotone", action="store_true")
    args = parser.parse_args()
    parameters = (
        {
            **PARAMETERS,
            "monotone_constraints": [1, 1, 1, 1, 1, 1, 1, 0, -1, -1, 1, 1],
            "monotone_constraints_method": "intermediate",
        }
        if args.monotone
        else PARAMETERS
    )
    result = train(args.output, args.model_output, parameters)
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"cross_validation", "evaluation"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "cv": {
                    key: value
                    for key, value in result["cross_validation"].items()
                    if key != "cases"
                },
                "evaluation": {
                    key: value for key, value in result["evaluation"].items() if key != "cases"
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
