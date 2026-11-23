"""Real title/abstract candidate discovery without claiming full-text evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .config import LibraryConfig
from .entities import (
    ClaimContext,
    EvidenceScope,
    Passage,
    RecommendationSource,
    SourceLocator,
    utc_now_iso,
)
from .evidence import Candidate
from .jcs import sha256_json
from .publication import (
    canonical_work_id,
    is_correction,
    normalize_doi,
    publication_status,
    related_dois,
)
from .reading import literal_pattern
from .relevance import BM25Index
from .retrieval import ReadingBatch, ReadingRecord, reading_identifier

READING_LOOKUP_LIMIT = 12


def _excludes_corrections(claim: ClaimContext) -> bool:
    text = f"{claim.claim_text} {' '.join(claim.query_terms)}".casefold()
    if re.search(r"排除更正|不要.{0,12}更正|exclude corrections?|no corrections?", text):
        return True
    return bool(re.search(r"原始研究|original research|primary research", text)) and not any(
        term in text for term in ("correction", "更正")
    )


class MetadataCorpusAdapter:
    def __init__(
        self,
        library: LibraryConfig,
        records: list[Mapping[str, Any]],
        *,
        top_k: int = 5,
    ) -> None:
        if not 1 <= top_k <= 20:
            raise ValueError("metadata top_k must be between 1 and 20")
        self.library = library
        self.adapter_id = (
            library.adapter_id
            if library.adapter_id != "fixture-local-v1"
            else "europepmc-abstract-sparse-v1"
        )
        self.simulation = False
        self.display_name = library.display_name
        self.record_count = len(records)
        self.index_snapshot = f"europepmc-{sha256_json(records)[:16]}"
        self.model_versions = {
            "retrieval": "python-bm25-ontology-v2",
            "lexical_engine": "python_bm25",
            "lexical_metric": "bm25_k1_1.5_b_0.75",
            "reranker": "none",
            "evidence_level": "abstract_metadata_only",
        }
        self._records = [
            dict(record)
            | {
                "work_id": canonical_work_id(record),
                "doi": normalize_doi(record.get("doi")) or record.get("doi"),
            }
            for record in records
        ]
        self._record_tuples = [
            (record["work_id"], record["title"], record["abstract"], record["doi"])
            for record in self._records
        ]
        self._lexical_index = BM25Index(self._record_tuples)
        self._top_k = top_k
        self.score_version = "metadata-bm25-ontology-v2"
        corpus_key = sha256_json(
            [
                (record["work_id"], record["title"], record["abstract"], record["doi"])
                for record in self._records
            ]
        )[:16]
        self.score_scale_group = f"metadata-bm25:{corpus_key}"

    def search(self, claim: ClaimContext) -> list[Candidate]:
        matches = self._lexical_index.score(claim)
        exclude_corrections = _excludes_corrections(claim)
        best_by_work = {}
        for record in self._records:
            if record["work_id"] not in matches or (exclude_corrections and is_correction(record)):
                continue
            previous = best_by_work.get(record["work_id"])
            if previous is None or record["paper_id"] < previous["paper_id"]:
                best_by_work[record["work_id"]] = record
        selected = sorted(
            best_by_work.values(),
            key=lambda record: (-matches[record["work_id"]].score, record["work_id"]),
        )[: self._top_k]
        return [
            self._candidate(record, claim, matches[record["work_id"]].score) for record in selected
        ]

    def discover_text(
        self, text: str, *, literal: bool, position: int | str | None, limit: int
    ) -> ReadingBatch:
        offset = int(position or 0)
        pattern = literal_pattern(text, exact=False)
        matches = {}
        if not literal:
            query_hash = sha256_json(text)
            matches = self._lexical_index.score(
                ClaimContext(
                    tenant_id="read-only",
                    claim_id="lookup",
                    query_id="lookup",
                    anchor_hash=query_hash,
                    claim_rule_version="literal-or-bm25-v1",
                    input_type="abstract",
                    claim_type="topic",
                    claim_text=text,
                    claim_index=0,
                    claim_hash=query_hash,
                    created_at=utc_now_iso(),
                )
            )
        rows = []
        for record in self._records:
            phrase = bool(pattern.search(record["title"]) or pattern.search(record["abstract"]))
            match = matches.get(record["work_id"])
            if phrase or match:
                rows.append(
                    (not phrase, -(match.score if match else 0), record["paper_id"], record)
                )
        rows.sort(key=lambda item: item[:3])
        return ReadingBatch(
            tuple(
                ReadingRecord(row[3], self.index_snapshot) for row in rows[offset : offset + limit]
            ),
            next_position=offset + limit if offset + limit < len(rows) else None,
            hit_count=len(rows),
        )

    def lookup_text(self, identifier: str) -> ReadingBatch:
        key = reading_identifier(identifier)
        rows = []
        for record in self._records:
            identifiers = {
                reading_identifier(str(record[field]))
                for field in ("work_id", "paper_id", "doi", "pmcid")
                if record.get(field)
            }
            if pmid := record.get("pmid"):
                identifiers.add(f"pmid:{pmid}")
            if key in identifiers:
                rows.append(record)
        rows.sort(key=lambda record: record["paper_id"])
        return ReadingBatch(
            tuple(ReadingRecord(row, self.index_snapshot) for row in rows[:READING_LOOKUP_LIMIT]),
            hit_count=len(rows),
        )

    def _candidate(self, record: Mapping[str, Any], claim: ClaimContext, score: float) -> Candidate:
        now = utc_now_iso()
        types = {str(item).casefold() for item in record.get("pub_types", [])}
        correction = is_correction(record)
        status = publication_status(record)
        review = "review" in types or "review-article" in types
        role = (
            "correction_notice"
            if correction
            else "review_secondary"
            if review
            else "primary_result"
        )
        passage = Passage(
            passage_id=f"{record['paper_id']}#abstract",
            work_id=record["work_id"],
            paper_id=record["paper_id"],
            library_id=self.library.library_id,
            raw_text=record["abstract"],
            normalized_text=record["abstract"],
            section_path=[record.get("section_label") or "Abstract (publisher metadata)"],
            paragraph_index=0,
            reading_order_index=0,
            layout_quality="high",
            parser_version=record.get("metadata_parser") or "europepmc-core-v1",
            normalization_version="html-unescape-strip-v1",
            index_version=self.index_snapshot,
            created_at=now,
            updated_at=now,
        )
        source = RecommendationSource(
            paper_id=record["paper_id"],
            library_id=self.library.library_id,
            source_role="metadata_api",
            entitlement_status="allowed",
            metadata_verified=True,
            simulation=False,
            adapter_id=self.adapter_id,
            display_name=self.display_name,
            title=record["title"],
            authors=record.get("authors"),
            year=record.get("year"),
            doi=record["doi"],
            publication_status=status,
            related_dois=related_dois(record),
        )
        return Candidate(
            passage=passage,
            quote=record["abstract"],
            display_score=score,
            evidence_role=role,
            locator=SourceLocator(source_uri=record["source_metadata_url"]),
            source=source,
            scope=EvidenceScope(),
            context=record["abstract"],
            document_type_eligible=not correction and status != "retracted",
            retraction_status=status,
            score_version=self.score_version,
            score_scale_group=self.score_scale_group,
            publication_types=sorted(types),
        )
