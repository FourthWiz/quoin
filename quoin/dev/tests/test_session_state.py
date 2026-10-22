"""Tests for terminal-state precedence and session-error mapping on the Verified path."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quoin.benchmarks.harness.session_state import (
    classify_session, is_judge_pending, iter_transcript_events,
    read_session_json, write_session_json,
)

FIX = Path(__file__).parent / "fixtures" / "benchmark_streams"


def _events(name):
    return [json.loads(l) for l in (FIX / name).read_text().splitlines() if l.strip()]


def test_auth_failure_is_error_not_judged():
    out = classify_session(None, {}, _events("auth_failure.jsonl"))
    assert (out.terminal_state, out.failure_reason) == ("error", "auth-failure")
    assert out.judge_required is False and out.genuine_assistant_turns == 0


def test_budget_halt_wins_even_when_result_is_error():
    out = classify_session(None, {}, _events("budget_halt.jsonl"))
    assert out.terminal_state == "budget_stopped"
    assert out.failure_reason == "budget-halt-detected"
    assert out.judge_required is False


def test_errored_after_turns_is_still_judged():
    """The fixture result is synthetic: genuine turns followed by an execution error."""
    out = classify_session(None, {}, _events("errored_after_turns.jsonl"))
    assert out.terminal_state == "completed"
    assert out.session_errored is True and out.judge_required is True


def test_timeout_outranks_budget_halt():
    out = classify_session("timeout", {}, _events("budget_halt.jsonl"))
    assert out.terminal_state == "timeout"


def test_cell_error_reason_is_kept():
    out = classify_session("error", {"failure_reason": "budget-cap-unarmed"}, _events("clean_success.jsonl"))
    assert (out.terminal_state, out.failure_reason) == ("error", "budget-cap-unarmed")


def test_zero_events_is_no_assistant_turn():
    out = classify_session(None, {}, [])
    assert (out.terminal_state, out.failure_reason) == ("error", "no-assistant-turn")


def test_clean_success():
    out = classify_session(None, {}, _events("clean_success.jsonl"))
    assert out.terminal_state == "completed"
    assert out.session_errored is False and out.result_seen is True and out.judge_required


def test_turns_without_result_event_complete_unresulted():
    events = [e for e in _events("clean_success.jsonl") if e["type"] != "result"]
    out = classify_session(None, {}, events)
    assert out.terminal_state == "completed" and out.result_seen is False


def test_budget_error_text_alone_is_detected():
    events = _events("clean_success.jsonl")
    events[-1] = {"type": "result", "subtype": "success", "is_error": True,
                  "errors": ["Reached maximum budget ($5)"]}
    assert classify_session(None, {}, events).terminal_state == "budget_stopped"


def test_truncated_lines_are_skipped_and_counted(tmp_path):
    lines = (FIX / "clean_success.jsonl").read_text().splitlines()
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + '\n{"type": "assistant", "mess')
    stats = {}
    out = classify_session(None, {}, iter_transcript_events(path, stats), skipped_lines=0)
    assert stats["skipped_lines"] == 1
    out2 = classify_session(None, {}, iter_transcript_events(path, stats), stats["skipped_lines"])
    assert out.terminal_state == "completed" and out2.skipped_lines >= 1


def test_judge_pending_table(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    assert is_judge_pending(d) is False
    write_session_json(d, {"judge_required": False})
    assert is_judge_pending(d) is False
    write_session_json(d, {"judge_required": True})
    assert is_judge_pending(d) is True
    (d / "judge.json").write_text(json.dumps({"verdict": "error", "judge_final": False}))
    assert is_judge_pending(d) is True
    (d / "judge.json").write_text("{not json")
    assert is_judge_pending(d) is True
    (d / "judge.json").write_text(json.dumps({"verdict": "pass", "judge_final": True}))
    assert is_judge_pending(d) is False
    assert read_session_json(d)["schema_version"] == 1
    assert not (d / "session.json.tmp").exists()
