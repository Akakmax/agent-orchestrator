"""Generator agent — builds the prompt and spawn args for sprint building.

Reads generator.md, formats it with sprint/contract data, returns a command
string that gets passed to `claude -p`.
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


def build_generator_command(
    db: OrchestratorDB,
    build_id: str,
    sprint_id: str,
    retry_context: str = "",
) -> str:
    """Format the generator prompt template with sprint and contract data.

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
    if contract and contract.get("criteria"):
        criteria_raw = contract["criteria"]
        # criteria is stored as JSON string in the DB
        if isinstance(criteria_raw, str):
            criteria_parsed = json.loads(criteria_raw)
        else:
            criteria_parsed = criteria_raw
        contract_criteria = json.dumps(criteria_parsed, indent=2)
    else:
        contract_criteria = "(No contract criteria found)"

    template = (_PROMPTS_DIR / "generator.md").read_text()
    builds_dir = OrchestratorConfig.BUILDS_DIR
    project_path = build.get("project_path") or f"{builds_dir}/{build_id}/project"
    git_branch = build.get("git_branch") or f"build/{build_id}"

    sprint_num = sprint["sprint_number"]
    log_path = str(Path(OrchestratorConfig.LOG_DIR) / f"build-{build_id}-sprint{sprint_num}-generator.log")

    return template.format(
        build_id=build_id,
        sprint_number=sprint_num,
        sprint_title=sprint["title"],
        contract_criteria=contract_criteria,
        builds_dir=builds_dir,
        project_path=project_path,
        git_branch=git_branch,
        sprint_id=sprint_id,
        python=_find_python(),
        cli=_cli_path(),
        retry_context=retry_context,
        allowed_paths=contract_criteria,
        allowed_new_paths="(see contract criteria)",
        checkpoint_interval=sprint.get("checkpoint_interval_minutes", 10),
        sprint_branch=sprint.get("git_branch", f"sprint/{sprint_id}"),
        base_commit=sprint.get("base_commit", "main"),
        log_path=log_path,
    )


def get_generator_spawn_args(
    db: OrchestratorDB,
    build_id: str,
    sprint_id: str,
    retry_context: str = "",
) -> dict:
    """Return spawn args dict for the generator agent.

    Keys: session_name, window_name, command, log_path
    """
    sprint = db.get_sprint(sprint_id)
    if not sprint:
        raise ValueError(f"Sprint {sprint_id} not found")

    prompt = build_generator_command(db, build_id, sprint_id, retry_context)
    sprint_num = sprint["sprint_number"]
    log_dir = Path(OrchestratorConfig.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = str(log_dir / f"build-{build_id}-sprint{sprint_num}-generator.log")
    prompt_file = f"{log_path}.prompt"
    Path(prompt_file).write_text(prompt)

    return {
        "session_name": f"build-{build_id}",
        "window_name": f"sprint{sprint_num}-generator",
        "command": build_command("generator", prompt, prompt_file=prompt_file),
        "log_path": log_path,
    }
