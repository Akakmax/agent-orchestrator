"""Tick loop — orchestration driver.

Main entry point for the orchestrator's periodic health check.
Processes all active builds: checks timeouts, agent health,
sprint progression, and build completion.
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
            actions.extend(_check_heartbeat(db, build, sprint, dry_run))

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


def _check_heartbeat(
    db: OrchestratorDB, build: dict, sprint: dict, dry_run: bool
) -> list[dict]:
    """Check heartbeat freshness and log growth for active sprints."""
    if sprint["status"] not in (SprintStatus.BUILDING, SprintStatus.EVALUATING):
        return []

    logs = db.get_agent_logs(build["id"], sprint_id=sprint["id"])
    if not logs:
        return []

    latest_log = logs[-1]
    log_path = latest_log.get("log_path")
    if not log_path:
        return []

    actions = []
    now = datetime.now(timezone.utc)

    # 1. Heartbeat file freshness (written by shell sidecar)
    hb_path = Path(f"{log_path}.heartbeat")
    if hb_path.exists():
        try:
            hb_time = datetime.fromisoformat(hb_path.read_text().strip())
            age_min = (now - hb_time).total_seconds() / 60
            if age_min > OrchestratorConfig.HEARTBEAT_STALE_MINUTES:
                actions.append({
                    "action": "heartbeat_stale",
                    "sprint_id": sprint["id"],
                    "build_id": build["id"],
                    "age_minutes": round(age_min, 1),
                })
        except (ValueError, OSError):
            pass

    # 2. Log file growth — if log hasn't grown, agent may be stuck
    lp = Path(log_path)
    if lp.exists():
        mtime = datetime.fromtimestamp(lp.stat().st_mtime, tz=timezone.utc)
        age_min = (now - mtime).total_seconds() / 60
        if age_min > OrchestratorConfig.LOG_STALE_MINUTES:
            actions.append({
                "action": "log_stale",
                "sprint_id": sprint["id"],
                "build_id": build["id"],
                "age_minutes": round(age_min, 1),
            })

    # 3. Sprint-level timeout
    started = datetime.fromisoformat(sprint["updated_at"])
    timeout = sprint.get("timeout_minutes") or OrchestratorConfig.SPRINT_TIMEOUT_MINUTES
    elapsed = (now - started).total_seconds() / 60
    if elapsed > timeout:
        if not dry_run:
            transition_sprint(db, sprint["id"], SprintStatus.FAILED)
        actions.append({
            "action": "sprint_timeout",
            "sprint_id": sprint["id"],
            "build_id": build["id"],
            "elapsed_minutes": round(elapsed, 1),
        })

    return actions


def _get_ready_sprints(sprints: list[dict]) -> list[dict]:
    """Return sprints whose dependencies are all PASSED and status is CONTRACTED.

    This enables parallel execution — all independent sprints spawn at once.
    """
    passed_ids = {s["id"] for s in sprints if s["status"] == SprintStatus.PASSED}
    ready = []
    for s in sprints:
        if s["status"] != SprintStatus.CONTRACTED:
            continue
        deps = json.loads(s.get("depends_on") or "[]")
        if all(dep_id in passed_ids for dep_id in deps):
            ready.append(s)
    return ready


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
    """Advance ready sprints — DAG-aware parallel progression."""
    actions = []
    ready = _get_ready_sprints(sprints)

    for sprint in ready:
        if not dry_run:
            # Mark as ready for spawning (status stays CONTRACTED,
            # the spawner picks it up on the next tick)
            pass  # Sprint is already CONTRACTED — spawner will handle it
        actions.append({
            "action": "sprint_ready",
            "build_id": build["id"],
            "sprint_id": sprint["id"],
            "sprint_number": sprint["sprint_number"],
        })

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

    if all_passed:
        if not dry_run:
            transition_build(db, build["id"], BuildStatus.REVIEWING)
        return [{"action": "build_to_review", "build_id": build["id"]}]

    return []
