"""
test_three_arm_gate.py — Tests for the three-arm benchmark gate driver and
its supporting modules: the persisted spend ledger (T-17), the three-arm
comparison emitter (T-11), the dry-run gate (T-09), and the sequential
driver itself (T-07).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
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
        from quoin.benchmarks.scripts.spend_ledger import append, precheck, resolve_authorised_ceiling

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 35.0, run_id="g0-candidate"))
        append(path, _settlement("a1", "candidate", 35.0, 35.0, run_id="g0-candidate"))
        authorised = resolve_authorised_ceiling(path, None)
        assert authorised == 50.0
        ok, message = precheck(path, planned_caps=[16.0], authorised=authorised)
        assert ok is False
        assert message.startswith("GATE-STOP: cumulative spend would exceed the $50.00 authorisation")

    def test_recorded_3_plus_caps_6_16_16_passes(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, precheck, resolve_authorised_ceiling

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("p1", "probe", 3.0, run_id=None))
        append(path, _settlement("p1", "probe", 3.0, 3.0, run_id=None))
        ok, message = precheck(
            path, planned_caps=[6.0, 16.0, 16.0], authorised=resolve_authorised_ceiling(path, None),
        )
        assert ok is True
        assert message == ""

    def test_absent_ledger_passes_precheck_within_authorisation(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import precheck, resolve_authorised_ceiling

        path = tmp_path / "nope.jsonl"
        ok, message = precheck(path, planned_caps=[6.0, 16.0, 16.0], authorised=resolve_authorised_ceiling(path, None))
        assert ok is True

    def test_precheck_requires_explicit_authorised_argument(self, tmp_path):
        """`precheck` no longer derives a ceiling on its own — omitting
        `authorised` entirely is a caller bug (TypeError), not a silent
        $50 default. `resolve_authorised_ceiling` is the explicit,
        visible-at-the-call-site replacement for that old fallback."""
        import inspect
        from quoin.benchmarks.scripts.spend_ledger import precheck

        params = inspect.signature(precheck).parameters
        assert params["authorised"].default is inspect.Parameter.empty

    def test_nan_authorised_no_longer_disables_the_ceiling(self, tmp_path):
        """Regression (round-4 fix: MAJOR 6). Pre-fix, `nan > x` is always
        False, so `recorded + planned > authorised` never fired and any
        planned spend passed regardless of recorded spend."""
        from quoin.benchmarks.scripts.spend_ledger import append, precheck

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 49.0, run_id="g0-candidate"))
        append(path, _settlement("a1", "candidate", 49.0, 49.0, run_id="g0-candidate"))
        ok, message = precheck(path, planned_caps=[1000.0], authorised=float("nan"))
        assert ok is False
        assert "finite" in message

    def test_negative_authorised_is_rejected(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import precheck

        ok, message = precheck(tmp_path / "nope.jsonl", planned_caps=[1.0], authorised=-1e9)
        assert ok is False
        assert "positive" in message

    def test_infinite_authorised_is_rejected(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import precheck

        ok, message = precheck(tmp_path / "nope.jsonl", planned_caps=[1.0], authorised=float("inf"))
        assert ok is False

    def test_nan_planned_cap_is_rejected(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import precheck

        ok, message = precheck(tmp_path / "nope.jsonl", planned_caps=[float("nan")], authorised=50.0)
        assert ok is False
        assert "planned spend cap" in message

    def test_positive_finite_float_argparse_type_rejects_nan_inf_and_negatives(self):
        import argparse as _argparse

        from quoin.benchmarks.scripts.spend_ledger import positive_finite_float

        for bad in ("nan", "inf", "-inf", "1e400", "-5", "0"):
            with pytest.raises(_argparse.ArgumentTypeError):
                positive_finite_float(bad)
        assert positive_finite_float("12.5") == 12.5

    def test_nan_settlement_row_makes_recorded_total_raise_not_return_nan(self, tmp_path):
        """Regression (round-4 fix: MAJOR 7). Pre-fix, `json.dumps(nan)`
        writes bare `NaN`, `json.loads` accepts it back, and
        `recorded_total` silently returned `nan` — which then makes
        `precheck`'s own comparison always pass."""
        import json as _json

        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        path = tmp_path / "ledger.jsonl"
        row = _reservation("a1", "candidate", 16.0, run_id="g1-candidate")
        settlement = _settlement("a1", "candidate", 16.0, float("nan"), run_id="g1-candidate")
        with path.open("w", encoding="utf-8") as f:
            f.write(_json.dumps(row) + "\n")
            f.write(_json.dumps(settlement) + "\n")
        with pytest.raises(ValueError):
            recorded_total(path)

    def test_append_refuses_a_nan_cap_or_actual_usd(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append

        path = tmp_path / "ledger.jsonl"
        with pytest.raises(ValueError):
            append(path, _reservation("a1", "raw", float("nan"), run_id="g1-raw"))
        with pytest.raises(ValueError):
            append(path, _settlement("a1", "raw", 6.0, float("-inf"), run_id="g1-raw"))

    def test_reauth_note_raises_ceiling_for_invocations_after_it_only(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, precheck, resolve_authorised_ceiling

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 35.0, run_id="g0-candidate"))
        append(path, _settlement("a1", "candidate", 35.0, 35.0, run_id="g0-candidate"))
        # Before the reauth-note, 35 + 20 = 55 > 50 fails.
        ok, _ = precheck(path, planned_caps=[20.0], authorised=resolve_authorised_ceiling(path, None))
        assert ok is False
        # An operator explicitly raises the ceiling to 60.
        append(path, {
            "ts": "2026-09-06T01:00:00Z", "attempt_id": str(uuid.uuid4()), "kind": "reservation",
            "invocation": "reauth-note", "gate_id": "g0", "arm": "raw", "cap_usd": 0.0,
            "actual_usd": None, "run_id": None, "new_ceiling_usd": 60.0,
            "note": "operator raised ceiling to $60",
        })
        # Now 35 + 20 = 55 <= 60 passes.
        ok, _ = precheck(path, planned_caps=[20.0], authorised=resolve_authorised_ceiling(path, None))
        assert ok is True

    def test_explicit_authorised_overrides_derivation(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import precheck, resolve_authorised_ceiling

        path = tmp_path / "nope.jsonl"
        assert resolve_authorised_ceiling(path, 5.0) == 5.0
        ok, _ = precheck(path, planned_caps=[10.0], authorised=5.0)
        assert ok is False


# ---------------------------------------------------------------------------
# T-11: the three-arm comparison emitter
# ---------------------------------------------------------------------------


def _write_summary(run_dir, run_id, cell, n_tasks=1, n_pass=1, total_cost=None,
                    mean_wall=100.0, gate_interventions=0):
    d = run_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_id": run_id,
        "cells": {
            cell: {
                "n_tasks": n_tasks,
                "n_pass": n_pass,
                "total_cost_usd_or_null": total_cost,
                "mean_wall_clock_s": mean_wall,
                "gate_intervention_count": gate_interventions,
            }
        },
    }
    (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")


class TestCompareArms:
    def test_three_synthetic_summaries_produce_expected_table(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import compare_arms

        _write_summary(tmp_path, "g1-raw", "simple-claude", total_cost=1.2)
        _write_summary(tmp_path, "g1-main", "quoin-claude", total_cost=10.0)
        _write_summary(tmp_path, "g1-candidate", "quoin-claude", total_cost=8.0)

        out_path = compare_arms(
            run_dir=tmp_path, gate_id="g1",
            arm_run_ids={"raw": "g1-raw", "main": "g1-main", "candidate": "g1-candidate"},
            arm_cells={"raw": "simple-claude", "main": "quoin-claude", "candidate": "quoin-claude"},
            arm_evidence={
                "main": {"installed_quoin_commit": "aaa", "task_completion_quality": 3},
                "candidate": {"installed_quoin_commit": "bbb", "task_completion_quality": 4},
            },
        )
        assert out_path == tmp_path / "g1-comparison" / "three-arm-comparison.md"
        text = out_path.read_text(encoding="utf-8")
        assert "| raw | simple-claude | g1-raw |" in text
        assert "aaa" in text and "bbb" in text
        assert "cost: candidate <= main** — PASS" in text
        assert "quality: candidate >= main (task_completion_quality)** — PASS" in text

    def test_comparison_output_is_sibling_of_run_dirs_not_nested(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import compare_arms

        _write_summary(tmp_path, "g1-raw", "simple-claude")
        out_path = compare_arms(
            run_dir=tmp_path, gate_id="g1",
            arm_run_ids={"raw": "g1-raw"}, arm_cells={"raw": "simple-claude"},
        )
        assert out_path.parent.name == "g1-comparison"
        assert out_path.parent.parent == tmp_path
        assert not (tmp_path / "g1-raw" / "g1-comparison").exists()

    def test_missing_summary_json_yields_not_available_not_an_exception(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import compare_arms, NOT_AVAILABLE

        out_path = compare_arms(
            run_dir=tmp_path, gate_id="g1",
            arm_run_ids={"main": "g1-main"},  # no summary.json ever written
            arm_cells={"main": "quoin-claude"},
        )
        text = out_path.read_text(encoding="utf-8")
        assert NOT_AVAILABLE in text

    def test_unscored_quality_yields_pending_never_fabricated_zero(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import compare_arms

        _write_summary(tmp_path, "g1-main", "quoin-claude", total_cost=10.0)
        _write_summary(tmp_path, "g1-candidate", "quoin-claude", total_cost=8.0)
        out_path = compare_arms(
            run_dir=tmp_path, gate_id="g1",
            arm_run_ids={"main": "g1-main", "candidate": "g1-candidate"},
            arm_cells={"main": "quoin-claude", "candidate": "quoin-claude"},
            # no task_completion_quality supplied for either arm
        )
        text = out_path.read_text(encoding="utf-8")
        from quoin.benchmarks.harness.compare_arms import _COLUMNS
        quality_col = _COLUMNS.index("task_completion_quality")
        for line in text.splitlines():
            if line.startswith("| main |") or line.startswith("| candidate |"):
                cell_value = [c.strip() for c in line.strip("|").split("|")][quality_col]
                assert cell_value == "pending", f"expected pending, got {cell_value!r} in row: {line}"
        assert "quality: candidate >= main (task_completion_quality)** — pending" in text

    def test_null_cost_yields_not_available_and_forces_cost_verdict_pending(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import compare_arms

        _write_summary(tmp_path, "g1-main", "quoin-claude", total_cost=None)
        _write_summary(tmp_path, "g1-candidate", "quoin-claude", total_cost=8.0)
        out_path = compare_arms(
            run_dir=tmp_path, gate_id="g1",
            arm_run_ids={"main": "g1-main", "candidate": "g1-candidate"},
            arm_cells={"main": "quoin-claude", "candidate": "quoin-claude"},
        )
        text = out_path.read_text(encoding="utf-8")
        assert "cost: candidate <= main** — pending" in text
        assert "cost: candidate <= main** — PASS" not in text

    def test_raw_arm_never_part_of_ac4_comparison(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import compare_arms

        # A pathological raw cost lower than candidate's must not affect
        # the cost verdict — raw is excluded from both AC-4 comparisons.
        _write_summary(tmp_path, "g1-raw", "simple-claude", total_cost=0.01)
        _write_summary(tmp_path, "g1-main", "quoin-claude", total_cost=10.0)
        _write_summary(tmp_path, "g1-candidate", "quoin-claude", total_cost=8.0)
        out_path = compare_arms(
            run_dir=tmp_path, gate_id="g1",
            arm_run_ids={"raw": "g1-raw", "main": "g1-main", "candidate": "g1-candidate"},
            arm_cells={"raw": "simple-claude", "main": "quoin-claude", "candidate": "quoin-claude"},
        )
        text = out_path.read_text(encoding="utf-8")
        assert "cost: candidate <= main** — PASS" in text


# ---------------------------------------------------------------------------
# T-07: the sequential three-arm driver and its supporting mechanisms
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, stanzas: dict) -> Path:
    """stanzas: {(event, matcher, basename): command_path}."""
    hooks: dict = {}
    for (event, matcher, basename), command in stanzas.items():
        hooks.setdefault(event, []).append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command, "timeout": 5}],
        })
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return settings_path


class TestHookStanzaWipe:
    def test_wipes_only_owned_stanzas_under_hooks_dir(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import wipe_arm_stanzas

        settings_path = _make_settings(tmp_path, {
            ("UserPromptSubmit", "*", "userpromptsubmit.sh"): "/home/u/.claude/hooks/userpromptsubmit.sh",
            ("PreCompact", "auto", "precompact.sh"): "/home/u/.claude/hooks/precompact.sh",
        })
        owned = {("UserPromptSubmit", "*", "userpromptsubmit.sh")}
        before, after = wipe_arm_stanzas(settings_path, owned)
        assert len(before["UserPromptSubmit"]) == 1
        assert after["UserPromptSubmit"] == []
        assert len(after["PreCompact"]) == 1  # not owned, untouched

    def test_conjunctive_predicate_spares_non_quoin_script_with_same_basename_shape(self, tmp_path):
        """The round-5 fix (MAJ-2): round 4's path-only predicate matched
        four live non-quoin echo_stdin_smoke.sh stanzas that happen to sit
        under ~/.claude/hooks/. Here the SAME basename+matcher pair is
        registered by a script whose command path does NOT live under
        .claude/hooks/, which must survive the wipe (path half of the
        conjunction fails)."""
        from quoin.benchmarks.scripts.run_three_arm_gate import wipe_arm_stanzas

        settings_path = _make_settings(tmp_path, {
            ("UserPromptSubmit", "*", "userpromptsubmit.sh"): "/some/other/place/userpromptsubmit.sh",
        })
        owned = {("UserPromptSubmit", "*", "userpromptsubmit.sh")}
        before, after = wipe_arm_stanzas(settings_path, owned)
        assert len(after["UserPromptSubmit"]) == 1, "path half of the conjunction must gate the wipe"

    def test_non_owned_basename_under_hooks_dir_survives(self, tmp_path):
        """A script that happens to live under .claude/hooks/ but is NOT
        one of this arm's own registered (event, matcher, basename)
        triples must survive — e.g. a genuinely unrelated script."""
        from quoin.benchmarks.scripts.run_three_arm_gate import wipe_arm_stanzas

        settings_path = _make_settings(tmp_path, {
            ("UserPromptSubmit", "*", "echo_stdin_smoke.sh"): "/home/u/.claude/hooks/echo_stdin_smoke.sh",
        })
        owned = {("UserPromptSubmit", "*", "userpromptsubmit.sh")}
        before, after = wipe_arm_stanzas(settings_path, owned)
        assert len(after["UserPromptSubmit"]) == 1

    def test_missing_settings_file_returns_empty_without_raising(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import wipe_arm_stanzas

        before, after = wipe_arm_stanzas(tmp_path / "nope.json", set())
        assert before == [] and after == []


def _worktree_at_commit(commit: str, dest: Path) -> bool:
    """Create a real, disposable `git worktree` of THIS repo at `commit`
    under `dest`. Returns False (does not raise) if the commit cannot be
    resolved in this checkout (e.g. a shallow clone) — callers skip rather
    than fail in that case; a repo where the commit legitimately can't be
    reached isn't a real regression."""
    if subprocess.run(
        ["git", "cat-file", "-e", commit], cwd=_REPO_ROOT, capture_output=True,
    ).returncode != 0:
        return False
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", "--force", str(dest), commit],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    return result.returncode == 0


def _remove_worktree(path: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=_REPO_ROOT, capture_output=True,
    )


class TestArmRegisteredStanzas:
    def test_main_arm_worktree_registers_seven_stanzas_no_compact(self, tmp_path):
        """Pins the pre-IVG-258 baseline commit's installer.py, building
        the worktree on the fly rather than depending on one some earlier
        session happened to leave at a fixed $TMPDIR path. Skips only if
        this checkout cannot reach that commit at all."""
        from quoin.benchmarks.scripts.run_three_arm_gate import arm_registered_stanzas

        main_root = tmp_path / "main"
        if not _worktree_at_commit("dd188d87ed2512b80d46a3ee80d333b842bd23b5", main_root):
            pytest.skip("baseline commit not reachable in this checkout")
        try:
            stanzas = arm_registered_stanzas(main_root)
            assert len(stanzas) == 7
            assert ("SessionStart", "compact", "sessionstart.sh") not in stanzas
        finally:
            _remove_worktree(main_root)

    def test_candidate_arm_worktree_registers_eight_stanzas_with_compact(self, tmp_path):
        """Pins this branch's own HEAD, which registers the
        `SessionStart`/`compact` stanza the baseline above does not."""
        from quoin.benchmarks.scripts.run_three_arm_gate import arm_registered_stanzas

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        candidate_root = tmp_path / "candidate"
        if not _worktree_at_commit(head, candidate_root):
            pytest.skip("HEAD not reachable as a worktree in this checkout")
        try:
            stanzas = arm_registered_stanzas(candidate_root)
            assert len(stanzas) == 8
            assert ("SessionStart", "compact", "sessionstart.sh") in stanzas
        finally:
            _remove_worktree(candidate_root)

    def test_missing_installer_file_returns_empty_set(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import arm_registered_stanzas

        assert arm_registered_stanzas(tmp_path) == set()


class TestCrossArmManifest:
    def test_different_trees_produce_different_manifests(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import cross_arm_manifest

        arm_a = tmp_path / "a"
        arm_b = tmp_path / "b"
        (arm_a / "quoin" / "scripts").mkdir(parents=True)
        (arm_b / "quoin" / "scripts").mkdir(parents=True)
        (arm_a / "quoin" / "scripts" / "x.py").write_text("print(1)")
        (arm_b / "quoin" / "scripts" / "x.py").write_text("print(2)")
        assert cross_arm_manifest(arm_a) != cross_arm_manifest(arm_b)

    def test_identical_trees_produce_identical_manifests(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import cross_arm_manifest

        arm_a = tmp_path / "a"
        arm_b = tmp_path / "b"
        (arm_a / "quoin" / "scripts").mkdir(parents=True)
        (arm_b / "quoin" / "scripts").mkdir(parents=True)
        (arm_a / "quoin" / "scripts" / "x.py").write_text("same")
        (arm_b / "quoin" / "scripts" / "x.py").write_text("same")
        assert cross_arm_manifest(arm_a) == cross_arm_manifest(arm_b)


class TestConfigRootAssertion:
    def test_raw_arm_correctly_isolated_passes(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import assert_config_root, build_arm_env

        raw_config = tmp_path / "raw-config"
        env = build_arm_env("raw", raw_config)
        assert_config_root("raw", env, tmp_path / "home", raw_config)  # must not raise

    def test_raw_arm_env_never_mutates_process_environ(self, monkeypatch, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import build_arm_env

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        build_arm_env("raw", tmp_path / "raw-config")
        assert "CLAUDE_CONFIG_DIR" not in os.environ

    def test_leaked_config_dir_on_main_raises_gate_stop(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import assert_config_root, GateStop

        leaked_env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "raw-config")}
        with pytest.raises(GateStop):
            assert_config_root("main", leaked_env, tmp_path / "home", tmp_path / "raw-config")

    def test_main_with_no_config_dir_set_passes(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import assert_config_root

        assert_config_root("main", {}, tmp_path / "home", tmp_path / "raw-config")  # must not raise

    def test_build_arm_env_pops_config_dir_for_main_and_candidate(self, monkeypatch, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import build_arm_env

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/leaked/from/somewhere")
        for arm in ("main", "candidate"):
            env = build_arm_env(arm, tmp_path / "raw-config")
            assert "CLAUDE_CONFIG_DIR" not in env


class TestRawIsolationFallbackD15:
    """The 2026-09-06 rehearsal found the isolated CLAUDE_CONFIG_DIR does
    not carry this machine's auth ("Not logged in · Please run /login") —
    a real, plan-anticipated D-15 fallback trigger, not a bug. These pin
    the fallback path: raw runs unisolated, honestly relabeled."""

    def test_raw_isolated_false_never_sets_config_dir(self, monkeypatch, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import build_arm_env

        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        env = build_arm_env("raw", tmp_path / "raw-config", raw_isolated=False)
        assert "CLAUDE_CONFIG_DIR" not in env

    def test_raw_isolated_false_asserts_like_main_candidate(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import assert_config_root

        # No CLAUDE_CONFIG_DIR set -> passes, same rule as main/candidate.
        assert_config_root("raw", {}, tmp_path / "home", tmp_path / "raw-config", raw_isolated=False)

    def test_raw_isolated_false_still_rejects_a_leaked_config_dir(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import assert_config_root, GateStop

        leaked_env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "somewhere-else")}
        with pytest.raises(GateStop):
            assert_config_root("raw", leaked_env, tmp_path / "home", tmp_path / "raw-config", raw_isolated=False)

    def test_raw_arm_note_text_matches_isolation_state(self):
        from quoin.benchmarks.scripts.run_three_arm_gate import raw_arm_note

        assert raw_arm_note(True) == "quoin-free floor, isolated CLAUDE_CONFIG_DIR"
        assert "NOT a quoin-free floor" in raw_arm_note(False)

    def test_driver_defaults_to_isolated_unless_flag_passed(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        assert ThreeArmGateDriver(ns).raw_isolated is True
        ns2 = TestDriverPlanOnly()._make_args(tmp_path, raw_no_isolation=True)
        assert ThreeArmGateDriver(ns2).raw_isolated is False


class TestSimpleClaudeAuthFailureNotCountedAsCompletion:
    def test_error_result_event_with_assistant_turn_is_not_had_assistant_event(self, monkeypatch, tmp_path):
        """Regression for the exact rehearsal finding: an authentication
        failure still emits a type:"assistant" event (turn_count=1) but
        the terminal result event carries is_error:true — that must NOT
        count as a genuine completion."""
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Not logged in · Please run /login"}]}},
            {"type": "result", "is_error": True, "total_cost_usd": 0, "result": "Not logged in · Please run /login"},
        ]
        lines = iter([__import__("json").dumps(e) for e in events] + [""])

        class ScriptedStdout:
            def readline(self):
                try:
                    return next(lines) + "\n"
                except StopIteration:
                    return ""

        class ScriptedProc:
            stdout = ScriptedStdout()
            stderr = None
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0

        real_popen = simple_claude.subprocess.Popen

        def fake_popen(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
                return ScriptedProc()
            return real_popen(cmd, *a, **kw)

        monkeypatch.setattr(simple_claude.subprocess, "Popen", fake_popen)
        result = simple_claude.invoke(
            task_spec={"id": "scenario_x", "description": "x"}, workdir=tmp_path,
            budget=BudgetSpec(), run_id="r1",
        )
        assert result["turn_count"] == 1
        assert result["extra"]["session_errored"] is True
        assert result["extra"]["had_assistant_event"] is False


class TestDriverPlanOnly:
    def _make_args(self, tmp_path, **overrides):
        ns = argparse.Namespace(
            gate_id="g1", suite=tmp_path / "suite.json", fixture_repo=tmp_path / "fixture",
            main_worktree=tmp_path / "main", candidate_worktree=tmp_path / "candidate",
            main_commit="main-sha", candidate_commit="candidate-sha",
            max_budget_usd_raw=6.0, max_budget_usd_main=16.0, max_budget_usd_candidate=16.0,
            spend_ledger=tmp_path / "ledger.jsonl", new_spend_ledger=True, authorised_usd=50.0,
            project_root=None, expected_suite_sha256=None, wall_clock_seconds=600,
            run_dir=tmp_path / "runs", rehearsal=False, plan_only=True,
        )
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

    def _patch_identity_checks(self, monkeypatch, tmp_path, ns):
        """Shared by tests that drive real preflight()/run() calls: stubs
        the arm-identity assertions (arm-HEAD, __about__ version, the
        interpreter probe) the same way existing tests already stub
        `verify_arm_installer_isolable` and
        `cross_arm_manifest` — these are real subprocess/filesystem probes
        that a synthetic tmp_path worktree cannot satisfy."""
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        def fake_worktree_head(root):
            if root == ns.main_worktree:
                return ns.main_commit
            if root == ns.candidate_worktree:
                return ns.candidate_commit
            return None

        monkeypatch.setattr(gate_mod, "worktree_head", fake_worktree_head)
        monkeypatch.setattr(gate_mod, "candidate_about_version_matches", lambda root, py: True)
        monkeypatch.setattr(gate_mod, "_install_py", lambda: "/usr/bin/python3")

    def test_plan_only_never_invokes_run_arm(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        def raising_run_arm(argv, env):
            raise AssertionError("--plan-only must never invoke an arm")

        args = self._make_args(tmp_path)
        driver = ThreeArmGateDriver(args, run_arm_fn=raising_run_arm)
        code = driver.run()
        assert code == 2  # worktrees don't exist in this synthetic test -> preflight fails
        # the assertion is really that raising_run_arm was never called, which
        # not raising here already proves.

    def test_plan_only_prints_arm_sequence(self, tmp_path, capsys):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        args = self._make_args(tmp_path)

        driver = ThreeArmGateDriver(args, run_arm_fn=lambda argv, env: (_ for _ in ()).throw(
            AssertionError("must not spawn")))
        driver.preflight = lambda: []  # bypass real install/git checks for this print-only assertion
        driver.run()
        out = capsys.readouterr().out
        assert "raw, main, candidate" in out

    def test_plan_only_never_calls_verify_model(self, tmp_path, monkeypatch):
        """--plan-only is spend-free by definition — the one live,
        spend-generating preflight call (--verify-model) must never fire."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_benchmark as rb_mod

        def raising_verify_model(*a, **kw):
            raise AssertionError("--plan-only must never call verify_model")

        monkeypatch.setattr(rb_mod, "verify_model", raising_verify_model)
        monkeypatch.delenv("QUOIN_BENCH_CLAUDE_MODEL", raising=False)

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        (tmp_path / "suite.json").write_text(json.dumps({"tasks": [{
            "id": "scenario_x", "source": "quoin_scenario", "description": "x subsys.py y",
            "target_subsystem": "subsys.py",
        }]}))
        args = self._make_args(tmp_path)
        driver = ThreeArmGateDriver(args, run_arm_fn=lambda argv, env: (_ for _ in ()).throw(
            AssertionError("must not spawn")))
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.verify_arm_installer_isolable",
            lambda root, py: True,
        )
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.cross_arm_manifest",
            lambda root: {"main": 1} if root == tmp_path / "main" else {"candidate": 2},
        )
        self._patch_identity_checks(monkeypatch, tmp_path, args)
        code = driver.run()
        assert code == 0  # all preflight checks pass, plan-only, never spawns

    def test_full_mode_calls_verify_model_once(self, tmp_path, monkeypatch):
        """Full mode (not plan-only, not rehearsal) DOES make the one live
        call — proven here via a mock that never touches the real CLI."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_benchmark as rb_mod

        calls = []

        def mock_verify_model(ledger_path=None, **kw):
            calls.append(ledger_path)
            return 0

        monkeypatch.setattr(rb_mod, "verify_model", mock_verify_model)
        monkeypatch.delenv("QUOIN_BENCH_CLAUDE_MODEL", raising=False)

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        (tmp_path / "suite.json").write_text(json.dumps({"tasks": [{
            "id": "scenario_x", "source": "quoin_scenario", "description": "x subsys.py y",
            "target_subsystem": "subsys.py",
        }]}))
        args = self._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(args, run_arm_fn=lambda argv, env: 0)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.verify_arm_installer_isolable",
            lambda root, py: True,
        )
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.cross_arm_manifest",
            lambda root: {"main": 1} if root == tmp_path / "main" else {"candidate": 2},
        )
        self._patch_identity_checks(monkeypatch, tmp_path, args)
        # The probe now runs LAST, after every fall-through check (round-4
        # fix: MAJOR 5) — including the green-rehearsal gate, so full mode
        # needs one on disk to ever reach the probe at all.
        (args.spend_ledger.parent / "rehearsal.md").write_text("GREEN", encoding="utf-8")
        driver.preflight()
        assert len(calls) == 1
        assert calls[0] == args.spend_ledger

    def test_missing_rehearsal_record_stops_the_probe_from_ever_firing(self, tmp_path, monkeypatch):
        """Regression (round-4 fix: MAJOR 5). Pre-fix, the probe ran ahead
        of the green-rehearsal-record check (and the suite-exists /
        dry-run-gate checks), so a full-mode run guaranteed to abort still
        spent the $1 probe call. Proved here with a stubbed verify_model
        and only the rehearsal-record precondition violated."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_benchmark as rb_mod

        calls = []

        def mock_verify_model(ledger_path=None, **kw):
            calls.append(ledger_path)
            return 0

        monkeypatch.setattr(rb_mod, "verify_model", mock_verify_model)
        monkeypatch.delenv("QUOIN_BENCH_CLAUDE_MODEL", raising=False)

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        (tmp_path / "suite.json").write_text(json.dumps({"tasks": [{
            "id": "scenario_x", "source": "quoin_scenario", "description": "x subsys.py y",
            "target_subsystem": "subsys.py",
        }]}))
        args = self._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(args, run_arm_fn=lambda argv, env: 0)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.verify_arm_installer_isolable",
            lambda root, py: True,
        )
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.cross_arm_manifest",
            lambda root: {"main": 1} if root == tmp_path / "main" else {"candidate": 2},
        )
        self._patch_identity_checks(monkeypatch, tmp_path, args)
        # Deliberately NOT writing rehearsal.md — the one precondition this
        # test violates.
        problems = driver.preflight()
        assert any("no green rehearsal record found" in p for p in problems)
        assert calls == [], "the paid probe must not fire when preflight is already guaranteed to abort"


class TestDriverLedgerFlow:
    def test_reservation_then_settlement_recorded_per_arm(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=lambda argv, env: 0)
        driver.arm_roots = {}  # skip install/stanza machinery for this ledger-only test
        driver.run_arm("raw")
        assert recorded_total(ns.spend_ledger) == 6.0  # charged at cap: no summary.json => actual None

    def test_teardown_flushes_unreserved_spawned_arm_at_cap(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {}
        driver.spawned_arms = {"candidate"}  # simulate: marked spawned, died before its reservation
        monkeypatch.setattr(driver, "install_arm" if hasattr(driver, "install_arm") else "arm_roots", driver.arm_roots)
        recorded = driver.teardown()  # candidate_root is None here -> returns True without reinstalling
        assert recorded is True
        assert recorded_total(ns.spend_ledger) == 16.0  # flushed at candidate's cap

    def test_teardown_restores_verbatim_settings_json(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {}
        driver.home = tmp_path / "home"
        (driver.home / ".claude").mkdir(parents=True)
        settings_path = driver.home / ".claude" / "settings.json"
        original = json.dumps({"hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": []}]}})
        settings_path.write_text(original, encoding="utf-8")
        driver.pre_provenance["settings_json_bytes"] = original.encode("utf-8")

        # Simulate a wipe having mutated the file mid-run.
        settings_path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        driver.teardown()
        assert settings_path.read_bytes() == original.encode("utf-8")


class TestDriverExceptionSafety:
    def test_gatestop_mid_loop_still_runs_teardown_and_returns_2(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver, GateStop

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {}
        driver.preflight = lambda: []
        teardown_calls = []

        def raising_run_arm(arm):
            raise GateStop("GATE-STOP: injected mid-arm failure")

        driver.run_arm = raising_run_arm
        real_teardown = driver.teardown

        def spy_teardown():
            teardown_calls.append(True)
            return real_teardown()

        driver.teardown = spy_teardown
        code = driver.run()
        assert code == 2
        assert teardown_calls, "teardown must run even when a GateStop interrupts the arm loop"

    def test_keyboard_interrupt_mid_loop_still_runs_teardown(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {}
        driver.preflight = lambda: []
        teardown_calls = []

        def raising_run_arm(arm):
            raise KeyboardInterrupt()

        driver.run_arm = raising_run_arm
        real_teardown = driver.teardown

        def spy_teardown():
            teardown_calls.append(True)
            return real_teardown()

        driver.teardown = spy_teardown
        code = driver.run()
        assert teardown_calls
        assert code in (1, 130)

    def test_compare_arms_import_error_degrades_to_warn_not_failure(self, tmp_path, capsys, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=lambda argv, env: 0)
        driver.arm_roots = {}
        driver.preflight = lambda: []

        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *a, **kw):
            if name == "quoin.benchmarks.harness.compare_arms":
                raise ImportError("simulated unavailable")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocking_import)
        code = driver.run()
        assert code == 0
        out = capsys.readouterr().out
        assert "WARN: compare_arms unavailable; comparison skipped" in out


# ---------------------------------------------------------------------------
# T-09: the dry-run gate
# ---------------------------------------------------------------------------


class TestDryRunGateCheck:
    def test_no_cap_fails_with_unbounded(self):
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

        ok, reason = dry_run_gate_check(max_budget_usd_per_task=None)
        assert ok is False
        assert "UNBOUNDED" in reason

    def test_worst_case_above_ceiling_fails(self):
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

        ok, reason = dry_run_gate_check(max_budget_usd_per_task=20.0, worst_case_usd=40.0)
        assert ok is False
        assert "38" in reason

    def test_worst_case_at_ceiling_passes(self):
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

        ok, reason = dry_run_gate_check(max_budget_usd_per_task=6.0, worst_case_usd=38.0)
        assert ok is True
        assert reason == ""

    def test_unpriced_model_override_fails(self, monkeypatch):
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check
        from quoin.benchmarks.harness.cells import simple_claude

        monkeypatch.setattr(simple_claude, "PINNED_MODEL", "claude-totally-unpriced-model")
        ok, reason = dry_run_gate_check(max_budget_usd_per_task=6.0, worst_case_usd=6.0)
        assert ok is False
        assert "pricing.json" in reason

    def test_model_override_outside_rehearsal_fails(self, monkeypatch):
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

        monkeypatch.setenv("QUOIN_BENCH_CLAUDE_MODEL", "claude-sonnet-4-6")
        ok, reason = dry_run_gate_check(max_budget_usd_per_task=6.0, worst_case_usd=6.0, rehearsal=False)
        assert ok is False
        assert "rehearsal" in reason

    def test_model_override_waived_under_rehearsal(self, monkeypatch):
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

        monkeypatch.setenv("QUOIN_BENCH_CLAUDE_MODEL", "claude-sonnet-4-6")
        ok, reason = dry_run_gate_check(max_budget_usd_per_task=1.0, worst_case_usd=1.0, rehearsal=True)
        assert ok is True

    def test_everything_correct_passes(self, monkeypatch):
        from quoin.benchmarks.scripts.run_benchmark import dry_run_gate_check

        monkeypatch.delenv("QUOIN_BENCH_CLAUDE_MODEL", raising=False)
        ok, reason = dry_run_gate_check(max_budget_usd_per_task=6.0, worst_case_usd=38.0)
        assert ok is True
        assert reason == ""


class TestDryRunGateCLIExitCodes:
    def _run(self, args_list, monkeypatch):
        import sys as _sys
        from quoin.benchmarks.scripts import run_benchmark as rb_mod

        old_argv = _sys.argv
        try:
            _sys.argv = ["run_benchmark.py"] + args_list
            with pytest.raises(SystemExit) as exc_info:
                rb_mod.main()
            return exc_info.value.code
        finally:
            _sys.argv = old_argv

    def test_no_cap_stops_with_gate_stop_prefix_and_exit_2(self, monkeypatch, capsys):
        code = self._run([
            "--suite", str(_REPO_ROOT / "quoin" / "benchmarks" / "suite-gate-medium-refactor.json"),
            "--cells", "simple-claude", "--run-id", "smoke", "--dry-run",
        ], monkeypatch)
        assert code == 2
        err = capsys.readouterr().err
        assert err.startswith("GATE-STOP: dry-run precondition failed")
        assert "UNBOUNDED" in err

    def test_caps_summing_above_threshold_stops(self, monkeypatch, capsys):
        code = self._run([
            "--suite", str(_REPO_ROOT / "quoin" / "benchmarks" / "suite-gate-medium-refactor.json"),
            "--cells", "simple-claude,quoin-claude", "--run-id", "smoke", "--dry-run",
            "--max-budget-usd-per-task", "20",
        ], monkeypatch)
        assert code == 2
        err = capsys.readouterr().err
        assert err.startswith("GATE-STOP:")

    def test_everything_correct_exits_zero_and_prints_worst_case(self, monkeypatch, capsys):
        code = self._run([
            "--suite", str(_REPO_ROOT / "quoin" / "benchmarks" / "suite-gate-medium-refactor.json"),
            "--cells", "simple-claude", "--run-id", "smoke", "--dry-run",
            "--max-budget-usd-per-task", "6",
        ], monkeypatch)
        assert code == 0
        out = capsys.readouterr().out
        assert "WORST CASE: $6.00" in out

    def test_dry_run_makes_zero_agent_invocations(self, monkeypatch):
        from quoin.benchmarks.scripts import run_benchmark as rb_mod

        def raising_run_benchmark(*a, **kw):
            raise AssertionError("--dry-run must never invoke run_benchmark")

        monkeypatch.setattr(rb_mod, "run_benchmark", raising_run_benchmark)
        code = self._run([
            "--suite", str(_REPO_ROOT / "quoin" / "benchmarks" / "suite-gate-medium-refactor.json"),
            "--cells", "simple-claude", "--run-id", "smoke", "--dry-run",
            "--max-budget-usd-per-task", "6",
        ], monkeypatch)
        assert code == 0


# ---------------------------------------------------------------------------
# T-16: the rehearsal record (gates T-10's full-mode preflight)
# ---------------------------------------------------------------------------


def _write_task_files(run_dir, run_id, cell, verdict="pass", cost=1.0,
                       installed_commit="abc", transcript="{}\n"):
    task_dir = run_dir / run_id / cell / "scenario_medium_refactor_plan"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "judge.json").write_text(json.dumps({"verdict": verdict}))
    (task_dir / "metrics.json").write_text(json.dumps({
        "installed_quoin_commit": installed_commit, "budget_cap_armed": True,
        "max_budget_usd_applied": 1.0,
    }))
    (task_dir / "transcript.jsonl").write_text(transcript)
    run_root = run_dir / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "summary.json").write_text(json.dumps({
        "cells": {cell: {"total_cost_usd_or_null": cost}}
    }))


class TestRehearsalRecord:
    def test_all_arms_green_writes_green_status(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, rehearsal=True)
        driver = ThreeArmGateDriver(ns)
        for arm, cell, commit in (
            ("raw", "simple-claude", None), ("main", "quoin-claude", "aaa"),
            ("candidate", "quoin-claude", "bbb"),
        ):
            _write_task_files(ns.run_dir, f"g1-{arm}", cell, installed_commit=commit)
        record_path = driver.write_rehearsal_record()
        text = record_path.read_text()
        assert "Status: **GREEN**" in text
        assert "installed_quoin_commit=aaa" in text
        assert "installed_quoin_commit=bbb" in text

    def test_error_verdict_forces_red(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, rehearsal=True)
        driver = ThreeArmGateDriver(ns)
        for arm, cell, commit in (
            ("raw", "simple-claude", None), ("main", "quoin-claude", "aaa"),
            ("candidate", "quoin-claude", "bbb"),
        ):
            _write_task_files(ns.run_dir, f"g1-{arm}", cell, installed_commit=commit,
                               verdict="error" if arm == "raw" else "pass")
        record_path = driver.write_rehearsal_record()
        text = record_path.read_text()
        assert "Status: **RED**" in text
        assert "raw: verdict is error" in text

    def test_same_installed_commit_on_main_and_candidate_forces_red(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, rehearsal=True)
        driver = ThreeArmGateDriver(ns)
        for arm, cell in (("raw", "simple-claude"), ("main", "quoin-claude"), ("candidate", "quoin-claude")):
            _write_task_files(ns.run_dir, f"g1-{arm}", cell, installed_commit="same-sha")
        record_path = driver.write_rehearsal_record()
        text = record_path.read_text()
        assert "Status: **RED**" in text
        assert "SAME installed_quoin_commit" in text

    def test_missing_files_reported_as_red_not_an_exception(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, rehearsal=True)
        driver = ThreeArmGateDriver(ns)
        record_path = driver.write_rehearsal_record()  # no task files written at all
        text = record_path.read_text()
        assert "Status: **RED**" in text

    def test_record_written_to_spend_ledger_sibling_path(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, rehearsal=True)
        driver = ThreeArmGateDriver(ns)
        record_path = driver.write_rehearsal_record()
        assert record_path == ns.spend_ledger.parent / "rehearsal.md"

    def test_preflight_requires_green_record_before_full_mode(self, tmp_path, monkeypatch):
        """The record this method writes is exactly what T-07 step 0's
        full-mode preflight checks for — proven end to end here."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        (tmp_path / "suite.json").write_text(json.dumps({"tasks": [{
            "id": "scenario_x", "source": "quoin_scenario", "description": "x subsys.py y",
            "target_subsystem": "subsys.py",
        }]}))
        monkeypatch.delenv("QUOIN_BENCH_CLAUDE_MODEL", raising=False)
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.verify_arm_installer_isolable",
            lambda root, py: True,
        )
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.cross_arm_manifest",
            lambda root: {"main": 1} if root == tmp_path / "main" else {"candidate": 2},
        )
        import quoin.benchmarks.scripts.run_benchmark as rb_mod
        monkeypatch.setattr(rb_mod, "verify_model", lambda ledger_path=None, **kw: 0)

        ns = TestDriverPlanOnly()._make_args(tmp_path, rehearsal=False, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        TestDriverPlanOnly()._patch_identity_checks(monkeypatch, tmp_path, ns)
        # No rehearsal.md at all yet.
        problems = driver.preflight()
        assert any("no green rehearsal record found" in p for p in problems)

        # Write a RED record -> still blocked.
        ns.spend_ledger.parent.mkdir(parents=True, exist_ok=True)
        (ns.spend_ledger.parent / "rehearsal.md").write_text("Status: **RED**\n")
        problems = driver.preflight()
        assert any("no green rehearsal record found" in p for p in problems)

        # Write a GREEN record -> that specific problem clears.
        (ns.spend_ledger.parent / "rehearsal.md").write_text("Status: **GREEN**\n")
        problems = driver.preflight()
        assert not any("no green rehearsal record found" in p for p in problems)


# ---------------------------------------------------------------------------
# A failed arm install must never let the arm spawn
# ---------------------------------------------------------------------------


class TestArmInstallFailureBlocksSpawn:
    def test_failing_install_raises_gatestop_before_spawn_or_reservation(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import GateStop, ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(
            ns, run_arm_fn=lambda argv, env: (_ for _ in ()).throw(
                AssertionError("must not spawn after a failed install")),
        )
        candidate_root = tmp_path / "candidate"
        candidate_root.mkdir()
        driver.arm_roots = {"candidate": candidate_root}

        failing_install = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        monkeypatch.setattr(gate_mod, "install_arm", lambda root, py: failing_install)

        with pytest.raises(GateStop, match="install failed"):
            driver.run_arm("candidate")

        assert "candidate" not in driver.spawned_arms
        assert "candidate" not in driver.reservations
        assert recorded_total(ns.spend_ledger) == 0.0

    def test_successful_install_but_failed_deploy_verification_also_blocks_spawn(self, tmp_path, monkeypatch):
        """D-02: a zero returncode alone is not sufficient — the
        byte-level deploy/hook verification must also pass."""
        from quoin.benchmarks.scripts.run_three_arm_gate import GateStop, ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(
            ns, run_arm_fn=lambda argv, env: (_ for _ in ()).throw(
                AssertionError("must not spawn if deploy verification failed")),
        )
        candidate_root = tmp_path / "candidate"
        candidate_root.mkdir()
        driver.arm_roots = {"candidate": candidate_root}

        ok_install = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        monkeypatch.setattr(gate_mod, "install_arm", lambda root, py: ok_install)
        monkeypatch.setattr(gate_mod, "verify_arm_deployed", lambda root, project_root: False)
        monkeypatch.setattr(gate_mod, "verify_hooks_deployed", lambda root, home: True)

        with pytest.raises(GateStop, match="deploy-drift verification failed"):
            driver.run_arm("candidate")

        assert "candidate" not in driver.spawned_arms


# ---------------------------------------------------------------------------
# The spend ledger fails closed on malformed or missing data
# ---------------------------------------------------------------------------


class TestSpendLedgerFailsClosed:
    def test_malformed_json_line_raises_instead_of_silently_skipping(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import append, recorded_total

        path = tmp_path / "ledger.jsonl"
        append(path, _reservation("a1", "candidate", 16.0, run_id="g1-candidate"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"not": "valid json"\n')

        with pytest.raises(ValueError):
            recorded_total(path)

    def test_row_missing_attempt_id_raises(self, tmp_path):
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        path = tmp_path / "ledger.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "reservation", "cap_usd": 16.0}) + "\n")

        with pytest.raises(ValueError):
            recorded_total(path)


class TestDriverLedgerExistenceAndAuthorisedUsd:
    def test_missing_ledger_without_new_flag_gate_stops(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, new_spend_ledger=False)
        driver = ThreeArmGateDriver(ns)
        problems = driver.preflight()
        assert any("spend ledger not found" in p for p in problems)

    def test_new_spend_ledger_flag_permits_a_missing_file(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, new_spend_ledger=True)
        driver = ThreeArmGateDriver(ns)
        problems = driver.preflight()
        assert not any("spend ledger not found" in p for p in problems)

    def test_explicit_authorised_usd_overrides_ledger_derived_default_ceiling(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(
            tmp_path, max_budget_usd_raw=1.0, max_budget_usd_main=1.0,
            max_budget_usd_candidate=1.0, authorised_usd=2.0,
        )
        driver = ThreeArmGateDriver(ns)
        # 1+1+1 = 3 planned > the operator-supplied 2.0 ceiling — must fail
        # even though it is well under the $50 ledger-derived default.
        problems = driver.preflight()
        assert any("exceed" in p for p in problems)

    def test_ledger_path_and_recorded_total_printed_in_preflight_banner(self, tmp_path, capsys):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver.preflight()
        out = capsys.readouterr().out
        assert str(ns.spend_ledger) in out
        assert "recorded_total=" in out


# ---------------------------------------------------------------------------
# Arm identity is an input, not a derivation
# ---------------------------------------------------------------------------


class TestArmIdentityAssertion:
    def test_mismatched_commit_is_gate_stop(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        monkeypatch.setattr(gate_mod, "worktree_head", lambda root: "some-other-sha")
        monkeypatch.setattr(gate_mod, "candidate_about_version_matches", lambda root, py: True)
        monkeypatch.setattr(gate_mod, "_install_py", lambda: "/usr/bin/python3")

        problems = driver.preflight()
        assert any("does not match expected commit" in p for p in problems)

    def test_missing_commit_arg_is_gate_stop(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False, main_commit=None)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}

        problems = driver.preflight()
        assert any("--main-commit was not supplied" in p for p in problems)

    def test_matching_commit_clears_the_identity_check(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        TestDriverPlanOnly()._patch_identity_checks(monkeypatch, tmp_path, ns)

        problems = driver.preflight()
        assert not any("does not match expected commit" in p for p in problems)
        assert not any("was not supplied" in p for p in problems)


class TestWorktreeOutsideProjectRoot:
    def test_worktree_inside_project_root_is_gate_stop(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False, project_root=tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        TestDriverPlanOnly()._patch_identity_checks(monkeypatch, tmp_path, ns)

        problems = driver.preflight()
        assert any("is inside the project root" in p for p in problems)


# ---------------------------------------------------------------------------
# An aborted arm kills its whole process group rather than
# orphaning the paid `claude` grandchild
# ---------------------------------------------------------------------------


class TestDefaultRunArmAbortSafety:
    def test_timeout_passed_to_wait_is_derived_from_argv_plus_margin(self, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import default_run_arm

        captured = {}

        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                captured["timeout"] = timeout
                return 0

        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.subprocess.Popen",
            lambda argv, env, start_new_session: FakeProc(),
        )
        code = default_run_arm(["prog", "--wall-clock-seconds", "600"], {})
        assert code == 0
        # 2x budget (the cell's own documented worst case, given its
        # backoff-time exemption is itself capped at one budget's worth)
        # plus the 120s teardown margin — round-5 fix, MAJOR 2. Pre-fix
        # this was budget + 120s (720.0), which fires BEFORE a cell that
        # legitimately used its full two-budget allowance could ever
        # terminate itself cleanly.
        assert captured["timeout"] == 1320.0

    def test_driver_timeout_always_covers_the_cells_own_worst_case(self, monkeypatch):
        """Regression (round-5 fix, MAJOR 2): the cell's own backoff
        exemption (`min(backoff_total, budget_seconds)` in both
        simple_claude.py and quoin_claude.py) lets a session run up to
        TWICE its configured `--wall-clock-seconds` before it self-
        terminates. The driver's SIGKILL backstop must never fire before
        that self-termination has a chance to — i.e. its timeout must
        stay >= 2x budget for every budget this CLI accepts, not just the
        one value spot-checked above."""
        from quoin.benchmarks.scripts.run_three_arm_gate import default_run_arm

        for budget in (1, 3, 30, 600, 5400):
            captured = {}

            class FakeProc:
                pid = 1

                def wait(self, timeout=None):
                    captured["timeout"] = timeout
                    return 0

            monkeypatch.setattr(
                "quoin.benchmarks.scripts.run_three_arm_gate.subprocess.Popen",
                lambda argv, env, start_new_session: FakeProc(),
            )
            default_run_arm(["prog", "--wall-clock-seconds", str(budget)], {})
            assert captured["timeout"] >= 2.0 * budget, (
                f"budget={budget}: driver timeout {captured['timeout']} is tighter "
                f"than the cell's own two-budget worst case"
            )

    def test_wait_timeout_kills_process_group_and_returns_124(self, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import default_run_arm

        killed = []

        class FakeProc:
            pid = 4243

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 0)

        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.subprocess.Popen",
            lambda argv, env, start_new_session: FakeProc(),
        )
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate._kill_process_group",
            lambda pid: killed.append(pid),
        )
        code = default_run_arm(["prog"], {})
        assert code == 124
        assert killed == [4243]

    def test_abort_exception_kills_process_group_before_propagating(self, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import default_run_arm

        killed = []

        class FakeProc:
            pid = 4244

            def wait(self, timeout=None):
                raise KeyboardInterrupt()

        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate.subprocess.Popen",
            lambda argv, env, start_new_session: FakeProc(),
        )
        monkeypatch.setattr(
            "quoin.benchmarks.scripts.run_three_arm_gate._kill_process_group",
            lambda pid: killed.append(pid),
        )
        with pytest.raises(KeyboardInterrupt):
            default_run_arm(["prog"], {})
        assert killed == [4244]

    def test_kill_process_group_terminates_a_real_child(self):
        from quoin.benchmarks.scripts.run_three_arm_gate import _kill_process_group

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
        )
        time.sleep(0.2)
        _kill_process_group(proc.pid)
        proc.wait(timeout=5)
        assert proc.returncode is not None and proc.returncode != 0


# ---------------------------------------------------------------------------
# A driver-level SIGKILL (exit 124) is fatal, not a warning: it means a
# fully paid arm produced no measurement, and spending the next arm on
# top of that loss compounds it rather than containing it.
# ---------------------------------------------------------------------------


class TestDriverKillIsFatalNotAWarning:
    def test_returncode_124_stops_the_run_and_skips_remaining_arms(self, tmp_path):
        """Regression (round-5 fix, MAJOR 2). Pre-fix, `run()` only ever
        printed `WARN: arm {arm} exited {returncode}` and kept spending
        the remaining arms — including 124, which uniquely means the
        driver itself killed the arm before it could write summary.json,
        so the settlement below already booked it at cap with nothing to
        compare. This must stop the run the same way any other GATE-STOP
        does, not fall through as an ordinary warning."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        (tmp_path / "suite.json").write_text(json.dumps(
            {"tasks": [{"id": "scenario_x", "description": "x"}]}
        ))

        called_arms: list[str] = []

        def fake_run_arm(argv, env):
            run_id = argv[argv.index("--run-id") + 1]
            arm = run_id.rsplit("-", 1)[-1]
            called_arms.append(arm)
            return 124

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=fake_run_arm)
        driver.arm_roots = {}
        driver.preflight = lambda: []

        code = driver.run()

        assert code == 2  # the GateStop exit path, same as any other GATE-STOP
        assert called_arms == ["raw"]  # main and candidate never spawned

    def test_returncode_124_still_tears_down_cleanly(self, tmp_path):
        """The GATE-STOP must not skip teardown — the same `finally` that
        runs it for every other GateStop still applies here."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        (tmp_path / "suite.json").write_text(json.dumps(
            {"tasks": [{"id": "scenario_x", "description": "x"}]}
        ))

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=lambda argv, env: 124)
        driver.arm_roots = {}
        driver.preflight = lambda: []

        driver.run()
        assert driver._torn_down is True

    def test_a_non_124_nonzero_exit_still_only_warns(self, tmp_path):
        """Only 124 (the driver's own SIGKILL) is fatal — an arm's own
        ordinary non-zero exit keeps the pre-existing warn-and-continue
        behaviour, unchanged."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        (tmp_path / "suite.json").write_text(json.dumps(
            {"tasks": [{"id": "scenario_x", "description": "x"}]}
        ))

        called_arms: list[str] = []

        def fake_run_arm(argv, env):
            run_id = argv[argv.index("--run-id") + 1]
            arm = run_id.rsplit("-", 1)[-1]
            called_arms.append(arm)
            return 1

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=fake_run_arm)
        driver.arm_roots = {}
        driver.preflight = lambda: []

        code = driver.run()
        assert code == 0
        assert called_arms == ["raw", "main", "candidate"]


    def test_returncode_124_rehearsal_invalidates_stale_green_record(self, tmp_path):
        """Regression pin (round-6 review fix). Before this fix, a killed
        rehearsal's GateStop exit skipped write_rehearsal_record()
        entirely, so a stale GREEN rehearsal.md left over from a PRIOR
        successful rehearsal survived untouched — and the full-mode
        preflight's substring check on that fixed path would then wave
        through a paid run on evidence from a different, older
        rehearsal."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        (tmp_path / "suite.json").write_text(json.dumps(
            {"tasks": [{"id": "scenario_x", "description": "x"}]}
        ))

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False, rehearsal=True)
        rehearsal_path = ns.spend_ledger.parent / "rehearsal.md"
        rehearsal_path.write_text("Status: **GREEN**\n", encoding="utf-8")

        driver = ThreeArmGateDriver(ns, run_arm_fn=lambda argv, env: 124)
        driver.arm_roots = {}
        driver.preflight = lambda: []

        code = driver.run()

        assert code == 2  # the GateStop exit path, same as any other GATE-STOP
        record_text = rehearsal_path.read_text(encoding="utf-8")
        assert "GREEN" not in record_text, (
            "a killed rehearsal must invalidate the stale record, not leave it standing"
        )


# ---------------------------------------------------------------------------
# A non-finite cost read back from summary.json is sanitised to None,
# not left to blow up spend_ledger.append with an unhandled ValueError
# ---------------------------------------------------------------------------


class TestNonFiniteCostSanitisedBeforeSettlement:
    def test_read_arm_actual_cost_returns_none_for_nan(self, tmp_path):
        """Regression (round-5 fix, MINOR 1). `spend_ledger.append`
        already refuses a non-finite `actual_usd` by raising a bare
        `ValueError` — correct on its own, but `run()`'s handler only
        catches `GateStop`, so a poisoned summary.json used to crash the
        driver as an unhandled traceback with the remaining arms unrun.
        `_read_arm_actual_cost` now degrades a non-finite value to `None`
        — the same "cost unknown" path an altogether-missing summary.json
        already takes, which the settlement books at cap instead of
        raising."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)

        run_dir = tmp_path / "runs" / "g1-raw"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(json.dumps(
            {"cells": {"simple-claude": {"total_cost_usd_or_null": float("nan")}}}
        ))
        driver.args.run_dir = tmp_path / "runs"

        assert driver._read_arm_actual_cost("g1-raw", "simple-claude") is None

    def test_read_arm_actual_cost_returns_none_for_infinity(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)

        run_dir = tmp_path / "runs" / "g1-raw"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(json.dumps(
            {"cells": {"simple-claude": {"total_cost_usd_or_null": float("inf")}}}
        ))
        driver.args.run_dir = tmp_path / "runs"

        assert driver._read_arm_actual_cost("g1-raw", "simple-claude") is None

    def test_a_poisoned_summary_json_does_not_crash_run_arm(self, tmp_path):
        """End-to-end: `run_arm` must not raise when the arm it just ran
        wrote a non-finite cost — it settles at `actual_usd=None` (cap)
        instead of propagating spend_ledger.append's ValueError."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        (tmp_path / "suite.json").write_text(json.dumps(
            {"tasks": [{"id": "scenario_x", "description": "x"}]}
        ))

        def fake_run_arm(argv, env):
            run_id = argv[argv.index("--run-id") + 1]
            run_dir = tmp_path / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "summary.json").write_text(json.dumps(
                {"cells": {"simple-claude": {"total_cost_usd_or_null": float("nan")}}}
            ))
            return 0

        ns = TestDriverPlanOnly()._make_args(
            tmp_path, plan_only=False, run_dir=tmp_path / "runs",
        )
        driver = ThreeArmGateDriver(ns, run_arm_fn=fake_run_arm)
        driver.arm_roots = {}

        returncode = driver.run_arm("raw")  # must not raise

        assert returncode == 0
        assert recorded_total(driver.args.spend_ledger) == driver.caps["raw"]  # booked at cap


# ---------------------------------------------------------------------------
# Teardown is idempotent
# ---------------------------------------------------------------------------


class TestTeardownIdempotent:
    def test_second_teardown_call_does_not_double_charge_ledger(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {}
        driver.spawned_arms = {"candidate"}

        first = driver.teardown()
        second = driver.teardown()

        assert first is True and second is True
        assert recorded_total(ns.spend_ledger) == 16.0


# ---------------------------------------------------------------------------
# The ledger append happens before the in-memory reservation is set
# ---------------------------------------------------------------------------


class TestReservationOrdering:
    def test_failed_ledger_append_prevents_spawn_and_reservation(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(
            ns, run_arm_fn=lambda argv, env: (_ for _ in ()).throw(
                AssertionError("must not spawn if the reservation append failed")),
        )
        driver.arm_roots = {}

        def failing_append(path, row):
            raise OSError("disk full")

        monkeypatch.setattr(gate_mod.spend_ledger, "append", failing_append)

        with pytest.raises(OSError):
            driver.run_arm("raw")

        assert "raw" not in driver.spawned_arms
        assert "raw" not in driver.reservations


# ---------------------------------------------------------------------------
# Fixture-remote containment is a real, enforced check
# ---------------------------------------------------------------------------


class TestFixtureRemoteContainment:
    def test_fixture_repo_with_remote_gate_stops_preflight(self, tmp_path, monkeypatch):
        fixture = tmp_path / "fixture"
        fixture.mkdir()
        subprocess.run(["git", "init"], cwd=fixture, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.com/x.git"],
            cwd=fixture, check=True, capture_output=True,
        )

        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False, fixture_repo=fixture)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        TestDriverPlanOnly()._patch_identity_checks(monkeypatch, tmp_path, ns)

        problems = driver.preflight()
        assert any("still has a remote configured" in p for p in problems)

    def test_fixture_repo_without_remote_passes(self, tmp_path, monkeypatch):
        fixture = tmp_path / "fixture"
        fixture.mkdir()
        subprocess.run(["git", "init"], cwd=fixture, check=True, capture_output=True)

        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False, fixture_repo=fixture)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        TestDriverPlanOnly()._patch_identity_checks(monkeypatch, tmp_path, ns)

        problems = driver.preflight()
        assert not any("still has a remote configured" in p for p in problems)


class TestRunnerFixtureRemoteReassertion:
    def test_worktree_remote_gate_stops_in_gate_mode(self, tmp_path, monkeypatch):
        from quoin.benchmarks.harness import runner

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.com/x.git"],
            cwd=repo, check=True, capture_output=True,
        )
        monkeypatch.setenv("QUOIN_BENCHMARK_GATE", "1")

        with pytest.raises(RuntimeError, match="still has a remote"):
            runner._assert_no_remote_in_gate_mode(repo)

    def test_is_a_noop_outside_gate_mode(self, tmp_path, monkeypatch):
        from quoin.benchmarks.harness import runner

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://example.com/x.git"],
            cwd=repo, check=True, capture_output=True,
        )
        monkeypatch.delenv("QUOIN_BENCHMARK_GATE", raising=False)

        runner._assert_no_remote_in_gate_mode(repo)  # must not raise


# ---------------------------------------------------------------------------
# The driver populates every compare_arms evidence column it can,
# end to end against synthetic result dirs
# ---------------------------------------------------------------------------


class TestDriverPopulatesCompareArmsColumnsEndToEnd:
    def test_turn_count_and_compaction_event_count_flow_from_metrics_json(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import _COLUMNS
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        (tmp_path / "suite.json").write_text(json.dumps(
            {"tasks": [{"id": "scenario_x", "description": "x"}]}
        ))

        def fake_run_arm(argv, env):
            run_id = argv[argv.index("--run-id") + 1]
            cell = argv[argv.index("--cells") + 1]
            task_dir = tmp_path / "runs" / run_id / cell / "scenario_x"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "metrics.json").write_text(json.dumps({
                "turn_count": 7, "compaction_event_count": 2,
            }))
            run_root = tmp_path / "runs" / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            (run_root / "summary.json").write_text(json.dumps({
                "cells": {cell: {"total_cost_usd_or_null": 1.0, "n_tasks": 1, "n_pass": 1}}
            }))
            return 0

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=fake_run_arm)
        driver.arm_roots = {}
        driver.preflight = lambda: []

        code = driver.run()
        assert code == 0

        out_path = tmp_path / "runs" / "g1-comparison" / "three-arm-comparison.md"
        text = out_path.read_text(encoding="utf-8")
        turn_idx = _COLUMNS.index("turn_count")
        compaction_idx = _COLUMNS.index("compaction_event_count")
        seen_arms = set()
        for line in text.splitlines():
            for arm in ("raw", "main", "candidate"):
                if line.startswith(f"| {arm} |"):
                    cells = [c.strip() for c in line.strip("|").split("|")]
                    assert cells[turn_idx] == "7", line
                    assert cells[compaction_idx] == "2", line
                    seen_arms.add(arm)
        assert seen_arms == {"raw", "main", "candidate"}


# ---------------------------------------------------------------------------
# Candidate provenance is rendered from the recorded commit, not
# hardcoded as "this stage's branch HEAD"
# ---------------------------------------------------------------------------


class TestComparisonProvenanceLine:
    def test_candidate_provenance_renders_recorded_commit_not_a_hardcoded_claim(self, tmp_path):
        from quoin.benchmarks.harness.compare_arms import compare_arms

        _write_summary(tmp_path, "g1-main", "quoin-claude", total_cost=10.0)
        _write_summary(tmp_path, "g1-candidate", "quoin-claude", total_cost=8.0)
        out_path = compare_arms(
            run_dir=tmp_path, gate_id="g1",
            arm_run_ids={"main": "g1-main", "candidate": "g1-candidate"},
            arm_cells={"main": "quoin-claude", "candidate": "quoin-claude"},
            arm_evidence={"candidate": {"installed_quoin_commit": "caae5aa4"}},
        )
        text = out_path.read_text(encoding="utf-8")
        assert "caae5aa4" in text
        assert "`candidate` is this stage's branch HEAD" not in text


# ---------------------------------------------------------------------------
# verify_hooks_deployed loads THIS arm's own installer.py, by file path —
# never whichever quoin.installer already sits in sys.modules
# ---------------------------------------------------------------------------


class TestArmInstallerLoadedByFilePath:
    def test_load_arm_installer_loads_the_exact_file_not_an_already_imported_module(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import _load_arm_installer
        import quoin.installer as real_installer  # already imported by the driver's own bootstrap

        arm_root = tmp_path / "arm"
        installer_dir = arm_root / "src" / "quoin"
        installer_dir.mkdir(parents=True)
        (installer_dir / "installer.py").write_text(
            "MARKER = 'arm-own-installer-98765'\n"
            "def expected_deployed_content(src, dest_claude):\n"
            "    return src.read_bytes()\n"
        )
        loaded = _load_arm_installer(arm_root)
        assert loaded is not None
        assert loaded.MARKER == "arm-own-installer-98765"
        assert not hasattr(real_installer, "MARKER")
        assert Path(loaded.__file__).resolve() == (installer_dir / "installer.py").resolve()

    def test_load_arm_installer_returns_none_when_the_arm_has_no_installer(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import _load_arm_installer

        assert _load_arm_installer(tmp_path / "no-such-arm") is None

    def test_verify_hooks_deployed_uses_the_arms_own_expected_content_function(self, tmp_path):
        """Proves the byte-compare is evaluated against THIS arm's own
        installer, not whichever `quoin.installer` happens to already be
        imported: the arm's fake `expected_deployed_content` appends a
        distinctive suffix no real installer would produce, and the
        deployed hook file is written to match it exactly. The old
        `sys.path`-insert-plus-`import_module` mechanism would have
        resolved through the driver's OWN already-imported `quoin.installer`
        regardless of `arm_root`, evaluating this arm's hooks against a
        completely different function and passing (or failing) for the
        wrong reason."""
        from quoin.benchmarks.scripts.run_three_arm_gate import HOOK_SCRIPTS, verify_hooks_deployed

        arm_root = tmp_path / "arm"
        installer_dir = arm_root / "src" / "quoin"
        installer_dir.mkdir(parents=True)
        (installer_dir / "installer.py").write_text(
            "def expected_deployed_content(src, dest_claude):\n"
            "    return src.read_bytes() + b'-ARM-MARKER'\n"
        )
        hooks_src = arm_root / "quoin" / "hooks"
        hooks_src.mkdir(parents=True)
        home = tmp_path / "home"
        hooks_dest = home / ".claude" / "hooks"
        hooks_dest.mkdir(parents=True)
        for fname in HOOK_SCRIPTS:
            (hooks_src / fname).write_text(f"# {fname}\n")
            (hooks_dest / fname).write_bytes((hooks_src / fname).read_bytes() + b"-ARM-MARKER")

        assert verify_hooks_deployed(arm_root, home) is True

        # A deployed hook missing the arm-specific marker must fail —
        # proving the comparison is genuinely evaluated against THIS arm's
        # `expected_deployed_content`, not a real installer's (which would
        # have no idea about "-ARM-MARKER" either way).
        (hooks_dest / HOOK_SCRIPTS[0]).write_bytes((hooks_src / HOOK_SCRIPTS[0]).read_bytes())
        assert verify_hooks_deployed(arm_root, home) is False


# ---------------------------------------------------------------------------
# candidate_about_version_matches: version equality alone is not enough —
# the reported __file__ must also resolve under arm_root
# ---------------------------------------------------------------------------


class TestCandidateVersionTwoFactorCheck:
    def _write_about(self, arm_root, version):
        about_dir = arm_root / "src" / "quoin"
        about_dir.mkdir(parents=True)
        (about_dir / "__about__.py").write_text(f'__version__ = "{version}"\n')

    def test_matching_version_but_file_outside_arm_root_fails(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts import run_three_arm_gate as gate_mod

        arm_root = tmp_path / "arm"
        self._write_about(arm_root, "9.9.9")

        class FakeResult:
            returncode = 0
            stdout = "9.9.9\n/somewhere/else/quoin/__init__.py\n"

        monkeypatch.setattr(gate_mod.subprocess, "run", lambda *a, **kw: FakeResult())
        assert gate_mod.candidate_about_version_matches(arm_root, "python3") is False

    def test_matching_version_and_file_under_arm_root_passes(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts import run_three_arm_gate as gate_mod

        arm_root = tmp_path / "arm"
        self._write_about(arm_root, "9.9.9")
        reported_file = arm_root / "src" / "quoin" / "__init__.py"

        class FakeResult:
            returncode = 0
            stdout = f"9.9.9\n{reported_file}\n"

        monkeypatch.setattr(gate_mod.subprocess, "run", lambda *a, **kw: FakeResult())
        assert gate_mod.candidate_about_version_matches(arm_root, "python3") is True

    def test_mismatched_version_fails_even_with_a_valid_path(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts import run_three_arm_gate as gate_mod

        arm_root = tmp_path / "arm"
        self._write_about(arm_root, "9.9.9")
        reported_file = arm_root / "src" / "quoin" / "__init__.py"

        class FakeResult:
            returncode = 0
            stdout = f"1.0.0\n{reported_file}\n"

        monkeypatch.setattr(gate_mod.subprocess, "run", lambda *a, **kw: FakeResult())
        assert gate_mod.candidate_about_version_matches(arm_root, "python3") is False


# ---------------------------------------------------------------------------
# Teardown asserts a BARE import resolves under the git root and matches
# the PRE_PROVENANCE value recorded before the arm loop (R-14)
# ---------------------------------------------------------------------------


class TestTeardownBareImportProvenance:
    def test_teardown_fails_when_bare_import_diverges_from_pre_provenance(self, tmp_path, monkeypatch):
        """Simulates the R-14 hazard directly: the machine's bare `import
        quoin` resolves somewhere OTHER than what was recorded before the
        arm loop (e.g. left pointing at a temp worktree). Teardown must
        report unverified — the OLD inverted check
        (`verify_arm_installer_isolable` on the candidate) would have
        passed here, since that only proves the arm mechanism still works,
        not that the machine is back on its own install."""
        import types as _types
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"candidate": tmp_path / "candidate"}
        driver.home = tmp_path / "home"
        (driver.home / ".claude").mkdir(parents=True)
        driver.pre_provenance["bare_quoin_file"] = str(gate_mod._repo_root / "src" / "quoin" / "__init__.py")

        monkeypatch.setattr(
            gate_mod, "install_arm",
            lambda root, py: _types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(
            gate_mod, "bare_import_quoin_file",
            lambda py: str(tmp_path / "candidate" / "src" / "quoin" / "__init__.py"),
        )
        assert driver.teardown() is False

    def test_teardown_passes_when_bare_import_matches_pre_provenance_under_git_root(self, tmp_path, monkeypatch):
        import types as _types
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"candidate": tmp_path / "candidate"}
        driver.home = tmp_path / "home"
        (driver.home / ".claude").mkdir(parents=True)
        recorded = str(gate_mod._repo_root / "src" / "quoin" / "__init__.py")
        driver.pre_provenance["bare_quoin_file"] = recorded

        monkeypatch.setattr(
            gate_mod, "install_arm",
            lambda root, py: _types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(gate_mod, "bare_import_quoin_file", lambda py: recorded)
        assert driver.teardown() is True

    def test_teardown_fails_when_pre_provenance_was_never_recorded(self, tmp_path, monkeypatch):
        """No PRE_PROVENANCE at all (e.g. preflight never ran) must not
        read as trivially verified."""
        import types as _types
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"candidate": tmp_path / "candidate"}
        driver.home = tmp_path / "home"
        (driver.home / ".claude").mkdir(parents=True)
        # driver.pre_provenance deliberately left empty.

        monkeypatch.setattr(
            gate_mod, "install_arm",
            lambda root, py: _types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(gate_mod, "bare_import_quoin_file", lambda py: "/some/path/quoin/__init__.py")
        assert driver.teardown() is False

    def test_teardown_fails_when_bare_import_matches_recording_but_is_not_under_repo_root(
        self, tmp_path, monkeypatch,
    ):
        """The one branch review-3 found untested: PRE_PROVENANCE and the
        post-run bare import AGREE with each other (so the equality check
        alone would pass) but BOTH point outside `_repo_root` — the
        wrong-interpreter hazard T-13 documents (a stale non-editable
        install in some other interpreter's site-packages). Proves the
        `relative_to(_repo_root)` assertion is load-bearing on its own, not
        redundant with the equality check."""
        import types as _types
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"candidate": tmp_path / "candidate"}
        driver.home = tmp_path / "home"
        (driver.home / ".claude").mkdir(parents=True)
        outside_repo_root = str(tmp_path / "some-other-venv" / "site-packages" / "quoin" / "__init__.py")
        driver.pre_provenance["bare_quoin_file"] = outside_repo_root

        monkeypatch.setattr(
            gate_mod, "install_arm",
            lambda root, py: _types.SimpleNamespace(returncode=0),
        )
        monkeypatch.setattr(gate_mod, "bare_import_quoin_file", lambda py: outside_repo_root)
        assert driver.teardown() is False

    def test_preflight_gate_stops_when_bare_import_resolves_outside_repo_root(self, tmp_path, monkeypatch):
        """Regression (round-4 fix: MAJOR 10). Pre-fix, this same
        wrong-interpreter property was only ever asserted in teardown,
        AFTER all three arms had already spent the full authorisation — a
        wrong-interpreter launch spent ~$38 before failing. Now asserted in
        preflight too, so it stops at $0."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=True)
        driver = ThreeArmGateDriver(ns)
        monkeypatch.setattr(
            gate_mod, "verify_arm_installer_isolable", lambda root, py: True,
        )
        monkeypatch.setattr(
            gate_mod, "cross_arm_manifest",
            lambda root: {"main": 1} if root == ns.main_worktree else {"candidate": 2},
        )
        TestDriverPlanOnly()._patch_identity_checks(monkeypatch, tmp_path, ns)
        outside_repo_root = str(tmp_path / "some-other-venv" / "site-packages" / "quoin" / "__init__.py")
        monkeypatch.setattr(gate_mod, "bare_import_quoin_file", lambda py: outside_repo_root)
        problems = driver.preflight()
        assert any("outside the git checkout" in p for p in problems)

    def test_preflight_does_not_gate_stop_when_bare_import_resolves_under_repo_root(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        tmp_path.joinpath("main").mkdir()
        tmp_path.joinpath("candidate").mkdir()
        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=True)
        driver = ThreeArmGateDriver(ns)
        monkeypatch.setattr(
            gate_mod, "verify_arm_installer_isolable", lambda root, py: True,
        )
        monkeypatch.setattr(
            gate_mod, "cross_arm_manifest",
            lambda root: {"main": 1} if root == ns.main_worktree else {"candidate": 2},
        )
        TestDriverPlanOnly()._patch_identity_checks(monkeypatch, tmp_path, ns)
        under_repo_root = str(gate_mod._repo_root / "src" / "quoin" / "__init__.py")
        monkeypatch.setattr(gate_mod, "bare_import_quoin_file", lambda py: under_repo_root)
        problems = driver.preflight()
        assert not any("outside the git checkout" in p for p in problems)


# ---------------------------------------------------------------------------
# The live model probe never fires once preflight has already collected a
# fatal problem, and its own budget is folded into the ledger precheck
# ---------------------------------------------------------------------------


class TestLiveProbeShortCircuitsOnExistingProblems:
    def test_verify_model_not_called_when_a_problem_already_exists(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        # Worktrees deliberately left non-existent — an early GATE-STOP —
        # so preflight collects a problem well before the live-probe block.
        import quoin.benchmarks.scripts.run_benchmark as rb_mod

        def must_not_be_called(**kw):
            raise AssertionError("verify_model must not fire once a problem already exists")

        monkeypatch.setattr(rb_mod, "verify_model", must_not_be_called)

        problems = driver.preflight()
        assert any("both arm worktrees must exist" in p for p in problems)

    def test_probe_budget_is_folded_into_the_ledger_precheck(self, tmp_path, monkeypatch):
        """`planned_caps` passed to `spend_ledger.precheck` must include
        the live probe's own cap — otherwise the probe can spend beyond
        what the precheck actually verified fits the authorisation."""
        from quoin.benchmarks.scripts.run_three_arm_gate import (
            MODEL_PROBE_MAX_BUDGET_USD, ThreeArmGateDriver,
        )
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        captured = {}
        real_precheck = gate_mod.spend_ledger.precheck

        def spy_precheck(path, planned_caps, authorised):
            captured["planned_caps"] = list(planned_caps)
            return real_precheck(path, planned_caps=planned_caps, authorised=authorised)

        monkeypatch.setattr(gate_mod.spend_ledger, "precheck", spy_precheck)

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns)
        driver.arm_roots = {"main": tmp_path / "main", "candidate": tmp_path / "candidate"}
        driver.preflight()

        assert "planned_caps" in captured
        assert MODEL_PROBE_MAX_BUDGET_USD in captured["planned_caps"]
        assert sum(captured["planned_caps"]) == pytest.approx(
            6.0 + 16.0 + 16.0 + MODEL_PROBE_MAX_BUDGET_USD
        )


# ---------------------------------------------------------------------------
# --authorised-usd is required in full mode; precheck no longer derives
# it silently from the ledger it polices
# ---------------------------------------------------------------------------


class TestAuthorisedUsdRequiredInFullMode:
    def test_full_mode_without_authorised_usd_gate_stops_before_any_file_io(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False, authorised_usd=None)
        driver = ThreeArmGateDriver(ns)
        problems = driver.preflight()
        assert any("--authorised-usd is required" in p for p in problems)

    def test_rehearsal_mode_requires_authorised_usd(self, tmp_path):
        """Round-4 fix (MAJOR 8): --rehearsal is a real, paid mode (~$3),
        so it must not be exempt from the explicit-ceiling requirement —
        only --plan-only (genuinely spend-free) stays exempt. Regression:
        pre-fix, this returned no such problem and let `preflight` fall
        through to `resolve_authorised_ceiling`'s derive-from-ledger
        fallback."""
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(
            tmp_path, rehearsal=True, plan_only=False, authorised_usd=None,
        )
        driver = ThreeArmGateDriver(ns)
        problems = driver.preflight()
        assert any("--authorised-usd is required" in p for p in problems)

    def test_rehearsal_mode_with_authorised_usd_does_not_hit_that_problem(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(
            tmp_path, rehearsal=True, plan_only=False, authorised_usd=50.0,
        )
        driver = ThreeArmGateDriver(ns)
        problems = driver.preflight()
        assert not any("--authorised-usd is required" in p for p in problems)

    def test_plan_only_mode_does_not_require_authorised_usd(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=True, authorised_usd=None)
        driver = ThreeArmGateDriver(ns)
        problems = driver.preflight()
        assert not any("--authorised-usd is required" in p for p in problems)


# ---------------------------------------------------------------------------
# settings.json: a pre-gate on-disk backup, and every rewrite is atomic
# ---------------------------------------------------------------------------


class TestSettingsJsonAtomicAndBackedUp:
    def test_pre_gate_backup_file_is_written_before_the_arm_loop(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=lambda argv, env: 0)
        driver.arm_roots = {}
        driver.home = tmp_path / "home"
        (driver.home / ".claude").mkdir(parents=True)
        original = json.dumps({"hooks": {}})
        (driver.home / ".claude" / "settings.json").write_text(original, encoding="utf-8")

        monkeypatch.setattr(driver, "preflight", lambda: [])
        driver.run()

        backup_path = ns.spend_ledger.parent / f"settings.json.pre-gate-{ns.gate_id}"
        assert backup_path.exists()
        assert backup_path.read_bytes() == original.encode("utf-8")
        # No leftover .tmp sibling from the atomic-write helper.
        assert not backup_path.with_suffix(backup_path.suffix + ".tmp").exists()

    def test_atomic_write_leaves_no_tmp_file_and_content_is_correct(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import _atomic_write_bytes

        target = tmp_path / "settings.json"
        target.write_bytes(b"old content")
        _atomic_write_bytes(target, b"new content")
        assert target.read_bytes() == b"new content"
        assert not target.with_suffix(target.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# The raw arm's isolated config dir is created via mkdtemp — never a
# fixed, guessable, exist_ok-adoptable path
# ---------------------------------------------------------------------------


class TestRawConfigDirSecureCreation:
    def test_two_invocations_get_distinct_unpredictable_directories(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        ns1 = TestDriverPlanOnly()._make_args(tmp_path / "a")
        ns2 = TestDriverPlanOnly()._make_args(tmp_path / "b")
        d1 = ThreeArmGateDriver(ns1)
        d2 = ThreeArmGateDriver(ns2)
        d1._ensure_raw_config_dir()
        d2._ensure_raw_config_dir()
        assert d1.raw_config_dir != d2.raw_config_dir
        assert d1.raw_config_dir.exists()
        assert d2.raw_config_dir.exists()

    def test_a_pre_existing_directory_at_the_old_fixed_path_is_never_adopted(self, tmp_path, monkeypatch):
        """Regression: the old fixed path
        `$TMPDIR/quoin-gate/raw-config` combined with
        `mkdir(exist_ok=True)` would silently ADOPT a pre-existing,
        attacker-owned directory there. `mkdtemp` never reuses an
        existing directory, so a pre-planted one at the old path is
        simply irrelevant — the driver's `raw_config_dir` never points at
        it."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        old_fixed_path = tmp_path / "quoin-gate" / "raw-config"
        old_fixed_path.mkdir(parents=True)
        (old_fixed_path / "settings.json").write_text('{"planted": true}', encoding="utf-8")

        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver._ensure_raw_config_dir()
        assert driver.raw_config_dir != old_fixed_path
        assert not (driver.raw_config_dir / "settings.json").exists()

    def test_no_fixed_intermediate_directory_is_created_under_tmpdir(self, tmp_path):
        """Regression (round-4 fix: MAJOR 9). The old
        `$TMPDIR/quoin-gate/raw-config-*` shape had `$TMPDIR/quoin-gate`
        as a fixed, attacker-pre-creatable intermediate — `mkdir(parents=
        True, exist_ok=True)` follows a symlink planted there. `mkdtemp`
        now runs directly against `tempfile.gettempdir()` with no fixed
        intermediate directory left to pre-create. (Not monkeypatching
        TMPDIR here — `tempfile.gettempdir()` caches its result process-
        wide on first call, so a later env-var override is not honoured
        within one test session; this test instead checks the real
        system temp dir the driver actually used.)"""
        import tempfile as _tempfile

        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver._ensure_raw_config_dir()
        # The new directory itself must land directly under the system
        # temp dir, not inside any fixed "quoin-gate" intermediate — an
        # already-leaked `quoin-gate/` from an earlier dev run (unrelated
        # to this fix) may still exist on this machine, so the assertion
        # is on THIS call's own result, not on global filesystem state.
        assert driver.raw_config_dir.parent.name != "quoin-gate"
        assert driver.raw_config_dir.parent == Path(_tempfile.gettempdir())

    def test_assert_raw_config_dir_safe_passes_for_a_freshly_created_mkdtemp_dir(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver._ensure_raw_config_dir()  # must not raise
        driver._assert_raw_config_dir_safe()  # re-run must also not raise

    def test_assert_raw_config_dir_safe_rejects_a_symlink_substituted_for_the_directory(self, tmp_path):
        """Regression (round-4 fix: MAJOR 9). Proves the exact TOCTOU the
        review demonstrated: the 0700 directory `mkdtemp` created is
        renamed away and replaced by a symlink to an attacker-owned
        directory before the spawn. `os.lstat` (never `os.stat`) catches
        the substitution instead of following it."""
        from quoin.benchmarks.scripts.run_three_arm_gate import GateStop, ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver._ensure_raw_config_dir()
        real_dir = driver.raw_config_dir
        attacker_dir = tmp_path / "attacker-owned"
        attacker_dir.mkdir(mode=0o700)
        shutil.rmtree(real_dir)
        os.symlink(attacker_dir, real_dir)
        with pytest.raises(GateStop, match="not a plain directory|symlink"):
            driver._assert_raw_config_dir_safe()

    def test_assert_raw_config_dir_safe_rejects_group_or_other_accessible_mode(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import GateStop, ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver._ensure_raw_config_dir()
        os.chmod(driver.raw_config_dir, 0o755)
        with pytest.raises(GateStop, match="0700|accessible"):
            driver._assert_raw_config_dir_safe()

    def test_assert_raw_config_dir_safe_rejects_wrong_owner(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import GateStop, ThreeArmGateDriver
        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod

        ns = TestDriverPlanOnly()._make_args(tmp_path)
        driver = ThreeArmGateDriver(ns)
        driver._ensure_raw_config_dir()
        real_lstat = gate_mod.os.lstat

        class _FakeStat:
            def __init__(self, real):
                self.st_mode = real.st_mode
                self.st_uid = real.st_uid + 1  # never the real uid

        monkeypatch.setattr(gate_mod.os, "lstat", lambda p: _FakeStat(real_lstat(p)))
        with pytest.raises(GateStop, match="not this process's uid"):
            driver._assert_raw_config_dir_safe()

    def test_run_arm_re_asserts_raw_config_dir_safety_immediately_before_spawn(self, tmp_path):
        """End-to-end: `run_arm` itself calls the re-assertion right
        before `run_arm_fn`, not just at creation time — a directory
        compromised AFTER creation but BEFORE spawn must still be caught."""
        from quoin.benchmarks.scripts.run_three_arm_gate import GateStop, ThreeArmGateDriver

        ns = TestDriverPlanOnly()._make_args(tmp_path, plan_only=False)
        driver = ThreeArmGateDriver(ns, run_arm_fn=lambda argv, env: (_ for _ in ()).throw(
            AssertionError("must not spawn once raw_config_dir safety fails")
        ))
        driver.arm_roots = {}
        driver._ensure_raw_config_dir()
        os.chmod(driver.raw_config_dir, 0o777)
        with pytest.raises(GateStop):
            driver.run_arm("raw")


# ---------------------------------------------------------------------------
# --spend-ledger inside the quoin git checkout is a GATE-STOP (IVG-119
# stranded-nested-root); --run-dir is resolved at parse time
# ---------------------------------------------------------------------------


class TestStrandedNestedLedgerAndRunDirResolution:
    def test_ledger_inside_the_quoin_repo_is_a_gate_stop(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import (
            _repo_root, _stranded_nested_ledger_problem,
        )

        nested = _repo_root / ".workflow_artifacts" / "some-task" / "spend-ledger.jsonl"
        problem, bypassed = _stranded_nested_ledger_problem(nested)
        assert problem is not None
        assert "IVG-119" in problem
        assert bypassed is False

    def test_ledger_outside_the_quoin_repo_is_fine(self, tmp_path):
        from quoin.benchmarks.scripts.run_three_arm_gate import _stranded_nested_ledger_problem

        problem, bypassed = _stranded_nested_ledger_problem(tmp_path / "ledger.jsonl")
        assert problem is None
        assert bypassed is False

    def test_run_dir_inside_the_quoin_repo_is_also_a_gate_stop(self, tmp_path):
        """Regression (round-4 fix: MAJOR 11). --run-dir is a spend-relevant
        lookup key too (`_read_arm_actual_cost` reads every arm's cost back
        from under it) — pre-fix, only --spend-ledger was guarded, so a
        run-dir landing in the same stranded nested root wrote real paid
        evidence somewhere nothing downstream would ever look for it."""
        from quoin.benchmarks.scripts.run_three_arm_gate import (
            _repo_root, _stranded_nested_ledger_problem,
        )

        nested_run_dir = _repo_root / ".workflow_artifacts" / "quoin-benchmarks" / "runs"
        problem, bypassed = _stranded_nested_ledger_problem(nested_run_dir)
        assert problem is not None
        assert "IVG-119" in problem
        assert bypassed is False

    def test_full_preflight_rejects_a_stranded_run_dir(self, tmp_path, monkeypatch):
        """End-to-end proof that preflight() itself, not just the bare
        predicate, now checks --run-dir."""
        from quoin.benchmarks.scripts.run_three_arm_gate import (
            ThreeArmGateDriver, _repo_root,
        )

        ns = TestDriverPlanOnly()._make_args(
            tmp_path, run_dir=_repo_root / ".workflow_artifacts" / "some-task" / "runs",
        )
        driver = ThreeArmGateDriver(ns)
        problems = driver.preflight()
        assert any("--run-dir" in p and "IVG-119" in p for p in problems)

    def test_allow_nested_ledger_env_var_is_an_escape_hatch(self, tmp_path, monkeypatch):
        """Regression (round-4 fix: MAJOR 15). The stranded-nested-root
        predicate encodes THIS workspace's two-level layout, not a
        universal invariant — a standalone quoin clone (the layout
        /init_workflow produces, where the project root IS the git root)
        would otherwise have every valid --spend-ledger path refused with
        no override."""
        from quoin.benchmarks.scripts.run_three_arm_gate import (
            _repo_root, _stranded_nested_ledger_problem,
        )

        nested = _repo_root / ".workflow_artifacts" / "some-task" / "spend-ledger.jsonl"
        monkeypatch.setenv("QUOIN_ALLOW_NESTED_LEDGER", "1")
        problem, bypassed = _stranded_nested_ledger_problem(nested)
        assert problem is None
        assert bypassed is True

    def test_escape_hatch_use_is_recorded_in_provenance_and_help_text(self, tmp_path, monkeypatch):
        """Regression (round-5 fix, MINOR 5): the escape hatch used to
        return silently with no trace anywhere an operator or a post-hoc
        audit would see it. Now preflight() records the bypass in
        `pre_provenance` (per path argument) and the env var is named in
        both the GATE-STOP message and the --spend-ledger/--run-dir
        --help text."""
        from quoin.benchmarks.scripts.run_three_arm_gate import (
            ThreeArmGateDriver, main, _repo_root,
        )

        monkeypatch.setenv("QUOIN_ALLOW_NESTED_LEDGER", "1")
        ns = TestDriverPlanOnly()._make_args(
            tmp_path,
            spend_ledger=_repo_root / ".workflow_artifacts" / "some-task" / "spend-ledger.jsonl",
            run_dir=_repo_root / ".workflow_artifacts" / "some-task" / "runs",
        )
        driver = ThreeArmGateDriver(ns)
        driver.preflight()
        assert driver.pre_provenance.get("nested_root_guard_bypassed_spend_ledger") is True
        assert driver.pre_provenance.get("nested_root_guard_bypassed_run_dir") is True

        # main() builds its parser inline, so exercise it directly via -h
        # (which argparse serves by printing help and raising SystemExit)
        # rather than reconstructing an equivalent parser by hand.
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
            main(["-h"])
        help_text = buf.getvalue()
        assert "QUOIN_ALLOW_NESTED_LEDGER" in help_text

    def test_run_dir_is_resolved_to_an_absolute_path_at_parse_time(self, tmp_path, monkeypatch):
        from quoin.benchmarks.scripts.run_three_arm_gate import main

        monkeypatch.chdir(tmp_path)
        captured = {}

        class _StubDriver:
            def __init__(self, args):
                captured["run_dir"] = args.run_dir

            def run(self):
                return 0

        import quoin.benchmarks.scripts.run_three_arm_gate as gate_mod
        monkeypatch.setattr(gate_mod, "ThreeArmGateDriver", _StubDriver)

        main([
            "--gate-id", "g1", "--suite", "suite.json", "--fixture-repo", "fixture",
            "--main-worktree", "main", "--candidate-worktree", "candidate",
            "--main-commit", "a", "--candidate-commit", "b",
            "--max-budget-usd-raw", "1", "--max-budget-usd-main", "1",
            "--max-budget-usd-candidate", "1", "--spend-ledger", "ledger.jsonl",
            "--new-spend-ledger", "--run-dir", "runs", "--plan-only",
        ])
        assert captured["run_dir"].is_absolute()
        assert captured["run_dir"] == (tmp_path / "runs").resolve()
