"""Content tests for the desktop-commander offer in /init_workflow.

The step mirrors the Serena offer: detect first, ask only when the server is
missing, and persist an explicit opt-out in a per-project sentinel so re-runs
do not ask again.
"""
from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent.parent
SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "init_workflow" / "SKILL.md"

SENTINEL = ".workflow_artifacts/.desktop-commander-skip"
INSTALL_CMD = "claude mcp add desktop-commander -- npx -y @wonderwhy-er/desktop-commander"
REPO_URL = "https://github.com/wonderwhy-er/DesktopCommanderMCP"


@pytest.fixture(scope="module")
def section() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.find("### Step 6.6: Offer desktop-commander setup")
    assert start != -1, "init_workflow must have a desktop-commander step"
    end = text.find("### Step 6.7", start)
    assert end != -1, "desktop-commander step must sit before Step 6.7"
    return text[start:end]


def test_step_ordered_after_serena_and_before_repo_spec() -> None:
    text = SKILL.read_text(encoding="utf-8")
    serena = text.find("### Step 6.5")
    dc = text.find("### Step 6.6")
    spec = text.find("### Step 6.7")
    assert -1 < serena < dc < spec


def test_sentinel_read_before_detection_and_written_on_opt_out(section: str) -> None:
    first = section.find(SENTINEL)
    assert first != -1
    assert first < section.find("ToolSearch"), "sentinel must short-circuit before detection"
    never = section.find('On **"Never ask again"**')
    assert never != -1 and SENTINEL in section[never:], "opt-out must write the sentinel"


def test_skip_for_now_does_not_persist(section: str) -> None:
    skip = section.find('On **"Skip for now"**')
    never = section.find('On **"Never ask again"**')
    assert -1 < skip < never
    assert SENTINEL not in section[skip:never]
    assert "no sentinel" in section[skip:never]


def test_detection_sources(section: str) -> None:
    assert "ToolSearch select:mcp__desktop-commander__" in section
    assert "claude mcp list" in section
    assert ".mcp.json" in section and "mcpServers" in section


def test_single_question_with_three_options(section: str) -> None:
    assert section.count("AskUserQuestion(") == 1
    assert "decision-gate: best-effort site=init_workflow-prompt-4" in section
    for label in ("Install now", "Skip for now", "Never ask again"):
        assert f'label: "{label}"' in section


def test_install_instructions_and_restart(section: str) -> None:
    install = section.find('On **"Install now"**')
    assert install != -1
    body = section[install:]
    assert INSTALL_CMD in body
    assert REPO_URL in body
    assert "RESTART" in body


def test_autonomous_records_not_configured_without_asking(section: str) -> None:
    auto = section.find("[autonomous]")
    assert auto != -1
    assert auto < section.find("AskUserQuestion(")
    assert "not configured" in section[auto:section.find("AskUserQuestion(")]


def test_no_literal_home_paths(section: str) -> None:
    assert "~/.claude/" not in section
