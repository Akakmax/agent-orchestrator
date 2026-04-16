"""SQLite database layer for the orchestrator.

Uses WAL mode for concurrent read/write safety.
All orchestrator writes go through this module — never raw SQL outside.
Follows the same patterns as blocks/hands/kanban/db.py.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import OrchestratorConfig

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS builds (
    id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    spec_path TEXT,
    project_path TEXT,
    git_branch TEXT,
    status TEXT NOT NULL DEFAULT 'planning',
    current_sprint INTEGER NOT NULL DEFAULT 0,
    total_sprints INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sprints (
    id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL REFERENCES builds(id),
    sprint_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    contract_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    negotiation_phase TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    sprint_id TEXT NOT NULL REFERENCES sprints(id),
    version INTEGER NOT NULL DEFAULT 1,
    proposed_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    criteria TEXT NOT NULL,
    review_notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id TEXT NOT NULL REFERENCES builds(id),
    sprint_id TEXT,
    from_agent TEXT NOT NULL,
    to_agent TEXT,
    msg_type TEXT NOT NULL,
    body TEXT NOT NULL,
    parent_id INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id TEXT NOT NULL REFERENCES builds(id),
    sprint_id TEXT,
    agent TEXT NOT NULL,
    session_id TEXT,
    log_path TEXT,
    summary TEXT,
    duration_seconds INTEGER,
    exit_code INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrospectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id TEXT NOT NULL REFERENCES builds(id),
    findings TEXT NOT NULL,
    changes_made TEXT,
    before_snapshot TEXT,
    approved_by TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


class OrchestratorDB:
    def __init__(self, db_path: str = OrchestratorConfig.DB_PATH):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── Builds ───────────────────────────────────────────────────────

    def create_build(self, prompt: str) -> dict:
        build_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO builds (id, prompt, status, current_sprint, total_sprints,
               created_at, updated_at) VALUES (?, ?, 'planning', 0, 0, ?, ?)""",
            (build_id, prompt, now, now),
        )
        self.conn.commit()
        return self.get_build(build_id)

    def get_build(self, build_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM builds WHERE id = ?", (build_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_build(self, build_id: str, **kwargs) -> dict:
        # Filter out None values — update_build can't NULL-ify fields.
        # This is intentional: callers pass keyword args and some may be None
        # when they don't want to change that field. Use direct SQL for NULL-ification.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        kwargs["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [build_id]
        self.conn.execute(f"UPDATE builds SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get_build(build_id)

    def list_builds(self, status: Optional[str] = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM builds WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM builds ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Sprints ──────────────────────────────────────────────────────

    def create_sprint(self, build_id: str, sprint_number: int, title: str,
                      description: Optional[str] = None) -> dict:
        sprint_id = _uuid()
        now = _now()
        self.conn.execute(
            """INSERT INTO sprints (id, build_id, sprint_number, title, description,
               status, attempts, max_attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (sprint_id, build_id, sprint_number, title, description,
             OrchestratorConfig.SPRINT_MAX_ATTEMPTS, now, now),
        )
        self.conn.commit()
        return self.get_sprint(sprint_id)

    def get_sprint(self, sprint_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM sprints WHERE id = ?", (sprint_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_sprints(self, build_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sprints WHERE build_id = ? ORDER BY sprint_number",
            (build_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_sprint(self, sprint_id: str, **kwargs) -> dict:
        # Unlike update_build, this does NOT filter None values.
        # This is needed because negotiation_phase=None is a valid state
        # (clearing the phase after negotiation completes).
        kwargs["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [sprint_id]
        self.conn.execute(f"UPDATE sprints SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get_sprint(sprint_id)

    def increment_sprint_attempts(self, sprint_id: str) -> dict:
        self.conn.execute(
            "UPDATE sprints SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (_now(), sprint_id),
        )
        self.conn.commit()
        return self.get_sprint(sprint_id)

    # ── Contracts ────────────────────────────────────────────────────

    def create_contract(self, sprint_id: str, proposed_by: str, criteria: dict,
                        review_notes: Optional[str] = None) -> dict:
        contract_id = _uuid()
        # Auto-increment version per sprint_id
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM contracts WHERE sprint_id = ?",
            (sprint_id,),
        ).fetchone()
        version = row[0] + 1
        now = _now()
        self.conn.execute(
            """INSERT INTO contracts (id, sprint_id, version, proposed_by, status,
               criteria, review_notes, created_at)
               VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?)""",
            (contract_id, sprint_id, version, proposed_by,
             json.dumps(criteria), review_notes, now),
        )
        self.conn.commit()
        return self.get_contract(contract_id)

    def get_contract(self, contract_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_latest_contract(self, sprint_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM contracts WHERE sprint_id = ? ORDER BY version DESC LIMIT 1",
            (sprint_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_contracts(self, sprint_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM contracts WHERE sprint_id = ? ORDER BY version",
            (sprint_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_contract(self, contract_id: str, **kwargs) -> dict:
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [contract_id]
        self.conn.execute(f"UPDATE contracts SET {set_clause} WHERE id = ?", values)
        self.conn.commit()
        return self.get_contract(contract_id)

    # ── Messages ─────────────────────────────────────────────────────

    def send_message(self, build_id: str, from_agent: str, msg_type: str, body: str,
                     to_agent: Optional[str] = None, sprint_id: Optional[str] = None,
                     parent_id: Optional[int] = None) -> dict:
        now = _now()
        cursor = self.conn.execute(
            """INSERT INTO messages (build_id, sprint_id, from_agent, to_agent,
               msg_type, body, parent_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (build_id, sprint_id, from_agent, to_agent, msg_type, body, parent_id, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)

    def list_messages(self, build_id: str, to_agent: Optional[str] = None,
                      sprint_id: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM messages WHERE build_id = ?"
        params: list = [build_id]
        if to_agent:
            # Include broadcasts (to_agent IS NULL) alongside direct messages
            sql += " AND (to_agent = ? OR to_agent IS NULL)"
            params.append(to_agent)
        if sprint_id:
            sql += " AND sprint_id = ?"
            params.append(sprint_id)
        sql += " ORDER BY created_at"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── Agent Logs ───────────────────────────────────────────────────

    def create_agent_log(self, build_id: str, agent: str,
                         sprint_id: Optional[str] = None,
                         session_id: Optional[str] = None,
                         log_path: Optional[str] = None,
                         summary: Optional[str] = None,
                         duration_seconds: Optional[int] = None,
                         exit_code: Optional[int] = None) -> dict:
        now = _now()
        cursor = self.conn.execute(
            """INSERT INTO agent_logs (build_id, sprint_id, agent, session_id,
               log_path, summary, duration_seconds, exit_code, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (build_id, sprint_id, agent, session_id, log_path, summary,
             duration_seconds, exit_code, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM agent_logs WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)

    def get_agent_logs(self, build_id: str,
                       sprint_id: Optional[str] = None) -> list[dict]:
        if sprint_id:
            rows = self.conn.execute(
                "SELECT * FROM agent_logs WHERE build_id = ? AND sprint_id = ? ORDER BY created_at",
                (build_id, sprint_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM agent_logs WHERE build_id = ? ORDER BY created_at",
                (build_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Retrospectives ───────────────────────────────────────────────

    def create_retrospective(self, build_id: str, findings: str,
                             changes_made: Optional[str] = None,
                             before_snapshot: Optional[str] = None) -> dict:
        now = _now()
        cursor = self.conn.execute(
            """INSERT INTO retrospectives (build_id, findings, changes_made,
               before_snapshot, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (build_id, findings, changes_made, before_snapshot, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM retrospectives WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row)
