"""Operation engine core: guard self-tests, lock, marker, preflight and recovery."""
import json
import os
import shutil
import subprocess

import pytest

import _streamdeck_helpers as h
import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (GuardViolation, deck_home, forbid_dangerous_subprocess, forbid_real_home_io,  # noqa: F401
                             forbid_real_seams, lifecycle_home, subprocess_calls)
from quoin.streamdeck import payload_build

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")

PROBE = os.path.join(h.REAL_HOME, ".claude", "guard-probe")


# ------------------------------------------------------------------ I/O guard self-tests

def _calls(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("x")
    srcdir = tmp_path / "srcdir"
    srcdir.mkdir()
    return {
        "builtins.open read": lambda: open(PROBE),
        "builtins.open write": lambda: open(PROBE, "w"),
        "io.open": lambda: __import__("io").open(PROBE),
        "os.open": lambda: os.open(PROBE, os.O_RDONLY),
        "os.replace": lambda: os.replace(str(src), PROBE),
        "os.rename": lambda: os.rename(str(src), PROBE),
        "os.symlink": lambda: os.symlink(str(src), PROBE),
        "os.link": lambda: os.link(str(src), PROBE),
        "os.mkdir": lambda: os.mkdir(PROBE),
        "os.makedirs": lambda: os.makedirs(PROBE),
        "os.rmdir": lambda: os.rmdir(PROBE),
        "os.remove": lambda: os.remove(PROBE),
        "os.unlink": lambda: os.unlink(PROBE),
        "os.chmod": lambda: os.chmod(PROBE, 0o600),
        "os.mkfifo": lambda: os.mkfifo(PROBE),
        "shutil.rmtree": lambda: shutil.rmtree(PROBE),
        "shutil.copy": lambda: shutil.copy(str(src), PROBE),
        "shutil.copy2": lambda: shutil.copy2(str(src), PROBE),
        "shutil.copyfile": lambda: shutil.copyfile(str(src), PROBE),
        "shutil.copytree": lambda: shutil.copytree(str(srcdir), PROBE),
        "shutil.move": lambda: shutil.move(str(src), PROBE),
        "source side of a copy": lambda: shutil.copy(PROBE, str(tmp_path / "dest")),
    }


def test_every_guarded_call_raises_before_touching_the_real_home(tmp_path, forbid_real_home_io):
    existed = os.path.lexists(PROBE)
    parent_existed = os.path.lexists(os.path.dirname(PROBE))
    calls = _calls(tmp_path)
    names = list(calls)
    for name in names:
        with pytest.raises(GuardViolation):
            calls[name]()
    assert len(forbid_real_home_io) == len(names)
    forbid_real_home_io.clear()
    assert os.path.lexists(PROBE) is existed
    assert os.path.lexists(os.path.dirname(PROBE)) is parent_existed
    assert not (tmp_path / "dest").exists()


def test_a_symlink_into_a_protected_root_is_caught_by_realpath(tmp_path, forbid_real_home_io):
    link = tmp_path / "sneaky"
    os.symlink.__wrapped__(os.path.dirname(PROBE), str(link))
    with pytest.raises(GuardViolation):
        open(str(link / "x"))
    with pytest.raises(GuardViolation):
        os.makedirs(str(link / "y"))
    forbid_real_home_io.clear()
    assert not os.path.lexists(os.path.join(os.path.dirname(PROBE), "y"))


def test_relative_dir_fd_calls_pass_through_and_tmp_io_works(tmp_path):
    (tmp_path / "f.txt").write_text("hello")
    fd = os.open(str(tmp_path), os.O_RDONLY)
    try:
        inner = os.open("f.txt", os.O_RDONLY, dir_fd=fd)
        assert os.read(inner, 5) == b"hello"
        os.close(inner)
    finally:
        os.close(fd)
    nested = tmp_path / "tree" / "deep"
    nested.mkdir(parents=True)
    (nested / "x").write_text("1")
    shutil.rmtree(str(tmp_path / "tree"))
    assert not (tmp_path / "tree").exists()


def test_protected_roots_include_the_shell_and_launch_locations():
    for sub in (".zshrc", ".zprofile", ".local/bin", "Library/LaunchAgents"):
        assert os.path.join(h.REAL_HOME, sub) in h.FORBIDDEN_ROOTS


# ------------------------------------------------------------------ subprocess recorder self-tests

def test_recorder_rejects_destructive_argv_and_records_the_rest(deck_home, subprocess_calls):
    zellij, osa = h.FAKE_BINS["zellij"], h.FAKE_BINS["osascript"]
    for argv in ([zellij, "kill-session", "x"], [zellij, "delete-all-sessions"],
                 [osa, "-e", 'tell application "Stream Deck" to QUIT APP'],
                 ["/usr/bin/pgrep", "-x", "Stream Deck"]):
        with pytest.raises(GuardViolation):
            subprocess.run(argv, capture_output=True)
    h.VIOLATIONS.clear()
    ok = [osa, "-e", 'display dialog "quite"']
    assert subprocess.run(ok, capture_output=True).returncode == 0
    assert ok in subprocess_calls
    assert [zellij, "kill-session", "x"] in subprocess_calls


def test_open_of_a_profile_outside_tmp_is_refused(deck_home):
    fake_open = h.FAKE_BINS["open"]
    with pytest.raises(GuardViolation):
        subprocess.run([fake_open, "/Users/someone/Quoin Live Agents.streamDeckProfile"], capture_output=True)
    h.VIOLATIONS.clear()


def test_forbid_real_seams_is_active():
    ops.assert_seam_tripwire()


# ------------------------------------------------------------------ fixtures

def test_run_hook_commands_refuses_the_system_python(lifecycle_home):
    lc.make_prototype(lifecycle_home)
    with pytest.raises(AssertionError, match="/usr/bin/python3"):
        ops.run_hook_commands(lifecycle_home)


def test_prototype_with_fixture_python_runs_inert(lifecycle_home):
    python = lc.fixture_python(lifecycle_home)
    lc.make_prototype(lifecycle_home, python=python)
    results = ops.run_hook_commands(lifecycle_home)
    assert results and all(python in cmd for cmd, *_ in results if "monitor.py" in cmd)
    ops.assert_hooks_inert(results)


def test_make_prototype_plugin_manifest_switch(lifecycle_home):
    lc.make_prototype(lifecycle_home, plugin_manifest=True)
    data = json.loads((lifecycle_home.plugin_dir / "manifest.json").read_text())
    assert data["UUID"] == payload_build.PLUGIN_UUID and data["Actions"][0]["UUID"] == payload_build.ACTION_UUID


def test_make_prototype_default_layout_has_no_plugin_manifest(lifecycle_home):
    lc.make_prototype(lifecycle_home)
    assert not (lifecycle_home.plugin_dir / "manifest.json").exists()


def test_make_installed_writes_a_json_plugin_manifest_and_takes_versions(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    data = json.loads((lifecycle_home.plugin_dir / "manifest.json").read_text())
    assert data["UUID"] == payload_build.PLUGIN_UUID


def test_make_installed_older_version(lifecycle_home):
    inst = lc.make_installed(lifecycle_home, component_version="0.9.0", version_dir_name="0.9.0")
    assert inst.manifest["component_version"] == "0.9.0"
    assert os.readlink(str(lifecycle_home.runtime / "current")) == "versions/0.9.0"
    assert inst.manifest["version_dir"].endswith("versions/0.9.0")


def test_fake_ctx_clock_follows_now(tmp_path):
    fake = lc.FakeCtx(tmp_path, now=ops.LATER)
    assert fake.ctx.now() == "2026-10-13T13:00:00Z"
    assert lc.FakeCtx(tmp_path).ctx.now() == "2026-10-13T12:00:00Z"
