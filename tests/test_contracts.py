"""Tests for contract negotiation logic."""
import json

import pytest

from orchestrator.contracts import (
    NegotiationExhaustedError,
    get_negotiation_status,
    propose_contract,
    review_contract,
)
from orchestrator.models import ContractStatus, NegotiationPhase, SprintStatus


class TestProposeContract:
    def test_propose_sets_evaluator_reviewing(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        contract = propose_contract(db, s["id"], {"tests": ["pass"]})
        assert contract["status"] == "proposed"
        sprint = db.get_sprint(s["id"])
        assert sprint["negotiation_phase"] == NegotiationPhase.EVALUATOR_REVIEWING

    def test_propose_syncs_allowed_paths(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        criteria = {
            "tests": ["pass"],
            "allowed_paths": ["src/", "lib/"],
            "timeout_minutes": 45,
        }
        propose_contract(db, s["id"], criteria)
        sprint = db.get_sprint(s["id"])
        assert json.loads(sprint["allowed_paths"]) == ["src/", "lib/"]
        assert sprint["timeout_minutes"] == 45

    def test_propose_handles_string_timeout(self, build):
        """String timeout_minutes should be coerced to int."""
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        propose_contract(db, s["id"], {"timeout_minutes": "30"})
        sprint = db.get_sprint(s["id"])
        assert sprint["timeout_minutes"] == 30

    def test_propose_handles_invalid_timeout(self, build):
        """Invalid timeout_minutes should be silently ignored."""
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        propose_contract(db, s["id"], {"timeout_minutes": "not_a_number"})
        sprint = db.get_sprint(s["id"])
        # Should keep default
        assert sprint["timeout_minutes"] == 60

    def test_max_rounds_exceeded(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        # Propose up to max rounds
        for i in range(3):
            propose_contract(db, s["id"], {"v": i + 1})
        # Next proposal should fail
        with pytest.raises(NegotiationExhaustedError):
            propose_contract(db, s["id"], {"v": 4})


class TestReviewContract:
    def test_approve_transitions_to_contracted(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        contract = propose_contract(db, s["id"], {"tests": ["pass"]})
        result = review_contract(db, s["id"], contract["id"], approve=True)
        assert result["status"] == ContractStatus.APPROVED
        sprint = db.get_sprint(s["id"])
        assert sprint["status"] == SprintStatus.CONTRACTED
        assert sprint["negotiation_phase"] is None
        assert sprint["contract_id"] == contract["id"]

    def test_reject_sets_generator_proposing(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        contract = propose_contract(db, s["id"], {"tests": ["pass"]})
        result = review_contract(db, s["id"], contract["id"],
                                 approve=False, notes="Need more tests")
        assert result["status"] == ContractStatus.REJECTED
        assert result["review_notes"] == "Need more tests"
        sprint = db.get_sprint(s["id"])
        assert sprint["negotiation_phase"] == NegotiationPhase.GENERATOR_PROPOSING

    def test_approve_with_notes(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        contract = propose_contract(db, s["id"], {"tests": ["pass"]})
        result = review_contract(db, s["id"], contract["id"],
                                 approve=True, notes="LGTM")
        assert result["review_notes"] == "LGTM"


class TestNegotiationStatus:
    def test_status_after_propose(self, build):
        db, b = build
        s = db.create_sprint(b["id"], 1, "Sprint 1")
        propose_contract(db, s["id"], {"tests": ["pass"]})
        status = get_negotiation_status(db, s["id"])
        assert status["round"] == 1
        assert status["max_rounds"] == 3
        assert status["negotiation_phase"] == NegotiationPhase.EVALUATOR_REVIEWING
