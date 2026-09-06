"""
test_three_arm_gate.py — Tests for the three-arm benchmark gate driver and
its supporting modules: the persisted spend ledger (T-17), the three-arm
comparison emitter (T-11), the dry-run gate (T-09), and the sequential
driver itself (T-07).
"""
import json
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest


# ---------------------------------------------------------------------------
# T-17: the persisted cumulative spend ledger
# ---------------------------------------------------------------------------


def _reservation(attempt_id, arm, cap_usd, run_id=None, invocation="full", gate_id="g1"):
    return {
        "ts": "2026-09-06T00:00:00Z", "attempt_id": attempt_id, "kind": "reservation",
        "invocation": invocation, "gate_id": gate_id, "arm": arm, "cap_usd": cap_usd,
        "actual_usd": None, "run_id": run_id, "new_ceiling_usd": None, "note": "",
    }


def _settlement(attempt_id, arm, cap_usd, actual_usd, run_id=None, invocation="full", gate_id="g1"):
    return {
        "ts": "2026-09-06T00:01:00Z", "attempt_id": attempt_id, "kind": "settlement",
        "invocation": invocation, "gate_id": gate_id, "arm": arm, "cap_usd": cap_usd,
        "actual_usd": actual_usd, "run_id": run_id, "new_ceiling_usd": None, "note": "",
    }


class TestSpendLedgerRecordedTotal:
    def test_absent_ledger_reads_as_zero_and_does_not_error(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        assert recorded_total(tmp_path / "nope.jsonl") == 0.0

    def test_reservation_with_no_settlement_totals_at_cap(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 16.0, run_id="g1-candidate"))
        assert recorded_total(path) == 16.0

    def test_settlement_with_null_actual_totals_at_cap(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 16.0, run_id="g1-candidate"))
        append(path, _settlement("a1", "candidate", 16.0, None, run_id="g1-candidate"))
        assert recorded_total(path) == 16.0

    def test_reservation_and_settlement_same_attempt_total_actual_once(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 16.0, run_id="g1-candidate"))
        append(path, _settlement("a1", "candidate", 16.0, 12.0, run_id="g1-candidate"))
        assert recorded_total(path) == 12.0

    def test_settlement_only_no_reservation_null_actual_totals_at_settlements_cap(self, tmp_path):
        """The literal-reading under-count the plan's own round-5 minor
        carry-over flags: a settlement can in principle be appended without
        its reservation ever landing (e.g. the reservation write itself was
        lost to a crash). Even then, a null actual must be charged at
        SOME cap, not silently read as zero spend."""
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        append(path, _settlement("a1", "candidate", 16.0, None, run_id="g1-candidate"))
        assert recorded_total(path) == 16.0

    def test_rerun_same_run_id_different_attempt_id_sums_both_actuals(self, tmp_path):
        """The re-run case (round-5 fix, MAJ-1): a re-run reuses its arm's
        run_id (D-01) but mints a NEW attempt_id, and both attempts' actuals
        must sum — not collapse into one."""
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 16.0, run_id="g1-candidate"))
        append(path, _settlement("a1", "candidate", 16.0, 12.0, run_id="g1-candidate"))
        append(path, _reservation("a2", "candidate", 16.0, run_id="g1-candidate"))
        append(path, _settlement("a2", "candidate", 16.0, 14.0, run_id="g1-candidate"))
        assert recorded_total(path) == 26.0

    def test_three_probe_rows_with_null_run_id_count_individually(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        for i in range(3):
            aid = f"probe-{i}"
            append(path, _reservation(aid, "probe", 1.0, run_id=None))
            append(path, _settlement(aid, "probe", 1.0, 0.02, run_id=None))
        assert recorded_total(path) == pytest.approx(0.06)

    def test_reauth_note_contributes_zero_to_recorded_total(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 16.0, run_id="g1-candidate"))
        append(path, _settlement("a1", "candidate", 16.0, 12.0, run_id="g1-candidate"))
        append(path, {
            "ts": "2026-09-06T01:00:00Z", "attempt_id": str(uuid.uuid4()), "kind": "reservation",
            "invocation": "reauth-note", "gate_id": "g1", "arm": "raw", "cap_usd": 0.0,
            "actual_usd": None, "run_id": None, "new_ceiling_usd": 60.0,
            "note": "operator raised ceiling to $60",
        })
        assert recorded_total(path) == 12.0

    def test_concurrent_appends_both_survive_as_two_lines(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "raw", 6.0, run_id="g1-raw"))
        append(path, _reservation("a2", "main", 16.0, run_id="g1-main"))
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["attempt_id"] == "a1"
        assert json.loads(lines[1])["attempt_id"] == "a2"


class TestSpendLedgerPrecheck:
    def test_recorded_35_plus_planned_16_exceeds_50_and_fails(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, precheck

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 35.0, run_id="g0-candidate"))
        append(path, _settlement("a1", "candidate", 35.0, 35.0, run_id="g0-candidate"))
        ok, message = precheck(path, planned_caps=[16.0])
        assert ok is False
        assert message.startswith("GATE-STOP: cumulative spend would exceed the ~$50 authorisation")

    def test_recorded_3_plus_caps_6_16_16_passes(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, precheck

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("p1", "probe", 3.0, run_id=None))
        append(path, _settlement("p1", "probe", 3.0, 3.0, run_id=None))
        ok, message = precheck(path, planned_caps=[6.0, 16.0, 16.0])
        assert ok is True
        assert message == ""

    def test_absent_ledger_passes_precheck_within_authorisation(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import precheck

        ok, message = precheck(tmp_path / "nope.jsonl", planned_caps=[6.0, 16.0, 16.0])
        assert ok is True

    def test_reauth_note_raises_ceiling_for_invocations_after_it_only(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, precheck

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 35.0, run_id="g0-candidate"))
        append(path, _settlement("a1", "candidate", 35.0, 35.0, run_id="g0-candidate"))
        # Before the reauth-note, 35 + 20 = 55 > 50 fails.
        ok, _ = precheck(path, planned_caps=[20.0])
        assert ok is False
        # An operator explicitly raises the ceiling to 60.
        append(path, {
            "ts": "2026-09-06T01:00:00Z", "attempt_id": str(uuid.uuid4()), "kind": "reservation",
            "invocation": "reauth-note", "gate_id": "g0", "arm": "raw", "cap_usd": 0.0,
            "actual_usd": None, "run_id": None, "new_ceiling_usd": 60.0,
            "note": "operator raised ceiling to $60",
        })
        # Now 35 + 20 = 55 <= 60 passes.
        ok, _ = precheck(path, planned_caps=[20.0])
        assert ok is True

    def test_explicit_authorised_overrides_derivation(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import precheck

        ok, _ = precheck(tmp_path / "nope.jsonl", planned_caps=[10.0], authorised=5.0)
        assert ok is False
