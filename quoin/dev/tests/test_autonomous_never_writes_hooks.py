"""Structural regression guard for IVG-153 Stage 2 T-13 (R-05): autonomous
mode COOPERATES with the hooks-driven context-utilization machinery — it
never writes to a hook script and never lowers a threshold constant.

This is a STRUCTURAL guard (distinct from `test_autonomous_hooks_untouched.py`,
which is the Stage-1 content/diff-based guard): it greps the supervisor
module and the autonomous-tagged SKILL.md surface directly for (a) any
file-write pattern that targets a path under `hooks/`, and (b) any
reassignment of a `QUOIN_*_BPS` / threshold-shaped constant. It is
intentionally coarse and FAILS CLOSED — a future edit that writes to a
hook script, or that redefines/lowers a threshold anywhere in the
autonomous-tagged surface, trips this test even if the specific string
patterns in `test_autonomous_hooks_untouched.py` don't happen to catch it.
"""
from __future__ import annotations

import re
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
REPO_ROOT = PKG_DIR.parent  # quoin/ (git root)
HOOKS_DIR = PKG_DIR / "hooks"
SUPERVISOR_MODULE = REPO_ROOT / "src" / "quoin" / "supervisor.py"
CLI_MODULE = REPO_ROOT / "src" / "quoin" / "cli.py"
RUN_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
END_OF_TASK_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"
RUN_CORE_DOC = PKG_DIR / "core" / "skills" / "run_hard.md"

# Files this guard scans for hook-write / threshold-reassignment patterns.
# Python modules that implement the supervisor + CLI, and the two SKILL.md /
# core-doc files that carry the Stage-2 autonomous edits (T-09..T-13).
SCANNED_FILES = [
    SUPERVISOR_MODULE,
    CLI_MODULE,
    RUN_SKILL,
    END_OF_TASK_SKILL,
    RUN_CORE_DOC,
]

# Any of these substrings appearing next to a write-shaped verb is a hard
# fail — a write TARGETING the hooks directory.
HOOK_WRITE_MARKERS = ("hooks/", "hooks\\")

# Write-shaped tokens: file-open-for-write modes, printf/cat redirection,
# mv-into, sed -i, python Path.write_text, shutil.copy/move destinations.
WRITE_VERB_PATTERN = re.compile(
    r'(open\([^)]*["\'][wa]b?["\']'  # open(..., "w"/"a"/"wb"/"ab")
    r'|write_text\('
    r'|write_bytes\('
    r'|>\s*[^\n]*hooks[/\\]'  # shell redirect into hooks/
    r'|mv\s+[^\n]*hooks[/\\]'  # mv ... hooks/...
    r'|fsops\.py\s+(?:mv|write-atomic|finalize|trash)\b[^\n]*hooks[/\\]'  # helper writes into hooks/
    r'|sed\s+-i[^\n]*hooks[/\\]'  # sed -i ... hooks/...
    r')',
    re.IGNORECASE,
)

# Threshold constant names that must never be reassigned (as opposed to
# merely read/referenced) anywhere in the autonomous-tagged surface.
THRESHOLD_CONSTANTS = (
    "QUOIN_BLOCK_BPS",
    "QUOIN_STOP_BPS",
    "QUOIN_COMPACT_FIRST_BPS",
    "QUOIN_PANIC_BPS",
    "BLOCK_BPS",
    "STOP_BPS",
    "COMPACT_FIRST_BPS",
    "PANIC_BPS",
)

# A reassignment looks like `NAME=<value>` (shell) or `NAME = <value>`
# (python) with no `:-` default-expansion marker immediately after (which
# would indicate a read/reference via `${NAME:-default}`, not a write).
THRESHOLD_ASSIGN_PATTERN = re.compile(
    r'\b(' + "|".join(re.escape(c) for c in THRESHOLD_CONSTANTS) + r')\s*='
)


def _existing_scanned_files() -> list[Path]:
    return [p for p in SCANNED_FILES if p.is_file()]


def _writes_into_hooks(line: str) -> bool:
    return any(marker in line for marker in HOOK_WRITE_MARKERS) and bool(WRITE_VERB_PATTERN.search(line))


def test_fsops_writes_into_hooks_are_detected() -> None:
    assert _writes_into_hooks("printf 'x' | python3 ~/.claude/scripts/fsops.py write-atomic \"~/.claude/hooks/stop.sh\"")
    assert _writes_into_hooks('python3 fsops.py mv "a" "hooks/b.sh"')
    assert _writes_into_hooks('python3 fsops.py finalize "a.tmp" "hooks/a" --cleanup "a.tmp"')
    assert not _writes_into_hooks('python3 fsops.py write-atomic ".workflow_artifacts/memory/x"')


def test_hooks_dir_roster_unchanged() -> None:
    """Corroborating structural check: the hooks/ directory still has
    exactly its known 8-script roster (`stop.sh` is the run-continuation
    hook added by ordinary development, IVG-280) — no script was added,
    removed, or renamed by the T-12/T-13 autonomous work."""
    assert HOOKS_DIR.is_dir()
    names = sorted(p.name for p in HOOKS_DIR.glob("*.sh"))
    expected = sorted(
        [
            "_lib.sh",
            "postcompact.sh",
            "precompact.sh",
            "rules.sh",
            "sessionend.sh",
            "sessionstart.sh",
            "stop.sh",
            "userpromptsubmit.sh",
            "worktreecreate.sh",
        ]
    )
    assert names == expected, f"hooks/ script roster changed: {names}"


def test_lib_sh_has_no_removed_threshold_defaults() -> None:
    """The retired context-threshold defaults and the no-run checkpoint knob
    stay out of hooks/_lib.sh, while the two live defaults remain."""
    lib_sh = HOOKS_DIR / "_lib.sh"
    assert lib_sh.is_file()
    text = lib_sh.read_text(encoding="utf-8")
    for needle in (
        "${QUOIN_BLOCK_BPS:-",
        "${QUOIN_STOP_BPS:-",
        "${QUOIN_COMPACT_FIRST_BPS:-",
        "${QUOIN_PANIC_BPS:-",
        "PRECOMPACT_NORUN_CHECKPOINT",
    ):
        assert needle not in text, f"hooks/_lib.sh still carries {needle}"
    assert "${QUOIN_BYTES_PER_TOKEN:-8.0}" in text
    assert "${QUOIN_EFFECTIVE_CONTEXT_LIMIT:-150000}" in text
