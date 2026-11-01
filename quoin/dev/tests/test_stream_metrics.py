"""Stream counters: counts on recorded and synthetic streams, tolerance of odd shapes, containment."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from quoin.benchmarks.harness import stream_metrics
from quoin.benchmarks.harness.stream_metrics import EXTRA_KEYS, StreamMetrics, sum_model_usage

FIX = Path(__file__).parent / "fixtures" / "benchmark_streams"
HARNESS = Path(stream_metrics.__file__).resolve().parent


def _events(name):
    return [json.loads(line) for line in (FIX / name).read_text().splitlines() if line.strip()]


def _run(events):
    sm = StreamMetrics()
    for event in events:
        sm.observe(event)
    return sm.as_extra()


def _user(*blocks, parent=None):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}, "parent_tool_use_id": parent}


def _result_block(is_error):
    return {"type": "tool_result", "tool_use_id": "t", "content": "x", "is_error": is_error}


def test_quoin_stream_with_subagent_errors():
    extra = _run(_events("quoin_subagent_tool_errors.jsonl"))
    assert extra["tool_error_count"] == 2
    assert extra["subagent_tool_error_count"] == 1
    assert extra["tool_result_count"] == 7
    assert extra["permission_denial_count"] == 1
    assert extra["tool_error_count_available"] is True
    assert extra["stream_metrics_error"] is None
    totals = extra["model_usage_totals"]
    assert totals["output"] == 12424
    assert totals["cache_write"] == 109410
    assert totals["cache_read"] == 667519
    assert totals["input"] == 29
    assert totals["cost_usd"] == pytest.approx(1.64815075)


def test_plain_stream_has_no_tool_errors():
    extra = _run(_events("plain_no_tool_errors.jsonl"))
    assert extra["tool_error_count"] == 0
    assert extra["subagent_tool_error_count"] == 0
    assert extra["tool_result_count"] == 3
    assert extra["permission_denial_count"] == 0
    assert extra["model_usage_totals"]["output"] == 6727
    assert extra["tool_error_count_available"] is True


def test_malformed_stream_is_tolerated():
    extra = _run(_events("malformed_stream.jsonl"))
    assert extra["tool_error_count"] == 1
    assert extra["permission_denial_count"] == 0
    assert extra["tool_error_count_available"] is True
    assert extra["model_usage"] == {}
    assert extra["model_usage_totals"] is None


@pytest.mark.parametrize("value", ["true", 1, "True", None, False, [True]])
def test_only_a_literal_true_counts_as_an_error(value):
    assert _run([_user(_result_block(value))])["tool_error_count"] == 0


def test_literal_true_counts_in_main_and_subagent_events():
    extra = _run([_user(_result_block(True)), _user(_result_block(True), parent="p")])
    assert extra["tool_error_count"] == 2
    assert extra["subagent_tool_error_count"] == 1


@pytest.mark.parametrize("event", [
    None, 5, "text", [], {"type": "user"}, {"type": "user", "message": None},
    {"type": "user", "message": {"content": "just text"}},
    {"type": "user", "message": {"content": {"type": "tool_result", "is_error": True}}},
    {"type": "user", "message": {"content": [None, 3, "x", ["y"]]}},
    {"type": "assistant", "message": {}},
    {"type": "result", "permission_denials": {"a": 1}},
    {"type": "result", "modelUsage": "oops"},
    {"type": "result", "modelUsage": {"m": ["x"]}},
])
def test_odd_shapes_do_not_raise(event):
    sm = StreamMetrics()
    sm.observe(event)
    extra = sm.as_extra()
    assert extra["tool_error_count"] == 0
    assert extra["tool_error_count_available"] is True


def test_result_without_model_usage():
    extra = _run([{"type": "result", "subtype": "success"}])
    assert extra["model_usage"] is None
    assert extra["model_usage_totals"] is None
    assert extra["permission_denial_count"] == 0


def test_no_result_event_leaves_denials_unknown():
    assert _run([_user(_result_block(False))])["permission_denial_count"] is None


def test_second_result_event_replaces_the_first():
    first = {"type": "result", "permission_denials": [1, 2], "modelUsage": {"a": {"outputTokens": 5}}}
    second = {"type": "result", "permission_denials": [1], "modelUsage": {"b": {"outputTokens": 9}}}
    extra = _run([first, second])
    assert extra["permission_denial_count"] == 1
    assert set(extra["model_usage"]) == {"b"}
    assert extra["model_usage_totals"]["output"] == 9


def test_sum_model_usage_over_two_models():
    totals = sum_model_usage({
        "a": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
              "cacheCreationInputTokens": 4, "costUSD": 0.5},
        "b": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 30,
              "cacheCreationInputTokens": 40, "costUSD": 0.25},
    })
    assert totals == {"input": 11, "output": 22, "cache_read": 33, "cache_write": 44, "cost_usd": 0.75}


def test_sum_model_usage_without_cost_and_for_empty_input():
    assert sum_model_usage({"a": {"outputTokens": 2}})["cost_usd"] is None
    assert sum_model_usage(None) is None
    assert sum_model_usage({}) is None
    assert sum_model_usage([1]) is None


def test_booleans_in_model_usage_are_rejected():
    extra = _run([{"type": "result", "modelUsage": {"a": {"outputTokens": True, "inputTokens": 4, "costUSD": False}}}])
    assert extra["model_usage"] == {"a": {"inputTokens": 4}}
    assert extra["model_usage_totals"]["output"] == 0
    assert extra["model_usage_totals"]["cost_usd"] is None


def test_a_failure_inside_observe_is_contained(monkeypatch):
    sm = StreamMetrics()
    sm.observe(_user(_result_block(True)))

    def boom(self, event):
        raise ValueError("schema drift")

    monkeypatch.setattr(StreamMetrics, "_observe", boom)
    sm.observe(_user(_result_block(True)))
    monkeypatch.undo()
    sm.observe(_user(_result_block(True)))
    extra = sm.as_extra()
    assert extra["tool_error_count"] == 1
    assert extra["tool_error_count_available"] is False
    assert extra["stream_metrics_error"].startswith("ValueError")


def test_a_failure_inside_as_extra_is_contained(monkeypatch):
    sm = StreamMetrics()
    monkeypatch.setattr(stream_metrics, "sum_model_usage", lambda usage: 1 / 0)
    extra = sm.as_extra()
    assert set(extra) == set(EXTRA_KEYS)
    assert extra["tool_error_count_available"] is False
    assert extra["stream_metrics_error"].startswith("ZeroDivisionError")
    assert extra["tool_error_count"] is None


def test_extra_keys_match_as_extra():
    assert set(StreamMetrics().as_extra()) == set(EXTRA_KEYS)
    assert list(StreamMetrics().as_extra()) == list(EXTRA_KEYS)


_BANNED = (".removeprefix(", ".removesuffix(", "functools.cache", "math.lcm")


@pytest.mark.parametrize("name", ["stream_metrics.py", "tiered_report.py"])
def test_python38_compatible(name):
    src = (HARNESS / name).read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 8))
    assert any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    assert not [tok for tok in _BANNED if tok in src]
