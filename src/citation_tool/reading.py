"""Text windows and literal locations; offsets are zero-based Unicode code points."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    section_path: tuple[str, ...]
    kind: str = "paragraph"
    element_id: str | None = None


def literal_pattern(text: str, *, exact: bool) -> re.Pattern[str]:
    return re.compile(re.escape(text), 0 if exact else re.IGNORECASE)


def match_window(text: str, start: int, end: int, *, flank: int = 240) -> dict:
    left, right = max(0, start - flank), min(len(text), end + flank)
    return {
        "start": left,
        "end": right,
        "text": text[left:right],
        "match_start": start,
        "match_end": end,
    }
