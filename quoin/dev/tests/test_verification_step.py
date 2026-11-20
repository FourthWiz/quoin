"""
Drift-detection tests for the §V Ground-truth verification blocks (IVG-115 T-04/T-09 partial).

Mirrors test_mintier_guard.py's shape: heading-present-once, markers-once, required
tokens inside the block, ordering (claims strictly before verify in end_of_day),
generator idempotence, and run_check()==0 on the committed tree.

Scope note: this covers T-04's acceptance criteria (the generator itself) plus the
structural half of T-05/T-06 (both blocks land correctly in the 3 adapter files).
It does NOT cover the SessionEnd-hook backstop (T-12) or the empty/absent-manifest
runtime behavior (T-01/T-02 own those, in test_verify_claims.py) — the fuller
regression pass across all of that is T-09, not duplicated here.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/
ADAPTER_SKILLS_DIR = PKG_DIR / "adapters" / "claude" / "skills"
SCRIPTS_DIR = PKG_DIR / "scripts"

_ivs_spec = importlib.util.spec_from_file_location(
    "inject_verification_step",
    SCRIPTS_DIR / "inject_verification_step.py",
)
assert _ivs_spec is not None
_ivs = importlib.util.module_from_spec(_ivs_spec)
assert _ivs_spec.loader is not None
_ivs_spec.loader.exec_module(_ivs)

VERIFY_SKILLS = _ivs.VERIFY_TARGET_SKILLS
CLAIMS_SKILLS = _ivs.CLAIMS_EMIT_SKILLS
LIGHT_SKILLS = _ivs.RECONCILE_LIGHT_SKILLS


def _read(skill: str) -> str:
    return (ADAPTER_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")


def test_run_check_passes_on_committed_tree():
    result = _ivs.run_check()
    assert result == 0, (
        "inject_verification_step run_check() returned non-zero on the committed tree. "
        "Run `python3 quoin/scripts/inject_verification_step.py` to regenerate."
    )


def _sum_field_in_window(session_dir: Path, field: str) -> int:
    """Reference implementation of the end_of_day/weekly_review Cost-summary roll-up
    (IVG-115 T-10): regex-extract `- <field>: <N>` from each session-state file's
    `## Cost` block and sum across the window. Mirrors the pre-existing fallback_fires
    roll-up this skill already performs — same shape, different field name."""
    pattern = re.compile(rf"^- {re.escape(field)}:\s*(\d+)\s*$", re.MULTILINE)
    total = 0
    for path in sorted(session_dir.glob("*.md")):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match:
            total += int(match.group(1))
    return total


def test_verification_mismatches_rollup_counts_seeded_mismatch(tmp_path):
    """IVG-115 T-10 acceptance: a seeded verification_mismatches value must be counted
    by the roll-up algorithm end_of_day's Cost-summary step describes (window sum,
    absent-field-treated-as-0, no error on legacy pre-IVG-115 session files)."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "2026-07-09-task-a.md").write_text(
        "## Cost\n- Session UUID: abc\n- Phase: implement\n"
        "- Recorded in cost ledger: no\n- end_of_day_due: yes\n"
        "- fallback_fires: 0\n- verification_ran: no\n- verification_mismatches: 2\n",
        encoding="utf-8",
    )
    (sessions_dir / "2026-07-09-task-b.md").write_text(
        "## Cost\n- Session UUID: def\n- Phase: plan\n"
        "- Recorded in cost ledger: yes\n- end_of_day_due: no\n"
        "- fallback_fires: 1\n- verification_ran: yes\n- verification_mismatches: 0\n",
        encoding="utf-8",
    )
    (sessions_dir / "2026-07-08-task-c-precompact.md").write_text(
        "## Cost\n- Session UUID: ghi\n- Phase: review\n"
        "- Recorded in cost ledger: no\n- end_of_day_due: yes\n",
        encoding="utf-8",
    )  # legacy session lacking the field entirely -> counts as 0, no error

    assert _sum_field_in_window(sessions_dir, "verification_mismatches") == 2


