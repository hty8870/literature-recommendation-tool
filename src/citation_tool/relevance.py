"""Query-aware lexical retrieval for small fixture indexes.

This is an honest sparse baseline, not a vector model or scientific relationship
verifier. Exact identifiers are additive anchors; curated ontology aliases cover
known terminology without turning excluded terms into required query terms.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .entities import ClaimContext

TOPIC_ALIASES = {
    "flowering": (
        "flowering",
        "floral",
        "flower development",
        "floral transition",
        "flowering time",
        "开花",
        "花发育",
        "花器官",
        "花起始",
    ),
    "soybean": ("soybean", "glycine max", "大豆"),
    "rice": ("rice", "oryza sativa", "水稻"),
    "arabidopsis": ("arabidopsis", "arabidopsis thaliana", "拟南芥"),
    "nitrogen": ("nitrogen", "nitrate", "氮"),
    "photosynthesis": ("photosynthesis", "photosynthetic", "光合"),
    "drought": ("drought", "water deficit", "osmotic stress", "干旱", "渗透胁迫"),
}
STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "can",
    "candidate",
    "citation",
    "could",
    "does",
    "during",
    "evidence",
    "find",
    "for",
    "from",
    "have",
    "into",
    "list",
    "local",
    "more",
    "not",
    "paper",
    "papers",
    "please",
    "related",
    "research",
    "results",
    "search",
    "should",
    "show",
    "some",
    "study",
    "support",
    "than",
    "that",
    "the",
    "their",
    "them",
    "these",
    "this",
    "those",
    "through",
    "under",
    "using",
    "was",
    "were",
    "what",
    "when",
    "which",
    "with",
    "without",
    "would",
}
# Explicit retrieval instructions: corpus exclusions ("excluding reviews", "不要大豆",
# "without measuring LFY2") and epistemic prohibitions ("do not infer clinical use").
# Scientific proposition negation ("X does not affect Y", "Suboptimal nutrition is
# not predictive ...") is deliberately absent: it is part of the research question,
# so its objects stay positive anchors and must not truncate the query.
EXCLUSION_INSTRUCTION = re.compile(
    r"(?<![a-z])exclud(?:e|es|ed|ing)(?![a-z])"
    r"|(?<![a-z])except(?: for)?(?![a-z])"
    r"|(?<![a-z])instead of(?![a-z])"
    r"|(?<![a-z])rather than(?![a-z])"
    r"|(?<![a-z])omit(?:s|ted|ting)?(?![a-z])"
    r"|(?<![a-z])without\s+(?:measuring|reporting|including|studying|covering|assessing"
    r"|evaluating|involving|using|requiring)(?![a-z])"
    r"|(?<![a-z])(?:do not|don't|never)\s+(?:include|including|infer|assume|conclude"
    r"|claim|treat|overstate|present|stud(?:y|ies)|papers?|literature|research)(?![a-z])"
    r"|不要把|不要|不包括|排除|而非|而不是|不涉及|未测定|没有测量"
    r"|不得(?:据此|推断|宣称|视为)|不能(?:据此|推断|宣称)",
    re.IGNORECASE,
)
# Bare "without X" is an exclusion only in keyword queries; in a claim it is usually
# a scientific statement ("mice without IFN-gamma"), which keeps its objects.
BARE_WITHOUT = re.compile(r"(?<![a-z])without(?![a-z])", re.IGNORECASE)
CLAUSE_END = re.compile(r"[，,；;。.!?！？]")
GENE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]*\d[A-Za-z0-9-]*(?![A-Za-z0-9])")
DOI = re.compile(r"10\.\d{4,9}/[^\s，。；]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class QueryRelevance:
    score: float
    matched_identifiers: tuple[str, ...]
    matched_topics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """Frozen interpretation of one claim, shareable with evaluation probes."""

    text: str
    terms: tuple[str, ...]
    identifiers: tuple[str, ...]
    topics: tuple[str, ...]
    excluded_topics: tuple[str, ...]
    excluded_identifiers: tuple[str, ...]
    doi: str | None


def _instruction_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in EXCLUSION_INSTRUCTION.finditer(text):
        end = CLAUSE_END.search(text, match.end())
        spans.append((match.start(), end.start() if end else len(text)))
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _strip_instructions(text: str) -> str:
    spans = _instruction_spans(text)
    if not spans:
        return " ".join(text.split())
    pieces, cursor = [], 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    cleaned = " ".join(" ".join(pieces).split())
    return re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", cleaned).lstrip(" ,;，；.。")


def _instructed(text: str, start: int, *, keywords: bool) -> bool:
    """True when an explicit retrieval instruction, not scientific negation, governs a term."""
    window = text[max(0, start - 64) : start]
    cue = None
    for match in EXCLUSION_INSTRUCTION.finditer(window):
        cue = match
    if keywords:
        for match in BARE_WITHOUT.finditer(window):
            cue = match
    if cue is None:
        return False
    between = window[cue.end() :].strip()
    return len(between) <= 32 and CLAUSE_END.search(between) is None


def record_allowed(
    claim: ClaimContext, title: str, text: str, doi: str, *, plan: QueryPlan | None = None
) -> bool:
    """Apply hard identity/exclusion constraints without requiring lexical topic overlap."""
    plan = plan or query_plan(claim)
    if plan.doi and (doi or "").casefold() != plan.doi.casefold():
        return False
    excluded_genes, excluded_matches = _matches(
        f"{title} {text}", list(plan.excluded_identifiers), list(plan.excluded_topics)
    )
    return not (excluded_genes or excluded_matches)


def semantic_query_text(claim: ClaimContext) -> str:
    """Keep positive research concepts; retrieval instructions must not become dense anchors."""
    if claim.input_type == "keywords" and claim.query_terms:
        terms = []
        for term in claim.query_terms:
            if BARE_WITHOUT.fullmatch(term.strip()):
                continue
            stripped = _strip_instructions(term)
            if stripped:
                terms.append(stripped)
        excluded = _query_concepts(claim)[2:]
        terms = [
            term
            for term in terms
            if not any(
                alias in term.casefold()
                for concept in excluded[0]
                for alias in TOPIC_ALIASES[concept]
            )
            and not any(gene in term.casefold() for gene in excluded[1])
        ]
        return " ".join(terms)
    return _strip_instructions(claim.claim_text).strip()


def semantic_query_facets(claim: ClaimContext) -> list[str]:
    full_query = semantic_query_text(claim)
    facets = [full_query] if full_query else []
    for topic in claim.topic_context:
        for fragment in re.split(r"(?<=[.!?。！？])\s+|(?<=[。！？])", topic):
            fragment = fragment.strip()
            if len(fragment) < 24:
                continue
            facet_claim = claim.model_copy(
                update={"claim_text": fragment, "topic_context": [], "query_terms": []}
            )
            facet = semantic_query_text(facet_claim)
            if len(facet) >= 24 and facet not in facets:
                facets.append(facet)
    return facets[:5]


def _terms(text: str) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)
        if len(token) >= 3 and token.casefold() not in STOPWORDS
    ]


def _query_terms(text: str, *, keywords: bool = False) -> list[str]:
    folded = text.casefold()
    return [
        match.group().casefold()
        for match in re.finditer(r"[A-Za-z][A-Za-z0-9-]*", text)
        if len(match.group()) >= 3
        and match.group().casefold() not in STOPWORDS
        and not _instructed(folded, match.start(), keywords=keywords)
    ]


def _plan_units(claim: ClaimContext) -> list[str]:
    """Independent texts to interpret: the claim and the keyword list (not duplicated)."""
    terms = " ".join(claim.query_terms)
    if not terms or terms == claim.claim_text:
        return [claim.claim_text]
    return [claim.claim_text, terms]


def _plan_text(claim: ClaimContext) -> str:
    return " ".join(_plan_units(claim))


def _query_concepts(claim: ClaimContext) -> tuple[list[str], list[str], list[str], list[str]]:
    keywords = claim.input_type == "keywords"
    identifiers: set[str] = set()
    excluded_identifiers: set[str] = set()
    positive_topics: set[str] = set()
    negative_topics: set[str] = set()
    for unit in _plan_units(claim):
        text = unit.casefold()
        doi_spans = [match.span() for match in DOI.finditer(text)]
        for match in GENE.finditer(text):
            inside_doi = any(
                start <= match.start() and match.end() <= end for start, end in doi_spans
            )
            if inside_doi:
                continue
            if _instructed(text, match.start(), keywords=keywords):
                excluded_identifiers.add(match.group())
            else:
                identifiers.add(match.group())
        for concept, aliases in TOPIC_ALIASES.items():
            occurrences = [
                (match.group(), match.start())
                for alias in aliases
                for match in re.finditer(re.escape(alias), text)
            ]
            if not occurrences:
                continue
            if all(_instructed(text, start, keywords=keywords) for _, start in occurrences):
                negative_topics.add(concept)
            elif any(not _instructed(text, start, keywords=keywords) for _, start in occurrences):
                positive_topics.add(concept)
    return (
        sorted(identifiers - excluded_identifiers),
        [concept for concept in TOPIC_ALIASES if concept in positive_topics - negative_topics],
        [concept for concept in TOPIC_ALIASES if concept in negative_topics - positive_topics],
        sorted(excluded_identifiers),
    )


def query_plan(claim: ClaimContext) -> QueryPlan:
    text = _plan_text(claim)
    identifiers, topics, excluded_topics, excluded_identifiers = _query_concepts(claim)
    keywords = claim.input_type == "keywords"
    terms: set[str] = set()
    for unit in _plan_units(claim):
        terms.update(_query_terms(unit, keywords=keywords))
    terms.update(identifiers)
    terms.update(topics)
    doi = DOI.search(text)
    return QueryPlan(
        text=text,
        terms=tuple(sorted(terms)),
        identifiers=tuple(identifiers),
        topics=tuple(topics),
        excluded_topics=tuple(excluded_topics),
        excluded_identifiers=tuple(excluded_identifiers),
        doi=doi.group().rstrip(".,;:!?") if doi else None,
    )


LEXICAL_JOINED_FALLBACK_VERSION = "unmatched-hyphen-joined-v1"
_HYPHENATED_WORD = re.compile(r"[a-z]{3,}(?:-[a-z]{3,}){1,2}")


def lexical_query_fallback(plan: QueryPlan, vocabulary: Mapping[str, int]) -> QueryPlan:
    aliases = {
        term: joined
        for term in plan.terms
        if _HYPHENATED_WORD.fullmatch(term)
        and not vocabulary.get(term)
        and vocabulary.get(joined := term.replace("-", ""))
    }
    if not aliases:
        return plan
    return replace(plan, terms=tuple(sorted({aliases.get(term, term) for term in plan.terms})))


def _identifier_pattern(term: str) -> str:
    split = re.fullmatch(r"([a-z]+)(\d.*)", term)
    if split is None:
        return re.escape(term)
    return rf"{re.escape(split[1])}[\s-]*{re.escape(split[2])}"


def _matches(text: str, identifiers: list[str], topics: list[str]) -> tuple[list[str], list[str]]:
    folded = text.casefold()
    genes = [
        term
        for term in identifiers
        if re.search(rf"(?<![a-z0-9]){_identifier_pattern(term)}(?![a-z0-9])", folded)
    ]
    topics = [
        concept for concept in topics if any(alias in folded for alias in TOPIC_ALIASES[concept])
    ]
    return genes, topics


class BM25Index:
    def __init__(self, records: list[tuple[str, str, str, str]]) -> None:
        self._records = records
        self.documents = {
            work_id: Counter(_terms(f"{title} {text} {doi}"))
            for work_id, title, text, doi in records
        }
        self.lengths = {work_id: sum(tokens.values()) for work_id, tokens in self.documents.items()}
        self.average_length = sum(self.lengths.values()) / len(self.documents) or 1
        self.frequencies = Counter()
        for tokens in self.documents.values():
            self.frequencies.update(tokens.keys())

    def score(self, claim: ClaimContext) -> dict[str, QueryRelevance]:
        return score_records(claim, self._records, index=self)


def score_records(
    claim: ClaimContext,
    records: list[tuple[str, str, str, str]],
    *,
    index: BM25Index | None = None,
    plan: QueryPlan | None = None,
) -> dict[str, QueryRelevance]:
    plan = plan or query_plan(claim)
    index = index or BM25Index(records)
    documents = index.documents
    lengths = index.lengths
    average_length = index.average_length
    frequencies = index.frequencies
    if not documents:
        return {}
    raw_scores = {}
    for work_id, tokens in documents.items():
        score = 0.0
        for term in plan.terms:
            frequency = tokens[term]
            if not frequency:
                continue
            idf = math.log(
                1 + (len(documents) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
            )
            score += (
                idf
                * frequency
                * 2.5
                / (frequency + 1.5 * (0.25 + 0.75 * lengths[work_id] / average_length))
            )
        raw_scores[work_id] = score
    max_score = max(raw_scores.values(), default=0)
    output = {}
    for work_id, title, text, doi in records:
        if plan.doi and (doi or "").casefold() != plan.doi.casefold():
            continue
        matched_identifiers, matched_topics = _matches(
            f"{title} {text}", list(plan.identifiers), list(plan.topics)
        )
        excluded_genes, excluded_matches = _matches(
            f"{title} {text}", list(plan.excluded_identifiers), list(plan.excluded_topics)
        )
        if excluded_genes or excluded_matches:
            continue
        coverage_denominator = len(plan.identifiers) + len(plan.topics)
        coverage = (
            (len(matched_identifiers) + len(matched_topics)) / coverage_denominator
            if coverage_denominator
            else 0.0
        )
        if raw_scores[work_id] <= 0 and not matched_identifiers and not matched_topics:
            continue
        lexical = raw_scores[work_id] / max_score if max_score else 0.0
        score = 100 * (0.75 * coverage + 0.25 * lexical)
        output[work_id] = QueryRelevance(
            score=round(min(100, score), 1),
            matched_identifiers=tuple(matched_identifiers),
            matched_topics=tuple(matched_topics),
        )
    return output
