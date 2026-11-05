"""Skill wiring for semantic lessons recall: every lessons bootstrap passes
--semantic, and /sleep refreshes the index and searches forgotten/ with the
flags the script actually accepts."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
SCRIPT = REPO_ROOT / "quoin" / "scripts" / "memory_select.py"


@pytest.mark.parametrize("skill", ["plan", "architect", "critic", "review", "implement"])
def test_lessons_bootstrap_passes_semantic(skill):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    calls = re.findall(r"memory_select\.py[^`]*", text)
    assert calls, f"{skill}: no memory_select.py call found"
    assert all("--semantic" in c for c in calls), calls


def test_sleep_refreshes_index_outside_dry_run_and_restores_via_forgotten_search():
    text = (SKILLS / "sleep" / "SKILL.md").read_text(encoding="utf-8")
    step = text.split("### Step 5b", 1)[1].split("### Step 6", 1)[0]
    assert "--build-index" in step and "forced_dry_run" in step
    restore = text.split("## --restore <pattern>", 1)[1]
    assert "--semantic --source forgotten" in restore


def test_forgotten_json_has_the_keys_sleep_reads(tmp_path):
    mem = tmp_path / "memory"
    (mem / "forgotten").mkdir(parents=True)
    (mem / "forgotten" / "2026-01-30.md").write_text(
        "> Source: /abs/insights.md:1..2\n> Forgotten: t\n> Score: forget=4, promote=0\n\n"
        "flaky pane timing\n\n---\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", "forgotten", "--task-text", "flaky",
         "--file", str(mem / "lessons-learned.md"), "--json"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    (hit,) = json.loads(out)["selected"]
    assert {"source", "lineno", "anchor", "score"} <= set(hit)
    assert hit["anchor"] == "/abs/insights.md:1..2"
