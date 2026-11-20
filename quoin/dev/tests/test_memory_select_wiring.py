"""Skill wiring for semantic lessons recall: every lessons bootstrap passes
--semantic, and /sleep refreshes the index and searches forgotten/ with the
flags the script actually accepts."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills"
SCRIPT = REPO_ROOT / "quoin" / "scripts" / "memory_select.py"


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
