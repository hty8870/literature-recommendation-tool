"""Bounded public HTTPS egress for user-supplied model endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from contextlib import aclosing
from urllib.parse import urlsplit

import httpx

MODEL_PROTOCOLS = {
    "chat_completions": ("OpenAI Chat Completions", "/chat/completions"),
    "responses": ("OpenAI Responses", "/responses"),
}
_PUBLIC_V6 = ipaddress.ip_network("2000::/3")
_BLOCKED_V6 = tuple(
    map(ipaddress.ip_network, ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20"))
)
_RESPONSE_LIMIT = 8 * 1024 * 1024
_REQUEST_LIMIT = 4 * 1024 * 1024


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        return address in _PUBLIC_V6 and not any(address in block for block in _BLOCKED_V6)
    return True


def normalize_model_base(value: str, protocol: str) -> str:
    message = "模型地址必须为公网 HTTPS（443 端口），不能包含凭据、查询参数或片段。"
    value = value.strip()
    if not value or len(value) > 512 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError(message)
    if any(c in value for c in ("\\", "@", "?", "#", "%")) or protocol not in MODEL_PROTOCOLS:
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        raise ValueError(message) from None
    if parsed.scheme != "https" or port not in (None, 443) or not host or host.endswith("."):
        raise ValueError(message)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (
            len(labels) < 2
            or len(host) > 253
            or labels[-1].isdigit()
            or labels[-1] in {"local", "localhost", "internal", "lan", "home"}
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
            )
        ):
            raise ValueError(message) from None
        netloc = host
    else:
        if not _public_address(host):
            raise ValueError(message)
        netloc = f"[{address}]" if address.version == 6 else str(address)
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"(?:/[A-Za-z0-9_~.-]+)*", path) or any(
        segment in {".", ".."} for segment in path.split("/")
    ):
        raise ValueError(message)
    for name, (_, suffix) in MODEL_PROTOCOLS.items():
        if path.endswith(suffix):
            if name != protocol:
                raise ValueError("模型地址末尾与所选 API 协议不一致。")
            path = path[: -len(suffix)]
            break
    return f"https://{netloc}{path}"


async def _resolve_public(host: str) -> list[str]:
    try:
        async with asyncio.timeout(5):
            rows = await asyncio.get_running_loop().getaddrinfo(
                host,
                443,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
    except (OSError, TimeoutError):
        raise httpx.ConnectError("Model endpoint DNS failed.") from None
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses or any(not _public_address(address) for address in addresses):
        raise httpx.ConnectError("Model endpoint must resolve only to public addresses.")
    return addresses


class AsyncOnlyModelTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "Custom model calls require the asynchronous transport.", request=request
        )


class PublicModelTransport(httpx.AsyncBaseTransport):
    def __init__(self, base: str, protocol: str, *, timeout_s: float, api_key: str) -> None:
        self.endpoint = httpx.URL(
            normalize_model_base(base, protocol) + MODEL_PROTOCOLS[protocol][1]
        )
        self._timeout = timeout_s
        self._secret = api_key.encode("ascii")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url != self.endpoint:
            raise httpx.ConnectError("Unexpected model endpoint.", request=request)
        body = await request.aread()
        if len(body) > _REQUEST_LIMIT:
            raise httpx.WriteError("Model request too large.", request=request)
        try:
            async with asyncio.timeout(self._timeout):
                addresses = await _resolve_public(request.url.host)
                for address in addresses[:3]:
                    try:
                        return await self._post_pinned(request, address, body)
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        continue
        except TimeoutError:
            raise httpx.ReadTimeout("Custom model request timed out.", request=request) from None
        raise httpx.ConnectError(
            "Could not establish verified TLS to model endpoint.", request=request
        )

    async def _post_pinned(
        self, request: httpx.Request, address: str, body: bytes
    ) -> httpx.Response:
        headers = httpx.Headers(
            {
                name: request.headers[name]
                for name in ("authorization", "content-type", "accept")
                if name in request.headers
            }
        )
        headers["host"] = request.url.netloc.decode("ascii")
        headers["accept-encoding"] = "identity"
        pinned = httpx.Request(
            "POST",
            request.url.copy_with(host=address),
            headers=headers,
            content=body,
            extensions={**request.extensions, "sni_hostname": request.url.host},
        )
        # httpcore connects to the numeric URL host and verifies TLS against sni_hostname.
        async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
            response = await transport.handle_async_request(pinned)
            response.request = pinned
            async with aclosing(response):
                if not 200 <= response.status_code < 300:
                    status = response.status_code if response.status_code >= 400 else 502
                    return httpx.Response(
                        status, json={"error": {"message": "自有模型端点拒绝了请求。"}}
                    )
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise httpx.ReadError("Unexpected compressed model response.", request=request)
                content = bytearray()
                async for chunk in response.aiter_raw():
                    if len(content) + len(chunk) > _RESPONSE_LIMIT:
                        raise httpx.ReadError("Model response too large.", request=request)
                    content.extend(chunk)
                if len(self._secret) >= 8 and self._secret in content:
                    raise httpx.ReadError("Model response contained a credential.", request=request)
                return httpx.Response(
                    response.status_code,
                    content=bytes(content),
                    headers={
                        "content-type": response.headers.get("content-type", "application/json")
                    },
                )
