"""Merger — conflict detection and LLM-assisted merge resolution.

Detects git merge conflicts, parses conflict hunks, resolves them via
a Haiku-tier Claude call, and commits the result. Used by the merge
queue in Sprint 2.
"""
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data types ───────────────────────────────────────────────────────

@dataclass
class MergeResult:
    success: bool
    conflict_files: list[str]
    resolution_log: str


# ── Internal helpers ─────────────────────────────────────────────────

def _run(args: list[str], cwd: str, check: bool = False,
         timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing stdout/stderr."""
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


# ── Public API ───────────────────────────────────────────────────────

def detect_conflicts(project_path: str, source_branch: str,
                     target_branch: str = "main") -> list[str]:
    """Attempt a dry-run merge and return a list of conflicting file paths.

    Runs `git merge --no-commit --no-ff` then immediately aborts.
    Returns an empty list when the merge would be clean.
    """
    # Ensure we're on the target branch first
    _run(["git", "checkout", target_branch], cwd=project_path)

    merge = _run(
        ["git", "merge", "--no-commit", "--no-ff", source_branch],
        cwd=project_path,
    )

    conflict_files: list[str] = []

    if merge.returncode != 0:
        # Parse files with unresolved conflicts (diff-filter=U = Unmerged)
        diff = _run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=project_path,
        )
        conflict_files = [
            line.strip()
            for line in diff.stdout.splitlines()
            if line.strip()
        ]

    # Always abort — we never leave a half-merged state
    _run(["git", "merge", "--abort"], cwd=project_path)

    return conflict_files


def extract_conflict_hunks(file_path: str) -> list[dict]:
    """Parse conflict markers in a file into structured hunks.

    Each hunk dict has keys:
        ours           — content from HEAD (between <<<<<<< and =======)
        theirs         — content from incoming branch (between ======= and >>>>>>>)
        context_before — up to 20 lines before the conflict marker
        context_after  — up to 20 lines after the conflict end marker
    """
    path = Path(file_path)
    lines = path.read_text().splitlines(keepends=True)

    hunks: list[dict] = []

    CONTEXT = 20
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            hunk_start = i

            # Collect context before
            context_before = "".join(lines[max(0, hunk_start - CONTEXT):hunk_start])

            # Collect ours (HEAD side)
            ours_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("======="):
                ours_lines.append(lines[i])
                i += 1

            # Skip the ======= separator
            i += 1

            # Collect theirs (incoming side)
            theirs_lines = []
            while i < len(lines) and not lines[i].startswith(">>>>>>>"):
                theirs_lines.append(lines[i])
                i += 1

            hunk_end = i  # points at the >>>>>>> line

            # Collect context after
            context_after = "".join(
                lines[hunk_end + 1: hunk_end + 1 + CONTEXT]
            )

            hunks.append({
                "ours": "".join(ours_lines),
                "theirs": "".join(theirs_lines),
                "context_before": context_before,
                "context_after": context_after,
            })

        i += 1

    return hunks


def resolve_with_llm(hunks: list[dict], file_path: str,
                     model: str = "haiku") -> str:
    """Ask Claude to merge the conflict hunks and return resolved file content.

    Shells out to `claude -p <prompt> --model haiku`. The prompt instructs
    Claude to produce the complete resolved file — no conflict markers.
    """
    hunk_sections = []
    for idx, h in enumerate(hunks, start=1):
        hunk_sections.append(
            f"=== Conflict {idx} ===\n"
            f"--- Context before ---\n{h['context_before']}\n"
            f"--- Ours (HEAD) ---\n{h['ours']}\n"
            f"--- Theirs (incoming) ---\n{h['theirs']}\n"
            f"--- Context after ---\n{h['context_after']}\n"
        )

    prompt = (
        f"You are resolving a git merge conflict in: {file_path}\n\n"
        "Both changes are intentional. Merge preserving both behaviors.\n\n"
        "Here are the conflict hunks with surrounding context:\n\n"
        + "\n".join(hunk_sections)
        + "\n\nReturn ONLY the resolved content for the conflicting sections "
        "(no conflict markers, no explanation, no code fences). "
        "The resolved content will replace the conflict region in the file."
    )

    result = subprocess.run(
        ["claude", "-p", prompt, "--model", model],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"LLM resolution failed for {file_path}: {result.stderr.strip()}"
        )

    return result.stdout


def apply_resolution(project_path: str, file_path: str,
                     resolved_content: str) -> bool:
    """Write resolved content to disk and stage the file.

    file_path may be relative (to project_path) or absolute.
    Returns True on success.
    """
    abs_path = Path(file_path) if Path(file_path).is_absolute() \
        else Path(project_path) / file_path

    try:
        abs_path.write_text(resolved_content)
    except OSError as exc:
        return False

    result = _run(
        ["git", "add", str(abs_path)],
        cwd=project_path,
    )
    return result.returncode == 0


def merge_branch(project_path: str, source_branch: str,
                 target_branch: str = "main") -> MergeResult:
    """Merge source_branch into target_branch, resolving conflicts via LLM.

    Flow:
      1. Enable rerere so git learns resolutions for future reuse.
      2. Attempt `git merge --no-ff`.
      3. If clean → return success.
      4. If conflicts → resolve each file, commit the merge.
      5. If any resolution fails → abort and return failure.
    """
    # Enable rerere (reuse recorded resolution) for this repo
    _run(
        ["git", "config", "rerere.enabled", "true"],
        cwd=project_path,
    )

    # Ensure we're on the target branch
    _run(["git", "checkout", target_branch], cwd=project_path)

    merge = _run(
        ["git", "merge", "--no-ff", source_branch],
        cwd=project_path,
    )

    if merge.returncode == 0:
        return MergeResult(
            success=True,
            conflict_files=[],
            resolution_log="Clean merge — no conflicts.",
        )

    # Identify conflicting files
    diff = _run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=project_path,
    )
    conflict_files = [
        line.strip()
        for line in diff.stdout.splitlines()
        if line.strip()
    ]

    if not conflict_files:
        # Merge failed for a non-conflict reason
        _run(["git", "merge", "--abort"], cwd=project_path)
        return MergeResult(
            success=False,
            conflict_files=[],
            resolution_log=f"Merge failed (non-conflict): {merge.stderr.strip()}",
        )

    log_lines: list[str] = []

    for rel_path in conflict_files:
        abs_path = str(Path(project_path) / rel_path)
        try:
            hunks = extract_conflict_hunks(abs_path)
            if not hunks:
                log_lines.append(f"{rel_path}: no conflict markers found — skipping")
                continue

            resolved = resolve_with_llm(hunks, rel_path)
            ok = apply_resolution(project_path, rel_path, resolved)

            if not ok:
                log_lines.append(f"{rel_path}: apply_resolution failed")
                _run(["git", "merge", "--abort"], cwd=project_path)
                return MergeResult(
                    success=False,
                    conflict_files=conflict_files,
                    resolution_log="\n".join(log_lines),
                )

            log_lines.append(f"{rel_path}: resolved via LLM")

        except Exception as exc:  # noqa: BLE001
            log_lines.append(f"{rel_path}: error — {exc}")
            _run(["git", "merge", "--abort"], cwd=project_path)
            return MergeResult(
                success=False,
                conflict_files=conflict_files,
                resolution_log="\n".join(log_lines),
            )

    # Commit the resolved merge
    commit = _run(
        ["git", "commit", "--no-edit"],
        cwd=project_path,
    )

    if commit.returncode != 0:
        _run(["git", "merge", "--abort"], cwd=project_path)
        return MergeResult(
            success=False,
            conflict_files=conflict_files,
            resolution_log="\n".join(log_lines)
            + f"\nCommit failed: {commit.stderr.strip()}",
        )

    return MergeResult(
        success=True,
        conflict_files=conflict_files,
        resolution_log="\n".join(log_lines),
    )


def run_post_merge_formatter(project_path: str,
                              changed_files: list[str]) -> bool:
    """Run formatters on changed files after a merge.

    Tries `black` for .py files and `prettier` for .js/.ts files if
    the respective tools are installed. Returns True if everything that
    ran succeeded (or no formatter was needed).
    """
    py_files = [f for f in changed_files if f.endswith(".py")]
    js_files = [f for f in changed_files if f.endswith((".js", ".ts"))]

    success = True

    if py_files and shutil.which("black"):
        result = _run(
            ["black"] + py_files,
            cwd=project_path,
        )
        if result.returncode != 0:
            success = False

    if js_files and shutil.which("prettier"):
        result = _run(
            ["prettier", "--write"] + js_files,
            cwd=project_path,
        )
        if result.returncode != 0:
            success = False

    return success
