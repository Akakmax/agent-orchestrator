"""Notifications for orchestrator events.

Uses a pluggable notification backend. Set a custom notifier via
set_notifier(fn) where fn accepts a single string message.
Default: print to stderr.
"""
import sys
import subprocess
from datetime import datetime, timezone, timedelta
from .models import OrchestratorConfig

_AEST_OFFSET = timedelta(hours=10)
_notifications_enabled = True
_notifier = None  # Custom notifier function, set via set_notifier()


def set_notifications(enabled):
    """Toggle notification delivery on/off (useful for tests)."""
    global _notifications_enabled
    _notifications_enabled = enabled


def set_notifier(fn):
    """Set a custom notification function. fn(message: str) -> None.

    Examples:
        set_notifier(lambda msg: subprocess.run(["lark-send.sh", msg]))
        set_notifier(lambda msg: requests.post(webhook_url, json={"text": msg}))
    """
    global _notifier
    _notifier = fn


def _should_notify_now():
    """Only notify during business hours (AEST)."""
    aest_hour = (datetime.now(timezone.utc) + _AEST_OFFSET).hour
    return OrchestratorConfig.BUSINESS_HOURS_START <= aest_hour < OrchestratorConfig.BUSINESS_HOURS_END


def _send(message: str) -> None:
    """Send a notification via the configured backend."""
    if not _notifications_enabled:
        return
    if _notifier:
        _notifier(message)
    else:
        print(f"[ORCHESTRATOR] {message}", file=sys.stderr)


def notify_build_created(build_id: str, prompt: str) -> None:
    """Notify that a new build has been created."""
    if not _should_notify_now():
        return
    _send(f"Build created: {build_id} — {prompt[:80]}")


def notify_sprint_failed(build_id: str, sprint_id: str, sprint_number: int, attempts: int) -> None:
    """Notify that a sprint has failed after exhausting retries."""
    if not _should_notify_now():
        return
    _send(f"Sprint failed: build={build_id} sprint={sprint_number} ({sprint_id}) after {attempts} attempt(s)")


def notify_build_complete(build_id: str, prompt: str) -> None:
    """Notify that a build has completed all sprints and is ready for review."""
    if not _should_notify_now():
        return
    _send(f"Build complete: {build_id} — {prompt[:80]}")


def notify_escalation(build_id: str, sprint_id: str, sprint_number: int, reason: str) -> None:
    """Notify that a sprint has been escalated for human review."""
    if not _should_notify_now():
        return
    _send(f"Escalation: build={build_id} sprint={sprint_number} ({sprint_id}) — {reason}")
