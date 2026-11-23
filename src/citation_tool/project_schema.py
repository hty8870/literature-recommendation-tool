"""Additive schema for account-owned projects and leased learning work."""

from __future__ import annotations

import sqlite3
import time

OTHER = "other"
SCAN_QUEUE_CAPACITY = 200
SCAN_DELAY_S = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_projects (
    tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, name TEXT NOT NULL,
    name_key TEXT NOT NULL, share_sources INTEGER NOT NULL,
    selected_json TEXT, learning_since TEXT, archived INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, project_id), UNIQUE (tenant_id, name_key)
);
CREATE TABLE IF NOT EXISTS workspace_learning_queue (
    tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
    ready_at INTEGER NOT NULL, lease TEXT, leased_until INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, project_id)
);
CREATE INDEX IF NOT EXISTS workspace_learning_ready ON workspace_learning_queue (ready_at);
CREATE TABLE IF NOT EXISTS workspace_source_requests (
    tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, application_id TEXT NOT NULL,
    connector_id TEXT NOT NULL, connector_version TEXT NOT NULL, reason TEXT NOT NULL,
    conversation_id TEXT, message_seq INTEGER, status TEXT NOT NULL,
    updated_at TEXT NOT NULL, approved_at TEXT, closed INTEGER NOT NULL DEFAULT 0,
    error_code TEXT, receipt_json TEXT, lease TEXT, leased_until INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, application_id), UNIQUE (tenant_id, project_id, connector_id)
);
CREATE INDEX IF NOT EXISTS workspace_source_inbox
    ON workspace_source_requests (tenant_id, closed, updated_at DESC);
CREATE INDEX IF NOT EXISTS workspace_source_dispatch
    ON workspace_source_requests (status, updated_at);
"""


def enqueue_learning(
    conn: sqlite3.Connection, tenant_id: str, project_id: str, turn_created_at: str
) -> None:
    if not tenant_id.startswith("account:"):
        return
    room = conn.execute(
        "SELECT COUNT(*) < ? FROM workspace_learning_queue", (SCAN_QUEUE_CAPACITY,)
    ).fetchone()[0]
    if not room:
        conn.execute(
            """UPDATE workspace_learning_queue SET generation=generation+1
               WHERE tenant_id=? AND project_id=? AND EXISTS (
                 SELECT 1 FROM workspace_projects p WHERE p.tenant_id=? AND p.project_id=?
                 AND p.archived=0 AND p.learning_since<=?)""",
            (tenant_id, project_id, tenant_id, project_id, turn_created_at),
        )
        return
    conn.execute(
        """INSERT INTO workspace_learning_queue(tenant_id, project_id, ready_at)
           SELECT tenant_id, project_id, ? FROM workspace_projects
           WHERE tenant_id=? AND project_id=? AND archived=0 AND learning_since<=?
           ON CONFLICT(tenant_id, project_id) DO UPDATE SET
             generation=generation+1""",
        (int(time.time()) + SCAN_DELAY_S, tenant_id, project_id, turn_created_at),
    )


def purge_learning_for_conversations(conn: sqlite3.Connection, tenant_id: str) -> None:
    conn.execute(
        """DELETE FROM workspace_source_requests WHERE tenant_id=?
           AND conversation_id IN (SELECT id FROM purge_c) AND status<>'ready'""",
        (tenant_id,),
    )
    conn.execute(
        """UPDATE workspace_source_requests SET conversation_id=NULL, message_seq=NULL
           WHERE tenant_id=? AND conversation_id IN (SELECT id FROM purge_c) AND status='ready'""",
        (tenant_id,),
    )
    conn.execute(
        """DELETE FROM workspace_learning_queue WHERE tenant_id=? AND project_id IN (
             SELECT COALESCE(project_id, ?) FROM conversations WHERE tenant_id=?
             AND conversation_id IN (SELECT id FROM purge_c))""",
        (tenant_id, OTHER, tenant_id),
    )
