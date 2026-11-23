"""DOI identity and explicitly sourced publication relationships."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlparse

DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
RELATION_KEYS = ("corrects", "corrected_by", "retracted_by")
RETRACTION_TYPES = {"retraction", "retracted publication", "retraction of publication"}


def normalize_doi(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    doi = unquote(value.strip())
    if doi.casefold().startswith(("https://doi.org/", "http://doi.org/")):
        doi = urlparse(doi).path.lstrip("/")
    doi = doi.removeprefix("doi:").strip().rstrip(".,;:!?，。；")
    return doi.casefold() if DOI_PATTERN.fullmatch(doi) else None


CITATION_DOI_PATTERN = re.compile(r"10\.\d{4,9}/[A-Za-z0-9._;()/:+%\-]+", re.IGNORECASE)
DOI_SOURCE_RULE_VERSION = "doi-source-spans-v1-delimiter-linebreak"
_DOI_SOURCE_TOKEN = re.compile(r"10\.\d{4,9}/[A-Za-z0-9._;()/:+%\-]*", re.IGNORECASE)
_DOI_SOURCE_LABEL = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi[ \t]{0,4}:[ \t]{0,4})$", re.IGNORECASE
)
_DOI_SOURCE_CONTINUATION = re.compile(
    r"[ \t]{0,16}\r?\n[ \t]{0,16}(?P<tail>[A-Za-z0-9][A-Za-z0-9._;()/:+%\-]*)"
    r"(?=[ \t]*(?:\r?\n|$))"
)


def _citation_doi_value(value: str) -> str | None:
    value = value.rstrip(".,;:!?")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return normalize_doi(value)


def citation_doi_spans(text: str) -> list[dict[str, str | int]]:
    spans: list[dict[str, str | int]] = []
    covered_end = 0
    for match in _DOI_SOURCE_TOKEN.finditer(text):
        if match.start() < covered_end:
            continue
        token = match.group()
        left = max(0, match.start() - 32)
        label = _DOI_SOURCE_LABEL.search(text[left : match.start()])
        continuation = (
            _DOI_SOURCE_CONTINUATION.match(text, match.end())
            if token.endswith("/") and label is not None
            else None
        )
        if continuation is not None:
            tail = continuation["tail"]
            if (
                any(character.isdigit() for character in tail)
                and (
                    any(character.isalpha() for character in tail)
                    or sum(character.isdigit() for character in tail) >= 5
                )
                and not _DOI_SOURCE_TOKEN.search(tail)
                and not tail.casefold().startswith(("http:", "https:", "doi:"))
                and (tail[0].islower() or tail[0].isdigit() or tail.upper() == tail)
                and (normalized := _citation_doi_value(token + tail))
            ):
                start, end = left + label.start(), continuation.end()
                spans.append(
                    {
                        "doi": normalized,
                        "raw": text[start:end],
                        "start": start,
                        "end": end,
                        "normalization": "path_delimiter_linebreak",
                    }
                )
                covered_end = end
                continue
        if CITATION_DOI_PATTERN.fullmatch(token) and (normalized := _citation_doi_value(token)):
            spans.append(
                {
                    "doi": normalized,
                    "raw": token,
                    "start": match.start(),
                    "end": match.end(),
                    "normalization": "literal",
                }
            )
    return spans


def citation_dois(text: str) -> set[str]:
    return {str(span["doi"]) for span in citation_doi_spans(text)}


def canonical_work_id(record: Mapping[str, Any]) -> str:
    doi = normalize_doi(record.get("doi"))
    if doi:
        return f"work:{doi}"
    work_id = record.get("work_id")
    if not isinstance(work_id, str) or not work_id.strip():
        raise ValueError("record requires a DOI or a stable work_id")
    return work_id.strip()


def related_dois(record: Mapping[str, Any]) -> dict[str, list[str]]:
    supplied = record.get("related_dois") or {}
    if not isinstance(supplied, Mapping):
        raise ValueError("related_dois must be an object")
    output = {}
    for key in RELATION_KEYS:
        values = supplied.get(key) or []
        if not isinstance(values, list):
            raise ValueError(f"related_dois.{key} must be an array")
        output[key] = sorted({doi for value in values if (doi := normalize_doi(value))})
    return output


def is_correction(record: Mapping[str, Any]) -> bool:
    types = {str(item).casefold() for item in record.get("pub_types", [])}
    return bool(types & {"published erratum", "correction"}) or str(
        record.get("title", "")
    ).casefold().startswith("correction:")


def publication_status(record: Mapping[str, Any]) -> str:
    types = {str(item).casefold() for item in record.get("pub_types", [])}
    relations = related_dois(record)
    if types & RETRACTION_TYPES or relations["retracted_by"]:
        return "retracted"
    status = record.get("retraction_status")
    if status not in {"none", "retracted", "corrected", "unknown"}:
        return "unknown"
    if status == "none" and not record.get("status_verified_at"):
        return "unknown"
    if relations["corrected_by"] and status == "none":
        return "corrected"
    return status
