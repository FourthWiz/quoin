"""IVG-126 T-04: Drift-test guard for the automated verify-fix loop.

Guards the `### Automated verify-fix loop (post-task)` subsection added to
the Claude adapter implement SKILL.md (T-01), the portable
`## Automated verify-fix loop` contract in the core intent doc (T-02), and
the Codex procedure extension (T-03).

Follows the `_region()` header-slice pattern from
test_gate_affected_area_tokens.py so tokens are asserted WITHIN the guarded
region — not merely anywhere in the file — and the path-helper style from
test_implement_adapter_pilot.py.

Round-3 plan defects (CRIT-1, MAJ-1, MIN-1, MIN-2) each get a dedicated
regression-guard test below, per the plan's strengthened T-04 acceptance
list: the guards assert the resolved-variable VALUE, not merely flag
presence, so a reintroduced defect fails loudly.
"""
from __future__ import annotations

import re
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/

ADAPTER_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "implement" / "SKILL.md"
CORE_DOC = PKG_DIR / "core" / "skills" / "implement.md"
CODEX_PROCEDURE = PKG_DIR / "adapters" / "codex" / "procedures" / "implement.md"

REGION_HEADER = "### Automated verify-fix loop (post-task)"


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _region(text: str, header: str) -> str:
    """Return text from header until the next H2/H3 markdown heading.

    Mirrors `_region()` in test_gate_affected_area_tokens.py: only lines
    starting with `## ` or `### ` end the region, so single-hash comment
    lines embedded in pseudocode (e.g. `# best-effort linter`) don't
    truncate it early.
    """
    start = text.find(header)
    assert start != -1, f"Region header not found: {header!r}"
    after = text[start + len(header):]
    m = re.search(r"^#{2,3} ", after, re.MULTILINE)
    if m:
        return after[: m.start()]
    return after


def _adapter_region() -> str:
    return _region(_load(ADAPTER_SKILL), REGION_HEADER)


# ── (a) Adapter SKILL.md — core token presence, scoped to the region ──────


# ── (b) Core doc — contract present, adapter-specific tokens absent ───────


def test_core_doc_has_verify_fix_loop_section():
    text = _load(CORE_DOC)
    assert "## Automated verify-fix loop" in text, (
        "core/skills/implement.md must contain '## Automated verify-fix loop' (T-02)."
    )


def test_core_doc_verify_fix_loop_stays_portable():
    text = _load(CORE_DOC)
    for token in ("affected_tests.py", "${CLAUDE_PLUGIN_ROOT}", "QUOIN_VERIFY_RETRIES"):
        assert token not in text, (
            f"core/skills/implement.md must NOT contain adapter-specific token {token!r} "
            "(T-02 core/adapter boundary — mirrors test_implement_core_doc_no_branch_hygiene_script_refs)."
        )


# ── (c) Codex procedure — verify/fix-loop token present ───────────────────

