"""SQLite persistence for the M0 skeleton.

This is a local, single-process store, not a portable production abstraction.
SQLite is the only backend for M0; PostgreSQL will need its own dialect and
transaction implementation. All writes are short explicit transactions, and a
query's sequence, event, recommendation/worker state, and applied watermark are
committed together.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .candidates import merge_candidates
from .entities import (
    AgentQuery,
    AgentRun,
    CandidateDiagnostic,
    Conversation,
    ConversationArtifact,
    ConversationMessage,
    ConversationTurn,
    EvaluationAnchor,
    Evidence,
    EvidenceBundle,
    PurgeReceipt,
    RankingPhase,
    RecommendationReason,
    SearchCreated,
    SnapshotManifest,
    utc_now_iso,
)
from .evidence import evidence_completeness, scope_applicability
from .jcs import sha256_json
from .project_schema import OTHER, enqueue_learning, purge_learning_for_conversations
from .project_schema import SCHEMA as PROJECT_SCHEMA

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, input_type TEXT NOT NULL,
    search_state TEXT NOT NULL, planning_mode TEXT NOT NULL,
    requested_library_ids TEXT NOT NULL, claims_json TEXT NOT NULL,
    options_json TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
    idempotency_key TEXT, create_response_json TEXT NOT NULL, claim_count INTEGER NOT NULL,
    applied_through_sequence INTEGER NOT NULL DEFAULT 0,
    latest_sequence INTEGER NOT NULL DEFAULT 0, finalize_cut INTEGER,
    event_set_hash TEXT, latest_checkpoint_id TEXT, final_snapshot_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, query_id),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS events (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL, occurred_at TEXT NOT NULL, producer_event_key TEXT,
    PRIMARY KEY (tenant_id, query_id, sequence),
    UNIQUE (tenant_id, query_id, event_id),
    UNIQUE (tenant_id, query_id, producer_event_key)
);
CREATE TABLE IF NOT EXISTS recommendations (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, result_id TEXT NOT NULL,
    work_id TEXT NOT NULL, claim_id TEXT NOT NULL, library_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, citable INTEGER NOT NULL, rankable INTEGER NOT NULL,
    display_score REAL, result_version INTEGER NOT NULL, updated_sequence INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, query_id, result_id)
);
CREATE TABLE IF NOT EXISTS workers (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, library_id TEXT NOT NULL,
    attempt INTEGER NOT NULL, payload_json TEXT NOT NULL, worker_state TEXT NOT NULL,
    candidates_seen INTEGER NOT NULL, candidates_verified INTEGER NOT NULL,
    recommendations_emitted INTEGER NOT NULL, last_emitted_sequence INTEGER,
    emitted_event_count INTEGER NOT NULL, drained INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, query_id, library_id, attempt)
);
CREATE TABLE IF NOT EXISTS candidate_diagnostics (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, result_id TEXT NOT NULL,
    paper_id TEXT NOT NULL, library_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, query_id, result_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL, immutable_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
    evidence_bundle_id TEXT NOT NULL, evidence_bundle_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, snapshot_id)
);
CREATE TABLE IF NOT EXISTS evidence_bundles (
    tenant_id TEXT NOT NULL, bundle_id TEXT NOT NULL, bundle_hash TEXT NOT NULL,
    object_key TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, bundle_id)
);
CREATE TABLE IF NOT EXISTS snapshot_requests (
    tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS agent_runs (
    tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
    request_hash TEXT NOT NULL, idempotency_key TEXT,
    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS conversations (
    tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL, title TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0, active_run_id TEXT NOT NULL DEFAULT '',
    latest_turn_index INTEGER NOT NULL DEFAULT 0, latest_anchor_json TEXT,
    last_active_query_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS conversations_recent
    ON conversations (tenant_id, updated_at DESC, conversation_id ASC);
CREATE TABLE IF NOT EXISTS conversation_messages (
    tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    role TEXT NOT NULL, text TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL, turn_index INTEGER NOT NULL,
    run_id TEXT NOT NULL, user_message_seq INTEGER NOT NULL,
    assistant_message_seq INTEGER, status TEXT NOT NULL,
    active_query_id TEXT NOT NULL DEFAULT '', summary TEXT, error_json TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL, finished_at TEXT,
    PRIMARY KEY (tenant_id, conversation_id, turn_index),
    UNIQUE (tenant_id, run_id),
    UNIQUE (tenant_id, conversation_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS conversation_artifacts (
    tenant_id TEXT NOT NULL, conversation_id TEXT NOT NULL, seq INTEGER NOT NULL,
    run_id TEXT NOT NULL, turn_index INTEGER NOT NULL, query_id TEXT NOT NULL,
    label TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id, seq),
    UNIQUE (tenant_id, conversation_id, query_id)
);
CREATE INDEX IF NOT EXISTS conversation_artifacts_query
    ON conversation_artifacts (tenant_id, query_id);
CREATE TABLE IF NOT EXISTS reason_cache (
    tenant_id TEXT NOT NULL, input_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, input_hash)
);
CREATE TABLE IF NOT EXISTS candidate_ranking_state (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, claim_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, PRIMARY KEY (tenant_id, query_id, claim_id)
);
CREATE TABLE IF NOT EXISTS candidate_ranking_history (
    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, claim_id TEXT NOT NULL,
    ranking_version INTEGER NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, query_id, claim_id, ranking_version)
);
CREATE TABLE IF NOT EXISTS ranking_cache (
    tenant_id TEXT NOT NULL, input_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, input_hash)
);
CREATE TABLE IF NOT EXISTS purge_receipts (
    tenant_id TEXT NOT NULL, receipt_id TEXT NOT NULL, counts_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, receipt_id)
);
"""

ALLOWED_TRANSITIONS = {
    "created": {"running", "cancelled"},
    "running": {"draining", "failed"},
    "draining": {"finalizing", "failed"},
    "finalizing": {"completed", "partial", "cancelled", "failed"},
}
TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}
WORKER_TERMINAL = {"completed", "failed", "timed_out", "cancelled"}
WORKER_EVENTS = {
    "worker_progress",
    "worker_completed",
    "worker_failed",
    "worker_timed_out",
    "worker_cancelled",
}


MAX_CONVERSATION_MESSAGES = 400
MAX_CONVERSATION_TURNS = 200
CONVERSATION_TITLE_CHARS = 120
CONVERSATION_STALE_RUN_SECONDS = 3600


class StoreConflict(Exception):
    code = "QUERY_STATE_CONFLICT"


class IdempotencyConflict(StoreConflict):
    code = "IDEMPOTENCY_KEY_CONFLICT"


class ConversationNotFound(StoreConflict):
    code = "CONVERSATION_NOT_FOUND"
    http_status = 404


class ConversationConflict(StoreConflict):
    code = "CONVERSATION_REVISION_CONFLICT"
    http_status = 409


class ConversationBusy(StoreConflict):
    code = "CONVERSATION_BUSY"
    http_status = 409


class ConversationCapacity(StoreConflict):
    code = "CONVERSATION_CAPACITY"
    http_status = 409


class ConversationQueryNotFound(StoreConflict):
    code = "CONVERSATION_ACTIVE_QUERY_INVALID"
    http_status = 409


class WorkerWatermarkError(StoreConflict):
    code = "WORKER_WATERMARK_MISMATCH"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_purge_ids(conn: sqlite3.Connection, table: str, values: list[str]) -> None:
    conn.execute(  # noqa: S608 - table is one of the fixed internal purge tables
        f"CREATE TEMP TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY)"
    )
    conn.execute(f"DELETE FROM {table}")  # noqa: S608
    conn.executemany(
        f"INSERT OR IGNORE INTO {table} (id) VALUES (?)",  # noqa: S608
        [(str(value),) for value in values],
    )


def _conversation_title(message: str) -> str:
    text = " ".join(str(message or "").split())
    return text[:CONVERSATION_TITLE_CHARS] or "未命名会话"


def _run_is_stale(updated_at: str, now: str) -> bool:
    try:
        previous = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return False
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current - previous).total_seconds() > CONVERSATION_STALE_RUN_SECONDS


class Store:
    def __init__(self, db_path: Path, *, learning_enabled: bool = False) -> None:
        self.db_path = Path(db_path)
        self.learning_enabled = learning_enabled
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.db_path, 0o600)
        with self._connect() as conn:
            conn.executescript(SCHEMA + PROJECT_SCHEMA)
        self._migrate_projects()
        self._migrate_candidate_identity()

    def _migrate_projects(self) -> None:
        with self._write() as conn:
            columns = conn.execute("PRAGMA table_info(conversations)").fetchall()
            if not any(row["name"] == "project_id" for row in columns):
                conn.execute("ALTER TABLE conversations ADD COLUMN project_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS conversations_project_recent "
                "ON conversations (tenant_id, project_id, updated_at DESC, conversation_id)"
            )

    def _migrate_candidate_identity(self) -> None:
        with self._write() as conn:
            columns = conn.execute("PRAGMA table_info(candidate_diagnostics)").fetchall()
            primary_key = {row["name"] for row in columns if row["pk"]}
            if primary_key == {"tenant_id", "query_id", "result_id"}:
                return
            conn.execute("ALTER TABLE candidate_diagnostics RENAME TO candidate_diagnostics_legacy")
            conn.execute(
                """CREATE TABLE candidate_diagnostics (
                    tenant_id TEXT NOT NULL, query_id TEXT NOT NULL, result_id TEXT NOT NULL,
                    paper_id TEXT NOT NULL, library_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY (tenant_id, query_id, result_id)
                )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO candidate_diagnostics
                SELECT * FROM candidate_diagnostics_legacy"""
            )
            conn.execute("DROP TABLE candidate_diagnostics_legacy")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
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

    @staticmethod
    def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608
            tuple(values.values()),
        )

    @staticmethod
    def _update(conn: sqlite3.Connection, table: str, values: dict, where: dict) -> None:
        assignments = ", ".join(f"{key}=?" for key in values)
        clauses = " AND ".join(f"{key}=?" for key in where)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {clauses}",  # noqa: S608
            tuple(values.values()) + tuple(where.values()),
        )

    @staticmethod
    def _search(conn: sqlite3.Connection, tenant_id: str, query_id: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM searches WHERE tenant_id=? AND query_id=?", (tenant_id, query_id)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _worker_row(
        conn: sqlite3.Connection, tenant_id: str, query_id: str, library_id: str, attempt: int
    ) -> dict | None:
        row = conn.execute(
            "SELECT * FROM workers WHERE tenant_id=? AND query_id=? AND library_id=? AND attempt=?",
            (tenant_id, query_id, library_id, attempt),
        ).fetchone()
        return dict(row) if row else None

    def create_search(
        self, record: dict, workers: list[dict], *, agent_run_id: str | None = None
    ) -> tuple[dict, bool]:
        with self._write() as conn:
            key = record.get("idempotency_key")
            if agent_run_id is not None and key:
                raise ValueError("Agent searches do not use a public idempotency key")
            if key:
                existing = conn.execute(
                    "SELECT * FROM searches WHERE tenant_id=? AND idempotency_key=?",
                    (record["tenant_id"], key),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != record["request_hash"]:
                        raise IdempotencyConflict("same Idempotency-Key with a different request")
                    return dict(existing), True
            self._insert(
                conn,
                "searches",
                {
                    "tenant_id": record["tenant_id"],
                    "query_id": record["query_id"],
                    "input_type": record["input_type"],
                    "search_state": record["search_state"],
                    "planning_mode": record["planning_mode"],
                    "requested_library_ids": _json(record["requested_library_ids"]),
                    "claims_json": _json(record["claims"]),
                    "options_json": _json(record["options"]),
                    "request_json": _json(record["request"]),
                    "request_hash": record["request_hash"],
                    "idempotency_key": key,
                    "create_response_json": _json(record["create_response"]),
                    "claim_count": record["claim_count"],
                    "created_at": record["created_at"],
                    "updated_at": record["created_at"],
                },
            )
            for worker in workers:
                self._insert_worker(conn, worker)
            if agent_run_id is not None:
                self._attach_agent_query(
                    conn,
                    record["tenant_id"],
                    agent_run_id,
                    AgentQuery(
                        created=SearchCreated.model_validate(record["create_response"]),
                        requested_library_ids=record["requested_library_ids"],
                    ),
                )
            return record, False

    @staticmethod
    def _insert_worker(conn: sqlite3.Connection, worker: dict) -> None:
        Store._insert(
            conn,
            "workers",
            {
                "tenant_id": worker["tenant_id"],
                "query_id": worker["query_id"],
                "library_id": worker["library_id"],
                "attempt": worker["attempt"],
                "payload_json": _json(worker),
                "worker_state": worker["worker_state"],
                "candidates_seen": worker["candidates_seen"],
                "candidates_verified": worker["candidates_verified"],
                "recommendations_emitted": worker["recommendations_emitted"],
                "last_emitted_sequence": worker["last_emitted_sequence"],
                "emitted_event_count": worker["emitted_event_count"],
                "drained": int(worker["drained"]),
                "updated_at": worker["updated_at"],
            },
        )

    def get_search(self, tenant_id: str, query_id: str) -> dict | None:
        with self._read() as conn:
            return self._search(conn, tenant_id, query_id)

    def get_state_bundle(self, tenant_id: str, query_id: str) -> dict | None:
        with self._read() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                return None
            recommendations = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM recommendations WHERE tenant_id=? AND query_id=?
                       ORDER BY updated_sequence, result_id""",
                    (tenant_id, query_id),
                )
            ]
            workers = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM workers WHERE tenant_id=? AND query_id=?
                       ORDER BY library_id, attempt""",
                    (tenant_id, query_id),
                )
            ]
            candidates = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT payload_json FROM candidate_diagnostics "
                    "WHERE tenant_id=? AND query_id=? ORDER BY result_id",
                    (tenant_id, query_id),
                )
            ]
            rankings = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT payload_json FROM candidate_ranking_state "
                    "WHERE tenant_id=? AND query_id=? ORDER BY claim_id",
                    (tenant_id, query_id),
                )
            ]
            return {
                "search": search,
                "recommendations": recommendations,
                "workers": workers,
                "candidates": candidates,
                "candidate_rankings": rankings,
            }

    @staticmethod
    def _event_dict(row: dict) -> dict:
        return {
            "tenant_id": row["tenant_id"],
            "query_id": row["query_id"],
            "sequence": row["sequence"],
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"],
            "payload": json.loads(row["payload_json"]),
            "payload_json": row["payload_json"],
            "payload_hash": row["payload_hash"],
        }

    def list_events(
        self, tenant_id: str, query_id: str, after_sequence: int, limit: int = 500
    ) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT * FROM events WHERE tenant_id=? AND query_id=? AND sequence>?
                   ORDER BY sequence LIMIT ?""",
                (tenant_id, query_id, after_sequence, limit),
            )
            return [self._event_dict(dict(row)) for row in rows]

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        query_id: str,
        event_type: str,
        payload: dict,
        producer_event_key: str | None = None,
    ) -> dict:
        search = self._search(conn, tenant_id, query_id)
        if search is None:
            raise KeyError(query_id)
        sequence = search["latest_sequence"] + 1
        event_id = f"evt_{uuid.uuid4().hex}"
        payload_json = _json(payload)
        occurred_at = utc_now_iso()
        self._insert(
            conn,
            "events",
            {
                "tenant_id": tenant_id,
                "query_id": query_id,
                "sequence": sequence,
                "event_id": event_id,
                "event_type": event_type,
                "payload_json": payload_json,
                "payload_hash": sha256_json(payload),
                "occurred_at": occurred_at,
                "producer_event_key": producer_event_key,
            },
        )
        event_hash = self._event_set_hash(conn, tenant_id, query_id, sequence)
        self._update(
            conn,
            "searches",
            {
                "latest_sequence": sequence,
                "applied_through_sequence": sequence,
                "event_set_hash": event_hash,
                "updated_at": occurred_at,
            },
            {"tenant_id": tenant_id, "query_id": query_id},
        )
        return {
            "query_id": query_id,
            "sequence": sequence,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload": payload,
            "payload_json": payload_json,
            "payload_hash": sha256_json(payload),
        }

    @staticmethod
    def _event_set_hash(
        conn: sqlite3.Connection, tenant_id: str, query_id: str, through_sequence: int
    ) -> str:
        rows = conn.execute(
            """SELECT event_id, event_type, payload_hash FROM events
               WHERE tenant_id=? AND query_id=? AND sequence<=? ORDER BY sequence""",
            (tenant_id, query_id, through_sequence),
        ).fetchall()
        return sha256_json([[row[0], row[1], row[2]] for row in rows])

    def append_state_event(self, tenant_id: str, query_id: str, new_state: str) -> dict:
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            previous = search["search_state"]
            if new_state not in ALLOWED_TRANSITIONS.get(previous, set()):
                raise StoreConflict(f"illegal transition {previous} -> {new_state}")
            event = self._insert_event(
                conn,
                tenant_id,
                query_id,
                "search_state_changed",
                {"previous_state": previous, "current_state": new_state},
            )
            self._update(
                conn,
                "searches",
                {"search_state": new_state},
                {"tenant_id": tenant_id, "query_id": query_id},
            )
            return event

    def fix_finalize_cut(self, tenant_id: str, query_id: str) -> int:
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            cut = search["latest_sequence"]
            self._update(
                conn,
                "searches",
                {"finalize_cut": cut},
                {"tenant_id": tenant_id, "query_id": query_id},
            )
            return cut

    def commit_worker_event(
        self, tenant_id: str, query_id: str, worker: dict, event_type: str
    ) -> dict:
        if event_type not in WORKER_EVENTS:
            raise ValueError(event_type)
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            if search["search_state"] != "running":
                raise StoreConflict("worker events are only accepted while running")
            existing = self._worker_row(
                conn, tenant_id, query_id, worker["library_id"], worker["attempt"]
            )
            if existing is None:
                raise KeyError(worker["library_id"])
            if event_type == "worker_completed":
                if worker["worker_state"] != "completed" or not worker["drained"]:
                    raise StoreConflict("worker_completed requires completed and drained=true")
                if worker["last_emitted_sequence"] != existing["last_emitted_sequence"]:
                    raise WorkerWatermarkError("worker_completed watermark does not match events")
            self._save_worker(conn, tenant_id, query_id, worker)
            return self._insert_event(conn, tenant_id, query_id, event_type, {"worker": worker})

    def _save_worker(
        self, conn: sqlite3.Connection, tenant_id: str, query_id: str, worker: dict
    ) -> None:
        self._update(
            conn,
            "workers",
            {
                "payload_json": _json(worker),
                "worker_state": worker["worker_state"],
                "candidates_seen": worker["candidates_seen"],
                "candidates_verified": worker["candidates_verified"],
                "recommendations_emitted": worker["recommendations_emitted"],
                "last_emitted_sequence": worker["last_emitted_sequence"],
                "emitted_event_count": worker["emitted_event_count"],
                "drained": int(worker["drained"]),
                "updated_at": worker["updated_at"],
            },
            {
                "tenant_id": tenant_id,
                "query_id": query_id,
                "library_id": worker["library_id"],
                "attempt": worker["attempt"],
            },
        )

    def commit_recommendation(
        self,
        tenant_id: str,
        query_id: str,
        worker: dict,
        recommendation: dict,
        producer_event_id: str,
    ) -> dict | None:
        producer_key = f"{worker['library_id']}:{worker['attempt']}:{producer_event_id}"
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            duplicate = conn.execute(
                "SELECT * FROM events WHERE tenant_id=? AND query_id=? AND producer_event_key=?",
                (tenant_id, query_id, producer_key),
            ).fetchone()
            if duplicate:
                return self._event_dict(dict(duplicate))
            if search["search_state"] != "running":
                raise StoreConflict("recommendations are not accepted after draining starts")
            previous_ranks = self._group_ranks(
                conn,
                tenant_id,
                query_id,
                recommendation["claim"]["claim_id"],
                recommendation["rankable_group"],
            )
            existing = conn.execute(
                "SELECT * FROM recommendations WHERE tenant_id=? AND query_id=? AND result_id=?",
                (tenant_id, query_id, recommendation["result_id"]),
            ).fetchone()
            event_type = "recommendation_created"
            previous_version = None
            if existing:
                previous = json.loads(existing["payload_json"])
                merged = self._merge_recommendation(previous, recommendation)
                if merged == previous:
                    return None
                previous_version = previous["result_version"]
                merged["result_version"] = previous_version + 1
                merged["updated_at"] = utc_now_iso()
                recommendation = merged
                event_type = "recommendation_merged"
                self._update(
                    conn,
                    "recommendations",
                    {
                        "payload_json": _json(recommendation),
                        "citable": int(recommendation["citable"]),
                        "rankable": int(recommendation["rankable"]),
                        "display_score": recommendation["display_score"],
                        "result_version": recommendation["result_version"],
                    },
                    {
                        "tenant_id": tenant_id,
                        "query_id": query_id,
                        "result_id": recommendation["result_id"],
                    },
                )
            else:
                self._insert(
                    conn,
                    "recommendations",
                    {
                        "tenant_id": tenant_id,
                        "query_id": query_id,
                        "result_id": recommendation["result_id"],
                        "work_id": recommendation["work_id"],
                        "claim_id": recommendation["claim"]["claim_id"],
                        "library_id": worker["library_id"],
                        "payload_json": _json(recommendation),
                        "citable": int(recommendation["citable"]),
                        "rankable": int(recommendation["rankable"]),
                        "display_score": recommendation["display_score"],
                        "result_version": recommendation["result_version"],
                        "updated_sequence": 0,
                    },
                )
            rank = self._rank_of(
                conn,
                tenant_id,
                query_id,
                recommendation["claim"]["claim_id"],
                recommendation["rankable_group"],
                recommendation["result_id"],
            )
            payload = {
                "recommendation": recommendation,
                "current_rank": rank,
                "rankable_group": recommendation["rankable_group"],
            }
            if event_type == "recommendation_merged":
                payload |= {
                    "merged_result_ids": [recommendation["result_id"]],
                    "previous_result_version": previous_version,
                }
            event = self._insert_event(conn, tenant_id, query_id, event_type, payload, producer_key)
            current_ranks = self._group_ranks(
                conn,
                tenant_id,
                query_id,
                recommendation["claim"]["claim_id"],
                recommendation["rankable_group"],
            )
            changes = []
            for result_id, current_rank in current_ranks.items():
                previous_rank = previous_ranks.get(result_id, 0)
                if result_id == recommendation["result_id"] or previous_rank == current_rank:
                    continue
                row = conn.execute(
                    """SELECT result_version FROM recommendations
                    WHERE tenant_id=? AND query_id=? AND result_id=?""",
                    (tenant_id, query_id, result_id),
                ).fetchone()
                changes.append(
                    {
                        "result_id": result_id,
                        "result_version": row["result_version"],
                        "previous_rank": previous_rank,
                        "current_rank": current_rank,
                        "rankable_group": recommendation["rankable_group"],
                    }
                )
            if changes:
                event = self._insert_event(
                    conn,
                    tenant_id,
                    query_id,
                    "recommendation_reranked",
                    {
                        "trigger_result_id": recommendation["result_id"],
                        "reason": "new_result"
                        if event_type == "recommendation_created"
                        else "evidence_merged",
                        "changes": changes,
                    },
                )
            worker |= {
                "last_emitted_sequence": event["sequence"],
                "local_watermark": event["sequence"],
                "emitted_event_count": worker["emitted_event_count"] + 1,
                "recommendations_emitted": worker["recommendations_emitted"] + 1,
                "updated_at": utc_now_iso(),
            }
            self._update(
                conn,
                "workers",
                {
                    "payload_json": _json(worker),
                    "last_emitted_sequence": event["sequence"],
                    "emitted_event_count": worker["emitted_event_count"],
                    "recommendations_emitted": worker["recommendations_emitted"],
                    "updated_at": worker["updated_at"],
                },
                {
                    "tenant_id": tenant_id,
                    "query_id": query_id,
                    "library_id": worker["library_id"],
                    "attempt": worker["attempt"],
                },
            )
            self._update(
                conn,
                "recommendations",
                {"updated_sequence": event["sequence"]},
                {
                    "tenant_id": tenant_id,
                    "query_id": query_id,
                    "result_id": recommendation["result_id"],
                },
            )
            return event

    @staticmethod
    def _merge_recommendation(previous: dict, incoming: dict) -> dict:
        sources = {item["paper_id"]: item for item in previous["sources"]}
        sources.update({item["paper_id"]: item for item in incoming["sources"]})
        evidence = {item["evidence_id"]: item for item in previous["evidence"]}
        evidence.update({item["evidence_id"]: item for item in incoming["evidence"]})
        scores = [
            value
            for value in (previous["display_score"], incoming["display_score"])
            if value is not None
        ]
        return {
            **previous,
            "sources": [sources[key] for key in sorted(sources)],
            "evidence": [evidence[key] for key in sorted(evidence)],
            "display_score": max(scores) if scores else None,
        }

    def _rank_of(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        query_id: str,
        claim_id: str,
        group: str | None,
        result_id: str,
    ) -> int:
        ranks = self._group_ranks(conn, tenant_id, query_id, claim_id, group)
        return ranks.get(result_id, 0)

    def _group_ranks(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        query_id: str,
        claim_id: str,
        group: str | None,
    ) -> dict[str, int]:
        if group is None or group == "evidence_only":
            return {}
        rows = conn.execute(
            """SELECT result_id, payload_json FROM recommendations
               WHERE tenant_id=? AND query_id=? AND claim_id=? AND rankable=1""",
            (tenant_id, query_id, claim_id),
        ).fetchall()
        items = [
            (row["result_id"], json.loads(row["payload_json"]))
            for row in rows
            if json.loads(row["payload_json"]).get("score_scale_group") == group
        ]
        items.sort(
            key=lambda item: (
                -float(item[1].get("display_score") or 0),
                -evidence_completeness(_evidence_list(item[1])),
                -scope_applicability(_evidence_list(item[1])),
                item[0],
            )
        )
        return {result_id: rank for rank, (result_id, _) in enumerate(items, start=1)}

    def commit_candidate_diagnostic(
        self,
        tenant_id: str,
        query_id: str,
        worker: dict,
        diagnostic: CandidateDiagnostic,
        claim_hash: str,
    ) -> dict:
        library_id = worker["library_id"]
        if diagnostic.source_uri:
            diagnostic = diagnostic.model_copy(
                update={"source_uris": {library_id: diagnostic.source_uri}}
            )
        with self._write() as conn:
            if self._search(conn, tenant_id, query_id) is None:
                raise KeyError(query_id)
            row = conn.execute(
                """SELECT payload_json FROM candidate_diagnostics
                WHERE tenant_id=? AND query_id=? AND result_id=?""",
                (tenant_id, query_id, diagnostic.result_id),
            ).fetchone()
            previous = CandidateDiagnostic.model_validate_json(row["payload_json"]) if row else None
            merged = merge_candidates(previous, diagnostic, claim_hash) if previous else diagnostic
            changed = previous != merged
            conn.execute(
                """INSERT INTO candidate_diagnostics (
                       tenant_id, query_id, result_id, paper_id, library_id,
                       payload_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, query_id, result_id) DO UPDATE SET
                       payload_json=excluded.payload_json""",
                (
                    tenant_id,
                    query_id,
                    merged.result_id,
                    merged.paper_id,
                    library_id,
                    merged.model_dump_json(),
                    utc_now_iso(),
                ),
            )
            event = self._insert_event(
                conn,
                tenant_id,
                query_id,
                "candidate_diagnostic" if changed else "worker_progress",
                {"candidate": merged.model_dump(mode="json")} if changed else {"worker": worker},
                f"candidate:{library_id}:{worker['attempt']}:{merged.work_id}",
            )
            worker |= {
                "last_emitted_sequence": event["sequence"],
                "local_watermark": event["sequence"],
                "updated_at": utc_now_iso(),
            }
            self._save_worker(conn, tenant_id, query_id, worker)
            return event

    def commit_candidate_provenance(
        self, tenant_id: str, query_id: str, candidate: CandidateDiagnostic
    ) -> bool:
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None or search["search_state"] != "running":
                return False
            row = conn.execute(
                "SELECT payload_json FROM candidate_diagnostics "
                "WHERE tenant_id=? AND query_id=? AND result_id=?",
                (tenant_id, query_id, candidate.result_id),
            ).fetchone()
            if row is None:
                raise StoreConflict("provenance update requires a known candidate")
            previous = CandidateDiagnostic.model_validate_json(row[0])
            if (previous.pool_version, previous.ranking_version) != (
                candidate.pool_version,
                candidate.ranking_version,
            ):
                raise StoreConflict("provenance update cannot replace another pool or ranking")
            conn.execute(
                "UPDATE candidate_diagnostics SET payload_json=? "
                "WHERE tenant_id=? AND query_id=? AND result_id=?",
                (candidate.model_dump_json(), tenant_id, query_id, candidate.result_id),
            )
            self._insert_event(
                conn,
                tenant_id,
                query_id,
                "candidate_provenance_updated",
                {"candidate": candidate.model_dump(mode="json")},
            )
            return True

    def commit_discovery_pool(
        self,
        tenant_id: str,
        query_id: str,
        ranking: dict,
        candidates: list[CandidateDiagnostic],
        *,
        reranked: bool = False,
    ) -> bool:
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            if search["search_state"] != "running":
                return False
            row = conn.execute(
                "SELECT payload_json FROM candidate_ranking_state "
                "WHERE tenant_id=? AND query_id=? AND claim_id=?",
                (tenant_id, query_id, ranking["claim_id"]),
            ).fetchone()
            previous = json.loads(row[0]) if row else None
            if reranked and (
                previous is None
                or previous["pool_version"] != ranking["pool_version"]
                or previous["pool_hash"] != ranking["pool_hash"]
            ):
                return False
            if not reranked and previous and ranking["pool_version"] <= previous["pool_version"]:
                raise StoreConflict("candidate pool versions must increase")
            self._save_discovery_pool(conn, tenant_id, query_id, ranking, candidates, reranked)
            return True

    def _save_discovery_pool(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        query_id: str,
        ranking: dict,
        candidates: list[CandidateDiagnostic],
        reranked: bool,
    ) -> None:
        for item in candidates:
            if item.claim_id != ranking["claim_id"] or item.pool_version != ranking["pool_version"]:
                raise StoreConflict("candidate pool identity mismatch")
            conn.execute(
                """INSERT INTO candidate_diagnostics
                (tenant_id, query_id, result_id, paper_id, library_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, query_id, result_id) DO UPDATE SET
                paper_id=excluded.paper_id, library_id=excluded.library_id,
                payload_json=excluded.payload_json""",
                (
                    tenant_id,
                    query_id,
                    item.result_id,
                    item.paper_id,
                    item.library_ids[0],
                    item.model_dump_json(),
                    utc_now_iso(),
                ),
            )
        conn.execute(
            """INSERT INTO candidate_ranking_state VALUES (?, ?, ?, ?)
            ON CONFLICT(tenant_id, query_id, claim_id)
            DO UPDATE SET payload_json=excluded.payload_json""",
            (tenant_id, query_id, ranking["claim_id"], _json(ranking)),
        )
        payload = {
            "ranking": ranking,
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
        if reranked:
            conn.execute(
                "INSERT INTO candidate_ranking_history VALUES (?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    query_id,
                    ranking["claim_id"],
                    ranking["ranking_version"],
                    _json(payload),
                ),
            )
        self._insert_event(
            conn,
            tenant_id,
            query_id,
            "candidate_reranked" if reranked else "candidate_pool_updated",
            payload,
        )

    def commit_ranking_phase(
        self, tenant_id: str, query_id: str, claim_id: str, pool_version: int, phase: RankingPhase
    ) -> bool:
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None or search["search_state"] != "running":
                return False
            where = (tenant_id, query_id, claim_id)
            row = conn.execute(
                "SELECT payload_json FROM candidate_ranking_state "
                "WHERE tenant_id=? AND query_id=? AND claim_id=?",
                where,
            ).fetchone()
            if row is None:
                return False
            ranking = json.loads(row[0])
            expected = None if phase == "blind" else "blind"
            if (
                ranking["status"] != "pending"
                or ranking["pool_version"] != pool_version
                or ranking.get("phase") != expected
            ):
                return False
            ranking["phase"] = phase
            conn.execute(
                "UPDATE candidate_ranking_state SET payload_json=? "
                "WHERE tenant_id=? AND query_id=? AND claim_id=?",
                (_json(ranking), *where),
            )
            self._insert_event(
                conn,
                tenant_id,
                query_id,
                "candidate_ranking_progress",
                {"claim_id": claim_id, "pool_version": pool_version, "phase": phase},
            )
            return True

    def get_candidate_ranking_history(self, tenant_id: str, query_id: str) -> list[dict]:
        with self._read() as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT payload_json FROM candidate_ranking_history "
                    "WHERE tenant_id=? AND query_id=? ORDER BY claim_id, ranking_version",
                    (tenant_id, query_id),
                )
            ]

    def get_cached_ranking(self, tenant_id: str, input_hash: str) -> dict | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT payload_json FROM ranking_cache WHERE tenant_id=? AND input_hash=?",
                (tenant_id, input_hash),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def cache_ranking(self, tenant_id: str, input_hash: str, payload: dict) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ranking_cache VALUES (?, ?, ?)",
                (tenant_id, input_hash, _json(payload)),
            )

    def get_candidate_diagnostics(self, tenant_id: str, query_id: str) -> list[dict]:
        with self._read() as conn:
            rows = conn.execute(
                """SELECT payload_json FROM candidate_diagnostics
                   WHERE tenant_id=? AND query_id=? ORDER BY result_id, library_id""",
                (tenant_id, query_id),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def finalize_search(
        self, tenant_id: str, query_id: str, session_state: str, termination_reason: str
    ) -> dict:
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            if search["final_snapshot_id"]:
                row = conn.execute(
                    """SELECT manifest_json FROM snapshots
                            WHERE tenant_id=? AND snapshot_id=?""",
                    (tenant_id, search["final_snapshot_id"]),
                ).fetchone()
                return json.loads(row["manifest_json"])
            cut = search["finalize_cut"]
            if cut is None:
                raise StoreConflict("finalize_cut must be fixed before final snapshot")
            recs = [
                json.loads(row["payload_json"])
                for row in conn.execute(
                    """SELECT payload_json FROM recommendations
                       WHERE tenant_id=? AND query_id=? AND citable=1 AND updated_sequence<=?
                       ORDER BY result_id""",
                    (tenant_id, query_id, cut),
                )
            ]
            workers = [
                json.loads(row["payload_json"])
                for row in conn.execute(
                    """SELECT payload_json FROM workers
                       WHERE tenant_id=? AND query_id=? ORDER BY library_id""",
                    (tenant_id, query_id),
                )
            ]
            coverage = coverage_from_workers(workers)
            claim_coverage = claim_coverage_from_workers(search["claim_count"], workers)
            is_partial = (
                any(worker["worker_state"] != "completed" for worker in workers)
                or claim_coverage["partial"] > 0
                or claim_coverage["failed"] > 0
                or claim_coverage["not_started"] > 0
            )
            final_state = (
                "partial" if is_partial and session_state == "completed" else session_state
            )
            now = utc_now_iso()
            bundle = _build_bundle(recs, now)
            EvidenceBundle.model_validate(bundle)
            existing_bundle = conn.execute(
                """SELECT manifest_json FROM evidence_bundles
                   WHERE tenant_id=? AND bundle_id=?""",
                (tenant_id, bundle["bundle_id"]),
            ).fetchone()
            if existing_bundle is None:
                self._insert(
                    conn,
                    "evidence_bundles",
                    {
                        "tenant_id": tenant_id,
                        "bundle_id": bundle["bundle_id"],
                        "bundle_hash": bundle["bundle_hash"],
                        "object_key": bundle["object_key"],
                        "manifest_json": _json(bundle),
                        "created_at": now,
                    },
                )
            else:
                persisted_bundle = json.loads(existing_bundle["manifest_json"])
                if (
                    persisted_bundle["bundle_hash"] != bundle["bundle_hash"]
                    or sha256_json({"items": persisted_bundle["items"]}) != bundle["bundle_hash"]
                    or {
                        key: value for key, value in persisted_bundle.items() if key != "created_at"
                    }
                    != {key: value for key, value in bundle.items() if key != "created_at"}
                ):
                    raise StoreConflict("evidence bundle content does not match its content hash")
            snapshot_id = f"snap_{uuid.uuid4().hex}"
            items = _snapshot_items(recs)
            score_groups = {
                item["rankable_group"]: {
                    "score_version": item["score_version"],
                    "calibration_status": "bootstrap",
                    "rubric_version": "rubric-0-3-v1",
                }
                for item in items
                if item["score_version"]
            }
            index_versions = {worker["library_id"]: worker["index_snapshot"] for worker in workers}
            model_versions: dict[str, str] = {}
            for worker in workers:
                model_versions.update(worker.get("model_versions") or {})
            manifest_core = {
                "snapshot_id": snapshot_id,
                "tenant_id": tenant_id,
                "query_id": query_id,
                "snapshot_kind": "final",
                "items": items,
                "item_count": len(items),
                "coverage": coverage,
                "claim_coverage": claim_coverage,
                "termination_reason": termination_reason,
                "is_partial": is_partial,
                "applied_through_sequence": cut,
                "event_set_hash": self._event_set_hash(conn, tenant_id, query_id, cut),
                "ranking_policy_version": "rank-policy-fixture-v1",
                "score_scale_groups": score_groups,
                "rubric_version": "rubric-0-3-v1",
                "index_versions": index_versions,
                "manuscript_context_version": None,
                "evidence_bundle_id": bundle["bundle_id"],
                "evidence_bundle_hash": bundle["bundle_hash"],
                "scoring_failure_count": 0,
                "uncalibrated_count": 0,
                "late_result_count": 0,
                "model_versions": model_versions,
                "calibration_versions": {"fixture-local-v1": "bootstrap-v1"},
                "status_check": {
                    "checked_at": now,
                    "retraction_notice_version": "fixture-none-v1",
                    "entitlement_version": "fixture-v1",
                },
                "supersedes_snapshot_id": None,
                "hash_alg": "SHA-256",
                "canonicalization": "RFC8785",
                "created_at": now,
            }
            manifest = {**manifest_core, "immutable_hash": sha256_json(manifest_core)}
            SnapshotManifest.model_validate(manifest)
            self._insert(
                conn,
                "snapshots",
                {
                    "tenant_id": tenant_id,
                    "query_id": query_id,
                    "snapshot_id": snapshot_id,
                    "snapshot_kind": "final",
                    "immutable_hash": manifest["immutable_hash"],
                    "manifest_json": _json(manifest),
                    "evidence_bundle_id": bundle["bundle_id"],
                    "evidence_bundle_hash": bundle["bundle_hash"],
                    "created_at": now,
                },
            )
            conn.execute(
                "UPDATE snapshot_requests SET snapshot_id=? WHERE tenant_id=? AND snapshot_id=?",
                (snapshot_id, tenant_id, f"pending:{query_id}"),
            )
            previous_state = search["search_state"]
            self._update(
                conn,
                "searches",
                {"search_state": final_state, "final_snapshot_id": snapshot_id, "updated_at": now},
                {"tenant_id": tenant_id, "query_id": query_id},
            )
            self._insert_event(
                conn,
                tenant_id,
                query_id,
                "search_state_changed",
                {"previous_state": previous_state, "current_state": final_state},
            )
            self._insert_event(
                conn,
                tenant_id,
                query_id,
                "search_finalized",
                {
                    "snapshot_id": snapshot_id,
                    "snapshot_kind": "final",
                    "coverage": coverage,
                    "termination_reason": termination_reason,
                    "immutable_hash": manifest["immutable_hash"],
                    "applied_through_sequence": cut,
                    "event_set_hash": manifest["event_set_hash"],
                },
            )
            return manifest

    def reserve_final_request(
        self, tenant_id: str, query_id: str, idempotency_key: str | None
    ) -> dict | None:
        request_hash = sha256_json({"query_id": query_id, "kind": "final"})
        with self._write() as conn:
            if idempotency_key:
                previous = conn.execute(
                    """SELECT request_hash, snapshot_id FROM snapshot_requests
                    WHERE tenant_id=? AND idempotency_key=?""",
                    (tenant_id, idempotency_key),
                ).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key belongs to another snapshot request"
                        )
                    if not previous["snapshot_id"].startswith("pending:"):
                        row = conn.execute(
                            """SELECT manifest_json FROM snapshots
                            WHERE tenant_id=? AND snapshot_id=?""",
                            (tenant_id, previous["snapshot_id"]),
                        ).fetchone()
                        return json.loads(row["manifest_json"])
                    return None
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            if search["final_snapshot_id"]:
                row = conn.execute(
                    """SELECT manifest_json FROM snapshots
                            WHERE tenant_id=? AND snapshot_id=?""",
                    (tenant_id, search["final_snapshot_id"]),
                ).fetchone()
                manifest = json.loads(row["manifest_json"])
                if idempotency_key:
                    self._insert(
                        conn,
                        "snapshot_requests",
                        {
                            "tenant_id": tenant_id,
                            "idempotency_key": idempotency_key,
                            "request_hash": request_hash,
                            "snapshot_id": search["final_snapshot_id"],
                        },
                    )
                return manifest
            if search["search_state"] in TERMINAL_STATES:
                raise StoreConflict("terminal search has no final snapshot")
            if idempotency_key:
                self._insert(
                    conn,
                    "snapshot_requests",
                    {
                        "tenant_id": tenant_id,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "snapshot_id": f"pending:{query_id}",
                    },
                )
            return None

    def create_checkpoint(
        self, tenant_id: str, query_id: str, idempotency_key: str | None = None
    ) -> dict:
        request_hash = sha256_json({"query_id": query_id, "kind": "checkpoint"})
        with self._write() as conn:
            if idempotency_key:
                previous = conn.execute(
                    """SELECT request_hash, snapshot_id FROM snapshot_requests
                    WHERE tenant_id=? AND idempotency_key=?""",
                    (tenant_id, idempotency_key),
                ).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key belongs to another snapshot request"
                        )
                    row = conn.execute(
                        """SELECT manifest_json FROM snapshots
                            WHERE tenant_id=? AND snapshot_id=?""",
                        (tenant_id, previous["snapshot_id"]),
                    ).fetchone()
                    return json.loads(row["manifest_json"])
            search = self._search(conn, tenant_id, query_id)
            if search is None:
                raise KeyError(query_id)
            if search["search_state"] in TERMINAL_STATES or search["final_snapshot_id"]:
                raise StoreConflict("checkpoint is not available after finalization")
            cut = search["applied_through_sequence"]
            recommendations = [
                json.loads(row["payload_json"])
                for row in conn.execute(
                    """SELECT payload_json FROM recommendations
                    WHERE tenant_id=? AND query_id=? AND citable=1
                    AND updated_sequence<=? ORDER BY result_id""",
                    (tenant_id, query_id, cut),
                )
            ]
            workers = [
                json.loads(row["payload_json"])
                for row in conn.execute(
                    """SELECT payload_json FROM workers
                    WHERE tenant_id=? AND query_id=? ORDER BY library_id""",
                    (tenant_id, query_id),
                )
            ]
            coverage = coverage_from_workers(workers)
            claims = claim_coverage_from_workers(search["claim_count"], workers)
            partial = any(worker["worker_state"] != "completed" for worker in workers)
            partial = partial or any(
                claims[state] for state in ("partial", "failed", "not_started")
            )
            now = utc_now_iso()
            bundle = _build_bundle(recommendations, now)
            existing_bundle = conn.execute(
                "SELECT bundle_hash FROM evidence_bundles WHERE tenant_id=? AND bundle_id=?",
                (tenant_id, bundle["bundle_id"]),
            ).fetchone()
            if existing_bundle and existing_bundle["bundle_hash"] != bundle["bundle_hash"]:
                raise StoreConflict("evidence bundle content collision")
            if not existing_bundle:
                self._insert(
                    conn,
                    "evidence_bundles",
                    {
                        "tenant_id": tenant_id,
                        "bundle_id": bundle["bundle_id"],
                        "bundle_hash": bundle["bundle_hash"],
                        "object_key": bundle["object_key"],
                        "manifest_json": _json(bundle),
                        "created_at": now,
                    },
                )
            items = _snapshot_items(recommendations, provisional=True)
            snapshot_id = f"snap_{uuid.uuid4().hex}"
            core = {
                "snapshot_id": snapshot_id,
                "tenant_id": tenant_id,
                "query_id": query_id,
                "snapshot_kind": "checkpoint",
                "items": items,
                "item_count": len(items),
                "coverage": coverage,
                "claim_coverage": claims,
                "termination_reason": "checkpoint_requested",
                "is_partial": partial,
                "applied_through_sequence": cut,
                "event_set_hash": self._event_set_hash(conn, tenant_id, query_id, cut),
                "ranking_policy_version": "rank-policy-fixture-v1",
                "score_scale_groups": {
                    item["rankable_group"]: {
                        "score_version": item["score_version"],
                        "calibration_status": "bootstrap",
                        "rubric_version": "rubric-0-3-v1",
                    }
                    for item in items
                    if item["score_version"]
                },
                "rubric_version": "rubric-0-3-v1",
                "index_versions": {
                    worker["library_id"]: worker["index_snapshot"] for worker in workers
                },
                "manuscript_context_version": None,
                "evidence_bundle_id": bundle["bundle_id"],
                "evidence_bundle_hash": bundle["bundle_hash"],
                "scoring_failure_count": 0,
                "uncalibrated_count": 0,
                "late_result_count": 0,
                "model_versions": {
                    key: value
                    for worker in workers
                    for key, value in worker["model_versions"].items()
                },
                "calibration_versions": {"fixture-local-v1": "bootstrap-v1"},
                "status_check": {
                    "checked_at": now,
                    "retraction_notice_version": "fixture-none-v1",
                    "entitlement_version": "fixture-v1",
                },
                "supersedes_snapshot_id": None,
                "hash_alg": "SHA-256",
                "canonicalization": "RFC8785",
                "created_at": now,
            }
            manifest = {**core, "immutable_hash": sha256_json(core)}
            SnapshotManifest.model_validate(manifest)
            self._insert(
                conn,
                "snapshots",
                {
                    "tenant_id": tenant_id,
                    "query_id": query_id,
                    "snapshot_id": snapshot_id,
                    "snapshot_kind": "checkpoint",
                    "immutable_hash": manifest["immutable_hash"],
                    "manifest_json": _json(manifest),
                    "evidence_bundle_id": bundle["bundle_id"],
                    "evidence_bundle_hash": bundle["bundle_hash"],
                    "created_at": now,
                },
            )
            if idempotency_key:
                self._insert(
                    conn,
                    "snapshot_requests",
                    {
                        "tenant_id": tenant_id,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "snapshot_id": snapshot_id,
                    },
                )
            self._update(
                conn,
                "searches",
                {"latest_checkpoint_id": snapshot_id},
                {"tenant_id": tenant_id, "query_id": query_id},
            )
            self._insert_event(
                conn,
                tenant_id,
                query_id,
                "search_finalized",
                {
                    "snapshot_id": snapshot_id,
                    "snapshot_kind": "checkpoint",
                    "coverage": coverage,
                    "termination_reason": "checkpoint_requested",
                    "immutable_hash": manifest["immutable_hash"],
                    "applied_through_sequence": cut,
                    "event_set_hash": manifest["event_set_hash"],
                },
            )
            return manifest

    def get_snapshot(self, tenant_id: str, snapshot_id: str) -> dict | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE tenant_id=? AND snapshot_id=?",
                (tenant_id, snapshot_id),
            ).fetchone()
            return dict(row) if row else None

    def get_bundle(self, tenant_id: str, bundle_id: str) -> dict | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM evidence_bundles WHERE tenant_id=? AND bundle_id=?",
                (tenant_id, bundle_id),
            ).fetchone()
            return dict(row) if row else None

    def get_evidence(self, tenant_id: str, snapshot_id: str, evidence_id: str) -> dict | None:
        snapshot = self.get_snapshot(tenant_id, snapshot_id)
        if snapshot is None:
            return None
        bundle = self.get_bundle(tenant_id, snapshot["evidence_bundle_id"])
        if bundle is None:
            return None
        for item in json.loads(bundle["manifest_json"])["items"]:
            if item["evidence_id"] == evidence_id:
                return item
        return None

    def settle_interrupted_work(self) -> None:
        """Close work left by a former service process without restarting it."""
        now = utc_now_iso()
        with self._write() as conn:
            searches = conn.execute(
                "SELECT * FROM searches "
                "WHERE search_state IN ('created','running','draining','finalizing')"
            ).fetchall()
            for search in searches:
                self._fail_search(
                    conn,
                    dict(search),
                    {
                        "code": "SEARCH_INTERRUPTED",
                        "message": "服务重启时检索未完成；此前提交的结果保留，可重新发起检索。",
                    },
                    now,
                )
            runs = conn.execute(
                "SELECT payload_json FROM agent_runs "
                "WHERE json_extract(payload_json,'$.status') IN ('pending','running')"
            ).fetchall()
            for row in runs:
                run = AgentRun.model_validate_json(row["payload_json"])
                self._save_agent_run(conn, self._interrupted_agent_run(run, now))
            rows = conn.execute("SELECT * FROM conversations WHERE active_run_id <> ''").fetchall()
            for row in rows:
                self._reconcile_active_turn(conn, row["tenant_id"], row, now=now)

    def fail_search(self, tenant_id: str, query_id: str, message: str) -> None:
        with self._write() as conn:
            search = self._search(conn, tenant_id, query_id)
            if search is not None and search["search_state"] not in TERMINAL_STATES:
                self._fail_search(
                    conn, search, {"code": "INTERNAL_ERROR", "message": message}, utc_now_iso()
                )

    def _fail_search(self, conn: sqlite3.Connection, search: dict, error: dict, now: str) -> None:
        tenant_id, query_id = search["tenant_id"], search["query_id"]
        workers = conn.execute(
            "SELECT payload_json FROM workers WHERE tenant_id=? AND query_id=? "
            "AND worker_state IN ('pending','running','draining') ORDER BY library_id, attempt",
            (tenant_id, query_id),
        ).fetchall()
        for row in workers:
            worker = json.loads(row["payload_json"])
            worker.update(worker_state="failed", last_error=error, ended_at=now, updated_at=now)
            if worker.get("source_agent_status") == "running":
                worker["source_agent_status"] = "failed"
            self._save_worker(conn, tenant_id, query_id, worker)
            self._insert_event(conn, tenant_id, query_id, "worker_failed", {"worker": worker})
        self._fail_pending_rankings(conn, tenant_id, query_id, error["code"], now)
        self._update(
            conn,
            "searches",
            {"search_state": "failed", "updated_at": now},
            {"tenant_id": tenant_id, "query_id": query_id},
        )
        self._insert_event(
            conn,
            tenant_id,
            query_id,
            "search_state_changed",
            {"previous_state": search["search_state"], "current_state": "failed", "error": error},
        )

    def _fail_pending_rankings(
        self, conn: sqlite3.Connection, tenant_id: str, query_id: str, code: str, now: str
    ) -> None:
        ranks = conn.execute(
            "SELECT payload_json FROM candidate_ranking_state WHERE tenant_id=? AND query_id=? "
            "AND json_extract(payload_json,'$.status')='pending' ORDER BY claim_id",
            (tenant_id, query_id),
        ).fetchall()
        for row in ranks:
            ranking = json.loads(row["payload_json"])
            version = ranking["ranking_version"] + 1
            ranking.update(
                status="failed",
                phase=None,
                failure_code=code,
                ranking_version=version,
                created_at=now,
            )
            candidates = []
            for row in conn.execute(
                "SELECT payload_json FROM candidate_diagnostics WHERE tenant_id=? AND query_id=? "
                "AND json_extract(payload_json,'$.claim_id')=? "
                "AND json_extract(payload_json,'$.pool_version')=? ORDER BY result_id",
                (tenant_id, query_id, ranking["claim_id"], ranking["pool_version"]),
            ):
                item = CandidateDiagnostic.model_validate_json(row["payload_json"])
                if item.ranking_status == "pending":
                    item = item.model_copy(
                        update={
                            "ranking_version": version,
                            "ranking_status": "failed",
                            "ranking_failure_code": code,
                        }
                    )
                candidates.append(item)
            self._save_discovery_pool(conn, tenant_id, query_id, ranking, candidates, reranked=True)

    def create_agent_run(
        self,
        run: AgentRun,
        idempotency_key: str | None,
        *,
        configuration: dict | None = None,
    ) -> tuple[AgentRun, bool]:
        request_hash = sha256_json(
            {"request": run.request.model_dump(mode="json"), "configuration": configuration}
        )
        with self._write() as conn:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM agent_runs WHERE tenant_id=? AND idempotency_key=?",
                    (run.tenant_id, idempotency_key),
                ).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "same Idempotency-Key with a different agent request"
                        )
                    return AgentRun.model_validate_json(row["payload_json"]), True
            self._insert(
                conn,
                "agent_runs",
                {
                    "tenant_id": run.tenant_id,
                    "run_id": run.run_id,
                    "request_hash": request_hash,
                    "idempotency_key": idempotency_key,
                    "payload_json": run.model_dump_json(),
                    "created_at": run.created_at,
                    "updated_at": run.updated_at,
                },
            )
        return run, False

    def get_agent_run(self, tenant_id: str, run_id: str) -> AgentRun | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT payload_json FROM agent_runs WHERE tenant_id=? AND run_id=?",
                (tenant_id, run_id),
            ).fetchone()
        return AgentRun.model_validate_json(row["payload_json"]) if row else None

    def get_agent_run_by_key(self, tenant_id: str, idempotency_key: str) -> AgentRun | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT payload_json FROM agent_runs WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
        return AgentRun.model_validate_json(row["payload_json"]) if row else None

    def create_conversation_agent_run(
        self,
        run: AgentRun,
        idempotency_key: str | None,
        *,
        configuration: dict | None = None,
        create_conversation: bool = False,
        expected_revision: int | None = None,
        project_id: str | None = None,
        request_intent: dict | None = None,
    ) -> tuple[AgentRun, bool]:
        user_message = run.user_message or ""
        request_hash = sha256_json(
            {
                "request": run.request.model_dump(mode="json"),
                "configuration": configuration,
                "user_message": user_message,
                "active_query_id": run.active_query_id,
                **(request_intent or {}),
            }
        )
        now = utc_now_iso()
        with self._write() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM agent_runs WHERE tenant_id=? AND idempotency_key=?",
                    (run.tenant_id, idempotency_key),
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "same Idempotency-Key with a different agent request"
                        )
                    replayed = AgentRun.model_validate_json(existing["payload_json"])
                    if (
                        run.conversation_id is not None
                        and replayed.conversation_id != run.conversation_id
                    ):
                        raise IdempotencyConflict(
                            "same Idempotency-Key bound to a different conversation"
                        )
                    return replayed, True
            if project_id is not None:
                project = conn.execute(
                    "SELECT archived FROM workspace_projects WHERE tenant_id=? AND project_id=?",
                    (run.tenant_id, project_id),
                ).fetchone()
                if project is None:
                    raise ConversationNotFound("project does not exist for this tenant")
                if project["archived"]:
                    raise ConversationConflict("project is archived")
            if run.conversation_id is None:
                if not create_conversation:
                    raise ValueError("conversation_id is required for a follow-up turn")
                run.conversation_id = f"cnv_{uuid.uuid4().hex}"
            row = self._conversation_row(conn, run.tenant_id, run.conversation_id)
            if create_conversation:
                if row is not None:
                    raise ConversationConflict("conversation already exists")
                self._insert(
                    conn,
                    "conversations",
                    {
                        "tenant_id": run.tenant_id,
                        "conversation_id": run.conversation_id,
                        "title": _conversation_title(user_message),
                        "project_id": project_id,
                        "revision": 0,
                        "active_run_id": "",
                        "latest_turn_index": 0,
                        "latest_anchor_json": (
                            run.evaluation_anchor.model_dump_json()
                            if run.evaluation_anchor
                            else None
                        ),
                        "last_active_query_id": "",
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                row = self._conversation_row(conn, run.tenant_id, run.conversation_id)
            if row is None:
                raise ConversationNotFound("conversation does not exist for this tenant")
            if row["project_id"] != project_id:
                raise ConversationConflict("conversation project cannot be changed")
            if expected_revision is not None and expected_revision != row["revision"]:
                raise ConversationConflict("conversation revision changed")
            if row["active_run_id"]:
                row = self._reconcile_active_turn(conn, run.tenant_id, row, now=now)
            if row["active_run_id"]:
                raise ConversationBusy("conversation already has an active run")
            message_count = conn.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE tenant_id=? AND conversation_id=?",
                (run.tenant_id, run.conversation_id),
            ).fetchone()[0]
            if message_count + 2 > MAX_CONVERSATION_MESSAGES:
                raise ConversationCapacity("conversation message capacity reached")
            turn_index = row["latest_turn_index"] + 1
            if turn_index > MAX_CONVERSATION_TURNS:
                raise ConversationCapacity("conversation turn capacity reached")
            active_query_id = run.active_query_id or (row["last_active_query_id"] or None)
            if (
                active_query_id
                and not conn.execute(
                    "SELECT 1 FROM conversation_artifacts "
                    "WHERE tenant_id=? AND conversation_id=? AND query_id=?",
                    (run.tenant_id, run.conversation_id, active_query_id),
                ).fetchone()
            ):
                raise ConversationQueryNotFound("active query does not belong to conversation")
            prior_query_ids = [
                item["query_id"]
                for item in conn.execute(
                    "SELECT query_id FROM conversation_artifacts "
                    "WHERE tenant_id=? AND conversation_id=? ORDER BY seq",
                    (run.tenant_id, run.conversation_id),
                ).fetchall()
            ]
            message_seq = (
                conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM conversation_messages "
                    "WHERE tenant_id=? AND conversation_id=?",
                    (run.tenant_id, run.conversation_id),
                ).fetchone()[0]
                + 1
            )
            self._insert(
                conn,
                "conversation_messages",
                {
                    "tenant_id": run.tenant_id,
                    "conversation_id": run.conversation_id,
                    "seq": message_seq,
                    "role": "user",
                    "text": user_message,
                    "run_id": run.run_id,
                    "created_at": now,
                },
            )
            self._insert(
                conn,
                "conversation_turns",
                {
                    "tenant_id": run.tenant_id,
                    "conversation_id": run.conversation_id,
                    "turn_index": turn_index,
                    "run_id": run.run_id,
                    "user_message_seq": message_seq,
                    "assistant_message_seq": None,
                    "status": "pending",
                    "active_query_id": active_query_id or "",
                    "summary": None,
                    "error_json": None,
                    "idempotency_key": idempotency_key,
                    "created_at": now,
                    "finished_at": None,
                },
            )
            self._update(
                conn,
                "conversations",
                {
                    "active_run_id": run.run_id,
                    "latest_turn_index": turn_index,
                    "latest_anchor_json": (
                        run.evaluation_anchor.model_dump_json()
                        if run.evaluation_anchor
                        else row["latest_anchor_json"]
                    ),
                    "last_active_query_id": active_query_id or "",
                    "updated_at": now,
                },
                {"tenant_id": run.tenant_id, "conversation_id": run.conversation_id},
            )
            run.turn_index = turn_index
            run.conversation_revision = row["revision"]
            run.active_query_id = active_query_id
            run.prior_query_ids = prior_query_ids
            self._insert(
                conn,
                "agent_runs",
                {
                    "tenant_id": run.tenant_id,
                    "run_id": run.run_id,
                    "request_hash": request_hash,
                    "idempotency_key": idempotency_key,
                    "payload_json": run.model_dump_json(),
                    "created_at": run.created_at,
                    "updated_at": run.updated_at,
                },
            )
        return run, False

    def attach_agent_query(self, tenant_id: str, run_id: str, query: AgentQuery) -> None:
        with self._write() as conn:
            self._attach_agent_query(conn, tenant_id, run_id, query)

    def _attach_agent_query(
        self, conn: sqlite3.Connection, tenant_id: str, run_id: str, query: AgentQuery
    ) -> None:
        row = conn.execute(
            "SELECT payload_json FROM agent_runs WHERE tenant_id=? AND run_id=?",
            (tenant_id, run_id),
        ).fetchone()
        if row is None:
            raise LookupError("agent run not found")
        run = AgentRun.model_validate_json(row["payload_json"])
        if run.status not in {"pending", "running"}:
            raise ConversationConflict("cannot attach a search to a terminal agent run")
        if query.created.query_id in {item.created.query_id for item in run.queries}:
            return
        run.queries.append(query)
        now = utc_now_iso()
        if run.conversation_id:
            artifact_seq = (
                conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM conversation_artifacts "
                    "WHERE tenant_id=? AND conversation_id=?",
                    (tenant_id, run.conversation_id),
                ).fetchone()[0]
                + 1
            )
            self._insert(
                conn,
                "conversation_artifacts",
                {
                    "tenant_id": tenant_id,
                    "conversation_id": run.conversation_id,
                    "seq": artifact_seq,
                    "run_id": run.run_id,
                    "turn_index": run.turn_index or 1,
                    "query_id": query.created.query_id,
                    "label": f"第{run.turn_index or 1}轮检索{len(run.queries)}",
                    "created_at": now,
                },
            )
        run.latest_query_id = query.created.query_id
        run.updated_at = now
        self._save_agent_run(conn, run)

    def save_agent_run(self, run: AgentRun) -> bool:
        with self._write() as conn:
            if run.status in {"pending", "running"}:
                updated = conn.execute(
                    "UPDATE agent_runs SET payload_json=?, updated_at=? "
                    "WHERE tenant_id=? AND run_id=? "
                    "AND json_extract(payload_json,'$.status') IN ('pending','running')",
                    (run.model_dump_json(), run.updated_at, run.tenant_id, run.run_id),
                )
                return updated.rowcount == 1
            self._save_agent_run(conn, run)
            return True

    def _save_agent_run(self, conn: sqlite3.Connection, run: AgentRun) -> None:
        self._update(
            conn,
            "agent_runs",
            {"payload_json": run.model_dump_json(), "updated_at": run.updated_at},
            {"tenant_id": run.tenant_id, "run_id": run.run_id},
        )

    @staticmethod
    def _interrupted_agent_run(run: AgentRun, now: str) -> AgentRun:
        preparation = run.citation_preparation
        if preparation is not None and preparation.status == "waiting":
            preparation = preparation.model_copy(update={"status": "partial"})
        return run.model_copy(
            update={
                "status": "failed",
                "summary": None,
                "error": {
                    "code": "AGENT_INTERRUPTED",
                    "message": "上一轮运行已中断；其结果不视为成功答复。",
                },
                "citation_preparation": preparation,
                "updated_at": now,
            }
        )

    def finish_conversation_run(self, run: AgentRun) -> Conversation | None:
        if not run.conversation_id:
            return None
        if run.status not in {"completed", "failed", "cancelled"}:
            raise ConversationConflict("cannot finish a conversation turn from a non-terminal run")
        now = utc_now_iso()
        with self._write() as conn:
            row = self._conversation_row(conn, run.tenant_id, run.conversation_id)
            turn = conn.execute(
                "SELECT * FROM conversation_turns WHERE tenant_id=? AND run_id=?",
                (run.tenant_id, run.run_id),
            ).fetchone()
            if row is None or turn is None:
                self._update(
                    conn,
                    "agent_runs",
                    {"payload_json": run.model_dump_json(), "updated_at": run.updated_at},
                    {"tenant_id": run.tenant_id, "run_id": run.run_id},
                )
                if row is not None and row["active_run_id"] == run.run_id:
                    self._update(
                        conn,
                        "conversations",
                        {"active_run_id": "", "updated_at": now},
                        {"tenant_id": run.tenant_id, "conversation_id": run.conversation_id},
                    )
                return None
            terminal = {"completed", "failed", "cancelled"}
            if turn["status"] in terminal and turn["status"] != run.status:
                reconciled = run.model_copy(
                    update={
                        "status": turn["status"],
                        "summary": turn["summary"],
                        "error": (json.loads(turn["error_json"]) if turn["error_json"] else None),
                        "conversation_revision": row["revision"],
                        "updated_at": now,
                    }
                )
                self._update(
                    conn,
                    "agent_runs",
                    {
                        "payload_json": reconciled.model_dump_json(),
                        "updated_at": reconciled.updated_at,
                    },
                    {"tenant_id": run.tenant_id, "run_id": run.run_id},
                )
                return self._conversation_payload(conn, row)
            if turn["status"] not in terminal:
                if row["active_run_id"] != run.run_id:
                    raise ConversationConflict("conversation active run changed")
                self._finish_turn(
                    conn,
                    run.tenant_id,
                    row,
                    turn,
                    status=run.status,
                    summary=run.summary,
                    error=run.error,
                    latest_query_id=run.latest_query_id,
                    now=now,
                )
                row = self._conversation_row(conn, run.tenant_id, run.conversation_id) or row
            elif row["active_run_id"] == run.run_id:
                self._update(
                    conn,
                    "conversations",
                    {"active_run_id": "", "updated_at": now},
                    {"tenant_id": run.tenant_id, "conversation_id": run.conversation_id},
                )
                row = self._conversation_row(conn, run.tenant_id, run.conversation_id) or row
            stored = run.model_copy(update={"conversation_revision": row["revision"]})
            self._update(
                conn,
                "agent_runs",
                {"payload_json": stored.model_dump_json(), "updated_at": stored.updated_at},
                {"tenant_id": stored.tenant_id, "run_id": stored.run_id},
            )
            if (
                self.learning_enabled
                and turn["status"] not in terminal
                and run.status in {"completed", "failed"}
                and run.request.input_type != "full_text"
            ):
                enqueue_learning(
                    conn, run.tenant_id, row["project_id"] or OTHER, turn["created_at"]
                )
            return self._conversation_payload(conn, row)

    def get_conversation(self, tenant_id: str, conversation_id: str) -> Conversation | None:
        with self._read() as conn:
            row = self._conversation_row(conn, tenant_id, conversation_id)
            return self._conversation_payload(conn, row) if row is not None else None

    def list_conversations(
        self,
        tenant_id: str,
        *,
        limit: int = 20,
        before: tuple[str, str] | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        columns = (
            "SELECT conversation_id, title, updated_at, project_id "
            "FROM conversations WHERE tenant_id=? "
        )
        params = [tenant_id]
        if project_id == OTHER:
            columns += "AND project_id IS NULL "
        elif project_id is not None:
            columns += "AND project_id=? "
            params.append(project_id)
        if before is not None:
            timestamp, conversation_id = before
            columns += "AND updated_at<=? AND (updated_at<? OR conversation_id>?) "
            params.extend((timestamp, timestamp, conversation_id))
        with self._read() as conn:
            rows = conn.execute(  # noqa: S608 - fixed SQL fragments
                columns + "ORDER BY updated_at DESC, conversation_id LIMIT ?",
                (*params, max(1, min(100, limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def rename_conversation(self, tenant_id: str, conversation_id: str, title: str) -> None:
        with self._write() as conn:
            changed = conn.execute(
                "UPDATE conversations SET title=? WHERE tenant_id=? AND conversation_id=?",
                (title, tenant_id, conversation_id),
            ).rowcount
            if not changed:
                raise ConversationNotFound("conversation does not exist for this tenant")

    def conversation_purge_scope(
        self, tenant_id: str, conversation_id: str
    ) -> dict[str, list[str]]:
        with self._read() as conn:
            if self._conversation_row(conn, tenant_id, conversation_id) is None:
                raise ConversationNotFound("conversation does not exist for this tenant")
            runs = conn.execute(
                "SELECT run_id FROM conversation_turns WHERE tenant_id=? AND conversation_id=?",
                (tenant_id, conversation_id),
            ).fetchall()
            queries = conn.execute(
                "SELECT a.query_id FROM conversation_artifacts a "
                "WHERE a.tenant_id=? AND a.conversation_id=? AND NOT EXISTS ("
                " SELECT 1 FROM conversation_artifacts other WHERE other.tenant_id=a.tenant_id "
                " AND other.query_id=a.query_id AND other.conversation_id<>a.conversation_id)",
                (tenant_id, conversation_id),
            ).fetchall()
        return {
            "conversation_ids": [conversation_id],
            "run_ids": [row["run_id"] for row in runs],
            "query_ids": [row["query_id"] for row in queries],
        }

    def account_purge_scope(self, tenant_id: str) -> dict[str, list[str]]:
        with self._read() as conn:
            return {
                "run_ids": [
                    row[0]
                    for row in conn.execute(
                        "SELECT run_id FROM agent_runs WHERE tenant_id=?",
                        (tenant_id,),
                    )
                ],
                "query_ids": [
                    row[0]
                    for row in conn.execute(
                        "SELECT query_id FROM searches WHERE tenant_id=?",
                        (tenant_id,),
                    )
                ],
                "manuscript_ids": [
                    row[0]
                    for row in conn.execute(
                        "SELECT manuscript_id FROM manuscripts WHERE tenant_id=?",
                        (tenant_id,),
                    )
                ],
            }

    def purge_account_rows(self, account_id: str) -> None:
        tenant_id = f"account:{account_id}"
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM manuscripts WHERE tenant_id=?", (tenant_id,)).fetchone():
                raise StoreConflict("remove account files before deleting account rows")
            for table in (
                "workspace_learning_queue",
                "workspace_source_requests",
                "workspace_projects",
                "conversation_messages",
                "conversation_turns",
                "conversation_artifacts",
                "conversations",
                "agent_runs",
                "snapshot_requests",
                "snapshots",
                "evidence_bundles",
                "candidate_ranking_history",
                "candidate_ranking_state",
                "candidate_diagnostics",
                "recommendations",
                "workers",
                "events",
                "searches",
                "reason_cache",
                "ranking_cache",
                "citation_text_pointers",
                "citation_texts",
                "citation_text_purged_queries",
                "manuscript_passages",
                "manuscript_deletions",
                "manuscript_upload_idempotency",
                "purge_receipts",
            ):
                conn.execute(f"DELETE FROM {table} WHERE tenant_id=?", (tenant_id,))  # noqa: S608
            conn.execute("DELETE FROM user_account_sessions WHERE account_id=?", (account_id,))
            conn.execute("DELETE FROM user_accounts WHERE account_id=?", (account_id,))

    def manuscript_purge_scope(self, tenant_id: str, manuscript_id: str) -> dict[str, list[str]]:
        with self._read() as conn:
            query_ids: set[str] = set()
            for row in conn.execute(
                "SELECT query_id, request_json, claims_json FROM searches WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall():
                try:
                    request = json.loads(row["request_json"])
                    claims = json.loads(row["claims_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                matched = (
                    request.get("input_type") == "full_text"
                    and request.get("manuscript_id") == manuscript_id
                )
                if not matched:
                    matched = any(
                        (claim.get("manuscript_anchor") or {}).get("manuscript_id") == manuscript_id
                        for claim in claims
                        if isinstance(claim, dict)
                    )
                if matched:
                    query_ids.add(row["query_id"])
            runs = []
            for row in conn.execute(
                "SELECT payload_json FROM agent_runs WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall():
                try:
                    runs.append(AgentRun.model_validate_json(row["payload_json"]))
                except Exception:  # noqa: S112 - legacy payloads are skipped by ID only
                    continue
            run_ids: set[str] = set()
            conversation_ids: set[str] = set()
            for run in runs:
                manifest = (run.context_manifest or {}).get("manuscript") or {}
                direct = (
                    (
                        run.request.input_type == "full_text"
                        and run.request.manuscript_id == manuscript_id
                    )
                    or (
                        isinstance(manifest, dict)
                        and manifest.get("manuscript_id") == manuscript_id
                    )
                    or any(
                        read.get("manuscript_id") == manuscript_id for read in run.manuscript_reads
                    )
                )
                via_query = any(query.created.query_id in query_ids for query in run.queries)
                if direct or via_query:
                    run_ids.add(run.run_id)
                    if run.conversation_id:
                        conversation_ids.add(run.conversation_id)
            for turn in conn.execute(
                "SELECT run_id, conversation_id FROM conversation_turns WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall():
                if turn["run_id"] in run_ids:
                    conversation_ids.add(turn["conversation_id"])
            changed = True
            while changed:
                changed = False
                for run in runs:
                    if run.run_id in run_ids:
                        continue
                    if run.conversation_id and run.conversation_id in conversation_ids:
                        run_ids.add(run.run_id)
                        changed = True
            for run in runs:
                if run.run_id not in run_ids:
                    continue
                if run.conversation_id:
                    conversation_ids.add(run.conversation_id)
                query_ids.update(query.created.query_id for query in run.queries)
        return {
            "conversation_ids": sorted(conversation_ids),
            "run_ids": sorted(run_ids),
            "query_ids": sorted(query_ids),
        }

    def purge_linked_data(
        self,
        tenant_id: str,
        *,
        conversation_ids: list[str],
        run_ids: list[str],
        query_ids: list[str],
    ) -> PurgeReceipt:
        receipt_id = f"prg_{uuid.uuid4().hex}"
        now = utc_now_iso()
        counts: dict[str, int] = {}
        limitations = {
            "external_model_logs": "not_covered",
            "evidence_bundle_objects": "metadata_only",
            "reason_and_ranking_caches": "tenant_scoped_conservative_clear",
            "other_tenants": "not_touched",
        }
        with self._write() as conn:
            _load_purge_ids(conn, "purge_q", query_ids)
            _load_purge_ids(conn, "purge_r", run_ids)
            _load_purge_ids(conn, "purge_c", conversation_ids)
            if conversation_ids:
                purge_learning_for_conversations(conn, tenant_id)
            snapshot_rows = conn.execute(
                "SELECT snapshot_id, evidence_bundle_id FROM snapshots "
                "WHERE tenant_id=? AND query_id IN (SELECT id FROM purge_q)",
                (tenant_id,),
            ).fetchall()
            _load_purge_ids(conn, "purge_s", [row["snapshot_id"] for row in snapshot_rows])
            _load_purge_ids(
                conn,
                "purge_b",
                [row["evidence_bundle_id"] for row in snapshot_rows],
            )

            def delete(sql: str, params: tuple = ()) -> None:
                counts_key = sql.split(" FROM ", 1)[1].split(" WHERE ", 1)[0].strip()
                cursor = conn.execute(sql, params)
                counts[counts_key] = max(0, cursor.rowcount)

            delete(
                "DELETE FROM snapshot_requests WHERE tenant_id=? "
                "AND snapshot_id IN (SELECT id FROM purge_s)",
                (tenant_id,),
            )
            delete(
                "DELETE FROM candidate_ranking_history WHERE tenant_id=? "
                "AND query_id IN (SELECT id FROM purge_q)",
                (tenant_id,),
            )
            delete(
                "DELETE FROM candidate_ranking_state WHERE tenant_id=? "
                "AND query_id IN (SELECT id FROM purge_q)",
                (tenant_id,),
            )
            for table in (
                "candidate_diagnostics",
                "recommendations",
                "workers",
                "events",
            ):
                delete(
                    f"DELETE FROM {table} WHERE tenant_id=? "  # noqa: S608
                    "AND query_id IN (SELECT id FROM purge_q)",
                    (tenant_id,),
                )
            delete(
                "DELETE FROM snapshots WHERE tenant_id=? AND query_id IN (SELECT id FROM purge_q)",
                (tenant_id,),
            )
            delete(
                "DELETE FROM evidence_bundles WHERE tenant_id=? "
                "AND bundle_id IN (SELECT id FROM purge_b) "
                "AND NOT EXISTS (SELECT 1 FROM snapshots s "
                "WHERE s.tenant_id=? AND s.evidence_bundle_id=evidence_bundles.bundle_id)",
                (tenant_id, tenant_id),
            )
            delete(
                "DELETE FROM searches WHERE tenant_id=? AND query_id IN (SELECT id FROM purge_q)",
                (tenant_id,),
            )
            delete(
                "DELETE FROM agent_runs WHERE tenant_id=? AND run_id IN (SELECT id FROM purge_r)",
                (tenant_id,),
            )
            for table in (
                "conversation_messages",
                "conversation_turns",
                "conversation_artifacts",
            ):
                delete(
                    f"DELETE FROM {table} WHERE tenant_id=? "  # noqa: S608
                    "AND conversation_id IN (SELECT id FROM purge_c)",
                    (tenant_id,),
                )
            delete(
                "DELETE FROM conversations WHERE tenant_id=? "
                "AND conversation_id IN (SELECT id FROM purge_c)",
                (tenant_id,),
            )
            delete("DELETE FROM reason_cache WHERE tenant_id=?", (tenant_id,))
            delete("DELETE FROM ranking_cache WHERE tenant_id=?", (tenant_id,))
            receipt = PurgeReceipt(
                receipt_id=receipt_id,
                status="completed",
                counts=counts,
                limitations=limitations,
                created_at=now,
            )
            self._insert(
                conn,
                "purge_receipts",
                {
                    "tenant_id": tenant_id,
                    "receipt_id": receipt.receipt_id,
                    "counts_json": _json(receipt.counts),
                    "limitations_json": _json(receipt.limitations),
                    "created_at": receipt.created_at,
                },
            )
        return receipt

    def get_purge_receipt(self, tenant_id: str, receipt_id: str) -> PurgeReceipt | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM purge_receipts WHERE tenant_id=? AND receipt_id=?",
                (tenant_id, receipt_id),
            ).fetchone()
        if row is None:
            return None
        return PurgeReceipt(
            receipt_id=row["receipt_id"],
            status="completed",
            counts=json.loads(row["counts_json"]),
            limitations=json.loads(row["limitations_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _conversation_row(
        conn: sqlite3.Connection, tenant_id: str, conversation_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM conversations WHERE tenant_id=? AND conversation_id=?",
            (tenant_id, conversation_id),
        ).fetchone()

    @staticmethod
    def _conversation_payload(conn: sqlite3.Connection, row: sqlite3.Row) -> Conversation:
        messages = [
            ConversationMessage(
                seq=item["seq"],
                role=item["role"],
                text=item["text"],
                run_id=item["run_id"],
                created_at=item["created_at"],
            )
            for item in conn.execute(
                "SELECT * FROM conversation_messages "
                "WHERE tenant_id=? AND conversation_id=? ORDER BY seq",
                (row["tenant_id"], row["conversation_id"]),
            ).fetchall()
        ]
        artifacts = [
            ConversationArtifact(
                seq=item["seq"],
                query_id=item["query_id"],
                run_id=item["run_id"],
                turn_index=item["turn_index"],
                label=item["label"],
                created_at=item["created_at"],
            )
            for item in conn.execute(
                "SELECT * FROM conversation_artifacts "
                "WHERE tenant_id=? AND conversation_id=? ORDER BY seq",
                (row["tenant_id"], row["conversation_id"]),
            ).fetchall()
        ]
        queries_by_run: dict[str, list[str]] = {}
        for artifact in artifacts:
            queries_by_run.setdefault(artifact.run_id, []).append(artifact.query_id)
        turns = [
            ConversationTurn(
                turn_index=item["turn_index"],
                run_id=item["run_id"],
                user_message_seq=item["user_message_seq"],
                assistant_message_seq=item["assistant_message_seq"],
                status=item["status"],
                query_ids=queries_by_run.get(item["run_id"], []),
                active_query_id=item["active_query_id"] or None,
                summary=item["summary"],
                error=json.loads(item["error_json"]) if item["error_json"] else None,
                created_at=item["created_at"],
                finished_at=item["finished_at"],
            )
            for item in conn.execute(
                "SELECT * FROM conversation_turns "
                "WHERE tenant_id=? AND conversation_id=? ORDER BY turn_index",
                (row["tenant_id"], row["conversation_id"]),
            ).fetchall()
        ]
        return Conversation(
            conversation_id=row["conversation_id"],
            tenant_id=row["tenant_id"],
            title=row["title"],
            project_id=row["project_id"],
            revision=row["revision"],
            active_run_id=row["active_run_id"] or None,
            latest_turn_index=row["latest_turn_index"],
            last_active_query_id=row["last_active_query_id"] or None,
            latest_anchor=(
                EvaluationAnchor.model_validate_json(row["latest_anchor_json"])
                if row["latest_anchor_json"]
                else None
            ),
            messages=messages,
            turns=turns,
            artifacts=artifacts,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _reconcile_active_turn(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        row: sqlite3.Row,
        *,
        now: str,
    ) -> sqlite3.Row:
        run_id = row["active_run_id"]
        if not run_id:
            return row
        turn = conn.execute(
            "SELECT * FROM conversation_turns WHERE tenant_id=? AND run_id=?",
            (tenant_id, run_id),
        ).fetchone()
        if turn is None:
            self._update(
                conn,
                "conversations",
                {"active_run_id": "", "updated_at": now},
                {"tenant_id": tenant_id, "conversation_id": row["conversation_id"]},
            )
            return self._conversation_row(conn, tenant_id, row["conversation_id"])
        if turn["status"] in {"completed", "failed", "cancelled"}:
            self._update(
                conn,
                "conversations",
                {"active_run_id": "", "updated_at": now},
                {"tenant_id": tenant_id, "conversation_id": row["conversation_id"]},
            )
            return self._conversation_row(conn, tenant_id, row["conversation_id"])
        run_row = conn.execute(
            "SELECT payload_json FROM agent_runs WHERE tenant_id=? AND run_id=?",
            (tenant_id, run_id),
        ).fetchone()
        if run_row is None:
            self._finish_turn(
                conn,
                tenant_id,
                row,
                turn,
                status="failed",
                summary=None,
                error={
                    "code": "AGENT_RUN_LOST",
                    "message": "上一轮运行记录缺失；不视为成功答复。",
                },
                latest_query_id=None,
                now=now,
            )
            return self._conversation_row(conn, tenant_id, row["conversation_id"])
        run = AgentRun.model_validate_json(run_row["payload_json"])
        if run.status in {"pending", "running"}:
            if not _run_is_stale(run.updated_at, now):
                return row
            run = self._interrupted_agent_run(run, now)
        self._finish_turn(
            conn,
            tenant_id,
            row,
            turn,
            status=run.status,
            summary=run.summary,
            error=run.error,
            latest_query_id=run.latest_query_id,
            now=now,
        )
        updated = self._conversation_row(conn, tenant_id, row["conversation_id"])
        self._save_agent_run(
            conn, run.model_copy(update={"conversation_revision": updated["revision"]})
        )
        return updated

    @staticmethod
    def _finish_turn(
        conn: sqlite3.Connection,
        tenant_id: str,
        row: sqlite3.Row,
        turn: sqlite3.Row,
        *,
        status: str,
        summary: str | None,
        error: dict | None,
        latest_query_id: str | None,
        now: str,
    ) -> None:
        assistant_seq = turn["assistant_message_seq"]
        if status == "completed" and summary:
            assistant_seq = (
                conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM conversation_messages "
                    "WHERE tenant_id=? AND conversation_id=?",
                    (tenant_id, row["conversation_id"]),
                ).fetchone()[0]
                + 1
            )
            Store._insert(
                conn,
                "conversation_messages",
                {
                    "tenant_id": tenant_id,
                    "conversation_id": row["conversation_id"],
                    "seq": assistant_seq,
                    "role": "assistant",
                    "text": summary,
                    "run_id": turn["run_id"],
                    "created_at": now,
                },
            )
        Store._update(
            conn,
            "conversation_turns",
            {
                "status": status,
                "assistant_message_seq": assistant_seq,
                "summary": summary,
                "error_json": _json(error) if error else None,
                "finished_at": now,
            },
            {
                "tenant_id": tenant_id,
                "conversation_id": row["conversation_id"],
                "turn_index": turn["turn_index"],
            },
        )
        Store._update(
            conn,
            "conversations",
            {
                "revision": row["revision"] + 1,
                "active_run_id": "",
                "last_active_query_id": latest_query_id or row["last_active_query_id"] or "",
                "updated_at": now,
            },
            {"tenant_id": tenant_id, "conversation_id": row["conversation_id"]},
        )

    def get_cached_reason(self, tenant_id: str, input_hash: str) -> RecommendationReason | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT payload_json FROM reason_cache WHERE tenant_id=? AND input_hash=?",
                (tenant_id, input_hash),
            ).fetchone()
        return RecommendationReason.model_validate_json(row["payload_json"]) if row else None

    def cache_reason(self, tenant_id: str, reason: RecommendationReason) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO reason_cache "
                "(tenant_id, input_hash, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    tenant_id,
                    reason.input_hash,
                    reason.model_dump_json(),
                    reason.generated_at,
                ),
            )


def _evidence_list(recommendation: dict) -> list[Evidence]:
    return [Evidence.model_validate(item) for item in recommendation["evidence"]]


def _build_bundle(recommendations: list[dict], now: str) -> dict:
    evidence = {
        item["evidence_id"]: item
        for recommendation in recommendations
        for item in recommendation["evidence"]
    }
    items = [evidence[key] for key in sorted(evidence)]
    bundle_hash = sha256_json({"items": items})
    return {
        "bundle_id": f"eb_{bundle_hash}",
        "bundle_hash": bundle_hash,
        "object_key": f"sqlite://evidence-bundles/{bundle_hash}",
        "item_count": len(items),
        "items": items,
        "created_at": now,
    }


def _snapshot_items(recommendations: list[dict], *, provisional: bool = False) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for recommendation in recommendations:
        group = recommendation.get("rankable_group") or "evidence_only"
        grouped.setdefault((recommendation["claim"]["claim_id"], group), []).append(recommendation)
    group_order = {"ssg:fixture-bootstrap-v1": 0, "evidence_only": 1}
    items: list[dict] = []
    for (claim_id, group), group_items in sorted(
        grouped.items(),
        key=lambda entry: (entry[0][0], group_order.get(entry[0][1], 9), entry[0][1]),
    ):
        group_items.sort(
            key=lambda item: (
                -float(item.get("display_score") or 0),
                -evidence_completeness(_evidence_list(item)),
                -scope_applicability(_evidence_list(item)),
                item["result_id"],
            )
        )
        for rank, recommendation in enumerate(group_items, start=1):
            evidence_ids = sorted(item["evidence_id"] for item in recommendation["evidence"])
            source_ids = sorted(
                {
                    source["paper_id"]
                    for source in recommendation["sources"]
                    if source["entitlement_status"] == "allowed"
                }
            )
            items.append(
                {
                    "result_id": recommendation["result_id"],
                    "work_id": recommendation["work_id"],
                    "claim_id": claim_id,
                    "tenant_id": recommendation["tenant_id"],
                    "query_id": recommendation["query_id"],
                    "claim_text": recommendation["claim"]["claim_text"],
                    "claim_type": recommendation["claim"]["claim_type"],
                    "claim_hash": recommendation["claim"]["claim_hash"],
                    "claim_scope": recommendation["claim"]["scope"],
                    "insertion_anchor": recommendation.get("insertion_anchor"),
                    "result_version": recommendation["result_version"],
                    "rank": rank,
                    "rankable_group": group,
                    "display_score": recommendation.get("display_score"),
                    "score_version": recommendation.get("score_version"),
                    "relationship": recommendation.get("relationship") or "insufficient",
                    "citable": True,
                    "provisional": provisional,
                    "source_record_ids": source_ids,
                    "evidence_ids": evidence_ids,
                    "evidence_set_hash": sha256_json({"evidence_ids": evidence_ids}),
                    "citation_text_id": recommendation.get("citation_text_id"),
                    "citation_text_hash": None,
                }
            )
    return items


def coverage_from_workers(workers: list[dict]) -> dict:
    buckets = {
        state: []
        for state in (
            "pending",
            "running",
            "draining",
            "completed",
            "failed",
            "timed_out",
            "cancelled",
        )
    }
    for worker in workers:
        buckets[worker["worker_state"]].append(worker["library_id"])
    return {
        "total_library_count": len(workers),
        "completed_library_ids": buckets["completed"],
        "failed_library_ids": buckets["failed"],
        "timed_out_library_ids": buckets["timed_out"],
        "cancelled_library_ids": buckets["cancelled"],
        "pending_library_ids": buckets["pending"],
        "running_library_ids": buckets["running"],
        "draining_library_ids": buckets["draining"],
    }


def claim_coverage_from_workers(total: int, workers: list[dict]) -> dict:
    states = [worker["worker_state"] for worker in workers]
    if total == 0:
        return {"total": 0, "completed": 0, "partial": 0, "failed": 0, "not_started": 0}
    if states and all(state == "completed" for state in states):
        return {"total": total, "completed": total, "partial": 0, "failed": 0, "not_started": 0}
    if any(state == "completed" for state in states):
        return {"total": total, "completed": 0, "partial": total, "failed": 0, "not_started": 0}
    if any(state in WORKER_TERMINAL for state in states):
        return {"total": total, "completed": 0, "partial": 0, "failed": total, "not_started": 0}
    return {"total": total, "completed": 0, "partial": 0, "failed": 0, "not_started": total}
