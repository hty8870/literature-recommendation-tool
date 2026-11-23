"""Deterministic evidence verification, eligibility, and score projection.

The M0 adapter supplies text and a pre-computed demo score. This module proves
the quote is an exact UTF-8 byte slice of the stored passage, checks locator and
reading-order facts, and keeps citable/rankable/score grouping separate from
the claim identity. It does not call a model or claim real retrieval quality.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .entities import (
    ClaimContext,
    Evidence,
    EvidenceScope,
    Passage,
    Recommendation,
    RecommendationSource,
    SourceLocator,
    stable_id,
    utc_now_iso,
)

SCORE_SCALE_GROUP = "ssg:fixture-bootstrap-v1"
SCORE_VERSION = "fixture-sparse-bm25-ontology-v2"
SCORE_KIND = "bootstrap"
CALIBRATION_STATUS = "bootstrap"
QUALITY_NOTE = "M0 本地演示夹具分，仅用于组内展示排序；不是真实检索、引用或概率评估。"
SCOPE_ORDER = {"match": 2, "partial": 1, "mismatch": 0, "unknown": 0}
ROLE_SUPPORT = {
    "primary_result": {"result", "discussion", "background"},
    "method": {"method"},
    "background": {"background", "topic"},
    "review_secondary": {"topic", "background", "discussion"},
    "correction_notice": set(),
}


@dataclass(frozen=True, slots=True)
class Candidate:
    passage: Passage
    quote: str
    display_score: float
    evidence_role: str
    locator: SourceLocator
    source: RecommendationSource
    scope: EvidenceScope
    context: str
    relationship: str = "support"
    scope_match: str | None = None
    display_quote: str | None = None
    document_type_eligible: bool = True
    retraction_status: str = "none"
    min_reading_order_confidence: float = 0.8
    publication_types: list[str] | None = None
    score_version: str | None = None
    score_scale_group: str | None = None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SCOPE_ALIASES = {
    "species": (
        {"soybean", "glycine max", "大豆"},
        {"rice", "oryza sativa", "水稻"},
        {"arabidopsis", "arabidopsis thaliana", "拟南芥"},
        {"maize", "zea mays", "corn", "玉米"},
    ),
    "system": (
        {"greenhouse", "controlled greenhouse", "温室"},
        {"field", "field trial", "open field", "田间"},
        {"in vitro", "体外"},
    ),
    "method": (
        {"expression profiling", "rna-seq", "rna sequencing", "表达分析"},
        {"association analysis", "association study", "关联分析"},
        {"knockdown", "gene knockdown", "基因敲低"},
    ),
}


def _scope_values(field: str, values: list[str]) -> set[str]:
    normalized = {value.strip().casefold() for value in values if value.strip()}
    expanded = set(normalized)
    for aliases in _SCOPE_ALIASES.get(field, ()):
        if normalized & aliases:
            expanded.update(aliases)
    return expanded


def _scope_match(claim_scope: EvidenceScope, evidence_scope: EvidenceScope) -> str:
    outcomes = []
    for field in ("species", "system", "condition", "dose", "method", "population"):
        claim_values = _scope_values(field, getattr(claim_scope, field))
        if not claim_values:
            continue
        evidence_values = _scope_values(field, getattr(evidence_scope, field))
        if not evidence_values:
            outcomes.append("unknown")
        elif claim_values & evidence_values:
            outcomes.append("match")
        else:
            outcomes.append("mismatch")
    if not outcomes:
        return "unknown"
    if "mismatch" in outcomes:
        return "mismatch"
    if "unknown" in outcomes:
        return "unknown"
    return "match"


def scope_applicability(evidence_items: list[Evidence]) -> int:
    return max((SCOPE_ORDER[item.scope_match] for item in evidence_items), default=0)


def evidence_completeness(evidence_items: list[Evidence]) -> int:
    return sum(1 for item in evidence_items if item.source_trace_valid)


def _verify_evidence(claim: ClaimContext, candidate: Candidate) -> Evidence:
    passage = candidate.passage
    raw_bytes = passage.raw_text.encode("utf-8")
    quote_bytes = candidate.quote.encode("utf-8")
    found = raw_bytes.find(quote_bytes)
    text_offset_valid = found >= 0 and raw_bytes[found : found + len(quote_bytes)] == quote_bytes
    start_offset = found if found >= 0 else 0
    end_offset = start_offset + len(quote_bytes) if found >= 0 else 0
    quote_sha = sha256_text(candidate.quote)
    raw_sha = sha256_text(passage.raw_text)
    locator = candidate.locator
    pdf_location_valid = (
        locator.pdf_page is not None
        and passage.page_start is not None
        and passage.page_end is not None
        and passage.page_start <= locator.pdf_page <= passage.page_end
    )
    reading_order_valid = (
        locator.reading_order_confidence is not None
        and locator.reading_order_confidence >= candidate.min_reading_order_confidence
        and locator.reading_order_index == passage.reading_order_index
    )
    source_trace_valid = text_offset_valid and pdf_location_valid and reading_order_valid
    match = candidate.scope_match or _scope_match(claim.scope, candidate.scope)
    return Evidence(
        evidence_id=stable_id(
            "ev_",
            claim.claim_id,
            passage.paper_id,
            passage.passage_id,
            str(start_offset),
            str(end_offset),
        ),
        work_id=passage.work_id,
        paper_id=passage.paper_id,
        passage_id=passage.passage_id,
        claim_id=claim.claim_id,
        evidence_role=candidate.evidence_role,
        quote=candidate.quote,
        quote_sha256=quote_sha,
        raw_text_sha256=raw_sha,
        display_quote=candidate.display_quote,
        context=candidate.context,
        start_offset=start_offset,
        end_offset=end_offset,
        locator=locator,
        parser_version=passage.parser_version,
        normalization_version=passage.normalization_version,
        source_resolver_id=f"fixture-resolver:{passage.library_id}:v1",
        ocr_confidence=passage.ocr_confidence,
        text_offset_valid=text_offset_valid,
        pdf_location_valid=pdf_location_valid,
        reading_order_valid=reading_order_valid,
        source_trace_valid=source_trace_valid,
        scope=candidate.scope,
        scope_match=match,
        created_at=utc_now_iso(),
    )


def build_recommendation(
    *, tenant_id: str, query_id: str, claim: ClaimContext, candidate: Candidate
) -> Recommendation:
    evidence = _verify_evidence(claim, candidate)
    source = candidate.source
    source_ok = (
        source.entitlement_status == "allowed"
        and source.metadata_verified
        and candidate.document_type_eligible
        and candidate.retraction_status != "retracted"
    )
    trace_ok = evidence.source_trace_valid
    scope_ok = evidence.scope_match in {"match", "partial"}
    role_ok = claim.claim_type in ROLE_SUPPORT.get(evidence.evidence_role, set())
    relationship = (
        candidate.relationship
        if trace_ok and scope_ok and role_ok and source_ok
        else "insufficient"
    )
    citable = relationship in {"support", "complement", "contrast"} and source_ok
    rankable = citable
    rejection_reasons = []
    if not trace_ok:
        rejection_reasons.append("source_trace_invalid")
    if not scope_ok:
        rejection_reasons.append(
            "evidence_scope_missing" if evidence.scope_match == "unknown" else "scope_mismatch"
        )
    if not role_ok:
        rejection_reasons.append("evidence_role_insufficient")
    if not source_ok:
        rejection_reasons.append("source_ineligible")
    if citable is False and not rejection_reasons:
        rejection_reasons.append("relationship_insufficient")
    result_id = stable_id(
        "res_", tenant_id, query_id, candidate.passage.work_id, claim.claim_id, "identity-v1"
    )
    now = utc_now_iso()
    return Recommendation(
        result_id=result_id,
        tenant_id=tenant_id,
        query_id=query_id,
        work_id=candidate.passage.work_id,
        claim=claim,
        sources=[source],
        evidence=[evidence],
        relationship=relationship,
        verification_status="verified" if trace_ok else "insufficient",
        citable=citable,
        rankable=rankable,
        rankable_group=(SCORE_SCALE_GROUP if rankable else ("evidence_only" if citable else None)),
        display_score=candidate.display_score if rankable else None,
        score_version=SCORE_VERSION if rankable else None,
        score_scale_group=SCORE_SCALE_GROUP if rankable else None,
        score_kind=SCORE_KIND if rankable else None,
        calibration_status=CALIBRATION_STATUS,
        score_status="scored" if rankable else "incomparable",
        provisional=True,
        simulation=source.simulation,
        rejection_reasons=rejection_reasons,
        quality_note=QUALITY_NOTE,
        created_at=now,
        updated_at=now,
    )
