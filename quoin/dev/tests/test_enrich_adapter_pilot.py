from pathlib import Path
import _adapter_pilot_helpers  # noqa: F401 -- imported for parity with sibling adapter-pilot tests

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent              # quoin/quoin/
CORE_SKILL_DOC = PKG_DIR / "core" / "skills" / "enrich.md"
ADAPTER_SKILL_MD = PKG_DIR / "adapters" / "claude" / "skills" / "enrich" / "SKILL.md"

# Per architecture R-2 (mirrors triage's core-doc contract): enrich's core doc forbids
# the deploy path ~/.claude, the slash form /enrich, and Claude-specific model/dispatch
# tokens.
FORBIDDEN_TOKENS = ("~/.claude", "/enrich", "Haiku", "Sonnet", "Opus", "Agent")
REQUIRED_MENTION = "enriched-prompt.md"


def test_core_skill_doc_exists():
    assert CORE_SKILL_DOC.is_file(), f"Missing {CORE_SKILL_DOC}"


def test_core_skill_doc_has_no_forbidden_tokens():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    assert not hits, f"Core doc contains forbidden tokens: {hits}"


def test_core_skill_doc_mentions_enriched_prompt_output():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert REQUIRED_MENTION in text, (
        f"Core doc must mention literal: {REQUIRED_MENTION}"
    )


def test_install_sh_has_enrich_override():
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter("enrich")

