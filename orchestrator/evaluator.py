"""Evaluator agent — builds the prompt and spawn args for sprint QA.

Reads evaluator.md, formats it with sprint/contract data, returns a command
string that gets passed to `claude -p`.
"""
import json
import shutil
from pathlib import Path

from .adapter import build_command
from .db import OrchestratorDB
from .models import OrchestratorConfig

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _extract_path_list(criteria: dict, key: str, sprint: dict) -> list[str]:
    """Extract a path list from contract criteria, falling back to sprint row."""
    if isinstance(criteria, dict) and key in criteria:
        val = criteria[key]
        return val if isinstance(val, list) else []
    raw = sprint.get(key)
    if raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _find_python() -> str:
    venv = Path(__file__).parents[1] / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return shutil.which("python3") or "python3"


def _cli_path() -> str:
    return str(Path(__file__).parents[1] / "scripts" / "orchestrator-cli.py")


def build_evaluator_command(
    db: OrchestratorDB,
    build_id: str,
    sprint_id: str,
) -> str:
    """Format the evaluator prompt template with sprint and contract data.

    Returns the fully rendered prompt string.
    """
    build = db.get_build(build_id)
    if not build:
        raise ValueError(f"Build {build_id} not found")

    sprint = db.get_sprint(sprint_id)
    if not sprint:
        raise ValueError(f"Sprint {sprint_id} not found")

    # Get contract criteria — use latest approved contract for this sprint
    contract = db.get_latest_contract(sprint_id)
    criteria_parsed = {}
    if contract and contract.get("criteria"):
        criteria_raw = contract["criteria"]
        if isinstance(criteria_raw, str):
            criteria_parsed = json.loads(criteria_raw)
        else:
            criteria_parsed = criteria_raw
        contract_criteria = json.dumps(criteria_parsed, indent=2)
    else:
        contract_criteria = "(No contract criteria found)"

    template = (_PROMPTS_DIR / "evaluator.md").read_text()
    builds_dir = OrchestratorConfig.BUILDS_DIR

    sprint_num = sprint["sprint_number"]
    log_path = str(Path(OrchestratorConfig.LOG_DIR) / f"build-{build_id}-sprint{sprint_num}-evaluator.log")

    # Extract file boundaries from contract criteria or sprint row
    allowed_paths = _extract_path_list(criteria_parsed, "allowed_paths", sprint)
    allowed_new_paths = _extract_path_list(criteria_parsed, "allowed_new_paths", sprint)

    allowed_paths_str = "\n".join(f"- {p}" for p in allowed_paths) if allowed_paths else "(No file boundaries defined — all project files allowed)"
    allowed_new_str = "\n".join(f"- {p}" for p in allowed_new_paths) if allowed_new_paths else "(No new-file boundaries defined — may create files in project root)"

    return template.format(
        build_id=build_id,
        sprint_number=sprint_num,
        sprint_title=sprint["title"],
        contract_criteria=contract_criteria,
        builds_dir=builds_dir,
        sprint_id=sprint_id,
        python=_find_python(),
        cli=_cli_path(),
        allowed_paths=allowed_paths_str,
        allowed_new_paths=allowed_new_str,
        base_commit=sprint.get("base_commit", "main"),
        log_path=log_path,
    )


def get_evaluator_spawn_args(
    db: OrchestratorDB,
    build_id: str,
    sprint_id: str,
) -> dict:
    """Return spawn args dict for the evaluator agent.

    Keys: session_name, window_name, command, log_path
    """
    sprint = db.get_sprint(sprint_id)
    if not sprint:
        raise ValueError(f"Sprint {sprint_id} not found")

    prompt = build_evaluator_command(db, build_id, sprint_id)
    sprint_num = sprint["sprint_number"]
    log_dir = Path(OrchestratorConfig.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = str(log_dir / f"build-{build_id}-sprint{sprint_num}-evaluator.log")
    prompt_file = f"{log_path}.prompt"
    Path(prompt_file).write_text(prompt)

    return {
        "session_name": f"build-{build_id}",
        "window_name": f"sprint{sprint_num}-evaluator",
        "command": build_command("evaluator", prompt, prompt_file=prompt_file),
        "log_path": log_path,
    }
