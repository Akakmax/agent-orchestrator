"""Tick loop — orchestration driver.

Main entry point for the orchestrator's periodic health check.
Processes all active builds: checks timeouts, agent health,
sprint progression, and build completion.
"""
from datetime import datetime, timezone, timedelta

from .db import OrchestratorDB
from .models import BuildStatus, SprintStatus, OrchestratorConfig
from .state_machine import transition_build, transition_sprint
from .spawner import get_backend, is_session_locked


def run_tick(db: OrchestratorDB, dry_run: bool = False) -> list[dict]:
    """Main tick entry point. Returns list of action dicts describing what happened."""
    actions: list[dict] = []
    backend = get_backend()

    # Process BUILDING and REVIEWING builds
    active_builds = []
    for status in (BuildStatus.BUILDING, BuildStatus.REVIEWING):
        active_builds.extend(db.list_builds(status=status))

    for build in active_builds:
        # 1. Check build timeout
        actions.extend(_check_build_timeout(db, build, dry_run))

        # If build timed out, skip further checks for it
        if any(a["action"] == "build_timeout" and a["build_id"] == build["id"]
               for a in actions):
            continue

        sprints = db.get_sprints(build["id"])

        # 2. Check each sprint
        for sprint in sprints:
            actions.extend(_check_contract_timeout(db, sprint, dry_run))
            actions.extend(_check_agent_health(db, build, sprint, backend, dry_run))

        # 3. Check sprint progression
        actions.extend(_check_sprint_progression(db, build, sprints, dry_run))

        # 4. Check build completion
        actions.extend(_check_build_completion(db, build, dry_run))

    return actions


def _check_build_timeout(
    db: OrchestratorDB, build: dict, dry_run: bool
) -> list[dict]:
    """Check if a build has exceeded BUILD_TIMEOUT_HOURS."""
    created = datetime.fromisoformat(build["created_at"])
    elapsed = datetime.now(timezone.utc) - created
    elapsed_hours = elapsed.total_seconds() / 3600

    if elapsed_hours <= OrchestratorConfig.BUILD_TIMEOUT_HOURS:
        return []

    if not dry_run:
        transition_build(db, build["id"], BuildStatus.FAILED)

    return [{"action": "build_timeout", "build_id": build["id"],
             "elapsed_hours": round(elapsed_hours, 2)}]


def _check_contract_timeout(
    db: OrchestratorDB, sprint: dict, dry_run: bool
) -> list[dict]:
    """Check if a sprint's contract negotiation has timed out."""
    if not sprint.get("negotiation_phase"):
        return []

    updated = datetime.fromisoformat(sprint["updated_at"])
    elapsed = datetime.now(timezone.utc) - updated
    elapsed_minutes = elapsed.total_seconds() / 60

    if elapsed_minutes <= OrchestratorConfig.CONTRACT_TIMEOUT_MINUTES:
        return []

    if not dry_run:
        transition_sprint(db, sprint["id"], SprintStatus.FAILED)

    return [{"action": "contract_timeout", "sprint_id": sprint["id"],
             "elapsed_minutes": round(elapsed_minutes, 2)}]


def _check_agent_health(
    db: OrchestratorDB, build: dict, sprint: dict,
    backend, dry_run: bool
) -> list[dict]:
    """Check if the agent for an active sprint is still alive."""
    # Only check sprints that should have running agents
    if sprint["status"] not in (SprintStatus.BUILDING, SprintStatus.EVALUATING):
        return []

    # Find latest agent_log for this sprint — use session_id from agent_logs
    logs = db.get_agent_logs(build["id"], sprint_id=sprint["id"])
    if not logs:
        return []  # Agent not spawned yet

    latest_log = logs[-1]
    session_id = latest_log.get("session_id")
    log_path = latest_log.get("log_path")

    if not session_id:
        return []  # No session to check

    # Check if agent is alive
    if backend.is_alive(session_id, log_path):
        return []

    # Agent is dead — check attempts
    attempts = sprint["attempts"]
    max_attempts = sprint["max_attempts"]

    if attempts >= max_attempts:
        # Escalate — too many failures
        if not dry_run:
            transition_sprint(db, sprint["id"], SprintStatus.FAILED)
        return [{"action": "sprint_escalated", "sprint_id": sprint["id"],
                 "build_id": build["id"], "attempts": attempts}]
    else:
        # Retry — increment attempts
        if not dry_run:
            db.increment_sprint_attempts(sprint["id"])
        return [{"action": "agent_crashed", "sprint_id": sprint["id"],
                 "build_id": build["id"],
                 "attempt": attempts + 1}]


def _check_sprint_progression(
    db: OrchestratorDB, build: dict, sprints: list[dict], dry_run: bool
) -> list[dict]:
    """Advance to next sprint if current sprint has passed."""
    actions = []
    current = build["current_sprint"]
    total = build["total_sprints"]

    for sprint in sprints:
        if (sprint["status"] == SprintStatus.PASSED
                and sprint["sprint_number"] == current
                and current < total):
            next_num = current + 1
            if not dry_run:
                db.update_build(build["id"], current_sprint=next_num)
            actions.append({
                "action": "advance_sprint",
                "build_id": build["id"],
                "sprint_number": next_num,
            })
            # Update current so we don't double-advance in this tick
            current = next_num

    return actions


def _check_build_completion(
    db: OrchestratorDB, build: dict, dry_run: bool
) -> list[dict]:
    """Transition build to REVIEWING if all sprints passed."""
    if build["status"] != BuildStatus.BUILDING:
        return []

    sprints = db.get_sprints(build["id"])
    if not sprints:
        return []

    all_passed = all(s["status"] == SprintStatus.PASSED for s in sprints)
    # Re-read build in case current_sprint was updated by progression check
    current_build = db.get_build(build["id"])
    at_end = current_build["current_sprint"] >= current_build["total_sprints"]

    if all_passed and at_end:
        if not dry_run:
            transition_build(db, build["id"], BuildStatus.REVIEWING)
        return [{"action": "build_to_review", "build_id": build["id"]}]

    return []
