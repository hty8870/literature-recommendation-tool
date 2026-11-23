"""Grouped learning-to-rank over the existing BM25/E5 candidate union."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citation_tool.entities import ClaimContext
from citation_tool.publication import canonical_work_id, is_correction, publication_status
from citation_tool.relevance import (
    BM25Index,
    QueryRelevance,
    _query_concepts,
    _query_terms,
    _terms,
    record_allowed,
    score_records,
)
from citation_tool.semantic import reciprocal_rank_fusion

FEATURE_VERSION = "hybrid-query-document-features-v1"
ARTIFACT_FORMAT = "citation-lightgbm-lambdarank-v1"
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
    "retracted",
    "title_term_coverage",
    "requested_type_match",
]
PRIMARY_TYPES = {
    "research-article",
    "clinical trial",
    "randomized controlled trial",
    "multicenter study",
    "journal article",
}


@dataclass(frozen=True, slots=True)
class HybridFeatureSet:
    fused: dict[str, QueryRelevance]
    features: dict[str, list[float]]


def _requested_types(claim: ClaimContext) -> set[str]:
    query = f"{claim.claim_text} {' '.join(claim.query_terms)}".casefold()
    requested = set()
    if re.search(r"\breviews?\b|综述", query):
        requested.add("review")
    if re.search(r"original research|primary (?:research|study|trial)|原始研究|临床试验", query):
        requested.add("primary")
    return requested


def _coverage(matched: int, wanted: int) -> float:
    return matched / wanted if wanted else 0.0


def hybrid_feature_set(
    claim: ClaimContext,
    records: list[Mapping[str, Any]],
    similarities: Mapping[str, float],
    *,
    lexical_index: BM25Index | None = None,
    dense_threshold: float = 0.80,
) -> HybridFeatureSet:
    tuples = [
        (canonical_work_id(record), record["title"], record["abstract"], record["doi"])
        for record in records
    ]
    sparse = score_records(claim, tuples, index=lexical_index)
    dense = {
        work_id: similarities[work_id]
        for work_id, title, text, doi in tuples
        if similarities[work_id] >= dense_threshold and record_allowed(claim, title, text, doi)
    }
    fused = reciprocal_rank_fusion(sparse, dense)
    sparse_order = sorted(sparse, key=lambda key: (-sparse[key].score, key))[:20]
    dense_order = sorted(dense, key=lambda key: (-dense[key], key))[:20]
    sparse_ranks = {work_id: rank for rank, work_id in enumerate(sparse_order, 1)}
    dense_ranks = {work_id: rank for rank, work_id in enumerate(dense_order, 1)}
    wanted_identifiers, wanted_topics, _, _ = _query_concepts(claim)
    query_tokens = set(_query_terms(f"{claim.claim_text} {' '.join(claim.query_terms)}"))
    requested_types = _requested_types(claim)
    by_id = {canonical_work_id(record): record for record in records}
    rows = {}
    for work_id in sorted(set(sparse_order) | set(dense_order)):
        lexical = sparse.get(work_id)
        record = by_id[work_id]
        publication_types = {str(value).casefold() for value in record.get("pub_types", [])}
        review = "review" in publication_types or "review-article" in publication_types
        primary = bool(publication_types & PRIMARY_TYPES) and not review
        type_match = 0.0
        if "review" in requested_types:
            type_match = float(review)
        elif "primary" in requested_types:
            type_match = float(primary and not is_correction(record))
        title_tokens = set(_terms(record["title"]))
        rows[work_id] = [
            lexical.score / 100 if lexical else 0.0,
            max(0.0, min(1.0, similarities[work_id])),
            1 / sparse_ranks[work_id] if work_id in sparse_ranks else 0.0,
            1 / dense_ranks[work_id] if work_id in dense_ranks else 0.0,
            fused[work_id].score / 100,
            _coverage(len(lexical.matched_topics) if lexical else 0, len(wanted_topics)),
            _coverage(len(lexical.matched_identifiers) if lexical else 0, len(wanted_identifiers)),
            float(review),
            float(is_correction(record)),
            float(publication_status(record) == "retracted"),
            _coverage(len(query_tokens & title_tokens), len(query_tokens)),
            type_match,
        ]
    return HybridFeatureSet(fused=fused, features=rows)


class LambdaRanker:
    def __init__(self, artifact: Mapping[str, Any]) -> None:
        if artifact.get("format") != ARTIFACT_FORMAT:
            raise ValueError("unsupported LTR artifact format")
        if artifact.get("feature_version") != FEATURE_VERSION:
            raise ValueError("LTR feature version mismatch")
        if artifact.get("feature_names") != FEATURE_NAMES:
            raise ValueError("LTR feature names mismatch")
        if artifact.get("objective") != "lambdarank":
            raise ValueError("LTR objective must be lambdarank")
        model_text = artifact.get("booster")
        if not isinstance(model_text, str) or not model_text.strip():
            raise ValueError("LTR booster must be non-empty text")
        digest = hashlib.sha256(model_text.encode("utf-8")).hexdigest()
        if artifact.get("model_sha256") != digest:
            raise ValueError("LTR booster checksum mismatch")
        from lightgbm import Booster

        self._booster = Booster(model_str=model_text)
        if self._booster.num_feature() != len(FEATURE_NAMES):
            raise ValueError("LTR booster feature count mismatch")
        self.model_id = f"lightgbm-lambdarank:{digest[:16]}"
        self.training = artifact.get("training", {})

    @classmethod
    def from_path(cls, path: Path) -> LambdaRanker:
        if path.stat().st_size > 20_000_000:
            raise ValueError("LTR artifact exceeds the 20MB limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("LTR artifact must be a JSON object")
        return cls(payload)

    def score(self, rows: Mapping[str, list[float]]) -> dict[str, QueryRelevance]:
        if not rows:
            return {}
        work_ids = sorted(rows)
        matrix = [rows[work_id] for work_id in work_ids]
        if any(len(row) != len(FEATURE_NAMES) for row in matrix):
            raise ValueError("LTR row feature count mismatch")
        raw_scores = self._booster.predict(matrix, num_threads=1)
        output = {}
        for work_id, raw_score in zip(work_ids, raw_scores, strict=True):
            if not math.isfinite(raw_score):
                raise ValueError("LTR produced a non-finite score")
            bounded = 100 / (1 + math.exp(-max(-60.0, min(60.0, raw_score))))
            output[work_id] = QueryRelevance(round(bounded, 4), (), ())
        return output


def artifact_payload(booster_text: str, *, training: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "format": ARTIFACT_FORMAT,
        "feature_version": FEATURE_VERSION,
        "feature_names": FEATURE_NAMES,
        "objective": "lambdarank",
        "model_sha256": hashlib.sha256(booster_text.encode("utf-8")).hexdigest(),
        "booster": booster_text,
        "training": dict(training),
        "score_semantics": "sigmoid of LambdaRank margin; query-local order, not a probability",
    }
