"""Safety checks for the operation record, recorded runtimes and the hook interpreter.

Every test runs under the real-home I/O guard, the dangerous-subprocess recorder and the real-seam tripwire.
"""
import dataclasses
import json
import os
import shutil

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home)
from quoin.streamdeck import engine

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")

WARNING = "ignored an unexpected path in the operation record"


def marker_path(lh):
    return lh.runtime / "operation.json"


def load_marker(lh):
    return json.loads(marker_path(lh).read_text())


def save_marker(lh, data):
    marker_path(lh).write_text(json.dumps(data))


def live_version_file(lh):
    for base, _dirs, files in os.walk(str(lh.runtime / "versions")):
        for name in sorted(files):
            if name.endswith(".js") or name.endswith(".py"):
                return os.path.join(base, name)
    raise AssertionError("no payload file found")


def interrupted_with_aside(lh, *, phase):
    """An install interrupted over a hand-edited version dir, so the marker records a version-dir aside."""
    engine.install(ops.make_env(lh))
    with open(live_version_file(lh), "a") as handle:
        handle.write("\n# edited by hand\n")
    if phase == "committed":
        fault = {"claude_settings:after": RuntimeError("boom")}
        with pytest.raises(engine.OperationError):
            engine.install(ops.make_env(lh, faults=fault, now=ops.LATER))
    else:
        with pytest.raises(SystemExit):
            engine.install(ops.make_env(lh, faults={"plugin_swap": SystemExit(9)}, now=ops.LATER))
    marker = load_marker(lh)
    assert marker["phase"] == phase
    return marker


def run_completing(lh, how):
    env = ops.make_env(lh, now=ops.LATER)
    if how == "install":
        return engine.install(env)
    if how == "uninstall":
        return engine.uninstall(env)
    raise AssertionError(how)


def make_outside(lh, source):
    """Move `source` out of the runtime and leave a symlink to it behind; returns the outside dir."""
    outside = lh.root / "outside-of-the-runtime"
    shutil.move(source, str(outside))
    os.symlink(str(outside), source)
    return outside


# ------------------------------------------------------------------ symlinked staging components

@pytest.mark.parametrize("how,phase", [("install", "committed"), ("uninstall", "committed"),
                                       ("install", "staging")])
def test_a_symlinked_aside_dir_is_never_followed(lifecycle_home, how, phase):
    lh = lifecycle_home
    marker = interrupted_with_aside(lh, phase=phase)
    aside = marker["previous"]["version_dir_aside"]["path"]
    outside = make_outside(lh, os.path.dirname(aside))
    before = lc.tree_snapshot(outside)
    assert before
    result = run_completing(lh, how)
    assert lc.tree_snapshot(outside) == before
    if how == "install":  # an uninstall deletes the whole staging dir without following links, so it has nothing to report
        assert any(WARNING in line for line in result.lines)
    assert not marker_path(lh).exists()


@pytest.mark.parametrize("how,phase", [("install", "committed"), ("uninstall", "committed"),
                                       ("install", "staging")])
def test_a_symlinked_rollback_dir_is_never_followed(lifecycle_home, how, phase):
    lh = lifecycle_home
    marker = interrupted_with_aside(lh, phase=phase)
    outside = make_outside(lh, marker["rollback_dir"])
    before = lc.tree_snapshot(outside)
    assert before
    result = run_completing(lh, how)
    assert lc.tree_snapshot(outside) == before
    if how == "install":  # an uninstall deletes the whole staging dir without following links, so it has nothing to report
        assert any(WARNING in line for line in result.lines)
    assert not marker_path(lh).exists()


# ------------------------------------------------------------------ typed fields and tracked names

def installed(lh):
    engine.install(ops.make_env(lh))
    return lh.resolve()


@pytest.mark.parametrize("field", ["created", "existing"])
@pytest.mark.parametrize("value", [["versions"], ["bin"], ["profile"], "versions", 7])
def test_only_tracked_names_and_lists_steer_a_rollback(lifecycle_home, field, value):
    lh = lifecycle_home
    paths = installed(lh)
    before = lc.tree_snapshot(lh.runtime)
    messages = []
    marker = {"op": "install", "phase": "staging", "steps_done": [], "draft": {},
              "previous": {field: value, "current_target": "versions/" + os.listdir(paths.versions_dir)[0]}}
    engine.rollback(paths, marker, messages.append)
    assert lc.tree_snapshot(lh.runtime) == before
    assert (lh.runtime / "versions").is_dir()
    if isinstance(value, list):
        assert messages and all(WARNING in m for m in messages)
    else:
        assert messages == []


def test_stale_lists_that_are_not_lists_are_ignored(lifecycle_home):
    marker = {"op": "install", "phase": "committed", "steps_done": [], "draft": {}, "previous": {},
              "stale": {"rollback_dirs": "abc", "plugin_asides": 5, "version_dir_asides": ["x", 3, None]}}
    carry = engine.carry_from_marker(marker, [])
    assert carry.stale == {"rollback_dirs": [], "plugin_asides": [], "version_dir_asides": ["x"]}


def test_the_tracked_names_cover_every_mode(lifecycle_home):
    lh = lifecycle_home
    for missing, mode in (((), "active"), (("python",), "dormant")):
        engine.install(ops.make_env(lh, missing=missing))
        tracked = []
        original = engine._Run._tracked

        def spy(self, tracked=tracked, original=original):
            result = original(self)
            tracked.extend(result)
            return result

        engine._Run._tracked = spy
        try:
            engine.install(ops.make_env(lh, missing=missing, now=ops.LATER))
        finally:
            engine._Run._tracked = original
        assert tracked, mode
        assert set(tracked) <= set(engine.TRACKED_RELS), (mode, tracked)


@pytest.mark.parametrize("target", ["/tmp/elsewhere", "../x", "versions/../../x", 5])
def test_an_invalid_current_target_leaves_current_alone(lifecycle_home, target):
    lh = lifecycle_home
    paths = installed(lh)
    current_before = os.readlink(paths.current)
    messages = []
    marker = {"op": "install", "phase": "staging", "steps_done": [], "draft": {}, "previous": {"current_target": target}}
    engine.rollback(paths, marker, messages.append)
    assert os.readlink(paths.current) == current_before
    assert messages and WARNING in messages[0]


def test_a_valid_current_target_is_restored(lifecycle_home):
    lh = lifecycle_home
    paths = installed(lh)
    name = os.listdir(paths.versions_dir)[0]
    os.unlink(paths.current)
    os.symlink("versions/other", paths.current)
    messages = []
    marker = {"op": "install", "phase": "staging", "steps_done": [], "draft": {},
              "previous": {"current_target": "versions/" + name}}
    engine.rollback(paths, marker, messages.append)
    assert os.readlink(paths.current) == "versions/" + name
    assert messages == []
    # the absolute form under the versions directory is accepted too
    os.unlink(paths.current)
    marker["previous"]["current_target"] = os.path.join(paths.versions_dir, name)
    engine.rollback(paths, marker, messages.append)
    assert os.readlink(paths.current) == os.path.join(paths.versions_dir, name)
    assert messages == []


# ------------------------------------------------------------------ the lock was taken for another runtime

def _ignoring_the_lock(snapshot):
    return {k: v for k, v in snapshot.items() if os.path.basename(k) != ".install.lock"}


@pytest.mark.parametrize("how", ["install", "upgrade", "uninstall"])
def test_a_lock_taken_for_one_runtime_refuses_work_on_another(lifecycle_home, monkeypatch, how):
    lh = lifecycle_home
    installed(lh)
    before = _ignoring_the_lock(lc.tree_snapshot(lh.root))
    real_read = engine.read_state
    calls = []

    def read_other_paths_under_the_lock(env, op, **kw):
        state = real_read(env, op, **kw)
        calls.append(op)
        if len(calls) >= 2:
            other = dataclasses.replace(state.paths, runtime=os.path.join(str(lh.root), "elsewhere"))
            return dataclasses.replace(state, paths=other)
        return state

    def run():
        env = ops.make_env(lh, now=ops.LATER)
        return getattr(engine, how)(env)

    monkeypatch.setattr(engine, "read_state", read_other_paths_under_the_lock)
    with pytest.raises(engine.OperationError) as exc:
        run()
    assert "re-run the command" in str(exc.value)
    assert _ignoring_the_lock(lc.tree_snapshot(lh.root)) == before
    assert not marker_path(lh).exists()
    monkeypatch.setattr(engine, "read_state", real_read)
    assert run().exit_code == 0


def test_a_rollback_never_restores_files_from_a_symlinked_rollback_dir(lifecycle_home):
    lh = lifecycle_home
    paths = installed(lh)
    hook = lh.runtime / "bin" / "quoin-deck-hook"
    original = hook.read_bytes()
    elsewhere = lh.root / "elsewhere" / "bin"
    elsewhere.mkdir(parents=True)
    (elsewhere / "quoin-deck-hook").write_text("#!/bin/sh\necho planted\n")
    staging = lh.runtime / "staging"
    staging.mkdir(exist_ok=True)
    os.symlink(str(lh.root / "elsewhere"), str(staging / "rollback-x"))
    marker = {"op": "install", "phase": "staging", "steps_done": [], "draft": {},
              "rollback_dir": str(staging / "rollback-x"), "previous": {"existing": ["bin/quoin-deck-hook"]}}
    messages = []
    engine.rollback(paths, marker, messages.append)
    assert hook.read_bytes() == original
    assert (elsewhere / "quoin-deck-hook").exists()
    assert messages and WARNING in messages[0]
