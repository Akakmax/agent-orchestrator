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

        # 3. Process merge queue (MERGING sprints)
        actions.extend(_process_merge_queue(db, build, sprints, dry_run))

        # 4. Check sprint progression (spawn agents for ready sprints)
        actions.extend(_check_sprint_progression(db, build, sprints, backend, dry_run))

        # 5. Check build completion
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
    try:
        timeout = int(sprint.get("timeout_minutes") or OrchestratorConfig.SPRINT_TIMEOUT_MINUTES)
    except (ValueError, TypeError):
        timeout = OrchestratorConfig.SPRINT_TIMEOUT_MINUTES
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


def _process_merge_queue(
    db: OrchestratorDB, build: dict, sprints: list[dict], dry_run: bool
) -> list[dict]:
    """Process sprints in MERGING state — run merge + targeted tests.

    For each MERGING sprint:
    1. Check if a merge_queue entry exists, create one if not
    2. Run merge_branch() to merge the sprint branch into target
    3. Run targeted tests on changed files
    4. On success → transition to EVALUATING
    5. On failure → transition back to BUILDING (retry) or FAILED
    """
    from .merger import merge_branch, run_post_merge_formatter  # noqa: PLC0415
    from .test_runner import run_targeted_tests, map_files_to_tests  # noqa: PLC0415

    actions = []
    build_id = build["id"]
    project_path = build.get("project_path")

    if not project_path:
        return actions  # No project path — can't merge

    for sprint in sprints:
        if sprint["status"] != SprintStatus.MERGING:
            continue

        sprint_id = sprint["id"]
        source_branch = sprint.get("git_branch") or f"sprint/{sprint_id}"
        target_branch = build.get("git_branch") or "main"

        if dry_run:
            actions.append({
                "action": "merge_pending",
                "build_id": build_id,
                "sprint_id": sprint_id,
                "source_branch": source_branch,
            })
            continue

        # Ensure merge_queue entry exists
        pending = db.get_pending_merges(build_id)
        entry = next((m for m in pending if m["sprint_id"] == sprint_id), None)
        if not entry:
            entry = db.create_merge_entry(
                build_id, sprint_id, source_branch, target_branch,
            )

        # Attempt the merge
        db.update_merge_entry(entry["id"], status="merging")

        try:
            result = merge_branch(project_path, source_branch, target_branch)

            if result.success:
                # Run post-merge formatter
                run_post_merge_formatter(project_path, result.conflict_files)

                # Run targeted tests on changed files
                changed = result.conflict_files  # At minimum, test conflict files
                test_files = map_files_to_tests(changed, project_path)
                test_result = run_targeted_tests(test_files, project_path)

                if test_result.passed:
                    db.update_merge_entry(
                        entry["id"],
                        status="resolved",
                        resolution_log=result.resolution_log,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    transition_sprint(db, sprint_id, SprintStatus.EVALUATING)
                    actions.append({
                        "action": "merge_resolved",
                        "build_id": build_id,
                        "sprint_id": sprint_id,
                        "conflicts": len(result.conflict_files),
                        "resolution_log": result.resolution_log,
                    })
                else:
                    # Tests failed after merge — revert and retry
                    db.update_merge_entry(
                        entry["id"],
                        status="failed",
                        resolution_log=f"{result.resolution_log}\nTests failed: {test_result.output[:500]}",
                    )
                    transition_sprint(db, sprint_id, SprintStatus.BUILDING)
                    actions.append({
                        "action": "merge_tests_failed",
                        "build_id": build_id,
                        "sprint_id": sprint_id,
                        "test_output": test_result.output[:200],
                    })
            else:
                # Merge/resolution failed
                db.update_merge_entry(
                    entry["id"],
                    status="failed",
                    conflict_files=json.dumps(result.conflict_files),
                    resolution_log=result.resolution_log,
                )
                transition_sprint(db, sprint_id, SprintStatus.BUILDING)
                actions.append({
                    "action": "merge_failed",
                    "build_id": build_id,
                    "sprint_id": sprint_id,
                    "conflict_files": result.conflict_files,
                    "resolution_log": result.resolution_log,
                })

        except Exception as e:
            db.update_merge_entry(
                entry["id"],
                status="failed",
                resolution_log=str(e),
            )
            actions.append({
                "action": "merge_error",
                "build_id": build_id,
                "sprint_id": sprint_id,
                "error": str(e),
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
        # Retry — respawn the dead agent, only increment attempts on success
        if not dry_run:
            from .adapter import needs_session_lock  # noqa: PLC0415

            role = latest_log.get("agent", "generator")
            if needs_session_lock(role) and is_session_locked():
                # Lock held — skip retry, don't burn an attempt
                return [{"action": "session_locked",
                         "sprint_id": sprint["id"],
                         "build_id": build["id"],
                         "role": role}]

            try:
                from .communication import get_comm_backend as _get_comm  # noqa: PLC0415
                retry_ctx = f"Retry attempt {attempts + 1}/{max_attempts}. Previous agent died."
                if role == "generator":
                    from .generator import get_generator_spawn_args  # noqa: PLC0415
                    spawn_args = get_generator_spawn_args(
                        db, build["id"], sprint["id"], retry_context=retry_ctx,
                    )
                else:
                    from .evaluator import get_evaluator_spawn_args  # noqa: PLC0415
                    spawn_args = get_evaluator_spawn_args(
                        db, build["id"], sprint["id"],
                    )
                # Inject comm backend callback for respawned agent
                comm_be = _get_comm(role)
                cb_cmd = comm_be.build_callback_command(
                    build["id"], sprint["id"], spawn_args["log_path"],
                )
                if cb_cmd:
                    spawn_args["post_exit_command"] = cb_cmd

                new_session_id = backend.spawn(**spawn_args)
                db.create_agent_log(
                    build_id=build["id"],
                    agent=role,
                    sprint_id=sprint["id"],
                    session_id=new_session_id,
                    log_path=spawn_args["log_path"],
                )
                # Only increment attempts after successful respawn
                db.increment_sprint_attempts(sprint["id"])
            except Exception:
                pass  # Spawn error — next tick will retry without burning attempt

        return [{"action": "agent_crashed", "sprint_id": sprint["id"],
                 "build_id": build["id"],
                 "attempt": attempts + 1}]


def _check_sprint_progression(
    db: OrchestratorDB, build: dict, sprints: list[dict],
    backend, dry_run: bool,
) -> list[dict]:
    """Advance ready sprints — DAG-aware parallel progression.

    Handles two cases:
    1. CONTRACTED sprints with deps met → spawn generator → BUILDING
    2. EVALUATING sprints without active evaluator → spawn evaluator
    """
    from .generator import get_generator_spawn_args  # noqa: PLC0415
    from .evaluator import get_evaluator_spawn_args  # noqa: PLC0415
    from .adapter import needs_session_lock  # noqa: PLC0415
    from .communication import get_comm_backend  # noqa: PLC0415

    actions = []
    build_id = build["id"]

    # Case 1: Spawn generators for ready sprints
    ready = _get_ready_sprints(sprints)
    for sprint in ready:
        if dry_run:
            actions.append({
                "action": "sprint_ready",
                "build_id": build_id,
                "sprint_id": sprint["id"],
                "sprint_number": sprint["sprint_number"],
            })
            continue

        # Check session lock for CC agents
        if needs_session_lock("generator") and is_session_locked():
            actions.append({
                "action": "session_locked",
                "build_id": build_id,
                "sprint_id": sprint["id"],
                "role": "generator",
            })
            continue

        try:
            spawn_args = get_generator_spawn_args(db, build_id, sprint["id"])

            # Inject comm backend callback (sendmessage, http, unix_socket)
            comm = get_comm_backend("generator")
            cb_cmd = comm.build_callback_command(build_id, sprint["id"], spawn_args["log_path"])
            if cb_cmd:
                spawn_args["post_exit_command"] = cb_cmd

            session_id = backend.spawn(**spawn_args)

            # Record agent log so health checks can find it
            db.create_agent_log(
                build_id=build_id,
                agent="generator",
                sprint_id=sprint["id"],
                session_id=session_id,
                log_path=spawn_args["log_path"],
            )

            transition_sprint(db, sprint["id"], SprintStatus.BUILDING)

            actions.append({
                "action": "generator_spawned",
                "build_id": build_id,
                "sprint_id": sprint["id"],
                "sprint_number": sprint["sprint_number"],
                "session_id": session_id,
            })
        except Exception as e:
            actions.append({
                "action": "spawn_error",
                "build_id": build_id,
                "sprint_id": sprint["id"],
                "role": "generator",
                "error": str(e),
            })

    # Case 2: Spawn evaluators for sprints awaiting evaluation
    for sprint in sprints:
        if sprint["status"] != SprintStatus.EVALUATING:
            continue

        # Check if evaluator already spawned for this sprint
        logs = db.get_agent_logs(build_id, sprint_id=sprint["id"])
        has_evaluator = any(lg["agent"] == "evaluator" for lg in logs)
        if has_evaluator:
            continue

        if dry_run:
            actions.append({
                "action": "evaluator_ready",
                "build_id": build_id,
                "sprint_id": sprint["id"],
            })
            continue

        if needs_session_lock("evaluator") and is_session_locked():
            actions.append({
                "action": "session_locked",
                "build_id": build_id,
                "sprint_id": sprint["id"],
                "role": "evaluator",
            })
            continue

        try:
            spawn_args = get_evaluator_spawn_args(db, build_id, sprint["id"])

            # Inject comm backend callback
            comm = get_comm_backend("evaluator")
            cb_cmd = comm.build_callback_command(build_id, sprint["id"], spawn_args["log_path"])
            if cb_cmd:
                spawn_args["post_exit_command"] = cb_cmd

            session_id = backend.spawn(**spawn_args)

            db.create_agent_log(
                build_id=build_id,
                agent="evaluator",
                sprint_id=sprint["id"],
                session_id=session_id,
                log_path=spawn_args["log_path"],
            )

            actions.append({
                "action": "evaluator_spawned",
                "build_id": build_id,
                "sprint_id": sprint["id"],
                "session_id": session_id,
            })
        except Exception as e:
            actions.append({
                "action": "spawn_error",
                "build_id": build_id,
                "sprint_id": sprint["id"],
                "role": "evaluator",
                "error": str(e),
            })

    return actions


def _check_build_completion(
    db: OrchestratorDB, build: dict, dry_run: bool
) -> list[dict]:
    """Transition build to REVIEWING if all sprints passed, or FAILED if stuck."""
    if build["status"] != BuildStatus.BUILDING:
        return []

    sprints = db.get_sprints(build["id"])
    if not sprints:
        return []

    all_passed = all(s["status"] == SprintStatus.PASSED for s in sprints)
    any_terminal_fail = any(
        s["status"] in (SprintStatus.FAILED, SprintStatus.ESCALATED)
        for s in sprints
    )

    if all_passed:
        if not dry_run:
            transition_build(db, build["id"], BuildStatus.REVIEWING)
        return [{"action": "build_to_review", "build_id": build["id"]}]

    if any_terminal_fail:
        # Only fail the build if no sprints are still in-progress
        in_progress = any(
            s["status"] in (SprintStatus.BUILDING, SprintStatus.EVALUATING,
                            SprintStatus.CONTRACTED)
            for s in sprints
        )
        if not in_progress:
            failed_ids = [
                s["id"] for s in sprints
                if s["status"] in (SprintStatus.FAILED, SprintStatus.ESCALATED)
            ]
            if not dry_run:
                transition_build(db, build["id"], BuildStatus.FAILED)
            return [{"action": "build_failed", "build_id": build["id"],
                     "failed_sprints": failed_ids}]

    return []
