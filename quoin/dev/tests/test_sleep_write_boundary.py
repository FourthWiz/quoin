#!/usr/bin/env python3
"""
test_sleep_write_boundary.py — asserts /sleep NEVER writes outside allowed targets.

Two-layer test (per plan D-06 / T-09):

Layer 1 — module purity:
  Run sleep_score.py --dry-run against the no_auto_memory_bleed/ fixture as a subprocess.
  Assert:
    (a) Exit code 0.
    (b) No stdout line contains ~/.claude/projects/ or /Users/*/.claude/projects/ paths.
  sleep_score.py is a pure-scoring module with no write path — this is a belt-and-suspenders
  check that scoring output never leaks auto-memory paths.

Layer 2 — SKILL.md body text grep:
  Open quoin/adapters/claude/skills/sleep/SKILL.md and assert:
    (a) The literal string "ONLY writes to" appears (write-target restriction statement).

Both layers must pass. Exit 0 on success, non-zero on failure.

Runnable with:
  .venv/bin/python quoin/dev/tests/test_sleep_write_boundary.py
"""

import re
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # Claude_workflow/
_SCRIPT = _PROJECT_ROOT / "quoin" / "scripts" / "sleep_score.py"
_FIXTURE_DIR = _PROJECT_ROOT / "quoin" / "dev" / "tests" / "fixtures" / "sleep" / "no_auto_memory_bleed"
_SKILL_MD = _PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills" / "sleep" / "SKILL.md"

# Python executable: prefer .venv if it exists
_VENV_PYTHON = _PROJECT_ROOT / ".venv" / "bin" / "python"
_PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def _pass(message: str) -> None:
    print(f"PASS: {message}")


def test_layer1_module_purity() -> None:
    """Layer 1: run sleep_score.py --dry-run; assert no auto-memory paths in output."""
    print("\n--- Layer 1: module purity (subprocess dry-run) ---")

    if not _SCRIPT.exists():
        _fail(f"sleep_score.py not found at {_SCRIPT}")

    if not _FIXTURE_DIR.exists():
        _fail(f"Fixture directory not found: {_FIXTURE_DIR}")

    cmd = [
        _PYTHON,
        str(_SCRIPT),
        "--dry-run",
        "--scan-dir", str(_FIXTURE_DIR),
        "--scan-days", "365",
        "--output", "json",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # (a) Exit code must be 0
    if result.returncode != 0:
        _fail(
            f"sleep_score.py exited with code {result.returncode}.\n"
            f"stderr: {result.stderr}\n"
            f"stdout: {result.stdout}"
        )

    stdout_lines = result.stdout.splitlines()

    # (b) No stdout line may contain auto-memory paths
    auto_memory_pattern = re.compile(r"~/\.claude/projects/|/\.claude/projects/")

    offending_lines = [
        (i + 1, line)
        for i, line in enumerate(stdout_lines)
        if auto_memory_pattern.search(line)
    ]

    if offending_lines:
        lines_str = "\n".join(f"  line {ln}: {txt}" for ln, txt in offending_lines)
        _fail(
            f"Output contains auto-memory paths (~/.claude/projects/) — write-target violation:\n{lines_str}"
        )

    _pass(f"Layer 1: {len(stdout_lines)} output line(s), 0 contain auto-memory paths")


def test_layer2_skill_md_text() -> None:
    """Layer 2: SKILL.md contains literal 'ONLY writes to' write-target restriction."""
    print("\n--- Layer 2: SKILL.md body text grep ---")

    if not _SKILL_MD.exists():
        _fail(f"quoin/adapters/claude/skills/sleep/SKILL.md not found at {_SKILL_MD}")

    content = _SKILL_MD.read_text(encoding="utf-8")

    # Assert the hard-rule literal string is present
    if "ONLY writes to" not in content:
        _fail(
            f"SKILL.md does not contain the literal string 'ONLY writes to'.\n"
            f"The Write-target restriction section is missing or was modified.\n"
            f"File: {_SKILL_MD}"
        )

    _pass("Layer 2: 'ONLY writes to' found in SKILL.md")


def main() -> None:
    print(f"test_sleep_write_boundary.py — project root: {_PROJECT_ROOT}")

    failures = 0

    # Layer 1
    try:
        test_layer1_module_purity()
    except SystemExit as e:
        failures += 1
        # Continue to Layer 2 even if Layer 1 fails, to report all issues

    # Layer 2
    try:
        test_layer2_skill_md_text()
    except SystemExit as e:
        failures += 1

    if failures > 0:
        print(f"\nFAILED: {failures} layer(s) failed.", file=sys.stderr)
        sys.exit(1)

    print("\nAll layers passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
