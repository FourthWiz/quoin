"""Phase 9 adapter pilot tests for plan, critic, revise, revise-fast.

Parametrized over the four skills migrated in Phase 9. Follows the
pattern established by test_review_adapter_pilot.py.

Skill parameters: (skill_name, expected_model, has_section_0)
  - plan: opus, no §0
  - critic: opus, no §0
  - revise: opus, no §0
  - revise-fast: sonnet, HAS §0
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
    ("plan", "opus", False),
    ("critic", "opus", False),
    ("revise", "opus", False),
    ("revise-fast", "sonnet", True),
]

SKILL_IDS = [s[0] for s in SKILLS]


def _core_doc(skill_name: str) -> Path:
    return PKG_DIR / "core" / "skills" / f"{skill_name}.md"


def _adapter_skill(skill_name: str) -> Path:
    return PKG_DIR / "adapters" / "claude" / "skills" / skill_name / "SKILL.md"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_exists(skill_name, expected_model, has_section_0):
    path = _core_doc(skill_name)
    assert path.is_file(), f"Missing core skill doc: {path}"


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", SKILLS, ids=SKILL_IDS)
def test_core_skill_doc_no_forbidden_tokens(skill_name, expected_model, has_section_0):
    text = _core_doc(skill_name).read_text(encoding="utf-8")
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    # Forbid slash-command invocation form (e.g. `/plan`, `/critic`) — match the
    # token only when followed by a space, end-of-string, or punctuation (not a
    # path separator like in `critic-response-*.md`).
    slash_cmd = f"/{skill_name}"
    if re.search(re.escape(slash_cmd) + r"(?=[^a-zA-Z0-9_\-]|$)", text):
        hits.append(slash_cmd)
    assert not hits, f"Core doc for {skill_name} contains forbidden tokens: {hits}"


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
    # revise and revise-fast carry BOTH revise.md AND revise-fast.md references
    if skill_name in ("revise", "revise-fast"):
        assert "quoin/core/skills/revise.md" in text, (
            f"Adapter SKILL.md for {skill_name} must reference quoin/core/skills/revise.md"
        )
        assert "quoin/core/skills/revise-fast.md" in text, (
            f"Adapter SKILL.md for {skill_name} must reference quoin/core/skills/revise-fast.md"
        )
    else:
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
            f"Adapter SKILL.md for {skill_name} (cheap-tier) must carry ## §0 Model dispatch block"
        )
    else:
        assert not has_block, (
            f"Adapter SKILL.md for {skill_name} (Opus-tier) must NOT carry ## §0 dispatch block"
        )


@pytest.mark.parametrize("skill_name,expected_model,has_section_0", [
    s for s in SKILLS if s[0] in ("revise", "revise-fast")
], ids=["revise", "revise-fast"])
def test_adapter_skill_md_has_scope_cap(skill_name, expected_model, has_section_0):
    """revise and revise-fast adapters must carry ## Scope cap section."""
    text = _adapter_skill(skill_name).read_text(encoding="utf-8")
    assert "## Scope cap" in text, (
        f"Adapter SKILL.md for {skill_name} must contain '## Scope cap' heading"
    )
    has_hyphen = "30-40 tool uses" in text
    has_emdash = "30–40 tool uses" in text  # em-dash variant
    assert has_hyphen or has_emdash, (
        f"Adapter SKILL.md for {skill_name} must contain '30-40 tool uses' (or em-dash variant)"
    )
    assert "automatic retry on stream-idle timeout" in text.lower(), (
        f"Adapter SKILL.md for {skill_name} must contain the standalone-no-retry note "
        f"(substring 'automatic retry on stream-idle timeout')"
    )


def test_revise_revise_fast_portable_doc_lines_byte_equal():
    """The portable-doc + see-also lines must be byte-identical between revise and revise-fast.

    This closes the R-03 SYNC-contract gap: both adapter files carry the same two
    mirrored lines, so the SYNC test (which only allows diffs in ## Model requirement
    and ## §0) continues to pass.
    """
    revise_text = _adapter_skill("revise").read_text(encoding="utf-8")
    revise_fast_text = _adapter_skill("revise-fast").read_text(encoding="utf-8")

    portable_doc_marker = "Portable intent doc:"
    see_also_marker = "See also"

    def _extract_portable_lines(text: str) -> list:
        """Extract the portable-doc and see-also lines from between H1 and first H2."""
        # Find H1
        h1_match = re.search(r"^# Revise\s*$", text, re.MULTILINE)
        if not h1_match:
            return []
        after_h1 = text[h1_match.end():]
        # Find first H2
        h2_match = re.search(r"^## ", after_h1, re.MULTILINE)
        between = after_h1[:h2_match.start()] if h2_match else after_h1
        lines = [ln for ln in between.splitlines() if portable_doc_marker in ln or see_also_marker in ln]
        return lines

    revise_lines = _extract_portable_lines(revise_text)
    revise_fast_lines = _extract_portable_lines(revise_fast_text)

    assert revise_lines, "revise adapter SKILL.md missing portable-doc lines between H1 and first H2"
    assert revise_fast_lines, "revise-fast adapter SKILL.md missing portable-doc lines between H1 and first H2"
    assert revise_lines == revise_fast_lines, (
        "The portable-doc + see-also lines must be byte-identical between revise and revise-fast adapters.\n"
        f"  revise: {revise_lines!r}\n"
        f"  revise-fast: {revise_fast_lines!r}"
    )
