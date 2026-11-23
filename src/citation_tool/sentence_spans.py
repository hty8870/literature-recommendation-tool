"""Conservative source sentence windows with Unicode code-point offsets."""

from __future__ import annotations

import re
from dataclasses import dataclass

SENTENCE_WINDOW_VERSION = "sent-v1"
MAX_EVIDENCE_CHARS = 1200
MAX_EVIDENCE_OPTIONS = 48
MAX_EVIDENCE_TOTAL_CHARS = 9000
_ABBREVIATIONS = frozenset(
    "e.g i.e et al fig figs eq eqs ref refs dr prof mr mrs ms vs cf ca approx "
    "no nos vol vols pp p sec secs min mins hr hrs a.u s.d s.e s.e.m ph.d b.sc m.sc".split()
)
_CLOSERS = frozenset("\"'”’»)]}）】")
_ENDINGS = frozenset(".!?。！？")
_OPENERS = frozenset("([{（【")
_SENTENCE_STARTERS = frozenset(
    "we the this these our however therefore in by a an they it results conclusions methods".split()
)
_BRACKET_CLOSERS = frozenset(")] }）】".replace(" ", ""))


@dataclass(frozen=True, slots=True)
class SourceSentenceSpan:
    start: int
    end: int
    sentences: int = 1


def _cjk(char: str) -> bool:
    return "\u3400" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff"


def _period_is_internal(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if after.isdigit() and (before.isdigit() or not before or before in " <>=+-"):
        return True
    token = re.search(r"[A-Za-z][A-Za-z.]*$", text[max(0, index - 40) : index])
    if token is not None:
        value = token.group().casefold()
        initial = len(value) == 1 or re.fullmatch(r"(?:[a-z]\.)+[a-z]", value)
        if initial or value == "al":
            following = re.match(r"\s*([A-Za-z]+)", text[index + 1 : index + 50])
            if following is None or following.group(1).casefold() not in _SENTENCE_STARTERS:
                return True
        elif value in _ABBREVIATIONS:
            return True
    return bool(
        after and not after.isspace() and after not in _CLOSERS | _ENDINGS and not _cjk(after)
    )


def sentence_spans(text: str) -> list[SourceSentenceSpan]:
    spans = []
    start = index = depth = 0
    while index < len(text):
        char = text[index]
        if char in _OPENERS:
            depth += 1
        elif char in _BRACKET_CLOSERS:
            depth = max(0, depth - 1)
        paragraph = char == "\n" and re.match(r"\n[ \t\r]*\n", text[index:]) is not None
        terminal = char in _ENDINGS and not (char == "." and _period_is_internal(text, index))
        if char in "!?" and index + 1 < len(text):
            following = text[index + 1]
            if (
                not following.isspace()
                and following not in _CLOSERS | _ENDINGS
                and not _cjk(following)
            ):
                terminal = False
        if not paragraph and not terminal:
            index += 1
            continue
        end = index + 1
        closing_depth = depth
        while end < len(text) and text[end] in _ENDINGS | _CLOSERS:
            if text[end] in _BRACKET_CLOSERS:
                closing_depth = max(0, closing_depth - 1)
            end += 1
        if terminal and closing_depth:
            index += 1
            continue
        while end < len(text) and text[end].isspace():
            end += 1
        if char == "." and end < len(text) and text[end].isascii() and text[end].islower():
            index += 1
            continue
        if any(value.isalnum() for value in text[start:end]):
            spans.append(SourceSentenceSpan(start, end))
        start = index = end
        depth = 0
    if start < len(text) and any(char.isalnum() for char in text[start:]):
        spans.append(SourceSentenceSpan(start, len(text)))
    return spans


def _evenly_bounded(
    spans: list[SourceSentenceSpan], count: int, char_budget: int
) -> list[SourceSentenceSpan]:
    for size in range(min(count, len(spans)), 0, -1):
        indices = (
            [0] if size == 1 else [round(i * (len(spans) - 1) / (size - 1)) for i in range(size)]
        )
        selected = [spans[index] for index in indices]
        if sum(span.end - span.start for span in selected) <= char_budget:
            return selected
    return []


def sentence_windows(
    text: str,
    *,
    max_chars: int = MAX_EVIDENCE_CHARS,
    max_options: int = MAX_EVIDENCE_OPTIONS,
    max_total_chars: int = MAX_EVIDENCE_TOTAL_CHARS,
) -> list[SourceSentenceSpan]:
    if min(max_chars, max_options, max_total_chars) < 1:
        raise ValueError("sentence window bounds must be positive")
    spans = sentence_spans(text)
    singles = [span for span in spans if span.end - span.start <= max_chars]
    selected = _evenly_bounded(singles, max(1, max_options // 2), max_total_chars * 2 // 3)
    pairs = [
        SourceSentenceSpan(first.start, second.end, 2)
        for first, second in zip(spans, spans[1:], strict=False)
        if second.end - first.start <= max_chars
    ]
    selected += _evenly_bounded(
        pairs,
        max_options - len(selected),
        max_total_chars - sum(span.end - span.start for span in selected),
    )
    return selected
