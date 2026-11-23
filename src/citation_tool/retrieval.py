"""Local fixture adapters with query-aware sparse retrieval.

Passages and scientific annotations are hand-written simulations. The sparse
retrieval computation is real, but it is not a vector index, production search
engine, or scientific relationship verifier.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .config import LibraryConfig, Settings
from .entities import (
    ClaimContext,
    EvidenceScope,
    Passage,
    RecommendationSource,
    SourceLocator,
    utc_now_iso,
)
from .evidence import Candidate
from .publication import normalize_doi
from .relevance import score_records


@dataclass(frozen=True, slots=True)
class ReadingRecord:
    data: Mapping[str, Any]
    snapshot: str
    official_record: dict | None = None


@dataclass(frozen=True, slots=True)
class ReadingBatch:
    items: tuple[ReadingRecord, ...]
    next_position: int | str | None = None
    hit_count: int | None = None


@runtime_checkable
class ReadingAdapter(Protocol):
    def discover_text(
        self, text: str, *, literal: bool, position: int | str | None, limit: int
    ) -> ReadingBatch: ...

    def lookup_text(self, identifier: str) -> ReadingBatch: ...


def reading_identifier(value: str) -> str:
    raw = value.strip().casefold()
    if doi := normalize_doi(raw):
        return doi
    if match := re.fullmatch(r"pmid\s*:?\s*(\d{1,12})", raw):
        return f"pmid:{match[1]}"
    return raw


class SourceSearchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class LibraryAdapter(Protocol):
    library: LibraryConfig
    adapter_id: str
    simulation: bool
    display_name: str
    index_snapshot: str
    model_versions: dict[str, str]

    def search(self, claim: ClaimContext) -> list[Candidate]: ...


@dataclass(frozen=True, slots=True)
class FixtureRecord:
    work_id: str
    paper_id: str
    title: str
    year: int
    doi: str
    raw_text: str
    quote: str
    section_path: tuple[str, ...]
    page: int
    reading_order: int
    display_score: float


_FAST_FIXTURES = (
    FixtureRecord(
        work_id="work:10.5555/demo-fast-001",
        paper_id="paper:fixture-fast:001",
        title="Demo fixture: LFY2 expression in soybean floral meristems",
        year=2024,
        doi="10.5555/demo-fast-001",
        raw_text=(
            "Simulation fixture: LFY2 expression was observed in soybean floral meristems "
            "during early flowering."
        ),
        quote="LFY2 expression was observed in soybean floral meristems",
        section_path=("Results", "Expression profiling"),
        page=3,
        reading_order=11,
        display_score=88.5,
    ),
    FixtureRecord(
        work_id="work:10.5555/demo-fast-002",
        paper_id="paper:fixture-fast:002",
        title="Demo fixture: LFY2 knockdown and soybean flowering time",
        year=2023,
        doi="10.5555/demo-fast-002",
        raw_text=(
            "Simulation fixture: LFY2 knockdown delayed soybean floral development in a "
            "controlled greenhouse trial."
        ),
        quote="LFY2 knockdown delayed soybean floral development",
        section_path=("Results", "Phenotyping"),
        page=5,
        reading_order=23,
        display_score=74.0,
    ),
)

_SLOW_FIXTURES = (
    FixtureRecord(
        work_id="work:10.5555/demo-slow-001",
        paper_id="paper:fixture-slow:001",
        title="Demo fixture: LFY2 and AP1 co-expression in soybean",
        year=2025,
        doi="10.5555/demo-slow-001",
        raw_text=(
            "Simulation fixture: RNA-seq detected LFY2 and AP1 co-expression during "
            "soybean floral initiation."
        ),
        quote="LFY2 and AP1 co-expression during soybean floral initiation",
        section_path=("Results", "Transcriptomics"),
        page=7,
        reading_order=31,
        display_score=91.2,
    ),
    FixtureRecord(
        work_id="work:10.5555/demo-slow-002",
        paper_id="paper:fixture-slow:002",
        title="演示夹具：LFY2 单倍型与大豆开花期",
        year=2022,
        doi="10.5555/demo-slow-002",
        raw_text=("模拟夹具：LFY2 单倍型 B 与大豆开花期提前相关，该文本仅用于 UTF-8 偏移核验。"),
        quote="LFY2 单倍型 B 与大豆开花期提前相关",
        section_path=("结果", "关联分析"),
        page=9,
        reading_order=47,
        display_score=69.5,
    ),
)

_FIXTURE_SETS = {"fast": _FAST_FIXTURES, "slow": _SLOW_FIXTURES}
DEMO_SCOPE = EvidenceScope(
    species=["Glycine max"],
    system=["greenhouse", "field"],
    method=["expression profiling", "association analysis"],
)


class FixtureLibraryAdapter:
    def __init__(self, library: LibraryConfig) -> None:
        if library.fixture_set not in _FIXTURE_SETS:
            raise ValueError(f"unknown fixture_set: {library.fixture_set}")
        self.library = library
        self.adapter_id = library.adapter_id
        self.simulation = True
        self.display_name = library.display_name
        self.index_snapshot = f"fixture-index-{library.fixture_set}-v1"
        self.model_versions = {
            "retrieval": "local-fixture-sparse-bm25-ontology-v2",
            "lexical_engine": "python_bm25",
            "lexical_metric": "bm25_k1_1.5_b_0.75",
            "reranker": "none",
            "verifier": "deterministic-rules-v1",
        }
        self._records = _FIXTURE_SETS[library.fixture_set]

    def _passage(self, record: FixtureRecord, now: str) -> Passage:
        return Passage(
            passage_id=f"{record.paper_id}#p1",
            work_id=record.work_id,
            paper_id=record.paper_id,
            library_id=self.library.library_id,
            raw_text=record.raw_text,
            normalized_text=record.raw_text,
            section_path=list(record.section_path),
            paragraph_index=0,
            reading_order_index=record.reading_order,
            layout_quality="high",
            page_start=record.page,
            page_end=record.page,
            printed_page_start=str(record.page),
            printed_page_end=str(record.page),
            parser_version="fixture-parser-v1",
            normalization_version="fixture-normalization-v1",
            index_version=self.index_snapshot,
            created_at=now,
            updated_at=now,
        )

    def all_candidates(self, claim: ClaimContext) -> list[Candidate]:
        return [self._candidate(record, claim, record.display_score) for record in self._records]

    def search(self, claim: ClaimContext) -> list[Candidate]:
        matches = score_records(
            claim,
            [
                (record.work_id, record.title, record.raw_text, record.doi)
                for record in self._records
            ],
        )
        return [
            self._candidate(record, claim, matches[record.work_id].score)
            for record in sorted(
                (item for item in self._records if item.work_id in matches),
                key=lambda item: (-matches[item.work_id].score, item.work_id),
            )
        ]

    def _candidate(
        self, record: FixtureRecord, claim: ClaimContext, display_score: float
    ) -> Candidate:
        now = utc_now_iso()
        role = (
            "background"
            if claim.claim_type in {"topic", "background"}
            else "method"
            if claim.claim_type == "method"
            else "primary_result"
        )
        passage = self._passage(record, now)
        source = RecommendationSource(
            paper_id=record.paper_id,
            library_id=self.library.library_id,
            source_role="publisher_pdf",
            entitlement_status="allowed",
            metadata_verified=True,
            simulation=True,
            adapter_id=self.adapter_id,
            display_name=self.display_name,
            title=record.title,
            year=record.year,
            doi=record.doi,
        )
        locator = SourceLocator(
            pdf_page=record.page,
            printed_page=str(record.page),
            section_path=list(record.section_path),
            paragraph_index=0,
            reading_order_index=record.reading_order,
            reading_order_confidence=0.97,
            source_uri=f"fixture://{record.paper_id}",
        )
        return Candidate(
            passage=passage,
            quote=record.quote,
            display_score=display_score,
            evidence_role=role,
            locator=locator,
            source=source,
            scope=EvidenceScope(**DEMO_SCOPE.model_dump()),
            context=record.raw_text,
        )


def build_fixture_adapters(settings: Settings) -> dict[str, FixtureLibraryAdapter]:
    settings.validate()
    return {library.library_id: FixtureLibraryAdapter(library) for library in settings.libraries}
