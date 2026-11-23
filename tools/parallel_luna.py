"""One observed HTTP admission pool for native Luna development benchmarks."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr
from tools.luna_credentials import MODEL

from citation_tool.entities import utc_now_iso
from citation_tool.reasons import DeepSeekModelGateway, validate_openai_proxy

MAX_INFLIGHT = 8
SUBJECT = ContextVar("luna_benchmark_subject", default="")


class ApiAdmissionStopped(RuntimeError):
    pass


def _retry_wait(value: str | None) -> float:
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return 5.0


class ObservedLunaClient(httpx.AsyncClient):
    def __init__(self, ceiling: int = MAX_INFLIGHT, **kwargs) -> None:
        if not 1 <= ceiling <= MAX_INFLIGHT:
            raise ValueError("unsupported benchmark concurrency")
        super().__init__(
            timeout=300,
            limits=httpx.Limits(max_connections=ceiling),
            proxy=validate_openai_proxy(os.environ.get("CITATION_OPENAI_PROXY")),
            **kwargs,
        )
        self.ceiling = ceiling
        self.limit = min(4, ceiling)
        self.active = 0
        self.peak = 0
        self.not_before = 0.0
        self.blocked: str | None = None
        self.rows: list[dict] = []
        self.transitions: list[dict] = []
        self._condition = asyncio.Condition()
        self._upstream_streak = 0
        self.started_at = time.monotonic()

    async def _acquire(self) -> None:
        async with self._condition:
            while True:
                if self.blocked:
                    raise ApiAdmissionStopped(self.blocked)
                delay = self.not_before - time.monotonic()
                if self.active < self.limit and delay <= 0:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    return
                try:
                    async with asyncio.timeout(delay if delay > 0 else None):
                        await self._condition.wait()
                except TimeoutError:
                    continue

    async def expand_after_pilot(self) -> None:
        async with self._condition:
            ok = sum(200 <= (row["status"] or 0) < 300 for row in self.rows)
            unstable = sum(
                (row.get("status") or 0) >= 500 or bool(row.get("error")) for row in self.rows
            )
            if (
                not self.blocked
                and ok >= self.limit
                and not unstable
                and not self.transitions
                and self.peak >= min(2, self.limit)
            ):
                self._resize(self.ceiling, "pilot_success")
                self._condition.notify_all()

    def _resize(self, limit: int, reason: str) -> None:
        if limit != self.limit:
            self.transitions.append(
                {"at": utc_now_iso(), "from": self.limit, "to": limit, "reason": reason}
            )
            self.limit = limit

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        if (
            request.method != "POST"
            or request.url.scheme != "https"
            or request.url.host != "api.openai.com"
            or request.url.path != "/v1/responses"
            or request.url.port not in (None, 443)
            or kwargs.get("stream")
        ):
            raise ValueError("benchmark transport only accepts non-streaming official Responses")
        await self._acquire()
        row = {
            "http_id": uuid.uuid4().hex,
            "subject": SUBJECT.get(),
            "at": utc_now_iso(),
            "start_s": time.monotonic() - self.started_at,
            "status": None,
            "error": None,
            "inflight_on_start": self.active,
        }
        response = None
        try:
            response = await super().send(request, **kwargs)
            row["status"] = response.status_code
            for key in ("x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens"):
                value = response.headers.get(key, "")
                if value.isdecimal() and len(value) < 16:
                    row[key] = int(value)
            return response
        except BaseException as exc:
            row["error"] = type(exc).__name__
            raise
        finally:
            row["end_s"] = time.monotonic() - self.started_at
            async with self._condition:
                self.rows.append(row)
                self.active -= 1
                self._feedback(response, row)
                self._condition.notify_all()

    def _feedback(self, response: httpx.Response | None, row: dict) -> None:
        status = row["status"]
        now = time.monotonic()
        if status in {401, 402, 403}:
            self.blocked = f"HTTP_{status}"
        elif status == 429 and response is not None:
            wait = _retry_wait(response.headers.get("retry-after"))
            try:
                exhausted = (response.json().get("error") or {}).get("code") == "insufficient_quota"
            except (ValueError, AttributeError):
                exhausted = False
            if exhausted or wait > 120:
                self.blocked = "HTTP_429_QUOTA_OR_LONG_COOLDOWN"
            else:
                self._resize(max(1, self.limit // 2), "HTTP_429")
                self.not_before = max(self.not_before, now + wait)
        if status in {500, 502, 503, 504, 507} or row["error"] in {"ConnectError", "ReadTimeout"}:
            self._upstream_streak += 1
            if self._upstream_streak >= 2:
                self._resize(max(1, self.limit // 2), "upstream_errors")
                self.not_before = max(self.not_before, now + 2.0)
                self._upstream_streak = 0
        else:
            self._upstream_streak = 0


class CancelTrackedChatOpenAI(ChatOpenAI):
    _close_cancelled = PrivateAttr(default=None)

    async def agenerate(self, messages, *args, run_id=None, **kwargs):
        if self._close_cancelled is None:
            return await super().agenerate(messages, *args, run_id=run_id, **kwargs)
        if len(messages) != 1:
            raise ValueError("the observed Luna transport audits one prompt per dispatch")
        invocation = run_id or uuid.uuid4()
        try:
            return await super().agenerate(messages, *args, run_id=invocation, **kwargs)
        except asyncio.CancelledError as exc:
            await self._close_cancelled(invocation, exc)
            raise


class ParallelLunaGateway(DeepSeekModelGateway):
    def __init__(self, config, client: ObservedLunaClient, audit) -> None:
        config.validate()
        if (config.provider, config.model, config.base_url, config.api_type) != (
            "openai",
            MODEL,
            "https://api.openai.com/v1",
            "responses",
        ):
            raise ValueError("expected authorized Luna API")
        self.client = client
        self.audit = audit
        super().__init__(config, max_inflight=2, timeout_ms=300_000)

    def _client(self, *, json_mode: bool = False, max_tokens: int = 2000) -> ChatOpenAI:
        model = CancelTrackedChatOpenAI(
            model=self.config.model,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self._timeout_s,
            max_retries=0,
            max_completion_tokens=max_tokens + 8192,
            use_responses_api=True,
            http_async_client=self.client,
            output_version="responses/v1",
            store=False,
            include=["reasoning.encrypted_content"],
            callbacks=[self.audit],
            model_kwargs={"response_format": {"type": "json_object"}} if json_mode else {},
        )
        model._close_cancelled = getattr(self.audit, "close_cancelled_invocation", None)
        return model
