"""The fake OpenCode executable's usage and ledger-writing additions."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from test_fake_opencode import Harness, fake

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups and signals are POSIX-only")

NEW_SCENARIOS = ("usage_revised", "usage_unknown_tokens", "usage_zero_cost", "ledger_append", "ledger_rewrite")


def _lines(res):
    return [json.loads(raw) for raw in res.stdout.decode().splitlines() if raw.strip()]


def _finishes(res):
    return [e for e in _lines(res) if e.get("type") == "step_finish"]


def test_custom_tokens_and_omitted_cost_are_verbatim(tmp_path):
    steps = [
        fake._step_finish("prt_a", "stop", tokens={"input": 7}, cost=0.5),
        fake._step_finish("prt_b", "stop", omit_cost=True),
        {"do": "exit", "code": 0},
    ]
    h = Harness(tmp_path, fake._scenario(*steps))
    try:
        finishes = _finishes(h.run("run", "--", "x"))
    finally:
        h.cleanup()
    assert finishes[0]["part"]["tokens"] == {"input": 7}
    assert finishes[0]["part"]["cost"] == 0.5
    assert "cost" not in finishes[1]["part"]
    assert finishes[1]["part"]["tokens"]["input"] == 10


def test_default_step_finish_is_unchanged():
    part = fake._step_finish("prt_a", "stop")["event"]["part"]
    assert part["tokens"] == {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    assert part["cost"] == 0.001


def test_append_file_appends_and_logs(tmp_path):
    h = Harness(tmp_path, fake._scenario(
        {"do": "append_file", "path": "sub/out.txt", "content": "one\n"},
        {"do": "append_file", "path": "sub/out.txt", "content": "two\n"},
        {"do": "exit", "code": 0},
    ))
    try:
        h.run("run", "--", "x")
        assert (h.cwd / "sub" / "out.txt").read_text() == "one\ntwo\n"
        assert (h.state / "effects.log").read_text().splitlines() == ["sub/out.txt", "sub/out.txt"]
    finally:
        h.cleanup()


def test_move_path_moves_and_logs(tmp_path):
    h = Harness(tmp_path, fake._scenario(
        {"do": "write_file", "path": "a/file.txt", "content": "x"},
        {"do": "move_path", "from": "a", "to": "deep/b"},
        {"do": "exit", "code": 0},
    ))
    try:
        h.run("run", "--", "x")
        assert not (h.cwd / "a").exists()
        assert (h.cwd / "deep" / "b" / "file.txt").read_text() == "x"
        assert "a -> deep/b" in (h.state / "effects.log").read_text().splitlines()
    finally:
        h.cleanup()


@pytest.mark.parametrize("name", NEW_SCENARIOS)
def test_new_scenarios_build_and_serialize(name):
    scenario = fake.SCENARIOS[name]()
    assert scenario["attempts"]
    json.dumps(scenario)


def test_revised_scenario_prints_two_lines_with_a_shared_part_id(tmp_path):
    h = Harness(tmp_path, fake.SCENARIOS["usage_revised"]())
    try:
        finishes = _finishes(h.run("run", "--", "x"))
    finally:
        h.cleanup()
    ids = [f["part"]["id"] for f in finishes]
    assert ids == ["prt_f1", "prt_f1", "prt_f2"]
    assert finishes[1]["part"]["tokens"]["input"] == 40


def test_unknown_tokens_and_zero_cost_shapes():
    unknown = fake.SCENARIOS["usage_unknown_tokens"]()["attempts"][0]["steps"][1]
    assert "output" not in unknown["event"]["part"]["tokens"]
    zero = fake.SCENARIOS["usage_zero_cost"]()["attempts"][0]["steps"][1]["event"]["part"]
    assert zero["cost"] == 0 and zero["tokens"]["input"] == 100 and zero["tokens"]["output"] == 20


def test_ledger_append_scenario_adds_an_agent_row(tmp_path):
    h = Harness(tmp_path, fake.SCENARIOS["ledger_append"]("demo"))
    try:
        ledger = h.cwd / ".workflow_artifacts" / "demo" / "cost-ledger.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("# Cost Ledger \u2014 demo\n", encoding="utf-8")
        h.run("run", "--", "x")
        assert "agent-row-1 | " in ledger.read_text(encoding="utf-8")
    finally:
        h.cleanup()


def test_ledger_rewrite_scenario_replaces_the_ledger(tmp_path):
    h = Harness(tmp_path, fake.SCENARIOS["ledger_rewrite"]())
    try:
        ledger = h.cwd / ".workflow_artifacts" / "demo" / "cost-ledger.md"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("# Cost Ledger \u2014 demo\nseed-row-1 | 2026-01-01 | plan | m | task | seeded | 0\n")
        h.run("run", "--", "x")
        assert "rewritten by agent" in ledger.read_text(encoding="utf-8")
    finally:
        h.cleanup()
