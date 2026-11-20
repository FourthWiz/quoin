from pathlib import Path
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent              # quoin/quoin/
CORE_SKILL_DOC = PKG_DIR / "core" / "skills" / "review.md"
ADAPTER_SKILL_MD = PKG_DIR / "adapters" / "claude" / "skills" / "review" / "SKILL.md"

# review is Opus-tier — forbidden tokens mirror the existing pilot tests plus gh CLI.
FORBIDDEN_TOKENS = ("~/.claude", "/review", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
# Load-bearing input/output anchor for the review skill.
REQUIRED_MENTION = ".workflow_artifacts/<task-name>/"


def test_core_skill_doc_exists():
    assert CORE_SKILL_DOC.is_file(), f"Missing {CORE_SKILL_DOC}"


def test_core_skill_doc_has_no_forbidden_tokens():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    assert not hits, f"Core doc contains forbidden tokens: {hits}"


def test_core_skill_doc_mentions_task_artifact_anchor():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert REQUIRED_MENTION in text, (
        f"Core doc must mention literal: {REQUIRED_MENTION}"
    )


def test_install_sh_has_review_override():
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter("review")


# ── IVG-70: Branch-hygiene review backstop assertions ─────────────────────


def test_review_core_doc_has_placement_backstop_rule():
    """IVG-70 T-08 AC-2: core review.md Behavior contract must contain the backstop line."""
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert "protected branch" in text, (
        "core/skills/review.md must mention 'protected branch' in the Behavior contract. "
        "IVG-70 T-06 core doc edit is missing or was reverted."
    )


def test_review_core_doc_no_adapter_specific_refs():
    """IVG-70 T-08: core review.md must NOT contain branch_hygiene.py or ${CLAUDE_PLUGIN_ROOT}."""
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert "branch_hygiene.py" not in text, (
        "core/skills/review.md must NOT contain 'branch_hygiene.py' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )
    assert "${CLAUDE_PLUGIN_ROOT}" not in text, (
        "core/skills/review.md must NOT contain '${CLAUDE_PLUGIN_ROOT}' "
        "(adapter-specific). IVG-70 adapter/core boundary violated."
    )


# ── IVG-128 T-12: review fan-out wiring guard (cheap early guard) ──────────


