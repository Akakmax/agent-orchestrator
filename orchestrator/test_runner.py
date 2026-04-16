"""Test runner — targeted post-merge test execution.

Maps changed source files to their corresponding test files, then runs
them via pytest (or unittest as a fallback). Called from merger.py after
conflict resolution and formatting.
"""
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


# ── Data types ───────────────────────────────────────────────────────

@dataclass
class TestResult:
    passed: bool
    output: str
    duration_seconds: float
    tests_run: int
    tests_failed: int


# ── Internal helpers ─────────────────────────────────────────────────

def _run(args: list[str], cwd: str,
         timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing stdout/stderr."""
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _has_pytest(project_path: str) -> bool:
    """Check if pytest is importable in the project's Python environment."""
    result = _run(
        ["python3", "-m", "pytest", "--version"],
        cwd=project_path,
        timeout=10,
    )
    return result.returncode == 0


# ── Public API ───────────────────────────────────────────────────────

def map_files_to_tests(changed_files: list[str],
                       project_path: str) -> list[str]:
    """Map changed source files to their corresponding test files.

    Checks four heuristics per file and returns any paths that exist on
    disk (relative to project_path).

    Heuristics (all checked, all matches returned):
        a. Same directory: foo.py  →  test_foo.py
        b. Tests directory: src/foo.py  →  tests/test_foo.py
        c. Test suffix: foo.py  →  foo_test.py
        d. Mirror structure: src/auth/handler.py  →  tests/auth/test_handler.py
    """
    root = Path(project_path)
    found: list[str] = []
    seen: set[Path] = set()

    for rel in changed_files:
        src = Path(rel)
        stem = src.stem
        parent = src.parent

        candidates: list[Path] = [
            # a. Same directory, test_ prefix
            parent / f"test_{stem}.py",
            # b. Top-level tests/ directory, test_ prefix
            Path("tests") / f"test_{stem}.py",
            # c. Same directory, _test suffix
            parent / f"{stem}_test.py",
            # d. Mirror structure under tests/, test_ prefix
            Path("tests") / parent / f"test_{stem}.py",
        ]

        for candidate in candidates:
            abs_candidate = root / candidate
            if abs_candidate not in seen and abs_candidate.exists():
                seen.add(abs_candidate)
                found.append(str(candidate))

    return found


def run_targeted_tests(test_files: list[str],
                       project_path: str) -> TestResult:
    """Run a specific set of test files and return a TestResult.

    Tries pytest first; falls back to unittest discover if pytest is not
    available. Returns a passing no-op result when test_files is empty.
    """
    if not test_files:
        return TestResult(
            passed=True,
            output="No tests to run",
            duration_seconds=0.0,
            tests_run=0,
            tests_failed=0,
        )

    start = time.monotonic()

    if _has_pytest(project_path):
        result = _run(
            ["python3", "-m", "pytest"] + test_files + ["-v", "--tb=short"],
            cwd=project_path,
        )
    else:
        result = _run(
            ["python3", "-m", "unittest"] + test_files,
            cwd=project_path,
        )

    duration = time.monotonic() - start
    output = (result.stdout + result.stderr).strip()
    passed = result.returncode == 0

    return TestResult(
        passed=passed,
        output=output,
        duration_seconds=round(duration, 3),
        tests_run=0,   # parsed from output when needed; 0 is safe default
        tests_failed=0 if passed else 1,
    )


def run_full_test_suite(project_path: str) -> TestResult:
    """Run the full test suite for the project and return a TestResult.

    Tries `python -m pytest` first; falls back to
    `python -m unittest discover` if pytest is not available.
    """
    start = time.monotonic()

    if _has_pytest(project_path):
        result = _run(
            ["python3", "-m", "pytest", "-v", "--tb=short"],
            cwd=project_path,
        )
    else:
        result = _run(
            ["python3", "-m", "unittest", "discover"],
            cwd=project_path,
        )

    duration = time.monotonic() - start
    output = (result.stdout + result.stderr).strip()
    passed = result.returncode == 0

    return TestResult(
        passed=passed,
        output=output,
        duration_seconds=round(duration, 3),
        tests_run=0,
        tests_failed=0 if passed else 1,
    )
