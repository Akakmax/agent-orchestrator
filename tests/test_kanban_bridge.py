"""Tests for the kanban bridge module."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator import kanban_bridge


@pytest.fixture()
def kanban_db(tmp_path):
    """Create a temporary kanban DB with the expected schema."""
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE issues (
        id TEXT PRIMARY KEY,
        title TEXT,
        source TEXT,
        service TEXT,
        severity TEXT,
        status TEXT,
        claimed_by TEXT,
        attempts INTEGER,
        max_attempts INTEGER,
        timeout_minutes INTEGER,
        notes_path TEXT,
        error_context TEXT,
        recurrence_count INTEGER,
        is_flapping INTEGER,
        parked_reason TEXT,
        resolution_summary TEXT,
        created_at TEXT,
        claimed_at TEXT,
        resolved_at TEXT,
        cooloff_until TEXT,
        updated_at TEXT,
        issue_type TEXT,
        diagnosis TEXT,
        proposed_fix TEXT,
        last_triggered_at TEXT
    )""")
    conn.execute("""CREATE TABLE escalation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id TEXT,
        from_agent TEXT,
        to_agent TEXT,
        reason TEXT,
        context_summary TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE attempt_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id TEXT,
        agent TEXT,
        attempt_number INTEGER,
        result TEXT,
        summary TEXT,
        duration_seconds INTEGER,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

    with patch.object(kanban_bridge, "KANBAN_DB_PATH", db_path):
        yield db_path


class TestAvailability:
    def test_available_when_db_exists(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            assert kanban_bridge.is_available() is True

    def test_unavailable_when_no_db(self, tmp_path):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH",
                          tmp_path / "nonexistent.db"):
            assert kanban_bridge.is_available() is False


class TestCreateIssue:
    def test_creates_issue(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            issue_id = kanban_bridge.create_issue_for_build(
                "abc123", "Test build", total_sprints=3)
            assert issue_id == "orch-abc123"

            # Verify in DB
            conn = sqlite3.connect(str(kanban_db))
            row = conn.execute(
                "SELECT * FROM issues WHERE id = ?", (issue_id,)
            ).fetchone()
            conn.close()
            assert row is not None

    def test_idempotent(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            id1 = kanban_bridge.create_issue_for_build("abc", "Test")
            id2 = kanban_bridge.create_issue_for_build("abc", "Test")
            assert id1 == id2

    def test_returns_none_when_unavailable(self, tmp_path):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH",
                          tmp_path / "nonexistent.db"):
            assert kanban_bridge.create_issue_for_build("abc", "Test") is None


class TestUpdateStatus:
    def test_building_sets_claimed(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            kanban_bridge.create_issue_for_build("abc", "Test")
            result = kanban_bridge.update_issue_status("abc", "building")
            assert result is True

            conn = sqlite3.connect(str(kanban_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM issues WHERE id = 'orch-abc'"
            ).fetchone()
            conn.close()
            assert row["claimed_by"] == "orchestrator"

    def test_done_sets_resolved(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            kanban_bridge.create_issue_for_build("abc", "Test")
            kanban_bridge.update_issue_status(
                "abc", "done", summary="All sprints passed")

            conn = sqlite3.connect(str(kanban_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM issues WHERE id = 'orch-abc'"
            ).fetchone()
            conn.close()
            assert row["status"] == "resolved"
            assert row["resolved_at"] is not None
            assert row["resolution_summary"] == "All sprints passed"

    def test_failed_sets_escalated(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            kanban_bridge.create_issue_for_build("abc", "Test")
            kanban_bridge.update_issue_status("abc", "failed",
                                             summary="Sprint 2 failed")

            conn = sqlite3.connect(str(kanban_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM issues WHERE id = 'orch-abc'"
            ).fetchone()
            conn.close()
            assert row["status"] == "escalated"


class TestAttemptLog:
    def test_logs_sprint_attempt(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            kanban_bridge.create_issue_for_build("abc", "Test")
            result = kanban_bridge.log_sprint_attempt(
                "abc", 1, "generator", "passed", "Sprint 1 done", 120)
            assert result is True

            conn = sqlite3.connect(str(kanban_db))
            rows = conn.execute(
                "SELECT * FROM attempt_log WHERE issue_id = 'orch-abc'"
            ).fetchall()
            conn.close()
            assert len(rows) == 1


class TestEscalation:
    def test_logs_escalation(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            kanban_bridge.create_issue_for_build("abc", "Test")
            result = kanban_bridge.escalate_build(
                "abc", "evaluator", "Sprint failed 3 times",
                "evaluator found test failures")
            assert result is True

            conn = sqlite3.connect(str(kanban_db))
            rows = conn.execute(
                "SELECT * FROM escalation_log WHERE issue_id = 'orch-abc'"
            ).fetchall()
            conn.close()
            assert len(rows) == 1


class TestGetLinkedIssue:
    def test_returns_issue(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            kanban_bridge.create_issue_for_build("abc", "Test")
            issue = kanban_bridge.get_linked_issue("abc")
            assert issue is not None
            assert issue["id"] == "orch-abc"

    def test_returns_none_for_unlinked(self, kanban_db):
        with patch.object(kanban_bridge, "KANBAN_DB_PATH", kanban_db):
            assert kanban_bridge.get_linked_issue("nonexistent") is None
