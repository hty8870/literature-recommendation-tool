"""Version-bound open full-text resolution and evidence selection."""

from __future__ import annotations

import calendar
import hashlib
import html
import http.client
import json
import math
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import quote, urlencode, urlparse

from pydantic import Field, ValidationError, model_validator

from .citation_format import CitationAuthor, CitationMetadata
from .citation_research_square import (
    LICENSE_URL as RESEARCH_SQUARE_LICENSE_URL,
)
from .citation_research_square import (
    MAX_RESEARCH_SQUARE_BYTES,
    RESEARCH_SQUARE_HOST,
    RESEARCH_SQUARE_NORMALIZATION,
    RESEARCH_SQUARE_PATH,
    PreprintError,
    parse_preprint,
    research_square_url,
)
from .entities import CandidateDiagnostic, Recommendation, StrictModel, stable_id, utc_now_iso
from .publication import normalize_doi
from .reading import TextBlock

EUROPE_PMC_HOST = "www.ebi.ac.uk"
EUROPE_PMC_REST_PREFIX = "/europepmc/webservices/rest/"
EUROPE_PMC_SEARCH_PATH = EUROPE_PMC_REST_PREFIX + "search"
EUROPE_PMC_ARTICLE_URL = "https://europepmc.org/article/PMC/{pmcid}"
PMC_OAI_HOST = "pmc.ncbi.nlm.nih.gov"
PMC_OAI_PATH = "/api/oai/v1/mh/"
OAI_NAMESPACE = "{http://www.openarchives.org/OAI/2.0/}"
_PMC_REQUEST_LOCK = threading.Lock()
_PMC_NEXT_REQUEST = 0.0
MAX_SEARCH_BYTES = 2_000_000
MAX_FULLTEXT_BYTES = 8_000_000
MAX_PARAGRAPH_CHARS = 20_000
MIN_PARAGRAPH_CHARS = 80
MAX_QUOTE_CHARS = 600
DEFAULT_TIMEOUT_MS = 30_000
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
PMID_TOKEN = re.compile(
    r"(?:paper:europepmc:MED:|/article/MED/|pubmed\.ncbi\.nlm\.nih\.gov/|PMID[:\s])(\d{5,12})",
    re.IGNORECASE,
)
PMCID_TOKEN = re.compile(r"(PMC\d{4,12})", re.IGNORECASE)
DOI_TOKEN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
OPEN_LICENSE = re.compile(
    r"(cc0|cc[ -]?by|creative\s+commons|open\s+access|public\s+domain)", re.IGNORECASE
)
CLOSED_LICENSE = re.compile(
    r"(all\s+rights\s+reserved|not\s+open\s+access|no\s+reuse|publisher\s+copyright)",
    re.IGNORECASE,
)
METHOD_SECTIONS = re.compile(
    r"(method|material|patient|participant|experimental|protocol|study design|"
    r"方法|材料|对象|实验|患者|受试者)",
    re.IGNORECASE,
)
RESULT_SECTIONS = re.compile(
    r"(result|finding|discussion|conclusion|结果|发现|讨论|结论)", re.IGNORECASE
)
BACKGROUND_SECTIONS = re.compile(
    r"(introduction|background|literature|引言|背景|综述)", re.IGNORECASE
)
SKIPPED_SECTIONS = re.compile(
    r"(reference|acknowledg|author contribution|funding|conflict|competing interest|"
    r"data availability|supplementary|associated data|footnote|abbreviation|"
    r"参考文献|致谢|作者贡献|基金|利益冲突|数据可用性|补充材料|脚注|缩略语)",
    re.IGNORECASE,
)
STOPWORDS = {
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "have",
    "into",
    "not",
    "that",
    "the",
    "their",
    "this",
    "was",
    "were",
    "with",
    "研究",
    "本文",
    "以及",
    "我们",
    "一个",
    "进行",
    "结果",
    "方法",
    "讨论",
}


class CitationSourceError(Exception):
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


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, accept: str, timeout_s: float, max_bytes: int) -> HttpResponse: ...


@dataclass(frozen=True, slots=True)
class HttpsTransport:
    def get(self, url: str, *, accept: str, timeout_s: float, max_bytes: int) -> HttpResponse:
        parsed = _validate_official_url(url)
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=timeout_s
        )
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": accept,
                    "User-Agent": "citation-tool-research/0.2",
                },
            )
            response = connection.getresponse()
            body = response.read(max_bytes + 1)
            status = response.status
            headers = {key.lower(): value for key, value in response.getheaders()}
        except (TimeoutError, OSError, http.client.HTTPException):
            raise CitationSourceError(
                "CITATION_SOURCE_TIMEOUT",
                504,
                "官方来源请求超时或网络不可达。",
                retryable=True,
            ) from None
        finally:
            connection.close()
        if status in {301, 302, 303, 307, 308}:
            raise CitationSourceError(
                "CITATION_SOURCE_REDIRECT_REFUSED",
                502,
                "Europe PMC 返回重定向；服务不跟随重定向。",
            )
        if len(body) > max_bytes:
            raise CitationSourceError(
                "CITATION_SOURCE_RESPONSE_TOO_LARGE",
                502,
                "Europe PMC 响应超过大小上限。",
            )
        return HttpResponse(status=status, headers=headers, body=body)


def _validate_official_url(url: str):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not (
            (parsed.hostname == EUROPE_PMC_HOST and parsed.path.startswith(EUROPE_PMC_REST_PREFIX))
            or (parsed.hostname == PMC_OAI_HOST and parsed.path == PMC_OAI_PATH)
            or (
                parsed.hostname == RESEARCH_SQUARE_HOST
                and not parsed.query
                and RESEARCH_SQUARE_PATH.fullmatch(parsed.path)
            )
        )
    ):
        raise CitationSourceError(
            "CITATION_SOURCE_URL_INVALID", 422, "只允许受支持的官方全文来源 HTTPS 地址。"
        )
    return parsed


def _get_with_retry(
    transport: HttpTransport,
    url: str,
    *,
    accept: str,
    timeout_s: float,
    max_bytes: int,
) -> HttpResponse:
    deadline = time.monotonic() + timeout_s
    for backoff in (0.25, 0.75, None):
        try:
            response = transport.get(
                url,
                accept=accept,
                timeout_s=max(0.001, deadline - time.monotonic()),
                max_bytes=max_bytes,
            )
        except CitationSourceError as exc:
            if (
                backoff is None
                or not exc.retryable
                or exc.code != "CITATION_SOURCE_TIMEOUT"
                or time.monotonic() + backoff >= deadline
            ):
                raise
        else:
            if backoff is None or response.status not in {500, 502, 503, 504}:
                return response
            retry_after = response.headers.get("retry-after")
            if retry_after is not None:
                if not re.fullmatch(r"[0-9]{1,3}", retry_after):
                    return response
                backoff = max(backoff, int(retry_after))
            if backoff > 3 or time.monotonic() + backoff >= deadline:
                return response
        time.sleep(backoff)
    raise AssertionError("bounded source retry did not return")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_title(value: str | None) -> str:
    text = _clean_text(value).casefold()
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class CitationIdentity(StrictModel):
    title: str | None = None
    alternate_titles: list[str] = Field(default_factory=list)
    authors: str | None = None
    year: int | None = Field(default=None, ge=1000, le=3000)
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    source_uris: list[str] = Field(default_factory=list)
    publication_status: Literal["none", "retracted", "corrected", "unknown"] = "unknown"
    publication_types: list[str] = Field(default_factory=list)
    related_dois: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_identity(self) -> CitationIdentity:
        if self.doi is not None:
            normalized = normalize_doi(self.doi)
            if normalized is None:
                raise ValueError("identity DOI is invalid")
            self.doi = normalized
        if self.pmid is not None and not re.fullmatch(r"\d{1,12}", self.pmid):
            raise ValueError("identity PMID is invalid")
        if self.pmcid is not None:
            match = PMCID_TOKEN.fullmatch(self.pmcid)
            if not match:
                raise ValueError("identity PMCID is invalid")
            self.pmcid = match.group(1).upper()
        if not (self.doi or self.pmid or self.pmcid or self.title):
            raise ValueError("identity requires DOI, PMID, PMCID, or title")
        return self

    @property
    def titles(self) -> list[str]:
        return [title for title in [self.title, *self.alternate_titles] if title]

    @classmethod
    def from_candidate(cls, candidate: CandidateDiagnostic) -> CitationIdentity:
        values = [
            candidate.paper_id,
            candidate.source_uri or "",
            *candidate.source_uris.values(),
            *(record.paper_id for record in candidate.source_records),
            *(record.source_uri or "" for record in candidate.source_records),
        ]
        dois = {
            normalized
            for value in [candidate.doi, *(record.doi for record in candidate.source_records)]
            if (normalized := normalize_doi(value))
        }
        pmids = {match.group(1) for value in values for match in PMID_TOKEN.finditer(value)}
        pmcids = {
            match.group(1).upper() for value in values for match in PMCID_TOKEN.finditer(value)
        }
        titles = [
            title
            for title in [candidate.title, *(record.title for record in candidate.source_records)]
            if title and title.strip()
        ]
        if len(dois) > 1:
            raise CitationSourceError(
                "CITATION_IDENTITY_AMBIGUOUS",
                422,
                "候选包含多个不同 DOI，无法确定唯一论文身份。",
                details={"dois": sorted(dois)},
            )
        if len(pmids) > 1:
            raise CitationSourceError(
                "CITATION_IDENTITY_AMBIGUOUS",
                422,
                "候选包含多个不同 PMID，无法确定唯一论文身份。",
                details={"pmids": sorted(pmids)},
            )
        if len(pmcids) > 1:
            raise CitationSourceError(
                "CITATION_IDENTITY_AMBIGUOUS",
                422,
                "候选包含多个不同 PMCID，无法确定唯一论文身份。",
                details={"pmcids": sorted(pmcids)},
            )
        primary_title = candidate.title or (titles[0] if titles else None)
        return cls(
            title=primary_title,
            alternate_titles=sorted({title for title in titles if title != primary_title}),
            authors=candidate.authors
            or next(
                (record.authors for record in candidate.source_records if record.authors), None
            ),
            year=candidate.year
            or next((record.year for record in candidate.source_records if record.year), None),
            doi=next(iter(dois), None),
            pmid=next(iter(pmids), None),
            pmcid=next(iter(pmcids), None),
            source_uris=sorted({value for value in values if value}),
            publication_status=candidate.publication_status,
            publication_types=sorted(candidate.publication_types),
            related_dois=candidate.related_dois,
        )

    @classmethod
    def from_recommendation(cls, recommendation: Recommendation) -> CitationIdentity:
        values = [source.paper_id for source in recommendation.sources] + [
            source.doi or "" for source in recommendation.sources
        ]
        dois = {
            normalized
            for source in recommendation.sources
            if (normalized := normalize_doi(source.doi))
        }
        pmids = {match.group(1) for value in values for match in PMID_TOKEN.finditer(value)}
        pmcids = {
            match.group(1).upper() for value in values for match in PMCID_TOKEN.finditer(value)
        }
        titles = [
            source.title
            for source in recommendation.sources
            if source.title and source.title.strip()
        ]
        if len(dois) > 1 or len(pmids) > 1 or len(pmcids) > 1:
            raise CitationSourceError(
                "CITATION_IDENTITY_AMBIGUOUS",
                422,
                "推荐包含多个不同论文编号，无法确定唯一论文身份。",
            )
        status = (
            "retracted"
            if any(source.publication_status == "retracted" for source in recommendation.sources)
            else "corrected"
            if any(source.publication_status == "corrected" for source in recommendation.sources)
            else "unknown"
        )
        related: dict[str, list[str]] = {}
        for source in recommendation.sources:
            for key, items in source.related_dois.items():
                related.setdefault(key, []).extend(items)
        first = recommendation.sources[0] if recommendation.sources else None
        first_title = first.title if first else None
        return cls(
            title=first_title,
            alternate_titles=sorted({title for title in titles if title != first_title}),
            authors=first.authors if first else None,
            year=first.year if first else None,
            doi=next(iter(dois), None),
            pmid=next(iter(pmids), None),
            pmcid=next(iter(pmcids), None),
            source_uris=sorted({value for value in values if value}),
            publication_status=status,  # type: ignore[arg-type]
            publication_types=[],
            related_dois={key: sorted(set(items)) for key, items in related.items()},
        )


class CitationPassage(StrictModel):
    evidence_id: str
    quote: str
    quote_sha256: str
    paragraph_text: str
    paragraph_sha256: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    section_path: list[str] = Field(default_factory=list)
    section_id: str | None = None
    paragraph_id: str | None = None
    paragraph_index: int = Field(ge=0)
    source_url: str
    source_level: Literal["fulltext_xml", "fulltext_html", "verified_evidence"] = "fulltext_xml"
    normalization_version: str = "jats-text-collapse-v1"
    evidence_role: Literal[
        "primary_result",
        "method",
        "background",
        "review_secondary",
        "correction_notice",
    ] = "primary_result"
    score: float | None = None


class CitationSourceProvenance(StrictModel):
    source_id: str
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    article_url: str
    xml_url: str | None = None
    fulltext_sha256: str | None = None
    retrieved_at: str
    license: str
    license_url: str | None = None
    is_open_access: bool
    source_level: Literal["fulltext_xml", "fulltext_html", "metadata_only"] = "fulltext_xml"
    api_version: str | None = None
    metadata_warnings: list[str] = Field(default_factory=list)


class CitationSourceBundle(StrictModel):
    metadata: CitationMetadata
    evidence: list[CitationPassage] = Field(default_factory=list)
    provenance: CitationSourceProvenance


@dataclass(frozen=True, slots=True)
class OpenArticleText:
    blocks: tuple[TextBlock, ...]
    sha256: str
    pmcid: str
    source_url: str
    license: str
    provider: Literal["europe_pmc", "pmc_oai"]
    fallback_code: str | None


class EuropePmcSourceResolver:
    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_evidence: int = 4,
        max_quote_chars: int = MAX_QUOTE_CHARS,
    ) -> None:
        if not 1_000 <= timeout_ms <= 60_000:
            raise ValueError("timeout_ms must be between 1000 and 60000")
        if not 1 <= max_evidence <= 8:
            raise ValueError("max_evidence must be between 1 and 8")
        if not 200 <= max_quote_chars <= 2000:
            raise ValueError("max_quote_chars must be between 200 and 2000")
        self._transport = transport or HttpsTransport()
        self._timeout_s = timeout_ms / 1000
        self._max_evidence = max_evidence
        self._max_quote_chars = max_quote_chars

    def _download_fulltext(
        self, record: dict, identity: CitationIdentity, *, timeout_s: float, retry: bool = False
    ) -> tuple[str, str, bytes]:
        self._check_publication_status(record, identity)
        self._check_open_access(record)
        pmcid = str(record.get("pmcid") or "").upper()
        xml_url = f"https://{EUROPE_PMC_HOST}{EUROPE_PMC_REST_PREFIX}{quote(pmcid)}/fullTextXML"
        options = {
            "accept": "application/xml",
            "timeout_s": timeout_s,
            "max_bytes": MAX_FULLTEXT_BYTES,
        }
        response = (
            _get_with_retry(self._transport, xml_url, **options)
            if retry
            else self._transport.get(xml_url, **options)
        )
        if response.status == 429:
            raise CitationSourceError(
                "CITATION_SOURCE_RATE_LIMITED", 503, "Europe PMC 全文接口限流。", retryable=True
            )
        if response.status != 200:
            raise CitationSourceError(
                "CITATION_SOURCE_UNAVAILABLE",
                502,
                f"Europe PMC 全文接口返回 HTTP {response.status}。",
                retryable=response.status >= 500,
            )
        return pmcid, xml_url, response.body

    def read_fulltext(self, record: dict, identity: CitationIdentity) -> OpenArticleText:
        deadline = time.monotonic() + self._timeout_s
        provider: Literal["europe_pmc", "pmc_oai"] = "europe_pmc"
        fallback_code = None
        try:
            pmcid, _, body = self._download_fulltext(
                record, identity, timeout_s=self._timeout_s / 2
            )
        except CitationSourceError as exc:
            if not exc.retryable or exc.code not in {
                "CITATION_SOURCE_TIMEOUT",
                "CITATION_SOURCE_UNAVAILABLE",
            }:
                raise
            # This branch runs only after the original identity, access and license checks.
            pmcid = str(record["pmcid"]).upper()
            provider, fallback_code = "pmc_oai", exc.code
            root, body = _read_pmc_oai(self._transport, pmcid, deadline)
        else:
            root = _load_article(body)
        _check_read_identity(root, pmcid, identity, record.get("title"))
        license_text, _ = _jats_license(root, record)
        retracted, corrected = _jats_publication_flags(root)
        if retracted or corrected:
            raise CitationSourceError(
                "CITATION_RETRACTED" if retracted else "CITATION_CORRECTED",
                409,
                "全文版本有撤稿或更正状态，不能作为普通论文全文证据读取。",
            )
        blocks = _reading_blocks(root)
        return OpenArticleText(
            blocks,
            hashlib.sha256(body).hexdigest(),
            pmcid,
            EUROPE_PMC_ARTICLE_URL.format(pmcid=pmcid)
            if provider == "europe_pmc"
            else f"https://{PMC_OAI_HOST}/articles/{pmcid}/",
            license_text,
            provider,
            fallback_code,
        )

    def resolve(self, identity: CitationIdentity, claim_text: str) -> CitationSourceBundle:
        self._check_publication_status({}, identity)
        preprint_url = research_square_url(identity.doi)
        try:
            record, api_version = self._lookup(identity)
        except CitationSourceError as exc:
            if (
                preprint_url
                and exc.retryable
                and exc.code
                in {
                    "CITATION_SOURCE_UNAVAILABLE",
                    "CITATION_SOURCE_TIMEOUT",
                    "CITATION_SOURCE_RATE_LIMITED",
                }
            ):
                return self._resolve_preprint(preprint_url, identity, {}, claim_text)
            raise
        deadline = time.monotonic() + self._timeout_s
        try:
            pmcid, xml_url, body = self._download_fulltext(
                record, identity, timeout_s=self._timeout_s / 2, retry=True
            )
        except CitationSourceError as exc:
            if preprint_url and exc.code in {
                "CITATION_FULLTEXT_UNAVAILABLE",
                "CITATION_SOURCE_NOT_OPEN",
            }:
                return self._resolve_preprint(preprint_url, identity, record, claim_text)
            if not exc.retryable or exc.code not in {
                "CITATION_SOURCE_UNAVAILABLE",
                "CITATION_SOURCE_TIMEOUT",
            }:
                raise
            pmcid = str(record["pmcid"]).upper()
            try:
                root, body = _read_pmc_oai(self._transport, pmcid, deadline)
            except CitationSourceError as fallback:
                if fallback.code in {
                    "CITATION_FULLTEXT_UNAVAILABLE",
                    "CITATION_SOURCE_UNAVAILABLE",
                    "CITATION_SOURCE_TIMEOUT",
                    "CITATION_SOURCE_RATE_LIMITED",
                }:
                    raise exc from fallback
                raise
            _check_read_identity(root, pmcid, identity, record.get("title"))
            xml_url = _pmc_oai_request(pmcid)[0]
            article_url = f"https://{PMC_OAI_HOST}/articles/{pmcid}/"
            api_version = "pmc-oai-v1"
        else:
            root = _load_article(body)
            article_url = EUROPE_PMC_ARTICLE_URL.format(pmcid=pmcid)
        metadata, evidence, warnings = self._parse_fulltext(
            root, record, identity, pmcid, claim_text, source_url=article_url
        )
        license_text = metadata.license or str(record.get("license") or "")
        provenance = CitationSourceProvenance(
            source_id=pmcid,
            doi=metadata.doi,
            pmid=metadata.pmid,
            pmcid=pmcid,
            article_url=article_url,
            xml_url=xml_url,
            fulltext_sha256=hashlib.sha256(body).hexdigest(),
            retrieved_at=utc_now_iso(),
            license=license_text,
            license_url=metadata.license_url,
            is_open_access=True,
            source_level="fulltext_xml",
            api_version=api_version,
            metadata_warnings=warnings,
        )
        return CitationSourceBundle(metadata=metadata, evidence=evidence, provenance=provenance)

    def _resolve_preprint(
        self, url: str, identity: CitationIdentity, record: dict, claim_text: str
    ) -> CitationSourceBundle:
        response = _get_with_retry(
            self._transport,
            url,
            accept="text/html",
            timeout_s=self._timeout_s,
            max_bytes=MAX_RESEARCH_SQUARE_BYTES,
        )
        if response.status in {401, 403}:
            raise CitationSourceError(
                "CITATION_SOURCE_NOT_OPEN", 422, "Research Square 未开放该版本的正文。"
            )
        if response.status in {404, 410}:
            raise CitationSourceError(
                "CITATION_FULLTEXT_UNAVAILABLE", 422, "Research Square 未提供该版本的正文。"
            )
        if response.status != 200:
            raise CitationSourceError(
                "CITATION_SOURCE_UNAVAILABLE",
                502,
                f"Research Square 全文接口返回 HTTP {response.status}。",
                retryable=response.status == 429 or response.status >= 500,
            )
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/html":
            raise CitationSourceError(
                "CITATION_SOURCE_INVALID_RESPONSE", 502, "Research Square 未返回可解析的正文网页。"
            )
        doi = identity.doi or ""
        try:
            preprint = parse_preprint(response.body, doi)
        except PreprintError as exc:
            code, status, message = {
                "identity": (
                    "CITATION_IDENTITY_MISMATCH",
                    422,
                    "Research Square 的 DOI 或版本不一致。",
                ),
                "license": ("CITATION_LICENSE_UNCLEAR", 422, "此预印本版本没有可确认的开放许可。"),
                "withdrawn": ("CITATION_RETRACTED", 409, "该预印本已撤回或撤稿。"),
                "unavailable": (
                    "CITATION_FULLTEXT_UNAVAILABLE",
                    422,
                    "Research Square 没有可核验的同版正文。",
                ),
            }[exc.kind]
            raise CitationSourceError(code, status, message) from exc
        family, _ = preprint.authors[0]
        family_tokens = _first_author_tokens(family)
        author_tokens = _first_author_tokens(identity.authors)
        date = preprint.version_date
        if (
            not self._title_matches(preprint.title, identity.titles)
            or (author_tokens and not family_tokens <= author_tokens)
            or (identity.year is not None and abs(date.year - identity.year) > 1)
        ):
            raise CitationSourceError(
                "CITATION_IDENTITY_MISMATCH",
                422,
                "Research Square 的标题、作者或版本年份与候选冲突。",
            )
        try:
            metadata = CitationMetadata(
                item_type="article",
                title=preprint.title,
                authors=[
                    CitationAuthor(family=name, given=given or None)
                    for name, given in preprint.authors
                ],
                year=date.year,
                month=date.month,
                day=date.day,
                container_title="Research Square",
                publisher="Research Square",
                doi=doi,
                url=url,
                license="CC BY 4.0",
                license_url=RESEARCH_SQUARE_LICENSE_URL,
            )
        except ValidationError as exc:
            raise CitationSourceError(
                "CITATION_METADATA_INVALID", 422, "Research Square 的版本题录不完整或无效。"
            ) from exc
        paragraphs = [
            _Paragraph(item.text, list(item.section_path), None, None, index)
            for index, item in enumerate(preprint.paragraphs, 1)
            if MIN_PARAGRAPH_CHARS <= len(item.text) <= MAX_PARAGRAPH_CHARS
        ]
        evidence = self._select_evidence(
            paragraphs,
            claim_text,
            is_review=preprint.is_review or _is_review(record),
            source_id=doi,
            source_url=url,
            source_level="fulltext_html",
            normalization_version=RESEARCH_SQUARE_NORMALIZATION,
        )
        return CitationSourceBundle(
            metadata=metadata,
            evidence=evidence,
            provenance=CitationSourceProvenance(
                source_id=doi,
                doi=doi,
                article_url=url,
                fulltext_sha256=hashlib.sha256(response.body).hexdigest(),
                retrieved_at=utc_now_iso(),
                license="CC BY 4.0",
                license_url=RESEARCH_SQUARE_LICENSE_URL,
                is_open_access=True,
                source_level="fulltext_html",
                api_version=RESEARCH_SQUARE_NORMALIZATION,
                metadata_warnings=["引用的是对应版本的预印本，不是期刊正式版。"],
            ),
        )

    def lookup_metadata(
        self, identity: CitationIdentity
    ) -> tuple[CitationMetadata, CitationSourceProvenance]:
        record, api_version = self._lookup(identity)
        self._check_publication_status(record, identity)
        metadata = self._metadata_from_record(record)
        provenance = CitationSourceProvenance(
            source_id=str(record.get("pmcid") or record.get("pmid") or record.get("id") or ""),
            doi=metadata.doi,
            pmid=metadata.pmid,
            pmcid=metadata.pmcid,
            article_url=(
                EUROPE_PMC_ARTICLE_URL.format(pmcid=metadata.pmcid)
                if metadata.pmcid
                else f"https://europepmc.org/article/{quote(str(record.get('source') or 'MED'))}/"
                f"{quote(str(record.get('id') or ''))}"
            ),
            retrieved_at=utc_now_iso(),
            license=metadata.license or "",
            license_url=metadata.license_url,
            is_open_access=str(record.get("isOpenAccess") or "").upper() == "Y",
            source_level="metadata_only",
            api_version=api_version,
        )
        return metadata, provenance

    def _lookup(self, identity: CitationIdentity) -> tuple[dict, str | None]:
        queries: list[tuple[str, str]] = []
        if identity.doi:
            queries.append(("doi", f'DOI:"{_query_value(identity.doi)}"'))
        if identity.pmid:
            queries.append(("pmid", f"EXT_ID:{identity.pmid} AND SRC:MED"))
        if identity.pmcid:
            queries.append(("pmcid", f"PMCID:{identity.pmcid}"))
        if not queries and identity.title:
            queries.append(("title", f'TITLE:"{_query_value(identity.title)}"'))
        if not queries:
            raise CitationSourceError(
                "CITATION_IDENTITY_UNRESOLVED",
                422,
                "候选没有可用于解析全文的 DOI、PMID、PMCID 或标题。",
            )
        seen: dict[tuple[str, str, str, str], dict] = {}
        api_version = None
        for kind, query in queries:
            records, version = self._search(query)
            api_version = api_version or version
            for record in records:
                if not self._matches_identity(record, identity, kind):
                    continue
                key = (
                    str(record.get("source") or ""),
                    str(record.get("id") or ""),
                    str(record.get("pmcid") or ""),
                    normalize_doi(record.get("doi")) or "",
                )
                seen[key] = record
            if seen:
                break
        if not seen:
            if identity.title and not any(kind == "title" for kind, _ in queries):
                records, version = self._search(f'TITLE:"{_query_value(identity.title)}"')
                api_version = api_version or version
                for record in records:
                    if self._matches_identity(record, identity, "title"):
                        key = (
                            str(record.get("source") or ""),
                            str(record.get("id") or ""),
                            str(record.get("pmcid") or ""),
                            normalize_doi(record.get("doi")) or "",
                        )
                        seen[key] = record
        if not seen:
            raise CitationSourceError(
                "CITATION_IDENTITY_UNRESOLVED",
                422,
                "Europe PMC 未返回与候选编号和题名一致的真实论文。",
                details={
                    "doi": identity.doi,
                    "pmid": identity.pmid,
                    "pmcid": identity.pmcid,
                },
            )
        if len(seen) > 1:
            open_access = [
                record
                for record in seen.values()
                if str(record.get("isOpenAccess") or "").upper() == "Y" and record.get("pmcid")
            ]
            if len(open_access) != 1:
                raise CitationSourceError(
                    "CITATION_IDENTITY_AMBIGUOUS",
                    422,
                    "Europe PMC 返回多个一致命中的记录，无法确定唯一全文。",
                    details={"records": sorted(seen)[:5]},
                )
            return open_access[0], api_version
        return next(iter(seen.values())), api_version

    def _search(self, query: str) -> tuple[list[dict], str | None]:
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": "5",
            "synonym": "false",
        }
        url = f"https://{EUROPE_PMC_HOST}{EUROPE_PMC_SEARCH_PATH}?{urlencode(params)}"
        response = _get_with_retry(
            self._transport,
            url,
            accept="application/json",
            timeout_s=self._timeout_s,
            max_bytes=MAX_SEARCH_BYTES,
        )
        if response.status == 429:
            raise CitationSourceError(
                "CITATION_SOURCE_RATE_LIMITED",
                503,
                "Europe PMC 请求触发限流，请稍后重试。",
                retryable=True,
            )
        if response.status != 200:
            raise CitationSourceError(
                "CITATION_SOURCE_UNAVAILABLE",
                502,
                f"Europe PMC 检索接口返回 HTTP {response.status}。",
                retryable=response.status >= 500,
            )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CitationSourceError(
                "CITATION_SOURCE_INVALID_RESPONSE",
                502,
                "Europe PMC 未返回有效的 UTF-8 JSON。",
            ) from None
        if not isinstance(payload, dict):
            raise CitationSourceError(
                "CITATION_SOURCE_INVALID_RESPONSE",
                502,
                "Europe PMC 检索响应必须是 JSON 对象。",
            )
        results = (payload.get("resultList") or {}).get("result", [])
        if not isinstance(results, list):
            raise CitationSourceError(
                "CITATION_SOURCE_INVALID_RESPONSE",
                502,
                "Europe PMC 检索响应缺少结果列表。",
            )
        return [item for item in results if isinstance(item, dict)], _optional_str(
            payload.get("version")
        )

    def _matches_identity(self, record: dict, identity: CitationIdentity, kind: str) -> bool:
        if identity.doi:
            if normalize_doi(record.get("doi")) != identity.doi:
                return False
        if identity.pmid and str(record.get("pmid") or "") != identity.pmid:
            return False
        if identity.pmcid and str(record.get("pmcid") or "").upper() != identity.pmcid:
            return False
        if kind == "title" and not identity.doi and not identity.pmid and not identity.pmcid:
            if not self._title_matches(record.get("title"), identity.titles):
                return False
        elif identity.titles and not self._title_matches(record.get("title"), identity.titles):
            return False
        if identity.year and record.get("pubYear"):
            try:
                if abs(int(record["pubYear"]) - identity.year) > 1:
                    return False
            except (TypeError, ValueError):
                return False
        author_tokens = _first_author_tokens(identity.authors)
        record_author = _record_first_author_family(record)
        if author_tokens and record_author and record_author.casefold() not in author_tokens:
            return False
        return True

    @staticmethod
    def _title_matches(candidate_title: object, identity_titles: list[str]) -> bool:
        if not candidate_title:
            return False
        normalized = _normalize_title(str(candidate_title))
        if not normalized:
            return False
        return any(_normalize_title(title) == normalized for title in identity_titles)

    def _check_publication_status(self, record: dict, identity: CitationIdentity) -> None:
        types = {value.casefold() for value in identity.publication_types}
        identity_retracted = identity.publication_status == "retracted" or bool(
            types & {"retraction", "retracted publication", "retraction of publication"}
        )
        identity_corrected = identity.publication_status == "corrected" or bool(
            types & {"published erratum", "correction", "erratum"}
        )
        if identity.related_dois.get("retracted_by"):
            identity_retracted = True
        if identity.related_dois.get("corrected_by"):
            identity_corrected = True
        if identity_retracted:
            raise CitationSourceError(
                "CITATION_RETRACTED",
                409,
                "候选论文已标记撤稿，拒绝生成引用建议。",
            )
        if identity_corrected:
            raise CitationSourceError(
                "CITATION_CORRECTED",
                409,
                "候选论文存在更正记录，拒绝基于未核实的版本生成引用建议。",
            )
        retracted, corrected = _record_publication_flags(record)
        if retracted:
            raise CitationSourceError(
                "CITATION_RETRACTED",
                409,
                "Europe PMC 标记该论文已撤稿，拒绝生成引用建议。",
            )
        if corrected:
            raise CitationSourceError(
                "CITATION_CORRECTED",
                409,
                "Europe PMC 标记该论文存在更正/勘误，拒绝基于未核实的版本生成引用建议。",
            )

    @staticmethod
    def _check_open_access(record: dict) -> None:
        pmcid = str(record.get("pmcid") or "").upper()
        if not PMCID_TOKEN.fullmatch(pmcid):
            raise CitationSourceError(
                "CITATION_FULLTEXT_UNAVAILABLE",
                422,
                "Europe PMC 未返回可用 PMCID，无法取得官方全文 XML。",
            )
        if str(record.get("isOpenAccess") or "").upper() != "Y":
            raise CitationSourceError(
                "CITATION_SOURCE_NOT_OPEN",
                422,
                "该论文不在 Europe PMC 开放获取范围，拒绝绕过权限获取全文。",
            )
        if record.get("inEPMC") is not None and str(record.get("inEPMC")).upper() != "Y":
            raise CitationSourceError(
                "CITATION_FULLTEXT_UNAVAILABLE",
                422,
                "Europe PMC 未提供该论文的全文 XML。",
            )
        license_text = str(record.get("license") or "")
        if not _license_is_open(license_text):
            raise CitationSourceError(
                "CITATION_LICENSE_UNCLEAR",
                422,
                "Europe PMC 未给出明确的开放许可，拒绝生成引用建议。",
                details={"license": license_text or None},
            )

    def _metadata_from_record(self, record: dict) -> CitationMetadata:
        authors = _record_authors(record)
        journal = (record.get("journalInfo") or {}).get("journal") or {}
        title = _clean_text(record.get("title"))
        year = _optional_int(record.get("pubYear"))
        container = _clean_text(
            journal.get("title") or journal.get("medlineAbbreviation") or record.get("journalTitle")
        )
        missing = [
            field
            for field, value in (
                ("title", title),
                ("authors", authors),
                ("year", year),
                ("container_title", container),
            )
            if not value
        ]
        if missing:
            raise CitationSourceError(
                "CITATION_METADATA_INCOMPLETE",
                422,
                "Europe PMC 题录缺少规范引文所需字段；未猜测补齐。",
                details={"missing_fields": missing},
            )
        journal_info = record.get("journalInfo") or {}
        try:
            return CitationMetadata(
                title=title,
                authors=authors,
                year=year,
                container_title=container,
                volume=_optional_str(journal_info.get("volume")),
                issue=_optional_str(journal_info.get("issue")),
                page=_optional_str(record.get("pageInfo")),
                doi=normalize_doi(record.get("doi")),
                pmid=_optional_str(record.get("pmid")),
                pmcid=(
                    _optional_str(record.get("pmcid")).upper()
                    if _optional_str(record.get("pmcid"))
                    else None
                ),
                language=_optional_str(record.get("language")),
                license=_optional_str(record.get("license")),
            )
        except ValidationError as exc:
            raise CitationSourceError(
                "CITATION_METADATA_INVALID",
                422,
                "Europe PMC 题录字段无法形成合法引文。",
            ) from exc

    def _parse_fulltext(
        self,
        root: ET.Element,
        record: dict,
        identity: CitationIdentity,
        pmcid: str,
        claim_text: str,
        *,
        source_url: str,
    ) -> tuple[CitationMetadata, list[CitationPassage], list[str]]:
        ids = _jats_ids(root)
        record_doi = normalize_doi(record.get("doi"))
        jats_doi = normalize_doi(ids.get("doi"))
        if identity.doi and jats_doi and identity.doi != jats_doi:
            raise CitationSourceError(
                "CITATION_IDENTITY_MISMATCH",
                422,
                "全文 XML 的 DOI 与候选 DOI 不一致。",
                details={"candidate_doi": identity.doi, "fulltext_doi": jats_doi},
            )
        if record_doi and jats_doi and record_doi != jats_doi:
            raise CitationSourceError(
                "CITATION_IDENTITY_MISMATCH",
                422,
                "Europe PMC 题录 DOI 与全文 XML 不一致。",
            )
        if identity.pmid and ids.get("pmid") and identity.pmid != ids["pmid"]:
            raise CitationSourceError(
                "CITATION_IDENTITY_MISMATCH",
                422,
                "全文 XML 的 PMID 与候选 PMID 不一致。",
            )
        if identity.pmcid and ids.get("pmcid") and identity.pmcid != ids["pmcid"].upper():
            raise CitationSourceError(
                "CITATION_IDENTITY_MISMATCH",
                422,
                "全文 XML 的 PMCID 与候选 PMCID 不一致。",
            )
        title = _jats_title(root)
        if not title:
            raise CitationSourceError(
                "CITATION_METADATA_INCOMPLETE",
                422,
                "全文 XML 缺少文章标题。",
                details={"missing_fields": ["title"]},
            )
        if identity.titles and not self._title_matches(title, identity.titles):
            raise CitationSourceError(
                "CITATION_IDENTITY_MISMATCH",
                422,
                "全文 XML 标题与候选标题不一致。",
                details={"candidate_titles": identity.titles, "fulltext_title": title},
            )
        authors, author_warnings = _jats_authors(root, record)
        journal = _jats_journal(root) or _clean_text(
            ((record.get("journalInfo") or {}).get("journal") or {}).get("title")
        )
        year, month, day, date_warnings = _jats_pub_date(root, record)
        volume, issue, page, article_number = _jats_bibliographic(root, record)
        license_text, license_url = _jats_license(root, record)
        related_retracted, related_corrected = _jats_publication_flags(root)
        if related_retracted:
            raise CitationSourceError(
                "CITATION_RETRACTED",
                409,
                "全文 XML 标记该论文已撤稿，拒绝生成引用建议。",
            )
        if related_corrected:
            raise CitationSourceError(
                "CITATION_CORRECTED",
                409,
                "全文 XML 标记该论文存在更正/勘误，拒绝基于未核实的版本生成引用建议。",
            )
        missing = [
            field
            for field, value in (
                ("title", title),
                ("authors", authors),
                ("year", year),
                ("container_title", journal),
            )
            if not value
        ]
        if missing:
            raise CitationSourceError(
                "CITATION_METADATA_INCOMPLETE",
                422,
                "全文 XML 缺少规范引文所需字段；未猜测补齐。",
                details={"missing_fields": missing},
            )
        warnings = [*author_warnings, *date_warnings]
        try:
            metadata = CitationMetadata(
                title=title,
                authors=authors,
                year=year,
                month=month,
                day=day,
                container_title=journal,
                volume=volume,
                issue=issue,
                page=page,
                article_number=article_number,
                doi=jats_doi or record_doi,
                pmid=ids.get("pmid") or _optional_str(record.get("pmid")),
                pmcid=ids.get("pmcid", pmcid),
                publisher=_jats_publisher(root),
                language=_optional_str(root.attrib.get(XML_LANG))
                or _optional_str(record.get("language")),
                license=license_text,
                license_url=license_url,
            )
        except ValidationError as exc:
            raise CitationSourceError(
                "CITATION_METADATA_INVALID",
                422,
                "全文 XML 题录字段无法形成合法引文。",
            ) from exc
        body = next((element for element in root.iter() if _local(element.tag) == "body"), None)
        if body is None:
            raise CitationSourceError(
                "CITATION_FULLTEXT_UNAVAILABLE", 422, "全文 XML 没有 body，无法提取真实段落。"
            )
        evidence = self._select_evidence(
            list(_body_paragraphs(body)),
            claim_text,
            is_review=_is_review(record),
            source_id=pmcid,
            source_url=source_url,
        )
        return metadata, evidence, warnings

    def _select_evidence(
        self,
        paragraphs: list[_Paragraph],
        claim_text: str,
        *,
        is_review: bool,
        source_id: str,
        source_url: str,
        source_level: Literal["fulltext_xml", "fulltext_html"] = "fulltext_xml",
        normalization_version: str = "jats-text-collapse-v1",
    ) -> list[CitationPassage]:
        if not paragraphs:
            raise CitationSourceError(
                "CITATION_INSUFFICIENT_EVIDENCE", 422, "全文没有可用的正文段落。"
            )
        claim_tokens = _tokens(claim_text)
        if not claim_tokens:
            raise CitationSourceError(
                "CITATION_INSUFFICIENT_EVIDENCE", 422, "请求没有可用于匹配全文段落的主题词。"
            )
        scored = []
        document_frequency: dict[str, int] = {}
        tokenized = []
        for paragraph in paragraphs:
            tokens = _tokens(paragraph.text)
            for token in set(tokens):
                document_frequency[token] = document_frequency.get(token, 0) + 1
            tokenized.append(tokens)
        total = len(paragraphs)
        average_length = sum(len(tokens) for tokens in tokenized) / max(total, 1)
        for paragraph, tokens in zip(paragraphs, tokenized, strict=True):
            score = _bm25(claim_tokens, tokens, document_frequency, total, average_length)
            if score <= 0:
                continue
            role = _evidence_role(paragraph.section_path, is_review)
            if role == "primary_result":
                score += 0.2
            scored.append((score, paragraph, role))
        scored.sort(key=lambda item: (-item[0], item[1].paragraph_index))
        selected = scored[: self._max_evidence]
        if not selected:
            raise CitationSourceError(
                "CITATION_INSUFFICIENT_EVIDENCE", 422, "未在全文中找到与请求主题相关的真实段落。"
            )
        evidence = []
        for score, paragraph, role in selected:
            quote, start, end = _quote_window(paragraph.text, claim_tokens, self._max_quote_chars)
            quote_sha256 = hashlib.sha256(quote.encode("utf-8")).hexdigest()
            evidence_id = stable_id(
                "ev_",
                source_id,
                paragraph.section_id or "",
                paragraph.paragraph_id or str(paragraph.paragraph_index),
                str(start),
                str(end),
                quote_sha256,
            )
            evidence.append(
                CitationPassage(
                    evidence_id=evidence_id,
                    quote=quote,
                    quote_sha256=quote_sha256,
                    paragraph_text=paragraph.text,
                    paragraph_sha256=hashlib.sha256(paragraph.text.encode("utf-8")).hexdigest(),
                    start_offset=start,
                    end_offset=end,
                    section_path=paragraph.section_path,
                    section_id=paragraph.section_id,
                    paragraph_id=paragraph.paragraph_id,
                    paragraph_index=paragraph.paragraph_index,
                    source_url=source_url,
                    source_level=source_level,
                    normalization_version=normalization_version,
                    evidence_role=role,
                    score=round(score, 4),
                )
            )
        return evidence


@dataclass(frozen=True, slots=True)
class _Paragraph:
    text: str
    section_path: list[str]
    section_id: str | None
    paragraph_id: str | None
    paragraph_index: int


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _query_value(value: str) -> str:
    return re.sub(r'["\\\r\n]', " ", value).strip()[:400]


def _first_author_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    first = value.split(",")[0].strip()
    return {
        token.strip(" .").casefold()
        for token in re.split(r"\s+", first)
        if len(token.strip(" .")) >= 2
    }


def _record_first_author_family(record: dict) -> str | None:
    authors = (record.get("authorList") or {}).get("author") or []
    if isinstance(authors, list) and authors:
        author = authors[0]
        if isinstance(author, dict):
            family = _optional_str(author.get("lastName"))
            if family:
                return family
            full_name = _optional_str(author.get("fullName"))
            if full_name:
                tokens = [token for token in full_name.split() if token.strip(" .")]
                return tokens[0].strip(" .") if tokens else None
    author_string = _optional_str(record.get("authorString"))
    if not author_string:
        return None
    tokens = [token for token in re.split(r"[\s,]+", author_string) if token.strip(" .")]
    return tokens[0].strip(" .") if tokens else None


def _record_authors(record: dict) -> list[CitationAuthor]:
    authors = (record.get("authorList") or {}).get("author") or []
    output = []
    if isinstance(authors, list):
        for author in authors:
            if not isinstance(author, dict):
                continue
            literal = _optional_str(author.get("collectiveName"))
            if literal:
                output.append(CitationAuthor(literal=literal))
                continue
            family = _optional_str(author.get("lastName"))
            given = _optional_str(author.get("firstName"))
            if family:
                output.append(CitationAuthor(family=family, given=given))
    return output


def _record_publication_flags(record: dict) -> tuple[bool, bool]:
    types = {
        str(value).casefold()
        for value in (record.get("pubTypeList") or {}).get("pubType", [])
        if value
    }
    retracted = str(record.get("retracted") or "").casefold() in {"y", "yes", "true"}
    corrected = bool(types & {"published erratum", "correction", "erratum"})
    links = (record.get("commentCorrectionList") or {}).get("commentCorrection", [])
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            kind = str(link.get("type") or "").casefold()
            if "retract" in kind:
                retracted = True
            if "erratum" in kind or "correct" in kind:
                corrected = True
    if types & {"retraction", "retracted publication", "retraction of publication"}:
        retracted = True
    return retracted, corrected


def _license_is_open(text: str) -> bool:
    if not text:
        return False
    if CLOSED_LICENSE.search(text):
        return False
    return bool(OPEN_LICENSE.search(text))


def _pmc_oai_request(pmcid: str) -> tuple[str, str]:
    identifier = f"oai:pubmedcentral.nih.gov:{pmcid.removeprefix('PMC')}"
    params = urlencode({"verb": "GetRecord", "identifier": identifier, "metadataPrefix": "pmc"})
    return f"https://{PMC_OAI_HOST}{PMC_OAI_PATH}?{params}", identifier


def _read_pmc_oai(
    transport: HttpTransport, pmcid: str, deadline: float
) -> tuple[ET.Element, bytes]:
    global _PMC_NEXT_REQUEST
    url, identifier = _pmc_oai_request(pmcid)
    if not _PMC_REQUEST_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise CitationSourceError("CITATION_SOURCE_TIMEOUT", 504, "等待 PMC 全文接口超时。")
    try:
        now = time.monotonic()
        wait = max(0.0, _PMC_NEXT_REQUEST - now)
        if now + wait >= deadline:
            raise CitationSourceError("CITATION_SOURCE_TIMEOUT", 504, "等待 PMC 全文接口超时。")
        if wait:
            time.sleep(wait)
        _PMC_NEXT_REQUEST = time.monotonic() + 0.35
        response = transport.get(
            url,
            accept="application/xml",
            timeout_s=max(0.001, deadline - time.monotonic()),
            max_bytes=MAX_FULLTEXT_BYTES,
        )
    finally:
        _PMC_REQUEST_LOCK.release()
    if response.status == 429:
        raise CitationSourceError("CITATION_SOURCE_RATE_LIMITED", 503, "PMC 全文接口限流。")
    if response.status != 200:
        raise CitationSourceError(
            "CITATION_SOURCE_UNAVAILABLE", 502, f"PMC 全文接口返回 HTTP {response.status}。"
        )
    return _load_article(response.body, oai_identifier=identifier), response.body


def _load_article(xml_bytes: bytes, *, oai_identifier: str | None = None) -> ET.Element:
    if len(xml_bytes) > MAX_FULLTEXT_BYTES:
        raise CitationSourceError("CITATION_SOURCE_RESPONSE_TOO_LARGE", 502, "全文超过大小上限。")
    upper = xml_bytes.upper()
    if b"\0" in xml_bytes or b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise CitationSourceError(
            "CITATION_SOURCE_UNSAFE_XML", 422, "全文 XML 含不支持的编码、DTD 或实体声明。"
        )
    try:
        root = ET.fromstring(xml_bytes)  # noqa: S314 - DTD/ENTITY rejected above
    except (ET.ParseError, ValueError, LookupError) as exc:
        raise CitationSourceError(
            "CITATION_SOURCE_INVALID_XML", 502, "官方全文 XML 无法解析。"
        ) from exc
    if oai_identifier is not None:
        if root.tag != OAI_NAMESPACE + "OAI-PMH":
            raise CitationSourceError("CITATION_SOURCE_INVALID_XML", 502, "无效的 PMC 回执。")
        if root.find(f"./{OAI_NAMESPACE}error") is not None:
            raise CitationSourceError("CITATION_FULLTEXT_UNAVAILABLE", 422, "PMC 不提供该篇全文。")
        record = root.find(f"./{OAI_NAMESPACE}GetRecord/{OAI_NAMESPACE}record")
        header = record.find(f"./{OAI_NAMESPACE}header") if record is not None else None
        if (
            header is None
            or header.get("status") == "deleted"
            or header.findtext(f"./{OAI_NAMESPACE}identifier") != oai_identifier
        ):
            raise CitationSourceError("CITATION_IDENTITY_MISMATCH", 422, "PMC 回执标识不一致。")
        root = record.find(f"./{OAI_NAMESPACE}metadata/{{*}}article")
    if root is None or _local(root.tag) != "article":
        raise CitationSourceError("CITATION_SOURCE_INVALID_XML", 502, "全文不是 JATS article。")
    return root


def _check_read_identity(
    root: ET.Element, pmcid: str, identity: CitationIdentity, record_title: str | None
) -> None:
    expected = {"pmcid": pmcid, "doi": identity.doi, "pmid": identity.pmid}
    seen: dict[str, set[str]] = {}
    for element in _article_meta(root):
        if _local(element.tag) != "article-id":
            continue
        kind = str(element.get("pub-id-type") or "").lower()
        value = _text(element)
        if kind in {"pmc", "pmcid"}:
            kind = "pmcid"
            value = f"PMC{value}" if value.isdigit() else value.upper()
        elif kind == "doi":
            value = normalize_doi(value) or value
        if kind in expected and value:
            seen.setdefault(kind, set()).add(value)
    same_identifier = any(expected[kind] in values for kind, values in seen.items())
    inconsistent = any(
        len(values) != 1 or (expected[kind] is not None and values != {expected[kind]})
        for kind, values in seen.items()
    )
    title = _normalize_title(_jats_title(root))
    if not same_identifier or inconsistent or not title or title != _normalize_title(record_title):
        raise CitationSourceError(
            "CITATION_IDENTITY_MISMATCH", 422, "全文 XML 与已打开题录的标识或标题不一致。"
        )


def _reading_blocks(root: ET.Element) -> tuple[TextBlock, ...]:
    body = root.find("./{*}body")
    if body is None:
        raise CitationSourceError("CITATION_FULLTEXT_UNAVAILABLE", 422, "全文 XML 无可读正文。")
    abstract = _article_meta(root).find("./{*}abstract")
    back = root.find("./{*}back")
    blocks = []
    body_has_text = False
    leaf_kinds = {
        "p": "paragraph",
        "title": "heading",
        "label": "label",
        "tr": "table_row",
        "ref": "reference",
        "code": "code",
        "preformat": "code",
        "def": "definition",
        "disp-formula": "formula",
    }
    for container, part in ((abstract, "Abstract"), (body, "Body"), (back, "Back matter")):
        if container is None:
            continue
        stack = [(container, (part,))]
        while stack:
            element, path = stack.pop()
            tag = _local(element.tag)
            if tag in {"graphic", "media", "supplementary-material", "sub-article"}:
                continue
            if tag == "alternatives":
                child = next((item for item in element if _local(item.tag) == "table"), None)
                if child is not None:
                    stack.append((child, path))
                continue
            if tag in {"sec", "ref-list", "ack", "app", "table-wrap", "fig"}:
                heading = element.find("./{*}title")
                if heading is None:
                    heading = element.find("./{*}label")
                section = _reading_text(heading) if heading is not None else ""
                path += (section[:240] or element.get("id", "")[:120] or tag,)
            if kind := leaf_kinds.get(tag):
                text = (
                    "\t".join(_reading_text(child) for child in element)
                    if kind == "table_row"
                    else _reading_text(element)
                ).strip()
                if text:
                    blocks.append(TextBlock(text, path, kind, element.get("id")))
                    body_has_text |= part == "Body" and kind not in {"heading", "label"}
                continue
            if len(path) > 40:
                raise CitationSourceError(
                    "CITATION_SOURCE_INVALID_XML", 422, "全文 XML 的章节嵌套超出读取范围。"
                )
            stack.extend((child, path) for child in reversed(element))
    if not body_has_text:
        raise CitationSourceError("CITATION_FULLTEXT_UNAVAILABLE", 422, "正文没有可读文本。")
    return tuple(blocks)


def _reading_text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _clean_text("".join(element.itertext()))


def _article_meta(root: ET.Element) -> ET.Element:
    for front in root:
        if _local(front.tag) != "front":
            continue
        for child in front:
            if _local(child.tag) == "article-meta":
                return child
    return root


def _journal_meta(root: ET.Element) -> ET.Element:
    for front in root:
        if _local(front.tag) != "front":
            continue
        for child in front:
            if _local(child.tag) == "journal-meta":
                return child
    return root


def _jats_ids(root: ET.Element) -> dict[str, str]:
    output: dict[str, str] = {}
    for element in _article_meta(root).iter():
        if _local(element.tag) != "article-id":
            continue
        kind = str(element.attrib.get("pub-id-type") or "").casefold()
        value = _text(element)
        if kind in {"doi", "pmid", "pmcid"} and value:
            output[kind] = value.upper() if kind == "pmcid" else value
    return output


def _jats_title(root: ET.Element) -> str:
    for element in _article_meta(root).iter():
        if _local(element.tag) == "article-title":
            text = _text(element)
            if text:
                return text
    return ""


def _contrib_authors(contrib_group: ET.Element) -> list[CitationAuthor]:
    output = []
    for contrib in contrib_group:
        if _local(contrib.tag) != "contrib":
            continue
        if str(contrib.attrib.get("contrib-type") or "author").casefold() != "author":
            continue
        for child in contrib:
            tag = _local(child.tag)
            if tag == "name":
                family = _text(child.find("./{*}surname"))
                given = _text(child.find("./{*}given-names"))
                if family:
                    output.append(CitationAuthor(family=family, given=given or None))
            elif tag == "collab":
                literal = _text(child)
                if literal:
                    output.append(CitationAuthor(literal=literal))
            elif tag == "string-name":
                name = _text(child)
                if name:
                    output.append(CitationAuthor(literal=name))
    return output


def _jats_authors(root: ET.Element, record: dict) -> tuple[list[CitationAuthor], list[str]]:
    authors = []
    for element in _article_meta(root).iter():
        if _local(element.tag) != "contrib-group":
            continue
        content_type = str(element.attrib.get("content-type") or "").casefold()
        if content_type and content_type != "author":
            continue
        authors.extend(_contrib_authors(element))
    warnings = []
    if not authors:
        authors = _record_authors(record)
        if authors:
            warnings.append("authors_from_europepmc_metadata")
    deduped = []
    seen = set()
    for author in authors:
        key = (author.family, author.given, author.literal)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(author)
    authors = deduped
    record_authors = _record_authors(record)
    if authors and record_authors:
        left = (authors[0].family or authors[0].literal or "").casefold()
        right = (record_authors[0].family or record_authors[0].literal or "").casefold()
        if left and right and left != right:
            raise CitationSourceError(
                "CITATION_METADATA_CONFLICT",
                422,
                "全文 XML 作者与 Europe PMC 题录作者冲突。",
                details={"fulltext_first_author": left, "metadata_first_author": right},
            )
    return authors, warnings


def _jats_journal(root: ET.Element) -> str:
    for element in _journal_meta(root).iter():
        if _local(element.tag) == "journal-title":
            text = _text(element)
            if text:
                return text
    return ""


@dataclass(frozen=True, slots=True)
class _PublicationDate:
    year: int
    month: int | None
    day: int | None
    source: str
    kind: str


def _month_number(value: object) -> int | None:
    number = _optional_int(value)
    if number is not None:
        return number if 1 <= number <= 12 else None
    text = _optional_str(value)
    if not text:
        return None
    normalized = text.casefold().rstrip(".")
    for month in range(1, 13):
        names = {calendar.month_name[month].casefold(), calendar.month_abbr[month].casefold()}
        if normalized in names or normalized[:3] == calendar.month_abbr[month].casefold():
            return month
    return None


def _publication_date(
    value: object,
    *,
    source: str,
    kind: str,
    month: object = None,
    day: object = None,
) -> _PublicationDate | None:
    text = _optional_str(value)
    if not text:
        return None
    date_match = re.fullmatch(r"(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?", text)
    if date_match:
        year = int(date_match.group(1))
        parsed_month = _optional_int(date_match.group(2))
        parsed_day = _optional_int(date_match.group(3))
    else:
        date_match = re.fullmatch(r"(\d{4})\s+([A-Za-z]+)(?:\s+(\d{1,2}))?", text)
        if date_match:
            year = int(date_match.group(1))
            parsed_month = _month_number(date_match.group(2))
            parsed_day = _optional_int(date_match.group(3))
        else:
            date_match = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", text)
            if not date_match:
                return None
            year = int(date_match.group(2))
            parsed_month = _month_number(date_match.group(1))
            parsed_day = None
    resolved_month = parsed_month if parsed_month is not None else _month_number(month)
    resolved_day = parsed_day if parsed_day is not None else _optional_int(day)
    if resolved_day is not None and not 1 <= resolved_day <= 31:
        return None
    return _PublicationDate(year, resolved_month, resolved_day, source, kind)


def _merge_publication_dates(
    dates: list[_PublicationDate], source_label: str
) -> _PublicationDate | None:
    if not dates:
        return None
    merged: dict[str, int | None] = {}
    for field in ("year", "month", "day"):
        values = {getattr(item, field) for item in dates if getattr(item, field) is not None}
        if len(values) > 1:
            raise CitationSourceError(
                "CITATION_METADATA_CONFLICT",
                422,
                f"{source_label}来源冲突，无法确定规范引文日期。",
                details={
                    "field": "publication_date",
                    "dates": [
                        {
                            "source": item.source,
                            "year": item.year,
                            "month": item.month,
                            "day": item.day,
                        }
                        for item in dates
                    ],
                },
            )
        merged[field] = next(iter(values), None)
    return _PublicationDate(
        year=int(merged["year"]),
        month=merged["month"],
        day=merged["day"],
        source=", ".join(item.source for item in dates),
        kind=dates[0].kind,
    )


def _publication_dates_differ(left: _PublicationDate, right: _PublicationDate) -> bool:
    return any(
        left_value is not None and right_value is not None and left_value != right_value
        for left_value, right_value in (
            (left.year, right.year),
            (left.month, right.month),
            (left.day, right.day),
        )
    )


def _jats_pub_date(
    root: ET.Element, record: dict
) -> tuple[int | None, int | None, int | None, list[str]]:
    jats_dates: dict[str, list[_PublicationDate]] = {
        "ppub": [],
        "collection": [],
        "online": [],
        "untyped": [],
    }
    for element in _article_meta(root).iter():
        if _local(element.tag) != "pub-date":
            continue
        raw_kind = (
            str(element.attrib.get("pub-type") or element.attrib.get("date-type") or "")
            .casefold()
            .replace("_", "-")
        )
        if raw_kind in {"ppub", "print", "print-publication"}:
            kind = "ppub"
        elif raw_kind == "collection":
            kind = "collection"
        elif raw_kind in {"epub", "electronic", "online", "ahead-of-print"}:
            kind = "online"
        elif raw_kind in {"", "pub", "publication"}:
            kind = "untyped"
        else:
            continue
        date = _publication_date(
            _text(element.find("./{*}year")),
            month=_text(element.find("./{*}month")),
            day=_text(element.find("./{*}day")),
            source=f"JATS {raw_kind or 'untyped'} pub-date",
            kind=kind,
        )
        if date is not None:
            jats_dates[kind].append(date)

    jats_print = _merge_publication_dates(jats_dates["ppub"], "JATS print publication date")
    jats_collection = _merge_publication_dates(
        jats_dates["collection"], "JATS collection publication date"
    )
    jats_online = _merge_publication_dates(jats_dates["online"], "JATS online publication date")
    jats_untyped = _merge_publication_dates(jats_dates["untyped"], "JATS publication date")

    journal_info = record.get("journalInfo") or {}
    if not isinstance(journal_info, dict):
        journal_info = {}
    api_formal_dates = []
    for field in ("printPublicationDate", "dateOfPublication"):
        date = _publication_date(
            journal_info.get(field) or record.get(field),
            source=f"Europe PMC {field}",
            kind="formal",
        )
        if date is not None:
            api_formal_dates.append(date)
    year_of_publication = journal_info.get("yearOfPublication") or record.get("yearOfPublication")
    month_of_publication = journal_info.get("monthOfPublication") or record.get(
        "monthOfPublication"
    )
    date = _publication_date(
        year_of_publication,
        month=month_of_publication,
        source="Europe PMC yearOfPublication",
        kind="formal",
    )
    if date is not None:
        api_formal_dates.append(date)
    api_formal = _merge_publication_dates(api_formal_dates, "Europe PMC formal publication date")

    api_year = _publication_date(
        record.get("pubYear"), source="Europe PMC pubYear", kind="api-year"
    )
    api_electronic_dates = []
    api_first_dates = []
    for source_record, prefix in ((record, "Europe PMC"), (journal_info, "Europe PMC journalInfo")):
        for field, dates in (
            ("electronicPublicationDate", api_electronic_dates),
            ("firstPublicationDate", api_first_dates),
        ):
            date = _publication_date(
                source_record.get(field), source=f"{prefix} {field}", kind="online"
            )
            if date is not None:
                dates.append(date)
    api_electronic = _merge_publication_dates(
        api_electronic_dates, "Europe PMC electronic publication date"
    )
    api_first = _merge_publication_dates(api_first_dates, "Europe PMC first publication date")
    online_candidates = [
        item for item in (jats_online, api_electronic, api_first) if item is not None
    ]
    online = next(iter(online_candidates), None)
    online_sources_differ = any(
        _publication_dates_differ(left, right)
        for index, left in enumerate(online_candidates)
        for right in online_candidates[index + 1 :]
    )

    formal = _merge_publication_dates(
        [item for item in (jats_print, api_formal) if item is not None],
        "JATS and Europe PMC formal publication date",
    )
    selected = formal or jats_collection or api_year or online or jats_untyped
    if selected is None:
        return None, None, None, []

    warnings = []
    if (
        online is not None
        and selected.kind != "online"
        and _publication_dates_differ(selected, online)
    ):
        warnings.append("online_publication_date_differs_from_issue_date")
    if online_sources_differ:
        warnings.append("online_publication_date_sources_differ")
    if (
        jats_untyped is not None
        and selected.kind != "untyped"
        and _publication_dates_differ(selected, jats_untyped)
    ):
        if online is None or _publication_dates_differ(jats_untyped, online):
            warnings.append("untyped_publication_date_differs_from_selected_date")
    if (
        jats_collection is not None
        and selected.kind != "collection"
        and _publication_dates_differ(selected, jats_collection)
    ):
        warnings.append("publication_date_source_disagreement")
    if api_year is not None and selected.kind != "api-year" and api_year.year != selected.year:
        if online is None or online.year != api_year.year:
            warnings.append("publication_year_conflict")
    if selected.kind == "online":
        warnings.append("publication_date_online_only")
    elif selected.kind == "untyped":
        warnings.append("publication_date_type_unreported")
    return selected.year, selected.month, selected.day, list(dict.fromkeys(warnings))


def _page_parts(value: str) -> tuple[str, str | None] | None:
    match = re.fullmatch(
        r"([A-Za-z]?\d+[A-Za-z]?)(?:\s*[-–—]\s*([A-Za-z]?\d+[A-Za-z]?))?",
        value.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    start = match.group(1).casefold()
    end = match.group(2).casefold() if match.group(2) else None
    return start, end


def _jats_bibliographic(
    root: ET.Element, record: dict
) -> tuple[str | None, str | None, str | None, str | None]:
    values: dict[str, str] = {}
    for element in _article_meta(root).iter():
        tag = _local(element.tag)
        if tag in {"volume", "issue", "fpage", "lpage", "elocation-id"} and tag not in values:
            value = _text(element)
            if value:
                values[tag] = value
    journal_info = record.get("journalInfo") or {}
    if not isinstance(journal_info, dict):
        journal_info = {}
    volume = values.get("volume") or _optional_str(journal_info.get("volume"))
    issue = values.get("issue") or _optional_str(journal_info.get("issue"))
    xml_page = None
    if values.get("fpage"):
        xml_page = values["fpage"]
        if values.get("lpage"):
            xml_page = f"{xml_page}-{values['lpage']}"
    api_page = _optional_str(record.get("pageInfo"))
    if xml_page is None:
        page = api_page
    elif api_page is None or xml_page == api_page:
        page = xml_page
    else:
        xml_parts = _page_parts(xml_page)
        api_parts = _page_parts(api_page)
        if xml_parts is None or api_parts is None:
            raise CitationSourceError(
                "CITATION_METADATA_CONFLICT",
                422,
                "全文 XML 与 Europe PMC 题录页码无法安全核对。",
                details={"field": "page", "fulltext_page": xml_page, "metadata_page": api_page},
            )
        xml_start, xml_end = xml_parts
        api_start, api_end = api_parts
        if xml_start != api_start or (xml_end and api_end and xml_end != api_end):
            raise CitationSourceError(
                "CITATION_METADATA_CONFLICT",
                422,
                "全文 XML 与 Europe PMC 题录页码冲突；未猜测选择。",
                details={"field": "page", "fulltext_page": xml_page, "metadata_page": api_page},
            )
        page = api_page if api_end and not xml_end else xml_page
    article_number = values.get("elocation-id")
    return volume, issue, page, article_number


def _jats_publisher(root: ET.Element) -> str | None:
    for element in _journal_meta(root).iter():
        if _local(element.tag) == "publisher-name":
            return _text(element) or None
    return None


def _jats_license(root: ET.Element, record: dict) -> tuple[str, str | None]:
    texts = []
    urls = []
    for element in _article_meta(root).iter():
        if _local(element.tag) != "license":
            continue
        text = _text(element)
        if text:
            texts.append(text)
        url = _optional_str(element.attrib.get(XLINK_HREF))
        if url:
            urls.append(url)
    metadata_license = str(record.get("license") or "")
    fulltext_license = " ".join(texts).strip()
    license_url = urls[0] if urls else None
    if fulltext_license and not _license_is_open(fulltext_license):
        if _license_is_open(metadata_license):
            raise CitationSourceError(
                "CITATION_LICENSE_CONFLICT",
                422,
                "全文 XML 许可与 Europe PMC 题录许可冲突。",
            )
        raise CitationSourceError(
            "CITATION_LICENSE_UNCLEAR",
            422,
            "全文 XML 未给出明确的开放许可。",
        )
    if not fulltext_license and not _license_is_open(metadata_license):
        raise CitationSourceError(
            "CITATION_LICENSE_UNCLEAR",
            422,
            "未找到明确的开放许可，拒绝生成引用建议。",
        )
    return fulltext_license or metadata_license, license_url


def _jats_publication_flags(root: ET.Element) -> tuple[bool, bool]:
    retracted = False
    corrected = False
    for element in root.iter():
        tag = _local(element.tag)
        if tag == "related-article":
            kind = str(element.attrib.get("related-article-type") or "").casefold()
            if "retract" in kind:
                retracted = True
            if "correct" in kind or "erratum" in kind:
                corrected = True
        if tag == "sub-article":
            kind = str(element.attrib.get("article-type") or "").casefold()
            if "retract" in kind:
                retracted = True
            if "correct" in kind or "erratum" in kind:
                corrected = True
    return retracted, corrected


def _body_paragraphs(body: ET.Element):
    counter = [0]

    def walk(element: ET.Element, path: list[str], section_id: str | None):
        for child in element:
            tag = _local(child.tag)
            if tag == "sec":
                title = ""
                for nested in child:
                    if _local(nested.tag) == "title":
                        title = _text(nested)
                        break
                if title and SKIPPED_SECTIONS.search(title):
                    continue
                nested_path = [*path, title or "Untitled section"]
                yield from walk(
                    child,
                    nested_path,
                    _optional_str(child.attrib.get("id")) or section_id,
                )
            elif tag == "p":
                text = _text(child)
                if not text or not MIN_PARAGRAPH_CHARS <= len(text) <= MAX_PARAGRAPH_CHARS:
                    continue
                counter[0] += 1
                paragraph_id = _optional_str(child.attrib.get("id")) or (
                    f"{section_id or 'body'}-p{counter[0]}"
                )
                yield _Paragraph(
                    text=text,
                    section_path=list(path) or ["Body"],
                    section_id=section_id,
                    paragraph_id=paragraph_id,
                    paragraph_index=counter[0],
                )
            elif tag in {"body", "abstract"}:
                yield from walk(child, path, section_id)

    yield from walk(body, [], None)


def _is_review(record: dict) -> bool:
    types = {
        str(value).casefold()
        for value in (record.get("pubTypeList") or {}).get("pubType", [])
        if value
    }
    return (
        bool(types & {"review", "review-article", "systematic review"})
        or "review" in str(record.get("title") or "").casefold()
    )


def _evidence_role(
    section_path: list[str],
    is_review: bool,
) -> Literal["primary_result", "method", "background", "review_secondary"]:
    if is_review:
        return "review_secondary"
    title = " ".join(section_path)
    if BACKGROUND_SECTIONS.search(title):
        return "background"
    if METHOD_SECTIONS.search(title):
        return "method"
    if RESULT_SECTIONS.search(title):
        return "primary_result"
    return "primary_result"


def _tokens(text: str) -> list[str]:
    lowered = text.casefold()
    words = [
        word for word in re.findall(r"[a-z0-9][a-z0-9+\-]{2,}", lowered) if word not in STOPWORDS
    ]
    cjk = re.findall(r"[\u3400-\u9fff]+", lowered)
    for sequence in cjk:
        words.extend(
            sequence[index : index + 2]
            for index in range(len(sequence) - 1)
            if sequence[index : index + 2] not in STOPWORDS
        )
    return words


def _bm25(
    query_tokens: list[str],
    document_tokens: list[str],
    document_frequency: dict[str, int],
    total_documents: int,
    average_length: float,
) -> float:
    if not query_tokens or not document_tokens:
        return 0.0
    frequencies: dict[str, int] = {}
    for token in document_tokens:
        frequencies[token] = frequencies.get(token, 0) + 1
    score = 0.0
    k1 = 1.5
    b = 0.75
    length = len(document_tokens)
    for token in dict.fromkeys(query_tokens):
        frequency = frequencies.get(token, 0)
        if not frequency:
            continue
        df = document_frequency.get(token, 0)
        idf = math.log((total_documents - df + 0.5) / (df + 0.5) + 1)
        score += (
            idf
            * frequency
            * (k1 + 1)
            / (frequency + k1 * (1 - b + b * length / max(average_length, 1)))
        )
    return score


def _quote_window(text: str, query_tokens: list[str], max_chars: int) -> tuple[str, int, int]:
    if len(text) <= max_chars:
        return text, 0, len(text)
    lowered = text.casefold()
    positions = [
        position for token in dict.fromkeys(query_tokens) if (position := lowered.find(token)) >= 0
    ]
    anchor = min(positions) if positions else 0
    start = max(0, min(anchor - max_chars // 3, len(text) - max_chars))
    end = min(len(text), start + max_chars)
    if start > 0:
        boundary = max((text.rfind(mark, 0, start + 1) for mark in ".!?。！？"), default=-1)
        if boundary >= start - 80:
            start = boundary + 1
    if end < len(text):
        candidates = [
            position for mark in ".!?。！？" if (position := text.find(mark, end - 1)) >= 0
        ]
        if candidates and min(candidates) <= end + 120:
            end = min(candidates) + 1
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return text[start:end], start, end


__all__ = [
    "CitationIdentity",
    "CitationPassage",
    "CitationSourceBundle",
    "CitationSourceError",
    "CitationSourceProvenance",
    "EuropePmcSourceResolver",
    "HttpResponse",
    "HttpsTransport",
]
