from __future__ import annotations

import pytest

from citation_tool.entities import CandidateDiagnostic
from citation_tool.listwise import (
    LISTWISE_ALGORITHM,
    LISTWISE_PROMPT_VERSION,
    RankedItem,
    RankingDraft,
    RerankFailure,
    evidence_options,
    resolve_evidence,
)
from citation_tool.sentence_spans import (
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_OPTIONS,
    MAX_EVIDENCE_TOTAL_CHARS,
    SENTENCE_WINDOW_VERSION,
    sentence_spans,
    sentence_windows,
)


def diagnostic(text: str, title: str | None = "A genuine scientific source"):
    return CandidateDiagnostic(
        result_id="r",
        work_id="w",
        paper_id="p",
        claim_id="c",
        title=title,
        evidence_excerpt=text,
        library_ids=["a"],
        relationship="insufficient",
        scope_match="unknown",
        rejection_reasons=["abstract_metadata_only"],
        quality_note="test",
    )


@pytest.mark.parametrize(
    ("text", "sentences"),
    [
        (
            "Dr. A. Smith et al. used Fig. 2, i.e. the U.S. cohort (refs. 5,9). "
            "The rate was 0.05 (P = .01). However, controls differed.",
            [
                "Dr. A. Smith et al. used Fig. 2, i.e. the U.S. cohort (refs. 5,9). ",
                "The rate was 0.05 (P = .01). ",
                "However, controls differed.",
            ],
        ),
        (
            "We tested protein A. We then tested protein B. The response differed.",
            ["We tested protein A. ", "We then tested protein B. ", "The response differed."],
        ),
        (
            "结果在P<0.05时显著。对照组未出现变化！Does it generalize? 仍需验证。",
            ["结果在P<0.05时显著。", "对照组未出现变化！", "Does it generalize? ", "仍需验证。"],
        ),
        (
            "The authors wrote “Results were positive.”\n\n"
            "A second result retained Case\u00a0and spacing.",
            [
                "The authors wrote “Results were positive.”\n\n",
                "A second result retained Case\u00a0and spacing.",
            ],
        ),
        (
            "E. coli were used at 1.5 mg/mL. We repeated the assay using e.g. buffer A.",
            ["E. coli were used at 1.5 mg/mL. ", "We repeated the assay using e.g. buffer A."],
        ),
    ],
)
def test_sentence_boundaries_preserve_scientific_punctuation_and_original_spaces(text, sentences):
    spans = sentence_spans(text)
    assert [text[span.start : span.end] for span in spans] == sentences
    assert "".join(text[span.start : span.end] for span in spans) == text
    assert all(span.start < span.end for span in spans)


def test_sentence_options_and_neighbor_windows_trace_exact_offsets_without_normalization():
    text = "CONCLUSIONS We observed Original\u00a0Case.\tHowever, controls were unaffected.  "
    text += "结果仍需独立验证。"
    item = diagnostic(text)
    options = evidence_options(item)
    assert options
    for identifier, quote in options.items():
        start, end = map(int, identifier.split(":")[-2:])
        original = item.title if identifier.startswith("title:") else item.evidence_excerpt
        assert quote == original[start:end]
        assert 0 < len(quote) <= MAX_EVIDENCE_CHARS
    assert any("\u00a0" in quote and "\t" in quote for quote in options.values())
    assert any(f":{SENTENCE_WINDOW_VERSION}:2:" in key for key in options)
    assert "abstract:0" not in options


def test_options_are_bounded_and_do_not_fall_back_to_the_entire_abstract():
    sentences = [
        f"Sentence {index} reports a complete observed result in this study. "
        for index in range(80)
    ]
    text = "".join(sentences)
    options = evidence_options(diagnostic(text))
    assert len(options) <= MAX_EVIDENCE_OPTIONS
    assert sum(map(len, options.values())) <= MAX_EVIDENCE_TOTAL_CHARS
    assert all(quote != text for quote in options.values())
    assert all(int(key.split(":")[-3]) <= 2 for key in options if key.startswith("abstract:"))
    assert options == evidence_options(diagnostic(text))
    assert any(
        str(len(text)) == key.split(":")[-1] for key in options if key.startswith("abstract:")
    )


def test_long_unpunctuated_source_is_not_cut_into_partial_sentences():
    text = "unpunctuated " * 200
    assert len(text) > MAX_EVIDENCE_CHARS
    assert sentence_windows(text) == []
    options = evidence_options(diagnostic(text))
    assert all(identifier.startswith("title:") for identifier in options)


def test_short_chinese_sentences_are_valid_complete_source_windows():
    text = "未见毒性。仍需复核。"
    item = diagnostic(text, title=None)
    options = evidence_options(item)
    identifier = next(key for key in options if options[key] == "未见毒性。")
    draft = RankingDraft(
        items=[
            RankedItem(
                work_id="w",
                reason="原文陈述了结果，同时仍需要核查条件。",
                evidence_id=identifier,
                alignment_score=2,
            )
        ]
    )
    assert resolve_evidence(draft, {"w": item}).items[0].evidence_quote == "未见毒性。"


def test_complete_neighbor_window_over_500_characters_survives_resolution_and_cache_roundtrip():
    first = (
        "The study measured a controlled response in the specified cells "
        + "under condition A " * 16
        + ". "
    )
    second = (
        "However, the control comparison and experimental result remained necessary "
        + "for interpretation " * 12
        + "."
    )
    text = first + second + " Another sentence is separate."
    item = diagnostic(text)
    identifier = next(
        key
        for key, quote in evidence_options(item).items()
        if ":2:" in key and quote == first + second + " "
    )
    draft = RankingDraft(
        items=[
            RankedItem(
                work_id="w",
                reason="该研究提供直接实验及对照结果，可进一步核查范围。",
                evidence_id=identifier,
                alignment_score=2,
            )
        ]
    )
    resolved = resolve_evidence(draft, {"w": item})
    assert 500 < len(resolved.items[0].evidence_quote) <= MAX_EVIDENCE_CHARS
    assert RankingDraft.model_validate_json(resolved.model_dump_json()) == resolved
    assert resolved.items[0].work_id == draft.items[0].work_id
    assert resolved.items[0].reason == draft.items[0].reason


def test_old_unknown_and_mismatched_ids_cannot_bypass_complete_windows():
    item = diagnostic("The source reports a measured result. Another condition is relevant.")
    valid = next(key for key in evidence_options(item) if key.startswith("abstract:"))
    for identifier, quote in (
        ("abstract:0", None),
        (None, "measured result"),
        (valid, "measured result"),
    ):
        draft = RankingDraft(
            items=[
                RankedItem(
                    work_id="w",
                    reason="该篇提供实际研究信息，可进一步核查条件。",
                    evidence_id=identifier,
                    evidence_quote=quote,
                    alignment_score=2,
                )
            ]
        )
        with pytest.raises(RerankFailure, match="INVALID_EVIDENCE"):
            resolve_evidence(draft, {"w": item})
    assert "v12" in LISTWISE_PROMPT_VERSION
    assert SENTENCE_WINDOW_VERSION in LISTWISE_ALGORITHM


@pytest.mark.parametrize("legacy_id", [False, True])
async def test_cached_fragments_are_revalidated_without_reranking_or_template_repair(legacy_id):
    import asyncio

    from citation_tool.listwise import ListwiseReranker
    from test_discovery import models
    from test_units import make_candidate, make_claim

    item = diagnostic("The source reports a measured result. Another statement differs.")
    identifier = next(key for key in evidence_options(item) if key.startswith("abstract:"))
    cached = RankingDraft(
        items=[
            RankedItem(
                work_id="w",
                reason="该文提供实际研究信息，仍需进一步核查适用范围。",
                evidence_id="abstract:0" if legacy_id else identifier,
                evidence_quote="measured result",
                alignment_score=1,
            )
        ]
    )

    class Cache:
        def get_cached_ranking(self, *args):
            return {"draft": cached.model_dump(), "window_count": 1, "served_model": "test"}

    discovery = models()
    ranker = ListwiseReranker(discovery, Cache(), asyncio.Semaphore(1))
    with pytest.raises(RerankFailure, match="INVALID_EVIDENCE"):
        await ranker.rank(make_claim(), [item], {"w": make_candidate()})
    assert discovery.ranking_model.inputs == []


@pytest.mark.parametrize("bounds", [{"max_chars": 0}, {"max_options": 0}, {"max_total_chars": 0}])
def test_invalid_window_bounds_fail_explicitly(bounds):
    with pytest.raises(ValueError):
        sentence_windows("A sentence.", **bounds)
