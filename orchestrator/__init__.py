"""Universal Agent Orchestrator — config-driven multi-agent build system.
Public API — import from here, not submodules."""

from .db import OrchestratorDB
from .models import (
    BuildStatus, SprintStatus, ContractStatus, AgentRole, MsgType,
    NegotiationPhase, OrchestratorConfig,
)
from .state_machine import (
    transition_build, transition_sprint, advance_to_next_sprint,
    BuildTransitionError, SprintTransitionError,
)
from .contracts import (
    propose_contract, review_contract, get_negotiation_status,
    NegotiationExhaustedError,
)


def init_db(db_path=None):
    """Initialize and return the orchestrator database."""
    return OrchestratorDB(db_path)

def create_build(db, prompt):
    """Create a new build from a prompt."""
    return db.create_build(prompt=prompt)

def get_build(db, build_id):
    """Get a build by ID."""
    return db.get_build(build_id)

def get_sprints(db, build_id):
    """Get all sprints for a build."""
    return db.get_sprints(build_id)
