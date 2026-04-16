"""State machine for build and sprint lifecycle transitions.

Validates transitions against the allowed graph in models.py,
sets terminal timestamps, and handles sprint advancement.
"""
from datetime import datetime, timezone

from .db import OrchestratorDB
from .models import (
    BuildStatus,
    SprintStatus,
    VALID_BUILD_TRANSITIONS,
    VALID_SPRINT_TRANSITIONS,
)


class BuildTransitionError(ValueError):
    """Raised on invalid build state transitions."""


class SprintTransitionError(ValueError):
    """Raised on invalid sprint state transitions."""


def transition_build(db: OrchestratorDB, build_id: str, new_status: str) -> dict:
    """Transition a build to a new status, validating against allowed transitions."""
    build = db.get_build(build_id)
    if not build:
        raise ValueError(f"Build {build_id} not found")

    current = build["status"]
    allowed = VALID_BUILD_TRANSITIONS.get(current, ())
    if new_status not in allowed:
        raise BuildTransitionError(
            f"Cannot transition build from '{current}' to '{new_status}'. "
            f"Allowed: {allowed}"
        )

    kwargs = {"status": new_status}
    if new_status in BuildStatus.TERMINAL:
        kwargs["completed_at"] = datetime.now(timezone.utc).isoformat()

    result = db.update_build(build_id, **kwargs)

    # Sync to kanban (no-op if kanban.db doesn't exist)
    try:
        from . import kanban_bridge  # noqa: PLC0415
        kanban_bridge.update_issue_status(build_id, new_status)
    except Exception:
        pass  # Kanban sync is best-effort

    return result


def transition_sprint(db: OrchestratorDB, sprint_id: str, new_status: str) -> dict:
    """Transition a sprint to a new status, validating against allowed transitions."""
    sprint = db.get_sprint(sprint_id)
    if not sprint:
        raise ValueError(f"Sprint {sprint_id} not found")

    current = sprint["status"]
    allowed = VALID_SPRINT_TRANSITIONS.get(current, ())
    if new_status not in allowed:
        raise SprintTransitionError(
            f"Cannot transition sprint from '{current}' to '{new_status}'. "
            f"Allowed: {allowed}"
        )

    kwargs = {"status": new_status}
    if new_status in SprintStatus.TERMINAL:
        kwargs["completed_at"] = datetime.now(timezone.utc).isoformat()
    if new_status == SprintStatus.CONTRACTED:
        kwargs["negotiation_phase"] = None

    return db.update_sprint(sprint_id, **kwargs)


def advance_to_next_sprint(
    db: OrchestratorDB, build_id: str
) -> dict | None:
    """Advance a build to its next sprint. Returns the next sprint or None if done."""
    build = db.get_build(build_id)
    if not build:
        raise ValueError(f"Build {build_id} not found")

    current = build["current_sprint"]
    total = build["total_sprints"]
    if current >= total:
        return None

    next_num = current + 1
    db.update_build(build_id, current_sprint=next_num)

    # Find the sprint with this number
    sprints = db.get_sprints(build_id)
    for s in sprints:
        if s["sprint_number"] == next_num:
            return s
    return None
