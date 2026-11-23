"""Transactional inbox, polling leases, and private session-log projections."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .accounts import AccountError
from .entities import utc_now_iso
from .project_connectors import TITLES, Connector
from .project_schema import OTHER

if TYPE_CHECKING:
    from .store import Store

LEASE_SECONDS = 150
DISCOVERY_COOLDOWN = 90
SCAN_MESSAGES = 8
SCAN_MESSAGE_CHARS = 1_800


@dataclass(frozen=True, slots=True)
class ScanJob:
    tenant_id: str
    project_id: str
    generation: int
    lease: str
    attempts: int


@dataclass(frozen=True, slots=True)
class InstallJob:
    tenant_id: str
    project_id: str
    application_id: str
    connector_id: str
    connector_version: str
    lease: str


@dataclass(frozen=True, slots=True)
class Finding:
    connector_id: str
    reason: str
    conversation_id: str
    seq: int


class LearningStore:
    def __init__(self, store: Store, connectors: dict[str, Connector]):
        self.store = store
        self.connectors = dict(connectors)

    def claim_scan(self) -> ScanJob | None:
        now = int(time.time())
        with self.store._write() as conn:
            conn.execute(
                "DELETE FROM workspace_learning_queue WHERE "
                "(leased_until<=? AND attempts>=2) OR (generation=0 AND ready_at<=?)",
                (now, now),
            )
            row = conn.execute(
                """SELECT q.* FROM workspace_learning_queue q JOIN workspace_projects p
                   ON (q.tenant_id,q.project_id)=(p.tenant_id,p.project_id)
                   WHERE q.generation>0 AND q.ready_at<=? AND q.leased_until<=? AND p.archived=0
                   AND p.learning_since IS NOT NULL ORDER BY q.ready_at, q.rowid LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            lease = uuid.uuid4().hex
            conn.execute(
                """UPDATE workspace_learning_queue SET lease=?, leased_until=?, attempts=attempts+1
                   WHERE tenant_id=? AND project_id=?""",
                (lease, now + LEASE_SECONDS, row["tenant_id"], row["project_id"]),
            )
            return ScanJob(
                row["tenant_id"], row["project_id"], row["generation"], lease, row["attempts"] + 1
            )

    @staticmethod
    def _scan_is_live(conn, job: ScanJob) -> bool:
        return (
            conn.execute(
                """SELECT 1 FROM workspace_learning_queue q JOIN workspace_projects p
               ON (q.tenant_id,q.project_id)=(p.tenant_id,p.project_id)
               WHERE q.tenant_id=? AND q.project_id=? AND q.lease=? AND q.leased_until>?
               AND p.archived=0 AND p.learning_since IS NOT NULL""",
                (job.tenant_id, job.project_id, job.lease, int(time.time())),
            ).fetchone()
            is not None
        )

    def scan_is_live(self, job: ScanJob) -> bool:
        with self.store._read() as conn:
            return self._scan_is_live(conn, job)

    def session_log(self, job: ScanJob) -> list[dict]:
        # The projection contains no assistant transcript, manuscript, query or model payload.
        # join to agent_runs only to exclude any conversation that has used a manuscript.
        with self.store._read() as conn:
            if not self._scan_is_live(conn, job):
                return []
            rows = conn.execute(
                """SELECT m.conversation_id, m.seq, substr(m.text,1,?) AS text,
                   (SELECT COUNT(*) FROM conversation_artifacts a WHERE a.tenant_id=m.tenant_id
                    AND a.conversation_id=m.conversation_id AND a.run_id=m.run_id) AS searches,
                   t.status FROM conversation_messages m
                   JOIN conversations c USING (tenant_id,conversation_id)
                   JOIN conversation_turns t USING (tenant_id,conversation_id,run_id)
                   JOIN workspace_projects p ON p.tenant_id=c.tenant_id
                     AND p.project_id=COALESCE(c.project_id, ?)
                   WHERE m.tenant_id=? AND p.project_id=? AND m.role='user'
                     AND t.status IN ('completed','failed') AND t.created_at>=p.learning_since
                     AND NOT EXISTS (
                       SELECT 1 FROM conversation_turns ft JOIN agent_runs r USING(tenant_id,run_id)
                       WHERE ft.tenant_id=m.tenant_id AND ft.conversation_id=m.conversation_id
                       AND json_extract(r.payload_json,'$.request.input_type')='full_text')
                   ORDER BY t.finished_at DESC, m.conversation_id, m.seq DESC LIMIT ?""",
                (SCAN_MESSAGE_CHARS, OTHER, job.tenant_id, job.project_id, SCAN_MESSAGES),
            ).fetchall()
        return [dict(row) for row in rows]

    def known(self, job: ScanJob) -> set[str]:
        with self.store._read() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT connector_id, connector_version FROM workspace_source_requests "
                    "WHERE tenant_id=? AND project_id=? AND status<>'cancelled'",
                    (job.tenant_id, job.project_id),
                )
                if (recipe := self.connectors.get(row[0])) and recipe.version == row[1]
            }

    def finish_scan(self, job: ScanJob, finding: Finding | None, *, retry: bool = False) -> None:
        scope = (job.tenant_id, job.project_id, job.lease)
        with self.store._write() as conn:
            if not self._scan_is_live(conn, job):
                return
            if finding is not None:
                recipe = self.connectors[finding.connector_id]
                # Recheck that the provenance still exists and belongs to this consent epoch.
                conn.execute(
                    """INSERT INTO workspace_source_requests
                         (tenant_id,project_id,application_id,connector_id,connector_version,reason,
                          conversation_id,message_seq,status,updated_at)
                       SELECT c.tenant_id,?,?,?, ?,?,c.conversation_id,m.seq,'pending',?
                       FROM conversations c
                       JOIN conversation_messages m USING(tenant_id,conversation_id)
                       JOIN workspace_projects p ON p.tenant_id=c.tenant_id
                         AND p.project_id=COALESCE(c.project_id, ?)
                       WHERE c.tenant_id=? AND c.conversation_id=? AND m.seq=? AND m.role='user'
                         AND p.project_id=? AND m.created_at>=p.learning_since
                       ON CONFLICT(tenant_id,project_id,connector_id) DO UPDATE SET
                         status='pending', connector_version=excluded.connector_version,
                         application_id=excluded.application_id,
                         reason=excluded.reason, conversation_id=excluded.conversation_id,
                         message_seq=excluded.message_seq, updated_at=excluded.updated_at,
                         closed=0, attempts=0, approved_at=NULL, error_code=NULL, receipt_json=NULL
                       WHERE workspace_source_requests.status='cancelled'
                          OR workspace_source_requests.connector_version
                             <> excluded.connector_version""",
                    (
                        job.project_id,
                        f"src_{uuid.uuid4().hex}",
                        finding.connector_id,
                        recipe.version,
                        finding.reason,
                        utc_now_iso(),
                        OTHER,
                        job.tenant_id,
                        finding.conversation_id,
                        finding.seq,
                        job.project_id,
                    ),
                )
            if retry and job.attempts < 2:
                conn.execute(
                    """UPDATE workspace_learning_queue SET ready_at=?, lease=NULL, leased_until=0
                       WHERE tenant_id=? AND project_id=? AND lease=?""",
                    (int(time.time()) + 30, *scope),
                )
            else:
                # Retain a low-cost cooldown row, even when up to date; only new turns dirty it.
                conn.execute(
                    """UPDATE workspace_learning_queue SET ready_at=?, lease=NULL, leased_until=0,
                       attempts=0, generation=MAX(0,generation-?)
                       WHERE tenant_id=? AND project_id=? AND lease=?""",
                    (int(time.time()) + DISCOVERY_COOLDOWN, job.generation, *scope),
                )

    def inbox(self, tenant_id: str) -> list[dict]:
        with self.store._read() as conn:
            rows = conn.execute(
                """SELECT r.*, p.name AS project_name FROM workspace_source_requests r
                   JOIN workspace_projects p USING(tenant_id,project_id)
                   WHERE r.tenant_id=? AND r.closed=0 AND p.archived=0
                   ORDER BY r.updated_at DESC, r.application_id LIMIT 60""",
                (tenant_id,),
            ).fetchall()
        return [self._payload(row) for row in rows if row["connector_id"] in self.connectors]

    def _payload(self, row) -> dict:
        recipe = self.connectors[row["connector_id"]]
        status = row["status"]
        if recipe.version != row["connector_version"]:
            status = "outdated"
        return {
            "application_id": row["application_id"],
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "status": status,
            "connector": recipe.public() | {"version": row["connector_version"]},
            "reason": TITLES[row["reason"]],
            "conversation_id": row["conversation_id"],
            "message_seq": row["message_seq"],
            "updated_at": row["updated_at"],
            "error_code": row["error_code"],
            "probe": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
        }

    def decide(self, tenant_id: str, application_id: str, version: str, decision: str) -> dict:
        with self.store._write() as conn:
            row = conn.execute(
                """SELECT r.*, p.name AS project_name, p.archived FROM workspace_source_requests r
                   JOIN workspace_projects p USING(tenant_id,project_id)
                   WHERE r.tenant_id=? AND r.application_id=?""",
                (tenant_id, application_id),
            ).fetchone()
            if row is None or row["archived"]:
                raise AccountError(404, "SOURCE_REQUEST_NOT_FOUND", "没有找到这条申请。")
            recipe = self.connectors.get(row["connector_id"])
            status = row["status"]
            if version != row["connector_version"] or not recipe:
                raise AccountError(409, "SOURCE_REQUEST_CHANGED", "接入方案已变化，请重新读取。")
            outdated = recipe.version != version
            if decision == "close" and (status in {"ready", "failed"} or outdated):
                conn.execute(
                    "UPDATE workspace_source_requests SET closed=1 "
                    "WHERE tenant_id=? AND application_id=?",
                    (tenant_id, application_id),
                )
                return self._payload(row) | {"closed": True}
            if not outdated and decision in {"approve", "retry"}:
                expected = "pending" if decision == "approve" else "failed"
                if (decision == "approve" and status in {"queued", "testing", "ready"}) or (
                    decision == "retry" and status in {"queued", "testing"}
                ):
                    return self._payload(row)
                if status == expected and not row["closed"]:
                    now = utc_now_iso()
                    conn.execute(
                        """UPDATE workspace_source_requests SET status='queued', approved_at=?,
                           attempts=0, error_code=NULL, updated_at=?
                           WHERE tenant_id=? AND application_id=?""",
                        (now, now, tenant_id, application_id),
                    )
                    return self._payload(row) | {
                        "status": "queued",
                        "error_code": None,
                        "updated_at": now,
                    }
            if decision == "decline" and status in {"pending", "declined"}:
                conn.execute(
                    """UPDATE workspace_source_requests SET status='declined', closed=1,
                       updated_at=? WHERE tenant_id=? AND application_id=?""",
                    (utc_now_iso(), tenant_id, application_id),
                )
                return self._payload(row) | {"status": "declined", "closed": True}
            raise AccountError(409, "SOURCE_REQUEST_STATE", "这条申请当前不能执行此操作。")

    def claim_install(self) -> InstallJob | None:
        now = int(time.time())
        with self.store._write() as conn:
            conn.execute(
                """UPDATE workspace_source_requests SET status='failed',
                   error_code='INSTALL_INTERRUPTED', updated_at=?,
                   lease=NULL, leased_until=0
                   WHERE status='testing' AND leased_until<=? AND attempts>=2""",
                (utc_now_iso(), now),
            )
            row = conn.execute(
                """SELECT r.* FROM workspace_source_requests r JOIN workspace_projects p
                   USING(tenant_id,project_id) WHERE r.approved_at IS NOT NULL AND p.archived=0
                   AND (r.status='queued' OR (r.status='testing' AND r.leased_until<=?))
                   ORDER BY r.updated_at, r.application_id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            lease = uuid.uuid4().hex
            conn.execute(
                """UPDATE workspace_source_requests SET status='testing', lease=?, leased_until=?,
                   attempts=attempts+1, updated_at=? WHERE tenant_id=? AND application_id=?""",
                (
                    lease,
                    now + LEASE_SECONDS,
                    utc_now_iso(),
                    row["tenant_id"],
                    row["application_id"],
                ),
            )
            return InstallJob(
                row["tenant_id"],
                row["project_id"],
                row["application_id"],
                row["connector_id"],
                row["connector_version"],
                lease,
            )

    def install_is_live(self, job: InstallJob) -> bool:
        with self.store._read() as conn:
            return (
                conn.execute(
                    """SELECT 1 FROM workspace_source_requests
                       WHERE tenant_id=? AND application_id=? AND lease=?
                       AND status='testing' AND leased_until>?""",
                    (job.tenant_id, job.application_id, job.lease, int(time.time())),
                ).fetchone()
                is not None
            )

    def finish_install(self, job: InstallJob, receipt: dict | None, error: str | None) -> None:
        recipe = self.connectors.get(job.connector_id)
        if (receipt is None) == (error is None):
            raise ValueError("install needs exactly one receipt or error")
        if receipt and (
            not recipe
            or recipe.version != job.connector_version
            or receipt.get("version") != job.connector_version
            or receipt.get("records", 0) <= 0
        ):
            raise ValueError("receipt is not for the approved version")
        with self.store._write() as conn:
            conn.execute(
                """UPDATE workspace_source_requests SET status=?, error_code=?, receipt_json=?,
                   lease=NULL, leased_until=0, updated_at=?
                   WHERE tenant_id=? AND application_id=? AND status='testing' AND lease=?
                   AND approved_at IS NOT NULL AND leased_until>?""",
                (
                    "ready" if receipt else "failed",
                    error,
                    json.dumps(receipt) if receipt else None,
                    utc_now_iso(),
                    job.tenant_id,
                    job.application_id,
                    job.lease,
                    int(time.time()),
                ),
            )
