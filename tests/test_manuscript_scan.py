from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import citation_tool.manuscript_scan as scan_module
from citation_tool.api import create_app
from citation_tool.config import Settings
from citation_tool.manuscript_api import build_manuscript_router, manuscript_error_handler
from citation_tool.manuscript_scan import (
    ClamAVScanner,
    ManuscriptScanError,
    ScanEngine,
    ScanReceipt,
    scan_capability,
)
from citation_tool.manuscripts import ManuscriptError, ManuscriptLimits
from citation_tool.runtime import settings_from_env

PUBLIC_PDF = b"%PDF-1.7\npublic generated test input\n%%EOF"
OWNER = "scan-owner"


def engine() -> ScanEngine:
    return ScanEngine("1.4.3", "123456", datetime.now(UTC).strftime("%a %b %d %H:%M:%S %Y"))


def version(*, offset_days: int = 0, number: str = "123456") -> bytes:
    date = datetime.now(UTC) + timedelta(days=offset_days)
    return f"ClamAV 1.4.3/{number}/{date:%a %b %d %H:%M:%S %Y}\0".encode()


class FakeSocket:
    def __init__(self, reply):
        self.reply = reply
        self.sent = bytearray()
        self.closed = False
        self.connected = None

    def settimeout(self, value):
        assert value > 0

    def connect(self, path):
        self.connected = path

    def sendall(self, data):
        self.sent.extend(data)

    def recv(self, size):
        if isinstance(self.reply, BaseException):
            raise self.reply
        part, self.reply = self.reply[:size], self.reply[size:]
        return part

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def socket_replies(monkeypatch, replies):
    queued = deque(replies)
    created = []

    def make(*args):
        assert args == (scan_module.socket.AF_UNIX, scan_module.socket.SOCK_STREAM)
        connection = FakeSocket(queued.popleft())
        created.append(connection)
        return connection

    monkeypatch.setattr(
        scan_module, "socket", SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, socket=make)
    )
    return created


def test_clamav_streams_original_bytes_and_versions_both_sides_of_scan(monkeypatch):
    v = version()
    connections = socket_replies(monkeypatch, [b"PONG\0", v, b"stream: OK\0", b"PONG\0", v])
    scanner = ClamAVScanner("/run/citation-av/clamd.sock")
    receipt = scanner.scan([b"abc", b"", b"\x00\xffdef"], max_bytes=20)
    assert receipt.input_sha256 == hashlib.sha256(b"abc\x00\xffdef").hexdigest()
    assert receipt.size_bytes == 8
    assert receipt.engine.definitions_version == "123456"
    assert receipt.as_dict()["status"] == "clean"
    assert connections[2].sent == (
        b"zINSTREAM\0"
        + b"\x00\x00\x00\x03abc"
        + b"\x00\x00\x00\x05\x00\xffdef"
        + b"\x00\x00\x00\x00"
    )
    assert all(connection.closed for connection in connections)
    assert all(connection.connected == "/run/citation-av/clamd.sock" for connection in connections)


@pytest.mark.parametrize(
    ("reply", "code", "status"),
    [
        (b"stream: Win.Test.EICAR_HDB-1 FOUND\0", "MANUSCRIPT_MALWARE_DETECTED", 422),
        (b"stream: Malware.Limitless FOUND\0", "MANUSCRIPT_MALWARE_DETECTED", 422),
        (b"INSTREAM size limit exceeded. ERROR\0", "MANUSCRIPT_SCAN_LIMIT", 413),
        (b"stream: Heuristics.Limits.Exceeded.MaxScanSize FOUND\0", "MANUSCRIPT_SCAN_LIMIT", 413),
        (b"stream: scanner error ERROR\0", "MANUSCRIPT_SCAN_FAILED", 503),
        (b"stream: OK but incomplete\0", "MANUSCRIPT_SCAN_INVALID_RESPONSE", 503),
        (b"secret-upload-body\0", "MANUSCRIPT_SCAN_INVALID_RESPONSE", 503),
        (b"stream: OK", "MANUSCRIPT_SCAN_INVALID_RESPONSE", 503),
        (b"x" * 2048 + b"\0", "MANUSCRIPT_SCAN_INVALID_RESPONSE", 503),
        (TimeoutError(), "MANUSCRIPT_SCAN_TIMEOUT", 503),
        (OSError("secret-path"), "MANUSCRIPT_SCAN_UNAVAILABLE", 503),
    ],
)
def test_scan_fails_closed_for_every_non_clean_reply(monkeypatch, reply, code, status):
    connections = socket_replies(monkeypatch, [b"PONG\0", version(), reply])
    with pytest.raises(ManuscriptScanError) as caught:
        ClamAVScanner("/run/citation-av/clamd.sock").scan([PUBLIC_PDF], max_bytes=1000)
    assert caught.value.code == code
    assert caught.value.status == status
    assert "secret" not in str(caught.value)
    assert all(connection.closed for connection in connections)


@pytest.mark.parametrize("offset_days", [-8, 2])
def test_stale_or_implausibly_future_definitions_are_not_available(monkeypatch, offset_days):
    socket_replies(monkeypatch, [b"PONG\0", version(offset_days=offset_days)])
    with pytest.raises(ManuscriptScanError, match="病毒库") as caught:
        ClamAVScanner("/run/citation-av/clamd.sock").probe()
    assert caught.value.code == "MANUSCRIPT_SCAN_STALE"


@pytest.mark.parametrize("reply", [b"ClamAV 1.4.3\0", b"ClamAV 1.4.3/123/not-a-date\0"])
def test_probe_without_confirmable_loaded_definitions_is_unavailable(monkeypatch, reply):
    socket_replies(monkeypatch, [b"PONG\0", reply])
    with pytest.raises(ManuscriptScanError) as caught:
        ClamAVScanner("/run/citation-av/clamd.sock").probe()
    assert caught.value.code == "MANUSCRIPT_SCAN_INVALID_RESPONSE"


def test_definition_changes_during_scan_require_a_retry(monkeypatch):
    socket_replies(
        monkeypatch,
        [b"PONG\0", version(), b"stream: OK\0", b"PONG\0", version(number="123457")],
    )
    with pytest.raises(ManuscriptScanError) as caught:
        ClamAVScanner("/run/citation-av/clamd.sock").scan([PUBLIC_PDF], max_bytes=1000)
    assert caught.value.code == "MANUSCRIPT_SCAN_CHANGED"


def test_stream_limit_and_timeout_release_the_scanner_slot(monkeypatch):
    v = version()
    socket_replies(
        monkeypatch,
        [b"PONG\0", v, b"", b"PONG\0", v, b"stream: OK\0", b"PONG\0", v],
    )
    scanner = ClamAVScanner("/run/citation-av/clamd.sock")
    with pytest.raises(ManuscriptScanError) as caught:
        scanner.scan([PUBLIC_PDF], max_bytes=2)
    assert caught.value.code == "MANUSCRIPT_SCAN_LIMIT"
    assert scanner.scan([PUBLIC_PDF], max_bytes=1000).size_bytes == len(PUBLIC_PDF)
    scanner._slot.acquire()
    try:
        with pytest.raises(ManuscriptScanError) as caught:
            scanner.scan([PUBLIC_PDF], max_bytes=1000)
        assert caught.value.code == "MANUSCRIPT_SCAN_BUSY"
    finally:
        scanner._slot.release()


class UploadService:
    def __init__(self, directory: Path, *, limit: int = 2_000_000):
        self._upload_temp_dir = directory
        self.limits = ManuscriptLimits(max_upload_bytes=limit)
        self.saved = []
        self._idempotency = {}

    def save_upload(
        self, tenant, filename, content_type, chunks, *, declared_size, idempotency_key
    ):
        data = b"".join(chunks)
        assert len(data) == declared_size
        digest = hashlib.sha256(data).hexdigest()
        key = (tenant, idempotency_key)
        if idempotency_key and key in self._idempotency:
            record = self._idempotency[key]
            if record["sha256"] != digest:
                raise ManuscriptError(409, "MANUSCRIPT_IDEMPOTENCY_CONFLICT", "请求冲突。")
            return record
        record = {
            "manuscript_id": f"ms_test{len(self.saved)}",
            "parse_status": "queued",
            "sha256": digest,
            "size_bytes": len(data),
            "tenant_id": tenant,
        }
        self.saved.append(record)
        if idempotency_key:
            self._idempotency[key] = record
        return record


class InjectedScanner:
    def __init__(self, *, error: ManuscriptScanError | None = None, partial: bool = False):
        self.error = error
        self.partial = partial
        self.calls = 0
        self.info = engine()

    def probe(self):
        if self.error:
            raise self.error
        return self.info

    def scan(self, chunks, *, max_bytes):
        self.calls += 1
        if self.error:
            raise self.error
        data = next(iter(chunks)) if self.partial else b"".join(chunks)
        assert len(data) <= max_bytes
        return ScanReceipt(hashlib.sha256(data).hexdigest(), len(data), self.info, "checked")


def upload_app(tmp_path, scanner=None, *, required=True, limit=2_000_000):
    service = UploadService(tmp_path, limit=limit)
    app = FastAPI()
    app.add_exception_handler(ManuscriptError, manuscript_error_handler)
    app.include_router(
        build_manuscript_router(
            service,
            lambda request: request.headers.get("x-tenant-id", OWNER),
            scan_required=required,
            scanner=scanner,
        )
    )
    return app, service


def upload_headers():
    return {"X-Filename": "public.pdf", "Content-Type": "application/pdf", "X-Tenant-ID": OWNER}


def test_scan_precedes_saving_and_receipt_matches_spooled_upload(tmp_path):
    scanner = InjectedScanner()
    app, service = upload_app(tmp_path, scanner)
    body = PUBLIC_PDF + b"a" * 1_200_000
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/manuscripts",
            content=body,
            headers={**upload_headers(), "Idempotency-Key": "same"},
        )
        again = client.post(
            "/api/v1/manuscripts",
            content=body,
            headers={**upload_headers(), "Idempotency-Key": "same"},
        )
        conflict = client.post(
            "/api/v1/manuscripts",
            content=PUBLIC_PDF,
            headers={**upload_headers(), "Idempotency-Key": "same"},
        )
    assert first.status_code == again.status_code == 202
    receipt = first.json()["security_scan"]
    assert receipt["status"] == "clean"
    assert receipt["input_sha256"] == first.json()["sha256"]
    assert receipt["size_bytes"] == len(body)
    assert len(service.saved) == 1
    assert scanner.calls == 3
    assert conflict.status_code == 409
    assert again.json()["manuscript_id"] == first.json()["manuscript_id"]


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("MANUSCRIPT_MALWARE_DETECTED", 422),
        ("MANUSCRIPT_SCAN_UNAVAILABLE", 503),
        ("MANUSCRIPT_SCAN_TIMEOUT", 503),
        ("MANUSCRIPT_SCAN_FAILED", 503),
        ("MANUSCRIPT_SCAN_INVALID_RESPONSE", 503),
        ("MANUSCRIPT_SCAN_LIMIT", 413),
    ],
)
def test_http_refuses_scan_failures_without_saving(tmp_path, code, status):
    error = ManuscriptScanError(code, "本次扫描未通过。", status=status)
    app, service = upload_app(tmp_path, InjectedScanner(error=error))
    with TestClient(app) as client:
        result = client.post("/api/v1/manuscripts", content=PUBLIC_PDF, headers=upload_headers())
    assert result.status_code == status
    assert result.json()["code"] == code
    assert not service.saved


def test_required_missing_scanner_never_accepts_and_development_degradation_is_explicit(tmp_path):
    app, service = upload_app(tmp_path)
    with TestClient(app) as client:
        refused = client.post("/api/v1/manuscripts", content=PUBLIC_PDF, headers=upload_headers())
    assert refused.status_code == 503
    assert not service.saved
    app, service = upload_app(tmp_path, required=False)
    with TestClient(app) as client:
        accepted = client.post("/api/v1/manuscripts", content=PUBLIC_PDF, headers=upload_headers())
    assert accepted.status_code == 202
    assert accepted.json()["security_scan"]["status"] == "not_scanned"


def test_partial_consumption_cannot_be_reported_as_clean(tmp_path):
    app, service = upload_app(tmp_path, InjectedScanner(partial=True))
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/manuscripts", content=PUBLIC_PDF + b"a" * 1_200_000, headers=upload_headers()
        )
    assert result.status_code == 503
    assert result.json()["code"] == "MANUSCRIPT_SCAN_INVALID_RESPONSE"
    assert not service.saved


@pytest.mark.parametrize("mode", ["unknown", "hash_mismatch"])
def test_fabricated_clean_receipt_cannot_reach_storage(tmp_path, mode):
    class BadReceiptScanner(InjectedScanner):
        def scan(self, chunks, *, max_bytes):
            receipt = super().scan(chunks, max_bytes=max_bytes)
            if mode == "unknown":
                return {"status": "clean"}
            return replace(receipt, input_sha256="0" * 64)

    app, service = upload_app(tmp_path, BadReceiptScanner())
    with TestClient(app) as client:
        result = client.post("/api/v1/manuscripts", content=PUBLIC_PDF, headers=upload_headers())
    assert result.status_code == 503
    assert result.json()["code"] == "MANUSCRIPT_SCAN_INVALID_RESPONSE"
    assert not service.saved


def test_oversized_body_is_rejected_without_invoking_scanner(tmp_path):
    scanner = InjectedScanner()
    app, service = upload_app(tmp_path, scanner, limit=1000)
    with TestClient(app) as client:
        result = client.post("/api/v1/manuscripts", content=b"a" * 1001, headers=upload_headers())
    assert result.status_code == 413
    assert scanner.calls == 0 and not service.saved


async def test_cancellation_waits_owned_scan_and_never_saves_its_late_result(tmp_path):
    class BlockingScanner(InjectedScanner):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def scan(self, chunks, *, max_bytes):
            self.started.set()
            assert self.release.wait(3)
            return super().scan(chunks, max_bytes=max_bytes)

    scanner = BlockingScanner()
    app, service = upload_app(tmp_path, scanner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post("/api/v1/manuscripts", content=PUBLIC_PDF, headers=upload_headers())
        )
        assert await asyncio.to_thread(scanner.started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not service.saved
        scanner.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not service.saved


def test_capability_and_app_wiring_fail_closed_when_required(tmp_path):
    required = scan_capability(uploads_enabled=True, required=True, scanner=None)
    assert required["manuscript_upload_enabled"] is False
    development = scan_capability(uploads_enabled=True, required=False, scanner=None)
    assert development["manuscript_upload_enabled"] is True
    assert development["manuscript_scan_mode"] == "development_unscanned"
    scanner = InjectedScanner(
        error=ManuscriptScanError("MANUSCRIPT_SCAN_UNAVAILABLE", "unavailable")
    )
    settings = replace(Settings.for_tests(tmp_path), manuscript_scan_required=True)
    app = create_app(settings, manuscript_scanner=scanner)
    with TestClient(app) as client:
        libraries = client.get("/api/v1/libraries").json()
        assert libraries["manuscript_scan_required"] is True
        assert libraries["manuscript_scan_available"] is False
        assert libraries["manuscript_upload_enabled"] is False
        scanner.error = None
        assert client.get("/api/v1/libraries").json()["manuscript_upload_enabled"] is True


def test_scan_configuration_uses_explicit_environment_and_bounded_socket(monkeypatch):
    monkeypatch.setenv("CITATION_MANUSCRIPT_SCAN_REQUIRED", "1")
    monkeypatch.setenv("CITATION_MANUSCRIPT_SCAN_SOCKET", "/run/citation-av/test.sock")
    monkeypatch.setenv("CITATION_MANUSCRIPT_SCAN_TIMEOUT_S", "45")
    settings = settings_from_env()
    assert settings.manuscript_scan_required is True
    assert settings.manuscript_scan_socket == "/run/citation-av/test.sock"
    assert settings.manuscript_scan_timeout_s == 45
    with pytest.raises(ValueError, match="Unix socket"):
        replace(settings, manuscript_scan_socket="http://external.example").validate()
    with pytest.raises(ValueError, match="between 1 and 90"):
        replace(settings, manuscript_scan_timeout_s=0).validate()
