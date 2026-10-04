"""The fake OpenCode executable's per-command scenarios and placeholders."""
from __future__ import annotations

import os

import pytest

from test_fake_opencode import Harness, fake

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups and signals are POSIX-only")


def _write(path, content="x"):
    return {"do": "write_file", "path": path, "content": content}


def _scenario(commands, attempts=None):
    return {
        "attempts": attempts or [{"steps": [_write("top.txt", "top"), {"do": "exit", "code": 0}]}],
        "commands": {k: {"attempts": [{"steps": s + [{"do": "exit", "code": 0}]} for s in v]}
                     for k, v in commands.items()},
    }


def _run(h, command, arg):
    return h.run("run", "--format", "json", "--command", command, "--", arg)


def _effects(h):
    path = h.state / "effects.log"
    return path.read_text().splitlines() if path.exists() else []


def test_lookup_prefers_command_at_stage_then_command_then_top_level(tmp_path):
    h = Harness(tmp_path, _scenario({
        "quoin-plan@2": [[_write("staged.txt")]],
        "quoin-plan": [[_write("plain.txt")]],
    }))
    try:
        _run(h, "quoin-plan", "stage 2 of demo")
        _run(h, "quoin-plan", "stage 1 of demo")
        _run(h, "quoin-critic", "stage 1 of demo")
        assert _effects(h) == ["staged.txt", "plain.txt", "top.txt"]
        keys = [e.get("scenario_key") for e in h.invocations()]
        assert keys == ["quoin-plan@2", "quoin-plan", None]
    finally:
        h.cleanup()


def test_attempt_counter_is_per_key(tmp_path):
    h = Harness(tmp_path, _scenario({
        "quoin-plan@1": [[_write("p1.txt")], [_write("p2.txt")]],
        "quoin-critic": [[_write("c1.txt")]],
    }))
    try:
        _run(h, "quoin-plan", "stage 1 of demo")
        _run(h, "quoin-critic", "stage 1 of demo")
        _run(h, "quoin-plan", "stage 1 of demo")
        assert _effects(h) == ["p1.txt", "c1.txt", "p2.txt"]
        assert [e["attempt"] for e in h.invocations()] == [1, 2, 3]
    finally:
        h.cleanup()


def test_placeholders_for_staged_and_stageless_arguments(tmp_path):
    steps = [_write("$stagedir/out-$task-$stage.txt", "[$context]")]
    h = Harness(tmp_path, _scenario({"quoin-plan": [steps]}))
    try:
        _run(h, "quoin-plan", "stage 3 of demo")
        _run(h, "quoin-plan", "demo")
        assert _effects(h) == [
            ".workflow_artifacts/demo/stage-3/out-demo-3.txt",
            ".workflow_artifacts/demo/out-demo-.txt",
        ]
        assert (h.cwd / ".workflow_artifacts/demo/stage-3/out-demo-3.txt").read_text() == "[]"
    finally:
        h.cleanup()


def test_context_suffix_before_the_marker(tmp_path):
    h = Harness(tmp_path, _scenario({"quoin-plan": [[_write("c.txt", "$context")]]}))
    try:
        _run(h, "quoin-plan", "stage 1 of demo (context: a/b.md, c/d.md) (non-interactive run)")
        assert (h.cwd / "c.txt").read_text() == "a/b.md c/d.md"
    finally:
        h.cleanup()


def test_context_suffix_after_the_marker_fails(tmp_path):
    h = Harness(tmp_path, _scenario({"quoin-plan": [[_write("c.txt")]]}))
    try:
        res = _run(h, "quoin-plan", "stage 1 of demo (non-interactive run) (context: a.md)")
        assert res.returncode != 0
        assert _effects(h) == ["argument-order-invalid"]
        assert not (h.cwd / "c.txt").exists()
    finally:
        h.cleanup()


def test_scenario_without_commands_records_the_same_fields(tmp_path):
    h = Harness(tmp_path, {"attempts": [{"steps": [{"do": "exit", "code": 0}]}]})
    try:
        _run(h, "quoin-plan", "stage 1 of demo")
        entry = h.invocations()[0]
        assert "scenario_key" not in entry
        assert entry["attempt"] == 1
    finally:
        h.cleanup()
