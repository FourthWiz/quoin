"""Per-phase time, token and cost breakdown of quoin-claude benchmark rows."""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from quoin.benchmarks.harness import phase_report as pr
from quoin.benchmarks.harness import tiered_report
from quoin.benchmarks.harness.result_writer import RunResult, write_run_result

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).parent / "fixtures" / "bench_phase_report" / "verified_large_012"
LIGHT_V1 = (REPO_ROOT / ".workflow_artifacts" / "quoin-benchmarks" / "runs"
            / "tiered-balanced-light-v1" / "quoin-claude")
T0 = datetime.datetime(2026, 11, 10, 10, 0, 0, tzinfo=datetime.timezone.utc)
MODEL = "claude-opus-5-5"


def _ts(seconds):
    return (T0 + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _usage(output=100):
    return {"input_tokens": 10, "output_tokens": output,
            "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}


def _assistant(mid, at, parent=None, agents=(), output=100):
    content = [{"type": "tool_use", "name": "Agent", "id": tid, "input": {"description": desc}}
               for tid, desc in agents] or [{"type": "text", "text": "x"}]
    return {"type": "assistant", "timestamp": _ts(at), "parent_tool_use_id": parent,
            "message": {"id": mid, "model": MODEL, "usage": _usage(output), "content": content}}


def _user(at, parent=None, result_for=None):
    content = [{"type": "tool_result", "tool_use_id": result_for}] if result_for else []
    return {"type": "user", "timestamp": _ts(at), "parent_tool_use_id": parent,
            "message": {"content": content}}


def _row(tmp_path, events, markers=None, wall=None, billed=None):
    row = tmp_path / "row"
    row.mkdir()
    (row / "transcript.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    metrics = {"wall_clock_seconds": wall if wall is not None else 10_000}
    if billed is not None:
        metrics["model_usage_totals"] = {"cost_usd": billed}
    (row / "metrics.json").write_text(json.dumps(metrics))
    if markers is not None:
        task = row / pr.EVIDENCE_DIR / "fix-thing"
        task.mkdir(parents=True)
        (task / "phases.log").write_text(
            "".join(f"{_ts(at)[:19]}Z {name}\n" for at, name in markers))
    return row


def test_parse_phases_log_keeps_valid_markers_only():
    text = ("2026-11-10T10:00:00Z setup\n"
            "2026-11-10T10:00:05Z discover skipped\n"
            "2026-11-10T10:00:09Z 2:implement\n"
            "garbage line\n"
            "2026-11-10T10:00:12Z plan\n")
    got = pr.parse_phases_log(text)
    assert [(m["phase"], m["stage"], m["skipped"]) for m in got] == [
        ("setup", None, False), ("discover", None, True), ("implement", 2, False)]
    assert got[2]["ts"] - got[0]["ts"] == 9


def test_markers_attribute_messages_and_subagents_to_the_spawning_phase(tmp_path):
    events = [
        _assistant("m1", 1),
        _assistant("m2", 12, agents=[("t1", "anything")]),
        _assistant("m2", 12, agents=[("t1", "anything")]),  # same message, second block
        _assistant("s1", 50, parent="t1"),
        _assistant("m3", 105),
        _assistant("s2", 150, parent="t1"),  # background subagent, still discover's
    ]
    row = _row(tmp_path, events, markers=[(0, "setup"), (10, "discover"), (100, "frame")])
    with open(row / pr.EVIDENCE_DIR / "fix-thing" / "phases.log", "a") as fh:
        fh.write("2026-11-10T10:00:05Z decompose skipped\n")
    report = pr.build_phase_report(row)
    phases = report["phases"]
    assert report["source"] == "markers"
    assert phases["setup"]["tokens"]["output"] == 100
    assert phases["discover"]["tokens"]["output"] == 300  # m2 once, s1, s2
    assert phases["frame"]["tokens"]["output"] == 100
    assert phases["decompose"]["skipped"] and not phases["decompose"]["ran"]
    assert phases["discover"]["ran"]
    # Each gap belongs to the phase active at its later timestamp.
    assert phases["setup"]["seconds"] == pytest.approx(0)
    assert phases["discover"]["seconds"] == pytest.approx(49)  # 1 -> 12 -> 50
    assert phases["frame"]["seconds"] == pytest.approx(100)  # 50 -> 105 -> 150


def test_costs_are_scaled_to_the_billed_total(tmp_path):
    events = [_assistant("m1", 1), _assistant("m2", 2)]
    unscaled = pr.build_phase_report(_row(tmp_path, events))
    recon = unscaled["reconstructed_cost_usd"]
    assert recon > 0 and unscaled["cost_scale"] is None
    assert unscaled["phases"]["setup"]["cost_usd_estimate"] == pytest.approx(recon)

    other = tmp_path / "b"
    other.mkdir()
    scaled = pr.build_phase_report(_row(other, events, billed=round(recon * 2, 6)))
    assert scaled["cost_scale"] == pytest.approx(2, rel=1e-3)
    assert scaled["billed_cost_usd"] == pytest.approx(recon * 2)
    assert scaled["phases"]["setup"]["cost_usd_reconstructed"] == pytest.approx(recon)
    assert scaled["phases"]["setup"]["cost_usd_estimate"] == pytest.approx(recon * 2, rel=1e-3)


def test_costs_are_scaled_per_model_when_model_usage_bills_each(tmp_path):
    events = [_assistant("m1", 1), _assistant("s1", 2, parent="t")]
    events[1]["message"]["model"] = "claude-sonnet-5-5"
    row = _row(tmp_path, events)
    base = pr.build_phase_report(row)
    opus = pr.build_phase_report(row, {"model_usage": {}})["phases"]["setup"]["cost_usd_reconstructed"]
    sonnet = base["reconstructed_cost_usd"] - opus
    metrics = {"model_usage_totals": {"cost_usd": opus * 2 + sonnet * 3},
               "model_usage": {MODEL: {"costUSD": opus * 2}, "claude-sonnet-5-5": {"costUSD": sonnet * 3}}}
    report = pr.build_phase_report(row, metrics)
    assert report["cost_scale_by_model"] == {MODEL: pytest.approx(2, rel=1e-3),
                                             "claude-sonnet-5-5": pytest.approx(3, rel=1e-3)}
    assert report["phases"]["setup"]["cost_usd_estimate"] == pytest.approx(opus * 2, rel=1e-3)
    assert report["phases"]["other"]["cost_usd_estimate"] == pytest.approx(sonnet * 3, rel=1e-3)


def test_durations_are_cut_to_the_wall_clock_largest_gap_first(tmp_path):
    events = [_assistant("m1", 0), _assistant("m2", 10, agents=[("t1", "Run /discover")]),
              _assistant("s1", 3610, parent="t1"), _user(3611, result_for="t1"),
              _assistant("m3", 3640)]
    report = pr.build_phase_report(_row(tmp_path, events, wall=100))
    seconds = {k: v["seconds"] for k, v in report["phases"].items()}
    assert sum(seconds.values()) == pytest.approx(100)
    assert report["timeline_seconds"] == pytest.approx(3640)
    assert report["clamped_seconds"] == pytest.approx(3540)
    # Only the 3600 s gap is cut; the smaller gaps keep their length.
    assert seconds["frame"] == pytest.approx(29)
    assert seconds["discover"] == pytest.approx(10 + 60 + 1)


def test_rows_without_markers_are_inferred_from_agent_descriptions(tmp_path):
    events = [
        _assistant("m1", 0),
        _assistant("m2", 5, agents=[("d", "Discover project memory")]),
        _assistant("d1", 30, parent="d"), _user(31, result_for="d"),
        _assistant("m3", 40, agents=[("c", "Context pack for regression")]),
        _assistant("c1", 60, parent="c"), _user(61, result_for="c"),
        _assistant("m4", 100),
        _assistant("m5", 110, agents=[("r", "Review regression fix diff")]),
        _assistant("r1", 130, parent="r"), _user(131, result_for="r"),
        _assistant("m6", 150),
    ]
    report = pr.build_phase_report(_row(tmp_path, events))
    assert report["source"] == "inferred" and report["marker_count"] == 0
    tokens = {k: v["tokens"]["output"] for k, v in report["phases"].items()}
    # The message that makes an Agent call belongs to the phase it starts.
    assert tokens == {"setup": 100, "discover": 200, "context": 200,
                      "implement": 100, "review": 200, "review-fixes": 100}
    assert report["phases"]["implement"]["seconds"] == pytest.approx(39)  # 61 -> 100


def test_light_v1_fixture_row_regression():
    # verified_large_012 of tiered-balanced-light-v1, trimmed to timestamps,
    # usage and Agent calls: its timestamps span 90 minutes of a 792 s session.
    report = pr.build_phase_report(FIXTURE)
    assert report["source"] == "inferred"
    assert list(report["phases"]) == ["setup", "discover", "context", "implement", "review",
                                      "review-fixes"]
    total = sum(p["seconds"] for p in report["phases"].values())
    assert total == pytest.approx(report["wall_clock_seconds"])
    assert report["clamped_seconds"] > 4000
    assert report["phases"]["discover"]["seconds"] < report["wall_clock_seconds"] / 2
    assert report["billed_cost_usd"] == pytest.approx(2.6639312)
    assert report["cost_scale"] == pytest.approx(1.845, abs=0.01)
    assert sum(p["cost_usd_estimate"] for p in report["phases"].values()) == pytest.approx(2.6639, abs=1e-3)
    biggest = max(report["phases"].items(), key=lambda kv: kv[1]["cost_usd_estimate"])[0]
    assert biggest == "discover"


@pytest.mark.skipif(not LIGHT_V1.is_dir(), reason="tiered-balanced-light-v1 run not on this machine")
def test_light_v1_run_never_exceeds_wall_clock():
    reports = [pr.build_phase_report(d) for d in sorted(LIGHT_V1.iterdir()) if d.is_dir()]
    for report in reports:
        assert sum(p["seconds"] for p in report["phases"].values()) <= report["wall_clock_seconds"] + 0.01


def test_summary_table_reports_means_shares_and_sessions():
    def rep(source, setup_usd, review_usd, review_ran=True):
        return {"source": source, "reconstructed_cost_usd": 1.0, "billed_cost_usd": 2.0, "phases": {
            "setup": {"cost_usd_estimate": setup_usd, "seconds": 10.0, "ran": True},
            "review": {"cost_usd_estimate": review_usd, "seconds": 30.0 if review_ran else 0.0,
                       "ran": review_ran},
        }}

    summary = pr.summarize([rep("markers", 1.0, 3.0), rep("inferred", 1.0, 0.0, review_ran=False)])
    rows = {r["phase"]: r for r in summary["rows"]}
    assert summary["n"] == 2 and summary["sources"] == {"markers": 1, "inferred": 1}
    assert rows["setup"]["usd_mean"] == pytest.approx(1.0)
    assert rows["review"]["usd_pct"] == pytest.approx(60.0)
    assert rows["review"]["seconds_pct"] == pytest.approx(60.0)
    assert rows["review"]["sessions_ran"] == 1

    def fmt(table):
        return "\n".join("|".join(row) for row in table)

    text = "\n".join(pr.render_markdown([("easy", summary), ("overall", summary)], fmt))
    assert "1 with markers, 1 inferred" in text
    assert "overall|review|$1.50|60%|15|60%|1/2" in text


def test_quoin_row_metrics_carry_the_phase_report(tmp_path):
    events = [json.loads(line) for line in (FIXTURE / "transcript.jsonl").read_text().splitlines()]
    result = RunResult(cell="quoin-claude", task_id="verified_large_012", run_id="R",
                       transcript_events=events, wall_clock_seconds=791.96,
                       extra={"model_usage_totals": {"cost_usd": 2.6639312}})
    out = write_run_result(result, tmp_path)
    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["phase_report"]["source"] == "inferred"
    assert metrics["phase_report"]["billed_cost_usd"] == pytest.approx(2.6639312)
    plain = write_run_result(RunResult(cell="simple-claude", task_id="t", run_id="R",
                                       transcript_events=events), tmp_path)
    assert "phase_report" not in json.loads((plain / "metrics.json").read_text())
    # The tiered summary reuses the stored report rather than re-parsing.
    assert tiered_report._phase_report_of(out, {}) == metrics["phase_report"]
