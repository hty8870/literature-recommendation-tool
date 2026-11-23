"""Public M0 contracts, including source provenance and versioned candidate rankings."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

InputType = Literal["keywords", "abstract", "full_text"]
ClaimType = Literal["background", "method", "result", "discussion", "topic"]
SearchState = Literal[
    "created", "running", "draining", "finalizing", "completed", "partial", "failed", "cancelled"
]
WorkerState = Literal[
    "pending", "running", "draining", "completed", "failed", "timed_out", "cancelled"
]
Relationship = Literal["support", "complement", "contrast", "insufficient"]
RankingPhase = Literal["blind", "score_review"]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return prefix + digest[:32]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvidenceScope(StrictModel):
    species: list[str] = Field(default_factory=list)
    system: list[str] = Field(default_factory=list)
    condition: list[str] = Field(default_factory=list)
    dose: list[str] = Field(default_factory=list)
    method: list[str] = Field(default_factory=list)
    population: list[str] = Field(default_factory=list)
    free_text: str = ""


class EvaluationAnchor(StrictModel):
    claim_text: str = Field(min_length=1)
    scope: EvidenceScope | None = None


class BoundingBox(StrictModel):
    page: int = Field(ge=1)
    x0: float
    y0: float
    x1: float
    y1: float
    origin: Literal["top_left", "bottom_left"] = "top_left"


class SourceLocator(StrictModel):
    pdf_page: int | None = Field(default=None, ge=1)
    printed_page: str | None = None
    section_path: list[str] = Field(default_factory=list)
    paragraph_index: int | None = Field(default=None, ge=0)
    reading_order_index: int | None = Field(default=None, ge=0)
    reading_order_confidence: float | None = Field(default=None, ge=0, le=1)
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    source_uri: str | None = None


class ClaimContext(StrictModel):
    tenant_id: str
    claim_id: str
    query_id: str
    anchor_hash: str
    claim_rule_version: str
    input_type: InputType
    claim_type: ClaimType
    claim_text: str
    topic_context: list[str] = Field(default_factory=list)
    query_terms: list[str] = Field(default_factory=list)
    manuscript_anchor: dict | None = None
    scope: EvidenceScope = Field(default_factory=EvidenceScope)
    evaluation_anchor: EvaluationAnchor | None = None
    claim_index: int = Field(ge=0)
    claim_hash: str
    created_at: str


class Passage(StrictModel):
    passage_id: str
    work_id: str
    paper_id: str
    library_id: str
    raw_text: str
    normalized_text: str
    section_path: list[str] = Field(default_factory=list)
    paragraph_index: int = Field(ge=0)
    reading_order_index: int = Field(ge=0)
    layout_quality: Literal["high", "medium", "low"] = "high"
    ocr_confidence: float | None = None
    page_start: int | None = None
    page_end: int | None = None
    printed_page_start: str | None = None
    printed_page_end: str | None = None
    bounding_boxes: list[BoundingBox] = Field(default_factory=list)
    parser_version: str = "fixture-parser-v1"
    normalization_version: str = "fixture-normalization-v1"
    embedding_version: str | None = None
    index_version: str = "fixture-index-v1"
    created_at: str
    updated_at: str


class SearchOptions(StrictModel):
    library_delays_ms: dict[str, int] = Field(default_factory=dict)
    soft_deadline_ms: int | None = Field(default=None, ge=100)


class SearchCreate(StrictModel):
    input_type: InputType
    keywords: list[str] | None = None
    topic_context: list[str] | None = None
    abstract: str | None = None
    manuscript_id: str | None = None
    manuscript_passage_index: int | None = Field(default=None, ge=0)
    library_ids: list[str] | None = None
    claim_scope: EvidenceScope | None = None
    options: SearchOptions = Field(default_factory=SearchOptions)

    @model_validator(mode="after")
    def exactly_one_input(self) -> SearchCreate:
        if self.input_type == "keywords":
            if not self.keywords or not any(item.strip() for item in self.keywords):
                raise ValueError("keywords input requires at least one non-empty keyword")
            if self.abstract is not None or self.manuscript_id is not None:
                raise ValueError("keywords input must not include abstract or manuscript_id")
            if self.manuscript_passage_index is not None:
                raise ValueError("keywords input must not include manuscript_passage_index")
        elif self.input_type == "abstract":
            if self.abstract is None or not self.abstract.strip():
                raise ValueError("abstract input requires a non-empty abstract")
            if self.keywords is not None or self.manuscript_id is not None:
                raise ValueError("abstract input must not include keywords or manuscript_id")
            if self.manuscript_passage_index is not None:
                raise ValueError("abstract input must not include manuscript_passage_index")
        else:
            if self.manuscript_id is None or not self.manuscript_id.strip():
                raise ValueError("full_text input requires manuscript_id")
            if self.keywords is not None or self.abstract is not None:
                raise ValueError("full_text input must not include keywords or abstract")
        return self


class LibraryInfo(StrictModel):
    library_id: str
    display_name: str
    adapter_id: str
    simulation: bool
    default_delay_ms: int
    notice: str
    source_type: Literal["fixture", "local_snapshot", "remote_api"] = "fixture"
    record_count: int | None = Field(default=None, ge=0)


class SearchCreated(StrictModel):
    query_id: str
    search_state: SearchState
    claims: list[ClaimContext]
    events_url: str
    adapter_mode: str
    scope_source: str
    simulation: bool
    notice: str


class SnapshotCreate(StrictModel):
    kind: Literal["checkpoint", "final"]


class RecommendationSource(StrictModel):
    paper_id: str
    library_id: str
    source_role: Literal[
        "publisher_pdf", "publisher_api", "metadata_api", "review_synthesis", "other"
    ]
    entitlement_status: Literal["allowed", "restricted", "unknown"]
    metadata_verified: bool
    simulation: bool
    adapter_id: str
    display_name: str
    title: str | None = None
    authors: str | None = None
    year: int | None = None
    doi: str | None = None
    publication_status: Literal["none", "retracted", "corrected", "unknown"] = "unknown"
    related_dois: dict[str, list[str]] = Field(default_factory=dict)


class Evidence(StrictModel):
    evidence_id: str
    work_id: str
    paper_id: str
    passage_id: str
    claim_id: str
    evidence_role: Literal[
        "primary_result", "method", "background", "review_secondary", "correction_notice"
    ]
    quote: str
    quote_sha256: str
    raw_text_sha256: str
    display_quote: str | None = None
    context: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    locator: SourceLocator
    parser_version: str
    normalization_version: str
    source_resolver_id: str
    ocr_confidence: float | None = None
    text_offset_valid: bool
    pdf_location_valid: bool
    reading_order_valid: bool
    source_trace_valid: bool
    scope: EvidenceScope
    scope_match: Literal["match", "partial", "mismatch", "unknown"]
    created_at: str


class RecommendationReason(StrictModel):
    status: Literal["generated", "failed", "unavailable"]
    text: str | None = Field(default=None, max_length=500)
    provider: str
    model: str
    served_model: str | None = None
    prompt_version: str
    input_hash: str
    generated_at: str
    failure_code: str | None = None
    alignment_score: int | None = Field(default=None, ge=0, le=3)
    missing_constraints: list[str] = Field(default_factory=list, max_length=8)


class Recommendation(StrictModel):
    result_id: str
    tenant_id: str
    query_id: str
    work_id: str
    claim: ClaimContext
    sources: list[RecommendationSource]
    evidence: list[Evidence]
    relationship: Relationship | None
    verification_status: Literal["pending", "verified", "insufficient", "error"]
    citable: bool
    rankable: bool
    rankable_group: str | None
    display_score: float | None = Field(default=None, ge=0, le=100)
    score_version: str | None = None
    score_scale_group: str | None = None
    score_kind: Literal["global_rerank", "source_calibration", "bootstrap"] | None = None
    calibration_status: Literal["bootstrap", "calibrated", "uncalibrated"]
    score_status: Literal["pending", "scored", "failed", "incomparable"]
    provisional: bool = True
    result_version: int = Field(default=1, ge=1)
    insertion_anchor: dict | None = None
    citation_text_id: str | None = None
    simulation: bool = True
    reason: RecommendationReason | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    quality_note: str
    created_at: str
    updated_at: str


class CandidateSourceScore(StrictModel):
    library_id: str
    score: float = Field(ge=0, le=100)
    score_version: str
    score_scale_group: str


class CandidateSourceRecord(StrictModel):
    library_id: str
    paper_id: str
    title: str
    abstract: str
    authors: str | None = None
    year: int | None = None
    doi: str | None = None
    source_uri: str | None = None
    publication_status: Literal["none", "retracted", "corrected", "unknown"] = "unknown"
    publication_types: list[str] = Field(default_factory=list)
    related_dois: dict[str, list[str]] = Field(default_factory=dict)
    content_hash: str


class RetrievalTrace(StrictModel):
    library_id: str
    query: str
    query_hash: str
    index_snapshot: str
    score: float
    score_version: str | None = None
    score_scale_group: str | None = None


class MatchAnnotation(StrictModel):
    field: Literal["title", "abstract", "doi"]
    kind: Literal["identifier", "term", "topic", "doi"]
    term: str
    quote: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class CandidateScoreReview(StrictModel):
    status: Literal["kept", "revised", "not_applicable", "failed"]
    blind_ordered_work_ids: list[str] = Field(min_length=1)
    window_count: int = Field(default=0, ge=0)
    served_model: str | None = None
    failure_code: str | None = None
    failure_details: dict = Field(default_factory=dict)


class CandidateRanking(StrictModel):
    claim_id: str
    pool_version: int = Field(ge=0)
    pool_hash: str
    ranking_version: int = Field(ge=0)
    status: Literal["pending", "completed", "failed", "unavailable"]
    phase: RankingPhase | None = None
    ordered_work_ids: list[str] = Field(default_factory=list)
    input_hash: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    failure_code: str | None = None
    window_count: int = Field(default=0, ge=0)
    failure_details: dict = Field(default_factory=dict)
    score_review: CandidateScoreReview | None = None
    created_at: str


class CandidateDiagnostic(StrictModel):
    result_id: str
    work_id: str
    paper_id: str
    claim_id: str
    title: str | None = None
    year: int | None = None
    authors: str | None = None
    doi: str | None = None
    library_ids: list[str]
    publication_status: Literal["none", "retracted", "corrected", "unknown"] = "unknown"
    related_dois: dict[str, list[str]] = Field(default_factory=dict)
    relationship: Relationship
    scope_match: Literal["match", "partial", "mismatch", "unknown"]
    rejection_reasons: list[str] = Field(min_length=1)
    reason: RecommendationReason | None = None
    evidence_excerpt: str = ""
    candidate_score: float | None = Field(default=None, ge=0, le=100)
    retrieval_scores: list[CandidateSourceScore] = Field(default_factory=list)
    score_version: str | None = None
    score_scale_group: str | None = None
    score_status: Literal["scoped", "incomparable", "failed"] = "scoped"
    publication_types: list[str] = Field(default_factory=list)
    source_uri: str | None = None
    source_uris: dict[str, str] = Field(default_factory=dict)
    retrieval_fingerprint: str | None = None
    source_records: list[CandidateSourceRecord] = Field(default_factory=list)
    retrieval_traces: list[RetrievalTrace] = Field(default_factory=list)
    match_annotations: list[MatchAnnotation] = Field(default_factory=list)
    common_features: dict[str, float | None] = Field(default_factory=dict)
    base_rank: int | None = Field(default=None, ge=1)
    llm_rank: int | None = Field(default=None, ge=1)
    rank_delta: int | None = None
    pool_version: int = Field(default=0, ge=0)
    ranking_version: int = Field(default=0, ge=0)
    ranking_status: Literal["pending", "ranked", "failed", "ineligible", "disabled"] = "disabled"
    ranking_failure_code: str | None = None
    reason_evidence_quote: str | None = None
    quality_note: str


class WorkerStatus(StrictModel):
    tenant_id: str
    query_id: str
    library_id: str
    attempt: int = Field(ge=1)
    worker_id: str
    lane_id: str
    index_snapshot: str
    model_versions: dict[str, str] = Field(default_factory=dict)
    worker_state: WorkerState
    candidates_seen: int = Field(default=0, ge=0)
    candidates_verified: int = Field(default=0, ge=0)
    recommendations_emitted: int = Field(default=0, ge=0)
    last_emitted_sequence: int | None = Field(default=None, ge=1)
    emitted_event_count: int = Field(default=0, ge=0)
    local_watermark: int | None = Field(default=None, ge=1)
    drained: bool = False
    started_at: str | None = None
    ended_at: str | None = None
    heartbeat_at: str | None = None
    last_error: dict | None = None
    source_agent_status: Literal["disabled", "running", "completed", "failed", "unavailable"] = (
        "disabled"
    )
    source_actions: list[dict] = Field(default_factory=list)
    source_decision: str | None = Field(default=None, max_length=4000)
    source_candidates_submitted: int = Field(default=0, ge=0)
    source_available_work_ids: list[str] = Field(default_factory=list)
    updated_at: str


class ClaimCoverage(StrictModel):
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    partial: int = Field(ge=0)
    failed: int = Field(ge=0)
    not_started: int = Field(ge=0)

    @model_validator(mode="after")
    def sums_to_total(self) -> ClaimCoverage:
        if self.completed + self.partial + self.failed + self.not_started != self.total:
            raise ValueError("claim coverage buckets must sum to total")
        return self


class Coverage(StrictModel):
    total_library_count: int = Field(ge=0)
    completed_library_ids: list[str] = Field(default_factory=list)
    failed_library_ids: list[str] = Field(default_factory=list)
    timed_out_library_ids: list[str] = Field(default_factory=list)
    cancelled_library_ids: list[str] = Field(default_factory=list)
    pending_library_ids: list[str] = Field(default_factory=list)
    running_library_ids: list[str] = Field(default_factory=list)
    draining_library_ids: list[str] = Field(default_factory=list)


class SearchSession(StrictModel):
    tenant_id: str
    query_id: str
    input_type: InputType
    search_state: SearchState
    requested_library_ids: list[str]
    planning_mode: Literal["off", "bounded", "agent_tool"]
    claim_count: int = Field(ge=0)
    claim_coverage: ClaimCoverage
    applied_through_sequence: int = Field(ge=0)
    finalize_cut: int | None = None
    event_set_hash: str | None = None
    latest_checkpoint_id: str | None = None
    final_snapshot_id: str | None = None
    created_at: str
    updated_at: str


class GroupEntry(StrictModel):
    result_id: str
    rank: int = Field(ge=1)


class ResultGroup(StrictModel):
    claim_id: str
    rankable_group: str
    entries: list[GroupEntry]


class SearchStateResponse(StrictModel):
    session: SearchSession
    results: list[Recommendation]
    candidate_diagnostics: list[CandidateDiagnostic] = Field(default_factory=list)
    candidate_rankings: list[CandidateRanking] = Field(default_factory=list)
    groups: list[ResultGroup]
    workers: list[WorkerStatus]
    coverage: Coverage
    as_of_sequence: int = Field(ge=0)
    latest_sequence: int = Field(ge=0)
    is_live: bool
    adapter_mode: str
    scope_source: str
    simulation_notice: str
    tenant_note: str
    libraries: list[LibraryInfo]


class SnapshotItem(StrictModel):
    result_id: str
    work_id: str
    claim_id: str
    tenant_id: str
    query_id: str
    claim_text: str
    claim_type: ClaimType
    claim_hash: str
    claim_scope: EvidenceScope
    insertion_anchor: dict | None
    result_version: int = Field(ge=1)
    rank: int = Field(ge=1)
    rankable_group: str
    display_score: float | None = None
    score_version: str | None = None
    relationship: str
    citable: bool
    provisional: bool = False
    source_record_ids: list[str]
    evidence_ids: list[str]
    evidence_set_hash: str
    citation_text_id: str | None = None
    citation_text_hash: str | None = None


class SnapshotManifest(StrictModel):
    snapshot_id: str
    tenant_id: str
    query_id: str
    snapshot_kind: Literal["checkpoint", "final", "supplemental"]
    items: list[SnapshotItem]
    item_count: int = Field(ge=0)
    coverage: Coverage
    claim_coverage: ClaimCoverage
    termination_reason: str
    is_partial: bool
    applied_through_sequence: int = Field(ge=0)
    event_set_hash: str
    ranking_policy_version: str
    score_scale_groups: dict
    rubric_version: str
    index_versions: dict
    manuscript_context_version: str | None = None
    evidence_bundle_id: str
    evidence_bundle_hash: str
    scoring_failure_count: int = Field(ge=0)
    uncalibrated_count: int = Field(ge=0)
    late_result_count: int = Field(ge=0)
    model_versions: dict
    calibration_versions: dict
    status_check: dict
    supersedes_snapshot_id: str | None = None
    hash_alg: Literal["SHA-256"]
    canonicalization: Literal["RFC8785"]
    immutable_hash: str
    created_at: str


class EvidenceBundle(StrictModel):
    bundle_id: str
    bundle_hash: str
    object_key: str
    item_count: int = Field(ge=0)
    items: list[Evidence]
    created_at: str


class AgentQuery(StrictModel):
    created: SearchCreated
    requested_library_ids: list[str]


class AgentToolCall(StrictModel):
    name: str
    arguments: dict
    query_id: str | None = None
    status: Literal["started", "completed", "failed"] = "started"


@dataclass(frozen=True)
class CitationBatchOutcome:
    ready: int = 0
    unavailable: int = 0
    interrupted: bool = False


class CitationPreparationSummary(StrictModel):
    status: Literal["waiting", "completed", "partial"]
    ready: int = Field(default=0, ge=0)
    unavailable: int = Field(default=0, ge=0)
    unfinished_batches: int = Field(default=0, ge=0)


class AgentRun(StrictModel):
    run_id: str
    tenant_id: str
    request: SearchCreate
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    queries: list[AgentQuery] = Field(default_factory=list)
    latest_query_id: str | None = None
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    summary: str | None = None
    error: dict | None = None
    citation_preparation: CitationPreparationSummary | None = None
    provider: str
    model: str
    served_model: str | None = None
    response_grounding: str | None = None
    design_reviews: list[dict] = Field(default_factory=list)
    conversation_id: str | None = None
    conversation_revision: int | None = Field(default=None, ge=0)
    turn_index: int | None = Field(default=None, ge=1)
    user_message: str | None = None
    active_query_id: str | None = None
    prior_query_ids: list[str] = Field(default_factory=list)
    context_manifest: dict = Field(default_factory=dict)
    manuscript_reads: list[dict] = Field(default_factory=list)
    evaluation_anchor: EvaluationAnchor | None = None
    created_at: str
    updated_at: str


class ConversationMessage(StrictModel):
    seq: int = Field(ge=1)
    role: Literal["user", "assistant"]
    text: str
    run_id: str
    created_at: str


class ConversationTurn(StrictModel):
    turn_index: int = Field(ge=1)
    run_id: str
    user_message_seq: int = Field(ge=1)
    assistant_message_seq: int | None = Field(default=None, ge=1)
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    query_ids: list[str] = Field(default_factory=list)
    active_query_id: str | None = None
    summary: str | None = None
    error: dict | None = None
    created_at: str
    finished_at: str | None = None


class ConversationArtifact(StrictModel):
    seq: int = Field(ge=1)
    query_id: str
    run_id: str
    turn_index: int = Field(ge=1)
    label: str
    created_at: str


class Conversation(StrictModel):
    conversation_id: str
    tenant_id: str
    title: str
    project_id: str | None = None
    revision: int = Field(ge=0)
    active_run_id: str | None = None
    latest_turn_index: int = Field(ge=0)
    last_active_query_id: str | None = None
    latest_anchor: EvaluationAnchor | None = None
    messages: list[ConversationMessage] = Field(default_factory=list)
    turns: list[ConversationTurn] = Field(default_factory=list)
    artifacts: list[ConversationArtifact] = Field(default_factory=list)
    created_at: str
    updated_at: str


class PurgeReceipt(StrictModel):
    receipt_id: str
    status: Literal["completed"]
    counts: dict[str, int] = Field(default_factory=dict)
    limitations: dict = Field(default_factory=dict)
    created_at: str
