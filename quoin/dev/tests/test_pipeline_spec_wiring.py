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


# ===========================================================================
# /gate — spec->architect document gate (MAJ-2)
# ===========================================================================


# ===========================================================================
# /architect + /thorough_plan — advisory "produce spec?" prompt
# ===========================================================================


# ===========================================================================
# /architect + /plan — bootstrap read of spec.md
# ===========================================================================


# ===========================================================================
# /review — Spec Compliance section + grandfather wording
# ===========================================================================


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
