"""Docs-parity for the auto-resume CLI lookup: the probe-budget knob's
defaults/clamps and the full `STALE_CLI` kind set must match what
`auto_resume.py` actually does, not drift from it in prose.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO / "quoin"
AUTO_RESUME_PATH = QUOIN_SRC / "core" / "scripts" / "auto_resume.py"


def _load_auto_resume():
    spec = importlib.util.spec_from_file_location("auto_resume_docs_parity", AUTO_RESUME_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _derive_stale_cli_kinds(source: str) -> set[str]:
    """Mechanically derives the full set of `STALE_CLI` kind strings from
    `auto_resume.py`'s own source, rather than hand-listing them (lesson
    2026-06-04 applies to code too, not just doc slicing): most kinds are
    literal `kind="..."` kwargs or `"kind": "..."` dict entries, but
    `record-invalid` is assigned indirectly (`kind=err_kind`, where
    `err_kind` is returned by `_load_runtime_record` as a bare
    `return None, "record-invalid"`), so that return-site literal is
    derived with its own pattern."""
    kinds = set(re.findall(r'kind\s*=\s*"([a-z][a-z-]*)"', source))
    kinds |= set(re.findall(r'"kind"\s*:\s*"([a-z][a-z-]*)"', source))
    kinds |= set(re.findall(r'return None,\s*"([a-z][a-z-]*)"', source))
    return kinds


def test_probe_budget_defaults_unset():
    ar = _load_auto_resume()
    assert ar._probe_budget_ms("start") == 1500
    assert ar._probe_budget_ms("stop") == 3000


def test_probe_budget_clamps_to_max_when_knob_huge(monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "999999")
    ar = _load_auto_resume()
    assert ar._probe_budget_ms("start") == 3000
    assert ar._probe_budget_ms("stop") == 7000


def test_probe_budget_clamps_to_min_when_knob_tiny(monkeypatch):
    monkeypatch.setenv("QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS", "1")
    ar = _load_auto_resume()
    assert ar._probe_budget_ms("start") == 250
    assert ar._probe_budget_ms("stop") == 250


def test_hooks_table_knob_line_has_all_four_numbers():
    hooks_table = (QUOIN_SRC / "memory" / "hooks-table.md").read_text(encoding="utf-8")
    knob_lines = [
        line for line in hooks_table.splitlines()
        if "QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS" in line
    ]
    assert len(knob_lines) == 1, knob_lines
    line = knob_lines[0]
    for number in ("1500", "3000", "7000", "250"):
        assert number in line, line


def test_autonomous_mode_docs_name_the_record_and_every_kind():
    ar = _load_auto_resume()
    source = AUTO_RESUME_PATH.read_text(encoding="utf-8")
    kinds = _derive_stale_cli_kinds(source)
    assert kinds, "regex derivation found no kinds — auto_resume.py's kind literals changed shape"

    autonomous_mode = (QUOIN_SRC / "memory" / "autonomous-mode.md").read_text(encoding="utf-8")
    assert "QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS" in autonomous_mode
    assert "quoin-runtime.json" in autonomous_mode
    for kind in kinds:
        assert kind in autonomous_mode, f"kind {kind!r} missing from autonomous-mode.md"
