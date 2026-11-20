from pathlib import Path
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent              # quoin/quoin/
CORE_SKILL_DOC = PKG_DIR / "core" / "skills" / "capture_insight.md"
ADAPTER_SKILL_MD = PKG_DIR / "adapters" / "claude" / "skills" / "capture_insight" / "SKILL.md"

FORBIDDEN_TOKENS = ("~/.claude", "/capture_insight", "Haiku", "Sonnet", "Opus", "Agent")
REQUIRED_MENTION = ".workflow_artifacts/memory/daily/insights-<YYYY-MM-DD>.md"


def test_core_skill_doc_exists():
    assert CORE_SKILL_DOC.is_file(), f"Missing {CORE_SKILL_DOC}"


def test_core_skill_doc_has_no_forbidden_tokens():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    assert not hits, f"Core doc contains forbidden tokens: {hits}"


def test_core_skill_doc_mentions_insights_path():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert REQUIRED_MENTION in text, (
        f"Core doc must mention literal: {REQUIRED_MENTION}"
    )


def test_install_sh_has_capture_insight_override():
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter("capture_insight")

