"""Pins the written decision not to run /run stages as native Claude Code Workflows.

Adopting workflows silently (a saved workflow script in the shipped trees) or
deleting the decision record fails here, so the record and the code cannot
drift apart without the decision being revisited.
"""
from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "quoin" / "docs" / "native-workflows.md"
REQUIRED_URLS = (
    "https://code.claude.com/docs/en/workflows",
    "https://code.claude.com/docs/en/sub-agents",
)
REQUIRED_SECTIONS = (
    "Native behaviour checked",
    "Quoin needs compared",
    "Unknowns",
    "Revisit triggers",
)
# Trees that ship to users; a workflow script here would mean adoption.
GUARDED_TREES = (
    REPO_ROOT / "quoin" / "adapters" / "claude",
    REPO_ROOT / "src" / "quoin",
)


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    marker = "\n## " + heading
    start = text.index(marker) + len(marker)
    end = text.find("\n## ", start)
    return text[start:end if end != -1 else None].strip()


def test_doc_states_the_reject_decision():
    assert DOC.is_file()
    assert _section(_doc(), "Decision").startswith("Reject")


@pytest.mark.parametrize("url", REQUIRED_URLS)
def test_doc_cites_native_docs(url):
    assert url in _doc()


@pytest.mark.parametrize("heading", REQUIRED_SECTIONS)
def test_doc_sections_present_and_non_empty(heading):
    assert _section(_doc(), heading)


def test_no_workflow_scripts_in_shipped_trees():
    for tree in GUARDED_TREES:
        for path in tree.rglob("*.js"):
            assert "export const meta" not in path.read_text(encoding="utf-8", errors="ignore"), path
        for path in tree.rglob("workflows"):
            assert ".claude" not in path.parts, path
