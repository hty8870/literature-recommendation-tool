"""Tenant-scoped HTTP access to manuscript parsing and lifecycle operations."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

from .manuscript_scan import ManuscriptScanError, ManuscriptScanner, ScanReceipt, scan_capability
from .manuscripts import (
    DOCX_CONTENT_TYPE,
    ERROR_MESSAGES,
    PDF_CONTENT_TYPE,
    ManuscriptError,
    ManuscriptService,
    _ParseFailure,
    _validate_docx_package,
)

TenantResolver = Callable[[Request], str | Awaitable[str]]


def _error(
    status: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
) -> ManuscriptError:
    return ManuscriptError(status, code, message, details=details, retryable=retryable)


async def _resolve_tenant(request: Request, tenant_resolver: TenantResolver) -> str:
    value = tenant_resolver(request)
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, str) or not value.strip():
        raise _error(401, "AUTH_REQUIRED", "需要有效的租户身份。")
    return value.strip()


def _decode_filename(request: Request) -> str | None:
    raw = request.headers.get("x-filename")
    if raw is None:
        return None
    if len(raw) > 2048:
        raise _error(400, "VALIDATION_ERROR", "X-Filename 过长。")
    try:
        decoded = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error(
            400,
            "VALIDATION_ERROR",
            "X-Filename 不是合法的 UTF-8 百分号编码。",
        ) from exc
    decoded = decoded.strip()
    return decoded or None


def _default_filename(content_type: str | None) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized == PDF_CONTENT_TYPE:
        return "manuscript.pdf"
    if normalized == DOCX_CONTENT_TYPE:
        return "manuscript.docx"
    raise _error(
        400,
        "VALIDATION_ERROR",
        "缺少 X-Filename，且 Content-Type 无法推断文件格式。",
    )


def _validate_declared_format(filename: str, content_type: str | None) -> None:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix and suffix not in {"pdf", "docx"}:
        raise _error(
            415,
            "MANUSCRIPT_UNSUPPORTED_TYPE",
            "仅支持 PDF 与 DOCX 文件。",
        )
    if normalized == PDF_CONTENT_TYPE and suffix == "docx":
        raise _error(
            415,
            "MANUSCRIPT_INVALID_SIGNATURE",
            "文件名扩展名与 PDF 内容类型不一致。",
        )
    if normalized == DOCX_CONTENT_TYPE and suffix == "pdf":
        raise _error(
            415,
            "MANUSCRIPT_INVALID_SIGNATURE",
            "文件名扩展名与 DOCX 内容类型不一致。",
        )


class _UploadBody:
    """Bounded upload body; stays in memory only for small files."""

    def __init__(self, handle, size: int) -> None:  # noqa: ANN001
        self.handle = handle
        self.size = size

    def chunks(self, chunk_size: int = 1024 * 1024):  # noqa: ANN201
        self.handle.seek(0)
        while True:
            chunk = self.handle.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        try:
            self.handle.close()
        except OSError:
            raise _error(
                500,
                "MANUSCRIPT_STORAGE_ERROR",
                ERROR_MESSAGES["MANUSCRIPT_STORAGE_ERROR"],
            ) from None


async def _run_sync(func, *args, **kwargs):  # noqa: ANN001, ANN201
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        with suppress(Exception):
            worker.result()
        raise asyncio.CancelledError
    return worker.result()


async def _read_limited_body(
    request: Request,
    limit: int,
    *,
    temp_dir: Path,
) -> _UploadBody:
    content_encoding = (request.headers.get("content-encoding") or "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise _error(415, "MANUSCRIPT_UNSUPPORTED_ENCODING", "不支持压缩的上传正文。")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise _error(400, "VALIDATION_ERROR", "Content-Length 无效。") from exc
        if declared_length > limit:
            raise _error(
                413,
                "MANUSCRIPT_TOO_LARGE",
                "稿件大小超过上传上限。",
                details={"size_bytes": declared_length, "limit": limit},
            )
    try:
        handle = tempfile.SpooledTemporaryFile(
            max_size=1024 * 1024,
            mode="w+b",
            dir=temp_dir,
        )
    except OSError:
        raise _error(
            500,
            "MANUSCRIPT_STORAGE_ERROR",
            ERROR_MESSAGES["MANUSCRIPT_STORAGE_ERROR"],
        ) from None
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise _error(
                    413,
                    "MANUSCRIPT_TOO_LARGE",
                    "稿件大小超过上传上限。",
                    details={"size_bytes": total, "limit": limit},
                )
            written = await _run_sync(handle.write, chunk)
            if written != len(chunk):
                raise _error(
                    500,
                    "MANUSCRIPT_STORAGE_ERROR",
                    ERROR_MESSAGES["MANUSCRIPT_STORAGE_ERROR"],
                )
        if not total:
            raise _error(400, "MANUSCRIPT_EMPTY_UPLOAD", "上传内容为空。")
        await _run_sync(handle.seek, 0)
        return _UploadBody(handle, total)
    except BaseException:
        with suppress(OSError):
            await _run_sync(handle.close)
        raise


def _validate_docx_security(
    body: bytes | _UploadBody,
    *,
    service: ManuscriptService,
    filename: str | None,
    content_type: str | None,
) -> None:
    """Preflight DOCX structure without opening external relationships."""
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = (filename or "").rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    if normalized != DOCX_CONTENT_TYPE and suffix != "docx":
        return
    if isinstance(body, _UploadBody):
        stream: io.IOBase = body.handle
    else:
        stream = io.BytesIO(body)
    stream.seek(0)
    head = stream.read(8)
    stream.seek(0)
    if not head.startswith(b"PK\x03\x04"):
        return
    try:
        archive = zipfile.ZipFile(stream)
    except zipfile.BadZipFile:
        return
    try:
        with archive:
            _validate_docx_package(archive, service.limits.as_parse_options())
    except _ParseFailure as exc:
        raise _error(422, exc.code, exc.message) from None
    except Exception:
        raise _error(
            422,
            "MANUSCRIPT_CORRUPT",
            ERROR_MESSAGES["MANUSCRIPT_CORRUPT"],
        ) from None
    else:
        stream.seek(0)


def _validate_upload_security(
    body: bytes | _UploadBody,
    *,
    service: ManuscriptService,
    filename: str | None,
    content_type: str | None,
) -> None:
    _validate_docx_security(
        body,
        service=service,
        filename=filename,
        content_type=content_type,
    )


def _scan_upload(body: _UploadBody, scanner: ManuscriptScanner | None) -> dict[str, Any]:
    if scanner is None:
        return {
            "status": "not_scanned",
            "notice": "开发模式未进行恶意文件扫描，仅限公开试用资料。",
        }
    size = 0
    digest = hashlib.sha256()

    def measured_chunks():
        nonlocal size
        for chunk in body.chunks():
            size += len(chunk)
            digest.update(chunk)
            yield chunk

    try:
        receipt = scanner.scan(measured_chunks(), max_bytes=body.size)
        if (
            not isinstance(receipt, ScanReceipt)
            or size != body.size
            or receipt.size_bytes != size
            or receipt.input_sha256 != digest.hexdigest()
        ):
            raise ManuscriptScanError(
                "MANUSCRIPT_SCAN_INVALID_RESPONSE", "恶意文件扫描结果与上传内容不一致，未接收稿件。"
            )
        return {
            **receipt.as_dict(),
            "notice": "本次扫描未发现已知恶意特征，不代表文件绝对安全。",
        }
    except ManuscriptScanError as exc:
        raise _error(exc.status, exc.code, exc.message, retryable=exc.retryable) from None
    except Exception:
        raise _error(
            503, "MANUSCRIPT_SCAN_UNAVAILABLE", "恶意文件扫描不可用，暂不接收全文。", retryable=True
        ) from None


def _save_upload(
    service: ManuscriptService,
    tenant_id: str,
    body: bytes | _UploadBody,
    filename: str,
    content_type: str | None,
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if isinstance(body, _UploadBody):
        size = body.size
        chunks = body.chunks()
    else:
        size = len(body)
        chunks = [body]
    return service.save_upload(
        tenant_id,
        filename,
        content_type or "application/octet-stream",
        chunks,
        declared_size=size,
        idempotency_key=idempotency_key,
    )


def _get_record(
    service: ManuscriptService,
    tenant_id: str,
    manuscript_id: str,
    *,
    catalog_offset: int = 0,
    catalog_limit: int = 100,
) -> dict[str, Any] | None:
    return service.get(
        tenant_id, manuscript_id, catalog_offset=catalog_offset, catalog_limit=catalog_limit
    )


def _get_passages(
    service: ManuscriptService,
    tenant_id: str,
    manuscript_id: str,
    *,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    return service.passages(tenant_id, manuscript_id, offset=offset, limit=limit)


def http_resource_path(request: Request, path: str) -> str:
    root_path = (request.scope.get("root_path") or "").rstrip("/")
    return f"{root_path}{path}"


def _no_store(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


def build_manuscript_router(
    service: ManuscriptService,
    tenant_resolver: TenantResolver,
    *,
    uploads_enabled: bool = True,
    scan_required: bool = False,
    scanner: ManuscriptScanner | None = None,
    delete_handler: Callable[[str, str], Awaitable[object]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/manuscripts", tags=["manuscripts"])
    uploads = asyncio.Semaphore(2)

    @router.get("/capability")
    async def upload_capability(request: Request) -> JSONResponse:
        await _resolve_tenant(request, tenant_resolver)
        capability = await asyncio.to_thread(
            scan_capability,
            uploads_enabled=uploads_enabled,
            required=scan_required,
            scanner=scanner,
        )
        return _no_store({**capability, "max_upload_bytes": service.limits.max_upload_bytes})

    @router.post("", status_code=202)
    async def upload_manuscript(request: Request) -> JSONResponse:
        tenant_id = await _resolve_tenant(request, tenant_resolver)
        if not uploads_enabled:
            raise _error(403, "MANUSCRIPT_UPLOAD_DISABLED", "当前服务未开放全文上传。")
        if scan_required and scanner is None:
            raise _error(
                503,
                "MANUSCRIPT_SCAN_UNAVAILABLE",
                "恶意文件扫描不可用，暂不接收全文。",
                retryable=True,
            )
        if uploads.locked():
            raise _error(429, "MANUSCRIPT_CAPACITY", "上传任务较多，请稍后重试。", retryable=True)
        async with uploads:
            try:
                async with asyncio.timeout(120):
                    filename = _decode_filename(request) or _default_filename(
                        request.headers.get("content-type")
                    )
                    _validate_declared_format(filename, request.headers.get("content-type"))
                    body = await _read_limited_body(
                        request,
                        service.limits.max_upload_bytes,
                        temp_dir=service._upload_temp_dir,
                    )
                    try:
                        scan_receipt = await _run_sync(_scan_upload, body, scanner)
                        await _run_sync(
                            _validate_upload_security,
                            body,
                            service=service,
                            filename=filename,
                            content_type=request.headers.get("content-type"),
                        )
                        record = await _run_sync(
                            _save_upload,
                            service,
                            tenant_id,
                            body,
                            filename,
                            request.headers.get("content-type"),
                            idempotency_key=request.headers.get("idempotency-key"),
                        )
                    finally:
                        await _run_sync(body.close)
            except TimeoutError as exc:
                raise _error(408, "MANUSCRIPT_UPLOAD_TIMEOUT", "上传或安全校验超时。") from exc
        manuscript_id = record["manuscript_id"]
        status_url = http_resource_path(request, f"/api/v1/manuscripts/{manuscript_id}")
        response_payload = {
            **record,
            "security_scan": scan_receipt,
            "status_url": status_url,
        }
        return JSONResponse(
            status_code=202,
            content=response_payload,
            headers={
                "Cache-Control": "no-store",
                "Location": status_url,
            },
        )

    @router.get("/{manuscript_id}")
    async def get_manuscript(
        request: Request,
        manuscript_id: str,
        catalog_offset: int = Query(0, ge=0, le=1_000_000),
        catalog_limit: int = Query(100, ge=0, le=500),
    ) -> JSONResponse:
        tenant_id = await _resolve_tenant(request, tenant_resolver)
        payload = await asyncio.to_thread(
            _get_record,
            service,
            tenant_id,
            manuscript_id,
            catalog_offset=catalog_offset,
            catalog_limit=catalog_limit,
        )
        if payload is None:
            raise _error(404, "MANUSCRIPT_NOT_FOUND", "稿件不存在。")
        payload["passages_url"] = http_resource_path(
            request, f"/api/v1/manuscripts/{manuscript_id}/passages"
        )
        return _no_store(payload)

    @router.get("/{manuscript_id}/passages")
    async def get_manuscript_passages(
        request: Request,
        manuscript_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1, le=200),
    ) -> JSONResponse:
        tenant_id = await _resolve_tenant(request, tenant_resolver)
        payload = await asyncio.to_thread(
            _get_passages,
            service,
            tenant_id,
            manuscript_id,
            offset=offset,
            limit=limit,
        )
        payload.setdefault("manuscript_id", manuscript_id)
        return _no_store(payload)

    @router.delete("/{manuscript_id}", status_code=204)
    async def delete_manuscript(request: Request, manuscript_id: str) -> Response:
        tenant_id = await _resolve_tenant(request, tenant_resolver)
        if delete_handler is not None:
            await delete_handler(tenant_id, manuscript_id)
        else:
            await asyncio.to_thread(service.delete, tenant_id, manuscript_id)
        return Response(
            status_code=204,
            headers={"Cache-Control": "no-store"},
        )

    return router


async def manuscript_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ManuscriptError):
        raise exc
    return JSONResponse(
        status_code=exc.status,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
        content={
            "type": "about:blank",
            "title": exc.code.replace("_", " ").title(),
            "status": exc.status,
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "request_id": getattr(request.state, "request_id", "unknown"),
            "details": exc.details,
        },
    )


def install_manuscript_routes(
    app: FastAPI,
    service: ManuscriptService,
    tenant_resolver: TenantResolver,
) -> APIRouter:
    app.add_exception_handler(ManuscriptError, manuscript_error_handler)
    router = build_manuscript_router(service, tenant_resolver)
    app.include_router(router)
    return router


__all__ = [
    "TenantResolver",
    "build_manuscript_router",
    "install_manuscript_routes",
    "manuscript_error_handler",
]
