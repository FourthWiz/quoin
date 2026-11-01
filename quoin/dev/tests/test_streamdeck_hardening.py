"""Safety checks for the operation record, recorded runtimes and the hook interpreter.

Every test runs under the real-home I/O guard, the dangerous-subprocess recorder and the real-seam tripwire.
"""
import dataclasses
import json
import os
import shutil
import types

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home)
from quoin.streamdeck import engine, macos, manifest, prereqs, status
import quoin.streamdeck as sd

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")

WARNING = "ignored an unexpected path in the operation record"
DROPPED = "ignored an unusable binary record in the operation record"
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)


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


# ------------------------------------------------------------------ helpers for the recorded state

def manifest_path(lh):
    return lh.runtime / "install-manifest.json"


def read_manifest(lh):
    return json.loads(manifest_path(lh).read_text())


def write_manifest(lh, data):
    manifest_path(lh).write_text(json.dumps(data, indent=2) + "\n")


def snapshot(lh):
    return lc.tree_snapshot(lh.root)


def status_exit(lh):
    report = status.finalize(status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN,
                                            now=ops.LATER))
    return report["exit_code"]


# ------------------------------------------------------------------ the recorded runtime

def install_elsewhere(lh):
    """An install at an alternative runtime whose manifest was copied to the shell-default location."""
    alt = lh.root / "alt-runtime"
    flags = {"runtime_dir": str(alt)}
    env = ops.make_env(lh, flags=flags)
    env.paths = lh.resolve(flags)
    engine.install(env)
    lh.runtime.mkdir(parents=True, exist_ok=True)
    (lh.runtime / "install-manifest.json").write_bytes((alt / "install-manifest.json").read_bytes())
    return alt


@pytest.mark.parametrize("how", ["install", "upgrade", "uninstall"])
def test_a_recorded_runtime_without_a_manifest_is_refused(lifecycle_home, how):
    lh = lifecycle_home
    alt = install_elsewhere(lh)
    os.unlink(str(alt / "install-manifest.json"))
    env = ops.make_env(lh, now=ops.LATER)
    before = snapshot(lh)
    with pytest.raises(engine.OperationError) as exc:
        getattr(engine, how)(env)
    assert "holds no matching manifest" in str(exc.value) and "--runtime-dir" in str(exc.value)
    assert snapshot(lh) == before


@pytest.mark.parametrize("how", ["install", "upgrade", "uninstall"])
def test_a_recorded_runtime_with_a_matching_manifest_is_followed(lifecycle_home, how):
    lh = lifecycle_home
    alt = install_elsewhere(lh)
    result = getattr(engine, how)(ops.make_env(lh, now=ops.LATER))
    assert result.exit_code == 0
    assert any("recorded runtime" in line for line in result.lines)
    assert (alt / "install-manifest.json").exists() == (how != "uninstall")


def test_an_explicit_runtime_flag_skips_the_recorded_runtime_check(lifecycle_home):
    lh = lifecycle_home
    alt = install_elsewhere(lh)
    os.unlink(str(alt / "install-manifest.json"))
    flags = {"runtime_dir": str(lh.runtime)}
    env = ops.make_env(lh, flags=flags, now=ops.LATER)
    env.paths = lh.resolve(flags)
    try:
        engine.install(env)
    except engine.OperationError as exc:
        assert "holds no matching manifest" not in str(exc)


# ------------------------------------------------------------------ purge roots

def install_with_state_root(lh):
    other = lh.root / "other-state"
    flags = {"state_root": str(other)}
    env = ops.make_env(lh, flags=flags)
    env.paths = lh.resolve(flags)
    engine.install(env)
    (other / "config.json").write_text("{}")
    return other, flags


def test_purge_refuses_a_recorded_state_root_without_the_flag(lifecycle_home):
    lh = lifecycle_home
    other, flags = install_with_state_root(lh)
    before = lc.tree_snapshot(other)
    assert (other / "config.json").exists() and (other / "state").is_dir()
    with pytest.raises(engine.OperationError) as exc:
        engine.uninstall(ops.make_env(lh, purge=True, now=ops.LATER))
    assert "pass --state-root %s to purge it" % other in str(exc.value)
    assert lc.tree_snapshot(other) == before
    # a plain uninstall never deletes state, so it needs no flag
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert (other / "config.json").exists()


def test_a_manifest_that_records_a_state_dir_apart_from_its_config_is_not_followed(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    data = read_manifest(lh)
    other_state = str(lh.root / "state-elsewhere")
    os.makedirs(other_state)
    data["paths"]["state"] = other_state
    write_manifest(lh, data)
    # the manifest breaks the state-dir invariant, so it never redirects a purge
    result = engine.uninstall(ops.make_env(lh, purge=True, now=ops.LATER))
    assert result.exit_code == 0
    assert os.path.isdir(other_state)


def test_purge_with_the_flag_removes_the_recorded_state(lifecycle_home):
    lh = lifecycle_home
    other, flags = install_with_state_root(lh)
    env = ops.make_env(lh, flags=flags, purge=True, now=ops.LATER)
    env.paths = lh.resolve(flags)
    engine.uninstall(env)
    assert not (other / "config.json").exists() and not (other / "state").exists()


# ------------------------------------------------------------------ one rule for a usable python

def test_usable_python_accepts_only_executable_regular_files(tmp_path):
    good = tmp_path / "python3"
    good.write_text("#!/bin/sh\n")
    good.chmod(0o755)
    plain = tmp_path / "plain"
    plain.write_text("x")
    plain.chmod(0o644)
    directory = tmp_path / "adir"
    directory.mkdir()
    newline = tmp_path / "py\nthon"
    newline.write_text("#!/bin/sh\n")
    newline.chmod(0o755)
    usable = engine.usable_python
    assert usable({"path": str(good)}) == str(good)
    assert usable({"path": str(plain)}) is None            # not executable
    assert usable({"path": str(directory)}) is None        # executable bit, but not a regular file
    assert usable({"path": str(newline)}) is None          # a real executable whose name has a newline
    assert usable({"path": str(tmp_path / "missing")}) is None
    assert usable({"path": "relative/python"}) is None
    assert usable({"path": str(good) + "\0x"}) is None     # must not raise from os.access
    assert usable({"path": None}) is None and usable({}) is None
    assert usable(str(good)) is None and usable(None) is None and usable(["x"]) is None


def operation(lh, binaries):
    run = engine._Run(ops.make_env(lh), "install")
    run.binaries = binaries
    return run


def test_the_hook_interpreter_warning_is_printed_once(lifecycle_home):
    lh = lifecycle_home
    run = operation(lh, {"python": {"path": "/no/such/python"}})
    assert run._python_for_shim() is None
    assert run._python_for_shim() is None
    assert [line for line in run.lines if "recorded python" in line] == [
        "warning: the recorded python /no/such/python is not usable; hooks only drain their input until quoin "
        "streamdeck install records a working interpreter"]


def test_no_python_entry_is_silent(lifecycle_home):
    run = operation(lifecycle_home, {})
    assert run._python_for_shim() is None
    assert run.lines == []


def test_a_dormant_install_without_python_says_nothing_about_a_recorded_python(lifecycle_home):
    lh = lifecycle_home
    result = engine.install(ops.make_env(lh, missing=("python",)))
    assert result.mode == "dormant"
    assert not any("recorded python" in line for line in result.lines)


# ------------------------------------------------------------------ an active deck with no usable python

def installed_active(lh):
    engine.install(ops.make_env(lh))
    data = read_manifest(lh)
    assert data["mode"] == "active" and data["binaries"]["python"]["path"] == lc.fixture_python(lh)
    return data


def recorded_python(lh, entry):
    data = read_manifest(lh)
    if entry is None:
        data["binaries"].pop("python", None)
    else:
        data["binaries"]["python"] = entry
    write_manifest(lh, data)


def bad_python_cases(lh):
    plain = lh.root / "plain-file"
    plain.write_text("x")
    plain.chmod(0o644)
    directory = lh.root / "a-directory"
    directory.mkdir()
    newline = lh.root / "py\nthon"
    newline.write_text("#!/bin/sh\n")
    newline.chmod(0o755)
    return {
        "missing file": {"path": str(lh.root / "gone" / "python3")},
        "not executable": {"path": str(plain)},
        "a directory": {"path": str(directory)},
        "newline in the name": {"path": str(newline)},
        "plain string": str(lc.fixture_python(lh)),
    }


@pytest.mark.parametrize("name", ["missing file", "not executable", "a directory", "newline in the name",
                                  "plain string"])
@pytest.mark.parametrize("how", ["install", "upgrade"])
def test_an_unusable_recorded_python_refuses_before_anything_changes(lifecycle_home, name, how):
    lh = lifecycle_home
    installed_active(lh)
    recorded_python(lh, bad_python_cases(lh)[name])
    env = ops.make_env(lh, missing=("python",), tty=False, now=ops.LATER)
    before = snapshot(lh)
    with pytest.raises(engine.OperationError) as exc:
        getattr(engine, how)(env)
    assert exc.value.code == 1
    assert "--python" in str(exc.value) and "no usable python is recorded" in str(exc.value)
    assert snapshot(lh) == before
    assert not marker_path(lh).exists()


def test_a_missing_python_entry_refuses_too(lifecycle_home):
    lh = lifecycle_home
    installed_active(lh)
    recorded_python(lh, None)
    env = ops.make_env(lh, missing=("python",), now=ops.LATER)
    before = snapshot(lh)
    with pytest.raises(engine.OperationError) as exc:
        engine.install(env)
    assert "none recorded" in str(exc.value)
    assert snapshot(lh) == before


def test_a_usable_recorded_python_keeps_the_deck_running(lifecycle_home):
    lh = lifecycle_home
    installed_active(lh)
    python = lc.fixture_python(lh)
    result = engine.install(ops.make_env(lh, missing=("python",), now=ops.LATER))
    assert result.exit_code == 0 and result.mode == "active"
    assert "PY=%s" % python in (lh.runtime / "bin" / "quoin-deck-hook").read_text()
    assert json.loads((lh.plugin_dir / "plugin-config.json").read_text())["python"] == python


def test_a_vanished_recorded_python_does_not_refuse_when_a_fresh_one_resolves(lifecycle_home):
    lh = lifecycle_home
    installed_active(lh)
    recorded_python(lh, {"path": str(lh.root / "gone" / "python3")})
    python = lc.fixture_python(lh)
    result = engine.install(ops.make_env(lh, missing=("node",), tty=False, now=ops.LATER))
    assert result.exit_code == 0
    assert read_manifest(lh)["binaries"]["python"]["path"] == python
    assert "PY=%s" % python in (lh.runtime / "bin" / "quoin-deck-hook").read_text()


def test_the_refusal_applies_to_a_dry_run_as_a_report(lifecycle_home):
    lh = lifecycle_home
    installed_active(lh)
    recorded_python(lh, {"path": str(lh.root / "gone" / "python3")})
    env = ops.make_env(lh, missing=("python",), dry_run=True, now=ops.LATER)
    before = snapshot(lh)
    result = engine.install(env)
    assert result.exit_code == 1
    assert any("dry run: the real run would refuse:" in line and "--python" in line for line in result.lines)
    assert snapshot(lh) == before


# ------------------------------------------------------------------ binary records inside the operation record

def interrupted_after_commit(lh):
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}))
    data = load_marker(lh)
    assert data["phase"] == "committed" and data["draft"]["wired"]
    return data


def plant_binaries(lh, binaries):
    data = load_marker(lh)
    data["draft"]["binaries"] = binaries
    save_marker(lh, data)


def count(lines, text):
    return len([line for line in lines if text in line])


def assert_finished_cleanly(lh, result, python):
    assert result.exit_code == 0
    assert not marker_path(lh).exists()
    data = read_manifest(lh)
    assert manifest.validate(data) == []
    assert data["binaries"]["python"]["path"] == python
    assert "PY=%s" % python in (lh.runtime / "bin" / "quoin-deck-hook").read_text()
    assert json.loads((lh.plugin_dir / "plugin-config.json").read_text())["python"] == python
    assert status_exit(lh) != 12
    assert engine.install(ops.make_env(lh, missing=("python",), now=ops.LATER)).exit_code == 0


def completing_with_manifest_python(lh, binaries):
    """First install (python present) records the fixture python; a later interrupted run left `binaries`."""
    engine.install(ops.make_env(lh))
    python = lc.fixture_python(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}, now=ops.LATER))
    plant_binaries(lh, binaries)
    env = ops.make_env(lh, missing=("python",), tty=False, now=ops.LATER)
    result = engine.install(env)
    return env, result, python


@pytest.mark.parametrize("draft", [{"python": {"path": "venv/bin/python"}}, {"python": "/abs/python3"}])
def test_an_unusable_python_in_the_record_is_dropped_and_the_manifest_python_applies(lifecycle_home, draft):
    lh = lifecycle_home
    env, result, python = completing_with_manifest_python(lh, draft)
    assert count(env.lines, DROPPED) == 1 and "python" in [l for l in env.lines if DROPPED in l][0]
    assert_finished_cleanly(lh, result, python)


@pytest.mark.parametrize("draft", ["abc", ["python"]])
def test_a_binaries_record_that_is_not_a_mapping_is_dropped(lifecycle_home, draft):
    lh = lifecycle_home
    env, result, python = completing_with_manifest_python(lh, draft)
    warnings = [line for line in env.lines if DROPPED in line]
    assert len(warnings) == 1 and warnings[0].endswith(": binaries")
    assert_finished_cleanly(lh, result, python)


def test_only_the_valid_entries_of_a_mixed_record_survive(lifecycle_home):
    lh = lifecycle_home
    mixed = {"zellij": {"path": "/abs/zellij-from-the-record"}, "node": {"mode": "weird"},
             "nope": {"path": "/x"}, "osascript": "rel"}
    env, result, python = completing_with_manifest_python(lh, mixed)
    recorded = env.fake_ctx.ctx.recorded
    assert recorded["zellij"]["path"] == "/abs/zellij-from-the-record"
    assert "nope" not in recorded
    assert recorded["node"]["mode"] in ("external", "bundled")
    assert isinstance(recorded["osascript"], dict)
    assert count(env.lines, DROPPED) == 3
    assert_finished_cleanly(lh, result, python)


def no_python_anywhere(lh, with_manifest):
    if with_manifest:
        engine.install(ops.make_env(lh))
        with pytest.raises(engine.OperationError):
            engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}, now=ops.LATER))
        recorded_python(lh, None)
    else:
        interrupted_after_commit(lh)
    plant_binaries(lh, {"python": {"path": "venv/bin/python"}})


@pytest.mark.parametrize("with_manifest", [True, False])
def test_no_usable_python_anywhere_is_refused_in_preflight(lifecycle_home, with_manifest):
    lh = lifecycle_home
    no_python_anywhere(lh, with_manifest)
    env = ops.make_env(lh, missing=("python",), tty=False, now=ops.LATER)
    before = snapshot(lh)
    with pytest.raises(engine.OperationError) as exc:
        engine.install(env)
    assert exc.value.code == 1 and "--python" in str(exc.value)
    assert snapshot(lh) == before          # the marker bytes included
    assert count(env.lines, DROPPED) == 1


@pytest.mark.parametrize("with_manifest", [True, False])
def test_the_dry_run_reports_the_same_refusal(lifecycle_home, with_manifest):
    lh = lifecycle_home
    no_python_anywhere(lh, with_manifest)
    env = ops.make_env(lh, missing=("python",), dry_run=True, now=ops.LATER)
    before = snapshot(lh)
    result = engine.install(env)
    assert result.exit_code == 1
    assert count(result.lines, DROPPED) == 1
    assert count(result.lines, "dry run: the real run would refuse:") == 1
    assert snapshot(lh) == before


@pytest.mark.parametrize("with_manifest", [True, False])
def test_passing_python_completes_the_interrupted_run(lifecycle_home, with_manifest):
    lh = lifecycle_home
    no_python_anywhere(lh, with_manifest)
    python = lc.fixture_python(lh)
    env = ops.make_env(lh, missing=("python",), flags={"python": python}, now=ops.LATER)
    env.fake_ctx.probes[(python, "-E", "-s", "-c", prereqs.PY_PROBE)] = (0, "3.14.0\n", "")
    result = engine.install(env)
    assert_finished_cleanly(lh, result, python)


def test_the_under_lock_repeat_still_refuses_when_the_preflight_is_skipped(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    no_python_anywhere(lh, True)
    monkeypatch.setattr(engine._Run, "_python_preflight", lambda self, *a: None)
    marker_before = marker_path(lh).read_bytes()
    with pytest.raises(engine.OperationError) as exc:
        engine.install(ops.make_env(lh, missing=("python",), now=ops.LATER))
    assert exc.value.code == 1 and "--python" in str(exc.value)
    assert marker_path(lh).read_bytes() == marker_before


def test_the_plugin_step_names_the_flag_when_every_earlier_check_is_skipped(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    no_python_anywhere(lh, True)
    monkeypatch.setattr(engine._Run, "_python_preflight", lambda self, *a: None)
    monkeypatch.setattr(engine._Run, "_require_python", lambda self, *a: None)
    with pytest.raises(engine.OperationError) as exc:
        engine.install(ops.make_env(lh, missing=("python",), now=ops.LATER))
    assert "--python" in str(exc.value) and "ValueError" not in str(exc.value)


# ------------------------------------------------------------------ manifest rules shared with the engine

@pytest.mark.parametrize("key,entry,expected", [
    ("python", {"path": "/abs/python3"}, None),
    ("python", "/abs/python3", None),
    ("python", {"path": "rel/python"}, "binaries.python path must be absolute"),
    ("nope", {"path": "/x"}, "binaries.nope is not a known key"),
    ("node", {"mode": "external", "path": "/abs/node"}, None),
    ("node", {"mode": "bundled", "path": None}, None),
    ("node", {"mode": "bundled", "path": "/abs/node"}, "binaries.node.path must be null for bundled mode"),
    ("node", {"mode": "external", "path": "rel"}, "binaries.node.path must be an absolute path for external mode"),
    ("node", {"mode": "weird"}, "binaries.node.mode must be external or bundled"),
    ("node", "/abs/node", "binaries.node must be an object with a mode"),
])
def test_binary_problem_follows_the_manifest_rule(key, entry, expected):
    assert manifest.binary_problem(key, entry) == expected


def test_validate_reports_the_same_messages(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    data = read_manifest(lh)
    data["binaries"] = {"nope": {"path": "/x"}, "node": {"mode": "weird"}, "python": {"path": "rel"}}
    assert [p for p in manifest.validate(data) if p.startswith("binaries")] == [
        "binaries.nope is not a known key", "binaries.node.mode must be external or bundled",
        "binaries.python path must be absolute"]


# ------------------------------------------------------------------ tools from the Python environment

def run_with_tools(lh, which, **ctx_attrs):
    flags = {"python": lc.fixture_python(lh)}
    env = ops.make_env(lh, flags=flags)
    env.fake_ctx.which_table.update(which)
    for key, value in ctx_attrs.items():
        setattr(env.fake_ctx.ctx, key, value)
    engine.install(env)
    return read_manifest(lh)["binaries"]


def test_tools_inside_a_venv_are_not_recorded(lifecycle_home):
    lh = lifecycle_home
    venv = str(lh.root / "venv")
    binaries = run_with_tools(
        lh, {"python3": venv + "/bin/python3", "claude": venv + "/lib/python3.12/site-packages/x/bin/claude",
             "codex": "/opt/tools/bin/codex"}, sys_prefix=venv, sys_is_venv=True)
    assert "python3" not in binaries and "claude" not in binaries
    assert binaries["codex"]["path"] == "/opt/tools/bin/codex"


def test_tools_inside_the_active_virtual_env_are_not_recorded(lifecycle_home):
    lh = lifecycle_home
    venv = str(lh.root / "other-venv")
    binaries = run_with_tools(lh, {"python3": venv + "/bin/python3", "codex": "/opt/tools/bin/codex"},
                              virtual_env=venv, sys_is_venv=False)
    assert "python3" not in binaries
    assert binaries["codex"]["path"] == "/opt/tools/bin/codex"


def test_tools_next_to_a_base_interpreter_are_recorded(lifecycle_home):
    lh = lifecycle_home
    binaries = run_with_tools(lh, {"claude": "/usr/local/bin/claude", "lazygit": "/usr/local/bin/lazygit"},
                              sys_prefix="/usr/local", sys_is_venv=False)
    assert binaries["claude"]["path"] == "/usr/local/bin/claude"
    assert binaries["lazygit"]["path"] == "/usr/local/bin/lazygit"
    # python discovery keeps its stricter rule: the base prefix is skipped for the fallback `python3` lookup
    ctx = ops.make_env(lh).fake_ctx
    ctx.which_table["python3"] = "/usr/local/bin/python3"
    ctx.ctx.sys_prefix, ctx.ctx.sys_is_venv = "/usr/local", False
    assert "/usr/local/bin/python3" not in [path for _src, path in prereqs.python_candidates(ctx.ctx)]


def test_a_carry_built_elsewhere_is_filtered_before_it_overlays_the_manifest(lifecycle_home):
    lh = lifecycle_home
    run = operation(lh, {})
    state = types.SimpleNamespace(manifest={"binaries": {"python": {"path": "/abs/python3"}}})
    carry = engine.Carry(completing=True, binaries={"python": {"path": "rel/python"}, "zellij": {"path": "/abs/z"}})
    assert run._recorded_binaries(state, carry) == {"python": {"path": "/abs/python3"}, "zellij": {"path": "/abs/z"}}


# ------------------------------------------------------------------ state directories during an install

def test_an_install_refuses_a_symlinked_state_dir_and_completes_once_it_is_fixed(lifecycle_home):
    lh = lifecycle_home
    target = lh.root / "somewhere-else"
    target.mkdir()
    lh.state_root.mkdir(parents=True, mode=0o700)
    os.symlink(str(target), str(lh.state_root / "state"))
    before = lc.tree_snapshot(target)
    with pytest.raises(engine.OperationError) as exc:
        engine.install(ops.make_env(lh))
    assert "is a symlink" in str(exc.value)
    assert lc.tree_snapshot(target) == before
    assert status_exit(lh) == 12
    os.unlink(str(lh.state_root / "state"))
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    assert result.exit_code == 0 and not marker_path(lh).exists()
    assert status_exit(lh) != 12


def test_an_install_tightens_a_loose_agents_dir(lifecycle_home):
    lh = lifecycle_home
    agents = lh.state_root / "state" / "agents"
    agents.mkdir(parents=True)
    os.chmod(str(lh.state_root / "state"), 0o700)
    os.chmod(str(agents), 0o755)
    result = engine.install(ops.make_env(lh))
    assert any("tightened permissions on %s" % agents in line for line in result.lines)
    assert (os.stat(str(agents)).st_mode & 0o777) == 0o700


# ------------------------------------------------------------------ apps directory override

def test_the_apps_directory_defaults_and_can_be_overridden():
    assert prereqs.apps_dir_from({}) == "/Applications"
    assert prereqs.apps_dir_from({prereqs.ENV_APPS_DIR: ""}) == "/Applications"
    assert prereqs.apps_dir_from({prereqs.ENV_APPS_DIR: "/x/Apps"}) == "/x/Apps"
