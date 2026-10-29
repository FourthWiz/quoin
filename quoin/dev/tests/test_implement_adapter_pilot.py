"""Phase 12 adapter pilot tests for implement.

Parametrized over the single skill migrated in Phase 12. Follows the
pattern established by test_architecture_planning_adapter_pilot.py.

Skill parameters: (skill_name, expected_model, has_section_0)
  - implement: sonnet, has §0
"""
import re
from pathlib import Path

import pytest
import yaml
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent  # quoin/quoin/

FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"

SKILLS = [
    ("implement", "sonnet", True),
]

SKILL_IDS = [s[0] for s in SKILLS]

# Phase 6–11 skills — used for the regression-guard test below.
PRIOR_PHASE_SKILLS = [
    "capture_insight", "triage", "start_of_day",
    "review", "plan", "critic", "revise", "revise-fast",
    "architect", "thorough_plan", "gate",
]


def _slash_regex(name: str) -> re.Pattern:
    """Word-boundary regex for a slash-command token.

    Matches /name only when:
    - NOT preceded by an alphanumeric char or [-_] (avoids matching path
      components like 'skills/implement.md')
    - NOT followed by an alphanumeric char or [-_] (avoids matching
      'implement-2026-05-09.md')
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


# ── Parametrized tests (run over the implement skill) ─────────────────────


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


def test_implement_has_no_preamble_md():
    """R-04 guard: implement is NOT a subagent-preamble spawn target.

    Per CLAUDE.md "Subagent preamble" section, the 7 spawn-target skills
    are: critic, revise, revise-fast, plan, review, gate, architect.
    implement is excluded — `build_preambles.py` does not generate one.
    Asserting absence prevents accidental preamble leak.
    """
    adapter_preamble = PKG_DIR / "adapters" / "claude" / "skills" / "implement" / "preamble.md"
    assert not adapter_preamble.exists(), (
        f"CONSTRAINT VIOLATION: {adapter_preamble} unexpectedly exists. "
        "implement is NOT a spawn-target; build_preambles.py must not generate one."
    )


# ── Regression-guard tests (Phase 11 skills must still be covered) ────────


def test_phase_11_skills_still_covered():
    """Regression guard for the implement work — verifies all 11 Phase 6–11 skills are still covered.

    Asserts that each Phase 6–11 migrated skill still has:
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


# ── MAJ/MIN regression guards (T-09, T-11, T-12 edits must stay intact) ──


def test_detection_rule_consistency_lists_implement_at_adapter_path():
    """Guard that test_detection_rule_consistency.py lists implement at the adapter path (T-09 Group D edit)."""
    target = PKG_DIR / "dev" / "tests" / "test_detection_rule_consistency.py"
    text = target.read_text(encoding="utf-8")
    assert 'ADAPTER_SKILLS_DIR / "implement" / "SKILL.md"' in text, (
        "test_detection_rule_consistency.py must list implement at the adapter path. "
        "The T-09 edit was reverted — restore it."
    )
    assert not re.search(r'(?<!ADAPTER_)SKILLS_DIR\s*/\s*"implement"\s*/\s*"SKILL\.md"', text), (
        "test_detection_rule_consistency.py must NOT list implement at the bare SKILLS_DIR path. "
        "The T-09 repoint was reverted — restore it."
    )


def test_install_fresh_clone_lists_implement_in_migrated_skills():
    """Guard that test_install_fresh_clone.py includes implement in MIGRATED_SKILLS (T-11 edit)."""
    target = PKG_DIR / "dev" / "tests" / "test_install_fresh_clone.py"
    text = target.read_text(encoding="utf-8")
    tuple_start = text.find('MIGRATED_SKILLS = (')
    assert tuple_start != -1, "test_install_fresh_clone.py missing MIGRATED_SKILLS tuple"
    tuple_end = text.find(')', tuple_start)
    tuple_region = text[tuple_start:tuple_end + 1]
    assert '"implement"' in tuple_region, (
        "test_install_fresh_clone.py MIGRATED_SKILLS tuple must include \"implement\" "
        "(T-11 edit). The entry was reverted — restore it."
    )


# ── IVG-70: Branch-hygiene precheck assertions ────────────────────────────


def test_implement_adapter_has_section_0b():
    """IVG-70 T-08: adapter SKILL.md must contain the §0b Branch-hygiene precheck heading."""
    text = _adapter_skill("implement").read_text(encoding="utf-8")
    assert "## §0b Branch-hygiene precheck" in text, (
        "implement adapter SKILL.md must contain '## §0b Branch-hygiene precheck'. "
        "The IVG-70 T-04 edit is missing or was reverted."
    )


def test_implement_adapter_references_branch_hygiene_script():
    """IVG-70 T-08: §0b must reference branch_hygiene.py via __QUOIN_HOME__/scripts/."""
    text = _adapter_skill("implement").read_text(encoding="utf-8")
    assert "branch_hygiene.py" in text, (
        "implement adapter SKILL.md must reference 'branch_hygiene.py'. "
        "The IVG-70 T-04 script reference is missing."
    )
    assert "__QUOIN_HOME__/scripts/branch_hygiene.py" in text, (
        "implement adapter SKILL.md must use '__QUOIN_HOME__/scripts/branch_hygiene.py' "
        "(no literal ~/.claude/ path). IVG-70 T-04 edit is missing or regressed."
    )


def test_implement_adapter_section_0b_has_workflow_artifacts_walkup():
    """IVG-70 T-08 AC-5: §0b must contain the .workflow_artifacts walk-up loop.

    The walk-up is the worktree-safe root resolution (round-2 MAJ-1 fix):
    under worktree-isolated dispatch $(pwd) is the worktree, not the project root.

    Checks are scoped to the §0b block to avoid false positives from pre-existing
    content in the §0 sidecar block (which legitimately contains path_resolve.py
    invocations for other purposes).
    """
    import re
    full_text = _adapter_skill("implement").read_text(encoding="utf-8")

    # Extract the §0b block: from "## §0b " to the next "## " heading
    section_0b_match = re.search(
        r"(## §0b Branch-hygiene precheck.*?)(?=^## |\Z)",
        full_text,
        re.DOTALL | re.MULTILINE,
    )
    assert section_0b_match is not None, (
        "implement adapter SKILL.md must contain '## §0b Branch-hygiene precheck'. "
        "IVG-70 T-04 edit is missing or was reverted."
    )
    section_0b = section_0b_match.group(1)

    # §0b must contain the .workflow_artifacts walk-up token
    assert ".workflow_artifacts" in section_0b, (
        "implement adapter SKILL.md §0b must contain '.workflow_artifacts' "
        "(walk-up loop for worktree-safe root resolution). IVG-70 T-04 AC-5 failed."
    )
    # §0b must NOT pass bare $(pwd) directly to --project-root
    bare_pwd_pattern = re.compile(r'--project-root\s+["\']?\$\(pwd\)["\']?')
    assert not bare_pwd_pattern.search(section_0b), (
        "implement adapter SKILL.md §0b must NOT pass bare $(pwd) directly to "
        "branch_hygiene.py --project-root. Must use the walked-up $PROJECT_ROOT var. "
        "IVG-70 T-04 AC-5 guard failed."
    )
    # §0b must NOT use path_resolve.py --project-root (CRIT-1 guard, scoped to §0b)
    assert "path_resolve.py --project-root" not in section_0b, (
        "implement adapter SKILL.md §0b must NOT use 'path_resolve.py --project-root' "
        "(that flag has no print-root mode — exits 2 with empty stdout). "
        "IVG-70 T-04 AC-4 CRIT-1 guard failed."
    )


def test_implement_adapter_section_0b_has_verbatim_option_labels():
    """IVG-70 T-08 AC-3: the three AskUserQuestion option labels must appear verbatim."""
    text = _adapter_skill("implement").read_text(encoding="utf-8")
    labels = [
        "Create feature branch from here",
        "I'll pick the base branch",
        "Proceed on protected branch anyway",
    ]
    for label in labels:
        assert label in text, (
            f"implement adapter SKILL.md §0b must contain verbatim option label: {label!r}. "
            "IVG-70 T-08 AC-3 (drift test) failed — update label and test together."
        )


def test_implement_adapter_section_0b_fail_open_warning_present():
    """IVG-70 T-08 AC-4: §0b must contain the fail-OPEN warning string."""
    text = _adapter_skill("implement").read_text(encoding="utf-8")
    assert "branch-hygiene precheck unavailable; proceeding" in text, (
        "implement adapter SKILL.md §0b must contain the fail-OPEN warning string "
        "'branch-hygiene precheck unavailable; proceeding'. IVG-70 T-08 AC-4 failed."
    )


def test_implement_adapter_section_0b_benchmark_dual_guard():
    """IVG-70 T-08 AC-4: §0b must contain the benchmark dual-guard bypass string."""
    text = _adapter_skill("implement").read_text(encoding="utf-8")
    assert "branch-hygiene auto-branch for benchmark run" in text, (
        "implement adapter SKILL.md §0b must contain the benchmark bypass string "
        "'branch-hygiene auto-branch for benchmark run'. IVG-70 T-08 AC-4 failed."
    )


def test_implement_core_doc_has_branch_hygiene_section():
    """IVG-70 T-08: core implement.md must contain the portable Branch hygiene section."""
    text = _core_doc("implement").read_text(encoding="utf-8")
    assert "## Branch hygiene" in text, (
        "core/skills/implement.md must contain '## Branch hygiene' section. "
        "IVG-70 T-04 core doc edit is missing or was reverted."
    )


def test_implement_core_doc_no_branch_hygiene_script_refs():
    """IVG-70 T-08: core implement.md must NOT reference branch_hygiene.py or __QUOIN_HOME__."""
    text = _core_doc("implement").read_text(encoding="utf-8")
    assert "branch_hygiene.py" not in text, (
        "core/skills/implement.md must NOT contain 'branch_hygiene.py' "
        "(adapter-specific; keep core portable). IVG-70 adapter/core boundary violated."
    )
    assert "__QUOIN_HOME__" not in text, (
        "core/skills/implement.md must NOT contain '__QUOIN_HOME__' "
        "(adapter-specific; keep core portable). IVG-70 adapter/core boundary violated."
    )


def test_scope_cap_warnings_lists_implement_at_adapter_path():
    """Guard that test_scope_cap_warnings_present.py reads implement from the adapter path (T-12 edit).

    After T-03 stubs the legacy file, the substrings `## §0a Scope cap`,
    `30-40 tool uses`, and `automatic retry on stream-idle timeout` only
    appear in the adapter file. If the constant is reverted to the legacy
    path, three assertions break loudly. This guard fires earlier with a
    clearer "T-12 edit was reverted" message.
    """
    target = PKG_DIR / "dev" / "tests" / "test_scope_cap_warnings_present.py"
    text = target.read_text(encoding="utf-8")
    assert (
        'IMPLEMENT_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "implement" / "SKILL.md"' in text
    ), (
        "test_scope_cap_warnings_present.py IMPLEMENT_SKILL must point to the adapter path. "
        "The T-12 edit was reverted — restore it."
    )
    assert (
        'IMPLEMENT_SKILL = REPO_ROOT / "quoin" / "skills" / "implement" / "SKILL.md"' not in text
    ), (
        "test_scope_cap_warnings_present.py must NOT point IMPLEMENT_SKILL at the legacy stub path. "
        "Three scope-cap substring assertions will fail. Restore the T-12 edit."
    )
