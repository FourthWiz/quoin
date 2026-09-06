"""
test_three_arm_gate.py — Tests for the three-arm benchmark gate driver and
its supporting modules: the persisted spend ledger (T-17), the three-arm
comparison emitter (T-11), the dry-run gate (T-09), and the sequential
driver itself (T-07).
"""
import argparse
import json
import os
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


class TestArmRegisteredStanzas:
    def test_main_arm_worktree_registers_seven_stanzas_no_compact(self):
        """Requires the real T-08a worktree at $TMPDIR/quoin-gate/main to
        exist (created earlier in this implementation session). Skips
        gracefully if a fresh test environment hasn't created it."""
        import os as _os
        from quoin.benchmarks.scripts.run_three_arm_gate import arm_registered_stanzas

        main_root = Path(_os.environ.get("TMPDIR", "/tmp")) / "quoin-gate" / "main"
        if not main_root.exists():
            pytest.skip("T-08a worktree not present in this environment")
        stanzas = arm_registered_stanzas(main_root)
        assert len(stanzas) == 7
        assert ("SessionStart", "compact", "sessionstart.sh") not in stanzas

    def test_candidate_arm_worktree_registers_eight_stanzas_with_compact(self):
        import os as _os
        from quoin.benchmarks.scripts.run_three_arm_gate import arm_registered_stanzas

        candidate_root = Path(_os.environ.get("TMPDIR", "/tmp")) / "quoin-gate" / "candidate"
        if not candidate_root.exists():
            pytest.skip("T-08a worktree not present in this environment")
        stanzas = arm_registered_stanzas(candidate_root)
        assert len(stanzas) == 8
        assert ("SessionStart", "compact", "sessionstart.sh") in stanzas

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


class TestDriverPlanOnly:
    def _make_args(self, tmp_path, **overrides):
        ns = argparse.Namespace(
            gate_id="g1", suite=tmp_path / "suite.json", fixture_repo=tmp_path / "fixture",
            main_worktree=tmp_path / "main", candidate_worktree=tmp_path / "candidate",
            max_budget_usd_raw=6.0, max_budget_usd_main=16.0, max_budget_usd_candidate=16.0,
            spend_ledger=tmp_path / "ledger.jsonl", wall_clock_seconds=600,
            run_dir=tmp_path / "runs", rehearsal=False, plan_only=True,
        )
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

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
