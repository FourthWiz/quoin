from pathlib import Path
import _adapter_pilot_helpers

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent              # quoin/quoin/
CORE_SKILL_DOC = PKG_DIR / "core" / "skills" / "start_of_day.md"
ADAPTER_SKILL_MD = PKG_DIR / "adapters" / "claude" / "skills" / "start_of_day" / "SKILL.md"

# Per architecture R-2: start_of_day core doc forbids the slash token /start_of_day,
# the deploy path ~/.claude, Claude-specific model/dispatch tokens, and the
# Claude-runtime-specific shell-tool dispatch literal "gh CLI".
FORBIDDEN_TOKENS = ("~/.claude", "/start_of_day", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")
# Anchored on the directory the skill READS its primary input from (the
# daily-cache directory). Distinct from triage's anchor (which used the
# input ROOT) — start_of_day has a single load-bearing input directory.
REQUIRED_MENTION = ".workflow_artifacts/memory/daily/"


def test_core_skill_doc_exists():
    assert CORE_SKILL_DOC.is_file(), f"Missing {CORE_SKILL_DOC}"


def test_core_skill_doc_has_no_forbidden_tokens():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    assert not hits, f"Core doc contains forbidden tokens: {hits}"


def test_core_skill_doc_mentions_daily_cache_dir():
    text = CORE_SKILL_DOC.read_text(encoding="utf-8")
    assert REQUIRED_MENTION in text, (
        f"Core doc must mention literal: {REQUIRED_MENTION}"
    )


def test_install_sh_has_start_of_day_override():
    # IVG-69 Stage B retarget: install.sh no longer carries ADAPTER_*_SRC vars.
    # Assert the equivalent contract against installer.py logic instead.
    _adapter_pilot_helpers.assert_installer_selects_adapter("start_of_day")

