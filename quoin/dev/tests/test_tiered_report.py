"""Tiered plain-vs-quoin report: hand-computed figures, denominators and validity flags."""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from aggregate_fixtures import (
    OPUS, build_tiered_run, suite_sha, write_manifest, write_row,
)
from quoin.benchmarks.harness import aggregate, tiered_report
from quoin.benchmarks.harness.cost import estimate_cost, load_pricing
from quoin.benchmarks.harness.tiered_report import (
    PLAIN_CELL, QUOIN_CELL, RETRIES_STATEMENT, build_tiered_report, load_row, read_manifest,
    wilson_interval,
)

SMALL_SUITE = Path(tiered_report.__file__).resolve().parent.parent / "suite-tiered-small.json"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _aggregate(tmp_path, **manifest):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path, **manifest)
    aggregate.aggregate_run(run_dir, run_id)
    run_path = run_dir / run_id
    summary = json.loads((run_path / "summary.json").read_text())
    return run_path, summary, (run_path / "summary.md").read_text()


def _rebuild(run_path, suite_path):
    suite = json.loads(Path(suite_path).read_text())
    return build_tiered_report(run_path, suite, read_manifest(run_path), suite_sha=suite_sha(suite_path))


# --- statistics -----------------------------------------------------------

@pytest.mark.parametrize("k,n,low,high", [
    (2, 5, 0.117621, 0.769276),
    (3, 5, 0.230724, 0.882379),
    (0, 1, 0.0, 0.793451),
    (9, 9, 0.700855, 1.0),
    (0, 0, 0.0, 1.0),
])
def test_wilson_interval(k, n, low, high):
    got = wilson_interval(k, n)
    assert got[0] == pytest.approx(low, abs=1e-6)
    assert got[1] == pytest.approx(high, abs=1e-6)


# --- the five-task comparison --------------------------------------------

def test_pass_rates_and_outcomes(tmp_path):
    _, summary, _ = _aggregate(tmp_path)
    t = summary["tiered"]
    rate = lambda g, arm: round(100 * g[arm]["final_pass_rate"], 1)  # noqa: E731
    assert [rate(t["tiers"][k], PLAIN_CELL) for k in ("easy", "medium", "large")] == [50.0, 50.0, 0.0]
    assert rate(t["overall"], PLAIN_CELL) == 40.0
    assert [rate(t["tiers"][k], QUOIN_CELL) for k in ("easy", "medium", "large")] == [100.0, 50.0, 0.0]
    assert rate(t["overall"], QUOIN_CELL) == 60.0
    deltas = [t["tiers"][k]["delta"]["final_pass_rate_pp"] for k in ("easy", "medium", "large")]
    assert deltas == pytest.approx([50.0, 0.0, 0.0])
    assert t["overall"]["delta"]["final_pass_rate_pp"] == pytest.approx(20.0)
    for group in (*t["tiers"].values(), t["overall"]):
        for arm in (PLAIN_CELL, QUOIN_CELL):
            assert group[arm]["first_attempt_pass_rate"] == group[arm]["final_pass_rate"]

    plain = t["overall"][PLAIN_CELL]["outcomes"]
    assert plain == {"pass": 2, "fail": 1, "error": 0, "timeout": 1, "budget_stopped": 1, "not_run": 0}
    quoin = t["overall"][QUOIN_CELL]["outcomes"]
    assert quoin == {"pass": 3, "fail": 0, "error": 1, "timeout": 0, "budget_stopped": 0, "not_run": 1}
    assert t["tiers"]["large"][PLAIN_CELL]["n"] == 1
    assert t["tiers"]["large"][QUOIN_CELL]["n"] == 1
    assert t["overall"][PLAIN_CELL]["n"] == 5
    assert t["overall"][PLAIN_CELL]["wilson_ci_95"] == pytest.approx([0.117621, 0.769276], abs=1e-6)
    assert t["overall"][QUOIN_CELL]["wilson_ci_95"] == pytest.approx([0.230724, 0.882379], abs=1e-6)


def test_tokens_usd_and_time(tmp_path):
    _, summary, _ = _aggregate(tmp_path)
    t = summary["tiered"]
    p, q, d = (t["overall"][PLAIN_CELL], t["overall"][QUOIN_CELL], t["overall"]["delta"])
    assert (p["tokens"]["output"]["sum"], p["tokens"]["output"]["mean"]) == (1500, 300.0)
    assert (q["tokens"]["output"]["sum"], q["tokens"]["output"]["mean"]) == (1100, 220.0)
    assert (d["tokens"]["output"]["sum"], d["tokens"]["output"]["mean"]) == (-400, -80.0)
    easy = t["tiers"]["easy"]
    assert (easy[PLAIN_CELL]["tokens"]["output"]["sum"], easy[PLAIN_CELL]["tokens"]["output"]["mean"]) == (300, 150.0)
    assert (easy[QUOIN_CELL]["tokens"]["output"]["sum"], easy[QUOIN_CELL]["tokens"]["output"]["mean"]) == (500, 250.0)
    assert (easy["delta"]["tokens"]["output"]["sum"], easy["delta"]["tokens"]["output"]["mean"]) == (200, 100.0)

    assert p["tokens"]["input"]["sum"] == 3000 and p["tokens"]["input"]["mean"] == 600.0
    assert p["tokens"]["cache_read"]["sum"] == 7500 and p["tokens"]["cache_write"]["sum"] == 4500
    assert q["tokens"]["input"]["sum"] == 2200 and q["tokens"]["input"]["mean"] == 440.0
    assert q["tokens"]["cache_read"]["sum"] == 5500 and q["tokens"]["cache_read"]["mean"] == 1100.0
    assert q["tokens"]["cache_write"]["sum"] == 3300 and q["tokens"]["cache_write"]["mean"] == 660.0

    assert p["tokens_source_counts"] == {"model_usage": 0, "usage": 5, "unavailable": 0}
    assert q["tokens_source_counts"] == {"model_usage": 3, "usage": 0, "unavailable": 2}

    assert (p["usd"]["sum"], p["usd"]["mean"]) == (pytest.approx(15.5), pytest.approx(3.1))
    assert (q["usd"]["sum"], q["usd"]["mean"]) == (pytest.approx(7.5), pytest.approx(1.5))
    assert (d["usd"]["sum"], d["usd"]["mean"]) == (pytest.approx(-8.0), pytest.approx(-1.6))
    assert q["usd_source_counts"] == {"runtime": 3, "estimated": 0, "unavailable": 2}

    assert (p["wall_clock_s"]["sum"], p["wall_clock_s"]["mean"]) == (150, 30.0)
    assert (q["wall_clock_s"]["sum"], q["wall_clock_s"]["mean"]) == (120, 24.0)
    assert (d["wall_clock_s"]["sum"], d["wall_clock_s"]["mean"]) == (-30, -6.0)
    assert p["tool_errors"] == {"sum": 5, "available_n": 5}
    assert q["tool_errors"] == {"sum": 5, "available_n": 3}


def test_complete_pairs(tmp_path):
    _, summary, _ = _aggregate(tmp_path)
    cp = summary["tiered"]["complete_pairs"]
    assert cp["n_pairs"] == 4 and cp["pairs_not_run"] == 1
    assert cp[PLAIN_CELL]["passes"] == 2 and cp[PLAIN_CELL]["pass_rate"] == 0.5
    assert cp[QUOIN_CELL]["passes"] == 3 and cp[QUOIN_CELL]["pass_rate"] == 0.75
    assert cp["delta"]["pass_rate_pp"] == pytest.approx(25.0)
    assert (cp[PLAIN_CELL]["usd_sum"], cp[QUOIN_CELL]["usd_sum"]) == (pytest.approx(10.0), pytest.approx(7.5))
    assert cp["delta"]["usd_sum"] == pytest.approx(-2.5)
    assert (cp[PLAIN_CELL]["tokens_sum"]["output"], cp[QUOIN_CELL]["tokens_sum"]["output"]) == (1000, 1100)
    assert cp["delta"]["tokens_sum"]["output"] == 100
    assert (cp[PLAIN_CELL]["wall_clock_s_sum"], cp[QUOIN_CELL]["wall_clock_s_sum"]) == (100, 120)
    assert cp["delta"]["wall_clock_s_sum"] == 20


def test_structure_flags_and_pairs(tmp_path):
    _, summary, _ = _aggregate(tmp_path)
    t = summary["tiered"]
    assert list(t)[:3] == ["schema_version", "arms", "denominator"]
    assert t["denominator"] == "suite" and t["tiers_filter"] is None
    assert t["retries"] == 0 and t["final_equals_first_attempt"] is True
    assert t["statement"] == RETRIES_STATEMENT
    assert list(t["tiers"]) == ["easy", "medium", "large"]
    assert [e["task_id"] for e in t["pairs"]] == ["e1", "e2", "m1", "m2", "l1"]
    v = t["validity"]
    assert v["valid"] is True and v["final"] is True
    assert v["suite_sha_match"] is True
    assert v["arm_config_mode"] == "not recorded" and v["contamination_checks"] == "not recorded"
    assert v["models"][QUOIN_CELL] == sorted(["claude-opus-4-7", "claude-sonnet-4-6"])
    m2 = t["pairs"][3]
    assert m2[QUOIN_CELL]["verdict"] == "error" and m2[QUOIN_CELL]["tokens_source"] == "unavailable"
    assert m2[QUOIN_CELL]["usd_source"] == "unavailable"
    l1 = t["pairs"][4]
    assert l1[QUOIN_CELL]["verdict"] == "not_run" and l1["tree_sha_match"] is None
    assert t["caveat"] == "Caveat: n=5 tasks (easy 2, medium 2, large 1) is directional, not statistically significant."


def test_markdown_sections(tmp_path):
    _, summary, md = _aggregate(tmp_path)
    assert "## Tiered comparison: plain (simple-claude) vs quoin (quoin-claude)" in md
    for heading in ("### Outcomes by tier", "### Cost, tokens and time by tier",
                    "### Complete pairs", "### Per-task pairs", "### Validity"):
        assert heading in md
    assert md.count("| delta") >= 8
    for task in ("e1", "e2", "m1", "m2", "l1"):
        assert "| %s " % task in md.split("### Per-task pairs")[1]
    assert RETRIES_STATEMENT in md
    assert "Caveat: n=5 tasks (easy 2, medium 2, large 1) is directional, not statistically significant." in md
    complete = md.split("### Complete pairs")[1].split("###")[0]
    assert "$10.00" in complete and "$7.50" in complete and "-2.50" in complete
    assert "Valid: yes\n" in md or "Valid: yes  \n" in md
    assert "provisional" not in md and "invalid" not in md


def test_complete_pairs_block_is_absent_when_every_pair_ran(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    write_row(run_dir / run_id, QUOIN_CELL, "l1", verdict="fail", wall=5, tokens={"tokens_out": 1})
    aggregate.aggregate_run(run_dir, run_id)
    md = (run_dir / run_id / "summary.md").read_text()
    assert "### Complete pairs" not in md
    assert json.loads((run_dir / run_id / "summary.json").read_text())["tiered"]["complete_pairs"]["pairs_not_run"] == 0


# --- denominators ---------------------------------------------------------

def test_real_suite_denominators_with_two_rows_written(tmp_path):
    run_path = tmp_path / "run-real"
    run_path.mkdir()
    write_row(run_path, PLAIN_CELL, "verified_easy_000", verdict="pass", wall=1, tokens={"tokens_out": 5})
    write_row(run_path, QUOIN_CELL, "verified_easy_000", verdict="fail", wall=1, tokens={"tokens_out": 5})
    write_manifest(run_path, run_id="run-real", suite_path=SMALL_SUITE)
    aggregate.aggregate_run(tmp_path, "run-real")
    t = json.loads((run_path / "summary.json").read_text())["tiered"]
    assert [t["tiers"][k]["n"] for k in ("easy", "medium", "large")] == [4, 3, 2]
    assert t["overall"][PLAIN_CELL]["n"] == 9
    not_run = sum(t["overall"][arm]["outcomes"]["not_run"] for arm in (PLAIN_CELL, QUOIN_CELL))
    assert not_run == 16
    assert t["overall"][PLAIN_CELL]["passes"] == 1
    assert t["caveat"] == "Caveat: n=9 tasks (easy 4, medium 3, large 2) is directional, not statistically significant."


def test_manifest_tiers_filter(tmp_path):
    _, summary, md = _aggregate(tmp_path, tiers=["easy"])
    t = summary["tiered"]
    assert t["tiers_filter"] == ["easy"]
    assert list(t["tiers"]) == ["easy"]
    assert t["overall"][PLAIN_CELL]["n"] == 2
    assert [e["task_id"] for e in t["pairs"]] == ["e1", "e2"]


# --- validity flags -------------------------------------------------------

def test_identity_trip_makes_the_run_invalid_and_banner_comes_first(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    write_row(run_dir / run_id, PLAIN_CELL, "e1", verdict="error", wall=1,
              extra={"failure_reason": "plain-arm-contaminated"})
    aggregate.aggregate_run(run_dir, run_id)
    summary = json.loads((run_dir / run_id / "summary.json").read_text())
    v = summary["tiered"]["validity"]
    assert v["valid"] is False and v["identity_trip_rows"] == ["simple-claude/e1"]
    md = (run_dir / run_id / "summary.md").read_text()
    section = md.split("## Tiered comparison")[1]
    assert section.index("**Results are invalid:**") < section.index("|")
    assert "Valid: NO" in md


def test_suite_sha_mismatch_is_invalid(tmp_path):
    _, summary, _ = _aggregate(tmp_path, suite_sha="0123456789abcdef")
    v = summary["tiered"]["validity"]
    assert v["suite_sha_match"] is False and v["valid"] is False


def test_unknown_suite_sha_is_unknown_not_a_mismatch(tmp_path):
    _, summary, _ = _aggregate(tmp_path, suite_sha="unknown")
    v = summary["tiered"]["validity"]
    assert v["suite_sha_match"] is None and v["valid"] is True


def test_tree_sha_mismatch_and_stray_directory(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    write_row(run_path, PLAIN_CELL, "e1", verdict="pass", wall=1, extra={"tree_sha": "aaa"})
    write_row(run_path, QUOIN_CELL, "e1", verdict="pass", wall=1, extra={"tree_sha": "bbb"})
    write_row(run_path, PLAIN_CELL, "e2", verdict="pass", wall=1, extra={"tree_sha": "same"})
    write_row(run_path, QUOIN_CELL, "e2", verdict="pass", wall=1, extra={"tree_sha": "same"})
    (run_path / PLAIN_CELL / "stray_task").mkdir()
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((run_path / "summary.json").read_text())["tiered"]
    assert t["validity"]["tree_sha_mismatches"] == ["e1"]
    assert t["validity"]["unexpected_task_dirs"] == ["simple-claude/stray_task"]
    by_id = {e["task_id"]: e for e in t["pairs"]}
    assert by_id["e1"]["tree_sha_match"] is False and by_id["e2"]["tree_sha_match"] is True


def test_garbled_judge_file_is_unreadable_and_not_final(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    (run_path / PLAIN_CELL / "e1" / "judge.json").write_text("{not json")
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((run_path / "summary.json").read_text())["tiered"]
    v = t["validity"]
    assert v["unreadable_rows"] == ["simple-claude/e1"] and v["final"] is False
    assert t["pairs"][0][PLAIN_CELL]["verdict"] == "error"


def test_unknown_verdict_is_an_error_row_but_not_unreadable(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    write_row(run_path, PLAIN_CELL, "e1", verdict="unknown", wall=1)
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((run_path / "summary.json").read_text())["tiered"]
    v = t["validity"]
    assert v["unknown_verdict_rows"] == ["simple-claude/e1"]
    assert v["unreadable_rows"] == [] and v["final"] is False
    assert t["pairs"][0][PLAIN_CELL]["verdict"] == "error"


def test_rows_awaiting_judging_make_results_provisional(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    for task_dir in sorted(run_path.glob("*/*")):
        if task_dir.is_dir():
            (task_dir / "session.json").write_text(json.dumps({"judge_required": True}))
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((run_path / "summary.json").read_text())["tiered"]
    v = t["validity"]
    assert len(v["judge_pending_rows"]) == 9
    assert v["unreadable_rows"] == [] and v["final"] is False and v["valid"] is True
    assert v["provisional_reasons"] == ["9 rows await judging (run --judge-only)"]
    md = (run_path / "summary.md").read_text()
    section = md.split("## Tiered comparison")[1]
    assert section.index("**Results are provisional:**") < section.index("|")
    assert "Valid: yes (provisional)" in md
    assert "Valid: yes\n" not in md and "Valid: yes  \n" not in md


def test_systemic_error_rows_make_results_provisional(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    write_row(run_dir / run_id, PLAIN_CELL, "e1", verdict="error", wall=1,
              extra={"failure_reason": "auth-failure", "session_errored": True})
    aggregate.aggregate_run(run_dir, run_id)
    v = json.loads((run_dir / run_id / "summary.json").read_text())["tiered"]["validity"]
    assert v["systemic_error_rows"] == ["simple-claude/e1"]
    assert v["session_errored_rows"] == ["simple-claude/e1"]
    assert v["final"] is False and v["valid"] is True


# --- cost fallbacks and failure containment ------------------------------

def test_missing_runtime_cost_is_estimated_from_model_usage(tmp_path):
    run_path = tmp_path / "r"
    run_path.mkdir()
    bucket = {"inputTokens": 1000, "outputTokens": 2000, "cacheReadInputTokens": 30000,
              "cacheCreationInputTokens": 4000}
    write_row(run_path, QUOIN_CELL, "e1", verdict="pass", wall=1, model_usage={OPUS: bucket})
    row = load_row(run_path / QUOIN_CELL / "e1", None, load_pricing())
    expected = float(estimate_cost(OPUS, 1000, 2000, 4000, 30000, load_pricing()))
    assert row["usd_source"] == "estimated" and row["usd"] == expected
    json.dumps(row)


def test_estimate_flows_through_aggregate_run(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    bucket = {"inputTokens": 1000, "outputTokens": 2000, "cacheReadInputTokens": 30000,
              "cacheCreationInputTokens": 4000}
    write_row(run_dir / run_id, QUOIN_CELL, "e1", verdict="pass", wall=1, model_usage={OPUS: bucket})
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((run_dir / run_id / "summary.json").read_text())["tiered"]
    entry = t["pairs"][0][QUOIN_CELL]
    assert entry["usd_source"] == "estimated"
    assert entry["usd"] == float(estimate_cost(OPUS, 1000, 2000, 4000, 30000, load_pricing()))


def test_usage_only_row_without_a_model_has_no_estimate(tmp_path):
    run_path = tmp_path / "r"
    run_path.mkdir()
    write_row(run_path, PLAIN_CELL, "e1", verdict="pass", wall=1, tokens={"tokens_in": 5, "tokens_out": 6})
    for model in (None, ""):
        row = load_row(run_path / PLAIN_CELL / "e1", model, load_pricing())
        assert row["usd_source"] == "unavailable" and row["usd"] == 0.0
        assert row["tokens_source"] == "usage"
    row = load_row(run_path / PLAIN_CELL / "e1", OPUS, load_pricing())
    assert row["usd_source"] == "estimated"


def test_row_without_tokens_never_becomes_a_zero_estimate(tmp_path):
    run_path = tmp_path / "r"
    run_path.mkdir()
    write_row(run_path, PLAIN_CELL, "e1", verdict="error", wall=1)
    row = load_row(run_path / PLAIN_CELL / "e1", OPUS, load_pricing())
    assert row["usd_source"] == "unavailable" and row["tokens_source"] == "unavailable"


def test_a_missing_directory_is_a_not_run_row():
    row = load_row(None, OPUS, {})
    assert row["verdict"] == "not_run" and row["ran"] is False
    assert row["tokens_source"] == "unavailable" and row["usd_source"] == "unavailable"


def test_tool_errors_unavailable_when_counter_keys_are_missing(tmp_path):
    run_path = tmp_path / "r"
    run_path.mkdir()
    write_row(run_path, PLAIN_CELL, "e1", verdict="error", wall=1)
    row = load_row(run_path / PLAIN_CELL / "e1", None, {})
    assert row["tool_errors_available"] is False and row["tool_errors"] == 0


def test_unreadable_suite_is_recorded_and_aggregate_still_returns(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    suite_path.write_text("{broken")
    summary = aggregate.aggregate_run(run_dir, run_id)
    assert summary["tiered"]["error"]
    assert "## Tiered comparison" in (run_dir / run_id / "summary.md").read_text()
    json.loads((run_dir / run_id / "summary.json").read_text())


def test_a_rendering_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)

    def boom(report, format_table):
        raise RuntimeError("render failed")

    monkeypatch.setattr(tiered_report, "render_tiered_markdown", boom)
    summary = aggregate.aggregate_run(run_dir, run_id)
    assert summary["tiered"] == {"error": "RuntimeError: render failed"}


def test_read_manifest_round_trip(tmp_path):
    write_manifest(tmp_path, run_id="r-1", n=3, nothing=None, tiers=["easy", "large"], model=OPUS)
    m = read_manifest(tmp_path)
    assert m["run_id"] == "r-1" and m["n"] == "3" and m["nothing"] is None
    assert m["tiers"] == ["easy", "large"] and m["model"] == OPUS
    assert read_manifest(tmp_path / "missing") == {}
