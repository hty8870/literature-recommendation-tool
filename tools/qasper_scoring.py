from __future__ import annotations

import json
import re
import string
from collections import Counter

_ARTICLES = re.compile(r"\b(a|an|the)\b")


def spaced(text: str) -> str:
    return " ".join(text.split())


def _tokens(text: str) -> list[str]:
    return _ARTICLES.sub(
        " ", text.lower().translate(str.maketrans("", "", string.punctuation))
    ).split()


def f1(predicted, expected) -> float:
    common = sum((Counter(predicted) & Counter(expected)).values())
    return 2 * common / (len(predicted) + len(expected)) if predicted and expected else 0.0


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def parse_answer(text: str) -> dict | None:
    text = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", text, re.S | re.I)
    try:
        value = json.loads(fence[1] if fence else text, object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"status", "answer", "evidence"}
        or value["status"] not in ("answered", "insufficient")
        or not isinstance(value["answer"], str)
        or not isinstance(value["evidence"], list)
        or (value["status"] == "answered" and not value["answer"].strip())
        or (value["status"] == "insufficient" and (value["answer"].strip() or value["evidence"]))
    ):
        return None
    if any(
        not isinstance(row, dict)
        or set(row) != {"passage_id", "quote"}
        or not all(isinstance(v, str) for v in row.values())
        for row in value["evidence"]
    ):
        return None
    return value


def score(text: str, *, stratum: str, passages: dict[str, str], references: list[dict]) -> dict:
    value = parse_answer(text)
    result = {
        "format_valid": value is not None,
        "answerability_match": None,
        "reference_token_f1": None,
        "evidence_paragraph_f1": None,
        "complete_reference_paragraph_set": None,
        "same_reference_min_f1": None,
        "evidence_items": [],
        "scientific_pass": None,
    }
    if value is None:
        return result
    answered = value["status"] == "answered"
    if stratum != "mixed":
        result["answerability_match"] = answered == (stratum != "unanswerable")
    used, valid = set(), set()
    for item in value["evidence"]:
        pid, quote = item["passage_id"], item["quote"]
        reason = (
            "duplicate"
            if pid in used
            else "unknown_passage"
            if pid not in passages
            else "quote_too_short"
            if len(re.findall(r"\w+", quote)) < 3
            else "not_exact"
            if quote not in passages[pid]
            else None
        )
        used.add(pid)
        if reason is None:
            valid.add(pid)
        result["evidence_items"].append(
            {"passage_id": pid, "exact": reason is None, "error": reason}
        )
    if stratum in {"mixed", "unanswerable"}:
        return result
    per_reference = [
        (
            f1(_tokens(value["answer"]), _tokens(ref["answer"])),
            f1(valid, ref["paragraph_ids"]),
            set(ref["paragraph_ids"]) <= valid,
        )
        for ref in references
    ]
    if per_reference:
        result.update(
            reference_token_f1=max(a for a, _, _ in per_reference),
            evidence_paragraph_f1=max(e for _, e, _ in per_reference),
            complete_reference_paragraph_set=any(c for _, _, c in per_reference),
            same_reference_min_f1=max(min(a, e) for a, e, _ in per_reference),
        )
    return result
