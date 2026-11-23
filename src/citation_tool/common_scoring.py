from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping

from .entities import CandidateDiagnostic, ClaimContext, EvidenceScope, MatchAnnotation
from .jcs import sha256_json
from .relevance import (
    DOI,
    TOPIC_ALIASES,
    BM25Index,
    _identifier_pattern,
    _query_concepts,
    _query_terms,
    _terms,
)
from .retrieval import LibraryAdapter
from .semantic import EmbeddingIndex

COMMON_SCORE_VERSION = "common-fixed-background-v1"


def evaluation_claim(claim: ClaimContext) -> ClaimContext:
    anchor = claim.evaluation_anchor
    if anchor is None:
        return claim
    return claim.model_copy(
        update={
            "claim_text": anchor.claim_text,
            "scope": anchor.scope if anchor.scope is not None else EvidenceScope(),
            "query_terms": [],
            "topic_context": [],
        }
    )


def match_annotations(claim: ClaimContext, item: CandidateDiagnostic) -> list[MatchAnnotation]:
    query = evaluation_claim(claim)
    identifiers, topics, _, _ = _query_concepts(query)
    terms = sorted(set(_query_terms(query.claim_text) + query.query_terms))
    patterns = [("identifier", term, _identifier_pattern(term)) for term in identifiers]
    patterns.extend(("term", term, re.escape(term)) for term in terms if term not in identifiers)
    patterns.extend(
        ("topic", concept, re.escape(alias))
        for concept in topics
        for alias in TOPIC_ALIASES[concept]
    )
    annotations = {}
    for field, text in (("title", item.title or ""), ("abstract", item.evidence_excerpt)):
        for kind, term, pattern in patterns:
            boundary = rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])" if term.isascii() else pattern
            match = re.search(boundary, text, re.I)
            if match:
                annotation = MatchAnnotation(
                    field=field,
                    kind=kind,
                    term=term,
                    quote=match.group(),
                    start=match.start(),
                    end=match.end(),
                )
                annotations[(field, kind, term)] = annotation
    doi_query = DOI.search(query.claim_text)
    if (
        doi_query
        and item.doi
        and item.doi.casefold() == doi_query.group().rstrip(".,;:!?").casefold()
    ):
        annotations[("doi", "doi", item.doi)] = MatchAnnotation(
            field="doi", kind="doi", term=item.doi, quote=item.doi, start=0, end=len(item.doi)
        )
    return [annotations[key] for key in sorted(annotations)]


class CommonScorer:
    def __init__(self, adapters: Mapping[str, LibraryAdapter]) -> None:
        backgrounds = [getattr(adapter, "_records", []) for adapter in adapters.values()]
        records = max(backgrounds, key=len, default=[])
        self._background = (
            BM25Index(
                [
                    (
                        record["work_id"],
                        record["title"],
                        record["abstract"],
                        record.get("doi") or "",
                    )
                    for record in records
                ]
            )
            if records
            else None
        )
        self._dense: EmbeddingIndex | None = next(
            (
                adapter._index
                for adapter in adapters.values()
                if isinstance(getattr(adapter, "_index", None), EmbeddingIndex)
            ),
            None,
        )
        self.version = COMMON_SCORE_VERSION + (":e5" if self._dense else ":lexical")
        self.group = (
            "common:"
            + sha256_json(
                {
                    "background": sorted(
                        (record["work_id"], sha256_json([record["title"], record["abstract"]]))
                        for record in records
                    ),
                    "version": self.version,
                    "embedding_model": self._dense.model_name if self._dense else None,
                    "embedding_revision": self._dense.model_revision if self._dense else None,
                }
            )[:24]
        )

    def score(self, claim: ClaimContext, item: CandidateDiagnostic) -> CandidateDiagnostic:
        query = evaluation_claim(claim)
        text = f"{item.title or ''}\n{item.evidence_excerpt}"
        counts = Counter(_terms(text))
        terms = sorted(set(_query_terms(f"{query.claim_text} {' '.join(query.query_terms)}")))
        raw = 0.0
        for term in terms:
            frequency = counts[term]
            if not frequency:
                continue
            background = self._background
            size = len(background.documents) if background else 1
            df = background.frequencies[term] if background else 0
            average = background.average_length if background else 180
            idf = math.log(1 + (size - df + 0.5) / (df + 0.5))
            raw += (
                idf
                * frequency
                * 2.5
                / (frequency + 1.5 * (0.25 + 0.75 * sum(counts.values()) / average))
            )
        annotations = match_annotations(query, item)
        identifiers, _, _, _ = _query_concepts(query)
        matched_ids = {entry.term for entry in annotations if entry.kind == "identifier"}
        term_coverage = sum(bool(counts[term]) for term in terms) / max(1, len(terms))
        identity_coverage = len(matched_ids) / len(identifiers) if identifiers else term_coverage
        lexical = raw / (raw + 8)
        dense = (
            self._dense.similarity_to_text(
                query, item.work_id, item.title or "", item.evidence_excerpt
            )
            if self._dense
            else None
        )
        score = (
            60 * max(0, dense) + 25 * lexical + 15 * identity_coverage
            if dense is not None
            else 75 * lexical + 25 * identity_coverage
        )
        return item.model_copy(
            update={
                "candidate_score": round(min(100, max(0, score)), 4),
                "score_version": self.version,
                "score_scale_group": self.group,
                "score_status": "scoped",
                "match_annotations": annotations,
                "common_features": {
                    "bm25_raw": round(raw, 6),
                    "lexical_saturation": round(lexical, 6),
                    "dense_cosine": round(dense, 6) if dense is not None else None,
                    "identifier_coverage": round(identity_coverage, 6),
                    "term_coverage": round(term_coverage, 6),
                },
            }
        )
