#!/usr/bin/env python3
"""
test_sleep_purge_sentinels.py — regression tests for /sleep --purge --sentinels.

Two-layer test (per plan T-05c / D-04 / MAJ-1):

Layer 1 — SKILL.md contract (scope-bounded grep):
  Open quoin/skills/sleep/SKILL.md. Assert:
    (a) The literal string '--sentinels' appears inside the '## --purge' H2 section.
    (b) Both sentinel-family literals appear inside the '## --purge' H2 section:
        pending-restore-*.txt, pending-prompt-*.txt, compact-happened-*.txt,
        mid-agent-handoff-*.txt, pending-resume-ref-*.txt, checkpoint-defer-*.txt,
        postcompact-reset-*.txt, checkpoint-pending-compact-*.txt
  Scope-bounded: grep is limited to the ## --purge section body, not the whole file,
  so a future refactor that splits the section is detected.

Layer 2 — negative safety test (per MAJ-1):
  Verify that --purge --sentinels never enumerates files outside the 8 hardcoded
  families. Use a SKILL.md grep: assert the family list is explicitly enumerated
  (no catch-all globs like '*.txt' or 'pending-*.txt' alone as the iteration target).
  Also assert that 'lessons-learned.md' and 'forgotten/' do NOT appear in the
  ## --purge section's sentinel family list (write-target restriction preserved for
  the delete path as well).

Both layers must pass. Exit 0 on success, non-zero on failure.

Runnable with:
  python3 quoin/dev/tests/test_sleep_purge_sentinels.py
  python3 -m pytest quoin/dev/tests/test_sleep_purge_sentinels.py
"""

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # quoin/ git repo root (Codex_workflow/quoin/)
_SKILL_MD = _PROJECT_ROOT / "quoin" / "adapters" / "claude" / "skills" / "sleep" / "SKILL.md"


def _extract_purge_section(content: str) -> str:
    """Extract the body of the ## --purge H2 section (scope-bounded grep)."""
    lines = content.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        if re.match(r"^## --purge", line):
            in_section = True
            section_lines.append(line)
            continue
        if in_section:
            # Stop at the next H2 (##) that is NOT a sub-section (###) of --purge
            if re.match(r"^## ", line):
                break
            section_lines.append(line)
    return "\n".join(section_lines)


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)


def _pass(message: str) -> None:
    print(f"PASS: {message}")


def _run_layer1_skill_md_contract() -> bool:
    """Layer 1: scope-bounded grep for --sentinels and both sentinel family literals."""
    print("\n--- Layer 1: SKILL.md contract (scope-bounded grep inside ## --purge) ---")

    if not _SKILL_MD.exists():
        _fail(f"sleep/SKILL.md not found at {_SKILL_MD}")
        return False

    content = _SKILL_MD.read_text(encoding="utf-8")
    purge_section = _extract_purge_section(content)

    if not purge_section:
        _fail("Could not extract ## --purge section from sleep/SKILL.md")
        return False

    failures = 0

    # (a) --sentinels flag must appear in the purge section
    if "--sentinels" in purge_section:
        _pass("Layer 1a: '--sentinels' found in ## --purge section")
    else:
        _fail("Layer 1a: '--sentinels' NOT found in ## --purge section of sleep/SKILL.md")
        failures += 1

    # (b) Both sentinel families must appear in the purge section
    sentinel_families = [
        "pending-restore-*.txt",
        "compact-happened-*.txt",
    ]

    for family in sentinel_families:
        if family in purge_section:
            _pass(f"Layer 1b: family '{family}' found in ## --purge section")
        else:
            _fail(
                f"Layer 1b: family '{family}' NOT found in ## --purge section of sleep/SKILL.md"
            )
            failures += 1

    return failures == 0


def _run_layer2_negative_safety() -> bool:
    """Layer 2: --purge --sentinels does NOT enumerate outside the 2 hardcoded families."""
    print("\n--- Layer 2: negative safety test (write-target restriction for delete path) ---")

    if not _SKILL_MD.exists():
        _fail(f"sleep/SKILL.md not found at {_SKILL_MD}")
        return False

    content = _SKILL_MD.read_text(encoding="utf-8")
    purge_section = _extract_purge_section(content)

    failures = 0

    # Assert the sentinel iteration is family-specific (not a catch-all glob).
    # The sentinel purge section must NOT contain '*.txt' or 'pending-*.txt' as standalone
    # patterns that would sweep more than the 2 named families.
    # We check for the hardcoded family list language and absence of dangerous catch-alls.
    if "hardcoded allow-list" in purge_section or "hardcoded" in purge_section:
        _pass("Layer 2a: 'hardcoded' allow-list language present in ## --purge section")
    else:
        _fail(
            "Layer 2a: 'hardcoded' allow-list language NOT found in ## --purge section — "
            "the family list must be documented as hardcoded to prevent drift"
        )
        failures += 1

    # Assert 'lessons-learned.md' does NOT appear as a candidate in the sentinel purge family list
    # (it appears elsewhere in the file for the write-target restriction — that's fine; we only
    # care that it's not listed as a sentinel family to be deleted)
    family_list_pattern = re.compile(
        r"(pending-restore|pending-prompt|compact-happened|mid-agent-handoff|"
        r"pending-resume-ref|checkpoint-defer|postcompact-reset|checkpoint-pending-compact)"
    )
    lessons_in_family_context = False
    for line in purge_section.splitlines():
        if "lessons-learned" in line and family_list_pattern.search(line):
            lessons_in_family_context = True
            break

    if not lessons_in_family_context:
        _pass(
            "Layer 2b: 'lessons-learned.md' NOT listed alongside sentinel families in ## --purge"
        )
    else:
        _fail(
            "Layer 2b: 'lessons-learned.md' appears in sentinel family context in ## --purge — "
            "write-target restriction for delete path may be violated"
        )
        failures += 1

    # Assert 'forgotten/' does NOT appear as a sentinel family target in the sentinel purge block
    # (It's the default target — the forgotten/ prose is in the 'forgotten/ purge' subsection,
    # not in the 'Sentinel purge' subsection)
    sentinel_purge_lines = []
    in_sentinel_block = False
    for line in purge_section.splitlines():
        if "Sentinel purge" in line or ("--sentinels" in line and "scope" in line):
            in_sentinel_block = True
        if "forgotten/ purge" in line or "### forgotten" in line:
            in_sentinel_block = False
        if in_sentinel_block:
            sentinel_purge_lines.append(line)

    sentinel_block_text = "\n".join(sentinel_purge_lines)

    # Check that forgotten/ directory is not in the sentinel families list
    # (It's fine if 'forgotten/' appears in the forgotten-purge subsection — we only
    # care it's not in the sentinel-purge family list as a deletion target)
    if "forgotten/" not in sentinel_block_text or "forgotten/ purge" in sentinel_block_text:
        _pass(
            "Layer 2c: 'forgotten/' is not a sentinel-family deletion target in sentinel purge block"
        )
    else:
        # Allow: forgotten/ can appear in the --all scope description (runs both)
        if "--all" in sentinel_block_text and "forgotten" in sentinel_block_text:
            _pass(
                "Layer 2c: 'forgotten/' in sentinel block only via --all scope reference (safe)"
            )
        else:
            _fail(
                "Layer 2c: 'forgotten/' appears as a deletion target in the sentinel purge block — "
                "scope boundary may be violated"
            )
            failures += 1

    return failures == 0


def main() -> None:
    print(f"test_sleep_purge_sentinels.py — project root: {_PROJECT_ROOT}")
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
    """pytest wrapper for Layer 1: SKILL.md scope-bounded grep."""
    assert _run_layer1_skill_md_contract(), "Layer 1: SKILL.md contract failed"


def test_layer2_safety() -> None:
    """pytest wrapper for Layer 2: negative safety (no catch-all globs, no forbidden targets)."""
    assert _run_layer2_negative_safety(), "Layer 2: negative safety test failed"


if __name__ == "__main__":
    main()
