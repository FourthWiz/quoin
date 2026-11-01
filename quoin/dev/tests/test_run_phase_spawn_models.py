"""`/run` names the model on every phase spawn.

An unnamed Agent spawn inherits the orchestrator's Opus, and a child skill's own
down-dispatch step was skipped in practice, so implement ran on Opus. These
checks pin the orchestrator-side rule that prevents it.
"""

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
RUN_SKILL = HERE.parent.parent / "adapters" / "claude" / "skills" / "run_hard" / "SKILL.md"


def _text():
    return RUN_SKILL.read_text(encoding="utf-8")


def _rule():
    m = re.search(r"\*\*Name the model on every phase spawn\.\*\*(.*)", _text())
    assert m, "run/SKILL.md lost the 'Name the model on every phase spawn' rule"
    return m.group(1)


def test_rule_maps_cheap_phases_to_sonnet():
    rule = _rule()
    sonnet = rule.split("`sonnet` for", 1)[1].split(";", 1)[0]
    for phase in ("implement", "gate", "end_of_task"):
        assert phase in sonnet, phase


def test_rule_maps_reasoning_phases_to_opus():
    rule = _rule()
    opus = rule.split("`opus` for", 1)[1].split(".", 1)[0]
    for phase in ("discover", "enrich", "specify", "architect", "thorough_plan", "review"):
        assert phase in opus, phase
    assert "fast route" in opus


def test_rule_has_a_1m_credit_fallback():
    assert "Usage credits required for 1M context" in _rule()


def test_full_path_implement_dispatch_is_sonnet():
    text = _text()
    assert "On the full path, dispatch this spawn with model sonnet" in text
    assert "default model tier" not in text
