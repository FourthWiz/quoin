"""Phase 21 adapter pilot tests for init_workflow.

Parametrized over the single skill migrated in Phase 21. Follows the
pattern established by test_discover_adapter_pilot.py (Phase 20),
test_expand_adapter_pilot.py (Phase 19), and prior phase pilots.

Skill parameters: (skill_name, expected_model, has_section_0)
  - init_workflow: opus, §0 absent (Opus-tier — NOT a cheap-tier self-dispatcher).

NOTE: has_section_0=False for init_workflow. This is the most likely copy-paste
error when adapting from Phase 19 (expand uses has_section_0=True). init_workflow
is Opus-tier and does NOT carry the §0 block. See CLAUDE.md "§0 Model
dispatch preamble": the 12 cheap-tier skills carry §0; the 9 Opus-tier
skills (including init_workflow) do NOT.

init_workflow characteristics:
  - model: opus (NOT sonnet, NOT haiku — differs from Phase 18 and 19 templates)
  - §0 Model dispatch block: absent (Opus-tier)
  - NOT a subagent-preamble spawn target (no preamble.md). The 7 spawn
    targets are: critic, revise, revise-fast, plan, review, gate, architect.
    init_workflow is excluded — build_preambles.py must not generate a preamble.md.
  - Eighth Opus-tier migration (after review, plan, critic, revise,
    architect, thorough_plan, discover) and the final Phase 6–21 migration.

PRIOR_PHASE_SKILLS lists the 20 Phase 6–20 skills. Later phases (22+) will
extend their own pilot's PRIOR_PHASE_SKILLS, NOT this file's.
"""
import re
from pathlib import Path

import pytest
import yaml
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent

FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"

SKILLS = [
    ("init_workflow", "opus", False),
]

SKILL_IDS = [s[0] for s in SKILLS]

# Phase 6–20 skills — used for the regression-guard test below.
PRIOR_PHASE_SKILLS = [
    "capture_insight", "triage", "start_of_day",
    "review", "plan", "critic", "revise", "revise-fast",
    "architect", "thorough_plan", "gate", "implement", "rollback",
    "end_of_task", "run", "end_of_day", "weekly_review", "cost_snapshot",
    "expand", "discover",
]


def _slash_regex(name: str) -> re.Pattern:
    """Word-boundary regex for a slash-command token.

    Matches /name only when:
    - NOT preceded by an alphanumeric char or [-_] (avoids matching path
      components like 'skills/init_workflow.md')
    - NOT followed by an alphanumeric char or [-_] (avoids matching
      'init_workflow-2026-05-12.md')
    """
    return re.compile(
        r"(?<![a-zA-Z0-9_\-])"
        + re.escape("/" + name.lstrip("/"))
        + r"(?=[^a-zA-Z0-9_\-]|$)"
    )


def _core_doc(skill_name: str) -> Path:
    return PKG_DIR / "core" / "skills" / f"{skill_name}.md"


def _adapter_skill(skill_name: str) -> Path:
    return PKG_DIR / "adapters" / "claude" / "skills" / skill_name / "SKILL.md"


# ── Parametrized tests (run over the init_workflow skill) ──────────────────────────


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_exists(skill_name, expected_model, has_section_0):
    path = _core_doc(skill_name)
    assert path.is_file(), f"Missing core skill doc: {path}"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_no_forbidden_tokens(skill_name, expected_model, has_section_0):
    text = _core_doc(skill_name).read_text(encoding="utf-8")

    # Static forbidden tokens — raw substring check.
    hits = [t for t in FORBIDDEN_TOKENS if t in text]

    # Per-skill slash form — negative-lookahead regex (D-03: bare substring FORBIDDEN).
    if _slash_regex(skill_name).search(text):
        hits.append(f"/{skill_name}")

    assert not hits, (
        f"Core doc for {skill_name} contains forbidden tokens: {hits}"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_mentions_anchor(skill_name, expected_model, has_section_0):
    text = _core_doc(skill_name).read_text(encoding="utf-8")
    assert REQUIRED_MENTION in text, (
        f"Core doc for {skill_name} must contain literal: {REQUIRED_MENTION}"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_exists(skill_name, expected_model, has_section_0):
    path = _adapter_skill(skill_name)
    assert path.is_file(), f"Missing adapter SKILL.md: {path}"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_declares_correct_model(skill_name, expected_model, has_section_0):
    text = _adapter_skill(skill_name).read_text(encoding="utf-8")
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"Adapter SKILL.md for {skill_name} missing YAML frontmatter"
    fm = yaml.safe_load(parts[1])
    assert fm.get("model") == expected_model, (
        f"Adapter SKILL.md for {skill_name} must declare model: {expected_model} "
        f"(got {fm.get('model')!r})"
    )
    assert fm.get("name") == skill_name, (
        f"Adapter SKILL.md for {skill_name} must declare name: {skill_name} "
        f"(got {fm.get('name')!r})"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_references_core_doc(skill_name, expected_model, has_section_0):
    text = _adapter_skill(skill_name).read_text(encoding="utf-8")
    ref = f"quoin/core/skills/{skill_name}.md"
    assert ref in text, (
        f"Adapter SKILL.md for {skill_name} must reference the portable intent doc: {ref}"
    )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_install_sh_has_adapter_override(skill_name, expected_model, has_section_0):
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter(skill_name)


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_adapter_skill_md_has_or_lacks_section_0(skill_name, expected_model, has_section_0):
    text = _adapter_skill(skill_name).read_text(encoding="utf-8")
    has_block = "## §0 Model dispatch" in text
    if has_section_0:
        assert has_block, (
            f"Adapter SKILL.md for {skill_name} (cheap-tier) must carry "
            "## §0 Model dispatch block"
        )
    else:
        assert not has_block, (
            f"Adapter SKILL.md for {skill_name} (Opus-tier) must NOT carry "
            "## §0 dispatch block"
        )


# ── Non-parametrized constraint guards ────────────────────────────────────


def test_init_workflow_has_no_preamble_md():
    """init_workflow is NOT a subagent-preamble spawn target.

    Per CLAUDE.md "Subagent preamble" section, the 7 spawn-target skills
    are: critic, revise, revise-fast, plan, review, gate, architect.
    init_workflow is excluded — build_preambles.py does not generate one.
    Asserting absence prevents accidental preamble leak.
    """
    adapter_preamble = (
        PKG_DIR / "adapters" / "claude" / "skills" / "init_workflow" / "preamble.md"
    )
    assert not adapter_preamble.exists(), (
        f"CONSTRAINT VIOLATION: {adapter_preamble} unexpectedly exists. "
        "init_workflow is NOT a spawn-target; build_preambles.py must not generate one."
    )


def test_adapter_preserves_load_bearing_behaviors():
    """T-02 byte-copy must preserve init_workflow's structural anchors and
    key behavior phrases.

    The heading anchors and behavior phrases guard against partial-content
    deletion that frontmatter tests alone would miss.
    """
    adapter = _adapter_skill("init_workflow").read_text(encoding="utf-8")
    # Heading anchors (H3 headings inside the Process body)
    assert "\n### Step 7: Copy quickstart guide + legacy detection\n" in adapter
    assert "\n### Step 4: Configure Claude Code permissions\n" in adapter
    assert "\n### Step 6: Run /discover\n" in adapter
    # Behavior anchors (short unique phrases byte-copy must preserve)
    assert "Legacy QUICKSTART location detected" in adapter
    assert "(project)/dev-workflow/QUICKSTART.md" in adapter
    assert "# Quoin — Quickstart (fallback)" in adapter
    # T-06: source files use __QUOIN_HOME__ placeholder; ~/.claude/ substituted at deploy time.
    assert "__QUOIN_HOME__/QUICKSTART.md" in adapter
    assert "dev-workflow/install.sh" in adapter
    assert "default to `[k]`" in adapter


# ── Regression-guard tests (Phase 6–20 skills must still be covered) ────────


def test_phase_20_skills_still_covered():
    """Regression guard for the init_workflow work — verifies all 20 Phase 6–20 skills are still covered.

    Asserts that each Phase 6–20 migrated skill still has:
    - its core doc at quoin/core/skills/<skill>.md
    - its adapter at quoin/adapters/claude/skills/<skill>/SKILL.md
    - its install.sh override variable and elif branch
    """
    from quoin import installer
    for skill in PRIOR_PHASE_SKILLS:
        core_doc = _core_doc(skill)
        assert core_doc.is_file(), (
            f"Regression: core doc for prior-phase skill {skill!r} is missing: {core_doc}"
        )
        adapter = _adapter_skill(skill)
        assert adapter.is_file(), (
            f"Regression: adapter SKILL.md for prior-phase skill {skill!r} is missing: {adapter}"
        )
        assert skill in installer.CANONICAL_SKILLS, (
            f"Regression: installer.CANONICAL_SKILLS missing {skill!r} for prior-phase skill"
        )


def test_install_fresh_clone_lists_init_workflow_in_migrated_skills():
    """Guard that test_install_fresh_clone.py includes init_workflow in MIGRATED_SKILLS (T-09 edit)."""
    target = PKG_DIR / "dev" / "tests" / "test_install_fresh_clone.py"
    text = target.read_text(encoding="utf-8")
    tuple_start = text.find('MIGRATED_SKILLS = (')
    assert tuple_start != -1, "test_install_fresh_clone.py missing MIGRATED_SKILLS tuple"
    tuple_end = text.find(')', tuple_start)
    tuple_region = text[tuple_start:tuple_end + 1]
    assert '"init_workflow"' in tuple_region, (
        'test_install_fresh_clone.py MIGRATED_SKILLS tuple must include "init_workflow" '
        "(T-09 edit). The entry was reverted — restore it."
    )
