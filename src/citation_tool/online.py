"""Europe PMC live API adapter with a bounded native-relevance candidate pool."""

from __future__ import annotations

import html
import http.client
import json
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from .config import LibraryConfig
from .entities import ClaimContext, utc_now_iso
from .evidence import Candidate
from .jcs import sha256_json
from .metadata_retrieval import MetadataCorpusAdapter
from .publication import canonical_work_id, is_correction, normalize_doi
from .retrieval import ReadingBatch, ReadingRecord, SourceSearchError, reading_identifier

ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
MAX_RESPONSE_BYTES = 10_000_000
DOI_IN_QUERY = re.compile(r"10\.\d{4,9}/[^\s，。；]+", re.IGNORECASE)
SAFE_SOURCE = re.compile(r"[A-Za-z0-9]+")
SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"<[^>]+>", "", html.unescape(value)).strip()


def _publication_relations(item: Mapping[str, Any]) -> dict[str, list[str]]:
    relations: dict[str, list[str]] = {
        key: [] for key in ("corrects", "corrected_by", "retracted_by")
    }
    container = item.get("commentCorrectionList") or item.get("corrections") or {}
    links = container.get("commentCorrection", []) if isinstance(container, Mapping) else []
    if not isinstance(links, list):
        return relations
    for link in links:
        if not isinstance(link, Mapping):
            continue
        match = DOI_IN_QUERY.search(str(link.get("reference", "")))
        kind = str(link.get("type", "")).casefold()
        target = (
            "corrects"
            if kind in {"erratum for", "corrects"}
            else "corrected_by"
            if kind in {"erratum in", "corrected by"}
            else "retracted_by"
            if kind in {"retraction in", "retracted by"}
            else None
        )
        if target and match and (doi := normalize_doi(match.group())):
            relations[target].append(doi)
    return {key: sorted(set(values)) for key, values in relations.items()}


def normalize_online_record(item: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    title = _clean_text(item.get("title"))
    source = str(item.get("source") or ("MED" if item.get("pmid") else "")).upper()
    source_id = str(item.get("id") or item.get("pmid") or "").strip()
    doi = normalize_doi(item.get("doi"))
    if not title or not SAFE_SOURCE.fullmatch(source) or not SAFE_ID.fullmatch(source_id):
        return None
    work_id = canonical_work_id({"doi": doi, "work_id": f"work:europepmc:{source}:{source_id}"})
    year_value = item.get("pubYear")
    year = (
        int(year_value)
        if isinstance(year_value, int) or isinstance(year_value, str) and year_value.isdigit()
        else None
    )
    pub_types = (item.get("pubTypeList") or {}).get("pubType", [])
    if not isinstance(pub_types, list):
        pub_types = []
    relations = _publication_relations(item)
    retracted = str(item.get("retracted") or "").casefold() in {"y", "yes", "true"}
    checked = (
        retracted
        or any(relations.values())
        or is_correction({"pub_types": pub_types, "title": title})
    )
    return {
        "work_id": work_id,
        "paper_id": f"paper:europepmc:{source}:{source_id}",
        "library_id": "europepmc-online",
        "simulation": False,
        "evidence_level": "abstract_metadata_only",
        "title": title,
        "abstract": _clean_text(item.get("abstractText")),
        "year": year,
        "doi": doi,
        "authors": item.get("authorString") or None,
        "pub_types": [str(value) for value in pub_types],
        "pmid": item.get("pmid"),
        "pmcid": item.get("pmcid"),
        "source_metadata_url": f"https://europepmc.org/article/{source}/{quote(source_id)}",
        "metadata_found": True,
        "related_dois": relations,
        "retraction_status": (
            "retracted" if retracted else "corrected" if relations["corrected_by"] else "unknown"
        ),
        "status_verified_at": utc_now_iso() if checked else None,
        "status_source": "Europe PMC live core response" if checked else None,
    }


def native_query(claim: ClaimContext) -> str:
    if claim.input_type in {"abstract", "full_text"} and not claim.topic_context:
        text = re.sub(r"\s+", " ", claim.claim_text).strip()
        if not text:
            raise SourceSearchError("QUERY_EMPTY", "没有可用于在线检索的摘要。")
        if len(text) > 4_000:
            raise SourceSearchError(
                "QUERY_TOO_LONG", "在线 Abstract 查询超过 4000 字符，请改用明确的关键词或主题。"
            )
        return f"({text}) AND HAS_ABSTRACT:y"
    raw = (
        claim.query_terms or claim.topic_context or [claim.claim_text]
        if claim.input_type == "keywords"
        else claim.topic_context
    )
    phrases = []
    for value in raw:
        phrase = re.sub(r"\s+", " ", str(value).replace('"', " ")).strip()
        if phrase:
            phrases.append(phrase)
    if not phrases:
        raise SourceSearchError("QUERY_EMPTY", "没有可用于在线检索的关键词。")
    query = " AND ".join(f'TITLE_ABS:"{phrase}"' for phrase in phrases)
    doi_query = DOI_IN_QUERY.search(" ".join(phrases))
    if doi_query and (doi := normalize_doi(doi_query.group())):
        query = f'DOI:"{doi}"'
    return f"({query}) AND HAS_ABSTRACT:y"


class EuropePMCOnlineAdapter:
    def __init__(
        self,
        library: LibraryConfig,
        *,
        result_limit: int = 50,
        timeout_ms: int = 20_000,
        transport: Callable[[str, float], Mapping[str, Any]] | None = None,
    ) -> None:
        if not 5 <= result_limit <= 100:
            raise ValueError("online result_limit must be between 5 and 100")
        if not 1_000 <= timeout_ms <= 60_000:
            raise ValueError("online timeout_ms must be between 1000 and 60000")
        self.library = library
        self.adapter_id = library.adapter_id
        self.simulation = False
        self.display_name = library.display_name
        self.record_count = None
        self.index_snapshot = "europepmc-online-pending-response"
        self.model_versions = {
            "retrieval": "europepmc-native-relevance-then-local-bm25",
            "evidence_level": "abstract_metadata_only",
            "source_scope": "remote_live_database",
            "snapshot_semantics": "bounded_query_response_not_remote_index",
        }
        self._result_limit = result_limit
        self._timeout_s = timeout_ms / 1000
        self._transport = transport or self._fetch

    def _query_page(self, query: str, limit: int, cursor: str = "*") -> tuple[Mapping, list]:
        params = {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": limit,
            "cursorMark": cursor,
            "synonym": "false",
        }
        payload = self._transport(f"{ENDPOINT}?{urlencode(params)}", self._timeout_s)
        container = payload.get("resultList", {})
        results = container.get("result", []) if isinstance(container, Mapping) else None
        if not isinstance(results, list):
            raise SourceSearchError(
                "SOURCE_INVALID_RESPONSE", "Europe PMC 返回的结果列表格式不正确。"
            )
        return payload, results

    def _reading_page(self, query: str, limit: int, cursor: str = "*") -> ReadingBatch:
        payload, results = self._query_page(query, limit, cursor)
        source_items = []
        for item in results[:limit]:
            if (data := normalize_online_record(item)) is not None:
                # Exclude any locally generated observation time from version identity.
                snapshot = f"europepmc-record-{sha256_json(item)[:20]}"
                source_items.append(ReadingRecord(data, snapshot, dict(item)))
        if results and not source_items:
            raise SourceSearchError("SOURCE_INVALID_RESPONSE", "在线库未返回可辨识的论文记录。")
        next_cursor = payload.get("nextCursorMark")
        total = payload.get("hitCount")
        return ReadingBatch(
            tuple(source_items),
            next_position=next_cursor
            if results
            and isinstance(next_cursor, str)
            and 0 < len(next_cursor) <= 8_000
            and next_cursor != cursor
            else None,
            hit_count=total if isinstance(total, int) else None,
        )

    def discover_text(
        self, text: str, *, literal: bool, position: int | str | None, limit: int
    ) -> ReadingBatch:
        if literal:
            clean = " ".join(re.sub(r'["\\\\]', " ", text).split())
            terms = [clean] if clean else []
        else:
            terms = list(dict.fromkeys(re.findall(r"[\w-]+", text.casefold())))
        if not terms or len(terms) > 24:
            raise SourceSearchError("QUERY_INVALID", "在线库请使用最多 24 个聚焦词，或用短语查找。")
        query = " AND ".join(f'TITLE_ABS:"{term}"' for term in terms)
        return self._reading_page(query, limit, str(position or "*"))

    def lookup_text(self, identifier: str) -> ReadingBatch:
        key = reading_identifier(identifier)
        if key.startswith("work:10."):
            key = key.removeprefix("work:")
        if re.fullmatch(r"10\.\d{4,9}/[a-z0-9._;()/:+%-]+", key):
            query = f'DOI:"{key}"'
            field, expected = "doi", key
        elif re.fullmatch(r"pmc\d{4,12}", key):
            query, field, expected = f"PMCID:{key.upper()}", "pmcid", key
        elif match := re.fullmatch(r"(?:pmid:|paper:europepmc:)(\d{1,12})", key):
            query, field, expected = f"EXT_ID:{match[1]} AND SRC:MED", "pmid", match[1]
        elif match := re.fullmatch(r"(?:paper|work):europepmc:([a-z0-9]+):([a-z0-9._-]+)", key):
            query = f'EXT_ID:"{match[2]}" AND SRC:{match[1].upper()}'
            field, expected = "paper_id", f"paper:europepmc:{match[1]}:{match[2]}"
        else:
            raise SourceSearchError(
                "SOURCE_ID_INVALID", "在线直达仅接受 DOI、PMID、PMCID 或本库记录 ID。"
            )
        batch = self._reading_page(query, 12)
        return ReadingBatch(
            tuple(
                row for row in batch.items if str(row.data.get(field) or "").casefold() == expected
            ),
            # Unread upstream pages cannot rule out a second identity.
            next_position=batch.next_position if (batch.hit_count or 0) > 12 else None,
            hit_count=batch.hit_count if (batch.hit_count or 0) > 12 else None,
        )

    def search(self, claim: ClaimContext) -> list[Candidate]:
        query = native_query(claim)
        payload, results = self._query_page(query, self._result_limit)
        normalized = [record for item in results if (record := normalize_online_record(item))]
        if results and not normalized:
            raise SourceSearchError(
                "SOURCE_INVALID_RESPONSE", "Europe PMC 返回的文献均缺少可用的身份或题录字段。"
            )
        response_hash = sha256_json(
            {"api_version": payload.get("version"), "query": query, "results": results}
        )[:16]
        self.index_snapshot = f"europepmc-online-response-{response_hash}"
        hit_count = payload.get("hitCount")
        self.model_versions = {
            **self.model_versions,
            "api_version": str(payload.get("version") or "unknown"),
            "query_hash": sha256_json(query),
            "candidate_pool": f"native_relevance_top_{self._result_limit}",
            "source_hit_count": str(hit_count) if isinstance(hit_count, int) else "unknown",
            "normalized_records": str(len(normalized)),
        }
        if not normalized:
            return []
        local = MetadataCorpusAdapter(self.library, normalized, top_k=5)
        local.adapter_id = self.adapter_id
        local.index_snapshot = self.index_snapshot
        local.score_version = "europepmc-online-pool-bm25-ontology-v2"
        local.score_scale_group = f"europepmc-online-bm25:{response_hash}"
        return local.search(claim)

    @staticmethod
    def _fetch(url: str, timeout_s: float) -> Mapping[str, Any]:
        if urlparse(url).scheme != "https" or urlparse(url).hostname != "www.ebi.ac.uk":
            raise SourceSearchError("SOURCE_URL_INVALID", "在线文献库地址不在允许范围内。")
        parsed = urlparse(url)
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout_s)
        try:
            connection.request(
                "GET",
                f"{parsed.path}?{parsed.query}",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "citation-tool-research/0.2",
                },
            )
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
        except (OSError, http.client.HTTPException):
            raise SourceSearchError(
                "SOURCE_TIMEOUT", "Europe PMC 在线请求超时或网络不可达。"
            ) from None
        finally:
            connection.close()
        if status != 200:
            code = "SOURCE_RATE_LIMITED" if status == 429 else "SOURCE_UNAVAILABLE"
            message = (
                "Europe PMC 请求触发限流，请稍后重试。"
                if status == 429
                else f"Europe PMC 在线服务返回 HTTP {status}。"
            )
            raise SourceSearchError(code, message)
        if len(body) > MAX_RESPONSE_BYTES:
            raise SourceSearchError("SOURCE_RESPONSE_TOO_LARGE", "Europe PMC 响应超过大小上限。")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SourceSearchError(
                "SOURCE_INVALID_RESPONSE", "Europe PMC 未返回有效的 UTF-8 JSON。"
            ) from None
        if not isinstance(payload, Mapping):
            raise SourceSearchError("SOURCE_INVALID_RESPONSE", "Europe PMC 响应必须是 JSON 对象。")
        return payload
