"""The prepared summary's telemetry keys and the phase loop's pre-launch seam."""
from __future__ import annotations

import os

import pytest

from quoin.opencode_adapter import compiler, cost, driver, phase_loop, runstore

import _opencode_driver_helpers as h
from _opencode_run_helpers import TASK, RecToken, ScriptedDriver, outcome
from test_opencode_driver_prepare import Setup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

KEYS = (
    "provider", "native_provider", "configured_effort", "effort_origin", "variant",
    "effort_diagnostic", "model_priced",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def test_prepared_record_carries_the_telemetry_keys(env):
    prepared = env.driver().prepare(env.request())
    record = runstore.load_record(runstore.store_dir(env.root), prepared.run_id)
    summary = record["prepared"]
    for key in KEYS:
        assert key in summary, key
    assert summary["native_provider"] == prepared.effective_model.split("/", 1)[0]
    assert isinstance(summary["provider"], str) and summary["provider"]
    assert summary["model_priced"] is False
    if summary["configured_effort"] is not None:
        assert summary["variant"] == compiler.VARIANT_PREFIX + summary["configured_effort"]
    else:
        assert summary["variant"] is None


def test_resume_prepare_refreshes_the_same_keys(env):
    drv = env.driver()
    prepared = drv.prepare(env.request())
    store = runstore.store_dir(env.root)
    record = runstore.load_record(store, prepared.run_id)
    record["state"] = "interrupted"
    record["prepared"] = {k: v for k, v in record["prepared"].items() if k not in KEYS}
    runstore.write_record(store, record)
    drv.prepare(env.request(), resume_run_id=prepared.run_id)
    refreshed = runstore.load_record(store, prepared.run_id)["prepared"]
    assert all(key in refreshed for key in KEYS)


def _doc(block):
    return {"provider": {"quoin-p": {"models": {"m": {"cost": block}}}}}


def test_model_priced_false_for_the_generated_document_true_for_a_priced_one(env):
    drv = env.driver()
    evaluation = drv._evaluate_launchable("work", drv._config_env(), __import__(
        "quoin.opencode_adapter.launch_env", fromlist=["Redactor"]).Redactor())
    fresh, _dir = drv._compile_pair(evaluation, drv._config_env())
    role = evaluation.resolutions.roles[0]
    assert cost.model_priced(fresh.document, compiler.native_model_ref(role)) is False
    assert cost.model_priced(_doc({"input": 1, "output": 2}), "quoin-p/m") is True
    for bad in ({"input": 1}, {"input": -1, "output": 2}, {"input": "1", "output": 2}):
        assert cost.model_priced(_doc(bad), "quoin-p/m") is False
    assert cost.model_priced(_doc({"input": 1, "output": 2}), "no-slash") is False


# -- the pre-launch seam -----------------------------------------------------


def _request(tmp_path):
    return driver.RunRequest(project_root=tmp_path, task=TASK, stage=None, phase="plan", profile="work")


def _go(tmp_path, script, on_prepared, *, token=None, new_run=False, drv=None, **kw):
    drv = drv or ScriptedDriver(tmp_path, script, **kw)
    token = token or RecToken(drv.clock)
    result = phase_loop.run_phase(
        drv, _request(tmp_path), max_relaunch=0, cancel=token, new_run=new_run,
        backoff_fn=lambda n: 0.0, monotonic=drv.clock, on_prepared=on_prepared,
    )
    return result, drv


def test_on_prepared_runs_once_immediately_before_start(tmp_path):
    seen = []
    drv = ScriptedDriver(tmp_path, [outcome()])
    _result, drv = _go(tmp_path, None, lambda prepared: seen.append((prepared.run_id, list(drv.names()))), drv=drv)
    assert len(seen) == 1
    run_id, names_then = seen[0]
    assert names_then == ["reconcile_task", "prepare"]
    assert drv.names() == ["reconcile_task", "prepare", "start", "observe"]
    assert run_id == _result.run_id


def test_on_prepared_is_not_called_on_a_resume(tmp_path):
    drv = ScriptedDriver(tmp_path, [outcome()])
    drv.seed("interrupted", attempts=[])
    seen = []
    result, drv = _go(tmp_path, None, seen.append, drv=drv)
    assert seen == [] and "resume" in drv.names()


def test_on_prepared_is_not_called_when_prepare_refuses(tmp_path):
    drv = ScriptedDriver(tmp_path, [])
    drv.prepare_errors[0] = driver.PrepareRefused("workflow-validation", "not-installed", "no")
    seen = []
    result, _ = _go(tmp_path, None, seen.append, drv=drv)
    assert result.outcome == "REFUSED" and seen == []


def test_on_prepared_is_not_called_when_cancelled_after_prepare(tmp_path):
    drv = ScriptedDriver(tmp_path, [])
    token = RecToken(drv.clock)
    drv.on_prepare = lambda d, n: token.cancel()
    seen = []
    result, _ = _go(tmp_path, None, seen.append, drv=drv, token=token)
    assert result.outcome == "CANCELLED" and seen == [] and "start" not in drv.names()


def test_on_prepared_is_not_called_when_the_run_budget_is_spent(tmp_path):
    drv = ScriptedDriver(tmp_path, [], limits={"max_run_seconds": 1})
    drv.on_prepare = lambda d, n: d.clock.advance(5)
    seen = []
    result, _ = _go(tmp_path, None, seen.append, drv=drv)
    assert result.reason == "run-budget" and seen == []


def test_an_exception_in_the_callback_does_not_stop_the_run(tmp_path):
    def boom(prepared):
        raise RuntimeError("callback failed")

    result, drv = _go(tmp_path, [outcome()], boom)
    assert result.outcome == "COMPLETED" and "start" in drv.names()
