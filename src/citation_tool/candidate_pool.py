from __future__ import annotations

import asyncio
import logging

from .candidates import retrieval_fingerprint
from .common_scoring import CommonScorer
from .entities import (
    CandidateDiagnostic,
    CandidateRanking,
    CandidateSourceRecord,
    CandidateSourceScore,
    ClaimContext,
    RankingPhase,
    RecommendationReason,
    RetrievalTrace,
    utc_now_iso,
)
from .evidence import Candidate, build_recommendation
from .jcs import sha256_json
from .listwise import LISTWISE_PROMPT_VERSION, DiscoveryModels, ListwiseReranker, RerankFailure
from .store import Store

logger = logging.getLogger(__name__)


def source_record(candidate: Candidate) -> CandidateSourceRecord:
    payload = {
        "library_id": candidate.passage.library_id,
        "paper_id": candidate.passage.paper_id,
        "title": candidate.source.title or "",
        "abstract": candidate.context,
        "authors": candidate.source.authors,
        "year": candidate.source.year,
        "doi": candidate.source.doi,
        "source_uri": candidate.locator.source_uri,
        "publication_status": candidate.retraction_status,
        "publication_types": sorted(candidate.publication_types or []),
        "related_dois": candidate.source.related_dois,
    }
    return CandidateSourceRecord(**payload, content_hash=sha256_json(payload))


class CandidatePool:
    def __init__(
        self,
        claim: ClaimContext,
        store: Store,
        scorer: CommonScorer,
        models: DiscoveryModels | None,
        semaphore: asyncio.Semaphore,
        *,
        score_review_enabled: bool = True,
    ) -> None:
        self.claim = claim
        self.store = store
        self.scorer = scorer
        self.models = models
        self.reranker = (
            ListwiseReranker(models, store, semaphore, score_review_enabled=score_review_enabled)
            if models
            else None
        )
        self.items: dict[str, CandidateDiagnostic] = {}
        self.evidence: dict[str, Candidate] = {}
        self.version = 0
        self.ranking_version = 0
        self.processed_version = 0
        self.ranking_ok = True
        self._dirty = asyncio.Event()
        self._closed = False
        self._ingest_lock = asyncio.Lock()
        self._task = asyncio.create_task(self._pump())

    async def submit(self, candidate: Candidate, recall: ClaimContext) -> None:
        async with self._ingest_lock:
            record = source_record(candidate)
            work_id = candidate.passage.work_id
            previous = self.items.get(work_id)
            records = (
                {entry.content_hash: entry for entry in previous.source_records} if previous else {}
            )
            records[record.content_hash] = record
            ordered_records = sorted(records.values(), key=lambda entry: entry.content_hash)
            representative = min(
                ordered_records,
                key=lambda entry: (
                    -len(entry.abstract),
                    entry.library_id,
                    entry.paper_id,
                    entry.content_hash,
                ),
            )
            trace = RetrievalTrace(
                library_id=candidate.passage.library_id,
                query=recall.claim_text,
                query_hash=recall.claim_hash,
                index_snapshot=candidate.passage.index_version,
                score=candidate.display_score,
                score_version=candidate.score_version,
                score_scale_group=candidate.score_scale_group,
            )
            traces = (
                {sha256_json(entry.model_dump()): entry for entry in previous.retrieval_traces}
                if previous
                else {}
            )
            traces[sha256_json(trace.model_dump())] = trace
            if (
                previous
                and previous.source_records == ordered_records
                and previous.retrieval_traces == [traces[key] for key in sorted(traces)]
            ):
                return
            if representative.content_hash == record.content_hash:
                self.evidence[work_id] = candidate
            selected = self.evidence[work_id]
            source_scores = (
                {
                    (entry.library_id, entry.score_version, entry.score_scale_group): entry
                    for entry in previous.retrieval_scores
                }
                if previous
                else {}
            )
            if candidate.score_version and candidate.score_scale_group:
                source_scores[
                    (record.library_id, candidate.score_version, candidate.score_scale_group)
                ] = CandidateSourceScore(
                    library_id=record.library_id,
                    score=candidate.display_score,
                    score_version=candidate.score_version,
                    score_scale_group=candidate.score_scale_group,
                )
            if previous and previous.source_records == ordered_records:
                updated = previous.model_copy(
                    update={
                        "retrieval_scores": [source_scores[key] for key in sorted(source_scores)],
                        "retrieval_traces": [traces[key] for key in sorted(traces)],
                    }
                )
                updated = updated.model_copy(
                    update={
                        "retrieval_fingerprint": retrieval_fingerprint(
                            updated, self.claim.claim_hash
                        ),
                    }
                )
                self.items[work_id] = updated
                self.store.commit_candidate_provenance(
                    self.claim.tenant_id, self.claim.query_id, updated
                )
                return
            recommendation = build_recommendation(
                tenant_id=self.claim.tenant_id,
                query_id=self.claim.query_id,
                claim=self.claim,
                candidate=selected,
            )
            states = {entry.publication_status for entry in ordered_records}
            status = (
                "retracted"
                if "retracted" in states
                else "corrected"
                if "corrected" in states
                else next(iter(states))
                if len(states) == 1
                else "unknown"
            )
            related = {
                key: sorted(
                    {doi for entry in ordered_records for doi in entry.related_dois.get(key, [])}
                )
                for key in ("corrects", "corrected_by", "retracted_by")
            }
            eligible = (
                selected.document_type_eligible
                and status != "retracted"
                and bool(representative.abstract.strip())
            )
            diagnostic = CandidateDiagnostic(
                result_id=recommendation.result_id,
                work_id=work_id,
                paper_id=representative.paper_id,
                claim_id=self.claim.claim_id,
                title=representative.title,
                year=representative.year,
                authors=representative.authors,
                doi=representative.doi,
                library_ids=sorted({entry.library_id for entry in ordered_records}),
                publication_status=status,
                related_dois=related,
                relationship="insufficient",
                scope_match=recommendation.evidence[0].scope_match,
                rejection_reasons=sorted(
                    set(recommendation.rejection_reasons)
                    | {"abstract_metadata_only"}
                    | ({"publication_retracted"} if status == "retracted" else set())
                ),
                evidence_excerpt=representative.abstract,
                source_uri=representative.source_uri,
                source_uris={
                    entry.library_id: entry.source_uri
                    for entry in ordered_records
                    if entry.source_uri
                },
                retrieval_scores=[source_scores[key] for key in sorted(source_scores)],
                source_records=ordered_records,
                retrieval_traces=[traces[key] for key in sorted(traces)],
                publication_types=representative.publication_types,
                ranking_status="pending" if eligible else "ineligible",
                score_status="failed",
                quality_note="真实题录与摘要候选，尚无全文证据或引用资格。",
            )
            try:
                diagnostic = await asyncio.to_thread(self.scorer.score, self.claim, diagnostic)
            except Exception as exc:
                logger.warning("common scoring failed type=%s", type(exc).__name__)
                diagnostic = diagnostic.model_copy(
                    update={
                        "score_status": "failed",
                        "ranking_failure_code": "COMMON_SCORING_FAILED",
                    }
                )
            self.items[work_id] = diagnostic
            self.version += 1
            base = sorted(
                (
                    item
                    for item in self.items.values()
                    if item.ranking_status != "ineligible" and item.candidate_score is not None
                ),
                key=lambda item: (-item.candidate_score, item.work_id),
            )
            ranks = {item.work_id: index for index, item in enumerate(base, 1)}
            for identifier, item in list(self.items.items()):
                item = item.model_copy(
                    update={
                        "pool_version": self.version,
                        "base_rank": ranks.get(identifier),
                        "llm_rank": None,
                        "rank_delta": None,
                        "reason": None,
                        "reason_evidence_quote": None,
                        "ranking_status": "ineligible"
                        if item.ranking_status == "ineligible"
                        else "pending",
                        "ranking_version": self.ranking_version,
                    }
                )
                self.items[identifier] = item.model_copy(
                    update={
                        "retrieval_fingerprint": retrieval_fingerprint(item, self.claim.claim_hash)
                    }
                )
            manifest = self._manifest("pending")
            self.store.commit_discovery_pool(
                self.claim.tenant_id,
                self.claim.query_id,
                manifest.model_dump(mode="json"),
                list(self.items.values()),
            )
            self._dirty.set()

    def _pool_hash(self) -> str:
        return sha256_json(
            [
                {
                    "work_id": item.work_id,
                    "sources": [entry.model_dump() for entry in item.source_records],
                    "base_score": item.candidate_score,
                    "version": item.score_version,
                    "ranking_eligible": item.ranking_status != "ineligible",
                }
                for item in sorted(self.items.values(), key=lambda item: item.work_id)
            ]
        )

    def _manifest(self, status: str, **fields) -> CandidateRanking:
        return CandidateRanking(
            claim_id=self.claim.claim_id,
            pool_version=self.version,
            pool_hash=self._pool_hash(),
            ranking_version=self.ranking_version,
            status=status,
            created_at=utc_now_iso(),
            **fields,
        )

    async def _pump(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            await asyncio.sleep(0.08)
            if self.version != self.processed_version:
                await self._rank_current()
            if self._closed and self.version == self.processed_version:
                return
            if self.version != self.processed_version:
                self._dirty.set()

    async def _rank_current(self) -> None:
        version = self.version
        eligible = [item for item in self.items.values() if item.ranking_status != "ineligible"]
        evidence = dict(self.evidence)
        decisions = []
        input_hash = None
        windows = 0
        served = None
        review = None
        failure = None
        failure_details = {}

        async def publish_phase(phase: RankingPhase) -> None:
            if version != self.version:
                return
            try:
                await asyncio.to_thread(
                    self.store.commit_ranking_phase,
                    self.claim.tenant_id,
                    self.claim.query_id,
                    self.claim.claim_id,
                    version,
                    phase,
                )
            except Exception as exc:
                logger.warning("candidate ranking progress unavailable type=%s", type(exc).__name__)

        try:
            if eligible:
                if self.reranker is None:
                    raise RerankFailure("MODEL_NOT_CONFIGURED")
                outcome = await self.reranker.rank(
                    self.claim, eligible, evidence, on_phase=publish_phase
                )
                decisions = outcome.items
                input_hash = outcome.input_hash
                windows = outcome.window_count
                served = outcome.served_model
                review = outcome.score_review
        except RerankFailure as exc:
            failure = exc.code
            failure_details = exc.details
        except Exception as exc:
            logger.warning("candidate rerank failed type=%s", type(exc).__name__)
            failure = "RERANK_INTERNAL_ERROR"
        async with self._ingest_lock:
            if version != self.version:
                return
            self.ranking_version += 1
            self.ranking_ok = failure is None and (review is None or review.status != "failed")
            for index, decision in enumerate(decisions, 1):
                item = self.items[decision.work_id]
                self.items[decision.work_id] = item.model_copy(
                    update={
                        "llm_rank": index,
                        "rank_delta": item.base_rank - index
                        if item.base_rank is not None
                        else None,
                        "ranking_version": self.ranking_version,
                        "ranking_status": "ranked",
                        "ranking_failure_code": None,
                        "reason_evidence_quote": decision.evidence_quote,
                        "reason": RecommendationReason(
                            status="generated",
                            text=decision.reason,
                            provider=self.models.provider,
                            model=self.models.model_name,
                            served_model=served,
                            prompt_version=LISTWISE_PROMPT_VERSION,
                            input_hash=input_hash,
                            generated_at=utc_now_iso(),
                            alignment_score=decision.alignment_score,
                            missing_constraints=decision.missing_constraints,
                        ),
                    }
                )
            if failure:
                for previous_item in eligible:
                    item = self.items[previous_item.work_id]
                    self.items[item.work_id] = item.model_copy(
                        update={
                            "ranking_version": self.ranking_version,
                            "ranking_status": "failed",
                            "ranking_failure_code": failure,
                            "llm_rank": None,
                            "rank_delta": None,
                            "reason": RecommendationReason(
                                status="failed",
                                provider=self.models.provider if self.models else "none",
                                model=self.models.model_name if self.models else "none",
                                prompt_version=LISTWISE_PROMPT_VERSION,
                                input_hash=input_hash or "unavailable",
                                failure_code=failure,
                                generated_at=utc_now_iso(),
                            ),
                        }
                    )
            manifest = self._manifest(
                "failed" if failure else "completed",
                ordered_work_ids=[decision.work_id for decision in decisions],
                input_hash=input_hash,
                model=self.models.model_name if self.models else None,
                prompt_version=LISTWISE_PROMPT_VERSION,
                failure_code=failure,
                failure_details=failure_details,
                window_count=windows,
                score_review=review,
            )
            committed = self.store.commit_discovery_pool(
                self.claim.tenant_id,
                self.claim.query_id,
                manifest.model_dump(mode="json"),
                list(self.items.values()),
                reranked=True,
            )
            if committed:
                self.processed_version = version

    async def finish(self) -> bool:
        self._closed = True
        self._dirty.set()
        await self._task
        return self.ranking_ok

    def request_cancel(self) -> None:
        self._task.cancel()

    async def cancel(self) -> None:
        self.request_cancel()
        await asyncio.gather(self._task, return_exceptions=True)
