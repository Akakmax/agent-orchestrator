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
    EVALUATING = "evaluating"
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"
    ALL = (PENDING, CONTRACTED, BUILDING, EVALUATING, PASSED, FAILED, ESCALATED)
    TERMINAL = (PASSED, FAILED, ESCALATED)


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
    ALL = (UPDATE, PROPOSAL, APPROVAL, REJECTION, CRITIQUE, QUESTION)


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


# ── State transitions ────────────────────────────────────────────────

VALID_BUILD_TRANSITIONS = {
    BuildStatus.PLANNING: (BuildStatus.BUILDING, BuildStatus.FAILED),
    BuildStatus.BUILDING: (BuildStatus.REVIEWING, BuildStatus.FAILED),
    BuildStatus.REVIEWING: (BuildStatus.DONE, BuildStatus.BUILDING, BuildStatus.FAILED),
    BuildStatus.DONE: (),
    BuildStatus.FAILED: (),
}

VALID_SPRINT_TRANSITIONS = {
    SprintStatus.PENDING: (SprintStatus.CONTRACTED, SprintStatus.FAILED),
    SprintStatus.CONTRACTED: (SprintStatus.BUILDING, SprintStatus.FAILED),
    SprintStatus.BUILDING: (SprintStatus.EVALUATING, SprintStatus.FAILED),
    SprintStatus.EVALUATING: (SprintStatus.PASSED, SprintStatus.BUILDING, SprintStatus.FAILED),
    SprintStatus.PASSED: (),
    SprintStatus.FAILED: (SprintStatus.ESCALATED,),
    SprintStatus.ESCALATED: (),
}
