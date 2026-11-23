"""Reviewed public metadata connectors. No model-supplied endpoints or executable adapters."""

from __future__ import annotations

import html
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import httpx

from .config import LibraryConfig
from .entities import ClaimContext, utc_now_iso
from .jcs import sha256_json
from .metadata_retrieval import MetadataCorpusAdapter
from .publication import canonical_work_id, normalize_doi
from .retrieval import ReadingBatch, ReadingRecord, SourceSearchError

MAX_BYTES = 1_500_000
USER_AGENT = "BioDataAgent-Literature/1.0 (+https://biodataagent.com/literature/)"
TITLES = {
    "explicit_request": "对话明确希望补充这个数据源。",
    "preprints": "现有选库不足以覆盖所需的学科预印本。",
    "repository_publications": "需要补查机构仓储中的公开论文。",
}


@dataclass(frozen=True, slots=True)
class Connector:
    library: LibraryConfig
    docs: str
    terms: str
    scope: str
    aliases: tuple[str, ...]
    reasons: tuple[str, ...]
    endpoint: str
    sample: str
    interval: float

    @property
    def connector_id(self) -> str:
        return self.library.library_id

    @property
    def version(self) -> str:
        return self.library.adapter_id

    def public(self) -> dict:
        return {
            "connector_id": self.connector_id,
            "version": self.version,
            "name": self.library.display_name,
            "docs": self.docs,
            "terms": self.terms,
            "scope": self.scope,
            "reasons": self.reasons,
        }


def _remote(identifier: str, name: str) -> LibraryConfig:
    return LibraryConfig(identifier, name, "", 0, f"{identifier}-metadata-v1", False, "remote_api")


CONNECTORS = {
    row.connector_id: row
    for row in (
        Connector(
            _remote("arxiv-online", "arXiv 在线"),
            "https://info.arxiv.org/help/api/user-manual.html",
            "https://info.arxiv.org/help/api/tou.html",
            "数学、物理、计算机等领域的预印本；使用 CC0 描述性题录与摘要，不获取 PDF。",
            ("arxiv", "arxiv.org"),
            ("explicit_request", "preprints"),
            "https://export.arxiv.org/api/query",
            "machine learning",
            3.0,
        ),
        Connector(
            _remote("zenodo-publications", "Zenodo 论文"),
            "https://developers.zenodo.org/",
            "https://about.zenodo.org/policies/",
            "公开论文类别的 CC0 题录与描述；不查数据文件、不保证同行评审。",
            ("zenodo", "zenodo.org"),
            ("explicit_request", "repository_publications"),
            "https://zenodo.org/api/records",
            "climate",
            1.0,
        ),
    )
}


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _html_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _text(html.unescape(re.sub(r"<[^<>]*>", " ", value)), limit)


def _year(value: str) -> int | None:
    return int(value[:4]) if re.match(r"(?:19|20)\d\d(?:-|$)", value) else None


def _record(connector, identifier, title, abstract, year, doi, authors, url, pub_type):
    if not title or not abstract:
        return None
    return {
        "work_id": canonical_work_id(
            {"work_id": f"work:{connector.connector_id}:{identifier}", "doi": doi}
        ),
        "paper_id": f"paper:{connector.connector_id}:{identifier}",
        "library_id": connector.connector_id,
        "title": title,
        "abstract": abstract,
        "year": year,
        "doi": doi,
        "authors": authors or None,
        "source_metadata_url": url,
        "simulation": False,
        "evidence_level": "abstract_metadata_only",
        "section_label": "Abstract"
        if connector.connector_id == "arxiv-online"
        else "Record description",
        "metadata_parser": connector.version,
        "pub_types": [pub_type],
        "retraction_status": "unknown",
    }


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")


def _arxiv(connector: Connector, body: bytes, limit: int) -> list[dict]:
    xml = body.decode("utf-8")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("unexpected XML declaration")
    root = ET.fromstring(xml)  # noqa: S314 - bounded UTF-8 with DTD/entities rejected above
    if root.tag != f"{_ATOM}feed":
        raise ValueError("expected arXiv Atom feed")

    def value(entry: ET.Element, field: str) -> str:
        return (entry.findtext(f"{_ATOM}{field}") or "").strip()

    records = []
    for item in root.findall(f"{_ATOM}entry")[:limit]:
        identifier = re.sub(r"^https?://arxiv\.org/abs/", "", value(item, "id"))
        if not _ARXIV_ID.fullmatch(identifier):
            continue
        # The preprint identity is not the same as a linked published DOI.
        record = _record(
            connector,
            identifier,
            _text(value(item, "title"), 500),
            _text(value(item, "summary"), 9_000),
            _year(value(item, "published")),
            None,
            ", ".join(
                _text(name.text, 100) for name in item.findall(f"{_ATOM}author/{_ATOM}name")[:30]
            ),
            f"https://arxiv.org/abs/{identifier}",
            "preprint",
        )
        if record:
            records.append(record)
    return records


def _zenodo(connector: Connector, body: bytes, limit: int) -> list[dict]:
    payload = json.loads(body)
    rows = payload["hits"]["hits"]
    if not isinstance(rows, list):
        raise ValueError("expected Zenodo hits array")
    records = []
    for item in rows[:limit]:
        if not isinstance(item, Mapping):
            continue
        identifier = str(item.get("id", ""))
        meta = item.get("metadata")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", identifier) or not isinstance(meta, Mapping):
            continue
        resource_type = meta.get("resource_type")
        if not isinstance(resource_type, Mapping) or resource_type.get("type") != "publication":
            continue
        if meta.get("access_right") != "open":
            continue
        subtype = resource_type.get("subtype")
        if subtype not in {"article", "preprint", "report", "conferencepaper", "workingpaper"}:
            continue
        creators = meta.get("creators")
        authors = ", ".join(
            _text(_EMAIL.sub("", str(person.get("name") or "")), 100)
            for person in (creators[:30] if isinstance(creators, list) else [])
            if isinstance(person, Mapping)
        )
        title = _EMAIL.sub("[已省略邮箱]", _html_text(meta.get("title"), 500))
        abstract = _EMAIL.sub("[已省略邮箱]", _html_text(meta.get("description"), 9_000))
        record = _record(
            connector,
            identifier,
            title,
            abstract,
            _year(_text(meta.get("publication_date"), 12)),
            normalize_doi(meta.get("doi")),
            authors,
            f"https://zenodo.org/records/{identifier}",
            str(subtype),
        )
        if record:
            records.append(record)
    return records


def _claim_terms(claim: ClaimContext) -> str:
    terms = (
        (claim.query_terms or claim.topic_context or [claim.claim_text])
        if (claim.input_type == "keywords")
        else claim.topic_context
    )
    text = " ".join(str(item) for item in terms[:8])
    if len(text) > 480 or not text.strip():
        raise SourceSearchError("SOURCE_FOCUSED_QUERY_REQUIRED", "请提供公开、聚焦的主题关键词。")
    return text


class ProjectMetadataAdapter:
    def __init__(
        self, connector: Connector, *, transport: Callable[[str], bytes] | None = None
    ) -> None:
        self.connector = connector
        self.library = connector.library
        self.adapter_id = connector.version
        self.display_name = self.library.display_name
        self.simulation = False
        self.record_count = None
        self.index_snapshot = f"{connector.connector_id}-pending"
        self.model_versions = {
            "retrieval": connector.version,
            "evidence_level": "abstract_metadata_only",
        }
        self._transport = transport or self._fetch
        self._gate = threading.Lock()
        self._next_at = 0.0

    @staticmethod
    def _fetch(url: str) -> bytes:
        with (
            httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=10,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("unsupported response encoding")
            data = bytearray()
            for chunk in response.iter_raw():
                if len(data) + len(chunk) > MAX_BYTES:
                    raise ValueError("source response too large")
                data.extend(chunk)
            return bytes(data)

    def _page(self, text: str, limit: int, offset: int = 0, *, identifier: str | None = None):
        arxiv = self.connector.connector_id == "arxiv-online"
        # Quote only simple words so the input cannot supply upstream query operators.
        words = " ".join(re.findall(r"[^\W_]+(?:-[^\W_]+)*", text)[:24])
        if not (words or identifier):
            raise SourceSearchError("QUERY_EMPTY", "请提供可查询的关键词。")
        url = self.connector.endpoint
        if arxiv:
            params = {"max_results": limit, "start": offset, "sortBy": "relevance"}
            if identifier:
                params["id_list"] = identifier
            else:
                params["search_query"] = " AND ".join(f'all:"{word}"' for word in words.split())
        else:
            limit = min(limit, 25)
            query = f"access_right:open AND ({words})"
            if identifier:
                query = f'access_right:open AND doi:"{identifier}"'
            params = {
                "q": query,
                "size": limit,
                "page": offset // limit + 1,
                "sort": "bestmatch",
                "type": "publication",
                "subtype": "article",
            }
        try:
            if not self._gate.acquire(timeout=12):
                raise SourceSearchError("SOURCE_RATE_LIMIT", "在线库正忙，请稍后重试。")
            try:
                now = time.monotonic()
                delay = self._next_at - now
                if delay > 0:
                    time.sleep(delay)
                self._next_at = time.monotonic() + self.connector.interval
                raw = self._transport(f"{url}?{urlencode(params)}")
            finally:
                self._gate.release()
            parser = _arxiv if arxiv else _zenodo
            return parser(self.connector, raw, limit)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise SourceSearchError("SOURCE_RATE_LIMIT", "上游限流，请稍后重试。") from None
            raise SourceSearchError("SOURCE_HTTP_ERROR", "上游暂未返回可用结果。") from None
        except httpx.HTTPError:
            raise SourceSearchError("SOURCE_UNAVAILABLE", "暂时无法连接上游。") from None
        except (ValueError, TypeError, KeyError, ET.ParseError):
            raise SourceSearchError("SOURCE_INVALID_RESPONSE", "无法验证上游题录格式。") from None

    def probe(self) -> dict:
        records = self._page(self.connector.sample, 8)
        if not records:
            raise SourceSearchError("CONNECTOR_EMPTY_PROBE", "公开样例未返回可用的论文题录。")
        return {
            "version": self.connector.version,
            "records": len(records),
            "sample_hash": sha256_json([row["paper_id"] for row in records]),
            "checked_at": utc_now_iso(),
        }

    def search(self, claim: ClaimContext):
        records = self._page(_claim_terms(claim), 30)
        self.index_snapshot = f"{self.connector.connector_id}-{sha256_json(records)[:20]}"
        if not records:
            return []
        local = MetadataCorpusAdapter(self.library, records, top_k=10)
        local.index_snapshot = self.index_snapshot
        local.score_scale_group = self.index_snapshot
        return local.search(claim)

    @staticmethod
    def _reading(records, *, next_position=None) -> ReadingBatch:
        return ReadingBatch(
            tuple(ReadingRecord(record, sha256_json(record)[:24]) for record in records),
            next_position,
        )

    def discover_text(self, text, *, literal, position, limit):
        position = int(position or 0)
        records = self._page(text[:480], limit, position)
        return self._reading(
            records, next_position=position + limit if len(records) == limit else None
        )

    def lookup_text(self, identifier):
        key = identifier.removeprefix(f"paper:{self.connector.connector_id}:").removeprefix(
            f"work:{self.connector.connector_id}:"
        )
        if self.connector.connector_id == "arxiv-online":
            key = key.removeprefix("arxiv:")
            if _ARXIV_ID.fullmatch(key):
                rows = self._page("", 8, identifier=key)
                return self._reading([row for row in rows if row["paper_id"].endswith(f":{key}")])
        elif (doi := normalize_doi(key)) and re.fullmatch(r"10\.\d{4,9}/[a-z0-9._/-]+", doi):
            rows = self._page("", 8, identifier=doi)
            return self._reading([row for row in rows if row["doi"] == doi])
        return ReadingBatch(())
