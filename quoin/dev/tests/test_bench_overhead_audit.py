"""Fixed-overhead audit of quoin-claude sessions against plain ones."""
from __future__ import annotations

import json

import pytest

from quoin.benchmarks.harness import overhead_audit as oa

MODEL = "claude-opus-5-5"


def _assistant(mid, cache_read, cache_write=0, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent,
            "timestamp": "2026-11-10T10:00:00.000Z",
            "message": {"id": mid, "model": MODEL, "content": [{"type": "text", "text": "x"}],
                        "usage": {"input_tokens": 2, "output_tokens": 10,
                                  "cache_read_input_tokens": cache_read,
                                  "cache_creation_input_tokens": cache_write}}}


def _row(cell_dir, task, skills, events, billed, verdict, totals=None):
    row = cell_dir / task
    row.mkdir(parents=True)
    init = {"type": "system", "subtype": "init", "skills": ["s"] * skills,
            "slash_commands": ["c"] * (skills + 3)}
    hook = {"type": "system", "subtype": "hook_response", "hook_name": "SessionStart:startup",
            "output": "abc", "stdout": "abc", "stderr": ""}
    lines = [init, hook] + events
    (row / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in lines) + "\n")
    (row / "metrics.json").write_text(json.dumps({"model_usage_totals": dict(totals or {}, cost_usd=billed)}))
    (row / "judge.json").write_text(json.dumps({"verdict": verdict}))


@pytest.fixture
def run_dir(tmp_path):
    run = tmp_path / "pro-x"
    quoin, plain = run / oa.QUOIN_CELL, run / oa.PLAIN_CELL
    # quoin: first call 1000 + 500 = 1502 tokens, 3 main turns (one repeated
    # per content block), one subagent turn; plain: 1002 tokens, 2 turns.
    _row(quoin, "t1", 50, [_assistant("a", 1000, 500), _assistant("a", 1000, 500),
                           _assistant("b", 1500), _assistant("c", 1500),
                           _assistant("s1", 800, parent="tool1")], 2.0, "pass")
    # plain carries billed totals; its streamed output (10 per message) is partial.
    _row(plain, "t1", 19, [_assistant("p", 1000), _assistant("q", 1000)], 1.0, "pass",
         totals={"input": 4, "output": 900, "cache_read": 2000, "cache_write": 0})
    _row(quoin, "only-quoin", 50, [_assistant("z", 10)], 1.0, "fail")
    return run


def test_pairs_profile_first_call_turns_and_cost(run_dir):
    pairs = oa.pair_run(run_dir)
    assert [p["task"] for p in pairs] == ["t1"]
    quoin, plain = pairs[0]["quoin"], pairs[0]["plain"]
    assert quoin["first_context_tokens"] == 1502 and plain["first_context_tokens"] == 1002
    assert (quoin["main_turns"], quoin["subagent_turns"]) == (3, 1)
    assert quoin["tokens"]["cache_read"] == 1000 + 1500 + 1500 + 800
    assert (quoin["skills_listed"], plain["slash_commands_listed"]) == (50, 22)
    assert quoin["hook_output_chars"] == 3 and quoin["hooks_seen"] == ["SessionStart:startup"]
    assert quoin["tokens_source"] == "transcript"
    assert (plain["tokens_source"], plain["tokens"]["output"]) == ("model_usage", 900)
    assert pairs[0]["first_context_delta"] == 500
    assert pairs[0]["prefix_reread_tokens"] == 500 * 2

    summary = oa.summarize_pairs(pairs)
    assert summary["n"] == 1
    assert summary["ratio"]["billed_cost_usd"] == pytest.approx(2.0)
    assert summary["ratio"]["main_turns"] == pytest.approx(1.5)
    assert (summary["quoin"]["passes"], summary["plain"]["passes"]) == (1, 1)


def test_static_sources_and_report(run_dir, tmp_path):
    root = tmp_path / "q"
    run = root / "adapters" / "claude" / "skills" / "run"
    run.mkdir(parents=True)
    (root / "CLAUDE.slim.md").write_text("x" * 400)
    (run / "SKILL.md").write_text('---\nname: run\ndescription: "abcd"\n---\n' + "y" * 1000)
    (run / "light-pipeline.md").write_text("z" * 800)

    report = oa.audit(run_dir, root, pricing={})
    rows = {r["source"]: r for r in report["sources"]}
    assert rows["CLAUDE.md as deployed (slim)"]["tokens_est"] == 100
    assert rows["CLAUDE.md as deployed (slim)"]["share_of_first_context_delta"] == pytest.approx(0.2)
    assert rows["run light-pipeline.md"]["share_of_first_context_delta"] is None
    assert "run reference.md" not in rows
    listing = next(r for r in report["sources"] if r["source"].startswith("skill listing (1 "))
    assert listing["chars"] == len("run") + len("abcd")

    text = oa.render_markdown(report)
    assert "Paired tasks: 1." in text and "| billed cost (USD) | 2.000 | 1.000 | 2.00x |" in text
