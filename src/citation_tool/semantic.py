"""Optional multilingual dense retrieval with a deterministic sparse/RRF fallback."""

from __future__ import annotations

import logging
import math
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import LibraryConfig, Settings
from .entities import ClaimContext
from .evidence import Candidate
from .jcs import sha256_json
from .metadata_retrieval import MetadataCorpusAdapter, _excludes_corrections
from .publication import canonical_work_id, is_correction
from .relevance import (
    LEXICAL_JOINED_FALLBACK_VERSION,
    QueryRelevance,
    lexical_query_fallback,
    query_plan,
    record_allowed,
    score_records,
    semantic_query_facets,
    semantic_query_text,
)

EMBEDDING_CACHE_VERSION = "e5-query-passage-normalized-v1"
FACET_QUERY_VERSION = "base-plus-topic-facets-v1"
RRF_VERSION = "rrf-k60-both-top20-constant-scale-v1"
SEMANTIC_QUERY_VERSION = "instruction-aware-negation-v3"
logger = logging.getLogger(__name__)
_FASTEMBED_MODEL_LOCK = threading.Lock()
_CUSTOM_MODELS_REGISTERED: set[str] = set()
DEFAULT_HYBRID_WINDOW = 5
RANKED_MATCHES_CACHE_LIMIT = 256


@dataclass(frozen=True, slots=True)
class _RankedMatches:
    matches: dict[str, QueryRelevance]
    facet_count: int
    embedding_revision: str | None


def hybrid_candidate_window(settings: Settings) -> int:
    return 10 if settings.candidate_pipeline == "agentic" else DEFAULT_HYBRID_WINDOW


def reciprocal_rank_fusion(
    sparse: Mapping[str, QueryRelevance],
    dense: Mapping[str, float],
    *,
    k: int = 60,
    candidate_count: int = 20,
) -> dict[str, QueryRelevance]:
    """Fuse ranks, never incomparable raw scores. The maximum has a fixed scale."""
    sparse_order = sorted(sparse, key=lambda key: (-sparse[key].score, key))[:candidate_count]
    dense_order = sorted(dense, key=lambda key: (-dense[key], key))[:candidate_count]
    sparse_ranks = {work_id: rank for rank, work_id in enumerate(sparse_order, 1)}
    dense_ranks = {work_id: rank for rank, work_id in enumerate(dense_order, 1)}
    output = {}
    for work_id in sorted(set(sparse_ranks) | set(dense_ranks)):
        contributions = [
            1 / (k + rank) for rank in (sparse_ranks.get(work_id), dense_ranks.get(work_id)) if rank
        ]
        lexical = sparse.get(work_id)
        output[work_id] = QueryRelevance(
            score=round(100 * sum(contributions) / (2 / (k + 1)), 4),
            matched_identifiers=lexical.matched_identifiers if lexical else (),
            matched_topics=lexical.matched_topics if lexical else (),
        )
    return output


class EmbeddingIndex:
    def __init__(
        self,
        records: list[Mapping[str, Any]],
        *,
        model_name: str,
        cache_dir: Path,
        model_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.model_name = model_name
        self._records = records
        self._ids = [canonical_work_id(record) for record in records]
        self._cache_dir = cache_dir
        self._cache_identity = {
            "model": model_name,
            "version": EMBEDDING_CACHE_VERSION,
            "documents": {
                canonical_work_id(record): f"{record['title']}\n{record['abstract']}"
                for record in records
            },
        }
        self.model_revision = self._find_revision()
        self._cache_path = self._new_cache_path()
        self._model_loader = model_loader or self._load_fastembed
        self._model_lock = threading.Lock()
        self._model: Any = None
        self._vectors: np.ndarray | None = None
        self._text_vectors: dict[str, np.ndarray] = {}
        self._query_vectors: dict[str, np.ndarray] = {}
        self._inference_lock = threading.RLock()
        self._positions = {work_id: position for position, work_id in enumerate(self._ids)}

    def _load_fastembed(self) -> Any:
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        with _FASTEMBED_MODEL_LOCK:
            if (
                self.model_name == "intfloat/multilingual-e5-small"
                and self.model_name not in _CUSTOM_MODELS_REGISTERED
            ):
                TextEmbedding.add_custom_model(
                    model=self.model_name,
                    pooling=PoolingType.MEAN,
                    normalization=True,
                    sources=ModelSource(hf=self.model_name),
                    dim=384,
                    model_file="onnx/model.onnx",
                )
                _CUSTOM_MODELS_REGISTERED.add(self.model_name)
            model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(self._cache_dir / "models"),
                threads=2,
            )
            self.model_revision = self._find_revision()
            self._cache_path = self._new_cache_path()
            return model

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def _new_cache_path(self) -> Path:
        cache_key = sha256_json({**self._cache_identity, "model_revision": self.model_revision})
        return self._cache_dir / f"{cache_key[:32]}.npz"

    def _find_revision(self) -> str:
        name = self.model_name.replace("/", "--")
        for root in (self._cache_dir, self._cache_dir / "models"):
            snapshots = root / f"models--{name}" / "snapshots"
            reference = root / f"models--{name}" / "refs" / "main"
            if reference.is_file():
                revision = reference.read_text(encoding="utf-8").strip()
                if (snapshots / revision / "onnx" / "model.onnx").is_file():
                    return revision
            revisions = sorted(
                path.name
                for path in snapshots.glob("*")
                if (path / "onnx" / "model.onnx").is_file()
            )
            if len(revisions) == 1:
                return revisions[0]
            if len(revisions) > 1:
                raise ValueError("embedding model cache contains ambiguous revisions")
        return "unresolved"

    def _ensure_model(self) -> Any:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    self._model = self._model_loader()
        return self._model

    @staticmethod
    def _normalized(vector: Any) -> list[float]:
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        if not norm or not math.isfinite(norm):
            raise ValueError("embedding returned an invalid vector")
        return [value / norm for value in values]

    def _load_matrix(self) -> np.ndarray:
        if self._vectors is not None:
            return self._vectors
        if self._cache_path.is_file():
            try:
                with np.load(self._cache_path, allow_pickle=False) as cached:
                    vectors = cached["vectors"]
                    ids = cached["ids"].tolist()
                    if (
                        cached["version"].item() == EMBEDDING_CACHE_VERSION
                        and cached["model"].item() == self.model_name
                        and cached["model_revision"].item() == self.model_revision
                        and ids == self._ids
                        and vectors.ndim == 2
                        and vectors.shape[0] == len(self._ids)
                        and np.isfinite(vectors).all()
                    ):
                        self._vectors = vectors
                        return self._vectors
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("embedding cache invalid; rebuilding type=%s", type(exc).__name__)
        documents = [
            f"passage: {record['title']}\n{record['abstract']}" for record in self._records
        ]
        vectors = np.asarray(
            [
                self._normalized(vector)
                for vector in self._ensure_model().embed(documents, batch_size=16)
            ],
            dtype=np.float32,
        )
        if len(vectors) != len(self._ids):
            raise ValueError("embedding count differs from document count")
        self._vectors = vectors
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with temporary.open("wb") as stream:
            np.savez(
                stream,
                version=EMBEDDING_CACHE_VERSION,
                model=self.model_name,
                model_revision=self.model_revision,
                ids=np.asarray(self._ids),
                vectors=self._vectors,
            )
        os.chmod(temporary, 0o600)
        temporary.replace(self._cache_path)
        return self._vectors

    def similarity_to_text(
        self, claim: ClaimContext, work_id: str, title: str, abstract: str
    ) -> float:
        query = f"query: {semantic_query_text(claim)}"
        document = f"{title}\n{abstract}"
        with self._inference_lock:
            if query not in self._query_vectors:
                embedded = list(self._ensure_model().embed([query], batch_size=1))
                if len(embedded) != 1:
                    raise ValueError("embedding did not return one query vector")
                if len(self._query_vectors) >= 128:
                    self._query_vectors.pop(next(iter(self._query_vectors)))
                self._query_vectors[query] = np.asarray(
                    self._normalized(embedded[0]), dtype=np.float32
                )
            position = self._positions.get(work_id)
            if position is not None and self._cache_identity["documents"][work_id] == document:
                vector = self._load_matrix()[position]
            else:
                key = sha256_json(document)
                if key not in self._text_vectors:
                    embedded = list(
                        self._ensure_model().embed([f"passage: {document}"], batch_size=1)
                    )
                    if len(embedded) != 1:
                        raise ValueError("embedding did not return one document vector")
                    if len(self._text_vectors) >= 256:
                        self._text_vectors.pop(next(iter(self._text_vectors)))
                    self._text_vectors[key] = np.asarray(
                        self._normalized(embedded[0]), dtype=np.float32
                    )
                vector = self._text_vectors[key]
            return float(vector @ self._query_vectors[query])

    def similarities(
        self, claim: ClaimContext, *, query_text: str | None = None
    ) -> dict[str, float]:
        query = f"query: {query_text if query_text is not None else semantic_query_text(claim)}"
        embedded = list(self._ensure_model().embed([query], batch_size=1))
        if len(embedded) != 1:
            raise ValueError("embedding did not return one query vector")
        query_vector = np.asarray(self._normalized(embedded[0]), dtype=np.float32)
        scores = self._load_matrix() @ query_vector
        return dict(zip(self._ids, map(float, scores), strict=True))


class SemanticMetadataAdapter(MetadataCorpusAdapter):
    def __init__(
        self,
        library: LibraryConfig,
        records: list[Mapping[str, Any]],
        *,
        settings: Settings,
        index: EmbeddingIndex | None = None,
        dense_threshold: float = 0.80,
        top_k: int = DEFAULT_HYBRID_WINDOW,
    ) -> None:
        super().__init__(library, records, top_k=top_k)
        self.adapter_id = "europepmc-abstract-hybrid-e5-rrf-v1"
        self.model_versions = {
            **self.model_versions,
            "retrieval": "bm25-plus-e5-reciprocal-rank-fusion",
            "reranker": RRF_VERSION,
            "embedding": settings.embedding_model,
            "embedding_cache": EMBEDDING_CACHE_VERSION,
            "semantic_query": SEMANTIC_QUERY_VERSION,
            "dense_threshold": str(dense_threshold),
        }
        self._index = index or EmbeddingIndex(
            self._records,
            model_name=settings.embedding_model,
            cache_dir=settings.embedding_cache_dir,
        )
        self._threshold = dense_threshold
        self._joined_fallback = settings.candidate_pipeline == "agentic"
        self.score_version = f"{settings.embedding_model}:{RRF_VERSION}:min{dense_threshold:.2f}"
        self._sparse_score_version = "metadata-bm25-ontology-v2"
        if self._joined_fallback:
            self.model_versions["lexical_query"] = LEXICAL_JOINED_FALLBACK_VERSION
            self.score_version += f":{LEXICAL_JOINED_FALLBACK_VERSION}"
            self._sparse_score_version += f":{LEXICAL_JOINED_FALLBACK_VERSION}"
        self.score_scale_group = self.score_scale_group.replace(
            "metadata-bm25", "metadata-hybrid", 1
        )
        self._hybrid_score_version = self.score_version
        self._hybrid_score_group = self.score_scale_group
        self._sparse_score_group = self.score_scale_group.replace(
            "metadata-hybrid", "metadata-bm25", 1
        )
        self._matches_cache: dict[str, _RankedMatches] = {}

    def _rank(self, claim: ClaimContext) -> _RankedMatches:
        key = claim.claim_hash
        if key in self._matches_cache:
            return self._matches_cache[key]
        tuples = self._record_tuples
        hard_plan = query_plan(claim)
        lexical_plan = (
            lexical_query_fallback(hard_plan, self._lexical_index.frequencies)
            if self._joined_fallback
            else hard_plan
        )
        sparse = (
            self._lexical_index.score(claim)
            if lexical_plan is hard_plan
            else score_records(claim, tuples, index=self._lexical_index, plan=lexical_plan)
        )
        facets = semantic_query_facets(claim)
        rankings = []
        try:
            for index, facet in enumerate(facets):
                similarities = (
                    self._index.similarities(claim)
                    if index == 0
                    else self._index.similarities(claim, query_text=facet)
                )
                dense = {
                    work_id: score
                    for work_id, title, text, doi in tuples
                    if (score := similarities.get(work_id, 0.0)) >= self._threshold
                    and record_allowed(claim, title, text, doi, plan=hard_plan)
                }
                rankings.append(reciprocal_rank_fusion(sparse, dense))
        except Exception as exc:
            logger.warning(
                "dense retrieval unavailable; sparse fallback type=%s", type(exc).__name__
            )
            return _RankedMatches(sparse, 0, None)
        totals = {}
        for ranking in rankings:
            for work_id, relevance in ranking.items():
                previous = totals.get(work_id)
                totals[work_id] = QueryRelevance(
                    score=(previous.score if previous else 0) + relevance.score,
                    matched_identifiers=(
                        previous.matched_identifiers
                        if previous and previous.matched_identifiers
                        else relevance.matched_identifiers
                    ),
                    matched_topics=(
                        previous.matched_topics
                        if previous and previous.matched_topics
                        else relevance.matched_topics
                    ),
                )
        matches = (
            {
                work_id: QueryRelevance(
                    score=round(relevance.score / len(rankings), 4),
                    matched_identifiers=relevance.matched_identifiers,
                    matched_topics=relevance.matched_topics,
                )
                for work_id, relevance in totals.items()
            }
            if rankings
            else {}
        )
        if len(self._matches_cache) >= RANKED_MATCHES_CACHE_LIMIT:
            self._matches_cache.pop(next(iter(self._matches_cache)))
        ranked = _RankedMatches(matches, len(rankings), self._index.model_revision)
        self._matches_cache[key] = ranked
        return ranked

    def _apply_rank_metadata(self, ranked: _RankedMatches, candidate_count: int) -> None:
        facets = ranked.facet_count
        self.model_versions["facet_count"] = str(facets)
        if facets == 0:
            self.model_versions.pop("embedding_revision", None)
            self.model_versions.update(
                dense_status="unavailable_sparse_fallback",
                facet_retrieval="unavailable_sparse_fallback",
                candidate_pool=f"sparse-top{candidate_count}",
            )
            self.score_version = self._sparse_score_version
            self.score_scale_group = self._sparse_score_group
            return
        assert ranked.embedding_revision is not None
        self.model_versions.update(
            dense_status="available",
            embedding_revision=ranked.embedding_revision,
            facet_retrieval=FACET_QUERY_VERSION if facets > 1 else "single_query",
            candidate_pool=f"mean-rrf-top{candidate_count}"
            if facets > 1
            else f"rrf-top{candidate_count}",
        )
        self.score_version = (
            f"{self._hybrid_score_version}:facets-v1" if facets > 1 else self._hybrid_score_version
        )
        self.score_scale_group = self._hybrid_score_group

    def search(self, claim: ClaimContext) -> list[Candidate]:
        ranked = self._rank(claim)
        matches = ranked.matches
        candidate_count = min(20, max(self._top_k, 2 * ranked.facet_count))
        self._apply_rank_metadata(ranked, candidate_count)
        selected = sorted(
            (
                record
                for record in self._records
                if canonical_work_id(record) in matches
                and not (_excludes_corrections(claim) and is_correction(record))
            ),
            key=lambda record: (
                -matches[canonical_work_id(record)].score,
                canonical_work_id(record),
                self.library.library_id,
            ),
        )[:candidate_count]
        return [
            self._candidate(record, claim, matches[canonical_work_id(record)].score)
            for record in selected
        ]
