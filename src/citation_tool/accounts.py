"""Optional user accounts and revocable sessions, separate from legacy token auth."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .auth import token_hash

ACCOUNT_COOKIE = "literature_account_session"
ACCOUNT_TENANT_PREFIX = "account:"
ACCOUNT_SESSION_TTL = 14 * 24 * 60 * 60
SESSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
SCRYPT_SCHEME = "scrypt-15-8-3"
MAX_SESSIONS = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_accounts (
    account_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_account_sessions (
    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY(account_id) REFERENCES user_accounts(account_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS user_account_sessions_account
    ON user_account_sessions(account_id, created_at);
CREATE INDEX IF NOT EXISTS user_account_sessions_expiry
    ON user_account_sessions(expires_at);
"""


class AccountError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    username: str
    display_name: str

    @property
    def tenant_id(self) -> str:
        return ACCOUNT_TENANT_PREFIX + self.account_id

    def payload(self) -> dict[str, str]:
        return {"id": self.account_id, "username": self.username, "display_name": self.display_name}


@dataclass(frozen=True, slots=True)
class AccountGrant:
    account: Account
    session: str = field(repr=False)
    recovery_code: str | None = field(default=None, repr=False)


def account_session_error() -> AccountError:
    return AccountError(401, "ACCOUNT_SESSION_EXPIRED", "登录会话已失效，请重新登录。")


def invalid_credentials() -> AccountError:
    return AccountError(401, "ACCOUNT_CREDENTIALS_INVALID", "账户名或凭据不正确。")


def is_account_tenant(tenant_id: str) -> bool:
    return tenant_id.casefold().startswith(ACCOUNT_TENANT_PREFIX)


def username_key(username: str) -> str:
    value = username.strip().lower()
    if not value.isascii() or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,31}", value):
        raise AccountError(
            400,
            "ACCOUNT_USERNAME_INVALID",
            "账户名应为 3–32 位英文字母、数字或 ._-，以字母或数字开头。",
        )
    return value


def display_name_value(display_name: str) -> str:
    name = " ".join(display_name.split())
    if not name or len(name) > 48 or not name.isprintable():
        raise AccountError(400, "ACCOUNT_NAME_INVALID", "显示名称应为 1–48 个可显示字符。")
    return name


def _password_bytes(value: str) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return b""
    return encoded if len(value) <= 128 and len(encoded) <= 512 else b""


def _derive(value: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(value, salt=salt, n=1 << 15, r=8, p=3, maxmem=40 * 1024 * 1024)


def _password_hash(value: str) -> str:
    encoded = _password_bytes(value)
    if len(value) < 12 or not encoded or not value.strip():
        raise AccountError(400, "ACCOUNT_PASSWORD_INVALID", "请输入 12–128 个字符的密码。")
    salt = secrets.token_bytes(16)
    return f"{SCRYPT_SCHEME}${salt.hex()}${_derive(encoded, salt).hex()}"


def _password_matches(value: str, stored: str | None) -> bool:
    encoded = _password_bytes(value)
    scheme, salt, digest = (stored or f"{SCRYPT_SCHEME}${'00' * 16}${'00' * 64}").split("$")
    if scheme != SCRYPT_SCHEME:
        return False
    verified = secrets.compare_digest(_derive(encoded, bytes.fromhex(salt)), bytes.fromhex(digest))
    return bool(stored and encoded and verified)


class AccountRateLimiter:
    def __init__(self) -> None:
        self._limits: dict[tuple[str, ...], deque[float]] = {}
        self._prune_at = 0.0

    def check(self, action: str, peer: str, identity: str) -> None:
        now = time.monotonic()
        if now >= self._prune_at:
            self._limits = {
                key: window
                for key, window in self._limits.items()
                if window and window[-1] > now - 600
            }
            self._prune_at = now + 30
        limits = (
            (("all",), 160),
            (("peer", peer), 30),
            (("action", peer, "register"), 6) if action == "register" else (("name", identity), 10),
        )
        windows = []
        for key, maximum in limits:
            window = self._limits.get(key, deque())
            while window and window[0] <= now - 600:
                window.popleft()
            if len(window) >= maximum:
                raise AccountError(
                    429, "ACCOUNT_RATE_LIMITED", "尝试次数过多，请稍后重试。", retryable=True
                )
            windows.append((key, window))
        for key, window in windows:
            self._limits[key] = window
            window.append(now)


class AccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self._connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(row["account_id"], row["username"], row["display_name"])

    @staticmethod
    def _issue(conn: sqlite3.Connection, account_id: str, replace_session: str = "") -> str:
        now = int(time.time())
        session = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM user_account_sessions WHERE expires_at<=?", (now,))
        if SESSION_TOKEN_PATTERN.fullmatch(replace_session):
            conn.execute(
                "DELETE FROM user_account_sessions WHERE token_hash=?",
                (token_hash(replace_session),),
            )
        conn.execute(
            "DELETE FROM user_account_sessions WHERE account_id=? AND token_hash IN ("
            " SELECT token_hash FROM user_account_sessions WHERE account_id=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (account_id, account_id, MAX_SESSIONS - 1),
        )
        conn.execute(
            "INSERT INTO user_account_sessions VALUES (?, ?, ?, ?)",
            (token_hash(session), account_id, now, now + ACCOUNT_SESSION_TTL),
        )
        return session

    def register(
        self, username: str, password: str, display_name: str, previous: str
    ) -> AccountGrant:
        name = display_name_value(display_name) if display_name.strip() else username
        account = Account(f"usr_{uuid.uuid4().hex}", username, name)
        digest = _password_hash(password)
        recovery = secrets.token_urlsafe(24)
        try:
            with self._connection(write=True) as conn:
                conn.execute(
                    "INSERT INTO user_accounts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        account.account_id,
                        username,
                        name,
                        digest,
                        token_hash(recovery),
                        int(time.time()),
                    ),
                )
                session = self._issue(conn, account.account_id, previous)
        except sqlite3.IntegrityError:
            raise AccountError(
                409, "ACCOUNT_USERNAME_TAKEN", "该账户名不可用，请换一个或登录。"
            ) from None
        return AccountGrant(account, session, recovery)

    def sign_in(self, username: str, password: str, previous: str) -> AccountGrant:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM user_accounts WHERE username=?", (username,)
            ).fetchone()
        if not _password_matches(password, row["password_hash"] if row else None) or row is None:
            raise invalid_credentials()
        with self._connection(write=True) as conn:
            current = conn.execute(
                "SELECT * FROM user_accounts WHERE account_id=? AND password_hash=?",
                (row["account_id"], row["password_hash"]),
            ).fetchone()
            if current is None:
                raise invalid_credentials()
            return AccountGrant(
                self._account(current), self._issue(conn, current["account_id"], previous)
            )

    def _session_row(self, conn: sqlite3.Connection, session: str) -> sqlite3.Row | None:
        if not SESSION_TOKEN_PATTERN.fullmatch(session):
            return None
        return conn.execute(
            "SELECT a.* FROM user_accounts a JOIN user_account_sessions s USING (account_id) "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (token_hash(session), int(time.time())),
        ).fetchone()

    def session_account(self, session: str) -> Account | None:
        if not SESSION_TOKEN_PATTERN.fullmatch(session):
            return None
        with self._connection() as conn:
            row = self._session_row(conn, session)
        return self._account(row) if row else None

    def sign_out(self, session: str) -> None:
        if SESSION_TOKEN_PATTERN.fullmatch(session):
            with self._connection(write=True) as conn:
                conn.execute(
                    "DELETE FROM user_account_sessions WHERE token_hash=?", (token_hash(session),)
                )

    def update_profile(self, session: str, display_name: str) -> Account:
        name = display_name_value(display_name)
        with self._connection(write=True) as conn:
            current = self._session_row(conn, session)
            if current is None:
                raise account_session_error()
            conn.execute(
                "UPDATE user_accounts SET display_name=? WHERE account_id=?",
                (name, current["account_id"]),
            )
        return Account(current["account_id"], current["username"], name)

    def _rotate(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        digest: str,
        previous: str,
    ) -> AccountGrant:
        recovery = secrets.token_urlsafe(24)
        account_id = row["account_id"]
        conn.execute(
            "UPDATE user_accounts SET password_hash=?, recovery_hash=? WHERE account_id=?",
            (digest, token_hash(recovery), account_id),
        )
        conn.execute("DELETE FROM user_account_sessions WHERE account_id=?", (account_id,))
        return AccountGrant(self._account(row), self._issue(conn, account_id, previous), recovery)

    def reset_password(
        self, username: str, recovery: str, password: str, previous: str
    ) -> AccountGrant:
        digest = _password_hash(password)
        code = recovery.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32}", code):
            raise invalid_credentials()
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM user_accounts WHERE username=?", (username,)
            ).fetchone()
            matched = secrets.compare_digest(
                row["recovery_hash"] if row else "0" * 64,
                token_hash(code),
            )
            if row is None or not matched:
                raise invalid_credentials()
            return self._rotate(conn, row, digest, previous)

    def verify_password(self, session: str, password: str) -> Account:
        with self._connection() as conn:
            row = self._session_row(conn, session)
        if row is None:
            raise account_session_error()
        if not _password_matches(password, row["password_hash"]):
            raise invalid_credentials()
        return self._account(row)

    def change_password(self, session: str, old: str, password: str) -> AccountGrant:
        with self._connection() as conn:
            row = self._session_row(conn, session)
        if row is None:
            raise account_session_error()
        if not _password_matches(old, row["password_hash"]):
            raise invalid_credentials()
        digest = _password_hash(password)
        with self._connection(write=True) as conn:
            current = self._session_row(conn, session)
            if current is None or current["password_hash"] != row["password_hash"]:
                raise account_session_error()
            return self._rotate(conn, current, digest, session)
