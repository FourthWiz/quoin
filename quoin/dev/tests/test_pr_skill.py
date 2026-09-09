"""
Structural validation tests for the /pr skill.

Verifies that the adapter SKILL.md, stub SKILL.md, and installer.py are
all consistently configured for the new /pr skill (IVG-53).

Per Stage 1 plan D-03: no live LLM calls — deterministic pathlib + string + YAML parsing only.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).parent
QUOIN_DIR = TESTS_DIR.parent.parent
REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_SKILLS_DIR = QUOIN_DIR / "adapters" / "claude" / "skills"
STUB_SKILLS_DIR = QUOIN_DIR / "skills"
CORE_SKILLS_DIR = QUOIN_DIR / "core" / "skills"
CLAUDE_MD = QUOIN_DIR / "CLAUDE.md"
INSTALLER_PY = REPO_ROOT / "src" / "quoin" / "installer.py"

PR_ADAPTER_SKILL = ADAPTER_SKILLS_DIR / "pr" / "SKILL.md"
PR_STUB_SKILL = STUB_SKILLS_DIR / "pr" / "SKILL.md"
PR_CORE_DOC = CORE_SKILLS_DIR / "pr.md"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a SKILL.md file."""
    text = _read(path)
    if not text.startswith("---"):
        return {}
    end = text.index("---", 3)
    return yaml.safe_load(text[3:end])


# ── T-01: Core skill doc ─────────────────────────────────────────────────────

def test_core_doc_exists():
    assert PR_CORE_DOC.exists(), "quoin/core/skills/pr.md must exist"


def test_core_doc_has_purpose_section():
    text = _read(PR_CORE_DOC)
    assert "## Purpose" in text, "core doc must have ## Purpose section"


def test_core_doc_has_contract_section():
    text = _read(PR_CORE_DOC)
    assert "## Contract" in text, "core doc must have ## Contract section"


# ── T-02: Stub SKILL.md ───────────────────────────────────────────────────────

def test_stub_exists():
    assert PR_STUB_SKILL.exists(), "quoin/skills/pr/SKILL.md must exist"


def test_stub_frontmatter_name():
    fm = _frontmatter(PR_STUB_SKILL)
    assert fm.get("name") == "pr", "stub frontmatter must have name: pr"


def test_stub_frontmatter_model():
    fm = _frontmatter(PR_STUB_SKILL)
    assert fm.get("model") == "sonnet", "stub frontmatter must have model: sonnet"


def test_stub_no_deprecated_markers():
    text = _read(PR_STUB_SKILL)
    assert "DEPRECATED LOCATION" not in text, "stub must not contain 'DEPRECATED LOCATION'"
    assert "deprecated stub" not in text, "stub must not contain 'deprecated stub'"


# ── T-03: Adapter SKILL.md ───────────────────────────────────────────────────

def test_adapter_exists():
    assert PR_ADAPTER_SKILL.exists(), "quoin/adapters/claude/skills/pr/SKILL.md must exist"


def test_adapter_frontmatter_name():
    fm = _frontmatter(PR_ADAPTER_SKILL)
    assert fm.get("name") == "pr", "adapter frontmatter must have name: pr"


def test_adapter_frontmatter_model():
    fm = _frontmatter(PR_ADAPTER_SKILL)
    assert fm.get("model") == "sonnet", "adapter frontmatter must have model: sonnet"


def test_adapter_frontmatter_description_nonempty():
    fm = _frontmatter(PR_ADAPTER_SKILL)
    assert fm.get("description"), "adapter frontmatter must have a non-empty description"


def test_adapter_s0_heading_present():
    text = _read(PR_ADAPTER_SKILL)
    heading = "## §0 Model dispatch (FIRST STEP — execute before anything else)"
    assert heading in text, "adapter must contain the §0 dispatch heading"


def test_adapter_s0_heading_unique():
    text = _read(PR_ADAPTER_SKILL)
    heading = "## §0 Model dispatch (FIRST STEP — execute before anything else)"
    assert text.count(heading) == 1, "§0 dispatch heading must appear exactly once"


def test_adapter_s0_model_sonnet():
    text = _read(PR_ADAPTER_SKILL)
    assert 'model: "sonnet"' in text, "§0 block must contain: model: \"sonnet\""


def test_adapter_s0_dispatched_tier():
    text = _read(PR_ADAPTER_SKILL)
    assert "dispatched-tier: sonnet" in text, "§0 block must contain: dispatched-tier: sonnet"


def test_adapter_s0_sidecar_present():
    text = _read(PR_ADAPTER_SKILL)
    assert "<!-- §0-sidecar-begin -->" in text, "adapter must contain §0-sidecar-begin comment"


def test_adapter_recursion_tokens():
    text = _read(PR_ADAPTER_SKILL)
    tokens = [
        "[no-redispatch]",
        "[no-redispatch:N]",
        "Quoin self-dispatch hard-cap reached at N=",
        "[quoin-stage-1: subagent dispatch unavailable;",
    ]
    for token in tokens:
        assert token in text, f"adapter must contain recursion token: {token!r}"


def test_adapter_s0b_intentionally_omitted():
    text = _read(PR_ADAPTER_SKILL)
    assert "§0b: intentionally omitted" in text, (
        "adapter must have comment explaining §0b was intentionally omitted"
    )


def test_adapter_branch_safety_check():
    text = _read(PR_ADAPTER_SKILL)
    assert "main/master" in text or ("main" in text and "master" in text), (
        "adapter must check that branch is not main/master"
    )


def test_adapter_already_pushed_check():
    text = _read(PR_ADAPTER_SKILL)
    assert "ls-remote" in text or "already_pushed" in text, (
        "adapter must check whether branch is already pushed"
    )


def test_adapter_version_file_detection():
    text = _read(PR_ADAPTER_SKILL)
    assert "pyproject.toml" in text, "adapter must mention pyproject.toml for version detection"
    assert "package.json" in text, "adapter must mention package.json for version detection"


def test_adapter_wait_for_merge():
    text = _read(PR_ADAPTER_SKILL)
    assert "merge" in text.lower(), "adapter must include wait-for-merge step"


# ── T-04: installer.py registration ─────────────────────────────────────────

def test_installer_canonical_skills_has_pr():
    text = _read(INSTALLER_PY)
    # Find the CANONICAL_SKILLS tuple and verify "pr" is in it
    assert '"pr"' in text, 'installer.py must contain "pr" in CANONICAL_SKILLS'


def test_installer_canonical_skills_order():
    """'pr' must appear between 'plan' and 'review' in CANONICAL_SKILLS."""
    text = _read(INSTALLER_PY)
    plan_pos = text.find('"plan"')
    pr_pos = text.find('"pr"')
    review_pos = text.find('"review"')
    assert plan_pos < pr_pos < review_pos, (
        '"pr" must appear between "plan" and "review" in CANONICAL_SKILLS'
    )


def test_installer_skill_overrides_pr_name_only():
    text = _read(INSTALLER_PY)
    assert '"pr": "name-only"' in text, (
        'SKILL_OVERRIDES must contain "pr": "name-only"'
    )


# ── T-05: CLAUDE.md phase value ───────────────────────────────────────────────

def test_claude_md_phase_value_pr():
    text = _read(CLAUDE_MD)
    assert "`pr`" in text or '"pr"' in text, (
        "CLAUDE.md must include 'pr' in the Phase values list"
    )


def test_claude_md_pr_in_model_assignments():
    text = _read(CLAUDE_MD)
    assert "| /pr |" in text, "CLAUDE.md model assignments table must include /pr row"


def test_claude_md_git_safety_references_pr():
    text = _read(CLAUDE_MD)
    assert "/pr" in text, "CLAUDE.md Git & PR Safety section must reference /pr"


# ── Comment-cleanup pre-flight wiring ──────────────────────────────────────────

def test_check_0_resolves_both_base_namespaces():
    text = _read(PR_ADAPTER_SKILL)
    assert "base_name" in text and "base_ref" in text, (
        "check 0 must resolve both base_name and base_ref"
    )
    assert text.index("Check 0") < text.index("Check 1"), (
        "base resolution must be check 0, ahead of the branch check"
    )


def test_gh_pr_create_uses_base_name():
    text = _read(PR_ADAPTER_SKILL)
    assert "--base <base_name>" in text, "gh pr create must consume base_name, not base_ref"


def test_step_6_checkout_uses_base_name():
    text = _read(PR_ADAPTER_SKILL)
    step6 = text[text.index("### Step 6"):]
    assert "base_name" in step6.split("### Step 6", 1)[-1][:400]


def test_cleanup_check_precedes_uncommitted_check():
    text = _read(PR_ADAPTER_SKILL)
    cleanup_idx = text.index("Check 4 — comment cleanup")
    uncommitted_idx = text.index("Check 5 — uncommitted changes check")
    assert cleanup_idx < uncommitted_idx


def test_step_3_push_condition_names_cleanup_committed():
    text = _read(PR_ADAPTER_SKILL)
    step3 = text[text.index("### Step 3"):text.index("### Step 4")]
    assert "cleanup_committed" in step3


def test_core_doc_contract_mentions_cleanup():
    text = _read(PR_CORE_DOC)
    contract = text[text.index("## Contract"):]
    assert "comment" in contract.lower() and "cleanup" in contract.lower()


def test_core_doc_preconditions_updated():
    text = _read(PR_CORE_DOC)
    preconditions = text[text.index("## Preconditions"):text.index("## Contract")]
    assert "cleanup" in preconditions.lower()


def test_comment_cleanup_invocations_are_fully_qualified():
    """Every mention of comment_cleanup.py in the adapter must be reachable
    on PATH — ~/.claude/scripts/ is not on PATH, so a bare filename silently
    no-ops the whole feature (the "missing script" branch fires and check 4
    quietly does nothing on every /pr)."""
    text = _read(PR_ADAPTER_SKILL)
    prefix = "python3 __QUOIN_HOME__/scripts/comment_cleanup.py"
    idx = 0
    mentions = 0
    while True:
        idx = text.find("comment_cleanup.py", idx)
        if idx == -1:
            break
        mentions += 1
        start = idx - len(prefix) + len("comment_cleanup.py")
        assert text[start:idx + len("comment_cleanup.py")] == prefix, (
            f"unqualified comment_cleanup.py mention at offset {idx}"
        )
        idx += len("comment_cleanup.py")
    assert mentions >= 3, "expected at least the 4b/4c/4e invocations"


def test_cleanup_pathspec_set_is_git_status_porcelain():
    text = _read(PR_ADAPTER_SKILL)
    check4 = text[text.index("Check 4"):text.index("Check 5")]
    assert "git status --porcelain" in check4, (
        "check 4's pathspec set must be defined as git status --porcelain output"
    )
    # The failure path must reuse the same set, not a script-report-derived
    # subset that omits the judgment pass's own edits. Normalize whitespace
    # first since the prose wraps mid-phrase.
    normalized = " ".join(check4.split())
    assert "same pathspecs" in normalized or "same pathspec set" in normalized, (
        "the failure-path checkout must reuse the same pathspec set defined above"
    )


def test_cleanup_pathspec_set_excludes_untracked_entries():
    text = _read(PR_ADAPTER_SKILL)
    check4 = text[text.index("Check 4"):text.index("Check 5")]
    normalized = " ".join(check4.split())
    assert "excluding `??`" in normalized or "excludes `??`" in normalized, (
        "check 4's pathspec set must explicitly exclude untracked (??) entries"
    )
    assert "git clean -f --" in normalized, (
        "the failure path must clean up leftover untracked residue, not abort on it"
    )
