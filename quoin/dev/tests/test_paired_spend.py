"""Tests for the paired-run spend checks and ledger rows."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from quoin.benchmarks.harness import paired_spend as ps
from quoin.benchmarks.scripts import spend_ledger


def _res(ledger, clauses):
    return {name: (ok, detail) for name, ok, detail in clauses}


def _write(ledger, run_id, attempt, cap, actual=None, kind="reservation"):
    spend_ledger.append(ledger, {
        "ts": "t", "attempt_id": attempt, "kind": kind, "invocation": "paired",
        "gate_id": "g", "arm": "plain", "cap_usd": cap, "actual_usd": actual,
        "run_id": run_id, "new_ceiling_usd": None, "note": "",
    })


def test_run_scoped_total_ignores_other_runs(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _write(ledger, "pilot", "a1", 5.0)
    _write(ledger, "pilot", "a1", 5.0, actual=4.0, kind="settlement")
    _write(ledger, "main", "b1", 5.5)
    assert spend_ledger.recorded_total(ledger) == pytest.approx(9.5)
    assert spend_ledger.recorded_total(ledger, "main") == pytest.approx(5.5)
    assert spend_ledger.recorded_total(ledger, "pilot") == pytest.approx(4.0)
    assert spend_ledger.recorded_total(ledger, "absent") == 0.0


def test_unsettled_reservation_counts_at_cap(tmp_path):
    ledger = tmp_path / "l.jsonl"
    attempt = ps.reserve(ledger, "r", "t1", "simple-claude", 5.5)
    assert spend_ledger.recorded_total(ledger, "r") == pytest.approx(5.5)
    ps.settle(ledger, attempt, "r", "t1", "simple-claude", 5.5, 2.0)
    assert spend_ledger.recorded_total(ledger, "r") == pytest.approx(2.0)


def test_settle_stores_null_for_bad_actuals(tmp_path):
    ledger = tmp_path / "l.jsonl"
    for index, bad in enumerate([float("nan"), "x", None, -1.0]):
        attempt = ps.reserve(ledger, "r", f"t{index}", "quoin-claude", 5.5)
        ps.settle(ledger, attempt, "r", f"t{index}", "quoin-claude", 5.5, bad)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    settlements = [r for r in rows if r["kind"] == "settlement"]
    assert len(settlements) == 4
    assert all(r["actual_usd"] is None for r in settlements)
    assert all(r["arm"] == "quoin" and r["invocation"] == "paired" for r in rows)
    assert spend_ledger.recorded_total(ledger, "r") == pytest.approx(22.0)


def test_settlement_actual_mapping():
    def result(cost=None, **extra):
        return SimpleNamespace(cost_runtime_usd=cost, extra=extra)

    assert ps.settlement_actual(result(pre_spawn_error=True)) == 0.0
    assert ps.settlement_actual(result(3.25)) == 3.25
    for bad in (float("nan"), "3", None, -2.0):
        assert ps.settlement_actual(result(bad)) is None
    assert ps.settlement_actual(None) is None


def test_worst_case():
    assert ps.worst_case(5.5, 9) == pytest.approx(99.0)
    assert ps.worst_case(6.5, 9) == pytest.approx(117.0)


def _pre(default_ledger, **kw):
    args = dict(ledger=default_ledger, run_id="r", cap=5.5, n_spawns=18, authorised=120.0, suite_ceiling=110.0)
    args.update(kw)
    return _res(default_ledger, ps.preflight_spend(**args))


def test_preflight_passes_together(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOIN_BENCH_CLAUDE_MODEL", raising=False)
    result = _pre(tmp_path / "l.jsonl")
    assert all(ok for ok, _ in result.values()), result


@pytest.mark.parametrize("override,clause", [
    ({"cap": None}, "cap-set"),
    ({"ledger": None}, "ledger-set"),
    ({"authorised": None}, "authorised-set"),
    ({"cap": 6.5}, "suite-ceiling"),
    ({"authorised": 50.0}, "authorised-ceiling"),
])
def test_preflight_each_clause_fails_alone(tmp_path, monkeypatch, override, clause):
    monkeypatch.delenv("QUOIN_BENCH_CLAUDE_MODEL", raising=False)
    result = _pre(tmp_path / "l.jsonl", **override)
    assert result[clause][0] is False


def test_preflight_model_override(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOIN_BENCH_CLAUDE_MODEL", "other-model")
    result = _pre(tmp_path / "l.jsonl")
    assert result["model-override"][0] is False
    assert result["pricing-key"][0] is True
    assert result["suite-ceiling"][0] is True


def test_preflight_ledger_read_error_is_a_failed_clause(tmp_path):
    bad = tmp_path / "l.jsonl"
    bad.write_text("not json\n")
    result = _pre(bad)
    assert result["suite-ceiling"][0] is False
    assert result["authorised-ceiling"][0] is False


def test_pair_precheck_refuses_the_pair_that_would_cross(tmp_path):
    ledger = tmp_path / "l.jsonl"
    cap, ceiling = 5.5, 110.0
    allowed = 0
    for index in range(10):
        ok, _ = ps.pair_precheck(ledger, "r", cap, 2, 500.0, ceiling)
        if not ok:
            break
        allowed += 1
        for cell in ("simple-claude", "quoin-claude"):
            attempt = ps.reserve(ledger, "r", f"t{index}", cell, cap)
            ps.settle(ledger, attempt, "r", f"t{index}", cell, cap, cap + 0.50)
    # Each pair costs 12.0 against a nominal 11.0, so only 9 pairs fit under 110.
    assert allowed == 9
    assert spend_ledger.recorded_total(ledger, "r") == pytest.approx(108.0)
    ok, message = ps.pair_precheck(ledger, "r", cap, 2, 500.0, ceiling)
    assert not ok and "suite ceiling" in message


def test_pair_precheck_authorised_ceiling_counts_other_runs(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _write(ledger, "pilot", "p1", 60.0)
    ok, message = ps.pair_precheck(ledger, "r", 5.5, 2, 72.0, 110.0)
    assert ok
    ok, message = ps.pair_precheck(ledger, "r", 5.5, 2, 70.0, 110.0)
    assert not ok and "authorised-ceiling" in message
    assert ps.spawn_precheck(ledger, 5.5, 66.0)[0]
    assert not ps.spawn_precheck(ledger, 5.5, 65.0)[0]
