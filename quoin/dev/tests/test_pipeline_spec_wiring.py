"""IVG-127 (specify-skill), stage 4: pipeline wiring — consumers + flow docs.

Six existing consumer skills (/run, /gate, /architect, /thorough_plan, /plan,
/review) are wired to read/offer/gate the task feature spec (spec.md), and
the canonical-flow docs (CLAUDE.md, core/workflow/rules.md) are updated to
show /specify in the pipeline. This is agent-interactive prose (like stage 3)
— we assert the ADAPTER SKILL.md text contract (structural contract over LLM
replay), mirroring test_repo_spec_lifecycle.py conventions. The adapter files
under adapters/claude/skills/<name>/SKILL.md are authoritative; legacy stubs
at skills/<name>/SKILL.md are deprecated pointers and are not read here.

GRANDFATHER OWNERSHIP NOTE (mirrors test_repo_spec_lifecycle.py): the
FUNCTIONAL grandfather invariant — "a task with no spec.md produces no
resolver/validator/type-detection error anywhere" — is owned by the Stage-1
suite: test_validate_artifact.py (type-detection/grandfather fixture cases)
and test_path_resolve.py (the resolver never enumerates filenames, so an
absent spec.md is simply never looked up). test_task_dir_with_no_spec_is_inert
below is a lightweight Stage-4-level re-confirmation only, not a substitute
for the Stage-1 functional guarantee.
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

ADAPTER_SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
RUN_SKILL_MD = ADAPTER_SKILLS / "run_hard" / "SKILL.md"
GATE_SKILL_MD = ADAPTER_SKILLS / "gate" / "SKILL.md"
ARCHITECT_SKILL_MD = ADAPTER_SKILLS / "architect" / "SKILL.md"
THOROUGH_PLAN_SKILL_MD = ADAPTER_SKILLS / "thorough_plan" / "SKILL.md"
PLAN_SKILL_MD = ADAPTER_SKILLS / "plan" / "SKILL.md"
REVIEW_SKILL_MD = ADAPTER_SKILLS / "review" / "SKILL.md"

CLAUDE_MD = REPO_ROOT / "quoin" / "CLAUDE.md"
RULES_MD = REPO_ROOT / "quoin" / "core" / "workflow" / "rules.md"

VALIDATOR = REPO_ROOT / "quoin" / "core" / "scripts" / "validate_artifact.py"
PATH_RESOLVE = REPO_ROOT / "quoin" / "core" / "scripts" / "path_resolve.py"
TEST_SIDECAR = REPO_ROOT / "quoin" / "dev" / "tests" / "fixtures" / "format-kit.sections.json"


def _read(path):
    return path.read_text(encoding="utf-8")


def _load_validator_module():
    spec = importlib.util.spec_from_file_location("_test_pipeline_spec_validator_core", VALIDATOR)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load validate_artifact.py from {VALIDATOR}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_validator(artifact_path, sections_json=TEST_SIDECAR):
    """Invoke validate_artifact.py via subprocess (CLI contract) against the
    test-fixture sidecar (content-identical to the source sidecar per T-12)."""
    cmd = [sys.executable, str(VALIDATOR), "--sections-json", str(sections_json), str(artifact_path)]
    result = subprocess.run(cmd, capture_output=True, cwd=str(REPO_ROOT))
    return result.returncode, result.stderr.decode("utf-8", errors="replace")


# ===========================================================================
# /run — Phase 1.5 / Checkpoint A0, additive (R-05 no-renumber guard)
# ===========================================================================

def test_run_skill_has_specify_phase_and_checkpoint_a0():
    text = _read(RUN_SKILL_MD)
    assert "Phase 1.5" in text
    assert "Checkpoint A0" in text
    assert "SPECIFY" in text or "Specify" in text
    assert "spec.md" in text


def test_run_skill_retains_existing_phase_and_checkpoint_tokens():
    """R-05: the Phase 1.5 insertion must be additive — Phases 2-6 and
    Checkpoints A-D keep their identifiers so resume/gate wiring is
    undisturbed. A renumber that dropped a sibling would fail this test."""
    text = _read(RUN_SKILL_MD)
    for token in ("Phase 2", "Phase 6", "Checkpoint A", "Checkpoint D"):
        assert token in text, f"expected retained token {token!r} missing from run/SKILL.md"


def test_run_skill_forwards_spec_path_downstream():
    text = _read(RUN_SKILL_MD)
    assert "spec.md" in text
    # Forwarded into at least the architect and thorough_plan spawn text.
    assert "if it exists" in text or "read-if-exists" in text


# ===========================================================================
# /gate — spec->architect document gate (MAJ-2)
# ===========================================================================

def test_gate_skill_has_specify_gate_block():
    text = _read(GATE_SKILL_MD)
    assert "After /specify" in text
    assert "spec.md" in text


def test_gate_skill_has_specify_architect_intensity_row():
    text = _read(GATE_SKILL_MD)
    assert "/specify" in text
    assert "/architect" in text
    # The MAJ-2 intensity-table row cells.
    assert "spec doc gate" in text


def test_gate_skill_has_specify_audit_phase_token():
    text = _read(GATE_SKILL_MD)
    assert "gate-specify" in text or "`specify`" in text


# ===========================================================================
# /architect + /thorough_plan — advisory "produce spec?" prompt
# ===========================================================================

def test_architect_has_advisory_spec_prompt_gated_on_medium_large():
    text = _read(ARCHITECT_SKILL_MD)
    assert "/specify" in text
    assert "advisory" in text.lower()
    assert "non-blocking" in text.lower() or "non blocking" in text.lower()
    assert "Medium or Large" in text or ("Medium" in text and "Large" in text)


def test_architect_advisory_has_small_skip_note():
    text = _read(ARCHITECT_SKILL_MD)
    assert "Small tasks skip" in text or "Small-task-skip" in text or "Small tasks skip this offer" in text


def test_thorough_plan_has_advisory_spec_prompt_gated_on_medium_large():
    text = _read(THOROUGH_PLAN_SKILL_MD)
    assert "/specify" in text
    assert "advisory" in text.lower()
    assert "non-blocking" in text.lower() or "non blocking" in text.lower()
    assert "Medium" in text and "Large" in text


def test_thorough_plan_advisory_has_small_skip_note():
    text = _read(THOROUGH_PLAN_SKILL_MD)
    assert "Small tasks skip" in text or "Small-task-skip" in text


def test_thorough_plan_forwards_spec_to_plan():
    text = _read(THOROUGH_PLAN_SKILL_MD)
    assert "spec.md" in text and "if it exists" in text


# ===========================================================================
# /architect + /plan — bootstrap read of spec.md
# ===========================================================================

def test_architect_reads_spec_at_bootstrap():
    text = _read(ARCHITECT_SKILL_MD)
    assert "spec.md" in text
    assert "if present" in text or "read-if-exists" in text


def test_plan_reads_spec_at_bootstrap():
    text = _read(PLAN_SKILL_MD)
    assert "spec.md" in text
    assert "if present" in text or "read-if-exists" in text


# ===========================================================================
# /review — Spec Compliance section + grandfather wording
# ===========================================================================

def test_review_has_spec_compliance_section():
    text = _read(REVIEW_SKILL_MD)
    assert "## Spec Compliance" in text


def test_review_has_grandfather_literal():
    text = _read(REVIEW_SKILL_MD)
    assert "No spec — verified against plan only." in text


def test_review_reads_spec_at_bootstrap():
    text = _read(REVIEW_SKILL_MD)
    assert "spec.md" in text
    assert "if present" in text or "read-if-exists" in text


# ===========================================================================
# Flow docs — CLAUDE.md canonical flow + per-skill enumeration; rules.md
# ===========================================================================

def test_claude_md_canonical_flow_includes_specify():
    text = _read(CLAUDE_MD)
    assert "/discover → /specify → GATE → /architect" in text


def test_claude_md_per_skill_enumeration_includes_specify():
    text = _read(CLAUDE_MD)
    assert "`/specify`" in text
    assert "task feature spec" in text


def test_rules_md_flow_string_includes_specify():
    text = _read(RULES_MD)
    assert "discover -> specify -> gate -> architect" in text


# ===========================================================================
# GRANDFATHER (AC-3): a task dir with no spec.md flows clean
# ===========================================================================

def test_task_dir_with_no_spec_is_inert():
    """Stage-4-level re-confirmation only (see module docstring) — the
    exhaustive absence-inert guarantee is owned by the Stage-1 suite."""
    validator_mod = _load_validator_module()

    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        task_dir = project_root / ".workflow_artifacts" / "no-spec-task"
        task_dir.mkdir(parents=True)
        plan_file = task_dir / "current-plan.md"
        plan_file.write_text("---\ntask: no-spec-task\n---\n## Tasks\nnone\n")
        # Deliberately NO spec.md anywhere in this tmp tree.

        # (a) path_resolve.py never errors on a task with no spec.md.
        resolve_cmd = [
            sys.executable, str(PATH_RESOLVE),
            "--task", "no-spec-task",
            "--project-root", str(project_root),
        ]
        resolve_result = subprocess.run(resolve_cmd, capture_output=True, cwd=str(REPO_ROOT))
        assert resolve_result.returncode == 0, (
            f"path_resolve.py failed on a spec-less task: "
            f"{resolve_result.stderr.decode('utf-8', errors='replace')}"
        )
        resolved_dir = resolve_result.stdout.decode("utf-8").strip()
        assert resolved_dir, "path_resolve.py printed no path"

        # (b) detect_type never routes an unrelated file to type 'spec'.
        detected = validator_mod.detect_type(str(plan_file), None)
        assert detected != "spec"

        # (c) validate_artifact.py raises no crash (exit 0 or 1, never 2+) over
        # this spec-less task dir.
        rc, stderr = run_validator(plan_file)
        assert rc in (0, 1), f"unexpected validator crash (rc={rc}): {stderr}"


# ===========================================================================
# Spec-Compliance-optional validation (proves T-12 registration + grandfather)
# ===========================================================================

_REVIEW_WITH_SPEC_COMPLIANCE = """---
task: fixture-task
review_round: 1
date: 2026-07-16
reviewer_model: claude-opus-4-7
branch: feat/fixture-branch
---
## For human

Implementation matches the plan and the task spec. Verdict: APPROVED.

## Summary

Reviewed the fixture-task implementation against both the plan and spec.md.

## Verdict

APPROVED

## Plan Compliance

Both plan tasks are fully implemented. No tasks skipped or deferred.

## Spec Compliance

All acceptance criteria in spec.md are satisfied by the implementation.

## Issues Found

No CRITICAL, MAJOR, or MINOR issues found.

## Integration Safety

No integration points affected.

## Test Coverage

Full coverage; all tests pass.

## Risk Assessment

| id | risk | status | notes |
|----|------|--------|-------|
| R-01 | none | n/a | n/a |
"""


def test_review_artifact_with_spec_compliance_validates():
    with tempfile.TemporaryDirectory() as tmpdir:
        artifact = Path(tmpdir) / "review-1.md"
        artifact.write_text(_REVIEW_WITH_SPEC_COMPLIANCE, encoding="utf-8")
        rc, stderr = run_validator(artifact)
        assert rc == 0, f"review WITH Spec Compliance failed to validate: {stderr}"


def test_review_artifact_without_spec_compliance_still_validates():
    """Grandfather: legacy review fixtures without the (optional) section
    must still pass — proves T-12 registered it as OPTIONAL, not required."""
    legacy_fixture = REPO_ROOT / "quoin" / "dev" / "tests" / "fixtures" / "review-v3-sample.md"
    assert "## Spec Compliance" not in _read(legacy_fixture), (
        "fixture drifted — expected this legacy sample to lack Spec Compliance "
        "for the grandfather check to be meaningful"
    )
    rc, stderr = run_validator(legacy_fixture)
    assert rc == 0, f"legacy review WITHOUT Spec Compliance failed to validate: {stderr}"
