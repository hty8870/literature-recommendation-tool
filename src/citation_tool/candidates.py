"""Arrival-order-independent projection of the same Work's source candidates."""

from __future__ import annotations

from .entities import CandidateDiagnostic, CandidateSourceScore, RecommendationReason
from .evidence import Candidate
from .jcs import sha256_json
from .reasons import ALIGNMENT_POLICY_VERSION, REASON_PROMPT_VERSION


def candidate_scoring(
    candidate: Candidate,
    reason: RecommendationReason | None,
    *,
    provider: str,
    supports_alignment: bool,
) -> tuple[float | None, str | None, str | None]:
    if candidate.source.source_role != "metadata_api":
        return candidate.display_score, candidate.score_version, candidate.score_scale_group
    if (
        not supports_alignment
        or reason is None
        or reason.status != "generated"
        or reason.alignment_score is None
    ):
        return None, None, None
    return (
        round(100 * reason.alignment_score / 3, 4),
        f"llm-{provider}-{REASON_PROMPT_VERSION}",
        f"metadata-common-alignment:{ALIGNMENT_POLICY_VERSION}",
    )


def candidate_sort_key(candidate: CandidateDiagnostic) -> tuple:
    if candidate.ranking_status != "disabled":
        return (
            candidate.ranking_status != "ranked",
            candidate.llm_rank or 10_000,
            candidate.ranking_status == "ineligible",
            candidate.base_rank or 10_000,
            candidate.work_id,
        )
    score = candidate.candidate_score if candidate.score_status == "scoped" else None
    return score is None, -(score or 0), (candidate.title or "").casefold(), candidate.work_id


def _merged_source_scores(
    previous: CandidateDiagnostic, incoming: CandidateDiagnostic
) -> list[CandidateSourceScore]:
    unique = {
        (item.library_id, item.score_version, item.score_scale_group): item
        for candidate in (previous, incoming)
        for item in candidate.retrieval_scores
    }
    return [unique[key] for key in sorted(unique)]


def retrieval_fingerprint(candidate: CandidateDiagnostic, claim_hash: str) -> str:
    return sha256_json(
        {
            "fingerprint_version": "candidate-order-v1",
            "claim_hash": claim_hash,
            "work_id": candidate.work_id,
            "title": candidate.title,
            "doi": candidate.doi,
            "year": candidate.year,
            "authors": candidate.authors,
            "publication_types": sorted(candidate.publication_types),
            "publication_status": candidate.publication_status,
            "related_dois": candidate.related_dois,
            "rejection_reasons": sorted(candidate.rejection_reasons),
            "candidate_score": candidate.candidate_score,
            "retrieval_scores": [
                item.model_dump(mode="json") for item in candidate.retrieval_scores
            ],
            "score_version": candidate.score_version,
            "score_scale_group": candidate.score_scale_group,
            "abstract_sha256": sha256_json(candidate.evidence_excerpt),
        }
    )


def merge_candidates(
    previous: CandidateDiagnostic, incoming: CandidateDiagnostic, claim_hash: str = ""
) -> CandidateDiagnostic:
    if previous.result_id != incoming.result_id or previous.work_id != incoming.work_id:
        raise ValueError("only source candidates for the same work and claim may be merged")
    if previous.claim_id != incoming.claim_id:
        raise ValueError("claims must not be merged")
    status = previous.publication_status
    if "retracted" in {status, incoming.publication_status}:
        status = "retracted"
    elif "corrected" in {status, incoming.publication_status}:
        status = "corrected"
    elif status != incoming.publication_status:
        status = "unknown"
    libraries = sorted({*previous.library_ids, *incoming.library_ids})
    relations = {
        key: sorted(
            {
                *previous.related_dois.get(key, []),
                *incoming.related_dois.get(key, []),
            }
        )
        for key in ("corrects", "corrected_by", "retracted_by")
    }
    source_uris = {**previous.source_uris, **incoming.source_uris}
    for candidate in (previous, incoming):
        for library_id in candidate.library_ids:
            if candidate.source_uri:
                source_uris.setdefault(library_id, candidate.source_uri)
    source_uris = dict(sorted(source_uris.items()))
    reason = previous.reason
    if (reason is None or reason.status != "generated") and incoming.reason is not None:
        reason = incoming.reason
    score = max(
        (
            value
            for value in (previous.candidate_score, incoming.candidate_score)
            if value is not None
        ),
        default=None,
    )
    score_version = previous.score_version or incoming.score_version
    score_group = previous.score_scale_group or incoming.score_scale_group
    score_status = "scoped" if score is not None else "failed"
    if (
        previous.score_status == "incomparable"
        or incoming.score_status == "incomparable"
        or (
            previous.candidate_score is not None
            and incoming.candidate_score is not None
            and (
                previous.score_version != incoming.score_version
                or previous.score_scale_group != incoming.score_scale_group
            )
        )
    ):
        score = score_version = score_group = None
        score_status = "incomparable"
    merged = previous.model_copy(
        update={
            "library_ids": libraries,
            "publication_status": status,
            "related_dois": relations,
            "source_uris": source_uris,
            "source_uri": next(iter(source_uris.values()), None),
            "candidate_score": score,
            "retrieval_scores": _merged_source_scores(previous, incoming),
            "score_version": score_version,
            "score_scale_group": score_group,
            "score_status": score_status,
            "rejection_reasons": sorted({*previous.rejection_reasons, *incoming.rejection_reasons}),
            "reason": reason,
        }
    )
    return merged.model_copy(
        update={
            "retrieval_fingerprint": (
                retrieval_fingerprint(merged, claim_hash)
                if claim_hash
                else previous.retrieval_fingerprint or incoming.retrieval_fingerprint
            )
        }
    )
