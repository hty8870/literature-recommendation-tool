"""Bounded local ClamAV scans of upload bytes before storage or parsing."""

from __future__ import annotations

import hashlib
import re
import socket
import struct
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol


class ManuscriptScanError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 503, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ScanEngine:
    engine_version: str
    definitions_version: str
    definitions_date: str
    engine: str = "ClamAV"


@dataclass(frozen=True, slots=True)
class ScanReceipt:
    input_sha256: str
    size_bytes: int
    engine: ScanEngine
    checked_at: str

    def as_dict(self) -> dict:
        return {"status": "clean", **asdict(self)}


class ManuscriptScanner(Protocol):
    def probe(self) -> ScanEngine: ...

    def scan(self, chunks: Iterable[bytes], *, max_bytes: int) -> ScanReceipt: ...


def scan_capability(
    *, uploads_enabled: bool, required: bool, scanner: ManuscriptScanner | None
) -> dict:
    available = False
    failure = None
    if scanner is not None and uploads_enabled:
        try:
            info = scanner.probe()
            available = isinstance(info, ScanEngine)
            if not available:
                failure = "MANUSCRIPT_SCAN_INVALID_RESPONSE"
        except ManuscriptScanError as exc:
            failure = exc.code
        except Exception:
            failure = "MANUSCRIPT_SCAN_UNAVAILABLE"
    elif required and uploads_enabled:
        failure = "MANUSCRIPT_SCAN_UNAVAILABLE"
    return {
        "manuscript_upload_configured": uploads_enabled,
        "manuscript_upload_enabled": uploads_enabled
        and (available if required or scanner is not None else True),
        "manuscript_scan_required": required,
        "manuscript_scan_available": available,
        "manuscript_scan_mode": (
            "required"
            if required
            else "configured"
            if scanner is not None
            else "development_unscanned"
        ),
        "manuscript_scan_failure": failure,
    }


class ClamAVScanner:
    def __init__(
        self,
        socket_path: str,
        *,
        timeout_s: float = 60,
        max_definitions_age_s: float = 7 * 86400,
    ):
        if not socket_path.startswith("/") or "\0" in socket_path:
            raise ValueError("ClamAV requires an absolute local Unix socket path")
        if not 0 < timeout_s <= 90:
            raise ValueError("scan timeout must be between 0 and 90 seconds")
        if max_definitions_age_s <= 0:
            raise ValueError("definitions maximum age must be positive")
        self.socket_path = socket_path
        self.timeout_s = float(timeout_s)
        self.max_definitions_age_s = float(max_definitions_age_s)
        self._slot = threading.BoundedSemaphore(1)

    def probe(self) -> ScanEngine:
        return self._probe(time.monotonic() + min(1.0, self.timeout_s))

    def _probe(self, deadline: float) -> ScanEngine:
        try:
            if self._command(b"zPING\0", deadline) != "PONG":
                raise _invalid_response()
            version = self._command(b"zVERSION\0", deadline)
        except ManuscriptScanError:
            raise
        except (OSError, ValueError, AttributeError):
            raise _unavailable() from None
        matched = re.fullmatch(
            r"ClamAV ([0-9][A-Za-z0-9.+_-]{0,63})/([0-9]{1,16})/([^\x00-\x1f]{1,80})",
            version,
        )
        if matched is None:
            raise _invalid_response()
        try:
            build_date = datetime.strptime(matched[3], "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
        except ValueError:
            raise _invalid_response() from None
        age = (datetime.now(UTC) - build_date).total_seconds()
        if age > self.max_definitions_age_s or age < -86400:
            raise ManuscriptScanError(
                "MANUSCRIPT_SCAN_STALE", "病毒库日期不可确认或已过期，暂不接收全文。"
            )
        return ScanEngine(matched[1], matched[2], matched[3])

    def scan(self, chunks: Iterable[bytes], *, max_bytes: int) -> ScanReceipt:
        if max_bytes < 1:
            raise ValueError("scan size limit must be positive")
        if not self._slot.acquire(blocking=False):
            raise ManuscriptScanError(
                "MANUSCRIPT_SCAN_BUSY", "恶意文件扫描正在处理另一份稿件，请稍后重试。", status=429
            )
        deadline = time.monotonic() + self.timeout_s
        digest = hashlib.sha256()
        size = 0
        try:
            info = self._probe(min(deadline, time.monotonic() + 1.0))
            with self._connect(deadline) as connection:
                self._send(connection, b"zINSTREAM\0", deadline)
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise _invalid_response()
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise _scan_limit()
                    digest.update(chunk)
                    self._send(connection, struct.pack("!I", len(chunk)), deadline)
                    self._send(connection, chunk, deadline)
                if size == 0:
                    raise _invalid_response()
                self._send(connection, b"\0\0\0\0", deadline)
                result = self._receive(connection, deadline)
            _validate_result(result)
            if self._probe(min(deadline, time.monotonic() + 1.0)) != info:
                raise ManuscriptScanError(
                    "MANUSCRIPT_SCAN_CHANGED", "扫描期间病毒库发生变化，请稍后重试。"
                )
            return ScanReceipt(
                input_sha256=digest.hexdigest(),
                size_bytes=size,
                engine=info,
                checked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        except ManuscriptScanError:
            raise
        except TimeoutError:
            raise _timeout() from None
        except (OSError, ValueError, AttributeError):
            raise _unavailable() from None
        finally:
            self._slot.release()

    def _connect(self, deadline: float):
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(_remaining(deadline))
            connection.connect(self.socket_path)
            return connection
        except BaseException:
            if "connection" in locals():
                connection.close()
            raise

    def _command(self, command: bytes, deadline: float) -> str:
        try:
            with self._connect(deadline) as connection:
                self._send(connection, command, deadline)
                return self._receive(connection, deadline)
        except TimeoutError:
            raise _timeout() from None

    @staticmethod
    def _send(connection, chunk: bytes, deadline: float) -> None:
        connection.settimeout(_remaining(deadline))
        connection.sendall(chunk)

    @staticmethod
    def _receive(connection, deadline: float) -> str:
        result = bytearray()
        while len(result) <= 2048:
            connection.settimeout(_remaining(deadline))
            chunk = connection.recv(min(2049 - len(result), 512))
            if not chunk:
                raise _invalid_response()
            result.extend(chunk)
            if len(result) > 2048:
                raise _invalid_response()
            if b"\0" in result:
                if not result.endswith(b"\0") or result.count(b"\0") != 1:
                    raise _invalid_response()
                try:
                    return result[:-1].decode("ascii")
                except UnicodeDecodeError:
                    raise _invalid_response() from None
        raise _invalid_response()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _timeout()
    return remaining


def _validate_result(result: str) -> None:
    if result == "stream: OK":
        return
    if any(
        marker in result.casefold()
        for marker in (".limits.exceeded", "limit exceeded", "maxscansize", "maxfilesize")
    ):
        raise _scan_limit()
    if result.startswith("stream: ") and result.endswith(" FOUND"):
        raise ManuscriptScanError(
            "MANUSCRIPT_MALWARE_DETECTED",
            "上传文件触发恶意文件检测，未保存或解析。",
            status=422,
            retryable=False,
        )
    if result.endswith(" ERROR"):
        raise ManuscriptScanError("MANUSCRIPT_SCAN_FAILED", "恶意文件扫描未完成，未接收稿件。")
    raise _invalid_response()


def _unavailable() -> ManuscriptScanError:
    return ManuscriptScanError("MANUSCRIPT_SCAN_UNAVAILABLE", "恶意文件扫描不可用，暂不接收全文。")


def _invalid_response() -> ManuscriptScanError:
    return ManuscriptScanError(
        "MANUSCRIPT_SCAN_INVALID_RESPONSE", "恶意文件扫描结果无法确认，未接收稿件。"
    )


def _timeout() -> ManuscriptScanError:
    return ManuscriptScanError("MANUSCRIPT_SCAN_TIMEOUT", "恶意文件扫描超时，未接收稿件。")


def _scan_limit() -> ManuscriptScanError:
    return ManuscriptScanError(
        "MANUSCRIPT_SCAN_LIMIT",
        "文件超出恶意文件扫描资源上限，未接收稿件。",
        status=413,
        retryable=False,
    )
