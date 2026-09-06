"""
test_benchmark_gate_plumbing.py — Tests for the three-arm benchmark gate's
cross-cutting plumbing.

Home for T-01 (config->cell kwarg threading, verdict short-circuit, judge
invocation_extra channel, spend-critical thread-omission guard), T-02
(install/commit/budget cell guards), T-03 (CLI wiring + between-task spend
tracker), T-04 (model pin acceptance) and T-05 (HarnessConfig.cells /
run_dir invariants) — per the plan's T-05 spec, this is their shared home.
"""
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest


# ---------------------------------------------------------------------------
# T-01: config->cell kwarg threading (D-11)
# ---------------------------------------------------------------------------


class TestKwargThreading:
    def test_adapter_declaring_field_receives_configs_value(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        received = {}

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id, quoin_install_script=None):
                received["quoin_install_script"] = quoin_install_script
                return {"verdict": "pass", "extra": {}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path, quoin_install_script=Path("x/install.sh"))
        runner.run_one_task(
            cell="stub-cell",
            task_spec={"id": "t1", "source": "unknown"},
            run_id="r1",
            config=config,
        )
        assert received["quoin_install_script"] == Path("x/install.sh")

    def test_adapter_declaring_neither_gets_only_base_four_kwargs(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        seen = {}

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id):
                seen["called"] = True
                return {"verdict": "pass", "extra": {}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        assert seen["called"] is True

    def test_adapter_declaring_only_repo_root_is_not_given_install_script(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.harness.judge import JudgeResult

        monkeypatch.setattr(
            runner, "judge_task",
            lambda *a, **kw: JudgeResult(task_id="t1", source_benchmark="unknown",
                                          verdict="pass", evidence_path=None,
                                          judge_runtime_seconds=0.0),
        )
        received = {}

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id, quoin_repo_root=None):
                received["quoin_repo_root"] = quoin_repo_root
                return {"verdict": None, "extra": {}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        result = runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        assert received["quoin_repo_root"] is None
        assert result.verdict == "pass"

    def test_stub_returning_no_verdict_is_judged_exactly_as_today(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.harness.judge import JudgeResult

        def stub_judge(task_id, task_dir, run_id, invocation_extra=None, evidence_dir=None):
            return JudgeResult(task_id=task_id, source_benchmark="unknown", verdict="fail",
                                evidence_path=None, judge_runtime_seconds=0.0)

        monkeypatch.setattr(runner, "judge_task", stub_judge)

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id):
                return {}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        result = runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        assert result.verdict == "fail"


# ---------------------------------------------------------------------------
# T-01: cell-reported terminal verdict short-circuits the judge (D-03)
# ---------------------------------------------------------------------------


class TestVerdictShortCircuit:
    def test_cell_error_verdict_skips_judge(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        def raising_judge(*a, **kw):
            raise AssertionError("judge_task must not be called on a terminal cell verdict")

        monkeypatch.setattr(runner, "judge_task", raising_judge)

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id):
                return {"verdict": "error", "extra": {"failure_reason": "boom"}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        result = runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        assert result.verdict == "error"
        assert result.evidence_path == "boom"

    def test_cell_timeout_verdict_skips_judge(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        monkeypatch.setattr(
            runner, "judge_task",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not be called")),
        )

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id):
                return {"verdict": "timeout", "extra": {}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        result = runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        assert result.verdict == "timeout"

    def test_normal_verdict_still_calls_judge_with_invocation_extra(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.harness.judge import JudgeResult

        calls = []

        def stub_judge(task_id, task_dir, run_id, invocation_extra=None, evidence_dir=None):
            calls.append(invocation_extra)
            return JudgeResult(task_id=task_id, source_benchmark="unknown", verdict="pass",
                                evidence_path=None, judge_runtime_seconds=0.0)

        monkeypatch.setattr(runner, "judge_task", stub_judge)

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id):
                return {"extra": {"installed_quoin_commit": "abc123"}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        result = runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        assert result.verdict == "pass"
        assert len(calls) == 1
        assert calls[0]["installed_quoin_commit"] == "abc123"


# ---------------------------------------------------------------------------
# T-01: extra keys reach metrics.json and never collide with a base key
# ---------------------------------------------------------------------------


class TestExtraMetricsMerge:
    def test_base_metrics_key_set_derived_from_source_is_unchanged(self):
        """Guards against the base key set drifting silently (round-3 note)."""
        import inspect
        from quoin.benchmarks.harness import result_writer

        src = inspect.getsource(result_writer.write_run_result)
        # The base keys written unconditionally or conditionally before
        # `metrics_data.update(result.extra)` — read from source so this
        # assertion cannot go stale independently of the code.
        unconditional = {"task_id", "cell", "wall_clock_seconds", "turn_count",
                          "gate_intervention_count"}
        conditional = {"tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write"}
        for key in unconditional | conditional:
            assert f'"{key}"' in src, f"expected base metrics key {key!r} in write_run_result source"
        assert "metrics_data.update(result.extra)" in src

    def test_added_keys_do_not_collide_with_base_metrics_keys(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import RunResult, write_run_result
        import json as _json

        base_keys = {
            "task_id", "cell", "wall_clock_seconds", "turn_count",
            "gate_intervention_count", "tokens_in", "tokens_out",
            "tokens_cache_read", "tokens_cache_write",
        }
        added_keys = {
            "installed_quoin_commit", "expected_quoin_commit", "install_ok",
            "install_reason", "install_returncode", "threaded_kwargs",
            "workflow_artifacts_captured", "workflow_artifacts_has_arch",
            "workflow_artifacts_has_plan", "max_budget_usd_applied",
            "budget_cap_armed", "expected_quoin_commit_armed", "failure_reason",
        }
        assert added_keys.isdisjoint(base_keys), "an added extra key collides with a base metrics key"

        result = RunResult(
            cell="c", task_id="t", run_id="r", verdict="pass",
            extra={"installed_quoin_commit": "abc123", "threaded_kwargs": ["expected_quoin_commit"]},
        )
        out_dir = write_run_result(result, tmp_path)
        metrics = _json.loads((out_dir / "metrics.json").read_text())
        assert metrics["installed_quoin_commit"] == "abc123"
        assert metrics["threaded_kwargs"] == ["expected_quoin_commit"]

    def test_threaded_kwargs_recorded_on_the_run_result(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id, quoin_install_script=None):
                return {"verdict": "pass", "extra": {}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        result = runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        assert result.extra["threaded_kwargs"] == ["quoin_install_script"]


# ---------------------------------------------------------------------------
# T-01: spend-critical thread-omission guard (ConfigThreadError)
# ---------------------------------------------------------------------------


class TestConfigThreadError:
    def test_raises_when_no_cell_declares_spend_critical_field(self, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        # simple-codex never declares max_budget_usd or expected_quoin_commit
        # in any task this stage adds — a stable negative case regardless of
        # T-14's later edits to the Claude cells.
        config = HarnessConfig(run_dir=tmp_path, cells=["simple-codex"])
        config.max_budget_usd = 5.0  # T-14 field; set dynamically — T-14 lands after T-01
        with pytest.raises(runner.ConfigThreadError):
            runner.run_cell(cell="simple-codex", suite=[], run_id="r1", config=config)

    def test_does_not_raise_when_spend_critical_fields_unset(self, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        # quoin_install_script / quoin_install_mode hold non-None defaults but
        # are NOT spend-critical fields, so a caller leaving both spend
        # fields None must not raise (round-5 fix, MIN-6).
        config = HarnessConfig(run_dir=tmp_path, cells=["simple-claude"])
        result = runner.run_cell(cell="simple-claude", suite=[], run_id="r1", config=config)
        assert result.cell == "simple-claude"

    def test_does_not_raise_when_at_least_one_cell_declares_the_field(self, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig

        config = HarnessConfig(run_dir=tmp_path, cells=["quoin-claude"])
        config.expected_quoin_commit = "abc123"
        # quoin-claude declares expected_quoin_commit (T-01) — must not raise.
        result = runner.run_cell(cell="quoin-claude", suite=[], run_id="r1", config=config)
        assert result.cell == "quoin-claude"


# ---------------------------------------------------------------------------
# T-02: install/commit/budget cell guards
# ---------------------------------------------------------------------------


@pytest.fixture
def arm_git_repo(tmp_path):
    """A minimal git repo standing in for an arm worktree, with an
    install.sh at {root}/quoin/install.sh so `arm_root` resolution
    (quoin_install_script.resolve().parent.parent) lands on `root`."""
    import subprocess

    root = tmp_path / "arm"
    (root / "quoin").mkdir(parents=True)
    (root / "quoin" / "install.sh").write_text("#!/bin/bash\nexit 0\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return root, sha


class _FakeStdout:
    """A file-like stdout stub: EOF on the first readline()."""
    def readline(self):
        return ""


class _FakeClaudeProc:
    """Stands in for a spawned `claude` process: no output, exits clean."""
    stdout = _FakeStdout()
    stderr = None

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _patch_claude_popen(monkeypatch, module, on_claude_spawn=None):
    """Patch `module.subprocess.Popen` so it only intercepts a `claude` CLI
    spawn; every other Popen call (including CPython's own `subprocess.run`,
    which is implemented in terms of `Popen` — so a blanket patch also
    breaks the install.sh / git rev-parse calls made by the SAME code path)
    is delegated to the real implementation.

    `on_claude_spawn`: `None` (default) raises — proves `claude` was never
    reached; a callable receiving `cmd` and returning a fake proc simulates
    a completed spawn instead.
    """
    real_popen = module.subprocess.Popen

    def wrapper(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
            if on_claude_spawn is None:
                raise AssertionError("must not spawn the claude CLI")
            return on_claude_spawn(cmd)
        return real_popen(cmd, *a, **kw)

    monkeypatch.setattr(module.subprocess, "Popen", wrapper)


def _guard_against_claude_popen(monkeypatch, module):
    """Proves the `claude` CLI is never spawned, while real git/bash
    subprocess calls made by the same invocation keep working."""
    _patch_claude_popen(monkeypatch, module, on_claude_spawn=None)


class TestQuoinClaudeInstallGuard:
    def test_script_mode_failing_install_yields_error_and_never_spawns(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude

        root, sha = arm_git_repo
        (root / "quoin" / "install.sh").write_text("#!/bin/bash\nexit 1\n")

        _guard_against_claude_popen(monkeypatch, quoin_claude)
        from quoin.benchmarks.harness.config import BudgetSpec

        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "install-failed"

    def test_missing_script_path_yields_error_and_never_spawns(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo

        _guard_against_claude_popen(monkeypatch, quoin_claude)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "does-not-exist.sh",
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "install-failed"
        assert result["extra"]["install_reason"] == "script-missing"

    def test_commit_mismatch_yields_error_and_never_spawns(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo

        _guard_against_claude_popen(monkeypatch, quoin_claude)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
            expected_quoin_commit="not-the-real-sha",
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "commit-mismatch"

    def test_skip_mode_installs_nothing_and_creates_fresh_artifacts_dir(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude

        root, sha = arm_git_repo
        workdir = tmp_path / "work"
        workdir.mkdir()

        def raise_run(*a, **kw):
            raise AssertionError("subprocess.run must not be called in skip mode")

        monkeypatch.setattr(quoin_claude.subprocess, "run", raise_run)
        install_result = quoin_claude._initialize_workflow_artifacts(
            workdir, root / "quoin" / "install.sh",
            quoin_install_mode="skip", arm_root=root,
        )
        assert (workdir / ".workflow_artifacts").exists()
        assert install_result == {
            "ok": True, "reason": "skip-driver-installed",
            "returncode": None, "stdout_tail": "", "stderr_tail": "",
        }

    def test_skip_mode_end_to_end_reaches_popen_without_installing(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo
        popen_called = {}

        def on_spawn(cmd):
            popen_called["cmd"] = cmd
            return _FakeClaudeProc()

        _patch_claude_popen(monkeypatch, quoin_claude, on_claude_spawn=on_spawn)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work-skip",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
            quoin_install_mode="skip",
            arm_root=root,
            expected_quoin_commit=sha,
        )
        assert popen_called
        assert result["extra"]["install_reason"] == "skip-driver-installed"
        assert result["extra"]["installed_quoin_commit"] == sha

    def test_module_mode_argv_has_no_bash_and_sets_pythonpath(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude

        root, sha = arm_git_repo
        captured = {}
        real_run = quoin_claude.subprocess.run

        def spy_run(argv, **kwargs):
            if argv[0] == quoin_claude.sys.executable:
                captured["argv"] = argv
                captured["env"] = kwargs.get("env")
                import types
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(quoin_claude.subprocess, "run", spy_run)
        install_result = quoin_claude._initialize_workflow_artifacts(
            tmp_path / "work2", root / "quoin" / "install.sh",
            quoin_install_mode="module", arm_root=root,
        )
        assert install_result["ok"] is True
        assert "bash" not in captured["argv"]
        assert captured["argv"][0] == quoin_claude.sys.executable
        assert "install" in captured["argv"]
        assert captured["env"]["PYTHONPATH"] == str(root / "src")

    def test_happy_path_records_installed_commit_and_reaches_popen(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo
        popen_called = {}

        def on_spawn(cmd):
            popen_called["cmd"] = cmd
            return _FakeClaudeProc()

        _patch_claude_popen(monkeypatch, quoin_claude, on_claude_spawn=on_spawn)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work3",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
            expected_quoin_commit=sha,
        )
        assert popen_called, "Popen should have been reached on the happy path"
        assert result["extra"]["installed_quoin_commit"] == sha
        assert result["extra"]["workflow_artifacts_captured"] in (True, False)
        assert "workflow_artifacts_has_arch" in result["extra"]
        assert "workflow_artifacts_has_plan" in result["extra"]

    def test_install_reason_round_trips_into_metrics_json(self, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.result_writer import RunResult, write_run_result
        import json as _json

        result = RunResult(cell="quoin-claude", task_id="t1", run_id="r1", verdict="error",
                            extra={"install_reason": "install-failed", "failure_reason": "install-failed"})
        out_dir = write_run_result(result, tmp_path / "runs")
        metrics = _json.loads((out_dir / "metrics.json").read_text())
        assert metrics["install_reason"] == "install-failed"


class TestQuoinClaudeGateModeGuards:
    def test_gate_mode_none_expected_commit_refuses_before_popen(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo
        monkeypatch.setenv("QUOIN_BENCHMARK_GATE", "1")

        _guard_against_claude_popen(monkeypatch, quoin_claude)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "expected-commit-unarmed"

    def test_gate_mode_none_budget_refuses_before_popen(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo
        monkeypatch.setenv("QUOIN_BENCHMARK_GATE", "1")

        _guard_against_claude_popen(monkeypatch, quoin_claude)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
            expected_quoin_commit=sha,
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "budget-cap-unarmed"

    def test_gate_mode_dropped_flag_refuses_before_popen_anti_silent_omission(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo
        monkeypatch.setenv("QUOIN_BENCHMARK_GATE", "1")
        # Stub the argv builder to silently drop --max-budget-usd even
        # though a cap was requested — the exact anti-silent-omission proof.
        monkeypatch.setattr(quoin_claude, "_build_claude_argv", lambda prompt, model, cap: ["claude", prompt])

        _guard_against_claude_popen(monkeypatch, quoin_claude)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
            expected_quoin_commit=sha,
            max_budget_usd=5.0,
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "budget-cap-unarmed"

    def test_outside_gate_mode_none_expected_commit_is_advisory_only(self, monkeypatch, arm_git_repo, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo
        monkeypatch.delenv("QUOIN_BENCHMARK_GATE", raising=False)

        _patch_claude_popen(monkeypatch, quoin_claude, on_claude_spawn=lambda cmd: _FakeClaudeProc())
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path / "work",
            budget=BudgetSpec(),
            run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh",
        )
        assert result["extra"]["expected_quoin_commit_armed"] is False
        assert result["verdict"] != "error"


class TestSimpleClaudeBudgetGuard:
    def test_gate_mode_none_budget_refuses_before_popen(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        monkeypatch.setenv("QUOIN_BENCHMARK_GATE", "1")

        _guard_against_claude_popen(monkeypatch, simple_claude)
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path,
            budget=BudgetSpec(),
            run_id="r1",
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "budget-cap-unarmed"

    def test_gate_mode_dropped_flag_refuses_before_popen(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        monkeypatch.setenv("QUOIN_BENCHMARK_GATE", "1")
        monkeypatch.setattr(simple_claude, "_build_claude_argv", lambda prompt, model, cap: ["claude", prompt])

        _guard_against_claude_popen(monkeypatch, simple_claude)
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path,
            budget=BudgetSpec(),
            run_id="r1",
            max_budget_usd=5.0,
        )
        assert result["verdict"] == "error"
        assert result["extra"]["failure_reason"] == "budget-cap-unarmed"

    def test_outside_gate_mode_records_advisory_and_spawns(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        monkeypatch.delenv("QUOIN_BENCHMARK_GATE", raising=False)

        _patch_claude_popen(monkeypatch, simple_claude, on_claude_spawn=lambda cmd: _FakeClaudeProc())
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path,
            budget=BudgetSpec(),
            run_id="r1",
        )
        assert result["extra"]["budget_cap_armed"] is False
        assert result["verdict"] != "error"


# ---------------------------------------------------------------------------
# T-03: CLI wiring, the between-task SpendTracker, and manifest real values
# ---------------------------------------------------------------------------


class TestSpendTracker:
    def test_stops_after_cap_exceeded_and_sets_budget_stopped(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.harness.judge import JudgeResult

        monkeypatch.setattr(
            runner, "judge_task",
            lambda *a, **kw: JudgeResult(task_id="t", source_benchmark="unknown", verdict="pass",
                                          evidence_path=None, judge_runtime_seconds=0.0),
        )

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id):
                return {"verdict": "pass", "extra": {}, "cost_available": True,
                        "cost_runtime_usd": 4.0}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        suite = [{"id": f"t{i}"} for i in range(5)]
        tracker = runner.SpendTracker(cap_usd=10.0)
        result = runner.run_cell(cell="stub-cell", suite=suite, run_id="r1", config=config, tracker=tracker)
        # $4, $8 (still <= 10), $12 (> 10) — stops after the THIRD task.
        assert len(result.task_results) == 3
        assert result.budget_stopped is True
        assert tracker.total_usd == 12.0

    def test_no_cap_runs_to_completion_byte_identical_task_count(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.harness.judge import JudgeResult

        monkeypatch.setattr(
            runner, "judge_task",
            lambda *a, **kw: JudgeResult(task_id="t", source_benchmark="unknown", verdict="pass",
                                          evidence_path=None, judge_runtime_seconds=0.0),
        )

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id):
                return {"verdict": "pass", "extra": {}, "cost_available": True,
                        "cost_runtime_usd": 100.0}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        suite = [{"id": f"t{i}"} for i in range(5)]
        result = runner.run_cell(cell="stub-cell", suite=suite, run_id="r1", config=config, tracker=None)
        assert len(result.task_results) == 5
        assert result.budget_stopped is False

    def test_tracker_none_cap_never_stops(self):
        from quoin.benchmarks.harness.runner import SpendTracker

        tracker = SpendTracker(cap_usd=None)
        for _ in range(10):
            assert tracker.add(1000.0) is False


class TestManifestRealValues:
    def test_none_cap_renders_as_yaml_null_not_python_none(self, tmp_path):
        from quoin.benchmarks.scripts.run_benchmark import _write_manifest

        manifest_path = _write_manifest(
            run_dir=tmp_path, run_id="r1", suite_path=tmp_path / "suite.json",
            cells=["simple-claude"], max_parallel=1, resume=False, repo_root=tmp_path,
            usd_cap=None,
        )
        text = manifest_path.read_text()
        assert "usd_kill_switch_per_cell_pair: null" in text
        assert "usd_kill_switch_per_cell_pair: None" not in text
        import yaml
        parsed = yaml.safe_load(text)
        assert parsed["usd_kill_switch_per_cell_pair"] is None

    def test_configured_values_round_trip_into_manifest(self, tmp_path):
        from quoin.benchmarks.scripts.run_benchmark import _write_manifest
        import subprocess

        fixture = tmp_path / "fixture"
        fixture.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=fixture, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=fixture, check=True)
        (fixture / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=fixture, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=fixture, check=True)
        fixture_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=fixture, capture_output=True, text=True, check=True,
        ).stdout.strip()

        manifest_path = _write_manifest(
            run_dir=tmp_path, run_id="r1", suite_path=tmp_path / "suite.json",
            cells=["simple-claude"], max_parallel=1, resume=False, repo_root=tmp_path,
            wall_clock_seconds=5400, usd_cap=38.0, fixture_repo=fixture,
        )
        import yaml
        parsed = yaml.safe_load(manifest_path.read_text())
        assert parsed["wall_clock_budget_seconds"] == 5400
        assert parsed["usd_kill_switch_per_cell_pair"] == 38.0
        assert parsed["fixture_repo_sha"] == fixture_sha

    def test_non_git_fixture_repo_writes_not_a_git_repo_marker(self, tmp_path):
        from quoin.benchmarks.scripts.run_benchmark import _write_manifest

        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        manifest_path = _write_manifest(
            run_dir=tmp_path, run_id="r1", suite_path=tmp_path / "suite.json",
            cells=["simple-claude"], max_parallel=1, resume=False, repo_root=tmp_path,
            fixture_repo=not_a_repo,
        )
        import yaml
        parsed = yaml.safe_load(manifest_path.read_text())
        assert parsed["fixture_repo_sha"] == "not_a_git_repo"


class TestCLIWiringToCell:
    def test_wrong_expected_commit_wired_through_run_benchmark_reaches_cell_as_error(self, monkeypatch, tmp_path):
        """The WIRED-UP path (round-3 acceptance): a deliberately wrong
        --expected-quoin-commit, passed through run_benchmark's own CLI
        surface (not the cell's invoke() directly), produces verdict=="error"
        with subprocess.Popen never reached."""
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.scripts import run_benchmark as rb_mod

        suite_path = tmp_path / "suite.json"
        suite_path.write_text(json.dumps({"tasks": [{"id": "t1", "description": "x"}]}))

        received_kwargs = {}

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id, expected_quoin_commit=None):
                received_kwargs["expected_quoin_commit"] = expected_quoin_commit
                if expected_quoin_commit == "wrong-sha":
                    return {"verdict": "error", "extra": {"failure_reason": "commit-mismatch"}}
                raise AssertionError("Popen must not be reached in this stub")

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)

        rb_mod.run_benchmark(
            suite_path=suite_path,
            cells=["stub-cell"],
            run_id="r1",
            run_dir=tmp_path / "runs",
            max_parallel=1,
            expected_quoin_commit="wrong-sha",
        )
        assert received_kwargs["expected_quoin_commit"] == "wrong-sha"
        judge_path = tmp_path / "runs" / "r1" / "stub-cell" / "t1" / "judge.json"
        judge_data = json.loads(judge_path.read_text())
        assert judge_data["verdict"] == "error"


# ---------------------------------------------------------------------------
# T-14: cost field name (D-14) and the CLI-enforced budget-halt outcome
# ---------------------------------------------------------------------------


class TestCostFieldName:
    def test_total_cost_usd_is_read(self):
        from quoin.benchmarks.harness.cells.simple_claude import _extract_cost_usd

        assert _extract_cost_usd({"type": "result", "total_cost_usd": 1.23}) == 1.23

    def test_legacy_cost_usd_is_a_fallback(self):
        from quoin.benchmarks.harness.cells.simple_claude import _extract_cost_usd

        assert _extract_cost_usd({"type": "result", "cost_usd": 1.23}) == 1.23

    def test_total_cost_usd_wins_over_legacy_when_both_present(self):
        from quoin.benchmarks.harness.cells.simple_claude import _extract_cost_usd

        assert _extract_cost_usd({"total_cost_usd": 2.0, "cost_usd": 1.0}) == 2.0

    def test_neither_field_yields_none(self):
        from quoin.benchmarks.harness.cells.simple_claude import _extract_cost_usd

        assert _extract_cost_usd({"type": "result"}) is None

    def test_max_budget_usd_reaches_argv_when_set_and_argv_unchanged_when_unset(self):
        from quoin.benchmarks.harness.cells.simple_claude import _build_claude_argv

        with_cap = _build_claude_argv("p", "m", 5.0)
        assert "--max-budget-usd" in with_cap
        assert "5.0" in with_cap
        without_cap = _build_claude_argv("p", "m", None)
        assert "--max-budget-usd" not in without_cap


class TestBudgetHaltDetection:
    def test_reached_maximum_budget_string_detected(self):
        from quoin.benchmarks.harness.cells.simple_claude import _detect_budget_halt

        assert _detect_budget_halt("... Reached maximum budget ($5.00) ...") is True

    def test_session_cost_not_a_number_string_detected(self):
        from quoin.benchmarks.harness.cells.simple_claude import _detect_budget_halt

        assert _detect_budget_halt("Session cost is not a number; refusing to continue") is True

    def test_unrelated_text_not_detected(self):
        from quoin.benchmarks.harness.cells.simple_claude import _detect_budget_halt

        assert _detect_budget_halt("all good, nothing to see here") is False

    def test_simulated_halt_yields_budget_stopped_verdict_and_short_circuits_judge(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        class HaltingStderr:
            def read(self):
                return "Reached maximum budget ($5.00); halting session"

        class HaltingProc:
            stdout = _FakeStdout()
            stderr = HaltingStderr()
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0

        _patch_claude_popen(monkeypatch, simple_claude, on_claude_spawn=lambda cmd: HaltingProc())
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"},
            workdir=tmp_path,
            budget=BudgetSpec(),
            run_id="r1",
        )
        assert result["verdict"] == "budget_stopped"
        assert result["extra"]["failure_reason"] == "budget-halt-detected"

        # And the runner-level short-circuit (T-01) treats it as terminal.
        from quoin.benchmarks.harness import runner

        def raising_judge(*a, **kw):
            raise AssertionError("judge_task must not be called on budget_stopped")

        monkeypatch.setattr(runner, "judge_task", raising_judge)
        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: simple_claude)
        from quoin.benchmarks.harness.config import HarnessConfig

        config = HarnessConfig(run_dir=tmp_path / "runs")
        run_result = runner.run_one_task(
            cell="simple-claude", task_spec={"id": "t1", "description": "x"},
            run_id="r1", config=config,
        )
        assert run_result.verdict == "budget_stopped"


# ---------------------------------------------------------------------------
# T-15: each arm/task gets its own workflow-artifact evidence directory
# ---------------------------------------------------------------------------


class TestWorkflowArtifactsEvidenceDir:
    def test_run_output_dir_threaded_from_runner_to_quoin_claude(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness import runner
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.harness.judge import JudgeResult

        monkeypatch.setattr(
            runner, "judge_task",
            lambda *a, **kw: JudgeResult(task_id="t", source_benchmark="unknown", verdict="pass",
                                          evidence_path=None, judge_runtime_seconds=0.0),
        )
        received = {}

        class StubAdapter:
            @staticmethod
            def invoke(task_spec, workdir, budget, run_id, run_output_dir=None):
                received["run_output_dir"] = run_output_dir
                return {"verdict": "pass", "extra": {}}

        monkeypatch.setattr(runner, "_load_cell_adapter", lambda cell: StubAdapter)
        config = HarnessConfig(run_dir=tmp_path)
        runner.run_one_task(cell="stub-cell", task_spec={"id": "t1"}, run_id="r1", config=config)
        from quoin.benchmarks.harness.result_writer import task_result_dir
        assert received["run_output_dir"] == task_result_dir(tmp_path, "r1", "stub-cell", "t1")

    def test_two_invocations_with_different_run_id_write_to_different_dirs(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        _patch_claude_popen(monkeypatch, quoin_claude, on_claude_spawn=lambda cmd: _FakeClaudeProc())
        (tmp_path / "work" / ".workflow_artifacts" / "task").mkdir(parents=True)
        (tmp_path / "work" / ".workflow_artifacts" / "task" / "architecture.md").write_text("x")

        out1 = tmp_path / "runs" / "run-a" / "quoin-claude" / "t1"
        out2 = tmp_path / "runs" / "run-b" / "quoin-claude" / "t1"
        quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path / "work",
            budget=BudgetSpec(), run_id="run-a", run_output_dir=out1,
        )
        # Recreate the workflow_artifacts fixture (invoke wipes it at start).
        (tmp_path / "work" / ".workflow_artifacts" / "task").mkdir(parents=True)
        (tmp_path / "work" / ".workflow_artifacts" / "task" / "architecture.md").write_text("x")
        quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path / "work",
            budget=BudgetSpec(), run_id="run-b", run_output_dir=out2,
        )
        assert (out1 / "workflow_artifacts_evidence").exists()
        assert (out2 / "workflow_artifacts_evidence").exists()
        assert out1 != out2

    def test_preseeded_old_shared_path_does_not_leak_into_a_fresh_run(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        _patch_claude_popen(monkeypatch, quoin_claude, on_claude_spawn=lambda cmd: _FakeClaudeProc())
        # Old shared path: workdir.parent / "artifacts_evidence" — pre-seed it
        # with an architecture.md the way a prior arm's leftovers would.
        old_shared = (tmp_path / "work").parent / "artifacts_evidence"
        old_shared.mkdir(parents=True, exist_ok=True)
        (old_shared / "architecture.md").write_text("leftover from another arm")

        (tmp_path / "work").mkdir(exist_ok=True)
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path / "work",
            budget=BudgetSpec(), run_id="run-c",
            run_output_dir=tmp_path / "runs" / "run-c" / "quoin-claude" / "t1",
        )
        # No architecture.md was created inside THIS task's own worktree, so
        # a run reading the arm-unique dest must not report the other arm's
        # leftover as its own evidence.
        assert result["workflow_artifacts_has_arch"] is False

    def test_fallback_path_is_run_and_cell_unique_when_run_output_dir_omitted(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        _patch_claude_popen(monkeypatch, quoin_claude, on_claude_spawn=lambda cmd: _FakeClaudeProc())
        (tmp_path / "work").mkdir()
        result = quoin_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path / "work",
            budget=BudgetSpec(), run_id="run-fallback",
        )
        fallback_dir = (
            Path(quoin_claude.tempfile.gettempdir()) / "quoin-benchmarks"
            / "artifacts_evidence-run-fallback-quoin-claude"
        )
        assert result["workflow_artifacts_captured"] is True
        assert (fallback_dir / "workflow_artifacts_evidence").exists()
