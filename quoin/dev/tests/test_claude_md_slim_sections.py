"""IVG-164 stage 1 T-08: slim content-assertion tests (architecture R-02 primary defense).

A ceiling test (test_claude_md_slim_ceiling.py) passes MORE easily as
content is dropped, so it defends nothing against an essential rule being
silently omitted from CLAUDE.slim.md. This file is the PRIMARY mechanical
defense against that failure mode: eight verbatim assertions pinning the
specific rules a main-session slim variant cannot safely lose, plus the
architecture-AC-3 reachability/ownership groups that close AC-3 limbs 2 and
4 (gates render and block; cost-ledger + session-state writes occur)
mechanically, independent of the T-11 behavioral pilot.

Independence discipline: the eight verbatim anchors below are hardcoded
literal strings transcribed directly from the live quoin/CLAUDE.md source
(not derived via build_claude_slim.CLASSIFICATION or its parser), so a bug
in the generator's own section-extraction logic, or a future accidental
reclassification of one of these headings from keep to drop, is caught here
independently of build_claude_slim.py and of test_build_claude_slim.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]  # quoin/ (repo root)
_SOURCE_ROOT = _REPO_ROOT / "quoin"
_SRC = _REPO_ROOT / "src"
_ADAPTER_SKILLS = _SOURCE_ROOT / "adapters" / "claude" / "skills"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

SOURCE = _SOURCE_ROOT / "CLAUDE.md"
SLIM = _SOURCE_ROOT / "CLAUDE.slim.md"
CATALOG = _SOURCE_ROOT / "memory" / "workflow-catalog.md"

QUOIN_HOME_PLACEHOLDER = "__QUOIN_HOME__"
# Same fixed worst-case literal as test_claude_md_slim_ceiling.py.
PROJECT_STANDIN = (
    "/Users/example/Library/CloudStorage/GoogleDrive-user@example.com/"
    "My Drive/Storage/project-name-x/.claude"
)


def _local_sections(text: str) -> dict[str, str]:
    """Assertion-local fence-aware section parser (independent of build_claude_slim.py).

    Same ~15-line algorithm as build_claude_slim._fence_aware_headings /
    parse_sections, deliberately re-implemented so the AC-3 reachability
    assertions below have two independent operands (two-independent-operands
    discipline, lesson 2026-08-02 vacuous substring drift tests).
    """
    lines = text.split("\n")
    in_fence = False
    starts: list[tuple[int, str]] = []
    pos = 0
    for ln in lines[:-1]:
        s = ln.lstrip()
        if s.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^#{1,3} ", ln):
            starts.append((pos, ln))
        pos += len(ln.encode("utf-8")) + 1
    raw = text.encode("utf-8")
    starts.append((len(raw), "<EOF>"))
    out: dict[str, str] = {}
    for i in range(len(starts) - 1):
        s, heading = starts[i]
        e, _ = starts[i + 1]
        out[heading] = raw[s:e].decode("utf-8")
    return out


# --- (1)-(7): the eight verbatim keep-set anchors ---------------------------

# (1) Never-auto-invoke rule (363 B) — the exact paragraph moved by T-02 from
# the old "### Task profiles" (source L157) into the new "### Explicit-command
# rule" H3, which the generator classifies keep.
NEVER_AUTO_INVOKE_PARAGRAPH = (
    "**CRITICAL RULE: `/implement` and `/end_of_task` require explicit user "
    "commands.** No skill may auto-invoke either. After `/thorough_plan` "
    "converges, the workflow STOPS and waits for `/implement`. After "
    "`/review` approves and the gate passes, the workflow STOPS and waits "
    "for `/end_of_task`. The user must consciously decide to start writing "
    "code AND to ship it."
)

# (2) Branch-hygiene enforcement summary (656 B) — the exact paragraph moved
# by T-02 from the old "### Git workflow" (source L196) into the new
# "### Branch hygiene (essential)" H3, which the generator classifies keep.
BRANCH_HYGIENE_ENFORCEMENT_PARAGRAPH = (
    "This rule is enforced at three layers: (1) `/implement` §0b "
    "branch-hygiene precheck (prompts to create a feature branch if on a "
    "protected branch pre-first-commit); (2) `/gate` FAILS if task commits "
    "(`has_task_commits: true` — commits ahead of upstream on main/master) "
    "land on a protected branch; (3) `/review` flags it as a backstop. Keys "
    "on the commits-ahead signal, NOT bare on-main status — a clean repo on "
    "main with no ahead commits is NOT a violation. Env knobs: "
    "`QUOIN_PROTECTED_BRANCHES` (csv, default `main,master`), "
    "`QUOIN_DISABLE_BRANCH_HYGIENE=1` (opt-out). Recovery recipe "
    "(mis-placed commits): `__QUOIN_HOME__/memory/branch-recovery.md`."
)

# (3) Canonical flow string.
CANONICAL_FLOW_STRING = (
    "/discover → /specify → GATE → /architect → GATE → /thorough_plan → "
    "GATE → /implement → GATE → /review → GATE → /end_of_task"
)

# (4) `.workflow_artifacts/<task-name>` task-layout convention.
TASK_LAYOUT_ANCHOR = ".workflow_artifacts/<task-name>/"

# (6) D-14 promotion: "### Workflow conventions" (209 B).
WORKFLOW_CONVENTIONS_ANCHOR = (
    "Never place stage plans into `.workflow_artifacts/finalized/` until "
    "`/end_of_task` is explicitly run."
)

# (7) D-14 promotion: "### Archiving completed work" (312 B).
ARCHIVING_ANCHOR = (
    "IMPORTANT: Never move to `finalized/` during planning or implementation"
)


def test_never_auto_invoke_rule_present_verbatim_in_slim():
    # 362 B live (D-13: live measurement wins over the plan's hand-counted
    # 363 B — same 1-2 B hand-count divergence T-02 recorded for its own
    # moved paragraphs).
    assert len(NEVER_AUTO_INVOKE_PARAGRAPH.encode("utf-8")) == 362
    slim_text = SLIM.read_text(encoding="utf-8")
    assert NEVER_AUTO_INVOKE_PARAGRAPH in slim_text, (
        "the never-auto-invoke CRITICAL RULE is missing verbatim from "
        "CLAUDE.slim.md — this is a main-session-essential safety rule and "
        "must never be dropped"
    )


def test_branch_hygiene_enforcement_present_verbatim_in_slim():
    # 655 B live (D-13: live measurement wins over the plan's hand-counted
    # 656 B).
    assert len(BRANCH_HYGIENE_ENFORCEMENT_PARAGRAPH.encode("utf-8")) == 655
    slim_text = SLIM.read_text(encoding="utf-8")
    assert BRANCH_HYGIENE_ENFORCEMENT_PARAGRAPH in slim_text, (
        "the branch-hygiene three-layer enforcement summary is missing "
        "verbatim from CLAUDE.slim.md"
    )


def test_canonical_flow_string_present_verbatim_in_slim():
    slim_text = SLIM.read_text(encoding="utf-8")
    assert CANONICAL_FLOW_STRING in slim_text


def test_task_layout_convention_present_in_slim():
    slim_text = SLIM.read_text(encoding="utf-8")
    assert TASK_LAYOUT_ANCHOR in slim_text


def test_git_pr_safety_section_present_verbatim_in_slim():
    """The full '### Git & PR Safety' section (push/PR restrictions) survives."""
    source_text = SOURCE.read_text(encoding="utf-8")
    sections = _local_sections(source_text)
    heading = "### Git & PR Safety"
    assert heading in sections, "heading not found in live source"
    slim_text = SLIM.read_text(encoding="utf-8")
    assert sections[heading] in slim_text, (
        "the '### Git & PR Safety' section (push/PR restrictions) is "
        "missing verbatim from CLAUDE.slim.md"
    )


def test_workflow_conventions_present_verbatim_in_slim():
    """D-14: '### Workflow conventions' (209 B) — never-finalize-early rule #1."""
    slim_text = SLIM.read_text(encoding="utf-8")
    assert WORKFLOW_CONVENTIONS_ANCHOR in slim_text


def test_archiving_completed_work_present_verbatim_in_slim():
    """D-14: '### Archiving completed work' (312 B) — never-finalize-early rule #2."""
    slim_text = SLIM.read_text(encoding="utf-8")
    assert ARCHIVING_ANCHOR in slim_text


def test_substitute_quoin_home_leaves_zero_residual(tmp_path):
    """(8) substitute_quoin_home() on CLAUDE.slim.md leaves zero residual __QUOIN_HOME__.

    The real guard here: assert_no_placeholders (installer.py) scans
    dest_root's _QUOIN_DEPLOYED_SUBDIRS and root-level files, but in project
    scope the deployed CLAUDE.md lives at dest_root.parent/CLAUDE.md —
    OUTSIDE that scan. This test closes the gap directly against the
    substitution function itself.
    """
    from quoin.installer import substitute_quoin_home

    slim_text = SLIM.read_text(encoding="utf-8")
    dest_root = tmp_path / ".claude"
    dest_root.mkdir()
    substituted = substitute_quoin_home(slim_text, dest_root)
    assert QUOIN_HOME_PLACEHOLDER not in substituted, (
        "substitute_quoin_home() left a residual __QUOIN_HOME__ in the "
        "deployed CLAUDE.slim.md projection"
    )


# --- AC-3 mechanical closure: two assertion groups ---------------------------

# (a) AC-3-scoped reachability sub-test. Sections that carry gate-invocation /
# cost-ledger / session-state prose and are classified drop — closing AC-3
# limbs 2 (gates render and block) and 4 (cost-ledger + session-state writes
# occur) mechanically, without a live run.
AC3_DROPPED_SECTIONS = [
    "### Cost tracking",
    "### Session state tracking",
    "### Communication",
]


@pytest.mark.parametrize("heading", AC3_DROPPED_SECTIONS)
def test_ac3_dropped_section_reachable_in_catalog_and_index(heading):
    source_text = SOURCE.read_text(encoding="utf-8")
    sections = _local_sections(source_text)
    assert heading in sections, f"heading not found in live source: {heading!r}"
    section_text = sections[heading]

    catalog_text = CATALOG.read_text(encoding="utf-8")
    assert section_text in catalog_text, (
        f"full section bytes for {heading!r} not found verbatim in "
        "workflow-catalog.md"
    )

    # Substituted deployed projection of the catalog still carries it. Some
    # of these sections (Cost tracking, Session state tracking) themselves
    # cite `__QUOIN_HOME__/memory/...` pointers, so the expected operand is
    # the SAME substitution applied to the section text, not the raw text.
    substituted_catalog = catalog_text.replace(QUOIN_HOME_PLACEHOLDER, PROJECT_STANDIN)
    substituted_section = section_text.replace(QUOIN_HOME_PLACEHOLDER, PROJECT_STANDIN)
    assert substituted_section in substituted_catalog, (
        f"{heading!r} section bytes disappeared after __QUOIN_HOME__ "
        "substitution"
    )

    slim_text = SLIM.read_text(encoding="utf-8")
    assert f"- {heading} -> " in slim_text, (
        f"CLAUDE.slim.md's pointer index carries no row naming {heading!r}"
    )


# (b) SKILL.md-ownership assertions over the deployed adapter corpus: the
# behaviours are skill-owned, not CLAUDE.md-owned, so they survive the slim
# variant regardless of what CLAUDE.md carries.
GATE_SKILL = _ADAPTER_SKILLS / "gate" / "SKILL.md"

# Live baselines re-verified at implement time (>= comparisons below so
# unrelated future skill additions do not red this test).
_LIVE_LEDGER_SELF_WRITE_COUNT = 26
_LIVE_SAVE_SESSION_STATE_COUNT = 10


def test_gate_skill_owns_audit_log_persistence_clause():
    gate_text = GATE_SKILL.read_text(encoding="utf-8")
    assert "gate-{phase}-{date}.md" in gate_text
    assert "audit log persistence" in gate_text.lower()
    assert "non-skippable" in gate_text.lower()


def test_ledger_self_write_marker_present_across_adapter_skills():
    matches = [
        p
        for p in sorted(_ADAPTER_SKILLS.glob("*/SKILL.md"))
        if "<!-- quoin:ledger-self-write -->" in p.read_text(encoding="utf-8")
    ]
    assert len(matches) >= _LIVE_LEDGER_SELF_WRITE_COUNT, (
        f"expected >= {_LIVE_LEDGER_SELF_WRITE_COUNT} adapter skills carrying "
        f"the ledger-self-write marker, found {len(matches)}"
    )


def test_save_session_state_section_present_across_adapter_skills():
    matches = [
        p
        for p in sorted(_ADAPTER_SKILLS.glob("*/SKILL.md"))
        if re.search(r"^## Save session state", p.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert len(matches) >= _LIVE_SAVE_SESSION_STATE_COUNT, (
        f"expected >= {_LIVE_SAVE_SESSION_STATE_COUNT} adapter skills carrying "
        f"a '## Save session state' section, found {len(matches)}"
    )
