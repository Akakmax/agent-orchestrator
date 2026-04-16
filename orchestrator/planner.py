"""Planner agent — builds the prompt and spawn args for the planning phase.

Reads planner.md, formats it with build data, returns a command string
that gets passed to `claude -p`.
"""
import json
import shutil
from pathlib import Path

from .adapter import build_command
from .db import OrchestratorDB
from .models import OrchestratorConfig

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _find_python() -> str:
    venv = Path(__file__).parents[1] / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return shutil.which("python3") or "python3"


def _cli_path() -> str:
    return str(Path(__file__).parents[1] / "scripts" / "orchestrator-cli.py")


def build_planner_command(db: OrchestratorDB, build_id: str) -> str:
    """Format the planner prompt template with build data.

    Returns the fully rendered prompt string.
    """
    build = db.get_build(build_id)
    if not build:
        raise ValueError(f"Build {build_id} not found")

    template = (_PROMPTS_DIR / "planner.md").read_text()
    builds_dir = OrchestratorConfig.BUILDS_DIR
    project_path = build.get("project_path") or f"{builds_dir}/{build_id}/project"

    return template.format(
        build_id=build_id,
        prompt=build["prompt"],
        builds_dir=builds_dir,
        project_path=project_path,
        python=_find_python(),
        cli=_cli_path(),
    )


def get_planner_spawn_args(db: OrchestratorDB, build_id: str) -> dict:
    """Return spawn args dict for the planner agent.

    Keys: session_name, window_name, command, log_path
    """
    prompt = build_planner_command(db, build_id)
    log_dir = Path(OrchestratorConfig.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = str(log_dir / f"build-{build_id}-planner.log")
    prompt_file = f"{log_path}.prompt"
    Path(prompt_file).write_text(prompt)

    return {
        "session_name": f"build-{build_id}",
        "window_name": "planner",
        "command": build_command("planner", prompt, prompt_file=prompt_file),
        "log_path": log_path,
    }
