"""Light /run pins implementation to Opus from any other session tier.

Covers model_tier.py (tier comparison in both directions, CLI verdict) and
pins the hand-authored implementer guard block in run/SKILL.md, plus the
autonomous no-hand-off rule in the Step 4b staged loop just above it.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[2]  # quoin/quoin/
REPO_ROOT = PKG_DIR.parent
SCRIPT = PKG_DIR / "scripts" / "model_tier.py"
RUN_SKILL = PKG_DIR / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
RUN_LIGHT = RUN_SKILL.with_name("light-pipeline.md")
GUARD_HEADING = "## Implementer tier guard (before Step 5)"

_spec = importlib.util.spec_from_file_location("model_tier_under_test", SCRIPT)
model_tier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(model_tier)


@pytest.mark.parametrize(
    "model, relation, line",
    [
        ("claude-haiku-5-5", "lower", "DISPATCH|haiku"),
        ("Sonnet 5.5", "lower", "DISPATCH|sonnet"),
        ("claude-opus-5-5", "equal", "INLINE|opus"),
        ("Opus 5.5", "equal", "INLINE|opus"),
        ("claude-fable-5-1", "higher", "DISPATCH|fable"),
        ("Claude Mythos", "higher", "DISPATCH|mythos"),
        ("gpt-9", "unknown", "DISPATCH|unknown"),
        ("", "unknown", "DISPATCH|unknown"),
    ],
)
def test_compare_and_verdict_against_opus(model, relation, line):
    assert model_tier.compare_tier(model) == relation
    assert model_tier.verdict(model) == line


def test_target_other_than_opus():
    assert model_tier.compare_tier("claude-opus-5-5", target="sonnet") == "higher"
    assert model_tier.verdict("claude-sonnet-5-5", target="sonnet") == "INLINE|sonnet"


@pytest.mark.parametrize("knob, line", [("", "DISPATCH|fable\n"), ("1", "INLINE|optout\n")])
def test_cli_prints_one_line(monkeypatch, knob, line):
    monkeypatch.setenv("QUOIN_DISABLE_MINTIER_GUARD", knob)
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "claude-fable-5-1"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert out == line


def _guard_block() -> str:
    text = RUN_SKILL.read_text(encoding="utf-8")
    assert text.count(GUARD_HEADING) == 1
    match = re.search(re.escape(GUARD_HEADING) + r".+?(?=^## )", text, re.S | re.M)
    assert match
    return match.group(0)


def test_guard_sits_before_step_5_and_is_not_a_mintier_block():
    text = RUN_SKILL.read_text(encoding="utf-8")
    assert text.index(GUARD_HEADING) < text.index("## Step 5 — Implement")
    assert "§0″" not in text and "§0doubleprime" not in text
    assert re.search(r"^model: opus$", text, re.M)


@pytest.mark.parametrize(
    "token",
    [
        "__QUOIN_HOME__/scripts/model_tier.py",
        'model: "opus"',
        "starts with `[no-redispatch]`",
        "[quoin-run-impl-autonomous: opus implementer dispatch failed; implementing inline at current tier]",
        "[quoin-run-impl: aborted; switch with /model, then /run --resume <task>]",
        "[quoin-run-impl: opus implementer unavailable; implementing at current tier per user choice]",
        '"Abort, switch with /model"',
        '"Proceed at current tier"',
    ],
)
def test_guard_block_tokens(token):
    assert token in _guard_block()


def test_fail_open_checks_autonomous_first():
    block = _guard_block()
    assert block.index("**Autonomous-class**") < block.index("**1M-credit-class**")


def test_model_tier_is_deployed(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "src"))
    from quoin.installer import DEPLOYED_SCRIPTS

    assert "model_tier.py" in DEPLOYED_SCRIPTS


def test_autonomous_staged_run_never_hands_off_between_stages():
    # The staged loop closes Step 4b in the light pipeline file.
    text = RUN_LIGHT.read_text(encoding="utf-8")
    start = text.index("Continue to the next stage without asking")
    loop = text[start:text.index("## Step 7 — Review", start)]
    assert "Under `AUTONOMOUS`, run every remaining stage in this session" in loop
    # The fresh-session hand-off must be scoped to interactive runs only.
    assert loop.index("Interactive only:") < loop.index("fresh session;")
