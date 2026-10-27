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
    assert plain == {"pass": 2, "fail": 1, "error": 0, "timeout": 1, "budget_stopped": 1, "not_run": 0, "invalid": 0}
    quoin = t["overall"][QUOIN_CELL]["outcomes"]
    assert quoin == {"pass": 3, "fail": 0, "error": 1, "timeout": 0, "budget_stopped": 0, "not_run": 1, "invalid": 0}
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
    assert "provisional" not in md
    assert "Results are invalid" not in md and "Valid: NO" not in md


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
    assert v["provisional_reasons"] == ["9 rows await judging (run --judge-only) and count as errors until then"]
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


# --- follow-ups: guarded section, pending rows, pricing, escaping, caps ------

def test_named_but_missing_suite_is_reported_not_dropped(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    suite_path.unlink()
    summary = aggregate.aggregate_run(run_dir, run_id)
    assert summary["tiered"] == {"error": "suite file not found: %s" % suite_path}
    assert "suite file not found" in (run_dir / run_id / "summary.md").read_text()


def test_missing_tiered_import_is_recorded_not_raised(tmp_path, monkeypatch):
    run_dir, run_id, _ = build_tiered_run(tmp_path)
    monkeypatch.setattr(tiered_report, "read_manifest", lambda p: (_ for _ in ()).throw(RuntimeError("x" * 500)))
    summary = aggregate.aggregate_run(run_dir, run_id)
    assert summary["tiered"]["error"].startswith("RuntimeError: xxx")
    assert len(summary["tiered"]["error"]) == 200


def test_pending_row_counts_as_error_and_the_banner_says_so(tmp_path):
    run_dir, run_id, _ = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    (run_path / PLAIN_CELL / "e1" / "session.json").write_text(json.dumps({"judge_required": True}))
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((run_path / "summary.json").read_text())["tiered"]
    assert t["tiers"]["easy"][PLAIN_CELL]["passes"] == 0
    assert t["tiers"]["easy"][PLAIN_CELL]["outcomes"]["error"] == 1
    assert "count as errors until then" in (run_path / "summary.md").read_text()


def test_runtime_cost_must_be_a_finite_number(tmp_path):
    usage = {OPUS: {"inputTokens": 10, "outputTokens": 10}}
    for bad in (float("nan"), float("inf")):
        task_dir = write_row(tmp_path, "quoin-claude", "e1", verdict="pass", wall=1,
                             model_usage=usage, cost=bad)
        row = load_row(task_dir, OPUS, load_pricing())
        assert row["usd_source"] == "estimated" and row["usd"] == row["usd"] < float("inf")


def test_empty_and_all_zero_usage_buckets_are_not_priceable(tmp_path):
    from quoin.benchmarks.harness.stream_metrics import sum_model_usage

    zero = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}
    assert sum_model_usage({OPUS: zero, "x": {}}) is None
    assert sum_model_usage({OPUS: zero, "claude-sonnet-4-5": {"outputTokens": 5}})["output"] == 5
    task_dir = write_row(tmp_path, "quoin-claude", "e1", verdict="pass", wall=1, model_usage={OPUS: zero})
    row = load_row(task_dir, OPUS, load_pricing())
    assert row["usd_source"] == "unavailable" and row["tokens_source"] == "unavailable"


def test_unsafe_suite_task_ids_are_skipped_and_listed(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    suite = json.loads(suite_path.read_text())
    for bad in ("..", "a/b", "x|y"):
        suite["tasks"].append({"id": bad, "tier": "easy", "source": "swebench_verified"})
    suite_path.write_text(json.dumps(suite))
    write_manifest(run_dir / run_id, run_id=run_id, suite_path=suite_path, suite_sha=suite_sha(suite_path),
                   simple_claude_model=OPUS, quoin_claude_model=OPUS, max_retries=0)
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((run_dir / run_id / "summary.json").read_text())["tiered"]
    assert t["validity"]["invalid_task_ids"] == ["..", "a/b", "x|y"]
    assert t["overall"]["n"] == 5
    assert "Skipped suite task ids" in (run_dir / run_id / "summary.md").read_text()


def test_table_cells_escape_pipes_without_touching_the_shared_helper(tmp_path):
    run_path, summary, _ = _aggregate(tmp_path)
    report = summary["tiered"]
    report["pairs"][0]["task_id"] = "a|b"
    seen = []
    tiered_report.render_tiered_markdown(report, lambda rows: seen.extend(rows) or "")
    cells = [c for row in seen for c in row]
    assert "a\\|b" in cells and "a|b" not in cells
    assert aggregate._format_md_table([["a|b"]]) == "| a|b |\n"


def test_quoin_contaminated_arm_is_an_identity_trip(tmp_path):
    run_dir, run_id, _ = build_tiered_run(tmp_path)
    write_row(run_dir / run_id, QUOIN_CELL, "e1", verdict="error", wall=1,
              extra={"failure_reason": "quoin-arm-contaminated"})
    aggregate.aggregate_run(run_dir, run_id)
    v = json.loads((run_dir / run_id / "summary.json").read_text())["tiered"]["validity"]
    assert v["valid"] is False and v["identity_trip_rows"] == ["quoin-claude/e1"]


def test_overshoot_and_permission_denials_are_summed_per_arm(tmp_path):
    run_dir, run_id, _ = build_tiered_run(tmp_path)
    rp = run_dir / run_id
    write_row(rp, QUOIN_CELL, "e1", verdict="pass", wall=1, cost=6.0,
              extra={"cap_usd": 5.5, "overshoot_usd": 0.5, "permission_denial_count": 3})
    write_row(rp, QUOIN_CELL, "e2", verdict="pass", wall=1, cost=5.0,
              extra={"cap_usd": 5.5, "overshoot_usd": 0.0, "permission_denial_count": 0})
    write_row(rp, PLAIN_CELL, "e1", verdict="pass", wall=1, cost=1.0,
              extra={"cap_usd": 5.5, "overshoot_usd": None, "permission_denial_count": 1})
    aggregate.aggregate_run(run_dir, run_id)
    t = json.loads((rp / "summary.json").read_text())["tiered"]
    q = t["overall"][QUOIN_CELL]
    assert q["cap_usd"] == 5.5 and q["overshoot_usd_sum"] == 0.5 and q["rows_over_cap"] == 1
    assert t["permission_denials_by_arm"] == {
        PLAIN_CELL: {"sum": 1, "rows_with_denials": 1},
        QUOIN_CELL: {"sum": 3, "rows_with_denials": 1},
    }
    md = (rp / "summary.md").read_text()
    assert "Overshoot (quoin-claude): cap $5.50 per task, $0.50 above cap in total, 1 rows over cap" in md
    assert "Permission denials: simple-claude 1 (in 1 rows); quoin-claude 3 (in 1 rows)" in md


def test_asymmetric_extra_skills_reach_the_validity_block(tmp_path):
    _, summary, md = _aggregate(tmp_path, asymmetric_extra_skills=["context-tracker", "next_steps 2"])
    assert summary["tiered"]["validity"]["asymmetric_extra_skills"] == ["context-tracker", "next_steps 2"]
    assert "Asymmetry:" in md and "context-tracker, next_steps 2" in md
    _, summary, md = _aggregate(tmp_path / "none")
    assert summary["tiered"]["validity"]["asymmetric_extra_skills"] == [] and "Asymmetry:" not in md


@pytest.mark.parametrize("extra", [None, {"tiers": ["easy"], "asymmetric_extra_skills": ["a", "b"]}])
def test_read_manifest_round_trips_the_real_writer(tmp_path, extra):
    from quoin.benchmarks.scripts import run_benchmark

    suite = tmp_path / "suite.json"
    suite.write_text("{}")
    run_benchmark._write_manifest(tmp_path, "r-1", suite, ["simple-claude", "quoin-claude"], 1, False,
                                  tmp_path, extra=extra)
    m = read_manifest(tmp_path / "r-1")
    assert m["run_id"] == "r-1" and m["cells"] == ["simple-claude", "quoin-claude"]
    assert m["suite_sha"] and m["max_retries"] == "0"
    if extra:
        assert m["tiers"] == ["easy"] and m["asymmetric_extra_skills"] == ["a", "b"]


def test_retries_with_judge_only_exits_2(monkeypatch):
    import sys

    from quoin.benchmarks.scripts import run_benchmark

    monkeypatch.setattr(sys, "argv", ["run_benchmark.py", "--suite", "tiered-small", "--run-id", "R",
                                      "--retries", "1", "--judge-only"])
    with pytest.raises(SystemExit) as exc:
        run_benchmark.main()
    assert exc.value.code == 2


def test_judge_only_passes_the_suite_path_to_the_report(tmp_path, monkeypatch):
    from quoin.benchmarks.harness import runner
    from quoin.benchmarks.harness.judge import JudgeResult
    from quoin.benchmarks.harness.session_state import write_session_json
    from quoin.benchmarks.scripts import run_benchmark

    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    (run_path / "run-manifest.yaml").unlink()
    for arm in (PLAIN_CELL, QUOIN_CELL):
        for task_id in ("e1", "e2", "m1", "m2", "l1"):
            task_dir = run_path / arm / task_id
            if task_dir.is_dir():
                (task_dir / "judge.json").unlink(missing_ok=True)
                write_session_json(task_dir, {"judge_required": True, "judge_final": False})

    def stub(task_id, task_dir, run_id, evidence_dir=None, invocation_extra=None,
             task_spec=None, judge_context=None):
        return JudgeResult(task_id, "swebench_verified", "pass", "ev", 0.0, extra={"judge_final": True})

    monkeypatch.setattr(runner, "judge_task", stub)
    suite = json.loads(suite_path.read_text())["tasks"]
    assert run_benchmark._judge_only_main(suite_path, suite, run_dir, run_id) == 0
    t = json.loads((run_path / "summary.json").read_text())["tiered"]
    assert t["overall"][PLAIN_CELL]["passes"] == 5 and t["overall"][QUOIN_CELL]["passes"] == 4
    assert t["validity"]["final"] is True


def test_run_benchmark_passes_the_suite_path_to_aggregate(tmp_path, monkeypatch):
    from quoin.benchmarks.harness import aggregate as agg
    from quoin.benchmarks.harness import runner
    from quoin.benchmarks.scripts import run_benchmark

    run_dir = tmp_path / "runs"
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({"tasks": []}))
    seen = {}

    class _Cell:
        task_results = []
        budget_stopped = False

    monkeypatch.setattr(runner, "run_cell", lambda **k: _Cell())
    monkeypatch.setattr(agg, "aggregate_run", lambda *a, **k: seen.update(k) or {})
    try:
        run_benchmark.run_benchmark(suite_path, ["simple-claude"], "R", run_dir, max_parallel=1)
    except Exception:
        pass
    assert seen.get("suite_path") == suite_path


# --- model pin, mismatch and unpriced models ---------------------------------

BENCH_MODELS_JSON = json.dumps(
    {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"},
    sort_keys=True,
)


def _validity_of(tmp_path, rows, **manifest):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path, **manifest)
    run_path = run_dir / run_id
    for cell, task_id, usage, cost in rows:
        write_row(run_path, cell, task_id, verdict="pass", wall=1,
                  model_usage=usage, cost=cost)
    aggregate.aggregate_run(run_dir, run_id)
    summary = json.loads((run_path / "summary.json").read_text())
    return summary["tiered"]["validity"], (run_path / "summary.md").read_text()


def test_unpriced_model_invalidates_and_runtime_cost_is_kept(tmp_path):
    usage = {"claude-opus-9-9": {"inputTokens": 10, "outputTokens": 5}}
    v, md = _validity_of(tmp_path, [(QUOIN_CELL, "e1", usage, 2.0)])
    assert v["unpriced_models"] == ["claude-opus-9-9"]
    assert v["valid"] is False
    assert "unpriced model: claude-opus-9-9" in v["invalid_reasons"]
    assert "Valid: NO (unpriced model: claude-opus-9-9)" in md
    assert "not_available" not in md


def test_unpriced_model_is_found_on_estimated_rows_too(tmp_path):
    usage = {"claude-opus-9-9": {"inputTokens": 10, "outputTokens": 5}}
    v, _ = _validity_of(tmp_path, [(QUOIN_CELL, "e1", usage, None)])
    assert v["unpriced_models"] == ["claude-opus-9-9"]


def test_synthetic_bucket_alone_does_not_invalidate(tmp_path):
    usage = {"<synthetic>": {"inputTokens": 10, "outputTokens": 5}}
    v, _ = _validity_of(tmp_path, [(QUOIN_CELL, "e1", usage, 2.0)])
    assert v["unpriced_models"] == [] and v["valid"] is True


def test_model_mismatch_rows_use_the_recorded_pin(tmp_path):
    ok = {"claude-opus-5-5": {"inputTokens": 1}, "claude-sonnet-5-5": {"inputTokens": 1}}
    stray = {"claude-opus-5-5": {"inputTokens": 1}, "claude-sonnet-4-6": {"inputTokens": 1}}
    no_top = {"claude-sonnet-5-5": {"inputTokens": 1}}
    rows = [(QUOIN_CELL, "e1", ok, 1.0), (QUOIN_CELL, "e2", stray, 1.0),
            (QUOIN_CELL, "m1", no_top, 1.0)]
    v, md = _validity_of(tmp_path, rows, effort="high", bench_models=BENCH_MODELS_JSON,
                         model_override=None)
    assert v["model_mismatch_rows"] == ["quoin-claude/e2", "quoin-claude/m1"]
    assert v["valid"] is True
    assert v["effort"] == "high"
    assert v["bench_models"]["haiku"] == "claude-haiku-5-5"
    assert "Effort: high" in md


def test_model_override_changes_the_effective_top_level_model(tmp_path):
    usage = {"claude-opus-4-7": {"inputTokens": 1}}
    v, _ = _validity_of(tmp_path, [(QUOIN_CELL, "e1", usage, 1.0)],
                        bench_models=BENCH_MODELS_JSON, model_override="claude-opus-4-7")
    assert v["model_mismatch_rows"] == []


def test_manifest_without_a_pin_skips_the_mismatch_check(tmp_path):
    usage = {"claude-opus-4-7": {"inputTokens": 1}}
    v, md = _validity_of(tmp_path, [(QUOIN_CELL, "e1", usage, 1.0)])
    assert v["model_mismatch_rows"] == [] and v["model_mismatch_checked"] is False
    assert "not recorded" in md


def test_haiku_estimates_are_flagged_as_a_lower_bound(tmp_path):
    usage = {"claude-haiku-5-5": {"inputTokens": 1000, "outputTokens": 10}}
    v, md = _validity_of(tmp_path, [(QUOIN_CELL, "e1", usage, None)])
    assert v["estimate_lower_bound_rows"] == ["quoin-claude/e1"]
    assert "Haiku 5.5 long-prompt tier not applied to table estimates; runtime cost is authoritative" in md


def test_observed_models_are_read_back_from_the_manifest(tmp_path):
    observed = json.dumps({QUOIN_CELL: ["claude-opus-5-5"], PLAIN_CELL: ["claude-opus-5-5"]},
                          sort_keys=True)
    v, md = _validity_of(tmp_path, [], observed_models=observed)
    assert v["observed_models"][QUOIN_CELL] == ["claude-opus-5-5"]
    assert "Observed models (quoin-claude): claude-opus-5-5" in md


# --- the quoin arm did not run the pipeline ----------------------------------------------

import shutil  # noqa: E402

from quoin.benchmarks.harness.session_state import write_session_json  # noqa: E402


def _quoin_session(run_path, task_id, *, terminal="completed", source="swebench_verified"):
    write_session_json(run_path / QUOIN_CELL / task_id, {
        "task_id": task_id, "source": source, "terminal_state": terminal,
    })


def _quoin_row(run_path, task_id, *, verdict="pass", plan, started=False, terminal="completed",
               cost=1.0, wall=10, extra=None):
    write_row(run_path, QUOIN_CELL, task_id, verdict=verdict, wall=wall,
              tokens={"tokens_out": 100}, cost=cost,
              extra={"workflow_artifacts_has_plan": plan, "workflow_pipeline_started": started,
                     "workflow_route": {"route": "full" if plan else "unknown", "profile": None},
                     **(extra or {})})
    _quoin_session(run_path, task_id, terminal=terminal)


def _ac2_run(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    _quoin_row(run_path, "e1", plan=False, cost=4.0)             # judged pass, no plan
    _quoin_row(run_path, "e2", plan=True, cost=2.5)
    _quoin_row(run_path, "m1", verdict="timeout", plan=False, started=True, terminal="timeout")
    _quoin_row(run_path, "m2", verdict="timeout", plan=True, terminal="timeout")
    return run_dir, run_id, run_path


def test_quoin_rows_without_a_plan_are_invalid_and_excluded(tmp_path):
    run_dir, run_id, run_path = _ac2_run(tmp_path)
    aggregate.aggregate_run(run_dir, run_id)
    summary = json.loads((run_path / "summary.json").read_text())
    t = summary["tiered"]
    quoin = t["overall"][QUOIN_CELL]
    assert quoin["n_suite"] == 5 and quoin["n_invalid"] == 2 and quoin["n"] == 3
    assert quoin["outcomes"]["invalid"] == 2
    # pass rate, cost and McNemar leave the excluded rows out
    assert quoin["passes"] == 1
    assert summary["cells"][QUOIN_CELL]["n_invalid"] == 2
    assert summary["cells"][QUOIN_CELL]["n_tasks"] == 2          # m2 and e2 (l1 has no dir)
    assert summary["cells"][QUOIN_CELL]["invalid_total_cost_usd_or_null"] == pytest.approx(5.0)
    assert quoin["usd"]["sum"] == pytest.approx(2.5 + 1.0)
    # pairs, split reasons and the post-plan stop are listed separately
    cp = t["complete_pairs"]
    assert cp["pairs_invalid"] == 2
    v = t["validity"]
    assert v["valid"] is False
    assert v["invalid_reasons"][0] == "quoin arm did not run the pipeline in 2/4 sessions"
    assert v["invalid_quoin_rows"] == ["e1", "m1"]
    assert v["no_pipeline_artifacts_rows"] == ["e1"]
    assert v["stopped_before_plan_rows"] == ["m1"]
    assert v["post_plan_timeout_rows"] == ["m2"]
    assert v["quoin_route_counts"]["full"] == 2
    md = (run_path / "summary.md").read_text()
    assert "Valid: NO (quoin arm did not run the pipeline in 2/4 sessions" in md
    assert "Effective n after excluding invalid rows: plain 5, quoin 3." in md
    assert "Quoin route" in md


def test_post_plan_stop_alone_is_not_invalid(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    _quoin_row(run_path, "e1", verdict="timeout", plan=True, terminal="timeout")
    aggregate.aggregate_run(run_dir, run_id)
    v = json.loads((run_path / "summary.json").read_text())["tiered"]["validity"]
    assert v["post_plan_timeout_rows"] == ["e1"] and v["invalid_quoin_rows"] == []
    assert v["valid"] is True


def test_runner_written_invalid_rows_are_excluded_from_the_legacy_cell(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    _quoin_row(run_path, "e1", verdict="invalid", plan=False)
    aggregate.aggregate_run(run_dir, run_id)
    cell = json.loads((run_path / "summary.json").read_text())["cells"][QUOIN_CELL]
    assert cell["n_invalid"] == 1
    assert cell["n_tasks"] == 3


def test_rule_does_not_touch_rows_without_a_session_or_with_another_source(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    # no session.json: counted exactly as before
    write_row(run_path, QUOIN_CELL, "e1", verdict="pass", wall=5, extra={"workflow_artifacts_has_plan": False})
    # a session from another source: counted as before
    write_row(run_path, QUOIN_CELL, "e2", verdict="pass", wall=5, extra={"workflow_artifacts_has_plan": False})
    _quoin_session(run_path, "e2", source="quoin_scenario")
    aggregate.aggregate_run(run_dir, run_id)
    summary = json.loads((run_path / "summary.json").read_text())
    assert "n_invalid" not in summary["cells"][QUOIN_CELL]
    assert summary["tiered"]["validity"]["invalid_quoin_rows"] == []
    assert summary["tiered"]["validity"]["valid"] is True


def test_v1_scope_guard_keeps_the_golden_bytes(tmp_path, monkeypatch):
    from aggregate_fixtures import build_v1_run

    monkeypatch.setattr(aggregate, "_wilson_ci", lambda k, n, alpha=0.05: (0.1, 0.9))
    monkeypatch.setattr(aggregate, "_mcnemar_pvalue", lambda a, b: 0.5)
    run_dir, run_id, _ = build_v1_run(tmp_path)
    for task_dir in (run_dir / run_id / QUOIN_CELL).iterdir():
        (task_dir / "metrics.json").write_text(
            json.dumps({**json.loads((task_dir / "metrics.json").read_text()),
                        "workflow_artifacts_has_plan": False})
        )
    aggregate.aggregate_run(run_dir, run_id)
    golden = Path(__file__).parent / "fixtures" / "aggregate_golden" / "v1_summary.json"
    assert (run_dir / run_id / "summary.json").read_bytes() == golden.read_bytes()


TIERED_SMALL = (
    Path(__file__).resolve().parents[3] / ".workflow_artifacts" / "quoin-benchmarks" / "runs" / "tiered-small"
)


def _snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*"))


@pytest.mark.skipif(not TIERED_SMALL.is_dir(), reason="no recorded tiered-small run on this machine")
def test_tiered_small_reaggregation(tmp_path):
    before = _snapshot(TIERED_SMALL)
    copy = tmp_path / "runs" / "tiered-small"
    shutil.copytree(TIERED_SMALL, copy)
    quoin_ids = sorted(p.name for p in (copy / QUOIN_CELL).iterdir() if p.is_dir())
    assert len(quoin_ids) == 9
    aggregate.aggregate_run(copy.parent, "tiered-small", suite_path=SMALL_SUITE)
    v = json.loads((copy / "summary.json").read_text())["tiered"]["validity"]
    assert "quoin arm did not run the pipeline in 9/9 sessions" in v["invalid_reasons"]
    assert v["invalid_quoin_rows"] == quoin_ids
    union = sorted(
        v["no_pipeline_artifacts_rows"] + v["stopped_before_plan_rows"] + v["no_pipeline_evidence_rows"]
    )
    assert union == quoin_ids
    assert _snapshot(TIERED_SMALL) == before


# --- patch size ------------------------------------------------------------

def _patch_text(path, removed, added):
    body = "".join(f"-o{i}\n" for i in range(removed)) + "".join(f"+n{i}\n" for i in range(added))
    return (
        f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{removed} +1,{added} @@\n{body}"
    )


def _patch_run(tmp_path):
    run_dir, run_id, suite_path = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    (run_path / "simple-claude" / "e1" / "diff.patch").write_text(
        _patch_text("src/a.py", 2, 5) + _patch_text("tests/test_a.py", 0, 12)
    )
    (run_path / "simple-claude" / "e2" / "diff.patch").write_bytes(b"")
    (run_path / "quoin-claude" / "e1" / "diff.patch").write_text(
        _patch_text("src/a.py", 1, 3) + _patch_text("tests/test_a.py", 0, 20) + _patch_text("docs/x.md", 0, 4)
    )
    aggregate.aggregate_run(run_dir, run_id)
    summary = json.loads((run_path / "summary.json").read_text())
    return run_path, summary, (run_path / "summary.md").read_text()


def test_patch_size_json_per_row_arm_tier_overall_and_delta(tmp_path):
    _, summary, _ = _patch_run(tmp_path)
    t = summary["tiered"]
    e1 = next(p for p in t["pairs"] if p["task_id"] == "e1")
    assert e1[PLAIN_CELL]["patch_size"]["non_test"] == {"added": 5, "removed": 2, "files": 1}
    assert e1[PLAIN_CELL]["patch_size"]["test"] == {"added": 12, "removed": 0, "files": 1}
    assert e1[QUOIN_CELL]["patch_size"]["non_test"] == {"added": 7, "removed": 1, "files": 2}

    easy = t["tiers"]["easy"]
    ps, qs = easy[PLAIN_CELL]["patch_size"], easy[QUOIN_CELL]["patch_size"]
    assert ps["n_measured"] == 2 and ps["n_empty"] == 1 and ps["n_missing"] == 0
    assert ps["non_test"] == {"added": 5, "removed": 2, "files": 1}
    assert ps["mean_lines_changed"] == pytest.approx(19 / 2)
    assert ps["mean_non_test_added"] == pytest.approx(2.5) and ps["mean_test_added"] == pytest.approx(6.0)
    assert qs["n_measured"] == 1 and qs["n_missing"] == 1
    assert qs["test"] == {"added": 20, "removed": 0, "files": 1}
    d = easy["delta"]["patch_size"]
    assert d["non_test"] == {"added": 2, "removed": -1, "files": 1}
    assert d["test"]["added"] == 8
    assert d["mean_lines_changed"] == pytest.approx(28 - 9.5)

    overall = t["overall"]
    assert overall[PLAIN_CELL]["patch_size"]["n_missing"] == 3
    assert overall[QUOIN_CELL]["patch_size"]["n_missing"] == 4


def test_patch_size_none_means_when_nothing_is_measured(tmp_path):
    _, summary, _ = _aggregate(tmp_path)
    large = summary["tiered"]["tiers"]["large"]
    assert large[PLAIN_CELL]["patch_size"]["mean_lines_changed"] is None
    assert large["delta"]["patch_size"]["mean_lines_changed"] is None
    assert large[PLAIN_CELL]["patch_size"]["n_missing"] == 1


def test_patch_size_markdown_tables_and_rule(tmp_path):
    _, _, md = _patch_run(tmp_path)
    assert "### Patch size by tier" in md and "### Patch size per task" in md
    assert "A file counts as a test file when" in md
    assert "Mean lines changed" in md


def test_existing_headings_are_unchanged_by_patch_size(tmp_path):
    _, summary, md = _aggregate(tmp_path)
    for heading in ("### Outcomes by tier", "### Cost, tokens and time by tier", "### Per-task pairs", "### Validity"):
        assert heading in md
    assert "pass_rate" not in json.dumps({k: v for k, v in summary["tiered"].items() if k == "retries"})


def test_unmodified_v3_evidence_copy_renders_patch_tables(tmp_path):
    src = Path(__file__).resolve().parents[3] / ".workflow_artifacts" / "quoin-benchmarks" / "runs" / "tiered-small-v3"
    if not src.is_dir():
        pytest.skip("tiered-small-v3 evidence is not present (gitignored)")
    import shutil

    dest_dir = tmp_path / "runs"
    dest_dir.mkdir()
    shutil.copytree(src, dest_dir / "tiered-small-v3")
    aggregate.aggregate_run(dest_dir, "tiered-small-v3")
    md = (dest_dir / "tiered-small-v3" / "summary.md").read_text()
    assert "### Patch size by tier" in md and "### Patch size per task" in md
