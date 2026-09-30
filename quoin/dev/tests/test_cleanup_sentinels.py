#!/usr/bin/env python3
"""
test_cleanup_sentinels.py — contract tests for /cleanup sentinel allow-list.

Two-layer test (per plan T-08 / D-04 / D-06):

Layer 1 — SKILL.md contract (scope-bounded grep, section-extracted):
  Open quoin/skills/cleanup/SKILL.md. Extract the
  '## Hardcoded sentinel allow-list' section. Assert:
    (a) All 9 sentinel-family literals appear in the extracted section.
    (b) 'hardcoded' allow-list language is present in the section.
    (c) The section mentions 'sentinel_globs' (shared-source pointer — drift guard).

Layer 2 — negative safety test:
  Scope-bounded to the same extracted allow-list section. Assert:
    (a) 'hardcoded' (or 'hardcoded allow-list') is present (drift guard).
    (b) No catch-all glob ('*.txt' or 'pending-*.txt' alone) is the sweep target.
    (c) 'lessons-learned.md' and 'forgotten/' do NOT appear as sweep targets.
    (d) 'trash_move' appears in the allow-list/sweep section; a hard delete ('rm -f' or 'fsops.py rm') does NOT.
    (e) 'find' lines in the section target named family globs, not bare '*.txt'.

Both layers must pass. Exit 0 on success, non-zero on failure.

Runnable with:
  python3 quoin/dev/tests/test_cleanup_sentinels.py
  python3 -m pytest quoin/dev/tests/test_cleanup_sentinels.py
"""

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # quoin/ git repo root
_SKILL_MD = _PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills" / "cleanup" / "SKILL.md"


def _has_hard_delete(text: str) -> bool:
    """True if the text contains a permanent-delete form (shell rm -f or the fsops helper)."""
    return "rm -f" in text or "fsops.py rm" in text


# Synthetic check: the predicate must catch the helper form too.
assert _has_hard_delete('python3 fsops.py rm "x"')
assert not _has_hard_delete("trash_move x")

def _extract_cleanup_section(content: str) -> str:
    """Extract the body of the ## Hardcoded sentinel allow-list section (scope-bounded)."""
    lines = content.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if re.match(r"^## Hardcoded sentinel allow-list", line):
            in_section = True
            section_lines.append(line)
            continue
        if in_section:
            # Stop at the next H2 that is NOT a sub-section (###) of this section
            if re.match(r"^## ", line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def _extract_core_procedure_section(content: str) -> str:
    """Extract the ## Core procedure section for find/sweep assertions."""
    lines = content.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if re.match(r"^## Core procedure", line):
            in_section = True
            section_lines.append(line)
            continue
        if in_section:
            if re.match(r"^## ", line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)


def _pass(message: str) -> None:
    print(f"PASS: {message}")


def _run_layer1_skill_md_contract() -> bool:
    """Layer 1: scope-bounded grep for all 9 sentinel family literals in allow-list section."""
    print("\n--- Layer 1: SKILL.md contract (scope-bounded grep inside ## Hardcoded sentinel allow-list) ---")

    if not _SKILL_MD.exists():
        _fail(f"cleanup/SKILL.md not found at {_SKILL_MD}")
        return False

    content = _SKILL_MD.read_text(encoding="utf-8")
    section = _extract_cleanup_section(content)

    if not section:
        _fail("Could not extract ## Hardcoded sentinel allow-list section from cleanup/SKILL.md")
        return False

    failures = 0

    # (a) All 9 sentinel families must appear in the allow-list section
    sentinel_families = [
        "pending-restore-*.txt",
        "pending-prompt-*.txt",
        "compact-happened-*.txt",
        "mid-agent-handoff-*.txt",
        "pending-resume-ref-*.txt",
        "checkpoint-defer-*.txt",
        "postcompact-reset-*.txt",
        "checkpoint-pending-compact-*.txt",
        "idle-advisory-pending-*.txt",
    ]

    for family in sentinel_families:
        if family in section:
            _pass(f"Layer 1a: family '{family}' found in ## Hardcoded sentinel allow-list section")
        else:
            _fail(
                f"Layer 1a: family '{family}' NOT found in ## Hardcoded sentinel allow-list section "
                "of cleanup/SKILL.md"
            )
            failures += 1

    # (b) 'hardcoded' must appear in the section
    if "hardcoded" in section.lower():
        _pass("Layer 1b: 'hardcoded' allow-list language present in allow-list section")
    else:
        _fail(
            "Layer 1b: 'hardcoded' language NOT found in ## Hardcoded sentinel allow-list section — "
            "the family list must be documented as hardcoded to prevent drift"
        )
        failures += 1

    # (c) 'sentinel_globs' shared-source pointer must appear in the allow-list section
    if "sentinel_globs" in section:
        _pass("Layer 1c: 'sentinel_globs' shared-source pointer present in allow-list section (drift guard)")
    else:
        _fail(
            "Layer 1c: 'sentinel_globs' NOT found in ## Hardcoded sentinel allow-list section — "
            "the section must include the canonical machine-readable source pointer "
            "(hooks/_lib.sh:sentinel_globs()) to prevent silent drift"
        )
        failures += 1

    return failures == 0


def _run_layer2_negative_safety() -> bool:
    """Layer 2: negative safety test — no catch-all globs, no forbidden targets, trash vs rm."""
    print("\n--- Layer 2: negative safety test (scope-bounded, allow-list section) ---")

    if not _SKILL_MD.exists():
        _fail(f"cleanup/SKILL.md not found at {_SKILL_MD}")
        return False

    content = _SKILL_MD.read_text(encoding="utf-8")
    section = _extract_cleanup_section(content)
    core_section = _extract_core_procedure_section(content)

    # Combined scope: allow-list section + core procedure (where find/trash_move calls live)
    combined = section + "\n" + core_section

    failures = 0

    # (a) 'hardcoded' language present (drift guard)
    if "hardcoded" in section.lower():
        _pass("Layer 2a: 'hardcoded' allow-list language present (drift guard)")
    else:
        _fail(
            "Layer 2a: 'hardcoded' allow-list language absent in allow-list section — "
            "the family list must state it is a hardcoded allow-list"
        )
        failures += 1

    # (b) No catch-all glob as sweep iteration target
    # '*.txt' as standalone grep target (not inside a named family glob) is forbidden
    # Named family globs like 'pending-restore-*.txt' are fine.
    # Check that '*.txt' doesn't appear as a bare find -name pattern (outside of a named family)
    bare_wildcard_in_find = re.search(r"-name\s+['\"]?\*\.txt['\"]?", combined)
    if bare_wildcard_in_find:
        _fail(
            "Layer 2b: bare '*.txt' catch-all appears as -name pattern in sweep. "
            "Only named family globs (e.g., 'pending-restore-*.txt') are permitted."
        )
        failures += 1
    else:
        _pass("Layer 2b: no bare '*.txt' catch-all glob as sweep iteration target")

    # (c) 'lessons-learned.md' not listed as a sentinel family (co-occurrence check)
    # It's fine to mention it in "NEVER targets lessons-learned.md" prose — we only
    # care that it's not named alongside sentinel family patterns as a sweep target.
    family_list_pattern = re.compile(
        r"(pending-restore|pending-prompt|compact-happened|mid-agent-handoff|"
        r"pending-resume-ref|checkpoint-defer|postcompact-reset|checkpoint-pending-compact)"
    )
    lessons_in_family_context = any(
        "lessons-learned" in line and family_list_pattern.search(line)
        for line in section.splitlines()
    )
    if not lessons_in_family_context:
        _pass(
            "Layer 2c: 'lessons-learned.md' NOT listed alongside sentinel families in allow-list section"
        )
    else:
        _fail(
            "Layer 2c: 'lessons-learned.md' appears in sentinel family context in allow-list section — "
            "it must not be listed as a sweep target"
        )
        failures += 1

    # (d) 'forgotten/' not listed as a sentinel family target (co-occurrence check)
    forgotten_in_family_context = any(
        "forgotten/" in line and family_list_pattern.search(line)
        for line in section.splitlines()
    )
    if not forgotten_in_family_context:
        _pass(
            "Layer 2d: 'forgotten/' NOT listed alongside sentinel families in allow-list section"
        )
    else:
        _fail(
            "Layer 2d: 'forgotten/' appears in sentinel family context in allow-list section — "
            "it must not be listed as a sweep target"
        )
        failures += 1

    # (d) 'trash_move' appears; 'rm -f' does NOT (trash-vs-rm distinction, D-06)
    if "trash_move" in combined:
        _pass("Layer 2e: 'trash_move' present in sweep/allow-list section (correct delete primitive)")
    else:
        _fail(
            "Layer 2e: 'trash_move' NOT found in sweep section — "
            "/cleanup must use trash_move (recoverable), not rm -f"
        )
        failures += 1

    if not _has_hard_delete(combined):
        _pass("Layer 2f: 'rm -f' NOT present in sweep section (trash-move vs hard-delete distinction)")
    else:
        _fail(
            "Layer 2f: hard delete ('rm -f' or 'fsops.py rm') found in sweep section — "
            "/cleanup must use trash_move, not rm. "
            "/sleep --purge uses rm; /cleanup does not."
        )
        failures += 1

    # (e) find lines target named family globs, not bare *.txt
    # Check the find calls in core_section use named family patterns
    find_lines = [ln for ln in core_section.splitlines() if "find " in ln and "-name" in ln]
    bare_find = [ln for ln in find_lines if re.search(r"-name\s+['\"]?\*\.txt['\"]?", ln)]
    if bare_find:
        _fail(
            f"Layer 2g: {len(bare_find)} find line(s) use bare '*.txt' as -name target. "
            "Only named family globs (e.g., pending-restore-*.txt) are permitted."
        )
        failures += 1
    else:
        _pass("Layer 2g: all find lines in Core procedure use named family globs (no bare *.txt)")

    return failures == 0


def main() -> None:
    print(f"test_cleanup_sentinels.py — project root: {_PROJECT_ROOT}")
    print(f"SKILL.md path: {_SKILL_MD}")

    failures = 0

    if not _run_layer1_skill_md_contract():
        failures += 1

    if not _run_layer2_negative_safety():
        failures += 1

    print()
    if failures == 0:
        print("All layers passed.")
        sys.exit(0)
    else:
        print(f"FAILED: {failures} layer(s) failed.", file=sys.stderr)
        sys.exit(1)


# pytest-compatible: expose as test functions
def test_layer1_contract() -> None:
    """pytest wrapper for Layer 1: scope-bounded grep for all 9 families in allow-list section."""
    assert _run_layer1_skill_md_contract(), "Layer 1: SKILL.md allow-list contract failed"


def test_layer2_safety() -> None:
    """pytest wrapper for Layer 2: negative safety (no catch-all globs, no forbidden targets, trash vs rm)."""
    assert _run_layer2_negative_safety(), "Layer 2: negative safety test failed"


if __name__ == "__main__":
    main()
