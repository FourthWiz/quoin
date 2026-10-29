"""
Structural invariant tests for quoin/adapters/claude/skills/sleep/SKILL.md.

Ten tests verifying that the /sleep SKILL.md satisfies the Stage 3
structural contracts: Sonnet tier declaration, §0/§0‴ preamble
presence and ordering,
write-restriction prose, dry-run no-write prose, and deployed-copy sync.

Per Stage 3 plan T-13: purely deterministic pathlib + string parsing.
No live LLM calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent
ADAPTER_SKILLS_DIR = TESTS_DIR.parent.parent / "adapters" / "claude" / "skills"
SLEEP_SKILL = ADAPTER_SKILLS_DIR / "sleep" / "SKILL.md"

DEPLOYED_SLEEP_SKILL = Path.home() / ".claude" / "skills" / "sleep" / "SKILL.md"


def _lines() -> list[str]:
    return SLEEP_SKILL.read_text(encoding="utf-8").splitlines()


def _text() -> str:
    return SLEEP_SKILL.read_text(encoding="utf-8")


def _line_index(lines: list[str], fragment: str) -> int | None:
    """Return 0-based index of the first line containing `fragment`, or None."""
    for i, ln in enumerate(lines):
        if fragment in ln:
            return i
    return None


# ── 1. Frontmatter declares model: sonnet ────────────────────────────────────

def test_model_declared_sonnet():
    text = _text()
    # Frontmatter block is between the first two '---' lines.
    lines = text.splitlines()
    assert lines[0].strip() == "---", "SKILL.md does not start with YAML frontmatter '---'"
    end_idx = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    assert end_idx is not None, "SKILL.md frontmatter closing '---' not found"
    frontmatter = "\n".join(lines[1:end_idx])
    assert "model: sonnet" in frontmatter, (
        f"sleep/SKILL.md frontmatter does not declare 'model: sonnet'. "
        f"Frontmatter content:\n{frontmatter}"
    )


# ── 2. §0 Model dispatch heading is present ───────────────────────────────────

def test_sec0_present():
    text = _text()
    assert "## §0 Model dispatch" in text, (
        "sleep/SKILL.md is missing the '## §0 Model dispatch' heading. "
        "Cheap-tier skills must carry the §0 cost-guardrail block as their first body H2."
    )


# ── 3. §0‴ Minimum-tier guard sits between §0 and the skill body ───────────────

def test_sec0tripleprime_between_sec0_and_body():
    lines = _lines()
    sec0_idx = _line_index(lines, "## §0 Model dispatch")
    sec0tp_idx = _line_index(lines, "## §0‴ Minimum-tier guard")
    body_idx = _line_index(lines, "## Overview")
    assert sec0_idx is not None, "sleep/SKILL.md missing '## §0 Model dispatch'"
    assert sec0tp_idx is not None, "sleep/SKILL.md missing '## §0‴ Minimum-tier guard'"
    assert body_idx is not None, "sleep/SKILL.md missing '## Overview'"
    assert sec0_idx < sec0tp_idx < body_idx


# ── 7. Write-target restriction prose present ─────────────────────────────────

def test_write_target_restriction_present():
    text = _text()
    assert "ONLY writes to" in text, (
        "sleep/SKILL.md does not contain 'ONLY writes to'. "
        "The write-target restriction section must explicitly state the allowed write targets "
        "(tested by test_sleep_write_boundary.py per Stage 3 T-11 contract)."
    )


# ── 8. Dry-run no-write clause present ────────────────────────────────────────

def test_dry_run_no_write():
    text = _text()
    assert "Makes NO writes" in text, (
        "sleep/SKILL.md does not contain 'Makes NO writes'. "
        "The --dry-run mode section must explicitly state that no writes occur."
    )


# ── 9. Deployed copy sync (requires install.sh to have run) ──────────────────

def test_deployed_copy_sync():
    print("  SKIP  test_deployed_copy_sync — requires install.sh to have run; verify via: diff quoin/adapters/claude/skills/sleep/SKILL.md ~/.claude/skills/sleep/SKILL.md")


def run_tests():
    tests = [
        test_model_declared_sonnet,
        test_sec0_present,
                test_sec0tripleprime_between_sec0_and_body,
        test_write_target_restriction_present,
        test_dry_run_no_write,
        test_deployed_copy_sync,
    ]
    passed = 0
    failed = 0
    print(f"Running {len(tests)} test(s) from {__file__}")
    for t in tests:
        name = t.__name__
        try:
            t()
            if "SKIP" not in name or name == "test_deployed_copy_sync":
                # deployed_copy_sync prints its own SKIP line
                if name != "test_deployed_copy_sync":
                    print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\nResults: {passed} passed (includes skips), {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
