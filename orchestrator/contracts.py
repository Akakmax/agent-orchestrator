"""Contract negotiation logic for the orchestrator.

Handles proposal/review cycles between generator and evaluator agents.
Each sprint goes through at most CONTRACT_MAX_ROUNDS of negotiation
before raising NegotiationExhaustedError.
"""
import json

from .db import OrchestratorDB
from .models import (
    AgentRole,
    ContractStatus,
    NegotiationPhase,
    OrchestratorConfig,
    SprintStatus,
)
from .state_machine import transition_sprint


class NegotiationExhaustedError(Exception):
    """Raised when max negotiation rounds exceeded."""


def propose_contract(
    db: OrchestratorDB,
    sprint_id: str,
    criteria: dict,
    proposed_by: str = AgentRole.GENERATOR,
) -> dict:
    """Propose a new contract for a sprint.

    Creates the contract, sets sprint phase to evaluator_reviewing.
    Raises NegotiationExhaustedError if max rounds exceeded.
    """
    existing = db.get_contracts(sprint_id)
    if len(existing) >= OrchestratorConfig.CONTRACT_MAX_ROUNDS:
        raise NegotiationExhaustedError(
            f"Sprint {sprint_id} exceeded max negotiation rounds "
            f"({OrchestratorConfig.CONTRACT_MAX_ROUNDS})"
        )

    # Sync file boundaries and timeout to sprint row for tick-loop access
    update_kwargs = {}
    if isinstance(criteria, dict):
        if "allowed_paths" in criteria:
            update_kwargs["allowed_paths"] = json.dumps(criteria["allowed_paths"])
        if "allowed_new_paths" in criteria:
            update_kwargs["allowed_new_paths"] = json.dumps(criteria["allowed_new_paths"])
        if "timeout_minutes" in criteria:
            try:
                update_kwargs["timeout_minutes"] = int(criteria["timeout_minutes"])
            except (ValueError, TypeError):
                pass  # Skip invalid value — default will be used
        if "checkpoint_interval_minutes" in criteria:
            try:
                update_kwargs["checkpoint_interval_minutes"] = int(criteria["checkpoint_interval_minutes"])
            except (ValueError, TypeError):
                pass
    if update_kwargs:
        db.update_sprint(sprint_id, **update_kwargs)

    contract = db.create_contract(sprint_id, proposed_by, criteria)
    db.update_sprint(sprint_id, negotiation_phase=NegotiationPhase.EVALUATOR_REVIEWING)
    return contract


def review_contract(
    db: OrchestratorDB,
    sprint_id: str,
    contract_id: str,
    approve: bool,
    notes: str | None = None,
) -> dict:
    """Review a proposed contract — approve or reject.

    On approve: marks contract approved, links to sprint, clears negotiation phase,
    and transitions the sprint to CONTRACTED status.
    On reject: marks contract rejected with notes, sets phase to generator_proposing.
    """
    if approve:
        kwargs = {"status": ContractStatus.APPROVED}
        if notes:
            kwargs["review_notes"] = notes
        contract = db.update_contract(contract_id, **kwargs)

        # Link contract to sprint and clear negotiation phase
        db.update_sprint(sprint_id, contract_id=contract_id, negotiation_phase=None)

        # M1 critical fix: transition sprint to contracted so it doesn't stay pending
        transition_sprint(db, sprint_id, SprintStatus.CONTRACTED)

        return contract
    else:
        contract = db.update_contract(
            contract_id, status=ContractStatus.REJECTED, review_notes=notes
        )
        db.update_sprint(
            sprint_id, negotiation_phase=NegotiationPhase.GENERATOR_PROPOSING
        )
        return contract


def get_negotiation_status(db: OrchestratorDB, sprint_id: str) -> dict:
    """Return current negotiation status for a sprint.

    Returns dict with: round, max_rounds, latest_contract, negotiation_phase.
    """
    contracts = db.get_contracts(sprint_id)
    sprint = db.get_sprint(sprint_id)
    latest = db.get_latest_contract(sprint_id)
    return {
        "round": len(contracts),
        "max_rounds": OrchestratorConfig.CONTRACT_MAX_ROUNDS,
        "latest_contract": latest,
        "negotiation_phase": sprint["negotiation_phase"] if sprint else None,
    }
