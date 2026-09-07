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
import re
import sys
import time
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


def _derive_base_metrics_keys() -> set:
    """The exact literal keys `write_run_result` assigns into
    `metrics_data` before `result.extra` is merged in — parsed from
    source, not restated by hand, so this cannot go stale independently
    of the code the way the previous hand-restated `added_keys` literal
    already had — it silently omitted `session_errored`,
    `had_assistant_event`, `commit_error` and `quoin_install_script`."""
    import inspect
    from quoin.benchmarks.harness import result_writer

    src = inspect.getsource(result_writer.write_run_result)
    before_merge = src.split("metrics_data.update(result.extra)")[0]
    return set(re.findall(r'metrics_data\["([a-zA-Z0-9_]+)"\]\s*=', before_merge))


def _derive_added_extra_keys() -> set:
    """Every literal key that ends up in the `extra` dict `write_run_result`
    merges into `metrics_data` — both cell adapters' own contributions
    (`result["extra"]["key"] = ...` and the bulk-update form
    `result["extra"].update({"key": ..., ...})`) plus `runner.py`'s own
    addition (`invocation_extra["key"] = ...`, which is what actually
    reaches `RunResult.extra` — see `runner.py`'s `invocation_extra`
    variable) — parsed from source rather than restated by hand."""
    import inspect
    from quoin.benchmarks.harness import runner
    from quoin.benchmarks.harness.cells import quoin_claude, simple_claude

    keys: set = set()
    for module in (simple_claude, quoin_claude):
        src = inspect.getsource(module)
        keys |= set(re.findall(r'result\["extra"\]\["([a-zA-Z0-9_]+)"\]\s*=', src))
        for block in re.findall(r'result\["extra"\]\.update\((\{.*?\})\)', src, re.DOTALL):
            keys |= set(re.findall(r'"([a-zA-Z0-9_]+)"\s*:', block))

    runner_src = inspect.getsource(runner)
    keys |= set(re.findall(r'invocation_extra\["([a-zA-Z0-9_]+)"\]\s*=', runner_src))
    return keys


class TestExtraMetricsMerge:
    def test_base_metrics_key_set_derived_from_source_is_unchanged(self):
        """Guards against the base key set drifting silently (round-3 note)."""
        from quoin.benchmarks.harness import result_writer
        import inspect

        src = inspect.getsource(result_writer.write_run_result)
        base_keys = _derive_base_metrics_keys()
        assert base_keys, "expected at least one base metrics key to be derivable from source"
        for key in base_keys:
            assert f'"{key}"' in src, f"expected base metrics key {key!r} in write_run_result source"
        assert "metrics_data.update(result.extra)" in src

    def test_added_keys_do_not_collide_with_base_metrics_keys(self, tmp_path):
        from quoin.benchmarks.harness.result_writer import RunResult, write_run_result
        import json as _json

        base_keys = _derive_base_metrics_keys()
        added_keys = _derive_added_extra_keys()
        collisions = added_keys & base_keys
        assert not collisions, f"an added extra key collides with a base metrics key: {collisions}"

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
            """Matches the real pipe's readline() protocol the background
            drain thread uses: one line, then EOF (empty string)."""
            def __init__(self):
                self._lines = iter(["Reached maximum budget ($5.00); halting session"])

            def readline(self):
                return next(self._lines, "")

        class HaltingProc:
            def __init__(self):
                # Fresh per instance, not a shared class attribute: this
                # fixture is reused for a second invoke() call below, and
                # HaltingStderr's readline() iterator would otherwise
                # already be exhausted by the first call.
                self.stdout = _FakeStdout()
                self.stderr = HaltingStderr()
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


# ---------------------------------------------------------------------------
# T-04: PINNED_MODEL is the permanent (dateless) pin for 4.6+-generation
# models — no dated snapshot exists to resolve (coordinator-verified against
# Anthropic's live docs 2026-09-06). PINNED_MODEL/pricing.json consistency
# and the --verify-model preflight, WITHOUT ever making a real live call.
# ---------------------------------------------------------------------------


class TestPinnedModelPricingConsistency:
    def test_pinned_model_is_an_exact_pricing_key(self):
        import json as _json
        from quoin.benchmarks.harness.cells.simple_claude import PINNED_MODEL

        pricing_path = (
            Path(__file__).resolve().parent.parent.parent
            / "benchmarks" / "harness" / "pricing.json"
        )
        pricing = _json.loads(pricing_path.read_text(encoding="utf-8"))
        assert PINNED_MODEL in pricing["models"], (
            f"PINNED_MODEL={PINNED_MODEL!r} is not an exact key of "
            f"pricing.json['models']={list(pricing['models'])!r} — "
            "cost.estimate_cost's prefix fallback would mask this, but "
            "run_benchmark._estimate_cost_dry_run does an EXACT lookup and "
            "silently falls back to hardcoded rates."
        )

    def test_pricing_json_has_no_stale_dated_snapshot_keys(self):
        import json as _json

        pricing_path = (
            Path(__file__).resolve().parent.parent.parent
            / "benchmarks" / "harness" / "pricing.json"
        )
        pricing = _json.loads(pricing_path.read_text(encoding="utf-8"))
        for key in pricing["models"]:
            last_segment = key.rsplit("-", 1)[-1]
            assert not (len(last_segment) == 8 and last_segment.isdigit()), (
                f"pricing.json key {key!r} looks like a stale dated-snapshot "
                "placeholder; 4.6+-generation models have no dated form"
            )


class TestVerifyModelPreflight:
    def test_resolve_model_id_reads_model_usage_key_when_no_top_level_model(self):
        from quoin.benchmarks.scripts.run_benchmark import _resolve_model_id_from_probe_response

        data = {
            "modelUsage": {
                "claude-opus-4-7": {"canonicalModel": "claude-opus-4-7", "costUSD": 0.35}
            }
        }
        assert _resolve_model_id_from_probe_response(data) == "claude-opus-4-7"

    def test_resolve_model_id_prefers_top_level_model_field(self):
        from quoin.benchmarks.scripts.run_benchmark import _resolve_model_id_from_probe_response

        data = {"model": "claude-opus-4-7", "modelUsage": {"other": {}}}
        assert _resolve_model_id_from_probe_response(data) == "claude-opus-4-7"

    def test_verify_model_match_exits_zero_with_no_real_call(self, tmp_path):
        from quoin.benchmarks.scripts.run_benchmark import verify_model

        def stub_probe(max_budget_usd):
            return {
                "modelUsage": {"claude-opus-4-7": {"canonicalModel": "claude-opus-4-7"}},
                "total_cost_usd": 0.35,
            }

        code = verify_model(ledger_path=tmp_path / "ledger.jsonl", run_probe=stub_probe)
        assert code == 0

    def test_verify_model_mismatch_exits_one(self, tmp_path):
        from quoin.benchmarks.scripts.run_benchmark import verify_model

        def stub_probe(max_budget_usd):
            return {
                "modelUsage": {"claude-sonnet-4-6": {"canonicalModel": "claude-sonnet-4-6"}},
                "total_cost_usd": 0.10,
            }

        code = verify_model(ledger_path=tmp_path / "ledger.jsonl", run_probe=stub_probe)
        assert code == 1

    def test_verify_model_records_probe_in_ledger(self, tmp_path):
        from quoin.benchmarks.scripts.run_benchmark import verify_model
        from quoin.benchmarks.scripts.spend_ledger import recorded_total

        def stub_probe(max_budget_usd):
            return {
                "modelUsage": {"claude-opus-4-7": {"canonicalModel": "claude-opus-4-7"}},
                "total_cost_usd": 0.35,
            }

        ledger = tmp_path / "ledger.jsonl"
        verify_model(ledger_path=ledger, run_probe=stub_probe)
        assert recorded_total(ledger) == pytest.approx(0.35)

    def test_verify_model_refuses_when_ledger_precheck_fails_and_makes_no_call(self, tmp_path):
        from quoin.benchmarks.scripts.run_benchmark import verify_model
        from quoin.benchmarks.scripts.spend_ledger import append

        ledger = tmp_path / "ledger.jsonl"
        append(ledger, {
            "ts": "2026-09-06T00:00:00Z", "attempt_id": "a1", "kind": "reservation",
            "invocation": "full", "gate_id": "g1", "arm": "candidate", "cap_usd": 50.0,
            "actual_usd": None, "run_id": "g1-candidate", "new_ceiling_usd": None, "note": "",
        })

        def raising_probe(max_budget_usd):
            raise AssertionError("must not spawn when the ledger precheck fails")

        code = verify_model(ledger_path=ledger, run_probe=raising_probe)
        assert code == 2


# ---------------------------------------------------------------------------
# T-05: HarnessConfig.cells matches REQUIRED_MODE_IDS; gate results land
# outside the design tree
# ---------------------------------------------------------------------------


class TestHarnessConfigInvariants:
    def test_default_cells_match_required_mode_ids_exactly(self):
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.scripts.validate_benchmarks import REQUIRED_MODE_IDS

        cells = HarnessConfig().cells
        assert set(cells) == REQUIRED_MODE_IDS
        assert len(cells) == len(set(cells)), "default cells list contains a duplicate"

    def test_a_fifth_cell_would_fail_this_assertion(self):
        """Verifies the assertion above is load-bearing, not vacuous —
        editing in a fifth cell locally (never committed) must break it."""
        from quoin.benchmarks.harness.config import HarnessConfig
        from quoin.benchmarks.scripts.validate_benchmarks import REQUIRED_MODE_IDS

        cells = list(HarnessConfig().cells) + ["a-fifth-cell"]
        assert set(cells) != REQUIRED_MODE_IDS

    def test_run_dir_is_under_workflow_artifacts_and_not_under_benchmarks(self):
        from quoin.benchmarks.harness.config import HarnessConfig

        run_dir = str(HarnessConfig().run_dir)
        assert run_dir.startswith(".workflow_artifacts")
        assert "benchmarks" not in Path(run_dir).parts, (
            "gate results must be recorded with the task, never published "
            "as benchmark results (D-09)"
        )


# ---------------------------------------------------------------------------
# T-06: the gate suite, the quoin_scenario prompt branch, and its judge
# ---------------------------------------------------------------------------


_GATE_SUITE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "benchmarks" / "suite-gate-medium-refactor.json"
)


class TestGateSuiteFile:
    def test_suite_parses_and_has_one_task(self):
        suite = json.loads(_GATE_SUITE_PATH.read_text(encoding="utf-8"))
        assert suite["schema_version"] == 2
        assert len(suite["tasks"]) == 1
        task = suite["tasks"][0]
        assert task["id"] == "scenario_medium_refactor_plan"
        assert task["source"] == "quoin_scenario"
        assert task["target_subsystem"] == "src/black/trans.py"
        assert task["target_subsystem"] in task["description"]

    def test_suite_contains_no_forbidden_result_claims(self):
        from quoin.benchmarks.scripts.validate_benchmarks import FORBIDDEN_RESULT_CLAIMS

        text = _GATE_SUITE_PATH.read_text(encoding="utf-8").lower()
        for phrase in FORBIDDEN_RESULT_CLAIMS:
            assert phrase not in text

    def test_dry_run_prints_a_one_task_matrix(self, capsys):
        import sys as _sys
        from quoin.benchmarks.scripts import run_benchmark as rb_mod

        old_argv = _sys.argv
        try:
            _sys.argv = [
                "run_benchmark.py", "--suite", str(_GATE_SUITE_PATH),
                "--cells", "simple-claude", "--run-id", "smoke", "--dry-run",
                # A real gate invocation always sets a per-task cap (T-09);
                # this value clears the $38 worst-case threshold (1 task x
                # 1 cell x $6). The "no cap set" / "caps too high" failure
                # scenarios are covered explicitly in TestDryRunGate below.
                "--max-budget-usd-per-task", "6",
            ]
            with pytest.raises(SystemExit) as exc_info:
                rb_mod.main()
            assert exc_info.value.code == 0
        finally:
            _sys.argv = old_argv
        out = capsys.readouterr().out
        assert "Total task invocations: 1" in out


class TestScenarioPromptBranch:
    def test_description_present_returns_it_verbatim(self):
        from quoin.benchmarks.harness.cells.simple_claude import _build_prompt

        task = {
            "id": "scenario_x", "source": "quoin_scenario",
            "description": "Plan a medium refactor for src/black/trans.py. Do not implement it.",
            "target_subsystem": "src/black/trans.py",
        }
        assert _build_prompt(task) == task["description"]

    def test_frozen_suite_prompt_contains_target_subsystem(self):
        from quoin.benchmarks.harness.cells.simple_claude import _build_prompt

        suite = json.loads(_GATE_SUITE_PATH.read_text(encoding="utf-8"))
        task = suite["tasks"][0]
        prompt = _build_prompt(task)
        assert task["target_subsystem"] in prompt

    def test_quoin_cell_prompt_is_identical_apart_from_run_prepend(self, monkeypatch, arm_git_repo, tmp_path):
        """Verified against the REAL quoin_claude.invoke() prompt
        construction (not a re-derived literal) so a prepend edit there
        can't silently drift from this test (D-18: the prepend is
        `/run --autonomous`, not plain `/run`, per F-11)."""
        from quoin.benchmarks.harness.cells import quoin_claude
        from quoin.benchmarks.harness.cells.simple_claude import _build_prompt
        from quoin.benchmarks.harness.config import BudgetSpec

        root, sha = arm_git_repo
        suite = json.loads(_GATE_SUITE_PATH.read_text(encoding="utf-8"))
        task = suite["tasks"][0]
        base_prompt = _build_prompt(task)

        captured = {}

        def on_spawn(cmd):
            captured["prompt"] = cmd[-1]
            return _FakeClaudeProc()

        _patch_claude_popen(monkeypatch, quoin_claude, on_claude_spawn=on_spawn)
        # skip mode + an isolated arm_git_repo fixture: this test must never
        # touch the real quoin/install.sh or this machine's real ~/.claude.
        quoin_claude.invoke(
            task_spec=task, workdir=tmp_path / "work", budget=BudgetSpec(), run_id="r1",
            quoin_install_script=root / "quoin" / "install.sh", quoin_install_mode="skip",
            arm_root=root, expected_quoin_commit=sha,
        )
        assert captured["prompt"] == f"Use /run --autonomous end-to-end on this task\n\n{base_prompt}"
        assert captured["prompt"].endswith(base_prompt)

    def test_description_present_but_omitting_subsystem_raises_and_never_spawns(self):
        from quoin.benchmarks.harness.cells.simple_claude import _build_prompt

        task = {
            "id": "scenario_x", "source": "quoin_scenario",
            "description": "Plan a medium refactor. Do not implement it.",
            "target_subsystem": "src/black/trans.py",
        }
        with pytest.raises(ValueError):
            _build_prompt(task)

    def test_empty_description_falls_back_to_scenario_file_and_still_fails_containment(self):
        from quoin.benchmarks.harness.cells.simple_claude import _build_prompt

        task = {
            "id": "scenario_x", "source": "quoin_scenario", "description": "",
            "scenario_file": "scenarios/medium-refactor-plan.md",
            "target_subsystem": "src/black/trans.py",
        }
        with pytest.raises(ValueError):
            _build_prompt(task)

    def test_preexisting_branches_are_byte_unchanged(self):
        from quoin.benchmarks.harness.cells.simple_claude import _build_prompt

        humaneval_task = {"source": "evalplus_humaneval_plus", "source_id": "HumanEval/0",
                           "description": "desc"}
        expected_humaneval = (
            "Solve the following HumanEval+ programming task. "
            "Write your solution as a Python function in a file called solution.py.\n\n"
            "Task ID: HumanEval/0\n"
            "Task description: desc\n\n"
            "Your solution should pass all tests in the evalplus test suite for this task."
        )
        assert _build_prompt(humaneval_task) == expected_humaneval

        swebench_task = {"source": "swebench_lite", "source_id": "repo__issue-1", "description": "desc"}
        expected_swebench = (
            "Fix the following GitHub issue from the SWE-bench Lite benchmark.\n\n"
            "Instance ID: repo__issue-1\n"
            "Description: desc\n\n"
            "Implement the fix in the repository. When done, your changes will be "
            "evaluated by the SWE-bench harness."
        )
        assert _build_prompt(swebench_task) == expected_swebench

        unknown_task = {"source": "mystery", "source_id": "x1", "description": "desc"}
        assert _build_prompt(unknown_task) == "Solve task: desc (source_id=x1)"


class TestScenarioJudge:
    def test_both_flags_true_judges_pass(self):
        from quoin.benchmarks.harness.judge import judge_scenario

        result = judge_scenario("scenario_x", Path("."), "r1", invocation_extra={
            "workflow_artifacts_has_arch": True, "workflow_artifacts_has_plan": True,
        })
        assert result.verdict == "pass"

    def test_both_flags_false_judges_fail(self):
        from quoin.benchmarks.harness.judge import judge_scenario

        result = judge_scenario("scenario_x", Path("."), "r1", invocation_extra={
            "workflow_artifacts_has_arch": False, "workflow_artifacts_has_plan": False,
        })
        assert result.verdict == "fail"

    def test_one_flag_false_judges_fail(self):
        from quoin.benchmarks.harness.judge import judge_scenario

        result = judge_scenario("scenario_x", Path("."), "r1", invocation_extra={
            "workflow_artifacts_has_arch": True, "workflow_artifacts_has_plan": False,
        })
        assert result.verdict == "fail"

    def test_absent_invocation_extra_judges_error(self):
        from quoin.benchmarks.harness.judge import judge_scenario

        assert judge_scenario("scenario_x", Path("."), "r1", invocation_extra=None).verdict == "error"
        assert judge_scenario("scenario_x", Path("."), "r1", invocation_extra={}).verdict == "error"

    def test_simple_claude_path_pass_on_assistant_event(self):
        from quoin.benchmarks.harness.judge import judge_scenario

        result = judge_scenario("scenario_x", Path("."), "r1", invocation_extra={
            "had_assistant_event": True, "budget_cap_armed": True,
        })
        assert result.verdict == "pass"

    def test_simple_claude_path_fail_without_assistant_event(self):
        from quoin.benchmarks.harness.judge import judge_scenario

        result = judge_scenario("scenario_x", Path("."), "r1", invocation_extra={
            "had_assistant_event": False, "budget_cap_armed": True,
        })
        assert result.verdict == "fail"

    def test_scenario_task_never_reaches_unknown_source_arm(self):
        from quoin.benchmarks.harness.judge import judge_task

        result = judge_task("scenario_medium_refactor_plan", Path("."), "r1",
                             invocation_extra={"had_assistant_event": True})
        assert result.source_benchmark == "quoin_scenario"
        assert "Unknown source benchmark" not in (result.evidence_path or "")

    def test_infer_source_maps_scenario_prefix(self):
        from quoin.benchmarks.harness.judge import _infer_source as judge_infer_source
        from quoin.benchmarks.harness.result_writer import _infer_source as writer_infer_source

        assert judge_infer_source("scenario_medium_refactor_plan") == "quoin_scenario"
        assert writer_infer_source("scenario_medium_refactor_plan") == "quoin_scenario"

    def test_preexisting_judge_branches_byte_unchanged(self, tmp_path):
        from quoin.benchmarks.harness.judge import judge_task

        # HumanEval+ branch: missing solution file -> error (byte-unchanged
        # shape, no evalplus dependency required for this assertion).
        result = judge_task("humaneval_plus_000", tmp_path, "r1")
        assert result.source_benchmark == "evalplus_humaneval_plus"
        assert result.verdict == "error"

        # SWE-bench branch: missing patch file -> error.
        result = judge_task("swebench_lite_000", tmp_path, "r1")
        assert result.source_benchmark == "swebench_lite"
        assert result.verdict == "error"


# ---------------------------------------------------------------------------
# A full stderr pipe must not deadlock stdout reads; the
# wall-clock check must be timer-driven
# ---------------------------------------------------------------------------


class TestStderrDrainAndTimerDrivenWallClock:
    def test_large_stderr_output_does_not_deadlock_stdout_reads(self, tmp_path, monkeypatch):
        """Regression for a real deadlock shape: a child that writes well
        past one pipe-buffer's worth of stderr, before it
        ever writes a stdout line, would previously block in the child's
        own write(2) forever — because nothing was draining stderr — which
        then blocked this process's stdout `readline()` forever too. With
        the drain thread in place the child unblocks and completes well
        within the wall-clock budget."""
        import sys as _sys

        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        script = tmp_path / "noisy.py"
        script.write_text(
            "import sys, json\n"
            "sys.stderr.write('x' * 5_000_000)\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.01}))\n"
            "sys.stdout.flush()\n"
        )

        real_popen = simple_claude.subprocess.Popen

        def fake_popen(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
                kw.pop("cwd", None)
                return real_popen([_sys.executable, str(script)], cwd=str(tmp_path), **kw)
            return real_popen(cmd, *a, **kw)

        monkeypatch.setattr(simple_claude.subprocess, "Popen", fake_popen)
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path,
            budget=BudgetSpec(wall_clock_seconds=20), run_id="r1",
        )
        assert result["verdict"] != "timeout"
        assert result["cost_available"] is True
        assert result["cost_runtime_usd"] == 0.01

    def test_partial_stdout_line_does_not_block_past_the_wall_clock_budget(self, tmp_path, monkeypatch):
        """Regression: text-mode `readline()` can block on a partial line
        the child has written but not yet completed, even after `select()`
        already reported the fd readable — the wall-clock check above it
        in the loop then never gets re-evaluated until the child eventually
        finishes the line (or exits). A child that writes a PARTIAL
        stream-json event, flushes, then sleeps well past the budget must
        still make `invoke()` return close to the budget, not the full
        sleep."""
        import sys as _sys

        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        script = tmp_path / "partial.py"
        script.write_text(
            "import sys\n"
            "sys.stdout.write('{\"type\": \"assistant\", ')\n"  # no trailing newline
            "sys.stdout.flush()\n"
            "import time; time.sleep(30)\n"
        )

        real_popen = simple_claude.subprocess.Popen

        def fake_popen(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
                kw.pop("cwd", None)
                return real_popen([_sys.executable, str(script)], cwd=str(tmp_path), **kw)
            return real_popen(cmd, *a, **kw)

        monkeypatch.setattr(simple_claude.subprocess, "Popen", fake_popen)
        start = time.monotonic()
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path,
            budget=BudgetSpec(wall_clock_seconds=5), run_id="r1",
        )
        elapsed = time.monotonic() - start
        assert result["verdict"] == "timeout"
        # The load-bearing bound: well under the child's 30s sleep. A
        # readline()-based reader blocks on the partial line until the
        # child exits (or writes a newline), so this takes ~30s pre-fix.
        assert elapsed < 15

    def test_stderr_buffer_growth_is_bounded_not_proportional_to_child_output(self, tmp_path, monkeypatch):
        """Regression: pre-fix, stderr memory was accidentally bounded only
        because an unread pipe deadlocked the child; the drain-thread fix
        that closed the deadlock replaced it with a plain unbounded list,
        so a session emitting tens of MB of stderr grew this process's
        memory by a proportional amount. `deque(maxlen=...)` bounds it
        regardless of session length."""
        import resource as _resource
        import sys as _sys

        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        # Many separate lines, not one giant blob — this is the realistic
        # shape of CLI diagnostic/log output, and the one the deque's
        # line-count bound (rather than a byte-count bound) actually caps.
        n_lines = 200_000
        payload_mb = n_lines * 100 / 1_000_000
        script = tmp_path / "loud.py"
        script.write_text(
            "import sys, json\n"
            f"for _ in range({n_lines}):\n"
            "    sys.stderr.write('x' * 99 + '\\n')\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'type': 'result', 'total_cost_usd': 0.01}))\n"
            "sys.stdout.flush()\n"
        )

        real_popen = simple_claude.subprocess.Popen

        def fake_popen(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
                kw.pop("cwd", None)
                return real_popen([_sys.executable, str(script)], cwd=str(tmp_path), **kw)
            return real_popen(cmd, *a, **kw)

        monkeypatch.setattr(simple_claude.subprocess, "Popen", fake_popen)

        before = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path,
            budget=BudgetSpec(wall_clock_seconds=20), run_id="r1",
        )
        after = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS/BSD, kilobytes on Linux.
        unit = 1 if _sys.platform == "darwin" else 1024
        grown_mb = (after - before) * unit / 1_000_000

        assert result["cost_available"] is True
        # The deque retains only the last _MAX_RETAINED_STDERR_LINES lines
        # (~100 bytes each) regardless of how many the child wrote — growth
        # must stay a small fraction of the ~20 MB payload, not track it.
        assert grown_mb < payload_mb / 4

    def test_wait_readable_returns_true_immediately_for_a_non_selectable_stream(self):
        from quoin.benchmarks.harness.cells.simple_claude import _wait_readable

        class NoFileno:
            def readline(self):
                return ""

        assert _wait_readable(NoFileno(), 5.0) is True
        assert _wait_readable(None, 5.0) is True

    def test_wait_readable_times_out_when_nothing_arrives_on_a_real_pipe(self):
        import os as _os

        from quoin.benchmarks.harness.cells.simple_claude import _wait_readable

        read_fd, write_fd = _os.pipe()
        try:
            with _os.fdopen(read_fd, "r") as reader:
                start = time.monotonic()
                ready = _wait_readable(reader, 0.2)
                elapsed = time.monotonic() - start
                assert ready is False
                assert elapsed < 2.0  # bounded by the timeout, not blocked forever
        finally:
            _os.close(write_fd)

    def test_drain_stream_collects_lines_until_eof(self):
        import os as _os
        import threading as _threading

        from quoin.benchmarks.harness.cells.simple_claude import _drain_stream

        read_fd, write_fd = _os.pipe()
        writer = _os.fdopen(write_fd, "w")
        buffer: list = []
        with _os.fdopen(read_fd, "r") as reader:
            thread = _threading.Thread(target=_drain_stream, args=(reader, buffer), daemon=True)
            thread.start()
            writer.write("line one\nline two\n")
            writer.flush()
            writer.close()
            thread.join(timeout=5)
        assert "".join(buffer) == "line one\nline two\n"

    def test_drain_stream_is_a_noop_for_none(self):
        from quoin.benchmarks.harness.cells.simple_claude import _drain_stream

        _drain_stream(None, [])  # must not raise


# ---------------------------------------------------------------------------
# The paid subprocess is killed on any exception path through
# the streaming loop, not just the happy path
# ---------------------------------------------------------------------------


class TestPaidProcessKilledOnExceptionPath:
    def test_json_error_mid_loop_still_kills_the_still_running_child(self, tmp_path, monkeypatch):
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        script = tmp_path / "hangs.py"
        script.write_text(
            "import time\n"
            "print('not json, deliberately malformed downstream')\n"
            "import sys; sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )

        real_popen = simple_claude.subprocess.Popen
        spawned: list = []

        def fake_popen(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "claude":
                kw.pop("cwd", None)
                proc = real_popen([sys.executable, str(script)], cwd=str(tmp_path), **kw)
                spawned.append(proc)
                return proc
            return real_popen(cmd, *a, **kw)

        monkeypatch.setattr(simple_claude.subprocess, "Popen", fake_popen)

        # Force the loop to raise on its first parsed line so we exercise
        # the exception path while the 30s-sleeping child is still alive.
        real_loads = simple_claude.json.loads
        call_count = {"n": 0}

        def flaky_loads(s):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated parse-path failure")
            return real_loads(s)

        monkeypatch.setattr(simple_claude.json, "loads", flaky_loads)

        start = time.monotonic()
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path,
            budget=BudgetSpec(wall_clock_seconds=25), run_id="r1",
        )
        elapsed = time.monotonic() - start
        assert result["verdict"] == "error"
        # The load-bearing assertion: the child process was actually
        # reaped (killed), not merely that invoke() happened to return
        # quickly — a version that returns fast via the exception path
        # WITHOUT killing anything would satisfy an elapsed-time-only
        # check just as well, proving nothing about the child's fate.
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
        # Secondary signal, kept: proves the child was killed rather than
        # left to run out its 30s sleep.
        assert elapsed < 10


# ---------------------------------------------------------------------------
# The retained transcript is bounded
# ---------------------------------------------------------------------------


class TestTranscriptRingBufferBounded:
    def test_events_beyond_the_cap_are_dropped_not_accumulated_unbounded(self, monkeypatch, tmp_path):
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        n_events = simple_claude._MAX_RETAINED_EVENTS + 50
        lines = (
            [json.dumps({"type": "assistant", "message": {}}) for _ in range(n_events)]
            + [json.dumps({"type": "result", "total_cost_usd": 0.01})]
        )
        line_iter = iter(lines + [""])

        class ScriptedStdout:
            def readline(self):
                try:
                    return next(line_iter) + "\n"
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
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path,
            budget=BudgetSpec(), run_id="r1",
        )
        # Bounded, regardless of how many events the session emitted...
        assert len(result["transcript_events"]) <= simple_claude._MAX_RETAINED_EVENTS
        # ...but counters that must stay exact are tracked incrementally,
        # not derived from the (now-truncated) retained list.
        assert result["turn_count"] == n_events
        # The in-memory truncation is disclosed, not silent.
        assert result["extra"]["transcript_events_dropped"] == n_events + 1 - simple_claude._MAX_RETAINED_EVENTS
        assert result["transcript_events"][0]["type"] == "truncation_notice"

    def test_run_output_dir_streams_the_full_untruncated_transcript_to_disk(self, monkeypatch, tmp_path):
        """The FILE on disk must hold every event, even ones the bounded
        in-memory ring buffer already dropped — this is the actual fix for
        the silent-truncation defect; the ring buffer bound above is only
        a memory guarantee, not a completeness one."""
        from quoin.benchmarks.harness.cells import simple_claude
        from quoin.benchmarks.harness.config import BudgetSpec

        n_events = simple_claude._MAX_RETAINED_EVENTS + 50
        lines = (
            [json.dumps({"type": "assistant", "message": {}}) for _ in range(n_events)]
            + [json.dumps({"type": "result", "total_cost_usd": 0.01})]
        )
        line_iter = iter(lines + [""])

        class ScriptedStdout:
            def readline(self):
                try:
                    return next(line_iter) + "\n"
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
        out_dir = tmp_path / "result"
        result = simple_claude.invoke(
            task_spec={"id": "t1", "description": "x"}, workdir=tmp_path,
            budget=BudgetSpec(), run_id="r1", run_output_dir=out_dir,
        )
        assert result["extra"]["transcript_streamed"] is True
        written = (out_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        # n_events "assistant" lines plus one "result" line — ALL of them,
        # not capped at _MAX_RETAINED_EVENTS like the in-memory buffer.
        assert len(written) == n_events + 1
        assert json.loads(written[0])["type"] == "assistant"
        assert json.loads(written[-1])["type"] == "result"
