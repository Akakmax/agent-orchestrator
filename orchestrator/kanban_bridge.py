"""Kanban bridge — optional integration between orchestrator and kanban.db.

Light integration: the orchestrator creates/updates kanban issues when builds
change state. If kanban.db doesn't exist, all operations are no-ops.

Link fields:
- Orchestrator builds table gets `kanban_issue_id` (optional)
- Kanban issues get `orchestrator_build_id` in notes_path or error_context

Events synced:
- Build created → kanban issue created (status: open)
- Build building → kanban issue claimed (claimed_by: orchestrator)
- Build reviewing → kanban issue status: reviewing
- Build done → kanban issue resolved
- Build failed → kanban issue escalated
- Sprint failed → attempt logged in kanban
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


KANBAN_DB_PATH = Path.home() / ".kanban" / "kanban.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _kanban_conn() -> Optional[sqlite3.Connection]:
    """Get a connection to kanban.db. Returns None if it doesn't exist."""
    if not KANBAN_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(KANBAN_DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except sqlite3.Error:
        return None


def is_available() -> bool:
    """Check if the kanban DB exists and is accessible."""
    return KANBAN_DB_PATH.exists()


def create_issue_for_build(
    build_id: str,
    title: str,
    total_sprints: int = 0,
) -> Optional[str]:
    """Create a kanban issue linked to an orchestrator build.

    Returns the kanban issue ID, or None if kanban is unavailable.
    """
    conn = _kanban_conn()
    if not conn:
        return None

    issue_id = f"orch-{build_id}"
    now = _now()
    try:
        conn.execute(
            """INSERT INTO issues (id, title, source, service, severity, status,
               attempts, max_attempts, timeout_minutes, error_context,
               recurrence_count, is_flapping, issue_type, created_at, updated_at)
               VALUES (?, ?, 'orchestrator', 'orchestrator', 'info', 'open',
               0, 1, 360, ?, 0, 0, 'build', ?, ?)""",
            (issue_id, f"[BUILD] {title}",
             f"build_id={build_id} sprints={total_sprints}",
             now, now),
        )
        conn.commit()
        return issue_id
    except sqlite3.IntegrityError:
        # Issue already exists
        return issue_id
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def update_issue_status(
    build_id: str,
    status: str,
    summary: Optional[str] = None,
) -> bool:
    """Update the kanban issue status for a build.

    Maps orchestrator build statuses to kanban statuses:
    - building → open (claimed_by=orchestrator)
    - reviewing → open (with review note)
    - done → resolved
    - failed → escalated
    """
    conn = _kanban_conn()
    if not conn:
        return False

    issue_id = f"orch-{build_id}"
    now = _now()

    kanban_status_map = {
        "planning": "open",
        "building": "open",
        "reviewing": "open",
        "done": "resolved",
        "failed": "escalated",
    }
    kanban_status = kanban_status_map.get(status, "open")

    try:
        updates = {"status": kanban_status, "updated_at": now}
        if status == "building":
            updates["claimed_by"] = "orchestrator"
            updates["claimed_at"] = now
        elif status == "done":
            updates["resolved_at"] = now
            if summary:
                updates["resolution_summary"] = summary
        elif status == "failed":
            if summary:
                updates["diagnosis"] = summary

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [issue_id]
        conn.execute(f"UPDATE issues SET {set_clause} WHERE id = ?", values)
        conn.commit()
        return conn.total_changes > 0
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def log_sprint_attempt(
    build_id: str,
    sprint_number: int,
    agent: str,
    result: str,
    summary: str,
    duration_seconds: Optional[int] = None,
) -> bool:
    """Log a sprint attempt in the kanban attempt_log.

    This connects orchestrator sprint attempts to the kanban audit trail.
    """
    conn = _kanban_conn()
    if not conn:
        return False

    issue_id = f"orch-{build_id}"
    now = _now()

    try:
        conn.execute(
            """INSERT INTO attempt_log (issue_id, agent, attempt_number,
               result, summary, duration_seconds, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (issue_id, agent, sprint_number, result, summary,
             duration_seconds, now),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def escalate_build(
    build_id: str,
    from_agent: str,
    reason: str,
    context: Optional[str] = None,
) -> bool:
    """Log an escalation in the kanban escalation_log."""
    conn = _kanban_conn()
    if not conn:
        return False

    issue_id = f"orch-{build_id}"
    now = _now()

    try:
        conn.execute(
            """INSERT INTO escalation_log (issue_id, from_agent, to_agent,
               reason, context_summary, created_at)
               VALUES (?, ?, 'developer', ?, ?, ?)""",
            (issue_id, from_agent, reason, context, now),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def get_linked_issue(build_id: str) -> Optional[dict]:
    """Get the kanban issue linked to a build, if any."""
    conn = _kanban_conn()
    if not conn:
        return None

    issue_id = f"orch-{build_id}"
    try:
        row = conn.execute(
            "SELECT * FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
