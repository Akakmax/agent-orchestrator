"""Agent adapter — config-driven CLI command builder.

Reads ~/.kanban/agents.toml to map roles to CLI agents.
Falls back to claude-code defaults when no config exists.
"""
import shlex
import shutil
from pathlib import Path
from typing import Optional

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

_CONFIG_PATH = Path.home() / ".kanban" / "agents.toml"

_DEFAULTS: dict = {
    "agents": {
        "claude-code": {
            "cli": "claude",
            "prompt_style": "flag",
            "prompt_flag": "-p",
            "extra_args": [],
            "session_resumable": False,
            "session_lock": True,
            "communication": {"protocol": "file"},
        }
    },
    "roles": {
        "planner": "claude-code",
        "generator": "claude-code",
        "evaluator": "claude-code",
        "retrospective": "claude-code",
    },
}

_config_cache: Optional[dict] = None


def load_config(force_reload: bool = False) -> dict:
    """Read agents.toml, merge with defaults, and cache after first load.

    Returns the merged config dict. Safe to call repeatedly — cached after
    the first successful load unless force_reload=True.
    """
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    if tomllib is None or not _CONFIG_PATH.exists():
        _config_cache = _DEFAULTS
        return _config_cache

    with _CONFIG_PATH.open("rb") as fh:
        raw = tomllib.load(fh)

    # Merge: file values override defaults, but defaults fill missing keys
    merged: dict = {}

    # Merge agents — start from defaults, overlay file entries
    default_agents = dict(_DEFAULTS["agents"])
    file_agents = raw.get("agents", {})
    merged["agents"] = {**default_agents, **file_agents}

    # Merge roles — file overrides defaults
    default_roles = dict(_DEFAULTS["roles"])
    file_roles = raw.get("roles", {})
    merged["roles"] = {**default_roles, **file_roles}

    _config_cache = merged
    return _config_cache


def _get_agent_config(role: str) -> dict:
    """Resolve role → agent name → agent config dict.

    Falls back to claude-code defaults if the role or agent is unknown.
    """
    cfg = load_config()
    agent_name = cfg["roles"].get(role, "claude-code")
    return cfg["agents"].get(agent_name, _DEFAULTS["agents"]["claude-code"])


def _find_cli(cli_name: str) -> str:
    """Resolve CLI binary path via shutil.which.

    Returns the resolved path, or the bare cli_name as fallback.
    """
    return shutil.which(cli_name) or cli_name


def build_command(
    role: str,
    prompt: str,
    prompt_file: Optional[str] = None,
) -> str:
    """Map role → agent → CLI command string.

    If prompt_file is provided the prompt is read from disk, avoiding shell
    injection.  Falls back to inline shell-escaped prompt for backward compat.
    """
    agent_cfg = _get_agent_config(role)
    cli = _find_cli(agent_cfg["cli"])
    style: str = agent_cfg.get("prompt_style", "flag")
    flag: str = agent_cfg.get("prompt_flag", "-p")
    extra: list = agent_cfg.get("extra_args", [])
    extra_str = " ".join(shlex.quote(a) for a in extra) if extra else ""

    if prompt_file:
        pf = shlex.quote(prompt_file)
        if style == "flag":
            # Prefer --prompt-file if that's the flag, else cat-based fallback
            if flag == "--prompt-file":
                parts = [cli, flag, pf]
                if extra_str:
                    parts.append(extra_str)
                return " ".join(parts)
            else:
                # cat-substitution variant
                parts = [cli, flag, f'"$(cat {pf})"']
                if extra_str:
                    parts.append(extra_str)
                return " ".join(parts)

        elif style == "subcommand":
            parts = [cli, flag]
            if extra_str:
                parts.append(extra_str)
            parts.append(f'"$(cat {pf})"')
            return " ".join(parts)

        else:  # stdin
            parts = [f"cat {pf}", "|", cli]
            if extra_str:
                parts.append(extra_str)
            return " ".join(parts)

    # ── Inline fallback (backward compat) ──────────────────────────────
    escaped = shlex.quote(prompt)
    if style == "flag":
        parts = [cli, flag, escaped]
        if extra_str:
            parts.append(extra_str)
        return " ".join(parts)

    elif style == "subcommand":
        parts = [cli, flag]
        if extra_str:
            parts.append(extra_str)
        parts.append(escaped)
        return " ".join(parts)

    else:  # stdin
        parts = [f"echo {escaped}", "|", cli]
        if extra_str:
            parts.append(extra_str)
        return " ".join(parts)


def get_agent_name(role: str) -> str:
    """Return the agent name string for a given role."""
    cfg = load_config()
    return cfg["roles"].get(role, "claude-code")


def needs_session_lock(role: str) -> bool:
    """Check if the agent for this role needs the shared CC session lock."""
    return bool(_get_agent_config(role).get("session_lock", False))
