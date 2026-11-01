"""
End-to-end structural smoke tests for path_resolve.py integration.

Deterministic — no live LLM, no git history, no clock.
Verifies that:
  - CLAUDE.md documents the multi-stage convention + resolver
  - All SKILL.md files that reference planning-artifact hardcodes also wire path_resolve.py
  - No residual hardcoded <task-name>/... planning-artifact paths remain post-T-05
  - The resolver routes explicit integer stages correctly
  - The resolver grandfathers each known in-flight production task
  - install.sh deploys path_resolve.py
  - Multi-match raises ValueError with the expected message

Run from project root:
  python3 -m pytest quoin/dev/tests/test_path_resolve_e2e.py -v
"""

import glob
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # .../Claude_workflow
ADAPTER_SKILLS_DIR = PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills"
CLAUDE_MD = PROJECT_ROOT / "quoin" / "CLAUDE.md"
INSTALL_SH = PROJECT_ROOT / "quoin" / "install.sh"
RESOLVER = PROJECT_ROOT / "quoin" / "scripts" / "path_resolve.py"
WA = PROJECT_ROOT / ".workflow_artifacts"

# ---------------------------------------------------------------------------
# Case (a): CLAUDE.md has the Multi-stage tasks section
# ---------------------------------------------------------------------------


def test_claude_md_has_multi_stage_section():
    """CLAUDE.md must document the multi-stage convention with the resolver."""
    text = CLAUDE_MD.read_text()
    lines = text.splitlines()

    # Find the heading anywhere in the file (line-window constraint dropped — heading
    # position drifts as CLAUDE.md grows; presence + content assertions are sufficient)
    heading_line = None
    for i, line in enumerate(lines, start=1):
        if "### Multi-stage tasks" in line:
            heading_line = i
            break
    assert heading_line is not None, (
        f"### Multi-stage tasks heading not found in {CLAUDE_MD}. "
        f"T-03 must add this section."
    )

    # Extract section text (up to the next H2/H3)
    section_lines = []
    in_section = False
    for line in lines:
        if "### Multi-stage tasks" in line:
            in_section = True
        elif in_section and re.match(r"^#{2,3} ", line):
            break
        if in_section:
            section_lines.append(line)
    section_text = "\n".join(section_lines)

    assert "## Stage decomposition" in section_text, (
        "### Multi-stage tasks section must mention '## Stage decomposition'"
    )
    assert "path_resolve.py" in section_text, (
        "### Multi-stage tasks section must mention 'path_resolve.py'"
    )
    assert "Grandfathering" in section_text, (
        "### Multi-stage tasks section must mention 'Grandfathering'"
    )


# ---------------------------------------------------------------------------
# Case (b): Every SKILL.md that has Form-A hardcoded paths references path_resolve.py
#           + explicit Form-B/C allow-list check
# ---------------------------------------------------------------------------

# D-09a: Form-A alternation regex (9 files)
HARDCODED_RE = re.compile(
    r"\.workflow_artifacts/<task-name>/(current-plan|critic-response|review-|gate-)"
)

# D-09b: Explicit allow-list for 3 Form-B/C files whose references
# the Form-A alternation cannot match (round-5 CRIT-1).
EXPLICIT_FORM_B_C_FILES = [
    str(ADAPTER_SKILLS_DIR / "plan" / "SKILL.md"),
    str(ADAPTER_SKILLS_DIR / "gate" / "SKILL.md"),
    str(ADAPTER_SKILLS_DIR / "architect" / "SKILL.md"),
]


def test_skill_files_reference_resolver_dynamic_glob_plus_form_b_c_allow_list():
    """
    IF a SKILL.md contains a Form-A hardcoded path reference, THEN it MUST
    also reference path_resolve.py. Plus explicit Form-B/C allow-list.
    (round-3 MAJ-B dynamic-glob; round-5 CRIT-1 Form-B/C extension)
    """
    skill_files = sorted(glob.glob(str(ADAPTER_SKILLS_DIR / "*" / "SKILL.md")))
    assert len(skill_files) >= 11, (
        f"glob returned {len(skill_files)} SKILL.md files; expected >= 11 "
        f"(informational lower bound dated 2026-04-26 per D-07; actual count "
        f"is determined by the combined D-09a + D-09b audit-grep procedure). "
        f"Check that cwd / PROJECT_ROOT is correct: {PROJECT_ROOT}"
    )

    # Form-A positive assertion
    for path in skill_files:
        body = Path(path).read_text()
        if HARDCODED_RE.search(body):
            assert "path_resolve.py" in body, (
                f"{path} contains a Form-A hardcoded `<task-name>/current-plan.md` "
                f"(or critic-/review-/gate-) reference but does NOT reference "
                f"`path_resolve.py`. Per D-07 + D-09a, every SKILL.md with a Form-A "
                f"reference MUST resolve via the resolver. Add the per-file edit "
                f"template (T-05) to this file or remove the hardcoded path."
            )

    # Form-B/C explicit allow-list assertion (D-09b / round-5 CRIT-1)
    for path in EXPLICIT_FORM_B_C_FILES:
        assert path in skill_files, (
            f"D-09b allow-list entry {path} is not in the quoin/adapters/claude/skills/*/SKILL.md "
            f"glob result. Either the file was removed (update D-09b) or PROJECT_ROOT "
            f"is wrong: {PROJECT_ROOT}"
        )
        body = Path(path).read_text()
        skill_name = Path(path).parts[-2]
        row_map = {"plan": 2, "gate": 8, "architect": 10}
        assert "path_resolve.py" in body, (
            f"{path} is in the D-09b Form-B/C allow-list (round-5 CRIT-1 fix) but does "
            f"NOT reference `path_resolve.py`. Per D-09a + D-09b combined contract, this "
            f"file MUST resolve planning-artifact paths via the resolver. T-05 row "
            f"{row_map.get(skill_name, '?')} must land before this assertion passes."
        )


# ---------------------------------------------------------------------------
# Case (c): No residual hardcoded <task-name>/... planning-artifact paths remain
# ---------------------------------------------------------------------------

RESIDUAL_RE = re.compile(
    r"\.workflow_artifacts/<task-name>/(current-plan|critic-response|review-|gate-)"
)
GATE_LEGACY_RE = re.compile(r"task subfolder for artifacts")

# Per-file Form-B/C residual-prose canaries (D-09b / round-5 CRIT-1)
FORM_B_C_RESIDUAL_CANARIES = {
    str(ADAPTER_SKILLS_DIR / "plan" / "SKILL.md"):
        "any prior `current-plan.md`, `critic-response-*.md`",
    str(ADAPTER_SKILLS_DIR / "gate" / "SKILL.md"):
        "task subfolder for artifacts",
    str(ADAPTER_SKILLS_DIR / "architect" / "SKILL.md"):
        "Read the task subfolder if it exists (prior `architecture.md`, `current-plan.md`)",
}


def test_skill_files_have_no_residual_hardcoded_path_dynamic_glob_plus_form_b_c():
    """
    After T-05, no SKILL.md may contain residual Form-A or Form-B/C hardcoded
    planning-artifact paths.
    (round-3 MAJ-B dynamic-glob; round-5 CRIT-1 Form-B/C residual-prose check)
    """
    skill_files = sorted(glob.glob(str(ADAPTER_SKILLS_DIR / "*" / "SKILL.md")))
    assert len(skill_files) >= 11, (
        f"glob returned {len(skill_files)} SKILL.md files; expected >= 11. "
        f"Check PROJECT_ROOT: {PROJECT_ROOT}"
    )

    # Form-A residual sweep
    violations = []
    for path in skill_files:
        body = Path(path).read_text()
        for m in RESIDUAL_RE.finditer(body):
            violations.append((path, m.group(0), m.start()))
        # round-2 C3 + round-3 MAJ-A: gate/SKILL.md legacy prose
        if path.endswith("/gate/SKILL.md"):
            assert not GATE_LEGACY_RE.search(body), (
                f"{path} still contains the round-1 line-116 prose `task subfolder "
                f"for artifacts`. T-05 row 8 (round-3 MAJ-A) requires replacing this "
                f"line with the verbatim rewrite block from the plan."
            )
    assert violations == [], (
        f"Residual Form-A hardcoded `<task-name>/(current-plan|critic-response|review-|gate-)` in:\n"
        + "\n".join(f"  {p}: matched `{m}` at offset {o}" for p, m, o in violations)
    )

    # Form-B/C residual-prose check (D-09b / round-5 CRIT-1)
    form_b_c_violations = []
    for path, canary in FORM_B_C_RESIDUAL_CANARIES.items():
        assert Path(path).is_file(), (
            f"D-09b residual-canary entry {path} not found on disk; either "
            f"the file was removed/moved (update FORM_B_C_RESIDUAL_CANARIES) or PROJECT_ROOT "
            f"is wrong: {PROJECT_ROOT}"
        )
        body = Path(path).read_text()
        if canary in body:
            form_b_c_violations.append((path, canary))
    assert form_b_c_violations == [], (
        f"Form-B/C residual round-1 prose still present after T-05 in:\n"
        + "\n".join(f"  {p}: still contains `{c}`" for p, c in form_b_c_violations)
        + "\nT-05 must replace these with resolver-wiring per the per-file edit template; "
          "see Decisions D-09b for the per-file anchor rationale."
    )


# ---------------------------------------------------------------------------
# Case (d): Resolver routes explicit integer stage correctly
# ---------------------------------------------------------------------------


def test_resolver_routes_explicit_stage_canonical():
    """path_resolve.py --stage 3 returns <root>/.workflow_artifacts/<task>/stage-3."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        task_dir = tmp_path / ".workflow_artifacts" / "fixture-task"
        (task_dir / "stage-3").mkdir(parents=True)

        result = subprocess.run(
            [
                sys.executable,
                str(RESOLVER),
                "--task", "fixture-task",
                "--stage", "3",
                "--project-root", str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"path_resolve.py exited {result.returncode}; stderr: {result.stderr!r}"
        )
        # Use Path.resolve() on both sides to normalize macOS /var → /private/var symlinks
        resolved = Path(result.stdout.strip()).resolve()
        expected = (tmp_path / ".workflow_artifacts" / "fixture-task" / "stage-3").resolve()
        assert resolved == expected, (
            f"Expected {expected!r}, got {resolved!r}"
        )


# ---------------------------------------------------------------------------
# Case (e): Resolver grandfathers each real in-flight task (hard-assert, no silent skip)
# ---------------------------------------------------------------------------

INFLIGHT_SNAPSHOT = (
    PROJECT_ROOT
    / "quoin"
    / "dev"
    / "tests"
    / "fixtures"
    / "path_resolve"
    / "_inflight-snapshot.txt"
)

# Import the resolver module directly for Python-API calls
sys.path.insert(0, str(PROJECT_ROOT / "quoin" / "scripts"))
from path_resolve import task_path  # noqa: E402


def _parse_snapshot():
    """
    Returns list of (task_name, arch_state, plan_state, stage_subfolder) tuples.
    arch_state: 'absent' | 'present-without-decomp' | 'present-with-decomp'
    stage_subfolder: folder name like 'stage-5' or '(none)'
    """
    rows = []
    for line in INFLIGHT_SNAPSHOT.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 4:
            rows.append(tuple(parts))
    return rows


@pytest.mark.parametrize("snapshot_row", _parse_snapshot())
def test_resolver_grandfathers_each_inflight_task_per_production_shape(snapshot_row):
    """
    For each real in-flight task folder, assert that task_path(task_name)
    returns the task root (Rule 3 — grandfathering).
    FAILS LOUDLY if the folder doesn't exist — that's the canary (round-2 MAJ-2).
    """
    task_name, arch_state, plan_state, stage_subfolder = snapshot_row

    task_root = WA / task_name
    assert task_root.exists(), (
        f"Expected in-flight task {task_name!r} at {task_root} but not found. "
        f"If this task was finalized via /end_of_task, REMOVE this assertion from "
        f"test_resolver_grandfathers_each_inflight_task_per_production_shape AND "
        f"remove the corresponding row from _inflight-snapshot.txt. "
        f"Do NOT silently skip."
    )

    resolved = task_path(task_name, stage=None, project_root=PROJECT_ROOT)
    assert resolved == task_root, (
        f"task_path({task_name!r}) returned {resolved!r} but expected {task_root!r} "
        f"(Rule 3 grandfathering — stage=None should route to task root)"
    )

    # Production-shape fingerprint assertions per snapshot arch_state
    arch_md = task_root / "architecture.md"
    plan_md = task_root / "current-plan.md"

    assert plan_md.exists(), (
        f"In-flight task {task_name!r}: expected root-level current-plan.md at {plan_md}"
    )

    if arch_state == "absent":
        assert not arch_md.exists(), (
            f"Snapshot says architecture.md is absent for {task_name!r} but {arch_md} exists. "
            f"Update _inflight-snapshot.txt."
        )
        if stage_subfolder != "(none)":
            assert (task_root / stage_subfolder).exists(), (
                f"Snapshot says {stage_subfolder} subfolder exists for {task_name!r} "
                f"but {task_root / stage_subfolder} not found. Update _inflight-snapshot.txt."
            )

    elif arch_state == "present-without-decomp":
        assert arch_md.exists(), (
            f"Snapshot says architecture.md is present for {task_name!r} but {arch_md} missing."
        )
        decomp_count = int(subprocess.run(
            ["grep", "-c", "## Stage decomposition", str(arch_md)],
            capture_output=True, text=True,
        ).stdout.strip() or "0")
        assert decomp_count == 0, (
            f"Snapshot says {task_name!r} architecture.md has NO Stage decomposition, "
            f"but grep found {decomp_count} occurrence(s). Update _inflight-snapshot.txt."
        )

    elif arch_state == "present-with-decomp":
        assert arch_md.exists(), (
            f"Snapshot says architecture.md is present for {task_name!r} but {arch_md} missing."
        )
        decomp_count = int(subprocess.run(
            ["grep", "-c", "## Stage decomposition", str(arch_md)],
            capture_output=True, text=True,
        ).stdout.strip() or "0")
        assert decomp_count >= 1, (
            f"Snapshot says {task_name!r} architecture.md HAS Stage decomposition, "
            f"but grep found {decomp_count} occurrence(s). Update _inflight-snapshot.txt."
        )


# ---------------------------------------------------------------------------
# Case (f): install.sh deploys path_resolve.py
# ---------------------------------------------------------------------------


def test_install_sh_lists_path_resolve():
    """installer.py DEPLOYED_SCRIPTS must include path_resolve.py."""
    import sys as _sys
    _sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from quoin import installer  # noqa: PLC0415

    assert "path_resolve.py" in installer.DEPLOYED_SCRIPTS, (
        "installer.DEPLOYED_SCRIPTS does not contain 'path_resolve.py'. "
        "The Python installer (which replaced the install.sh for-loop) must include "
        "path_resolve.py in its DEPLOYED_SCRIPTS constant."
    )


# ---------------------------------------------------------------------------
# Case (g): Multi-match raises ValueError
# ---------------------------------------------------------------------------


def test_resolver_multi_match_raises():
    """
    When two stage descriptions share a search substring, task_path raises
    ValueError with 'matches 2 stages' and 'disambiguate by using --stage <integer>'.
    Also verified via CLI (exit 2 + same message in stderr).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        task_dir = tmp_path / ".workflow_artifacts" / "synthetic"
        task_dir.mkdir(parents=True)

        # architecture.md with two stage descriptions sharing "data" substring.
        # Use the numbered-list format that ROW_RE matches:
        # r"^[0-9]+\.\s+(?:[✅✓✗⏳⛔⚠️\s])*S-([0-9]+):\s*(.+?)\s*$"
        arch_text = """# Architecture

## Stage decomposition

1. S-01: data-migration — migrate data to new schema
2. S-02: data-cleanup — clean up old data tables
"""
        (task_dir / "architecture.md").write_text(arch_text)
        (task_dir / "stage-1").mkdir()
        (task_dir / "stage-2").mkdir()

        # Python API: should raise ValueError
        import importlib
        import path_resolve as pr_module
        importlib.reload(pr_module)

        with pytest.raises(ValueError) as exc_info:
            pr_module.task_path("synthetic", stage="data", project_root=tmp_path)

        msg = str(exc_info.value)
        assert "matches 2 stages" in msg, (
            f"ValueError message should contain 'matches 2 stages', got: {msg!r}"
        )
        assert "disambiguate by using --stage <integer>" in msg, (
            f"ValueError message should contain 'disambiguate by using --stage <integer>', got: {msg!r}"
        )

        # CLI form: should exit 2 with same message in stderr
        result = subprocess.run(
            [
                sys.executable,
                str(RESOLVER),
                "--task", "synthetic",
                "--stage", "data",
                "--project-root", str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"CLI should exit 2 for multi-match, got {result.returncode}; "
            f"stderr: {result.stderr!r}"
        )
        assert "matches 2 stages" in result.stderr, (
            f"CLI stderr should contain 'matches 2 stages', got: {result.stderr!r}"
        )
        assert "disambiguate by using --stage <integer>" in result.stderr, (
            f"CLI stderr should contain 'disambiguate by using --stage <integer>', "
            f"got: {result.stderr!r}"
        )
