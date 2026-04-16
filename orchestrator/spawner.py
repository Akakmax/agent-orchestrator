"""Spawner — tmux + headless backends, shared CC session lock.

Manages process spawning for orchestrator agent sessions and a shared
session lock that prevents concurrent Claude Code usage between the
orchestrator and kanban systems.
"""
import json
import os
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import OrchestratorConfig

SESSION_LOCK_PATH = Path("/tmp/claude-session.lock")


# ── PID helper ───────────────────────────────────────────────────────

def _pid_is_alive(pid: int) -> bool:
    """Check if PID is alive AND belongs to a relevant process.

    Just checking os.kill(pid, 0) is NOT enough — macOS recycles PIDs
    to unrelated processes. We verify the process name contains
    'python', 'claude', or 'node'.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=5,
        )
        comm = result.stdout.strip().lower()
        return any(name in comm for name in ("python", "claude", "node", "bash", "codex", "gemini", "ollama"))
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── Session lock ─────────────────────────────────────────────────────

def acquire_session_lock(owner: str, purpose: str) -> bool:
    """Try to acquire the shared CC session lock. Returns True if acquired.

    If a stale lock exists (dead PID), it's cleaned up automatically.
    """
    lock = SESSION_LOCK_PATH

    if lock.exists():
        try:
            data = json.loads(lock.read_text())
            pid = data.get("pid")
            if pid and _pid_is_alive(pid):
                return False  # Lock held by a live process
            # PID dead or not relevant — stale lock
        except (json.JSONDecodeError, KeyError, ValueError):
            pass  # Corrupt lock file
        lock.unlink(missing_ok=True)

    lock.write_text(json.dumps({
        "pid": os.getpid(),
        "owner": owner,
        "purpose": purpose,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }))
    return True


def release_session_lock() -> None:
    """Release the shared CC session lock."""
    SESSION_LOCK_PATH.unlink(missing_ok=True)


def is_session_locked() -> bool:
    """Check if the session lock is held by a live process.

    Cleans up stale locks automatically.
    """
    lock = SESSION_LOCK_PATH
    if not lock.exists():
        return False
    try:
        data = json.loads(lock.read_text())
        pid = data.get("pid")
        if pid and _pid_is_alive(pid):
            return True
    except (json.JSONDecodeError, KeyError, ValueError):
        pass
    # Stale or corrupt lock
    lock.unlink(missing_ok=True)
    return False


def get_session_lock_info() -> Optional[dict]:
    """Read the lock file contents. Returns None if no lock."""
    lock = SESSION_LOCK_PATH
    if not lock.exists():
        return None
    try:
        return json.loads(lock.read_text())
    except (json.JSONDecodeError, ValueError):
        return None


# ── Spawn backends ───────────────────────────────────────────────────

class SpawnBackend(ABC):
    """Base class for process spawning backends."""

    @abstractmethod
    def spawn(self, session_name: str, window_name: str,
              command: str, log_path: str,
              post_exit_command: str = "") -> str:
        """Spawn a command. Returns a session_id string.

        post_exit_command: optional shell command appended after exit code
        is written (used by comm backends for completion callbacks).
        """

    @abstractmethod
    def is_alive(self, session_id: str, log_path: str = None) -> bool:
        """Check if the spawned process is still running."""

    def get_output(self, session_id: str, log_path: str = None) -> str:
        """Read the log file output."""
        if log_path and Path(log_path).exists():
            return Path(log_path).read_text()
        return ""

    def get_exit_code(self, log_path: str) -> Optional[int]:
        """Read the exit code from the .exit file."""
        exit_path = Path(f"{log_path}.exit")
        if exit_path.exists():
            try:
                return int(exit_path.read_text().strip())
            except (ValueError, OSError):
                return None
        return None


class TmuxBackend(SpawnBackend):
    """Spawn commands in tmux sessions with log capture."""

    def spawn(self, session_name: str, window_name: str,
              command: str, log_path: str,
              post_exit_command: str = "") -> str:
        session_id = f"{session_name}:{window_name}"

        # Write command to a script file (REVIEW FIX m3: avoids quote breakage)
        script_path = f"{log_path}.sh"
        script_lines = (
            f"# Heartbeat sidecar — writes timestamp every 5 min while alive\n"
            f"(while true; do date -u +%FT%TZ > {log_path}.heartbeat; sleep 300; done) &\n"
            f"HB_PID=$!\n"
            f"trap 'kill $HB_PID 2>/dev/null; wait $HB_PID 2>/dev/null' EXIT\n"
            f"set -o pipefail; {command} 2>&1 | tee {log_path}\n"
            f"EXIT_CODE=$?; echo $EXIT_CODE > {log_path}.exit\n"
        )
        if post_exit_command:
            script_lines += f"{post_exit_command}\n"
        Path(script_path).write_text(script_lines)

        # Check if tmux session exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, text=True,
        )

        if result.returncode != 0:
            # Create new session with window
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name,
                 "-n", window_name],
                capture_output=True, text=True,
            )
        else:
            # Add window to existing session
            subprocess.run(
                ["tmux", "new-window", "-t", session_name,
                 "-n", window_name],
                capture_output=True, text=True,
            )

        # Keep pane around after command exits for debugging
        if OrchestratorConfig.TMUX_REMAIN_ON_EXIT:
            subprocess.run(
                ["tmux", "set-option", "-t", session_id,
                 "remain-on-exit", "on"],
                capture_output=True, text=True,
            )

        # Send the command
        subprocess.run(
            ["tmux", "send-keys", "-t", session_id,
             f"bash {script_path}", "Enter"],
            capture_output=True, text=True,
        )

        return session_id

    def is_alive(self, session_id: str, log_path: str = None) -> bool:
        # If exit file exists, process is done
        if log_path and Path(f"{log_path}.exit").exists():
            return False

        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_id,
             "-F", "#{pane_dead}"],
            capture_output=True, text=True,
        )

        if result.returncode != 0:
            return False  # Session/window doesn't exist

        # pane_dead: "1" = dead, "0" = alive
        return result.stdout.strip() == "0"


class HeadlessBackend(SpawnBackend):
    """Spawn commands as background processes."""

    def spawn(self, session_name: str, window_name: str,
              command: str, log_path: str,
              post_exit_command: str = "") -> str:
        # Write script file like tmux backend
        script_path = f"{log_path}.sh"
        script_lines = (
            f"# Heartbeat sidecar — writes timestamp every 5 min while alive\n"
            f"(while true; do date -u +%FT%TZ > {log_path}.heartbeat; sleep 300; done) &\n"
            f"HB_PID=$!\n"
            f"trap 'kill $HB_PID 2>/dev/null; wait $HB_PID 2>/dev/null' EXIT\n"
            f"set -o pipefail; {command} 2>&1 | tee {log_path}\n"
            f"EXIT_CODE=$?; echo $EXIT_CODE > {log_path}.exit\n"
        )
        if post_exit_command:
            script_lines += f"{post_exit_command}\n"
        Path(script_path).write_text(script_lines)

        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            ["bash", script_path],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return f"pid:{proc.pid}"

    def is_alive(self, session_id: str, log_path: str = None) -> bool:
        # REVIEW FIX m1: use _pid_is_alive instead of bare os.kill
        pid = int(session_id.split(":")[1])
        return _pid_is_alive(pid)


# ── Factory ──────────────────────────────────────────────────────────

_BACKENDS = {
    "tmux": TmuxBackend,
    "headless": HeadlessBackend,
}


def get_backend(name: str = None) -> SpawnBackend:
    """Get a spawn backend by name. Defaults to OrchestratorConfig.SPAWN_BACKEND."""
    name = name or OrchestratorConfig.SPAWN_BACKEND
    cls = _BACKENDS.get(name)
    if cls is None:
        raise ValueError(f"Unknown spawn backend: {name!r}. Choose from: {list(_BACKENDS)}")
    return cls()
