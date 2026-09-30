"""T-07/T-08 (IVG-127, specify-skill stage 3): repo main spec lifecycle.

/init_workflow, /discover, and /specify are interactive — direct runtime
testing requires a Claude Code harness. We test the ADAPTER SKILL.md text
contract instead (per the 'structural contract over LLM replay' lesson,
mirroring test_init_workflow_legacy_quickstart.py and
test_discover_adapter_pilot.py). The adapter files under
adapters/claude/skills/<name>/SKILL.md are authoritative; legacy stubs at
skills/<name>/SKILL.md are deprecated pointers and are not read here.

GRANDFATHER OWNERSHIP NOTE (satisfies stage-3 plan MIN-3): the FUNCTIONAL
grandfather invariant — "a repo with no spec.md produces no resolver/
validator/type-detection error anywhere" — is owned by the Stage-1 test
suite: test_validate_artifact.py (type-detection/grandfather fixture cases)
and test_path_resolve.py (proves the resolver never enumerates filenames,
so an absent spec.md is simply never looked up). Stage 3 adds NO new code
path that reads the repo spec — every read here is agent-interactive prose
— so the tests below assert SKILL.md PROSE (static text-contract checks),
not the functional guard. They must not be mistaken for the Stage-1
functional guarantee; test_detect_type_and_validator_inert_when_spec_absent
below is a lightweight Stage-3-level re-confirmation only, not a substitute
for the Stage-1 suite.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

ADAPTER_SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
INIT_SKILL_MD = ADAPTER_SKILLS / "init_workflow" / "SKILL.md"
DISCOVER_SKILL_MD = ADAPTER_SKILLS / "discover" / "SKILL.md"
SPECIFY_SKILL_MD = ADAPTER_SKILLS / "specify" / "SKILL.md"

VALIDATOR = REPO_ROOT / "quoin" / "core" / "scripts" / "validate_artifact.py"
SIDECAR = REPO_ROOT / "quoin" / "memory" / "format-kit.sections.json"

MARKER_PATH = ".workflow_artifacts/.init-bootstrap-active"

REPO_SPEC_HEADINGS = [
    "## Context",
    "## Goals",
    "## Capabilities",
    "## Acceptance criteria",
    "## Non-goals",
]


def _read(path: Path) -> str:
    assert path.exists(), f"expected adapter SKILL.md at {path}"
    return path.read_text()


# ---------------------------------------------------------------------------
# Loader — mirrors test_checkpoint_ivg84_hash_and_tier.py (importlib, no
# dotted import — quoin/core/scripts is not a regular package).
# ---------------------------------------------------------------------------

def _load_validator_module():
    spec = importlib.util.spec_from_file_location("_test_repo_spec_validator_core", VALIDATOR)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load validate_artifact.py from {VALIDATOR}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_validator(artifact_path, sections_json=SIDECAR):
    """Invoke validate_artifact.py via subprocess (CLI contract, no --type
    override) so artifact-type detection runs through the real filename
    regex (basename ^spec)."""
    cmd = [sys.executable, str(VALIDATOR), "--sections-json", str(sections_json), str(artifact_path)]
    result = subprocess.run(cmd, capture_output=True, cwd=str(REPO_ROOT))
    return result.returncode, result.stderr.decode("utf-8", errors="replace")


# ===========================================================================
# T-07: init_workflow seeding presence assertions
# ===========================================================================

def test_init_workflow_seeding_prompt_present():
    text = _read(INIT_SKILL_MD)
    assert "What is this repo about?" in text


def test_init_workflow_references_repo_spec_path():
    text = _read(INIT_SKILL_MD)
    assert ".workflow_artifacts/spec.md" in text


def test_init_workflow_five_headings_present():
    text = _read(INIT_SKILL_MD)
    missing = [h for h in REPO_SPEC_HEADINGS if h not in text]
    assert not missing, f"init_workflow SKILL.md missing repo-spec headings: {missing}"


def test_init_workflow_idempotency_grandfather_skip_phrasing():
    text = _read(INIT_SKILL_MD)
    assert "Idempotency/grandfather gate FIRST" in text
    # absence-is-inert phrasing on the Skip branch
    assert "No file is created, no error" in text


def test_init_workflow_discover_handoff_suppression_note():
    text = _read(INIT_SKILL_MD)
    assert "MUST suppress its own repo-spec draft/refresh offer" in text
    assert "self-clear it after honoring" in text


# ===========================================================================
# T-07: discover offer presence assertions
# ===========================================================================

def test_discover_offer_heading_present():
    text = _read(DISCOVER_SKILL_MD)
    assert "Repo main spec (optional offer)" in text


def test_discover_suppression_guard_phrase():
    text = _read(DISCOVER_SKILL_MD)
    assert "Suppression guard FIRST" in text
    assert MARKER_PATH in text


def test_discover_self_clear_step_present():
    text = _read(DISCOVER_SKILL_MD)
    assert "DELETE the marker" in text
    assert f'fsops.py rm "{MARKER_PATH}"' in text


def test_discover_absent_draft_and_exists_refresh_diff_branches():
    text = _read(DISCOVER_SKILL_MD)
    assert "is ABSENT" in text and "DRAFT a repo main spec" in text
    assert "EXISTS" in text and "REFRESH it" in text
    assert "DIFF" in text


# ===========================================================================
# T-07: mechanism-token + recovery-token cross-assertion (round-1 MAJ-1,
# round-2 MAJ-1). This is the load-bearing test: it verifies the actual
# de-dup AND stale-recovery wiring (not just prose) by asserting the shared
# marker-path literal appears at every mechanism site in BOTH adapter
# SKILL.md files, and that discover's occurrence includes the RECOVERY
# (fsops.py rm / self-clear) step specifically — not merely a marker CHECK.
# ===========================================================================

def test_marker_token_occurrence_floor_init_workflow():
    text = _read(INIT_SKILL_MD)
    count = text.count(MARKER_PATH)
    # Three mechanism sites: Step-6 stale pre-clear, Step-6 write, Step-6.7 cleanup.
    assert count >= 3, (
        f"init_workflow SKILL.md must reference {MARKER_PATH} at least 3 times "
        f"(pre-clear + write + Step-6.7 cleanup sites); found {count}"
    )


def test_marker_token_occurrence_floor_discover():
    text = _read(DISCOVER_SKILL_MD)
    count = text.count(MARKER_PATH)
    # Two mechanism sites: suppression-guard CHECK, self-clear removal.
    assert count >= 2, (
        f"discover SKILL.md must reference {MARKER_PATH} at least 2 times "
        f"(check + self-clear sites); found {count}"
    )


def test_discover_recovery_token_present_not_check_only():
    """A marker-check-only discover (no self-clear) MUST fail this test.

    This asserts the RECOVERY token specifically: the literal
    `fsops.py rm ".workflow_artifacts/.init-bootstrap-active"` co-located with the
    marker path, proving discover actually consumes-and-clears the marker
    rather than merely checking for its presence.
    """
    text = _read(DISCOVER_SKILL_MD)
    recovery_token = f'fsops.py rm "{MARKER_PATH}"'
    assert recovery_token in text, (
        "discover SKILL.md is missing the marker self-clear (recovery) step — "
        f"expected literal '{recovery_token}'. A discover that only CHECKS the "
        "marker without clearing it would strand the marker forever after an "
        "aborted init bootstrap (round-2 MAJ-1) and must fail this test."
    )


# ===========================================================================
# T-08: specify gating-contract test (MIN-2)
# ===========================================================================

def test_specify_repo_spec_update_check_heading_present():
    text = _read(SPECIFY_SKILL_MD)
    assert "## Repo main spec update check" in text


def test_specify_gating_contract_phrases():
    text = _read(SPECIFY_SKILL_MD)
    # (a) grandfather no-op: absent -> skip, no error
    assert "is ABSENT, skip this check silently" in text
    assert "This is never an error." in text
    # (b) gated-diff phrasing: diff surfaced + approve/reject gate via AskUserQuestion
    assert "Surface a DIFF against the current" in text
    assert "AskUserQuestion" in text
    assert 'Approve' in text and 'Reject' in text
    # (c) never automatic
    assert "NEVER write automatically" in text or "never auto-writes" in text


def test_specify_old_out_of_scope_line_removed():
    text = _read(SPECIFY_SKILL_MD)
    assert "Repo-level main spec is out of scope" not in text


# ===========================================================================
# T-08: grandfather assertions (all three skills) — static text-contract
# check per the ownership note in the module docstring above.
# ===========================================================================

def test_grandfather_absence_no_error_init_workflow():
    text = _read(INIT_SKILL_MD)
    assert "No file is created, no error" in text


def test_grandfather_absence_no_error_discover():
    text = _read(DISCOVER_SKILL_MD)
    assert "no file, no error" in text


def test_grandfather_absence_no_error_specify():
    text = _read(SPECIFY_SKILL_MD)
    assert "This is never an error." in text


# ===========================================================================
# T-08: lightweight Stage-3 grandfather re-confirmation (functional guard
# is Stage-1-owned per the module docstring; this is a positive
# re-confirmation at the Stage-3 boundary, not the exhaustive guarantee).
# ===========================================================================

def test_detect_type_and_validator_inert_when_spec_absent():
    validator_mod = _load_validator_module()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        # No spec.md anywhere in this tmp dir.
        other_file = tmp_path / "current-plan.md"
        other_file.write_text("---\ntask: x\n---\n## Tasks\nnone\n")

        # detect_type must never route a non-spec-named file to type 'spec'.
        detected = validator_mod.detect_type(str(other_file), None)
        assert detected != "spec"

        # Running the validator against this file raises no error (exit 0 or 1,
        # never a crash/exit 2) — absence of spec.md anywhere is inert.
        rc, stderr = run_validator(other_file)
        assert rc in (0, 1), f"unexpected validator crash (rc={rc}): {stderr}"


# ===========================================================================
# T-08: repo-spec validator fixture (5 repo-variant headings)
# ===========================================================================

VALID_REPO_SPEC = """---
title: Example repo
scope: repo
date: 2026-07-16
status: draft
---
## Context
This repo does example things.

## Goals
- Goal one.

## Capabilities
- Capability one.

## Acceptance criteria
- Criterion one.

## Non-goals
- Non-goal one.
"""

REPO_SPEC_MISSING_ACCEPTANCE_CRITERIA = """---
title: Example repo
scope: repo
date: 2026-07-16
status: draft
---
## Context
This repo does example things.

## Goals
- Goal one.

## Capabilities
- Capability one.

## Non-goals
- Non-goal one.
"""


def test_repo_spec_fixture_validates_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text(VALID_REPO_SPEC)

        validator_mod = _load_validator_module()
        assert validator_mod.detect_type(str(spec_path), None) == "spec"

        rc, stderr = run_validator(spec_path)
        assert rc == 0, f"valid repo spec fixture failed validation: {stderr}"


def test_repo_spec_fixture_missing_acceptance_criteria_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        spec_path = Path(tmpdir) / "spec.md"
        spec_path.write_text(REPO_SPEC_MISSING_ACCEPTANCE_CRITERIA)

        rc, stderr = run_validator(spec_path)
        assert rc == 1
        # NOTE: the stage-3 plan's T-08 ack text says this case "FAILS V-02";
        # the actual validator implementation checks missing required
        # sections under V-07 (check_v07), not V-02 (V-02 is the *allowed*-
        # heading-set check, which this fixture does not violate — every
        # heading present is in the allowed set, it's just missing a
        # *required* one). Asserting the real invariant code here rather
        # than the plan's label; both assert "validation fails when
        # ## Acceptance criteria is missing", which is the load-bearing
        # behavior the ack criterion cares about.
        assert "FAIL V-07" in stderr
        assert "Acceptance criteria" in stderr
