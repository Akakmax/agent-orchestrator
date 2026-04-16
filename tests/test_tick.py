"""Tests for the tick loop logic.

These test the tick functions with a real DB but mock the spawn backend
so no actual tmux/processes are created.
"""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.db import OrchestratorDB
from orchestrator.models import BuildStatus, SprintStatus, OrchestratorConfig
from orchestrator.state_machine import transition_build, transition_sprint
from orchestrator.contracts import propose_contract, review_contract
from orchestrator.tick import (
    run_tick,
    _check_build_timeout,
    _check_build_completion,
    _get_ready_sprints,
)


def _setup_contracted_sprint(db, build_id, sprint_num, title, depends_on="[]"):
    """Helper: create a sprint, propose + approve contract, return sprint."""
    s = db.create_sprint(build_id, sprint_num, title, depends_on=depends_on)
    c = propose_contract(db, s["id"], {"tests": ["pass"]})
    review_contract(db, s["id"], c["id"], approve=True)
    return db.get_sprint(s["id"])


class TestBuildTimeout:
    def test_fresh_build_not_timed_out(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.BUILDING)
        b = db.get_build(b["id"])
        actions = _check_build_timeout(db, b, dry_run=False)
        assert len(actions) == 0

    def test_old_build_times_out(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.BUILDING)
        # Backdate created_at
        old_time = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        db.execute("UPDATE builds SET created_at = ? WHERE id = ?",
                   (old_time, b["id"]))
        db.commit()
        b = db.get_build(b["id"])
        actions = _check_build_timeout(db, b, dry_run=False)
        assert len(actions) == 1
        assert actions[0]["action"] == "build_timeout"
        assert db.get_build(b["id"])["status"] == BuildStatus.FAILED

    def test_dry_run_doesnt_change_state(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.BUILDING)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        db.execute("UPDATE builds SET created_at = ? WHERE id = ?",
                   (old_time, b["id"]))
        db.commit()
        b = db.get_build(b["id"])
        actions = _check_build_timeout(db, b, dry_run=True)
        assert len(actions) == 1
        assert db.get_build(b["id"])["status"] == BuildStatus.BUILDING


class TestBuildCompletion:
    def test_all_passed_transitions_to_reviewing(self, db):
        b = db.create_build("Test")
        transition_build(db, b["id"], BuildStatus.BUILDING)
        s = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        transition_sprint(db, s["id"], SprintStatus.BUILDING)
        transition_sprint(db, s["id"], SprintStatus.EVALUATING)
        transition_sprint(db, s["id"], SprintStatus.PASSED)
        db.update_build(b["id"], total_sprints=1)
        b = db.get_build(b["id"])
        actions = _check_build_completion(db, b, dry_run=False)
        assert len(actions) == 1
        assert actions[0]["action"] == "build_to_review"
        assert db.get_build(b["id"])["status"] == BuildStatus.REVIEWING

    def test_mixed_statuses_no_transition(self, db):
        b = db.create_build("Test")
        transition_build(db, b["id"], BuildStatus.BUILDING)
        s1 = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        s2 = _setup_contracted_sprint(db, b["id"], 2, "Sprint 2")
        transition_sprint(db, s1["id"], SprintStatus.BUILDING)
        transition_sprint(db, s1["id"], SprintStatus.EVALUATING)
        transition_sprint(db, s1["id"], SprintStatus.PASSED)
        # s2 still contracted
        db.update_build(b["id"], total_sprints=2)
        b = db.get_build(b["id"])
        actions = _check_build_completion(db, b, dry_run=False)
        assert len(actions) == 0

    def test_failed_sprint_fails_build_when_no_in_progress(self, db):
        b = db.create_build("Test")
        transition_build(db, b["id"], BuildStatus.BUILDING)
        s = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        transition_sprint(db, s["id"], SprintStatus.FAILED)
        db.update_build(b["id"], total_sprints=1)
        b = db.get_build(b["id"])
        actions = _check_build_completion(db, b, dry_run=False)
        assert len(actions) == 1
        assert actions[0]["action"] == "build_failed"

    def test_failed_sprint_waits_if_others_in_progress(self, db):
        b = db.create_build("Test")
        transition_build(db, b["id"], BuildStatus.BUILDING)
        s1 = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        s2 = _setup_contracted_sprint(db, b["id"], 2, "Sprint 2")
        transition_sprint(db, s1["id"], SprintStatus.FAILED)
        transition_sprint(db, s2["id"], SprintStatus.BUILDING)
        db.update_build(b["id"], total_sprints=2)
        b = db.get_build(b["id"])
        actions = _check_build_completion(db, b, dry_run=False)
        assert len(actions) == 0  # Waits for s2


class TestReadySprints:
    def test_no_deps_ready_immediately(self, db):
        b = db.create_build("Test")
        s = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        sprints = db.get_sprints(b["id"])
        ready = _get_ready_sprints(sprints)
        assert len(ready) == 1
        assert ready[0]["id"] == s["id"]

    def test_unmet_deps_not_ready(self, db):
        b = db.create_build("Test")
        s1 = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        s2 = _setup_contracted_sprint(db, b["id"], 2, "Sprint 2",
                                      depends_on=json.dumps([s1["id"]]))
        sprints = db.get_sprints(b["id"])
        ready = _get_ready_sprints(sprints)
        # Only s1 ready, s2 blocked by s1
        assert len(ready) == 1
        assert ready[0]["id"] == s1["id"]

    def test_met_deps_becomes_ready(self, db):
        b = db.create_build("Test")
        s1 = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        s2 = _setup_contracted_sprint(db, b["id"], 2, "Sprint 2",
                                      depends_on=json.dumps([s1["id"]]))
        # Pass s1
        transition_sprint(db, s1["id"], SprintStatus.BUILDING)
        transition_sprint(db, s1["id"], SprintStatus.EVALUATING)
        transition_sprint(db, s1["id"], SprintStatus.PASSED)
        sprints = db.get_sprints(b["id"])
        ready = _get_ready_sprints(sprints)
        assert len(ready) == 1
        assert ready[0]["id"] == s2["id"]

    def test_parallel_independent_sprints(self, db):
        """Two sprints with no deps should both be ready."""
        b = db.create_build("Test")
        s1 = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        s2 = _setup_contracted_sprint(db, b["id"], 2, "Sprint 2")
        sprints = db.get_sprints(b["id"])
        ready = _get_ready_sprints(sprints)
        assert len(ready) == 2


class TestTickIntegration:
    """Integration tests for run_tick with mocked backend."""

    @patch("orchestrator.tick.get_backend")
    def test_tick_dry_run_no_side_effects(self, mock_get_backend, db):
        b = db.create_build("Test")
        transition_build(db, b["id"], BuildStatus.BUILDING)
        s = _setup_contracted_sprint(db, b["id"], 1, "Sprint 1")
        db.update_build(b["id"], total_sprints=1)

        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend

        actions = run_tick(db, dry_run=True)

        # Should see sprint_ready but no actual spawning
        ready_actions = [a for a in actions if a["action"] == "sprint_ready"]
        assert len(ready_actions) == 1
        mock_backend.spawn.assert_not_called()
        # Sprint should still be contracted
        assert db.get_sprint(s["id"])["status"] == SprintStatus.CONTRACTED

    @patch("orchestrator.tick.get_backend")
    def test_tick_empty_build(self, mock_get_backend, db):
        """Build with no sprints shouldn't crash."""
        b = db.create_build("Test")
        transition_build(db, b["id"], BuildStatus.BUILDING)

        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend

        actions = run_tick(db, dry_run=False)
        # No sprints = no actions (except possibly build completion check)
        assert all(a["action"] != "sprint_ready" for a in actions)
