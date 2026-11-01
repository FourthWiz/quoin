"""Operation-marker handling: version-dir asides, state changes under the lock, planted paths, odd shapes."""
import json
import os

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
from quoin.streamdeck import engine, macos, status
import quoin.streamdeck as sd

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)


def marker_path(lh):
    return lh.runtime / "operation.json"


def load_marker(lh):
    return json.loads(marker_path(lh).read_text())


def save_marker(lh, data):
    marker_path(lh).write_text(json.dumps(data))


def live_version_file(lh):
    versions = lh.runtime / "versions"
    for base, _dirs, files in os.walk(str(versions)):
        for name in sorted(files):
            if name.endswith(".js") or name.endswith(".py"):
                return os.path.join(base, name)
    raise AssertionError("no payload file found")


# ------------------------------------------------------------------ version-dir aside

def interrupted_over_a_modified_version_dir(lh):
    engine.install(ops.make_env(lh))
    with open(live_version_file(lh), "a") as handle:
        handle.write("\n# edited by hand\n")
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}, now=ops.LATER))
    marker = load_marker(lh)
    aside = marker["previous"]["version_dir_aside"]
    assert isinstance(aside, dict) and os.path.isdir(aside["path"])
    return aside


def test_a_completing_install_removes_a_version_dir_aside(lifecycle_home):
    lh = lifecycle_home
    aside = interrupted_over_a_modified_version_dir(lh)
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    assert not marker_path(lh).exists()
    assert not os.path.exists(aside["path"])
    assert "completing an interrupted operation" in "\n".join(result.lines)
    ops.assert_hooks_inert(ops.run_hook_commands(lh))


def test_an_uninstall_removes_a_version_dir_aside(lifecycle_home):
    lh = lifecycle_home
    aside = interrupted_over_a_modified_version_dir(lh)
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert not marker_path(lh).exists()
    assert not os.path.exists(aside["path"])
    assert not (lh.runtime / "versions").exists()


# ------------------------------------------------------------------ state changed before the lock

def test_a_run_that_waited_for_the_lock_does_not_roll_back_a_finished_install(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    with pytest.raises(SystemExit):
        engine.install(ops.make_env(lh, faults={"plugin_swap": SystemExit(9)}))
    assert load_marker(lh)["phase"] == "staging"
    real_lock = engine._Run._lock
    done = []

    def lock_after_the_other_run_finished(self, paths):
        if not done:
            done.append(True)
            engine.install(ops.make_env(lh, now=ops.LATER))
        return real_lock(self, paths)

    monkeypatch.setattr(engine._Run, "_lock", lock_after_the_other_run_finished)
    with pytest.raises(engine.OperationError) as exc:
        engine.install(ops.make_env(lh, now=ops.LATER))
    assert "re-run" in str(exc.value)
    assert not marker_path(lh).exists()
    assert os.path.islink(str(lh.runtime / "current")) and os.path.isdir(str(lh.runtime / "current"))
    assert (lh.runtime / "install-manifest.json").exists() and (lh.plugin_dir / "manifest.json").exists()
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    report = status.finalize(status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN,
                                            now=ops.LATER))
    assert report["exit_code"] == 0, report["problems"]


# ------------------------------------------------------------------ partial uninstall re-check

def test_the_partial_uninstall_recheck_reads_the_recorded_runtime_not_the_shell_default(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    alt = lh.root / "alt-runtime"
    flags = {"runtime_dir": str(alt)}
    env = ops.make_env(lh, flags=flags)
    env.paths = lh.resolve(flags)
    engine.install(env)
    # the manifest the shell-default location holds records the other runtime
    lh.runtime.mkdir(parents=True, exist_ok=True)
    (lh.runtime / "install-manifest.json").write_bytes((alt / "install-manifest.json").read_bytes())
    real_lock = engine._Run._lock

    def lock_then_leave_a_full_uninstall_marker(self, paths):
        fd = real_lock(self, paths)
        assert paths.runtime == str(alt)  # the recorded runtime wins over the shell default
        (alt / "operation.json").write_text(json.dumps(
            {"op": "uninstall", "phase": "committed", "steps_done": [], "runtimes": None, "draft": {},
             "stale": {}, "previous": {}}))
        return fd

    monkeypatch.setattr(engine._Run, "_lock", lock_then_leave_a_full_uninstall_marker)
    with pytest.raises(engine.OperationError) as exc:
        engine.uninstall(ops.make_env(lh, runtimes=["claude"], now=ops.LATER))
    assert "uninstall" in str(exc.value)


# ------------------------------------------------------------------ plugin aside recorded before the rename

def test_a_completing_run_records_the_plugin_aside_before_renaming(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    with open(str(lh.plugin_dir / "plugin-config.json"), "a") as handle:
        handle.write(" ")
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}, now=ops.LATER))
    seen = {}
    real_rename = os.rename

    def spy(src, dst, *a, **kw):
        if os.fspath(src) == str(lh.plugin_dir) and ".quoin-plugin-old-" in os.fspath(dst):
            seen["recorded"] = list(load_marker(lh)["stale"]["plugin_asides"])
            seen["dst"] = os.fspath(dst)
        return real_rename(src, dst, *a, **kw)

    # make the live plugin differ so the completing run swaps it again
    (lh.plugin_dir / "manifest.json").write_text((lh.plugin_dir / "manifest.json").read_text() + "\n")
    monkeypatch.setattr(os, "rename", spy)
    engine.install(ops.make_env(lh, now=ops.LATER))
    assert seen.get("dst") in seen.get("recorded", [])


# ------------------------------------------------------------------ planted paths

def plant_staging_marker(lh, **previous):
    """A crashed install's staging marker, with the given previous-state overrides and extra stale paths."""
    with pytest.raises(SystemExit):
        engine.install(ops.make_env(lh, faults={"plugin_swap": SystemExit(9)}))
    data = load_marker(lh)
    data["previous"].update(previous)
    save_marker(lh, data)
    return data


def make_victims(lh):
    victims = {}
    docs = lh.home / "Documents"
    docs.mkdir()
    (docs / "thesis.txt").write_text("keep")
    victims["docs"] = docs
    outside = lh.root / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    victims["outside"] = outside
    return victims


def survives(victims):
    return all((v / n).exists() for v, n in ((victims["docs"], "thesis.txt"), (victims["outside"], "keep.txt")))


def test_rollback_ignores_planted_paths(lifecycle_home):
    lh = lifecycle_home
    victims = make_victims(lh)
    data = plant_staging_marker(
        lh, created=["../../../Documents", "/%s" % victims["docs"], "bin/../../outside"],
        existing=[str(victims["docs"])], created_paths=[str(victims["docs"]), str(victims["outside"])],
        plugin_aside={"path": str(victims["docs"]), "kept": False},
        version_dir_aside={"path": str(victims["outside"]), "original": str(victims["docs"])})
    data["rollback_dir"] = str(victims["docs"])
    data["draft"]["version_dir"] = str(victims["outside"])
    save_marker(lh, data)
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    assert survives(victims)
    assert any("unexpected path" in line for line in result.lines)
    assert not marker_path(lh).exists()


def test_rollback_does_not_follow_a_symlink_out_of_the_runtime(lifecycle_home):
    lh = lifecycle_home
    victims = make_victims(lh)
    (lh.runtime / "bin").mkdir(parents=True, exist_ok=True)
    os.rmdir(str(lh.runtime / "bin"))
    os.symlink(str(victims["outside"]), str(lh.runtime / "bin"))
    (victims["outside"] / "quoin-deck-hook").write_text("keep")
    plant = {"created": ["bin/quoin-deck-hook"]}
    marker = {"op": "install", "phase": "staging", "steps_done": [], "previous": plant, "draft": {}}
    paths = lh.resolve()
    messages = []
    engine.rollback(paths, marker, messages.append)
    assert (victims["outside"] / "quoin-deck-hook").exists() and survives(victims)
    assert messages


@pytest.mark.parametrize("key", ["rollback_dirs", "plugin_asides", "version_dir_asides"])
def test_a_completing_install_ignores_planted_stale_paths(lifecycle_home, key):
    lh = lifecycle_home
    victims = make_victims(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}))
    data = load_marker(lh)
    data["stale"][key] = [str(victims["docs"]), "../Documents"]
    data["rollback_dir"] = str(victims["docs"])
    save_marker(lh, data)
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    assert survives(victims)
    assert any("unexpected path" in line for line in result.lines)


@pytest.mark.parametrize("key", ["rollback_dirs", "plugin_asides", "version_dir_asides"])
def test_uninstall_ignores_planted_stale_paths(lifecycle_home, key):
    lh = lifecycle_home
    victims = make_victims(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}))
    data = load_marker(lh)
    data["stale"][key] = [str(victims["docs"]), str(victims["outside"])]
    data["previous"]["plugin_aside"] = {"path": str(victims["docs"]), "kept": False}
    save_marker(lh, data)
    result = engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert survives(victims)
    assert any("unexpected path" in line for line in result.lines)


def test_marker_path_ok_accepts_only_owned_shapes(lifecycle_home):
    paths = lifecycle_home.resolve()
    ok = engine.marker_path_ok
    good_rb = os.path.join(paths.staging, "rollback-abc")
    assert ok(paths, "rollback_dir", good_rb)
    assert not ok(paths, "rollback_dir", paths.staging)
    assert not ok(paths, "rollback_dir", good_rb + "/../..")
    assert not ok(paths, "rollback_dir", "relative/rollback-x")
    assert ok(paths, "version_aside", os.path.join(good_rb, "aside", "1.0.0"))
    assert not ok(paths, "version_aside", os.path.join(good_rb, "other", "1.0.0"))
    assert ok(paths, "version_dir", os.path.join(paths.versions_dir, "1.0.0"))
    assert not ok(paths, "version_dir", paths.versions_dir)
    assert ok(paths, "plugin_aside", os.path.join(paths.plugins_dir, ".quoin-plugin-old-1"))
    assert not ok(paths, "plugin_aside", paths.plugin_dir)
    assert ok(paths, "created_path", paths.versions_dir) and not ok(paths, "created_path", paths.home)
    assert ok(paths, "rel", "bin/quoin-deck-hook")
    for bad in ("/etc", "../x", "a/../b", "", "a//b", None, 3):
        assert not ok(paths, "rel", bad)


# ------------------------------------------------------------------ status on odd markers

def test_status_treats_non_string_runtimes_as_a_malformed_marker(lifecycle_home):
    lh = lifecycle_home
    lh.runtime.mkdir(parents=True, exist_ok=True)
    save_marker(lh, {"op": "uninstall", "phase": "committed", "steps_done": [], "runtimes": [1, None]})
    report = status.finalize(status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN,
                                            now=ops.LATER))
    assert report["state"] == "operation-incomplete" and report["exit_code"] == 12
    assert "quoin streamdeck uninstall`" in json.dumps(report["problems"])
