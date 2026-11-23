"""Private project settings and the source-selection authority."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .accounts import AccountError, is_account_tenant
from .entities import utc_now_iso
from .project_schema import OTHER

if TYPE_CHECKING:
    from .store import Store

MAX_PROJECTS = 30


@dataclass(frozen=True, slots=True)
class Project:
    project_id: str
    name: str
    share_sources: bool
    library_ids: tuple[str, ...]
    available_library_ids: tuple[str, ...]
    learning_enabled: bool
    archived: bool
    revision: int


def project_name(value: str) -> tuple[str, str]:
    name = " ".join(value.split())
    key = unicodedata.normalize("NFKC", name).casefold()
    if not (0 < len(name) <= 72 and name.isprintable()) or key in {"其它", "其他", OTHER}:
        raise AccountError(400, "PROJECT_NAME_INVALID", "请输入 1–72 字的项目名，不使用「其它」。")
    return name, key


def project_not_found() -> AccountError:
    return AccountError(404, "PROJECT_NOT_FOUND", "没有找到这个项目。")


def validate_library_ids(ids: list[str], allowed: tuple[str, ...]) -> None:
    if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(allowed):
        raise AccountError(400, "PROJECT_SOURCES_INVALID", "请至少选择一个本项目可用的数据源。")


class ProjectStore:
    def __init__(
        self, store: Store, public_ids: tuple[str, ...], connector_versions: Mapping[str, str]
    ) -> None:
        self.store = store
        self.public_ids = public_ids
        self.versions = dict(connector_versions)

    @staticmethod
    def _require_private(tenant_id: str) -> None:
        if not is_account_tenant(tenant_id):
            raise AccountError(401, "ACCOUNT_REQUIRED", "请登录后使用私人项目。")

    @staticmethod
    def _row(conn: sqlite3.Connection, tenant_id: str, project_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM workspace_projects WHERE tenant_id=? AND project_id=?",
            (tenant_id, project_id),
        ).fetchone()

    def _project(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        project_id: str,
        row: sqlite3.Row | None,
    ) -> Project:
        if row is None and project_id != OTHER:
            raise project_not_found()
        active = conn.execute(
            "SELECT connector_id, connector_version FROM workspace_source_requests "
            "WHERE tenant_id=? AND project_id=? AND status='ready' ORDER BY connector_id",
            (tenant_id, project_id),
        )
        available = self.public_ids + tuple(
            item[0] for item in active if self.versions.get(item[0]) == item[1]
        )
        if row is None:
            return Project(OTHER, "其它", False, self.public_ids, available, False, False, 0)
        saved = json.loads(row["selected_json"]) if row["selected_json"] is not None else []
        selected = tuple(item for item in saved if item in available) or self.public_ids
        return Project(
            project_id,
            row["name"],
            bool(row["share_sources"]),
            selected,
            available,
            row["learning_since"] is not None,
            bool(row["archived"]),
            row["revision"],
        )

    def get(self, tenant_id: str, project_id: str) -> Project:
        self._require_private(tenant_id)
        with self.store._read() as conn:
            return self._project(
                conn, tenant_id, project_id, self._row(conn, tenant_id, project_id)
            )

    def list(self, tenant_id: str) -> list[dict]:
        self._require_private(tenant_id)
        with self.store._read() as conn:
            rows = conn.execute(
                "SELECT project_id, name, archived FROM workspace_projects "
                "WHERE tenant_id=? AND project_id<>? ORDER BY archived, name_key, project_id",
                (tenant_id, OTHER),
            ).fetchall()
        return [{"project_id": OTHER, "name": "其它", "archived": False}] + [
            {**dict(row), "archived": bool(row["archived"])} for row in rows
        ]

    def create(self, tenant_id: str, value: str) -> Project:
        self._require_private(tenant_id)
        name, key = project_name(value)
        identifier = f"prj_{uuid.uuid4().hex}"
        with self.store._write() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM workspace_projects WHERE tenant_id=? AND project_id<>?",
                (tenant_id, OTHER),
            ).fetchone()[0]
            if count >= MAX_PROJECTS:
                raise AccountError(409, "PROJECT_LIMIT", "已达到 30 个项目上限。")
            try:
                conn.execute(
                    "INSERT INTO workspace_projects "
                    "(tenant_id, project_id, name, name_key, share_sources) VALUES (?,?,?,?,1)",
                    (tenant_id, identifier, name, key),
                )
            except sqlite3.IntegrityError:
                raise AccountError(409, "PROJECT_NAME_TAKEN", "已有同名项目。") from None
            return self._project(
                conn, tenant_id, identifier, self._row(conn, tenant_id, identifier)
            )

    def update(
        self,
        tenant_id: str,
        project_id: str,
        *,
        revision: int,
        name: str | None = None,
        share_sources: bool | None = None,
        library_ids: list[str] | None = None,
        learning_enabled: bool | None = None,
        archived: bool | None = None,
    ) -> Project:
        self._require_private(tenant_id)
        with self.store._write() as conn:
            row = self._row(conn, tenant_id, project_id)
            current = self._project(conn, tenant_id, project_id, row)
            if current.revision != revision:
                raise AccountError(409, "PROJECT_CHANGED", "项目设置已变更，请重新读取。")
            if project_id == OTHER and any(
                i is not None for i in (name, share_sources, library_ids, archived)
            ):
                raise AccountError(400, "PROJECT_OTHER_FIXED", "「其它」没有共享选库或归档设置。")
            target_name, name_key = (
                project_name(name)
                if name is not None
                else (
                    current.name,
                    row["name_key"] if row else OTHER,
                )
            )
            share = current.share_sources if share_sources is None else share_sources
            is_archived = current.archived if archived is None else archived
            if is_archived and (learning_enabled is True or library_ids is not None):
                raise AccountError(409, "PROJECT_ARCHIVED", "请先恢复已归档的项目。")
            selected_json = row["selected_json"] if row else None
            if library_ids is not None:
                if not share:
                    raise AccountError(409, "PROJECT_NOT_SHARED", "此项目未共享数据源。")
                validate_library_ids(library_ids, current.available_library_ids)
                selected_json = json.dumps(library_ids, separators=(",", ":"))
            since = row["learning_since"] if row else None
            if is_archived or learning_enabled is False:
                since = None
            elif learning_enabled and since is None:
                since = utc_now_iso()
            if row is None:
                conn.execute(
                    "INSERT INTO workspace_projects "
                    "(tenant_id, project_id, name, name_key, share_sources) VALUES (?,?,?,?,0)",
                    (tenant_id, OTHER, "其它", OTHER),
                )
            try:
                conn.execute(
                    """UPDATE workspace_projects SET name=?, name_key=?, share_sources=?,
                       selected_json=?, learning_since=?, archived=?, revision=revision+1
                       WHERE tenant_id=? AND project_id=?""",
                    (
                        target_name,
                        name_key,
                        share,
                        selected_json,
                        since,
                        is_archived,
                        tenant_id,
                        project_id,
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccountError(409, "PROJECT_NAME_TAKEN", "已有同名项目。") from None
            if since is None:
                conn.execute(
                    "DELETE FROM workspace_learning_queue WHERE tenant_id=? AND project_id=?",
                    (tenant_id, project_id),
                )
            if is_archived:
                conn.execute(
                    """UPDATE workspace_source_requests SET closed=1, status='cancelled',
                       lease=NULL, leased_until=0, updated_at=? WHERE tenant_id=? AND project_id=?
                       AND status IN ('pending','queued','testing','failed')""",
                    (utc_now_iso(), tenant_id, project_id),
                )
            return self._project(
                conn, tenant_id, project_id, self._row(conn, tenant_id, project_id)
            )

    def resolve_sources(
        self, project: Project, requested: list[str] | None, previous: list[str] | None
    ) -> list[str]:
        if requested is not None:
            validate_library_ids(requested, project.available_library_ids)
            return requested
        if project.share_sources:
            return list(project.library_ids)
        if previous:
            remaining = [item for item in previous if item in project.available_library_ids]
            if remaining:
                return remaining
        return list(self.public_ids)
