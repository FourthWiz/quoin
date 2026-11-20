"""Drift tests: the parent's child-watch instructions stay in step with child_watch.py."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_SKILL = REPO_ROOT / "quoin" / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"
AUTO_MODE = REPO_ROOT / "quoin" / "memory" / "autonomous-mode.md"
RUN_CORE = REPO_ROOT / "quoin" / "core" / "skills" / "run_hard.md"
SCRIPT = REPO_ROOT / "quoin" / "core" / "scripts" / "child_watch.py"

HEADING = re.compile(r"(?m)^#{1,6} ")
H3 = re.compile(r"(?m)^### Watching a handed-off child\s*$")
STATES = ("PROGRESS", "ALIVE", "STALL", "DONE", "HALTED", "NEEDS_DECISION", "DEAD", "EXPIRED")
SIGNALS = ("done sentinel", "halt sentinel", "needs-decision", "new `.done`", "commit", "liveness")


def _script():
    spec = importlib.util.spec_from_file_location("child_watch_contract_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _section(text: str, start_pat: "re.Pattern", what: str) -> str:
    m = start_pat.search(text)
    assert m, f"start marker missing: {what}"
    nxt = HEADING.search(text, m.end())
    body = text[m.start(): nxt.start() if nxt else len(text)]
    assert body.strip(), what
    return body


def test_autonomous_mode_subsection_matches_script():
    text = AUTO_MODE.read_text(encoding="utf-8")
    body = _section(text, re.compile(r"(?m)^### Watching a handed-off child\s*$"), "autonomous-mode subsection")
    for state in STATES:
        assert state in body, state
    for signal in ("done sentinel", "halt sentinel", "needs-decision", ".done", "commit", "liveness"):
        assert signal in body, signal
    assert "observe-only" in body and "--once" in body
    mod = _script()
    for knob, default in (
        (mod.INTERVAL_KNOB, mod.INTERVAL_DEFAULT),
        (mod.STALL_KNOB, mod.STALL_DEFAULT),
        (mod.MAX_HOURS_KNOB, mod.MAX_HOURS_DEFAULT),
    ):
        flat = " ".join(body.split())
        assert f"`{knob}` (default {default}," in flat, knob


def test_repairs_used_reset_note():
    flat = " ".join(AUTO_MODE.read_text(encoding="utf-8").split())
    assert "`repairs_used` also resets on progress" in flat
    assert "`repairs_used`, reset only by the consent rule" not in flat
    assert "`child-watch-<task>.json`" in flat


def test_runtime_neutral_contract_mentions_watching():
    text = RUN_CORE.read_text(encoding="utf-8")
    start = text.index("## Autonomous durability contract")
    nxt = HEADING.search(text, start + 5)
    section = text[start: nxt.start() if nxt else len(text)]
    assert "observe-only" in section and "takeover" in section
