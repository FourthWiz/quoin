"""Pins the written cost-estimator decision.

Removing one of the three estimators without revisiting the decision record
fails here, so the record and the code cannot drift apart silently.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "quoin" / "docs" / "cost-estimators.md"
SCRIPTS = ("cost_from_jsonl.py", "agent_transcript_cost.py", "cost_ledger_summary.py")
REQUIRED_URLS = (
    "https://code.claude.com/docs/en/headless",
    "https://code.claude.com/docs/en/agent-sdk/cost-tracking",
    "https://code.claude.com/docs/en/costs",
    "https://code.claude.com/docs/en/statusline",
    "https://code.claude.com/docs/en/monitoring-usage",
    "https://code.claude.com/docs/en/hooks",
)


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    marker = "\n## " + heading
    start = text.index(marker) + len(marker)
    end = text.find("\n## ", start)
    return text[start:end if end != -1 else None].strip()


def _deployed_scripts():
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        import quoin.installer as inst
    finally:
        sys.path.pop(0)
    return tuple(inst.DEPLOYED_SCRIPTS)


def test_doc_states_the_keep_decision():
    text = _doc()
    assert "Keep" in text
    for name in SCRIPTS:
        assert name in text


def test_doc_carries_the_cost_state_caveats():
    text = _doc()
    assert "double-count" in text
    assert "cost-state" in text
    assert "undocumented" in text


@pytest.mark.parametrize("url", REQUIRED_URLS)
def test_doc_cites_each_native_source(url):
    assert url in _doc()


@pytest.mark.parametrize("name", SCRIPTS)
def test_estimator_still_exists_and_is_deployed(name):
    assert (REPO_ROOT / "quoin" / "scripts" / name).is_file()
    assert name in _deployed_scripts()


@pytest.mark.parametrize("heading", ("Unknowns", "Revisit triggers"))
def test_doc_sections_are_present_and_non_empty(heading):
    assert _section(_doc(), heading)
