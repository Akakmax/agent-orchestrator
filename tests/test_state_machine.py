"""Tests for build and sprint state machine transitions."""
import pytest

from orchestrator.models import BuildStatus, SprintStatus
from orchestrator.state_machine import (
    BuildTransitionError,
    SprintTransitionError,
    transition_build,
    transition_sprint,
)


class TestBuildTransitions:
    def test_planning_to_building(self, build):
        db, b = build
        result = transition_build(db, b["id"], BuildStatus.BUILDING)
        assert result["status"] == BuildStatus.BUILDING

    def test_planning_to_failed(self, build):
        db, b = build
        result = transition_build(db, b["id"], BuildStatus.FAILED)
        assert result["status"] == BuildStatus.FAILED
        assert result["completed_at"] is not None

    def test_building_to_reviewing(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.BUILDING)
        result = transition_build(db, b["id"], BuildStatus.REVIEWING)
        assert result["status"] == BuildStatus.REVIEWING

    def test_reviewing_to_done(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.BUILDING)
        transition_build(db, b["id"], BuildStatus.REVIEWING)
        result = transition_build(db, b["id"], BuildStatus.DONE)
        assert result["status"] == BuildStatus.DONE
        assert result["completed_at"] is not None

    def test_invalid_planning_to_done(self, build):
        db, b = build
        with pytest.raises(BuildTransitionError):
            transition_build(db, b["id"], BuildStatus.DONE)

    def test_invalid_planning_to_reviewing(self, build):
        db, b = build
        with pytest.raises(BuildTransitionError):
            transition_build(db, b["id"], BuildStatus.REVIEWING)

    def test_done_is_terminal(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.BUILDING)
        transition_build(db, b["id"], BuildStatus.REVIEWING)
        transition_build(db, b["id"], BuildStatus.DONE)
        with pytest.raises(BuildTransitionError):
            transition_build(db, b["id"], BuildStatus.BUILDING)

    def test_failed_is_terminal(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.FAILED)
        with pytest.raises(BuildTransitionError):
            transition_build(db, b["id"], BuildStatus.BUILDING)

    def test_reviewing_can_go_back_to_building(self, build):
        db, b = build
        transition_build(db, b["id"], BuildStatus.BUILDING)
        transition_build(db, b["id"], BuildStatus.REVIEWING)
        result = transition_build(db, b["id"], BuildStatus.BUILDING)
        assert result["status"] == BuildStatus.BUILDING

    def test_nonexistent_build(self, db):
        with pytest.raises(ValueError, match="not found"):
            transition_build(db, "nonexistent", BuildStatus.BUILDING)


class TestSprintTransitions:
    def test_pending_to_contracted(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        assert result["status"] == SprintStatus.CONTRACTED
        # negotiation_phase should be cleared
        assert result["negotiation_phase"] is None

    def test_contracted_to_building(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        assert result["status"] == SprintStatus.BUILDING

    def test_building_to_merging(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.MERGING)
        assert result["status"] == SprintStatus.MERGING

    def test_building_to_evaluating(self, build_with_sprints):
        """Direct building → evaluating (single-agent, no merge needed)."""
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.EVALUATING)
        assert result["status"] == SprintStatus.EVALUATING

    def test_merging_to_evaluating(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        transition_sprint(db, sprints[0]["id"], SprintStatus.MERGING)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.EVALUATING)
        assert result["status"] == SprintStatus.EVALUATING

    def test_evaluating_to_passed(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        transition_sprint(db, sprints[0]["id"], SprintStatus.EVALUATING)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.PASSED)
        assert result["status"] == SprintStatus.PASSED
        assert result["completed_at"] is not None

    def test_evaluating_retry(self, build_with_sprints):
        """Evaluating → building (evaluation failed, retry)."""
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        transition_sprint(db, sprints[0]["id"], SprintStatus.EVALUATING)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        assert result["status"] == SprintStatus.BUILDING

    def test_failed_to_escalated(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.FAILED)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.ESCALATED)
        assert result["status"] == SprintStatus.ESCALATED

    def test_invalid_pending_to_building(self, build_with_sprints):
        """Can't skip contracted."""
        db, b, sprints = build_with_sprints
        with pytest.raises(SprintTransitionError):
            transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)

    def test_invalid_pending_to_passed(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        with pytest.raises(SprintTransitionError):
            transition_sprint(db, sprints[0]["id"], SprintStatus.PASSED)

    def test_passed_is_terminal(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)
        transition_sprint(db, sprints[0]["id"], SprintStatus.EVALUATING)
        transition_sprint(db, sprints[0]["id"], SprintStatus.PASSED)
        with pytest.raises(SprintTransitionError):
            transition_sprint(db, sprints[0]["id"], SprintStatus.BUILDING)

    def test_blocked_to_contracted(self, build_with_sprints):
        db, b, sprints = build_with_sprints
        transition_sprint(db, sprints[0]["id"], SprintStatus.BLOCKED)
        result = transition_sprint(db, sprints[0]["id"], SprintStatus.CONTRACTED)
        assert result["status"] == SprintStatus.CONTRACTED
