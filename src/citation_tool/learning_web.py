"""DeepSeek Search and Fetch, with explicitly bounded public-only inputs."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

TOPICS = {
    "computing": "computer science searchable scholarly repository metadata API preprints",
    "physical_sciences": "physics mathematics searchable preprint repository metadata API",
    "life_sciences": "life sciences scholarly repository public metadata search API",
    "earth_sciences": "earth climate science scholarly repository publication metadata search API",
    "cross_disciplinary": "multidisciplinary public repository publications search metadata API",
}
DEEPSEEK_BASE = "https://api.deepseek.com/v1/tools/"
MAX_RESPONSE = 800_000
_KEY = re.compile(r"[\x21-\x7e]{8,512}")


class WebLookupError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def search_key() -> str | None:
    value = os.environ.get("CITATION_LEARNING_SEARCH_KEY", "").strip()
    if not value and (directory := os.environ.get("CREDENTIALS_DIRECTORY")):
        path = Path(directory) / "citation_learning_search_key"
        if path.is_file() and path.stat().st_size <= 1024:
            value = path.read_text(encoding="utf-8").strip()
    return value if _KEY.fullmatch(value) else None


def doc_url(value: object, hosts: frozenset[str]) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"https://[A-Za-z0-9./_~-]{1,420}", value):
        return None
    parsed = urlsplit(value)
    if parsed.netloc in hosts and not set(parsed.path.split("/")) & {".", ".."}:
        return value
    return None


class DeepSeekWebSearch:
    def __init__(self, key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not _KEY.fullmatch(key):
            raise ValueError("Invalid discovery search credential shape")
        self._key = key
        self._transport = transport

    async def _request(self, name: str, body: dict) -> dict:
        try:
            async with (
                asyncio.timeout(18),
                httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=15,
                    transport=self._transport,
                    headers={"Authorization": f"Bearer {self._key}", "Accept-Encoding": "identity"},
                ) as client,
                client.stream("POST", DEEPSEEK_BASE + name, json=body) as response,
            ):
                if not 200 <= response.status_code < 300:
                    code = {
                        401: "WEB_SEARCH_AUTH_FAILED",
                        403: "WEB_SEARCH_AUTH_FAILED",
                        402: "WEB_SEARCH_BALANCE",
                        429: "WEB_SEARCH_RATE_LIMIT",
                    }.get(response.status_code, "WEB_SEARCH_UPSTREAM_ERROR")
                    raise WebLookupError(code)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise WebLookupError("WEB_SEARCH_BAD_RESPONSE")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESPONSE:
                        raise WebLookupError("WEB_SEARCH_BAD_RESPONSE")
                    data.extend(chunk)
                if self._key.encode("ascii") in data:
                    raise WebLookupError("WEB_SEARCH_BAD_RESPONSE")
                payload = json.loads(data)
                if not isinstance(payload, dict):
                    raise WebLookupError("WEB_SEARCH_BAD_RESPONSE")
                return payload
        except (httpx.HTTPError, TimeoutError):
            raise WebLookupError("WEB_SEARCH_UNAVAILABLE") from None
        except (UnicodeError, json.JSONDecodeError):
            raise WebLookupError("WEB_SEARCH_BAD_RESPONSE") from None

    async def search(self, query: str) -> list[dict]:
        payload = await self._request("search", {"query": query, "count": 6, "need_content": False})
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise WebLookupError("WEB_SEARCH_BAD_RESPONSE")
        return [
            {
                field: str(row.get(field) or "")[:limit]
                for field, limit in (
                    ("title", 200),
                    ("url", 420),
                    ("snippet", 500),
                )
            }
            for row in rows[:6]
            if isinstance(row, dict)
        ]

    async def fetch(self, url: str) -> dict:
        payload = await self._request("fetch", {"url": url})
        text = payload.get("content")
        if not isinstance(text, str) or not text.strip():
            raise WebLookupError("WEB_FETCH_EMPTY")
        return {"title": str(payload.get("title") or "")[:160], "content": text[:8_000]}
