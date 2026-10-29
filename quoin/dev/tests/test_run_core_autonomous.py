"""T-16 (IVG-153 autonomous-run-mode): the runtime-neutral core doc for run
must document the opt-in autonomous span while staying token-clean.

Companion to test_run_adapter_pilot.py's forbidden-token guard — this file
adds the autonomous-specific content assertions without duplicating the
full parametrized pilot suite.
"""
import re
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent
PKG_DIR = TESTS_DIR.parent.parent

CORE_DOC = PKG_DIR / "core" / "skills" / "run_hard.md"

# Mirrors test_run_adapter_pilot.py::FORBIDDEN_TOKENS — the core doc is
# runtime-neutral and must never name a specific model tier or dispatch
# mechanism. The bare "/run" slash form is checked separately below via the
# same word-boundary regex test_run_adapter_pilot.py uses, so legitimate
# substrings like "offer/run the spec phase" are not false-flagged.
FORBIDDEN_TOKENS = ("~/.claude", "Haiku", "Sonnet", "Opus", "Agent", "gh CLI")

# Word-boundary regex for the bare "/run" slash-command token — mirrors
# test_run_adapter_pilot.py::_slash_regex(skill_name="run").
_SLASH_RUN = re.compile(
    r"(?<![a-zA-Z0-9_\-])"
    + re.escape("/run")
    + r"(?=[^a-zA-Z0-9_\-]|$)"
)


def _text() -> str:
    return CORE_DOC.read_text(encoding="utf-8")


def test_core_doc_exists() -> None:
    assert CORE_DOC.is_file(), f"Missing core skill doc: {CORE_DOC}"


def test_core_doc_mentions_autonomous() -> None:
    assert "autonomous" in _text().lower(), (
        "core/skills/run_hard.md must document the opt-in autonomous span"
    )


def test_core_doc_mentions_non_interactive_formulation() -> None:
    assert "non-interactive formulation" in _text(), (
        "core/skills/run_hard.md must describe the non-interactive formulation pass "
        "that gates entry into the autonomous span"
    )


def test_core_doc_mentions_hard_stop() -> None:
    assert "hard stop" in _text(), (
        "core/skills/run_hard.md must state that hard stops are preserved under "
        "autonomous mode"
    )


def test_core_doc_never_auto_creates_pr() -> None:
    text = _text().lower()
    assert "pull request" in text or " pr " in text or "pr creation" in text, (
        "core/skills/run_hard.md autonomous section must restate the never-auto-PR "
        "invariant"
    )


def test_core_doc_documents_the_resume_record() -> None:
    """review-3 MINOR 13: the portable core previously said nothing about
    the plain-run resumability record -- a Codex adapter implementing this
    skill from `core/skills/run_hard.md` alone would not know to write it."""
    text = _text()
    assert "run-state-{task}.json" in text
    assert "run-notes-{task}.md" in text
    assert "never before the gate or verdict that determines" in text


def test_core_doc_stays_token_clean() -> None:
    text = _text()
    hits = [t for t in FORBIDDEN_TOKENS if t in text]
    if _SLASH_RUN.search(text):
        hits.append("/run")
    assert not hits, (
        f"core/skills/run_hard.md contains forbidden tokens: {hits}"
    )
