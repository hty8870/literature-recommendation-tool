"""Private manuscript upload, real PDF/DOCX parsing and passage indexing.

Manuscripts are user-private research material. The original bytes live only in
a tenant-scoped private directory; parsed passages live in SQLite next to the
service database. Nothing here writes to the public paper libraries, snapshots
or reason caches, and no function logs manuscript text or storage paths.

CPU-heavy parsing runs in a spawned child process with a hard timeout. The
service only keeps a bounded number of jobs in flight, so a large upload cannot
starve the event loop or the public search path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import sqlite3
import statistics
import threading
import time
import uuid
import zipfile
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .entities import utc_now_iso

logger = logging.getLogger(__name__)

PARSER_VERSION = "manuscript-parser-v2-pdfium-index-mapping"
PDF_CONTENT_TYPE = "application/pdf"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
UPLOAD_CONTENT_TYPES = {PDF_CONTENT_TYPE, DOCX_CONTENT_TYPE, "application/octet-stream"}

PARSE_QUEUED = "queued"
PARSE_PARSING = "parsing"
PARSE_READY = "ready"
PARSE_FAILED = "failed"
TERMINAL_PARSE_STATES = {PARSE_READY, PARSE_FAILED}

PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURE = b"PK\x03\x04"
CFB_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
MAX_FILENAME_CHARS = 200

ERROR_MESSAGES = {
    "MANUSCRIPT_NOT_FOUND": "稿件不存在或不属于当前租户。",
    "MANUSCRIPT_INVALID_REQUEST": "上传请求缺少文件名或格式不合法。",
    "MANUSCRIPT_INVALID_SIGNATURE": "文件内容与声明的 PDF/DOCX 格式不符。",
    "MANUSCRIPT_UNSUPPORTED_TYPE": "仅支持 PDF 或 DOCX 文件。",
    "MANUSCRIPT_TOO_LARGE": "文件超过允许的大小上限。",
    "MANUSCRIPT_ENCRYPTED": "文件已加密或属于不支持的旧格式，无法解析。",
    "MANUSCRIPT_EMPTY": "文件为空。",
    "MANUSCRIPT_NOT_READY": "稿件仍在解析中，暂不可读取正文。",
    "MANUSCRIPT_PARSE_FAILED": "稿件解析失败，没有可读取的正文。",
    "MANUSCRIPT_CAPACITY": "稿件解析任务过多，请稍后重试。",
    "MANUSCRIPT_STORAGE_ERROR": "稿件存储或读取失败。",
    "MANUSCRIPT_DELETE_FAILED": "稿件删除未完全成功，请重试。",
    "MANUSCRIPT_CORRUPT": "文件已损坏，无法解析。",
    "MANUSCRIPT_EMPTY_TEXT": "文件中没有可提取的文本。",
    "MANUSCRIPT_PAGE_LIMIT": "文件页数超过允许上限。",
    "MANUSCRIPT_PASSAGE_LIMIT": "文件段落数超过允许上限。",
    "MANUSCRIPT_TEXT_LIMIT": "文件文本量超过允许上限。",
    "MANUSCRIPT_PARSE_TIMEOUT": "稿件解析超时。",
    "MANUSCRIPT_OCR_UNAVAILABLE": "本地 OCR 不可用，扫描页未能解析。",
    "MANUSCRIPT_PARSE_WORKER_FAILED": "本地解析进程未能正常完成。",
    "MANUSCRIPT_UNSAFE_ARCHIVE": "DOCX 压缩包包含不安全的路径或条目。",
    "MANUSCRIPT_DECOMPRESSION_LIMIT": "DOCX 解压后的大小或条目数超过允许上限。",
    "MANUSCRIPT_UNSAFE_XML": "DOCX 包含不允许的 XML 声明。",
    "MANUSCRIPT_EXTERNAL_LINK": "DOCX 包含不允许的外部关系，已拒绝解析。",
    "MANUSCRIPT_UNSUPPORTED_STRUCTURE": "DOCX 包含不支持的宏或嵌入式结构。",
    "MANUSCRIPT_IDEMPOTENCY_CONFLICT": "Idempotency-Key 已用于不同的上传请求。",
}

_WARNING_MESSAGES = {
    "reading_order_unverified": "未验证双栏/跨栏阅读顺序；段落顺序来自真实 PDF 文本坐标。",
    "ocr_text_unverified": "扫描页文本来自本地 OCR，尚未逐字人工核验。",
    "ocr_low_confidence": "部分 OCR 行置信度较低，引用前需要核对原文。",
    "ocr_unavailable": "本地 OCR 不可用；扫描页未解析，不伪造文本。",
    "ocr_page_limit": "扫描页数量超过 OCR 上限；超出部分未解析。",
    "empty_page": "存在没有可用文本的页面。",
    "page_pixels_exceeded": "页面像素超过渲染上限，该页未做 OCR。",
    "page_rotation": "页面带旋转，未计算该页坐标。",
    "passage_split": "超长段落按字符上限拆分，字符区间仍对应拆分后的文本。",
    "reading_order_text_layer_only": "阅读顺序仅依据 PDF 文本层，未做版面模型核验。",
    "docx_no_pdf_pages": "DOCX 没有 PDF 页码；锚点仅包含段落与章节。",
    "ignored_external_link": "DOCX 中的外部超链接仅保留可见文字；未访问链接或将其作为来源证据。",
    "bbox_unavailable": "该段缺少可计算的页面坐标。",
    "char_index_mismatch": "PDF 文本索引与字符框索引存在差异；已映射可用坐标，仍需核查。",
    "line_breaks_preserved": "提取文本保留原始换行，未做断词修复。",
    "parse_timeout": "稿件解析超过硬超时，子进程已终止。",
    "parse_interrupted": "服务重启时解析任务尚未完成，已标记为失败。",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS manuscripts (
    tenant_id TEXT NOT NULL,
    manuscript_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    source_format TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    passage_count INTEGER NOT NULL DEFAULT 0,
    text_chars INTEGER NOT NULL DEFAULT 0,
    text_page_count INTEGER NOT NULL DEFAULT 0,
    ocr_page_count INTEGER NOT NULL DEFAULT 0,
    empty_page_count INTEGER NOT NULL DEFAULT 0,
    ocr_mean_confidence REAL,
    warnings_json TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    parse_started_at TEXT,
    parsed_at TEXT,
    parse_duration_ms INTEGER,
    delete_after TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, manuscript_id)
);
CREATE TABLE IF NOT EXISTS manuscript_passages (
    tenant_id TEXT NOT NULL,
    manuscript_id TEXT NOT NULL,
    passage_index INTEGER NOT NULL,
    page INTEGER,
    paragraph_index INTEGER,
    section_path_json TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    source_method TEXT NOT NULL,
    ocr_confidence REAL,
    bbox_json TEXT,
    reading_order_confidence REAL,
    warnings_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, manuscript_id, passage_index)
);
CREATE TABLE IF NOT EXISTS manuscript_deletions (
    tenant_id TEXT NOT NULL,
    manuscript_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    failure_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, manuscript_id)
);
CREATE TABLE IF NOT EXISTS manuscript_upload_idempotency (
    tenant_id TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    manuscript_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key_hash)
);
CREATE INDEX IF NOT EXISTS manuscripts_status_idx
    ON manuscripts (tenant_id, parse_status, updated_at);
"""


class ManuscriptError(Exception):
    """Stable public error; safe to map onto the API problem+json handler."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, "稿件请求失败。")
        self.details = details or {}
        self.retryable = retryable
        super().__init__(self.message)


class _ParseFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        warnings: list[dict[str, Any]] | None = None,
    ) -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, "稿件解析失败。")
        self.warnings = warnings or []
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ManuscriptLimits:
    max_upload_bytes: int = 50 * 1024 * 1024
    max_pages: int = 500
    max_page_pixels: int = 30_000_000
    max_passages: int = 20_000
    max_text_chars: int = 5_000_000
    max_passage_chars: int = 4_000
    max_ocr_pages: int = 50
    ocr_min_text_chars: int = 16
    ocr_render_scale: float = 2.0
    ocr_low_confidence: float = 0.60
    docx_max_uncompressed_bytes: int = 200 * 1024 * 1024
    docx_max_entries: int = 5_000
    docx_max_entry_bytes: int = 50 * 1024 * 1024
    docx_max_compression_ratio: int = 200

    def as_parse_options(self) -> dict[str, Any]:
        return {
            "max_pages": self.max_pages,
            "max_page_pixels": self.max_page_pixels,
            "max_passages": self.max_passages,
            "max_text_chars": self.max_text_chars,
            "max_passage_chars": self.max_passage_chars,
            "max_ocr_pages": self.max_ocr_pages,
            "ocr_min_text_chars": self.ocr_min_text_chars,
            "ocr_render_scale": self.ocr_render_scale,
            "ocr_low_confidence": self.ocr_low_confidence,
            "docx_max_uncompressed_bytes": self.docx_max_uncompressed_bytes,
            "docx_max_entries": self.docx_max_entries,
            "docx_max_entry_bytes": self.docx_max_entry_bytes,
            "docx_max_compression_ratio": self.docx_max_compression_ratio,
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _from_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tenant_key(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]


_MANUSCRIPT_ID_RE = re.compile(r"^ms_[0-9a-f]{32}$")
_PDF_EXTENSIONS = {"pdf"}
_DOCX_EXTENSIONS = {"docx"}


def _validate_manuscript_id(manuscript_id: str) -> bool:
    return bool(_MANUSCRIPT_ID_RE.fullmatch(manuscript_id or ""))


def _normalize_content_type(value: str) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~:-]{1,128}", value):
        raise ManuscriptError(400, "MANUSCRIPT_INVALID_REQUEST")
    return value


def _upload_request_sha256(
    filename: str,
    content_type: str,
    kind: str,
    content_sha256: str,
) -> str:
    identity = _json([filename, content_type, kind, content_sha256])
    return _sha256_bytes(identity.encode("utf-8"))


def _detect_format(filename: str, content_type: str) -> str:
    normalized = _normalize_content_type(content_type)
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if normalized == PDF_CONTENT_TYPE:
        return "pdf"
    if normalized == DOCX_CONTENT_TYPE:
        return "docx"
    if normalized in {"application/octet-stream", "application/zip", ""}:
        if suffix in _PDF_EXTENSIONS:
            return "pdf"
        if suffix in _DOCX_EXTENSIONS:
            return "docx"
    raise ManuscriptError(415, "MANUSCRIPT_UNSUPPORTED_TYPE")


def sanitize_filename(raw: str) -> str:
    if not isinstance(raw, str):
        raise ManuscriptError(400, "MANUSCRIPT_INVALID_REQUEST")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ManuscriptError(400, "MANUSCRIPT_INVALID_REQUEST")
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".")
    if not name or name in {".", ".."}:
        raise ManuscriptError(400, "MANUSCRIPT_INVALID_REQUEST")
    return name[:MAX_FILENAME_CHARS]


def _validate_tenant(tenant_id: str) -> str:
    if not isinstance(tenant_id, str):
        raise ManuscriptError(400, "MANUSCRIPT_INVALID_REQUEST")
    tenant = tenant_id.strip()
    if not tenant or len(tenant) > 128:
        raise ManuscriptError(400, "MANUSCRIPT_INVALID_REQUEST")
    return tenant


def _file_signature_ok(kind: str, head: bytes) -> bool:
    if kind == "pdf":
        return PDF_SIGNATURE in head
    return head.startswith(ZIP_SIGNATURE)


def _warning(code: str, *, count: int | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "code": code,
        "message": _WARNING_MESSAGES.get(code, "解析质量警告。"),
    }
    if count is not None:
        entry["count"] = count
    return entry


def _merge_warnings(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if not code:
            continue
        if code not in merged:
            merged[code] = {
                "code": code,
                "message": item.get("message") or _WARNING_MESSAGES.get(code, "解析质量警告。"),
            }
            order.append(code)
        count = item.get("count")
        if isinstance(count, int) and count > 0:
            merged[code]["count"] = int(merged[code].get("count") or 0) + count
    return [merged[code] for code in order]


@dataclass(slots=True)
class _Job:
    tenant_id: str
    manuscript_id: str
    source_path: Path
    kind: str
    result_path: Path
    process: multiprocessing.process.BaseProcess | None = None
    cancelled: bool = False


def _terminate_process(process: multiprocessing.process.BaseProcess) -> None:
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
        process.join(2.0)
    if process.is_alive():
        process.kill()
    process.join()


class ManuscriptUpload:
    """One in-flight upload; bytes are written by the HTTP router as they arrive."""

    def __init__(
        self,
        service: ManuscriptService,
        tenant_id: str,
        filename: str,
        content_type: str,
        *,
        declared_size: int | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.service = service
        self.tenant_id = tenant_id
        self.filename = filename
        self.content_type = _normalize_content_type(content_type) or "application/octet-stream"
        self.kind = _detect_format(filename, self.content_type)
        self.idempotency_key = _validate_idempotency_key(idempotency_key)
        if declared_size is not None and declared_size > service.limits.max_upload_bytes:
            raise ManuscriptError(413, "MANUSCRIPT_TOO_LARGE")
        self.manuscript_id = f"ms_{uuid.uuid4().hex}"
        self._dir = service._manuscript_dir(tenant_id, self.manuscript_id)
        self._dir.mkdir(parents=True, exist_ok=False)
        _chmod_private(self._dir)
        self._part_path = self._dir / "original.part"
        handle = None
        try:
            handle = open(self._part_path, "xb")  # noqa: SIM115 - closed by finish/abort
            os.chmod(self._part_path, 0o600)
        except OSError:
            if handle is not None:
                with suppress(OSError):
                    handle.close()
            shutil.rmtree(self._dir, ignore_errors=True)
            raise ManuscriptError(500, "MANUSCRIPT_STORAGE_ERROR") from None
        self._handle = handle
        self._hasher = hashlib.sha256()
        self._size = 0
        self._state = "open"

    def write(self, chunk: bytes) -> None:
        if self._state != "open":
            raise RuntimeError("upload is no longer open")
        if not chunk:
            return
        self._size += len(chunk)
        if self._size > self.service.limits.max_upload_bytes:
            self.abort()
            raise ManuscriptError(413, "MANUSCRIPT_TOO_LARGE")
        try:
            self._handle.write(chunk)
        except OSError:
            self.abort()
            raise ManuscriptError(500, "MANUSCRIPT_STORAGE_ERROR") from None
        self._hasher.update(chunk)

    def finish(self) -> dict[str, Any]:
        if self._state != "open":
            raise RuntimeError("upload is no longer open")
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
        except OSError:
            self.abort()
            raise ManuscriptError(500, "MANUSCRIPT_STORAGE_ERROR") from None
        self._state = "closed"
        if self._size <= 0:
            self.abort()
            raise ManuscriptError(422, "MANUSCRIPT_EMPTY")
        try:
            with open(self._part_path, "rb") as handle:
                head = handle.read(1024)
        except OSError:
            self.abort()
            raise ManuscriptError(500, "MANUSCRIPT_STORAGE_ERROR") from None
        if self.kind == "docx" and head.startswith(CFB_SIGNATURE):
            self.abort()
            raise ManuscriptError(422, "MANUSCRIPT_ENCRYPTED")
        if not _file_signature_ok(self.kind, head):
            self.abort()
            raise ManuscriptError(400, "MANUSCRIPT_INVALID_SIGNATURE")
        digest = self._hasher.hexdigest()
        request_sha256 = _upload_request_sha256(self.filename, self.content_type, self.kind, digest)
        final_path = self._dir / ("original.pdf" if self.kind == "pdf" else "original.docx")
        key = self.idempotency_key
        key_hash = hashlib.sha256(key.encode("ascii")).hexdigest() if key is not None else None
        lock = self.service._idempotency_lock if key is not None else nullcontext()
        with lock:
            if key_hash is not None:
                existing = self.service._find_idempotent_upload(self.tenant_id, key_hash)
                if existing is not None:
                    self.abort()
                    if existing["request_sha256"] != request_sha256:
                        raise ManuscriptError(409, "MANUSCRIPT_IDEMPOTENCY_CONFLICT")
                    record = self.service.get(
                        self.tenant_id, existing["manuscript_id"], catalog_limit=0
                    )
                    if record is None:
                        raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
                    return record
            try:
                os.replace(self._part_path, final_path)
                os.chmod(final_path, 0o600)
            except OSError:
                self.abort()
                raise ManuscriptError(500, "MANUSCRIPT_STORAGE_ERROR") from None
            now = utc_now_iso()
            try:
                with self.service._write() as conn:
                    conn.execute(
                        """INSERT INTO manuscripts (
                            tenant_id, manuscript_id, filename, content_type, source_format,
                            size_bytes, sha256, parse_status, parser_version, warnings_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            self.tenant_id,
                            self.manuscript_id,
                            self.filename,
                            self.content_type,
                            self.kind,
                            self._size,
                            digest,
                            PARSE_QUEUED,
                            PARSER_VERSION,
                            _json([]),
                            now,
                            now,
                        ),
                    )
                    if key_hash is not None:
                        conn.execute(
                            """INSERT INTO manuscript_upload_idempotency (
                                tenant_id, key_hash, request_sha256, manuscript_id, created_at
                            ) VALUES (?, ?, ?, ?, ?)""",
                            (
                                self.tenant_id,
                                key_hash,
                                request_sha256,
                                self.manuscript_id,
                                now,
                            ),
                        )
                self.service._submit(self.tenant_id, self.manuscript_id, final_path, self.kind)
            except BaseException:
                self.service._discard_manuscript(self.tenant_id, self.manuscript_id)
                raise
        record = self.service.get(self.tenant_id, self.manuscript_id, catalog_limit=0)
        if record is None:
            raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
        return record

    def abort(self) -> None:
        if self._state == "open":
            try:
                self._handle.close()
            except OSError:
                pass
        self._state = "aborted"
        shutil.rmtree(self._dir, ignore_errors=True)


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


class ManuscriptService:
    """Tenant-scoped private manuscript store with bounded background parsing."""

    def __init__(
        self,
        db_path: Path,
        storage_dir: Path,
        *,
        limits: ManuscriptLimits | None = None,
        parse_concurrency: int = 1,
        max_pending_jobs: int = 8,
        parse_timeout_s: float = 180.0,
        ocr_enabled: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.storage_dir = Path(storage_dir)
        self.limits = limits or ManuscriptLimits()
        self.parse_timeout_s = float(parse_timeout_s)
        if parse_concurrency < 1 or parse_concurrency > 8:
            raise ValueError("parse_concurrency must be between 1 and 8")
        if max_pending_jobs < parse_concurrency or max_pending_jobs > 64:
            raise ValueError("max_pending_jobs must be between parse_concurrency and 64")
        if self.parse_timeout_s <= 0 or self.parse_timeout_s > 3_600:
            raise ValueError("parse_timeout_s must be between 0 and 3600")
        self.parse_concurrency = parse_concurrency
        self.max_pending_jobs = max_pending_jobs
        self.ocr_enabled = bool(ocr_enabled)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch(mode=0o600, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private(self.storage_dir)
        parse_temp = self.storage_dir / ".parse"
        parse_temp.mkdir(parents=True, exist_ok=True)
        _chmod_private(parse_temp)
        self._upload_temp_dir = self.storage_dir / ".uploads"
        self._upload_temp_dir.mkdir(parents=True, exist_ok=True)
        _chmod_private(self._upload_temp_dir)
        self._cleanup_parse_temp()
        self._cleanup_upload_temp()
        self._cleanup_incomplete_uploads()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        self._cleanup_orphaned_manuscripts()
        self._mark_interrupted_jobs()
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()
        self._idempotency_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=parse_concurrency, thread_name_prefix="manuscript-parse"
        )
        self._closed = False

    def _cleanup_parse_temp(self) -> None:
        """A restart means no in-flight result file can still be consumed."""
        parse_temp = self.storage_dir / ".parse"
        for path in parse_temp.glob("*.json*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_upload_temp(self) -> None:
        for path in self._upload_temp_dir.iterdir():
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_incomplete_uploads(self) -> None:
        for path in self.storage_dir.glob("*/ms_*/original.part"):
            if _validate_manuscript_id(path.parent.name):
                shutil.rmtree(path.parent, ignore_errors=True)

    def _cleanup_orphaned_manuscripts(self) -> None:
        with self._read() as conn:
            rows = conn.execute("SELECT tenant_id, manuscript_id FROM manuscripts").fetchall()
        registered = {(_tenant_key(row["tenant_id"]), row["manuscript_id"]) for row in rows}
        for tenant_dir in self.storage_dir.iterdir():
            if tenant_dir.name in {".parse", ".uploads"} or tenant_dir.is_symlink():
                continue
            if not tenant_dir.is_dir():
                continue
            for manuscript_dir in tenant_dir.iterdir():
                if (
                    manuscript_dir.is_dir()
                    and not manuscript_dir.is_symlink()
                    and _validate_manuscript_id(manuscript_dir.name)
                    and (tenant_dir.name, manuscript_dir.name) not in registered
                ):
                    shutil.rmtree(manuscript_dir, ignore_errors=True)

    def _mark_interrupted_jobs(self) -> None:
        """Single-process service: a prior queued/parsing row cannot still be alive."""
        now = utc_now_iso()
        with self._write() as conn:
            conn.execute(
                """UPDATE manuscripts SET
                    parse_status=?, error_code=?, error_message=?, warnings_json=?,
                    parsed_at=?, updated_at=?
                WHERE parse_status IN (?, ?)""",
                (
                    PARSE_FAILED,
                    "MANUSCRIPT_PARSE_WORKER_FAILED",
                    "服务重启，解析任务中断；请重新上传稿件。",
                    _json([_warning("parse_interrupted")]),
                    now,
                    now,
                    PARSE_QUEUED,
                    PARSE_PARSING,
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _manuscript_dir(self, tenant_id: str, manuscript_id: str) -> Path:
        return self.storage_dir / _tenant_key(tenant_id) / manuscript_id

    def _find_idempotent_upload(self, tenant_id: str, key_hash: str) -> dict[str, str] | None:
        with self._read() as conn:
            row = conn.execute(
                """SELECT request_sha256, manuscript_id FROM manuscript_upload_idempotency
                WHERE tenant_id=? AND key_hash=?""",
                (tenant_id, key_hash),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_sha256": str(row["request_sha256"]),
            "manuscript_id": str(row["manuscript_id"]),
        }

    def open_upload(
        self,
        tenant_id: str,
        filename: str,
        content_type: str,
        *,
        declared_size: int | None = None,
        idempotency_key: str | None = None,
    ) -> ManuscriptUpload:
        tenant = _validate_tenant(tenant_id)
        clean_name = sanitize_filename(filename)
        return ManuscriptUpload(
            self,
            tenant,
            clean_name,
            content_type,
            declared_size=declared_size,
            idempotency_key=idempotency_key,
        )

    def save_upload(
        self,
        tenant_id: str,
        filename: str,
        content_type: str,
        chunks: Iterable[bytes],
        *,
        declared_size: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        upload = self.open_upload(
            tenant_id,
            filename,
            content_type,
            declared_size=declared_size,
            idempotency_key=idempotency_key,
        )
        try:
            for chunk in chunks:
                upload.write(chunk)
            return upload.finish()
        except BaseException:
            upload.abort()
            raise

    def _discard_manuscript(self, tenant_id: str, manuscript_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                "DELETE FROM manuscript_passages WHERE tenant_id=? AND manuscript_id=?",
                (tenant_id, manuscript_id),
            )
            conn.execute(
                "DELETE FROM manuscript_upload_idempotency WHERE tenant_id=? AND manuscript_id=?",
                (tenant_id, manuscript_id),
            )
            conn.execute(
                "DELETE FROM manuscripts WHERE tenant_id=? AND manuscript_id=?",
                (tenant_id, manuscript_id),
            )
        shutil.rmtree(self._manuscript_dir(tenant_id, manuscript_id), ignore_errors=True)

    def _submit(self, tenant_id: str, manuscript_id: str, source_path: Path, kind: str) -> None:
        key = f"{tenant_id}\x1f{manuscript_id}"
        result_path = self.storage_dir / ".parse" / f"{uuid.uuid4().hex}.json"
        with self._jobs_lock:
            if self._closed:
                raise ManuscriptError(503, "MANUSCRIPT_STORAGE_ERROR")
            if len(self._jobs) >= self.max_pending_jobs:
                raise ManuscriptError(429, "MANUSCRIPT_CAPACITY", retryable=True)
            job = _Job(tenant_id, manuscript_id, source_path, kind, result_path)
            self._jobs[key] = job
        try:
            self._executor.submit(self._run_job, job)
        except RuntimeError as exc:
            with self._jobs_lock:
                self._jobs.pop(key, None)
            raise ManuscriptError(503, "MANUSCRIPT_STORAGE_ERROR") from exc

    def _run_job(self, job: _Job) -> None:
        key = f"{job.tenant_id}\x1f{job.manuscript_id}"
        started = time.monotonic()
        process: multiprocessing.process.BaseProcess | None = None
        try:
            if not self._mark_parsing(job):
                return
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_parse_worker,
                args=(
                    str(job.source_path),
                    job.kind,
                    str(job.result_path),
                    self.limits.as_parse_options(),
                    self.ocr_enabled,
                ),
                daemon=True,
            )
            with self._jobs_lock:
                if job.cancelled:
                    return
                job.process = process
                process.start()
            process.join(self.parse_timeout_s)
            timed_out = process.is_alive()
            if timed_out:
                with self._jobs_lock:
                    _terminate_process(process)
            with self._jobs_lock:
                cancelled = job.cancelled
            if cancelled:
                return
            duration_ms = int((time.monotonic() - started) * 1000)
            if timed_out:
                self._finish_failed(
                    job,
                    "MANUSCRIPT_PARSE_TIMEOUT",
                    duration_ms,
                    [_warning("parse_timeout")],
                )
                return
            payload = self._read_result(job.result_path)
            if payload is None:
                self._finish_failed(job, "MANUSCRIPT_PARSE_WORKER_FAILED", duration_ms, [])
                return
            if not payload.get("ok"):
                warnings = payload.get("warnings") or []
                self._finish_failed(
                    job,
                    str(payload.get("error_code") or "MANUSCRIPT_CORRUPT"),
                    duration_ms,
                    warnings if isinstance(warnings, list) else [],
                )
                return
            self._finish_ready(job, payload, duration_ms)
        except BaseException as exc:
            logger.error(
                "manuscript parse job crashed id=%s kind=%s error_type=%s",
                job.manuscript_id,
                job.kind,
                type(exc).__name__,
            )
            try:
                self._finish_failed(
                    job,
                    "MANUSCRIPT_PARSE_WORKER_FAILED",
                    int((time.monotonic() - started) * 1000),
                    [],
                )
            except BaseException as failure_exc:
                logger.error(
                    "manuscript parse failure record failed id=%s error_type=%s",
                    job.manuscript_id,
                    type(failure_exc).__name__,
                )
        finally:
            try:
                with self._jobs_lock:
                    try:
                        if process is not None:
                            _terminate_process(process)
                    finally:
                        self._jobs.pop(key, None)
            finally:
                for result_path in (
                    job.result_path,
                    job.result_path.with_suffix(job.result_path.suffix + ".tmp"),
                ):
                    try:
                        result_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _read_result(self, result_path: Path) -> dict[str, Any] | None:
        try:
            with open(result_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _mark_parsing(self, job: _Job) -> bool:
        now = utc_now_iso()
        with self._write() as conn:
            cursor = conn.execute(
                """UPDATE manuscripts
                SET parse_status=?, parse_started_at=?, updated_at=?
                WHERE tenant_id=? AND manuscript_id=? AND parse_status=?""",
                (PARSE_PARSING, now, now, job.tenant_id, job.manuscript_id, PARSE_QUEUED),
            )
            return cursor.rowcount > 0

    def _finish_ready(self, job: _Job, payload: dict[str, Any], duration_ms: int) -> None:
        passages = payload.get("passages")
        if not isinstance(passages, list) or not passages:
            self._finish_failed(job, "MANUSCRIPT_EMPTY_TEXT", duration_ms, [])
            return
        now = utc_now_iso()
        warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
        with self._write() as conn:
            row = conn.execute(
                "SELECT parse_status FROM manuscripts WHERE tenant_id=? AND manuscript_id=?",
                (job.tenant_id, job.manuscript_id),
            ).fetchone()
            if row is None or row["parse_status"] != PARSE_PARSING:
                return
            conn.execute(
                "DELETE FROM manuscript_passages WHERE tenant_id=? AND manuscript_id=?",
                (job.tenant_id, job.manuscript_id),
            )
            conn.executemany(
                """INSERT INTO manuscript_passages (
                    tenant_id, manuscript_id, passage_index, page, paragraph_index,
                    section_path_json, char_start, char_end, text, text_sha256,
                    source_method, ocr_confidence, bbox_json, reading_order_confidence,
                    warnings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        job.tenant_id,
                        job.manuscript_id,
                        int(item["passage_index"]),
                        item.get("page"),
                        item.get("paragraph_index"),
                        _json(item.get("section_path") or []),
                        int(item["char_start"]),
                        int(item["char_end"]),
                        str(item["text"]),
                        str(item["text_sha256"]),
                        str(item["source_method"]),
                        item.get("ocr_confidence"),
                        _json(item.get("bbox")) if item.get("bbox") else None,
                        item.get("reading_order_confidence"),
                        _json(item.get("warnings") or []),
                    )
                    for item in passages
                ],
            )
            conn.execute(
                """UPDATE manuscripts SET
                    parse_status=?, page_count=?, passage_count=?, text_chars=?,
                    text_page_count=?, ocr_page_count=?, empty_page_count=?,
                    ocr_mean_confidence=?, warnings_json=?, error_code=NULL,
                    error_message=NULL, parsed_at=?, parse_duration_ms=?, updated_at=?
                WHERE tenant_id=? AND manuscript_id=? AND parse_status=?""",
                (
                    PARSE_READY,
                    int(payload.get("page_count") or 0),
                    len(passages),
                    int(payload.get("text_chars") or 0),
                    int(payload.get("text_page_count") or 0),
                    int(payload.get("ocr_page_count") or 0),
                    int(payload.get("empty_page_count") or 0),
                    payload.get("ocr_mean_confidence"),
                    _json(_merge_warnings(warnings)),
                    now,
                    duration_ms,
                    now,
                    job.tenant_id,
                    job.manuscript_id,
                    PARSE_PARSING,
                ),
            )

    def _finish_failed(
        self,
        job: _Job,
        code: str,
        duration_ms: int,
        warnings: list[dict[str, Any]],
    ) -> None:
        now = utc_now_iso()
        message = ERROR_MESSAGES.get(code, "稿件解析失败。")
        with self._write() as conn:
            conn.execute(
                """UPDATE manuscripts SET
                    parse_status=?, error_code=?, error_message=?, warnings_json=?,
                    parsed_at=?, parse_duration_ms=?, updated_at=?
                WHERE tenant_id=? AND manuscript_id=?""",
                (
                    PARSE_FAILED,
                    code,
                    message,
                    _json(_merge_warnings(warnings)),
                    now,
                    duration_ms,
                    now,
                    job.tenant_id,
                    job.manuscript_id,
                ),
            )

    def _cancel_job(self, tenant_id: str, manuscript_id: str) -> None:
        key = f"{tenant_id}\x1f{manuscript_id}"
        with self._jobs_lock:
            job = self._jobs.get(key)
            if job is not None:
                job.cancelled = True
                if job.process is not None:
                    _terminate_process(job.process)

    def get(
        self,
        tenant_id: str,
        manuscript_id: str,
        *,
        catalog_offset: int = 0,
        catalog_limit: int = 100,
    ) -> dict[str, Any] | None:
        tenant = _validate_tenant(tenant_id)
        if not _validate_manuscript_id(manuscript_id):
            return None
        catalog_offset = max(0, min(int(catalog_offset), 1_000_000))
        catalog_limit = max(0, min(int(catalog_limit), 500))
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM manuscripts WHERE tenant_id=? AND manuscript_id=?",
                (tenant, manuscript_id),
            ).fetchone()
            if row is None:
                return None
            catalog: list[dict[str, Any]] = []
            truncated = False
            if catalog_limit:
                rows = conn.execute(
                    """SELECT passage_index, page, paragraph_index, section_path_json,
                        char_start, char_end, text, source_method, ocr_confidence,
                        warnings_json
                    FROM manuscript_passages
                    WHERE tenant_id=? AND manuscript_id=?
                    ORDER BY passage_index ASC
                    LIMIT ? OFFSET ?""",
                    (tenant, manuscript_id, catalog_limit + 1, catalog_offset),
                ).fetchall()
                truncated = len(rows) > catalog_limit
                catalog = [_catalog_entry(item) for item in rows[:catalog_limit]]
        return _manifestation(row, catalog, truncated, catalog_offset)

    def list_catalog(
        self,
        tenant_id: str,
        manuscript_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        tenant = _validate_tenant(tenant_id)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 500))
        with self._read() as conn:
            row = conn.execute(
                "SELECT passage_count FROM manuscripts WHERE tenant_id=? AND manuscript_id=?",
                (tenant, manuscript_id),
            ).fetchone()
            if row is None:
                raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
            rows = conn.execute(
                """SELECT passage_index, page, paragraph_index, section_path_json,
                    char_start, char_end, text, source_method, ocr_confidence,
                    warnings_json
                FROM manuscript_passages
                WHERE tenant_id=? AND manuscript_id=?
                ORDER BY passage_index ASC
                LIMIT ? OFFSET ?""",
                (tenant, manuscript_id, limit + 1, offset),
            ).fetchall()
        catalog = [_catalog_entry(item) for item in rows[:limit]]
        return {
            "manuscript_id": manuscript_id,
            "offset": offset,
            "limit": limit,
            "passage_count": int(row["passage_count"]),
            "returned": len(catalog),
            "has_more": len(rows) > limit,
            "catalog": catalog,
        }

    def passages(
        self,
        tenant_id: str,
        manuscript_id: str,
        *,
        offset: int = 0,
        limit: int = 10,
    ) -> dict[str, Any]:
        tenant = _validate_tenant(tenant_id)
        if not _validate_manuscript_id(manuscript_id):
            raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 50))
        with self._read() as conn:
            row = conn.execute(
                "SELECT parse_status, passage_count, error_code, error_message FROM manuscripts "
                "WHERE tenant_id=? AND manuscript_id=?",
                (tenant, manuscript_id),
            ).fetchone()
            if row is None:
                raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
            status = row["parse_status"]
            if status != PARSE_READY:
                if status == PARSE_FAILED:
                    raise ManuscriptError(
                        409,
                        "MANUSCRIPT_PARSE_FAILED",
                        details={
                            "parse_status": status,
                            "error": {
                                "code": row["error_code"],
                                "message": row["error_message"],
                            },
                        },
                    )
                raise ManuscriptError(
                    409,
                    "MANUSCRIPT_NOT_READY",
                    details={"parse_status": status},
                )
            rows = conn.execute(
                """SELECT passage_index, page, paragraph_index, section_path_json,
                    char_start, char_end, text, text_sha256, source_method,
                    ocr_confidence, bbox_json, reading_order_confidence, warnings_json
                FROM manuscript_passages
                WHERE tenant_id=? AND manuscript_id=?
                ORDER BY passage_index ASC
                LIMIT ? OFFSET ?""",
                (tenant, manuscript_id, limit + 1, offset),
            ).fetchall()
        passages = [_passage_payload(item) for item in rows[:limit]]
        return {
            "manuscript_id": manuscript_id,
            "parse_status": status,
            "offset": offset,
            "limit": limit,
            "passage_count": int(row["passage_count"]),
            "returned": len(passages),
            "has_more": len(rows) > limit,
            "passages": passages,
        }

    def read_manuscript(
        self,
        tenant_id: str,
        manuscript_id: str,
        *,
        catalog_offset: int = 0,
        catalog_limit: int = 50,
        passage_offset: int = 0,
        passage_limit: int = 10,
    ) -> dict[str, Any]:
        """Bounded private passage access for the agent and API clients."""
        record = self.get(
            tenant_id,
            manuscript_id,
            catalog_offset=catalog_offset,
            catalog_limit=catalog_limit,
        )
        if record is None:
            raise ManuscriptError(404, "MANUSCRIPT_NOT_FOUND")
        if record["parse_status"] == PARSE_READY:
            record["passages"] = self.passages(
                tenant_id,
                manuscript_id,
                offset=passage_offset,
                limit=passage_limit,
            )["passages"]
        return record

    def wait_for_parse(
        self,
        tenant_id: str,
        manuscript_id: str,
        *,
        timeout_s: float = 120.0,
        interval_s: float = 0.1,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        record = None
        while True:
            record = self.get(tenant_id, manuscript_id, catalog_limit=0)
            if record is None or record["parse_status"] in TERMINAL_PARSE_STATES:
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(max(0.01, min(interval_s, 1.0)))

    def delete(self, tenant_id: str, manuscript_id: str) -> dict[str, Any]:
        tenant = _validate_tenant(tenant_id)
        if not _validate_manuscript_id(manuscript_id):
            return {"manuscript_id": manuscript_id, "deleted": True, "already_deleted": True}
        self._cancel_job(tenant, manuscript_id)
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM manuscripts WHERE tenant_id=? AND manuscript_id=?",
                (tenant, manuscript_id),
            ).fetchone()
            passage_count = 0
            if row is not None:
                count_row = conn.execute(
                    """SELECT COUNT(*) AS total FROM manuscript_passages
                    WHERE tenant_id=? AND manuscript_id=?""",
                    (tenant, manuscript_id),
                ).fetchone()
                passage_count = int(count_row["total"]) if count_row else 0
        if row is None:
            return {"manuscript_id": manuscript_id, "deleted": True, "already_deleted": True}
        directory = self._manuscript_dir(tenant, manuscript_id)
        failures: list[str] = []
        try:
            if directory.exists():
                shutil.rmtree(directory)
        except OSError:
            failures.append("original_or_derived_files")
        now = utc_now_iso()
        scope = {
            "original_files": not failures,
            "passages": passage_count,
            "derived": ["passages", "catalog", "parse_result"],
        }
        if failures:
            with self._write() as conn:
                conn.execute(
                    """INSERT INTO manuscript_deletions
                    (tenant_id, manuscript_id, deleted_at, scope_json, failure_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, manuscript_id) DO UPDATE SET
                        deleted_at=excluded.deleted_at,
                        scope_json=excluded.scope_json,
                        failure_json=excluded.failure_json""",
                    (tenant, manuscript_id, now, _json(scope), _json(failures)),
                )
            raise ManuscriptError(
                500,
                "MANUSCRIPT_DELETE_FAILED",
                details={"failure_items": failures},
                retryable=True,
            )
        with self._write() as conn:
            conn.execute(
                "DELETE FROM manuscript_passages WHERE tenant_id=? AND manuscript_id=?",
                (tenant, manuscript_id),
            )
            conn.execute(
                "DELETE FROM manuscript_upload_idempotency WHERE tenant_id=? AND manuscript_id=?",
                (tenant, manuscript_id),
            )
            conn.execute(
                "DELETE FROM manuscripts WHERE tenant_id=? AND manuscript_id=?",
                (tenant, manuscript_id),
            )
            conn.execute(
                """INSERT INTO manuscript_deletions
                (tenant_id, manuscript_id, deleted_at, scope_json, failure_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, manuscript_id) DO UPDATE SET
                    deleted_at=excluded.deleted_at,
                    scope_json=excluded.scope_json,
                    failure_json=excluded.failure_json""",
                (tenant, manuscript_id, now, _json(scope), _json([])),
            )
        return {
            "manuscript_id": manuscript_id,
            "deleted": True,
            "already_deleted": False,
            "receipt": {
                "deleted_at": now,
                "scope": scope,
                "failure_items": [],
            },
        }

    def close(self) -> None:
        with self._jobs_lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job.cancelled = True
                if job.process is not None:
                    _terminate_process(job.process)
        self._executor.shutdown(wait=True, cancel_futures=True)


def _manifestation(
    row: sqlite3.Row,
    catalog: list[dict[str, Any]],
    truncated: bool,
    catalog_offset: int,
) -> dict[str, Any]:
    warnings = _from_json(row["warnings_json"], [])
    error = None
    if row["parse_status"] == PARSE_FAILED:
        error = {
            "code": row["error_code"] or "MANUSCRIPT_CORRUPT",
            "message": row["error_message"] or ERROR_MESSAGES["MANUSCRIPT_CORRUPT"],
        }
    quality = {
        "reading_order_confidence": None,
        "reading_order_valid": False,
        "text_page_count": int(row["text_page_count"]),
        "ocr_page_count": int(row["ocr_page_count"]),
        "empty_page_count": int(row["empty_page_count"]),
        "ocr_mean_confidence": row["ocr_mean_confidence"],
        "warnings": [item.get("code") for item in warnings if item.get("code")],
    }
    return {
        "manuscript_id": row["manuscript_id"],
        "parse_status": row["parse_status"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "source_format": row["source_format"],
        "size_bytes": int(row["size_bytes"]),
        "sha256": row["sha256"],
        "page_count": int(row["page_count"]),
        "passage_count": int(row["passage_count"]),
        "text_chars": int(row["text_chars"]),
        "warnings": warnings,
        "error": error,
        "delete_after": row["delete_after"],
        "parser_version": row["parser_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "parsed_at": row["parsed_at"],
        "parse_duration_ms": row["parse_duration_ms"],
        "quality": quality,
        "catalog": catalog,
        "catalog_offset": catalog_offset,
        "catalog_truncated": truncated,
    }


def _catalog_entry(row: sqlite3.Row) -> dict[str, Any]:
    text = str(row["text"])
    preview = " ".join(text.split())[:160]
    return {
        "passage_index": int(row["passage_index"]),
        "page": row["page"],
        "paragraph_index": row["paragraph_index"],
        "section_path": _from_json(row["section_path_json"], []),
        "char_start": int(row["char_start"]),
        "char_end": int(row["char_end"]),
        "chars": len(text),
        "source_method": row["source_method"],
        "ocr_confidence": row["ocr_confidence"],
        "warnings": _from_json(row["warnings_json"], []),
        "preview": preview,
    }


def _passage_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "passage_index": int(row["passage_index"]),
        "page": row["page"],
        "paragraph_index": row["paragraph_index"],
        "section_path": _from_json(row["section_path_json"], []),
        "char_start": int(row["char_start"]),
        "char_end": int(row["char_end"]),
        "text": row["text"],
        "text_sha256": row["text_sha256"],
        "source_method": row["source_method"],
        "ocr_confidence": row["ocr_confidence"],
        "bbox": _from_json(row["bbox_json"], None),
        "reading_order_confidence": row["reading_order_confidence"],
        "warnings": _from_json(row["warnings_json"], []),
        "text_truncated": False,
    }


# ---------------------------------------------------------------------------
# Parsing (child process)
# ---------------------------------------------------------------------------


def _parse_worker(
    source_path: str,
    kind: str,
    result_path: str,
    options: dict[str, Any],
    ocr_enabled: bool,
) -> None:
    """Write parser results only to the private handoff file."""
    try:
        if kind == "pdf":
            payload = _parse_pdf(source_path, options, ocr_enabled)
        elif kind == "docx":
            payload = _parse_docx(source_path, options)
        else:
            raise _ParseFailure("MANUSCRIPT_UNSUPPORTED_TYPE")
        result: dict[str, Any] = {"ok": True, **payload}
    except _ParseFailure as exc:
        result = {
            "ok": False,
            "error_code": exc.code,
            "message": exc.message,
            "warnings": exc.warnings,
        }
    except BaseException:
        result = {
            "ok": False,
            "error_code": "MANUSCRIPT_CORRUPT",
            "message": ERROR_MESSAGES["MANUSCRIPT_CORRUPT"],
            "warnings": [],
        }
    try:
        temporary = f"{result_path}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, result_path)
    except OSError:
        pass


@dataclass(slots=True)
class _Line:
    text: str
    bbox: dict[str, float] | None
    height: float
    confidence: float | None = None


def _iter_line_ranges(text: str) -> Iterator[tuple[int, int]]:
    position = 0
    total = len(text)
    while position < total:
        match = re.search(r"[\r\n]", text[position:])
        if match is None:
            yield position, total
            return
        end = position + match.start()
        yield position, end
        separator = text[end : end + 2]
        position = end + (2 if separator == "\r\n" else 1)


def _utf16_units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _text_lines(
    textpage: Any,
    page_text: str,
    page_height: float,
    *,
    use_bbox: bool,
) -> list[_Line]:
    lines: list[_Line] = []
    char_index_from_text_index = None
    if use_bbox:
        from pypdfium2 import raw as pdfium_raw

        char_index_from_text_index = pdfium_raw.FPDFText_GetCharIndexFromTextIndex
    text_index = 0
    previous_end = 0
    for start, end in _iter_line_ranges(page_text):
        if char_index_from_text_index is not None:
            text_index += _utf16_units(page_text[previous_end:start])
        content = page_text[start:end].rstrip()
        bbox = None
        height = 0.0
        if char_index_from_text_index is not None and content.strip():
            bbox = _line_bbox(
                textpage,
                page_text,
                start,
                end,
                page_height,
                text_index,
                char_index_from_text_index,
            )
            if bbox is not None:
                height = bbox["y1"] - bbox["y0"]
        lines.append(_Line(text=content, bbox=bbox, height=height))
        if char_index_from_text_index is not None:
            text_index += _utf16_units(page_text[start:end])
            previous_end = end
    return lines


def _line_bbox(
    textpage: Any,
    page_text: str,
    start: int,
    end: int,
    page_height: float,
    text_index_start: int,
    char_index_from_text_index: Any,
) -> dict[str, float] | None:
    text_index = text_index_start
    indices = []
    for index in range(start, end):
        char = page_text[index]
        if not char.isspace():
            char_index = char_index_from_text_index(textpage, text_index)
            if char_index < 0:
                return None
            indices.append(char_index)
        text_index += 2 if ord(char) > 0xFFFF else 1
    if not indices:
        return None
    if len(indices) > 300:
        step = max(1, len(indices) // 200)
        indices = sorted({indices[0], indices[-1], *indices[::step]})
    xs: list[float] = []
    ys: list[float] = []
    for index in indices:
        try:
            x0, y0, x1, y1 = textpage.get_charbox(index)
        except Exception:  # noqa: S112 - one bad glyph must not drop the line
            continue
        values = (x0, y0, x1, y1)
        if not all(math.isfinite(float(value)) for value in values):
            continue
        if x1 <= x0 and y1 <= y0:
            continue
        xs.extend((float(x0), float(x1)))
        ys.extend((float(y0), float(y1)))
    if not xs or not ys:
        return None
    top = page_height - max(ys)
    bottom = page_height - min(ys)
    if bottom <= top:
        return None
    return {
        "x0": round(min(xs), 3),
        "y0": round(top, 3),
        "x1": round(max(xs), 3),
        "y1": round(bottom, 3),
    }


def _group_lines(lines: list[_Line]) -> list[list[_Line]]:
    heights = [line.height for line in lines if line.text.strip() and line.height > 0]
    median_height = statistics.median(heights) if heights else 12.0
    gap_threshold = max(median_height * 0.8, 6.0)
    indent_threshold = max(median_height * 0.9, 8.0)
    groups: list[list[_Line]] = []
    current: list[_Line] = []
    previous: _Line | None = None
    for line in lines:
        if not line.text.strip():
            if current:
                groups.append(current)
                current = []
            previous = None
            continue
        if line.bbox is None:
            if current:
                groups.append(current)
                current = []
            groups.append([line])
            previous = None
            continue
        if previous is not None and previous.bbox is not None:
            gap = line.bbox["y0"] - previous.bbox["y1"]
            indent = line.bbox["x0"] - previous.bbox["x0"]
            backwards = gap < -gap_threshold
            column_reset = indent < -max(median_height * 4, 24.0)
            if gap > gap_threshold or backwards or indent > indent_threshold or column_reset:
                if current:
                    groups.append(current)
                    current = []
        current.append(line)
        previous = line
    if current:
        groups.append(current)
    return groups


def _union_bbox(lines: list[_Line]) -> dict[str, float] | None:
    boxes = [line.bbox for line in lines if line.bbox is not None]
    if not boxes:
        return None
    return {
        "x0": round(min(box["x0"] for box in boxes), 3),
        "y0": round(min(box["y0"] for box in boxes), 3),
        "x1": round(max(box["x1"] for box in boxes), 3),
        "y1": round(max(box["y1"] for box in boxes), 3),
    }


def _chunk_text(text: str, limit: int) -> list[str]:
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            cut = text.rfind("\n", start, end)
            if cut <= start + limit // 3:
                cut = text.rfind(" ", start, end)
            if cut > start:
                end = cut
        chunk = text[start:end].strip("\n")
        if chunk.strip():
            chunks.append(chunk)
        start = end
        while start < len(text) and text[start] in "\r\n":
            start += 1
    return chunks


def _emit_groups(
    groups: list[list[_Line]],
    *,
    page: int | None,
    source_method: str,
    options: dict[str, Any],
    warnings: list[dict[str, Any]],
    section_path: list[str] | None = None,
) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []
    for paragraph_index, group in enumerate(groups):
        text = "\n".join(line.text.rstrip() for line in group).strip("\n")
        if not text.strip():
            continue
        confidences = [line.confidence for line in group if line.confidence is not None]
        confidence = round(sum(confidences) / len(confidences), 4) if confidences else None
        chunks = _chunk_text(text, int(options["max_passage_chars"]))
        if len(chunks) > 1:
            warnings.append(_warning("passage_split"))
        passage_warnings = (
            ["ocr_text_unverified"] if source_method == "pdf_ocr" else ["reading_order_unverified"]
        )
        if any(line.bbox is None for line in group):
            passage_warnings.append("bbox_unavailable")
        for chunk in chunks:
            emitted.append(
                {
                    "page": page,
                    "paragraph_index": paragraph_index,
                    "section_path": list(section_path or []),
                    "text": chunk,
                    "source_method": source_method,
                    "ocr_confidence": confidence,
                    "bbox": _bbox_payload(page, _union_bbox(group)),
                    "reading_order_confidence": None,
                    "warnings": passage_warnings,
                }
            )
    return emitted


def _bbox_payload(page: int | None, bbox: dict[str, float] | None) -> dict[str, Any] | None:
    if bbox is None or page is None:
        return None
    return {
        "page": page,
        "x0": bbox["x0"],
        "y0": bbox["y0"],
        "x1": bbox["x1"],
        "y1": bbox["y1"],
        "origin": "top_left",
    }


def _finalize_passages(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    cursor = 0
    finalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        text = str(item["text"])
        entry = dict(item)
        entry["passage_index"] = index
        entry["char_start"] = cursor
        entry["char_end"] = cursor + len(text)
        entry["text_sha256"] = _sha256_bytes(text.encode("utf-8"))
        finalized.append(entry)
        cursor += len(text) + 2
    return finalized, cursor


def _pdf_failure(exc: BaseException) -> _ParseFailure:
    message = str(exc).lower()
    if "password" in message or "encrypt" in message or "security" in message:
        return _ParseFailure("MANUSCRIPT_ENCRYPTED")
    return _ParseFailure("MANUSCRIPT_CORRUPT")


def _parse_pdf(source_path: str, options: dict[str, Any], ocr_enabled: bool) -> dict[str, Any]:
    import numpy as np
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(source_path)
    except Exception as exc:
        raise _pdf_failure(exc) from exc
    raw_passages: list[dict[str, Any]] = []
    text_chars = 0
    warnings: list[dict[str, Any]] = []
    text_page_count = 0
    ocr_page_count = 0
    empty_page_count = 0
    ocr_confidences: list[float] = []
    ocr_engine: Any = None
    ocr_failed = False
    try:
        page_count = len(document)
        if page_count <= 0:
            raise _ParseFailure("MANUSCRIPT_EMPTY_TEXT")
        if page_count > int(options["max_pages"]):
            raise _ParseFailure("MANUSCRIPT_PAGE_LIMIT")
        for page_index in range(page_count):
            page = document[page_index]
            try:
                page_number = page_index + 1
                width, height = page.get_size()
                try:
                    rotation = int(page.get_rotation()) % 360
                except Exception:
                    rotation = 0
                usable_bbox = rotation == 0
                textpage = None
                page_text = ""
                text_index_ok = True
                try:
                    textpage = page.get_textpage()
                    page_text = textpage.get_text_range() or ""
                    text_index_ok = textpage.count_chars() == len(page_text)
                except Exception as exc:
                    if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
                        raise _pdf_failure(exc) from exc
                    page_text = ""
                lines = _text_lines(
                    textpage,
                    page_text,
                    float(height),
                    use_bbox=usable_bbox and textpage is not None,
                )
                if textpage is not None:
                    try:
                        textpage.close()
                    except Exception:  # noqa: S110 - best-effort resource release
                        pass
                if not text_index_ok and page_text.strip():
                    warnings.append(_warning("char_index_mismatch"))
                non_whitespace = sum(1 for char in page_text if not char.isspace())
                if non_whitespace >= int(options["ocr_min_text_chars"]):
                    text_page_count += 1
                    groups = _group_lines(lines)
                    page_passages = _emit_groups(
                        groups,
                        page=page_number,
                        source_method="pdf_text",
                        options=options,
                        warnings=warnings,
                    )
                    if page_text and not page_passages:
                        warnings.append(_warning("empty_page"))
                        empty_page_count += 1
                    raw_passages.extend(page_passages)
                    text_chars += sum(len(item["text"]) + 2 for item in page_passages)
                else:
                    if not ocr_enabled:
                        warnings.append(_warning("ocr_unavailable"))
                        empty_page_count += 1
                    elif ocr_page_count >= int(options["max_ocr_pages"]):
                        warnings.append(_warning("ocr_page_limit"))
                        empty_page_count += 1
                    elif not usable_bbox:
                        warnings.append(_warning("page_rotation"))
                        empty_page_count += 1
                    elif width <= 0 or height <= 0:
                        warnings.append(_warning("page_pixels_exceeded"))
                        empty_page_count += 1
                    else:
                        pixels = float(width) * float(height)
                        scale = min(
                            float(options["ocr_render_scale"]),
                            math.sqrt(float(options["max_page_pixels"]) / pixels),
                        )
                        if scale < 1.0:
                            warnings.append(_warning("page_pixels_exceeded"))
                            empty_page_count += 1
                        else:
                            if ocr_engine is None and not ocr_failed:
                                try:
                                    from rapidocr_onnxruntime import RapidOCR

                                    ocr_engine = RapidOCR()
                                except Exception:
                                    ocr_failed = True
                                    ocr_engine = None
                            if ocr_engine is None:
                                warnings.append(_warning("ocr_unavailable"))
                                empty_page_count += 1
                            else:
                                bitmap = page.render(scale=scale)
                                try:
                                    image = np.array(bitmap.to_pil().convert("RGB"))
                                finally:
                                    bitmap.close()
                                try:
                                    result, _ = ocr_engine(image)
                                except Exception:
                                    result = None
                                ocr_lines = _ocr_lines(result, scale)
                                if ocr_lines:
                                    ocr_page_count += 1
                                    ocr_confidences.extend(
                                        line.confidence
                                        for line in ocr_lines
                                        if line.confidence is not None
                                    )
                                    groups = _group_lines(ocr_lines)
                                    page_passages = _emit_groups(
                                        groups,
                                        page=page_number,
                                        source_method="pdf_ocr",
                                        options=options,
                                        warnings=warnings,
                                    )
                                    raw_passages.extend(page_passages)
                                    text_chars += sum(
                                        len(item["text"]) + 2 for item in page_passages
                                    )
                                else:
                                    warnings.append(_warning("empty_page"))
                                    empty_page_count += 1
                if len(raw_passages) > int(options["max_passages"]):
                    raise _ParseFailure(
                        "MANUSCRIPT_PASSAGE_LIMIT", warnings=_merge_warnings(warnings)
                    )
                if text_chars > int(options["max_text_chars"]):
                    raise _ParseFailure("MANUSCRIPT_TEXT_LIMIT", warnings=_merge_warnings(warnings))
            finally:
                try:
                    page.close()
                except Exception:  # noqa: S110 - best-effort resource release
                    pass
        if not raw_passages:
            if ocr_failed:
                raise _ParseFailure(
                    "MANUSCRIPT_OCR_UNAVAILABLE", warnings=_merge_warnings(warnings)
                )
            raise _ParseFailure("MANUSCRIPT_EMPTY_TEXT", warnings=_merge_warnings(warnings))
    finally:
        try:
            document.close()
        except Exception:  # noqa: S110 - best-effort resource release
            pass

    warnings.extend(
        [
            _warning("reading_order_unverified"),
            _warning("line_breaks_preserved"),
        ]
    )
    if ocr_page_count:
        warnings.append(_warning("ocr_text_unverified", count=ocr_page_count))
        threshold = float(options["ocr_low_confidence"])
        if any(confidence < threshold for confidence in ocr_confidences):
            warnings.append(_warning("ocr_low_confidence"))
    if empty_page_count:
        warnings.append(_warning("empty_page", count=empty_page_count))
    ocr_mean = round(sum(ocr_confidences) / len(ocr_confidences), 4) if ocr_confidences else None
    passages, canonical_chars = _finalize_passages(raw_passages)
    return {
        "page_count": page_count,
        "passages": passages,
        "warnings": _merge_warnings(warnings),
        "text_chars": canonical_chars,
        "text_page_count": text_page_count,
        "ocr_page_count": ocr_page_count,
        "empty_page_count": empty_page_count,
        "ocr_mean_confidence": ocr_mean,
    }


def _ocr_lines(result: Any, scale: float) -> list[_Line]:
    lines: list[_Line] = []
    for item in result or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        box = item[0]
        raw_text = str(item[1]).strip()
        if not raw_text or not isinstance(box, (list, tuple)) or len(box) < 4:
            continue
        try:
            xs = [float(point[0]) / scale for point in box]
            ys = [float(point[1]) / scale for point in box]
        except (TypeError, ValueError, IndexError):
            continue
        if not xs or not ys:
            continue
        confidence = None
        if len(item) > 2 and item[2] is not None:
            try:
                confidence = float(item[2])
            except (TypeError, ValueError):
                confidence = None
        bbox = {
            "x0": round(min(xs), 3),
            "y0": round(min(ys), 3),
            "x1": round(max(xs), 3),
            "y1": round(max(ys), 3),
        }
        lines.append(
            _Line(
                text=raw_text,
                bbox=bbox,
                height=max(0.0, bbox["y1"] - bbox["y0"]),
                confidence=confidence,
            )
        )
    lines.sort(
        key=lambda line: (
            round(line.bbox["y0"], 1) if line.bbox else 0.0,
            line.bbox["x0"] if line.bbox else 0.0,
        )
    )
    return lines


# ---------------------------------------------------------------------------
# DOCX parsing
# ---------------------------------------------------------------------------


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paragraph_style(paragraph: Any) -> str | None:
    for child in paragraph:
        if _local_name(child.tag) != "pPr":
            continue
        for item in child:
            if _local_name(item.tag) == "pStyle":
                for key, value in item.attrib.items():
                    if _local_name(key) == "val":
                        return str(value)
        for item in child:
            if _local_name(item.tag) == "outlineLvl":
                for key, value in item.attrib.items():
                    if _local_name(key) == "val":
                        return f"outline:{value}"
    return None


def _heading_level(style: str | None, style_names: dict[str, str]) -> int | None:
    if not style:
        return None
    if style.startswith("outline:"):
        try:
            level = int(style.split(":", 1)[1]) + 1
        except ValueError:
            return None
        return level if 1 <= level <= 6 else None
    candidates = [style, style_names.get(style, "")]
    for candidate in candidates:
        normalized = re.sub(r"[\s_-]+", "", candidate).lower()
        match = re.fullmatch(r"(?:heading|标题)([1-6])", normalized)
        if match:
            return int(match.group(1))
    return None


def _paragraph_text(paragraph: Any) -> str:
    parts: list[str] = []

    def walk(element: Any) -> None:
        for child in element:
            name = _local_name(child.tag)
            if name in {"del", "instrText", "delText", "drawing", "pict", "object"}:
                continue
            if name == "t":
                parts.append(child.text or "")
            elif name == "tab":
                parts.append("\t")
            elif name in {"br", "cr"}:
                parts.append("\n")
            elif name == "noBreakHyphen":
                parts.append("-")
            else:
                walk(child)

    walk(paragraph)
    text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    return text


def _iter_paragraphs(element: Any) -> Iterator[Any]:
    for child in element:
        name = _local_name(child.tag)
        if name == "p":
            yield child
        elif name == "del":
            continue
        else:
            yield from _iter_paragraphs(child)


def _docx_body(root: Any) -> Any:
    for child in root:
        if _local_name(child.tag) == "body":
            return child
    return root


def _validate_docx_archive(infos: list[zipfile.ZipInfo], options: dict[str, Any]) -> None:
    if len(infos) > int(options["docx_max_entries"]):
        raise _ParseFailure("MANUSCRIPT_DECOMPRESSION_LIMIT")
    total = 0
    seen: set[str] = set()
    for info in infos:
        name = info.filename or ""
        if not name or "\x00" in name:
            raise _ParseFailure("MANUSCRIPT_UNSAFE_ARCHIVE")
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise _ParseFailure("MANUSCRIPT_UNSAFE_ARCHIVE")
        parts = [part for part in normalized.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise _ParseFailure("MANUSCRIPT_UNSAFE_ARCHIVE")
        if normalized.casefold() in seen:
            raise _ParseFailure("MANUSCRIPT_UNSAFE_ARCHIVE")
        seen.add(normalized.casefold())
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise _ParseFailure("MANUSCRIPT_UNSAFE_ARCHIVE")
        if info.flag_bits & 0x1:
            raise _ParseFailure("MANUSCRIPT_ENCRYPTED")
        if info.file_size > int(options["docx_max_entry_bytes"]):
            raise _ParseFailure("MANUSCRIPT_DECOMPRESSION_LIMIT")
        total += int(info.file_size)
        if total > int(options["docx_max_uncompressed_bytes"]):
            raise _ParseFailure("MANUSCRIPT_DECOMPRESSION_LIMIT")
        if info.file_size > 1024 * 1024:
            if info.compress_size <= 0:
                raise _ParseFailure("MANUSCRIPT_DECOMPRESSION_LIMIT")
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > int(options["docx_max_compression_ratio"]):
                raise _ParseFailure("MANUSCRIPT_DECOMPRESSION_LIMIT")


def _read_docx_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with archive.open(info) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _ParseFailure("MANUSCRIPT_DECOMPRESSION_LIMIT")
    return data


_XML_ENCODING_RE = re.compile(
    rb"<\?xml\s+.*?\bencoding\s*=\s*(['\"])([^'\"]+)\1",
    re.IGNORECASE | re.DOTALL,
)
_XML_FORBIDDEN_DECLARATION_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b")


def _decode_xml_for_security(data: bytes) -> str:
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding = "utf-32"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif data.startswith(b"\x00\x00\x00<"):
        encoding = "utf-32-be"
    elif data.startswith(b"<\x00\x00\x00"):
        encoding = "utf-32-le"
    elif data.startswith(b"\x00<\x00"):
        encoding = "utf-16-be"
    elif data.startswith(b"<\x00"):
        encoding = "utf-16-le"
    else:
        declaration = _XML_ENCODING_RE.search(data[:1024])
        encoding = declaration.group(2).decode("ascii") if declaration else "utf-8-sig"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeError) as exc:
        raise _ParseFailure("MANUSCRIPT_UNSAFE_XML") from exc


def _contains_unsafe_xml_declaration(text: str) -> bool:
    position = 0
    while True:
        start = text.find("<", position)
        if start < 0:
            return False
        if text.startswith("<!--", start):
            end = text.find("-->", start + 4)
            if end < 0:
                return False
            position = end + 3
        elif text.startswith("<![CDATA[", start):
            end = text.find("]]>", start + 9)
            if end < 0:
                return False
            position = end + 3
        elif text.startswith("<?", start):
            end = text.find("?>", start + 2)
            if end < 0:
                return False
            position = end + 2
        elif _XML_FORBIDDEN_DECLARATION_RE.match(text, start):
            return True
        else:
            position = start + 1


def _reject_unsafe_xml(data: bytes) -> None:
    if _contains_unsafe_xml_declaration(_decode_xml_for_security(data)):
        raise _ParseFailure("MANUSCRIPT_UNSAFE_XML")


def _validate_docx_relationships(data: bytes) -> int:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)  # noqa: S314 - unsafe declarations are rejected first
    except ET.ParseError as exc:
        raise _ParseFailure("MANUSCRIPT_CORRUPT") from exc
    external_hyperlinks = 0
    for relationship in root.iter():
        if _local_name(relationship.tag) != "Relationship":
            continue
        attributes = {key.rsplit("}", 1)[-1]: value for key, value in relationship.attrib.items()}
        target = str(attributes.get("Target") or "")
        target_mode = str(attributes.get("TargetMode") or "").strip().casefold()
        external = bool(
            target_mode == "external"
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)
            or target.startswith(("//", "\\\\"))
        )
        if not external:
            continue
        relation_type = str(attributes.get("Type") or "").rstrip("/").rsplit("/", 1)[-1]
        if relation_type.casefold() != "hyperlink":
            raise _ParseFailure("MANUSCRIPT_EXTERNAL_LINK")
        external_hyperlinks += 1
    return external_hyperlinks


def _validate_docx_unsupported_structure(infos: list[zipfile.ZipInfo]) -> None:
    names = {item.filename.replace("\\", "/").casefold() for item in infos}
    if any(name.startswith("word/vbaproject") for name in names):
        raise _ParseFailure("MANUSCRIPT_UNSUPPORTED_STRUCTURE")
    if any(name == "word/embeddings" or name.startswith("word/embeddings/") for name in names):
        raise _ParseFailure("MANUSCRIPT_UNSUPPORTED_STRUCTURE")
    if any(name == "word/activex" or name.startswith("word/activex/") for name in names):
        raise _ParseFailure("MANUSCRIPT_UNSUPPORTED_STRUCTURE")
    if any(name.startswith("word/oleobject") for name in names):
        raise _ParseFailure("MANUSCRIPT_UNSUPPORTED_STRUCTURE")


def _validate_docx_package(
    archive: zipfile.ZipFile,
    options: dict[str, Any],
) -> tuple[dict[str, bytes], int]:
    infos = archive.infolist()
    _validate_docx_archive(infos, options)
    names = {info.filename.replace("\\", "/"): info for info in infos}
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise _ParseFailure("MANUSCRIPT_CORRUPT")

    xml_parts: dict[str, bytes] = {}
    relationship_infos: list[zipfile.ZipInfo] = []
    for info in infos:
        name = info.filename.replace("\\", "/")
        lowered = name.casefold()
        if not lowered.endswith((".xml", ".rels")):
            continue
        data = _read_docx_entry(archive, info, int(options["docx_max_entry_bytes"]))
        _reject_unsafe_xml(data)
        if lowered == "[content_types].xml":
            if "macroenabled" in _decode_xml_for_security(data).casefold():
                raise _ParseFailure("MANUSCRIPT_UNSUPPORTED_STRUCTURE")
        if name in {"word/document.xml", "word/styles.xml"}:
            xml_parts[name] = data
        if lowered.endswith(".rels"):
            relationship_infos.append(info)

    external_hyperlinks = 0
    for info in relationship_infos:
        data = _read_docx_entry(archive, info, int(options["docx_max_entry_bytes"]))
        external_hyperlinks += _validate_docx_relationships(data)
    _validate_docx_unsupported_structure(infos)
    return xml_parts, external_hyperlinks


def _docx_style_names(data: bytes | None) -> dict[str, str]:
    import xml.etree.ElementTree as ET

    if data is None:
        return {}
    try:
        root = ET.fromstring(data)  # noqa: S314 - all package XML is preflighted first
    except ET.ParseError:
        return {}
    names: dict[str, str] = {}
    for style in root.iter():
        if _local_name(style.tag) != "style":
            continue
        style_id = None
        for key, value in style.attrib.items():
            if _local_name(key) == "styleId":
                style_id = str(value)
        name = None
        for child in style:
            if _local_name(child.tag) == "name":
                for key, value in child.attrib.items():
                    if _local_name(key) == "val":
                        name = str(value)
        if style_id and name:
            names[style_id] = name
    return names


def _parse_docx(source_path: str, options: dict[str, Any]) -> dict[str, Any]:
    import xml.etree.ElementTree as ET

    try:
        archive = zipfile.ZipFile(source_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise _ParseFailure("MANUSCRIPT_CORRUPT") from exc
    try:
        xml_parts, external_hyperlinks = _validate_docx_package(archive, options)
    finally:
        archive.close()
    data = xml_parts["word/document.xml"]
    try:
        root = ET.fromstring(data)  # noqa: S314 - all package XML is preflighted first
    except ET.ParseError as exc:
        raise _ParseFailure("MANUSCRIPT_CORRUPT") from exc
    style_names = _docx_style_names(xml_parts.get("word/styles.xml"))

    warnings: list[dict[str, Any]] = [_warning("docx_no_pdf_pages")]
    if external_hyperlinks:
        warnings.append(_warning("ignored_external_link", count=external_hyperlinks))
    raw_passages: list[dict[str, Any]] = []
    section_path: list[str] = []
    paragraph_index = 0
    text_chars = 0
    for paragraph in _iter_paragraphs(_docx_body(root)):
        text = _paragraph_text(paragraph)
        style = _paragraph_style(paragraph)
        level = _heading_level(style, style_names)
        heading_text = " ".join(text.split())
        if level is not None and heading_text:
            section_path = section_path[: level - 1] + [heading_text[:200]]
        if text.strip():
            chunks = _chunk_text(text, int(options["max_passage_chars"]))
            if len(chunks) > 1:
                warnings.append(_warning("passage_split"))
            for chunk in chunks:
                raw_passages.append(
                    {
                        "page": None,
                        "paragraph_index": paragraph_index,
                        "section_path": list(section_path),
                        "text": chunk,
                        "source_method": "docx_xml",
                        "ocr_confidence": None,
                        "bbox": None,
                        "reading_order_confidence": None,
                        "warnings": ["docx_no_pdf_pages"],
                    }
                )
                text_chars += len(chunk) + 2
                if text_chars > int(options["max_text_chars"]):
                    raise _ParseFailure("MANUSCRIPT_TEXT_LIMIT", warnings=_merge_warnings(warnings))
                if len(raw_passages) > int(options["max_passages"]):
                    raise _ParseFailure(
                        "MANUSCRIPT_PASSAGE_LIMIT", warnings=_merge_warnings(warnings)
                    )
        paragraph_index += 1
    if not raw_passages:
        raise _ParseFailure("MANUSCRIPT_EMPTY_TEXT", warnings=_merge_warnings(warnings))
    passages, canonical_chars = _finalize_passages(raw_passages)
    return {
        "page_count": 0,
        "passages": passages,
        "warnings": _merge_warnings(warnings),
        "text_chars": canonical_chars,
        "text_page_count": 0,
        "ocr_page_count": 0,
        "empty_page_count": 0,
        "ocr_mean_confidence": None,
    }
