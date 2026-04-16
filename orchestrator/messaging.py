"""Inter-agent communication helpers for the orchestrator.

Convenience wrappers around OrchestratorDB.send_message / list_messages.
Each function sets the correct MsgType so callers don't have to remember.
"""
from typing import Optional

from .db import OrchestratorDB
from .models import MsgType


def send_update(db: OrchestratorDB, build_id: str, from_agent: str,
                body: str, sprint_id: Optional[str] = None) -> dict:
    """Broadcast an update (to_agent=None)."""
    return db.send_message(
        build_id, from_agent, MsgType.UPDATE, body,
        sprint_id=sprint_id,
    )


def send_proposal(db: OrchestratorDB, build_id: str, sprint_id: str,
                  from_agent: str, to_agent: str, body: str) -> dict:
    """Send a proposal from one agent to another."""
    return db.send_message(
        build_id, from_agent, MsgType.PROPOSAL, body,
        to_agent=to_agent, sprint_id=sprint_id,
    )


def send_critique(db: OrchestratorDB, build_id: str, sprint_id: str,
                  from_agent: str, to_agent: str, body: str) -> dict:
    """Send a critique from one agent to another."""
    return db.send_message(
        build_id, from_agent, MsgType.CRITIQUE, body,
        to_agent=to_agent, sprint_id=sprint_id,
    )


def send_rejection(db: OrchestratorDB, build_id: str, sprint_id: str,
                   from_agent: str, to_agent: str, body: str,
                   parent_id: Optional[int] = None) -> dict:
    """Send a rejection, optionally threaded to a parent message."""
    return db.send_message(
        build_id, from_agent, MsgType.REJECTION, body,
        to_agent=to_agent, sprint_id=sprint_id, parent_id=parent_id,
    )


def get_conversation(db: OrchestratorDB, build_id: str,
                     sprint_id: Optional[str] = None) -> list[dict]:
    """Return all messages for a build/sprint in chronological order."""
    return db.list_messages(build_id=build_id, sprint_id=sprint_id)
