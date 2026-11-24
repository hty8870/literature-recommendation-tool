from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr

from .accounts import AccountError
from .credential_vault import CredentialError, CredentialVault
from .entities import StrictModel
from .model_egress import normalize_model_base
from .reasons import ModelConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_account_models (
    account_id TEXT PRIMARY KEY REFERENCES user_accounts(account_id),
    revision TEXT NOT NULL, api_type TEXT NOT NULL, base_url TEXT NOT NULL,
    model TEXT NOT NULL, sealed_key BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS delete_user_account_model BEFORE DELETE ON user_accounts
BEGIN
    DELETE FROM user_account_models WHERE account_id = OLD.account_id;
END;
"""


class ModelDetails(StrictModel):
    api_type: Literal["chat_completions", "responses"]
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=128)

    def context(self, account_id: str, revision: str) -> bytes:
        return json.dumps(["account-model-v1", account_id, revision, self.model_dump()]).encode()


class ModelDraft(ModelDetails):
    api_key: SecretStr | None = Field(default=None, max_length=512, exclude=True)

    def details(self) -> ModelDetails:
        return ModelDetails(
            api_type=self.api_type,
            base_url=normalize_model_base(self.base_url, self.api_type),
            model=self.model.strip(),
        )


class ModelSettingsUpdate(StrictModel):
    revision: str | None = Field(pattern=r"^[0-9a-f]{32}$")
    model: ModelDraft | None


class ModelSettingsView(StrictModel):
    revision: str | None = None
    model: ModelDetails | None = None


def _unavailable() -> AccountError:
    return AccountError(
        503, "ACCOUNT_MODEL_UNAVAILABLE", "无法使用账户模型的加密 Key，请联系管理员检查凭据。"
    )


class AccountModels:
    def __init__(self, db_path: Path, key_path: Path | None) -> None:
        self.db_path = db_path
        self.managed_key = key_path is not None
        self.vault = CredentialVault(
            key_path or db_path.with_name(f"{db_path.name}.account-model.key")
        )
        with self._connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.db_path, timeout=5)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def _row(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM user_account_models WHERE account_id=?", (account_id,)
        ).fetchone()

    @staticmethod
    def _view(row: sqlite3.Row | None) -> ModelSettingsView:
        if row is None:
            return ModelSettingsView()
        return ModelSettingsView(
            revision=row["revision"],
            model=ModelDetails(
                api_type=row["api_type"], base_url=row["base_url"], model=row["model"]
            ),
        )

    def read(self, account_id: str) -> ModelSettingsView:
        with self._connection() as conn:
            return self._view(self._row(conn, account_id))

    def _key(self, row: sqlite3.Row, details: ModelDetails) -> str:
        try:
            return self.vault.open(
                row["sealed_key"], details.context(row["account_id"], row["revision"])
            ).decode("ascii")
        except (CredentialError, UnicodeError):
            raise _unavailable() from None

    def resolve(self, account_id: str) -> ModelConfig | None:
        with self._connection() as conn:
            row = self._row(conn, account_id)
        if row is None:
            return None
        details = self._view(row).model
        assert details is not None
        config = ModelConfig(
            provider="custom", api_key=self._key(row, details), **details.model_dump()
        )
        config.validate()
        return config

    def save(self, account_id: str, update: ModelSettingsUpdate) -> ModelSettingsView:
        with self._connection(write=True) as conn:
            row = self._row(conn, account_id)
            current = self._view(row)
            if update.revision != current.revision:
                raise AccountError(
                    409, "ACCOUNT_MODEL_CHANGED", "其他设备已修改模型设置，请重新打开后再保存。"
                )
            draft = update.model
            if draft is None:
                conn.execute("DELETE FROM user_account_models WHERE account_id=?", (account_id,))
                return ModelSettingsView()
            try:
                details = draft.details()
                key = draft.api_key.get_secret_value().strip() if draft.api_key is not None else ""
                if (
                    not key
                    and row is not None
                    and current.model is not None
                    and (
                        current.model.base_url == details.base_url
                        and current.model.api_type == details.api_type
                    )
                ):
                    key = self._key(row, current.model)
                if not key:
                    raise ValueError("新接口或协议需要重新输入 API Key。")
                if key in details.model or key in details.base_url:
                    raise ValueError("API Key 只能填写在 Key 输入框。")
                ModelConfig(provider="custom", api_key=key, **details.model_dump()).validate()
            except ValueError as exc:
                raise AccountError(400, "MODEL_CONFIG_INVALID", str(exc)) from None
            revision = uuid.uuid4().hex
            try:
                create_key = (
                    not self.managed_key
                    and conn.execute("SELECT 1 FROM user_account_models LIMIT 1").fetchone() is None
                )
                sealed = self.vault.seal(
                    key.encode("ascii"),
                    details.context(account_id, revision),
                    create_key=create_key,
                )
            except CredentialError:
                raise _unavailable() from None
            conn.execute(
                "INSERT INTO user_account_models VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision, "
                "api_type=excluded.api_type, base_url=excluded.base_url, model=excluded.model, "
                "sealed_key=excluded.sealed_key",
                (account_id, revision, details.api_type, details.base_url, details.model, sealed),
            )
            return ModelSettingsView(revision=revision, model=details)
