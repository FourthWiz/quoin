"""The runtime launch seam leaves the Claude launch path untouched."""
from __future__ import annotations

import subprocess

import pytest

from quoin import supervisor as sup


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


_BEHAVIOURS = {
    "success": lambda: _Proc(0, "text", ""),
    "nonzero": lambda: _Proc(7, "", "bad"),
    "timeout-str": lambda: subprocess.TimeoutExpired(["c"], 1, output="p", stderr="e"),
    "timeout-bytes": lambda: subprocess.TimeoutExpired(["c"], 1, output=b"p", stderr=b"e"),
    "oserror": lambda: OSError("boom"),
}


def _patch(monkeypatch, repo_root, behaviour):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sup, "resolve_repo_root", lambda root: repo_root)
    return calls


@pytest.mark.parametrize("mode", ["allowedTools", "bypassPermissions"])
@pytest.mark.parametrize("name", sorted(_BEHAVIOURS))
def test_launcher_matches_direct_launch(monkeypatch, tmp_path, mode, name):
    direct_calls = _patch(monkeypatch, tmp_path / "repo", _BEHAVIOURS[name]())
    direct = sup.make_launch_fn(tmp_path, permission_mode=mode)("demo")
    seam_calls = list(direct_calls)
    direct_calls.clear()
    via = sup.launcher_for("claude").make_launch_fn(tmp_path, permission_mode=mode)("demo")
    assert direct_calls == seam_calls
    assert via == direct


def test_launcher_matches_direct_launch_with_spawn_tracking(monkeypatch, tmp_path):
    recorded = []

    class FakePopen:
        pid = 99

        def __init__(self, argv, **kwargs):
            recorded.append((argv, kwargs))
            self.returncode = 0

        def communicate(self, timeout=None):
            return "out", "err"

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sup, "resolve_repo_root", lambda root: tmp_path)
    spawned = []
    direct = sup.make_launch_fn(tmp_path)("demo", on_spawn=spawned.append)
    first = list(recorded)
    recorded.clear()
    via = sup.launcher_for("claude").make_launch_fn(tmp_path)("demo", on_spawn=spawned.append)
    assert recorded == first
    assert via == direct
    assert spawned == [99, 99]


def test_launcher_resolves_module_function_at_call_time(monkeypatch, tmp_path):
    seen = []
    sentinel = object()

    def recorder(*args, **kwargs):
        seen.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(sup, "make_launch_fn", recorder)
    out = sup.ClaudeLauncher().make_launch_fn(tmp_path, permission_mode="x")
    assert out is sentinel
    assert seen == [((tmp_path,), {"permission_mode": "x"})]


def test_launcher_keeps_child_tracking_flag(tmp_path):
    fn = sup.launcher_for("claude").make_launch_fn(tmp_path)
    assert fn.supports_child_tracking is True


def test_opencode_whole_task_is_unavailable():
    with pytest.raises(sup.RuntimeUnavailable) as err:
        sup.launcher_for("opencode")
    assert "--phase" in str(err.value)


def test_unknown_runtime_is_a_plain_value_error():
    with pytest.raises(ValueError) as err:
        sup.launcher_for("codex")
    assert not isinstance(err.value, sup.RuntimeUnavailable)


def test_launcher_shape():
    launcher = sup.launcher_for("claude")
    assert hasattr(launcher, "runtime") and callable(launcher.make_launch_fn)
    assert launcher.runtime == "claude"
    assert sup.RUNTIMES == ("claude", "opencode")
