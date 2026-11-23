"""Deterministic CSL/citeproc citation formatting and RIS/BibTeX serialization."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .entities import StrictModel
from .publication import normalize_doi

CSL_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "csl"
CITATION_STYLE_KEYS = ("apa", "vancouver")
CitationStyle = Literal["apa", "vancouver"]
ReferenceFormat = Literal["ris", "bibtex", "csl-json"]
COPY_FORMATS = {
    "apa": "APA 7",
    "vancouver": "Vancouver（NLM）",
    "bibtex": "BibTeX",
    "ris": "RIS",
}
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PAGE_PATTERN = re.compile(r"^[A-Za-z]?\d+[A-Za-z]?(?:[-–—][A-Za-z]?\d+[A-Za-z]?)?$")
PMID_PATTERN = re.compile(r"^\d{1,12}$")
PMCID_PATTERN = re.compile(r"^PMC\d+$")
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")


class CitationFormatError(Exception):
    def __init__(
        self,
        code: str,
        status: int,
        message: str,
        *,
        details: dict | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.details = details or {}
        self.retryable = retryable


class CitationAuthor(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    family: str | None = Field(default=None, max_length=200)
    given: str | None = Field(default=None, max_length=200)
    literal: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def requires_a_name(self) -> CitationAuthor:
        if self.literal and (self.family or self.given):
            raise ValueError("author literal cannot be combined with family or given")
        if not (self.family or self.literal):
            raise ValueError("author requires family or literal")
        return self


class CitationMetadata(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    item_type: Literal["article-journal", "article"] = "article-journal"
    title: str = Field(min_length=1, max_length=2000)
    authors: list[CitationAuthor] = Field(min_length=1, max_length=100)
    year: int = Field(ge=1000, le=3000)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    container_title: str = Field(min_length=1, max_length=500)
    volume: str | None = Field(default=None, max_length=64)
    issue: str | None = Field(default=None, max_length=64)
    page: str | None = Field(default=None, max_length=64)
    article_number: str | None = Field(default=None, max_length=64)
    doi: str | None = Field(default=None, max_length=256)
    pmid: str | None = Field(default=None, max_length=32)
    pmcid: str | None = Field(default=None, max_length=32)
    publisher: str | None = Field(default=None, max_length=300)
    url: str | None = Field(default=None, max_length=2000)
    language: str | None = Field(default=None, max_length=32)
    license: str | None = Field(default=None, max_length=4000)
    license_url: str | None = Field(default=None, max_length=2000)


class FormattedCitation(StrictModel):
    style: CitationStyle
    style_id: str
    style_title: str
    style_sha256: str
    renderer: str
    postprocess: str | None = None
    bibliography: str
    in_text: str | None
    numbering_required: bool
    csl_item: dict


@dataclass(frozen=True, slots=True)
class _StyleInfo:
    key: CitationStyle
    path: Path
    style_id: str
    title: str
    citation_format: str
    sha256: str


def _style_path(style: str) -> Path:
    return CSL_ASSET_DIR / ("apa.csl" if style == "apa" else "nlm-citation-sequence.csl")


@lru_cache(maxsize=2)
def _style_info(style: str) -> _StyleInfo:
    if style not in CITATION_STYLE_KEYS:
        raise CitationFormatError(
            "CITATION_STYLE_UNSUPPORTED",
            422,
            "不支持的引用样式。",
            details={"citation_style": style, "supported": list(CITATION_STYLE_KEYS)},
        )
    path = _style_path(style)
    if not path.is_file():
        raise CitationFormatError(
            "CITATION_STYLE_MISSING",
            503,
            "服务端缺少官方 CSL 样式文件。",
            details={"style": style},
        )
    data = path.read_bytes()
    try:
        root = ET.fromstring(data)  # noqa: S314 - bundled, hash-noticed CSL asset
    except ET.ParseError as exc:
        raise CitationFormatError(
            "CITATION_STYLE_INVALID",
            503,
            "CSL 样式文件无法解析。",
            details={"style": style},
        ) from exc
    namespace = {"csl": "http://purl.org/net/xbiblio/csl"}
    style_id = _element_text(root.find("csl:info/csl:id", namespace))
    title = _element_text(root.find("csl:info/csl:title", namespace))
    category = root.find("csl:info/csl:category", namespace)
    citation_format = category.get("citation-format", "") if category is not None else ""
    if not style_id or not citation_format:
        raise CitationFormatError(
            "CITATION_STYLE_INVALID",
            503,
            "CSL 样式缺少官方标识或引用格式。",
            details={"style": style},
        )
    return _StyleInfo(
        key=style,  # type: ignore[arg-type]
        path=path,
        style_id=style_id,
        title=title,
        citation_format=citation_format,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _element_text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def _load_citeproc():
    try:
        from citeproc import (
            Citation,
            CitationItem,
            CitationStylesBibliography,
            CitationStylesStyle,
            formatter,
        )
        from citeproc.source.json import CiteProcJSON
    except ImportError as exc:
        raise CitationFormatError(
            "CITATION_FORMAT_DEPENDENCY_MISSING",
            503,
            "服务端缺少 citeproc-py，无法生成 CSL 规范引文；未用模板或模型拼接替代。",
            details={"dependency": "citeproc-py"},
            retryable=False,
        ) from exc
    return {
        "Citation": Citation,
        "CitationItem": CitationItem,
        "CitationStylesBibliography": CitationStylesBibliography,
        "CitationStylesStyle": CitationStylesStyle,
        "formatter": formatter,
        "CiteProcJSON": CiteProcJSON,
        "version": getattr(__import__("citeproc"), "__version__", "unknown"),
    }


def _clean_text(value: str | None, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if CONTROL_CHARS.search(text):
        raise CitationFormatError(
            "CITATION_METADATA_INVALID",
            422,
            "题录字段包含控制字符。",
            details={"field": field},
        )
    if len(text) > max_length:
        raise CitationFormatError(
            "CITATION_METADATA_INVALID",
            422,
            "题录字段超过允许长度。",
            details={"field": field, "max_length": max_length},
        )
    return text


def _validate_common(metadata: CitationMetadata) -> None:
    fields = {
        "title": _clean_text(metadata.title, "title", 2000),
        "container_title": _clean_text(metadata.container_title, "container_title", 500),
        "year": metadata.year,
        "authors": metadata.authors,
    }
    missing = [key for key, value in fields.items() if not value]
    if missing:
        raise CitationFormatError(
            "CITATION_METADATA_INCOMPLETE",
            422,
            "题录缺少生成规范引文所需的真实字段；未猜测补齐。",
            details={"missing_fields": missing},
        )
    for author in metadata.authors:
        _clean_text(author.family or author.literal, "authors", 300)
        _clean_text(author.given, "authors", 200)
    for field in (
        "volume",
        "issue",
        "page",
        "article_number",
        "publisher",
        "url",
        "language",
        "license",
        "license_url",
    ):
        limit = 4000 if field == "license" else 2000
        _clean_text(getattr(metadata, field), field, limit)
    if metadata.doi is not None:
        if normalize_doi(metadata.doi) is None:
            raise CitationFormatError(
                "CITATION_METADATA_CONFLICT",
                422,
                "DOI 格式不合法；未猜测或改写编号。",
                details={"field": "doi"},
            )
    if metadata.pmid is not None and not PMID_PATTERN.fullmatch(metadata.pmid):
        raise CitationFormatError(
            "CITATION_METADATA_CONFLICT",
            422,
            "PMID 格式不合法。",
            details={"field": "pmid"},
        )
    if metadata.pmcid is not None and not PMCID_PATTERN.fullmatch(metadata.pmcid):
        raise CitationFormatError(
            "CITATION_METADATA_CONFLICT",
            422,
            "PMCID 格式不合法。",
            details={"field": "pmcid"},
        )
    for field in ("page", "article_number"):
        value = getattr(metadata, field)
        if value is not None and not PAGE_PATTERN.fullmatch(value):
            raise CitationFormatError(
                "CITATION_METADATA_CONFLICT",
                422,
                "页码或文章编号格式不合法。",
                details={"field": field},
            )


def validate_metadata(metadata: CitationMetadata, style: str) -> None:
    _validate_common(metadata)
    if style not in CITATION_STYLE_KEYS:
        raise CitationFormatError(
            "CITATION_STYLE_UNSUPPORTED",
            422,
            "不支持的引用样式。",
            details={"citation_style": style, "supported": list(CITATION_STYLE_KEYS)},
        )
    missing = []
    if metadata.item_type == "article-journal" and not metadata.volume:
        missing.append("volume")
    if not (metadata.page or metadata.article_number or metadata.doi):
        missing.append("page_or_article_number_or_doi")
    if missing:
        raise CitationFormatError(
            "CITATION_METADATA_INCOMPLETE",
            422,
            "题录缺少该 CSL 样式所需的卷期或页码/DOI；未猜测补齐。",
            details={"missing_fields": missing, "style": style},
        )


def _flat(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _csl_item(metadata: CitationMetadata) -> dict:
    authors = []
    for author in metadata.authors:
        if author.literal:
            authors.append({"literal": _flat(author.literal)})
        else:
            item = {}
            if author.family:
                item["family"] = _flat(author.family)
            if author.given:
                item["given"] = _flat(author.given)
            authors.append(item)
    date_parts: list[int] = [metadata.year]
    if metadata.month is not None:
        date_parts.append(metadata.month)
        if metadata.day is not None:
            date_parts.append(metadata.day)
    item: dict = {
        "id": "citation-item",
        "type": metadata.item_type,
        "title": _flat(metadata.title),
        "author": authors,
        "issued": {"date-parts": [date_parts]},
        "container-title": _flat(metadata.container_title),
    }
    optional = {
        "volume": metadata.volume,
        "issue": metadata.issue,
        "page": metadata.page or metadata.article_number,
        "article-number": metadata.article_number,
        "DOI": normalize_doi(metadata.doi) if metadata.doi else None,
        "PMID": metadata.pmid,
        "PMCID": metadata.pmcid,
        "publisher": metadata.publisher,
        "URL": metadata.url,
        "language": metadata.language,
    }
    item.update({key: value for key, value in optional.items() if value})
    if metadata.item_type == "article":
        repository = item.pop("container-title")
        item.setdefault("publisher", repository)
        item["genre"] = "Preprint"
    return item


def format_citation(
    metadata: CitationMetadata,
    style: CitationStyle,
    *,
    locale: str = "en-US",
) -> FormattedCitation:
    info = _style_info(style)
    validate_metadata(metadata, style)
    citeproc = _load_citeproc()
    csl_item = _csl_item(metadata)
    try:
        source = citeproc["CiteProcJSON"]([csl_item])
        style_object = citeproc["CitationStylesStyle"](
            str(info.path), locale=locale, validate=False
        )
        bibliography = citeproc["CitationStylesBibliography"](
            style_object, source, citeproc["formatter"].plain
        )
        citation = citeproc["Citation"]([citeproc["CitationItem"](csl_item["id"])])
        bibliography.register(citation)
        bibliography_text = "".join(bibliography.bibliography()[0]).strip()
        postprocess = None
        if info.citation_format == "numeric":
            bibliography_text = _normalize_numeric_spacing(bibliography_text)
            bibliography_text = re.sub(r"^\d+\.\s*", "", bibliography_text).strip()
            postprocess = "numeric-spacing-v1"
        in_text = None
        if info.citation_format == "author-date":
            in_text = "".join(bibliography.cite(citation, lambda value: value)).strip()
    except CitationFormatError:
        raise
    except Exception as exc:
        raise CitationFormatError(
            "CITATION_FORMAT_FAILED",
            422,
            "CSL 样式无法用当前题录生成引文。",
            details={"style": style, "reason": type(exc).__name__},
        ) from exc
    if not bibliography_text:
        raise CitationFormatError(
            "CITATION_FORMAT_FAILED",
            422,
            "CSL 样式返回空书目条目。",
            details={"style": style},
        )
    return FormattedCitation(
        style=style,
        style_id=info.style_id,
        style_title=info.title,
        style_sha256=info.sha256,
        renderer=f"citeproc-py {citeproc['version']}",
        postprocess=postprocess,
        bibliography=bibliography_text,
        in_text=in_text,
        numbering_required=info.citation_format != "author-date",
        csl_item=csl_item,
    )


def _ris_author(author: CitationAuthor) -> str:
    if author.literal:
        return _flat(author.literal)
    return _flat(f"{author.family}, {author.given}".strip(", "))


def _page_parts(metadata: CitationMetadata) -> tuple[str | None, str | None]:
    page = metadata.page or metadata.article_number
    if not page:
        return None, None
    match = re.fullmatch(r"([^-–—]+)[-–—](.+)", page)
    if match:
        return match.group(1), match.group(2)
    return page, None


def _normalize_numeric_spacing(text: str) -> str:
    text = re.sub(r"^(\d+\.)\s*", r"\1 ", text)
    text = re.sub(
        r"(?<=\d)(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?=\d)",
        r" \1 ",
        text,
    )
    text = re.sub(
        r"(?<=\d)(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
        r" \1",
        text,
    )
    text = re.sub(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{1,2})\b",
        r"\1 \2",
        text,
    )
    return re.sub(r"\b(doi|PMID|PMCID):(?=\S)", r"\1: ", text)


def serialize_ris(metadata: CitationMetadata) -> str:
    _validate_common(metadata)
    preprint = metadata.item_type == "article"
    lines = ["TY  - UNPB" if preprint else "TY  - JOUR"]
    for author in metadata.authors:
        lines.append(f"AU  - {_ris_author(author)}")
    lines.append(f"TI  - {_flat(metadata.title)}")
    if preprint:
        lines.extend([f"PB  - {_flat(metadata.container_title)}", "M3  - Preprint"])
    else:
        lines.append(f"JO  - {_flat(metadata.container_title)}")
    for tag, value in (
        ("VL", metadata.volume),
        ("IS", metadata.issue),
        ("PY", str(metadata.year)),
        ("DO", normalize_doi(metadata.doi) if metadata.doi else None),
        ("UR", metadata.url),
    ):
        if value:
            lines.append(f"{tag}  - {value}")
    start, end = _page_parts(metadata)
    if start:
        lines.append(f"SP  - {start}")
    if end:
        lines.append(f"EP  - {end}")
    lines.append("ER  -")
    return "\n".join(lines) + "\n"


def _bibtex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "&": r"\&",
        "%": r"\%",
        "_": r"\_",
        "#": r"\#",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


def _bibtex_key(metadata: CitationMetadata) -> str:
    first = metadata.authors[0]
    name = first.family or first.literal or "reference"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9]", "", ascii_name).casefold() or "reference"
    title_word = next(
        (
            word
            for word in re.findall(r"[A-Za-z0-9]+", metadata.title)
            if word.casefold() not in {"a", "an", "the", "on", "of", "in", "for", "and"}
        ),
        "reference",
    ).casefold()
    return f"{ascii_name}{metadata.year}{title_word}"


def serialize_bibtex(metadata: CitationMetadata) -> str:
    _validate_common(metadata)
    authors = " and ".join(_ris_author(author) for author in metadata.authors)
    preprint = metadata.item_type == "article"
    fields = [
        ("author", authors),
        ("title", _flat(metadata.title)),
        ("note", f"Preprint, {_flat(metadata.container_title)}")
        if preprint
        else ("journal", _flat(metadata.container_title)),
        ("year", str(metadata.year)),
    ]
    for key, value in (
        ("volume", metadata.volume),
        ("number", metadata.issue),
        ("pages", metadata.page or metadata.article_number),
        ("doi", normalize_doi(metadata.doi) if metadata.doi else None),
        ("url", metadata.url),
    ):
        if value:
            fields.append((key, value))
    body = ",\n".join(f"  {key} = {{{_bibtex_escape(value)}}}" for key, value in fields)
    entry_type = "unpublished" if preprint else "article"
    return f"@{entry_type}{{{_bibtex_key(metadata)},\n{body}\n}}\n"


def serialize_reference(metadata: CitationMetadata, fmt: ReferenceFormat) -> str:
    if fmt == "ris":
        return serialize_ris(metadata)
    if fmt == "bibtex":
        return serialize_bibtex(metadata)
    if fmt == "csl-json":
        _validate_common(metadata)
        return json.dumps(_csl_item(metadata), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    raise CitationFormatError(
        "CITATION_FORMAT_UNSUPPORTED",
        422,
        "不支持的引文序列化格式。",
        details={"format": fmt},
    )


def copy_formats(metadata: CitationMetadata) -> list[dict]:
    output = []
    for key, label in COPY_FORMATS.items():
        option = {"format": key, "label": label}
        try:
            if key in CITATION_STYLE_KEYS:
                style = "apa" if key == "apa" else "vancouver"
                rendered = format_citation(metadata, style)
                option.update(
                    text=rendered.bibliography,
                    style_id=rendered.style_id,
                    numbering_required=rendered.numbering_required,
                )
            else:
                option["text"] = serialize_reference(
                    metadata, "bibtex" if key == "bibtex" else "ris"
                )
        except CitationFormatError as exc:
            option["failure"] = {"code": exc.code, "message": exc.message, "details": exc.details}
        output.append(option)
    return output


__all__ = [
    "CITATION_STYLE_KEYS",
    "CitationAuthor",
    "CitationFormatError",
    "CitationMetadata",
    "FormattedCitation",
    "copy_formats",
    "format_citation",
    "serialize_bibtex",
    "serialize_reference",
    "serialize_ris",
    "validate_metadata",
]
