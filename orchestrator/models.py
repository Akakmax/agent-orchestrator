"""Orchestrator data models, constants, and configuration.

All status values, agent roles, message types, and config defaults live here.
Follow the same pattern as blocks/hands/kanban/models.py.
"""
from pathlib import Path


class BuildStatus:
    PLANNING = "planning"
    BUILDING = "building"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"
    ALL = (PLANNING, BUILDING, REVIEWING, DONE, FAILED)
    TERMINAL = (DONE, FAILED)


class SprintStatus:
    PENDING = "pending"
    CONTRACTED = "contracted"
    BUILDING = "building"
    MERGING = "merging"
    EVALUATING = "evaluating"
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"
    ALL = (PENDING, CONTRACTED, BUILDING, MERGING, EVALUATING, PASSED, FAILED, ESCALATED, BLOCKED)
    TERMINAL = (PASSED, FAILED, ESCALATED)


class MergeStatus:
    PENDING = "pending"
    MERGING = "merging"
    RESOLVED = "resolved"
    FAILED = "failed"
    ALL = (PENDING, MERGING, RESOLVED, FAILED)


class ContractStatus:
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    ALL = (PROPOSED, APPROVED, REJECTED)


class AgentRole:
    PLANNER = "planner"
    GENERATOR = "generator"
    EVALUATOR = "evaluator"
    RETROSPECTIVE = "retrospective"
    ALL = (PLANNER, GENERATOR, EVALUATOR, RETROSPECTIVE)


class MsgType:
    UPDATE = "update"
    PROPOSAL = "proposal"
    APPROVAL = "approval"
    REJECTION = "rejection"
    CRITIQUE = "critique"
    QUESTION = "question"
    STEERING = "steering"
    ALL = (UPDATE, PROPOSAL, APPROVAL, REJECTION, CRITIQUE, QUESTION, STEERING)


class NegotiationPhase:
    GENERATOR_PROPOSING = "generator_proposing"
    EVALUATOR_REVIEWING = "evaluator_reviewing"


class OrchestratorConfig:
    SPRINT_MAX_ATTEMPTS = 3
    CONTRACT_MAX_ROUNDS = 3
    CONTRACT_TIMEOUT_MINUTES = 30
    BUILD_TIMEOUT_HOURS = 6
    TICK_INTERVAL_MINUTES = 2
    SPAWN_BACKEND = "tmux"
    TMUX_REMAIN_ON_EXIT = True
    LOG_DIR = str(Path.home() / ".kanban" / "logs")
    RETRO_AUTO_APPROVE = "none"
    EVALUATOR_STRICTNESS = "strict"
    BUSINESS_HOURS_START = 8
    BUSINESS_HOURS_END = 22
    DB_PATH = str(Path.home() / ".kanban" / "orchestrator.db")
    BUILDS_DIR = str(Path.home() / ".kanban" / "builds")
    HEARTBEAT_INTERVAL_SECONDS = 300    # sidecar writes heartbeat every 5 min
    HEARTBEAT_STALE_MINUTES = 8         # heartbeat older than this = suspect
    LOG_STALE_MINUTES = 15              # log unchanged this long = possibly stuck
    CHECKPOINT_STALE_MINUTES = 45       # no checkpoint this long = stalled
    SPRINT_TIMEOUT_MINUTES = 60         # default per-sprint timeout
    MERGE_STRATEGY = "sequential"       # sequential merge, respawn on conflict


# ── State transitions ────────────────────────────────────────────────

VALID_BUILD_TRANSITIONS = {
    BuildStatus.PLANNING: (BuildStatus.BUILDING, BuildStatus.FAILED),
    BuildStatus.BUILDING: (BuildStatus.REVIEWING, BuildStatus.FAILED),
    BuildStatus.REVIEWING: (BuildStatus.DONE, BuildStatus.BUILDING, BuildStatus.FAILED),
    BuildStatus.DONE: (),
    BuildStatus.FAILED: (),
}

VALID_SPRINT_TRANSITIONS = {
    SprintStatus.PENDING: (SprintStatus.CONTRACTED, SprintStatus.FAILED, SprintStatus.BLOCKED),
    SprintStatus.CONTRACTED: (SprintStatus.BUILDING, SprintStatus.FAILED),
    SprintStatus.BUILDING: (SprintStatus.MERGING, SprintStatus.EVALUATING, SprintStatus.FAILED),
    SprintStatus.MERGING: (SprintStatus.EVALUATING, SprintStatus.BUILDING, SprintStatus.FAILED),
    SprintStatus.EVALUATING: (SprintStatus.PASSED, SprintStatus.BUILDING, SprintStatus.FAILED),
    SprintStatus.PASSED: (),
    SprintStatus.FAILED: (SprintStatus.ESCALATED,),
    SprintStatus.ESCALATED: (),
    SprintStatus.BLOCKED: (SprintStatus.CONTRACTED, SprintStatus.FAILED),
}
