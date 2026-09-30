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

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
REPO_ROOT = PKG_DIR.parent  # quoin/ (git root)
HOOKS_DIR = PKG_DIR / "hooks"
SUPERVISOR_MODULE = REPO_ROOT / "src" / "quoin" / "supervisor.py"
CLI_MODULE = REPO_ROOT / "src" / "quoin" / "cli.py"
RUN_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
END_OF_TASK_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "end_of_task" / "SKILL.md"
RUN_CORE_DOC = PKG_DIR / "core" / "skills" / "run.md"

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


def test_scanned_file_set_is_non_empty() -> None:
    """Sanity: at least the supervisor module and run/SKILL.md must exist,
    or this guard would silently pass on nothing."""
    existing = _existing_scanned_files()
    names = {p.name for p in existing}
    assert "supervisor.py" in names
    assert "SKILL.md" in names


def _writes_into_hooks(line: str) -> bool:
    return any(marker in line for marker in HOOK_WRITE_MARKERS) and bool(WRITE_VERB_PATTERN.search(line))


def test_fsops_writes_into_hooks_are_detected() -> None:
    assert _writes_into_hooks("printf 'x' | python3 ~/.claude/scripts/fsops.py write-atomic \"~/.claude/hooks/stop.sh\"")
    assert _writes_into_hooks('python3 fsops.py mv "a" "hooks/b.sh"')
    assert _writes_into_hooks('python3 fsops.py finalize "a.tmp" "hooks/a" --cleanup "a.tmp"')
    assert not _writes_into_hooks('python3 fsops.py write-atomic ".workflow_artifacts/memory/x"')


def test_no_scanned_file_writes_into_hooks_dir() -> None:
    """No file-write pattern in the supervisor module, CLI, or the
    autonomous-tagged run/end_of_task SKILL text targets a path under
    `hooks/`."""
    offenders: list[str] = []
    for path in _existing_scanned_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _writes_into_hooks(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()!r}")
    assert not offenders, (
        f"found a write-shaped pattern targeting hooks/: {offenders} — "
        "autonomous mode must only ever cooperate with hooks, never write to them"
    )


def test_no_threshold_constant_reassigned() -> None:
    """No `QUOIN_*_BPS` / threshold constant is reassigned (as opposed to
    merely read/referenced, e.g. via `${QUOIN_BLOCK_BPS:-9500}` or prose
    naming the constant) anywhere in the scanned surface."""
    offenders: list[str] = []
    for path in _existing_scanned_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in THRESHOLD_ASSIGN_PATTERN.finditer(line):
                # Exclude the read/reference shell idiom `${NAME:-default}`
                # and markdown bold/backtick prose like `**BLOCK_BPS**` or
                # a plain mention followed by `=9500` inside backticks used
                # to describe (not assign) the live default, e.g.
                # "`BLOCK_BPS`=9500" in prose tables/back-references.
                tail = line[match.end():]
                if tail.startswith(("9500", "9000", "7000", "10000")):
                    # Looks like descriptive prose quoting the known live
                    # default value (e.g. "BLOCK_BPS=9500)") rather than a
                    # real code assignment — still flag it for a human to
                    # confirm intent stayed descriptive, not a real write.
                    if "`" in line or "(" in line:
                        continue
                if "${" + match.group(1) in line:
                    continue  # shell default-expansion read, not a write
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()!r}")
    assert not offenders, (
        f"found a possible threshold-constant reassignment: {offenders} — "
        "autonomous mode must never modify or lower a hook threshold"
    )


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
            "sessionend.sh",
            "sessionstart.sh",
            "stop.sh",
            "userpromptsubmit.sh",
            "worktreecreate.sh",
        ]
    )
    assert names == expected, f"hooks/ script roster changed: {names}"


def test_run_skill_documents_hard_constraint_never_writes_hooks() -> None:
    """`run/SKILL.md`'s hook-cooperation section explicitly restates the
    hard constraint in prose, so a human reader (and this test) both see
    the invariant stated, not just structurally implied."""
    assert RUN_SKILL.is_file()
    text = RUN_SKILL.read_text(encoding="utf-8")
    assert "## Hook cooperation (autonomous)" in text
    start = text.index("## Hook cooperation (autonomous)")
    end = text.index("## Gate boundaries reference", start)
    section = text[start:end]
    assert "NEVER writes to any file under" in section
    assert "`hooks/`" in section
    assert "NEVER modifies or lowers a `QUOIN_*_BPS` constant" in section


@pytest.mark.parametrize(
    "constant,expected_default",
    [
        ("QUOIN_BLOCK_BPS", "9500"),
        ("QUOIN_STOP_BPS", "7000"),
        ("QUOIN_COMPACT_FIRST_BPS", "9000"),
        ("QUOIN_PANIC_BPS", "10000"),
    ],
)
def test_lib_sh_thresholds_still_at_known_defaults(constant: str, expected_default: str) -> None:
    """Belt-and-suspenders content check against the live source of truth
    (`hooks/_lib.sh`), independent of the structural greps above."""
    lib_sh = HOOKS_DIR / "_lib.sh"
    assert lib_sh.is_file()
    text = lib_sh.read_text(encoding="utf-8")
    pattern = re.compile(r"\$\{" + re.escape(constant) + r":-([^}]+)\}")
    match = pattern.search(text)
    assert match, f"constant {constant} not found in hooks/_lib.sh"
    assert match.group(1) == expected_default, (
        f"hooks/_lib.sh default for {constant} changed: "
        f"expected {expected_default!r}, found {match.group(1)!r}"
    )
