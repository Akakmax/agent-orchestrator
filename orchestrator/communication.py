"""Communication backends — how the orchestrator detects agent completion.

Each backend implements build_callback_command() (injected into the spawner
wrapper script after exit) and optional start/stop listener hooks for
push-based protocols.
"""
from abc import ABC, abstractmethod
from pathlib import Path

from .adapter import _get_agent_config


# ── Base class ────────────────────────────────────────────────────────

class CommBackend(ABC):
    """Base class for orchestrator communication backends."""

    @abstractmethod
    def build_callback_command(
        self,
        build_id: str,
        sprint_id: str,
        log_path: str,
    ) -> str:
        """Return a shell command appended to the wrapper script after exit.

        The command is run after the agent exits.  Return an empty string
        if no callback is needed.
        """

    def start_listener(self) -> None:
        """Start any background listener (no-op for poll/file backends)."""

    def stop_listener(self) -> None:
        """Stop any background listener (no-op for poll/file backends)."""


# ── Backends ──────────────────────────────────────────────────────────

class FileBackend(CommBackend):
    """Default backend — completion detected via the .exit file on disk.

    The spawner wrapper already writes log_path.exit; no extra callback needed.
    """

    def build_callback_command(
        self,
        build_id: str,
        sprint_id: str,
        log_path: str,
    ) -> str:
        """No additional callback required — exit file is written by spawner."""
        return ""


class SendMessageBackend(CommBackend):
    """Push backend — calls orchestrator-cli.py msg send after agent exits.

    Parameters
    ----------
    python_path:
        Absolute path to the Python interpreter to use.
    cli_path:
        Absolute path to orchestrator-cli.py.
    """

    def __init__(self, python_path: str, cli_path: str) -> None:
        self._python = python_path
        self._cli = cli_path

    def build_callback_command(
        self,
        build_id: str,
        sprint_id: str,
        log_path: str,
    ) -> str:
        """Return a shell command that sends a completion message via CLI."""
        return (
            f'{self._python} {self._cli} msg send'
            f' --build {build_id} --sprint {sprint_id}'
            f' --from agent --to orchestrator --type update'
            f' --body "agent completed: exit_code=$(cat {log_path}.exit 2>/dev/null || echo unknown)"'
        )


class HttpBackend(CommBackend):
    """Push backend — POSTs a JSON callback to localhost after agent exits.

    Config dict keys:
        callback_port (int, required)
        callback_url  (str, optional, default /callback/<sprint_id>)
    """

    def __init__(self, config: dict) -> None:
        self._port: int = config["callback_port"]
        self._url_tmpl: str = config.get("callback_url", "/callback/{sprint_id}")

    def build_callback_command(
        self,
        build_id: str,
        sprint_id: str,
        log_path: str,
    ) -> str:
        """Return a curl POST command sent after the agent exits."""
        try:
            url = f"http://localhost:{self._port}" + self._url_tmpl.format(
                sprint_id=sprint_id,
            )
        except KeyError:
            # Malformed callback_url template — fall back to default
            url = f"http://localhost:{self._port}/callback/{sprint_id}"
        payload = (
            f'{{"build_id":"{build_id}",'
            f'"sprint_id":"{sprint_id}",'
            f'"log_path":"{log_path}"}}'
        )
        return f"curl -s -X POST -H 'Content-Type: application/json' -d '{payload}' {url}"


class UnixSocketBackend(CommBackend):
    """Push backend — writes completion notice to a Unix domain socket.

    Config dict keys:
        socket_path (str, required)
    """

    def __init__(self, config: dict) -> None:
        self._socket: str = config["socket_path"]

    def build_callback_command(
        self,
        build_id: str,
        sprint_id: str,
        log_path: str,
    ) -> str:
        """Return a socat command that writes a completion message to the socket."""
        msg = f"{build_id}:{sprint_id}:{log_path}"
        return f"echo {msg!r} | socat - UNIX-CONNECT:{self._socket}"


# ── Factory ───────────────────────────────────────────────────────────

def get_comm_backend(role: str) -> CommBackend:
    """Resolve role → agent → communication config → backend instance.

    Falls back to FileBackend for unknown or missing protocols.
    """
    agent_cfg = _get_agent_config(role)
    comm: dict = agent_cfg.get("communication", {})
    protocol: str = comm.get("protocol", "file")

    if protocol == "file":
        return FileBackend()

    if protocol == "sendmessage":
        # Lazy import to avoid circular dependency with generator.py
        from .generator import _find_python, _cli_path  # noqa: PLC0415
        return SendMessageBackend(
            python_path=_find_python(),
            cli_path=_cli_path(),
        )

    if protocol == "http":
        return HttpBackend(comm)

    if protocol == "unix_socket":
        return UnixSocketBackend(comm)

    # Unknown protocol — fall back to file-based detection
    return FileBackend()
