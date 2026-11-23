"""Bearer-token and cookie-session authentication with hashed credentials only."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import Settings

COOKIE_NAME = "literature_session"
SESSION_TTL_S = 12 * 60 * 60


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthManager:
    def __init__(self, db_path: Path, *, tenant_id: str, token_sha256: str) -> None:
        if len(token_sha256) != 64 or any(char not in "0123456789abcdef" for char in token_sha256):
            raise ValueError("CITATION_ACCESS_TOKEN_SHA256 must be a lowercase SHA-256 hex digest")
        self.db_path = db_path
        self.tenant_id = tenant_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.db_path, 0o600)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL, revoked_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO api_keys (token_hash, tenant_id, created_at) "
                "VALUES (?, ?, ?)",
                (token_sha256, tenant_id, int(time.time())),
            )
        self._failed_logins: dict[str, list[float]] = {}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def register_token(self, token: str, tenant_id: str) -> None:
        if not token or not tenant_id.strip():
            raise ValueError("token and tenant are required")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO api_keys (token_hash, tenant_id, created_at) VALUES (?, ?, ?)",
                (token_hash(token), tenant_id, int(time.time())),
            )

    def tenant_for_token(self, token: str) -> str | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id FROM api_keys WHERE token_hash=? AND revoked_at IS NULL",
                (token_hash(token),),
            ).fetchone()
        return row["tenant_id"] if row else None

    def create_session(self, token: str) -> tuple[str, str] | None:
        tenant = self.tenant_for_token(token)
        if tenant is None:
            return None
        session = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, tenant_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash(session), tenant, now, now + SESSION_TTL_S),
            )
        return session, tenant

    def tenant_for_session(self, session: str) -> str | None:
        if not session:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id, expires_at FROM sessions "
                "WHERE token_hash=? AND revoked_at IS NULL",
                (token_hash(session),),
            ).fetchone()
        if not row or row["expires_at"] <= int(time.time()):
            return None
        return row["tenant_id"]

    def revoke_session(self, session: str) -> None:
        if not session:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE token_hash=?",
                (int(time.time()), token_hash(session)),
            )

    def login_allowed(self, key: str) -> bool:
        now = time.monotonic()
        recent = [value for value in self._failed_logins.get(key, []) if now - value < 60]
        self._failed_logins[key] = recent
        if len(recent) >= 5:
            return False
        self._failed_logins[key] = [*recent, now]
        if len(self._failed_logins) > 1000:
            self._failed_logins = {
                item: values
                for item, values in self._failed_logins.items()
                if values and now - values[-1] < 60
            }
        return True


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.casefold() == "bearer" else ""


def authenticate_request(request: Request, settings: Settings) -> str | None:
    auth: AuthManager = request.app.state.auth
    token = bearer_token(request)
    if token:
        return auth.tenant_for_token(token)
    return auth.tenant_for_session(request.cookies.get(COOKIE_NAME, ""))


def auth_error(*, cookie: bool = False) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if cookie:
        headers["Set-Cookie"] = f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=lax; Max-Age=0"
    return JSONResponse(
        status_code=401,
        content={
            "type": "about:blank",
            "title": "Authentication required",
            "status": 401,
            "code": "AUTH_REQUIRED",
            "message": "需要有效的访问令牌或登录会话。",
        },
        headers=headers,
    )
