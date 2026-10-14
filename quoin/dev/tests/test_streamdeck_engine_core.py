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
from quoin.streamdeck import engine, hooks_merge, jsonfile, macos, manifest, payload_build
import quoin.streamdeck as sd

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


# ------------------------------------------------------------------ helpers

LINUX = macos.PlatformInfo("linux", "", "x86_64", False)


def write_marker(lh, data):
    lh.runtime.mkdir(parents=True, exist_ok=True)
    with open(str(lh.runtime / "operation.json"), "w") as handle:
        if isinstance(data, str):
            handle.write(data)
        else:
            json.dump(data, handle)


def marker_bytes(lh):
    return (lh.runtime / "operation.json").read_bytes()


def staging_marker(**over):
    data = {"op": "install", "phase": "staging", "adoption": False, "steps_done": [], "rollback_dir": None,
            "draft": {"hooks": []}, "carried_hooks": [], "stale": {},
            "previous": {"current_target": None, "first_install": True, "existing": [], "created": [],
                         "created_paths": [], "plugin_aside": None}}
    data.update(over)
    return data


def committed_marker(**over):
    data = {"op": "install", "phase": "committed", "adoption": False, "steps_done": ["commit"],
            "draft": {"hooks": [], "mode": "active", "binaries": {}, "wired": True, "legacy_owned_events": {}},
            "carried_hooks": [], "stale": {}, "previous": {}}
    data.update(over)
    return data


def hook_record(event, created, runtime="claude", path="/s/settings.json", command="/rt/bin/quoin-deck-hook claude"):
    return {"runtime": runtime, "settings_path": path, "event": event, "command": command, "timeout": 3,
            "created_event_key": created}


def state_of(env, op="install"):
    return engine.read_state(env, op)


# ------------------------------------------------------------------ platform gate and preflight

@pytest.mark.parametrize("op", ["install", "upgrade"])
def test_non_macos_is_refused_with_code_2_and_writes_nothing(lifecycle_home, op):
    env = ops.make_env(lifecycle_home, platform=LINUX)
    before = lc.tree_snapshot(lifecycle_home.root)
    with pytest.raises(engine.OperationError) as exc:
        getattr(engine, op)(env)
    assert exc.value.code == 2 and "macOS only" in exc.value.message
    assert lc.tree_snapshot(lifecycle_home.root) == before


def bad_settings(lh, which, how):
    path = lh.claude_settings if which == "claude" else lh.codex_home / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if how == "malformed":
        path.write_text("{not json")
    elif how == "hooks-list":
        path.write_text('{"hooks": []}')
    elif how == "directory":
        path.mkdir()
    elif how == "unreadable":
        path.write_text("{}")
    return path


@pytest.mark.parametrize("which", ["claude", "codex"])
@pytest.mark.parametrize("how", ["malformed", "hooks-list", "directory", "unreadable"])
def test_unusable_settings_refuse_before_any_write(lifecycle_home, which, how):
    lh = lifecycle_home
    other = lh.codex_home / "hooks.json" if which == "claude" else lh.claude_settings
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("{}")
    bad = bad_settings(lh, which, how)
    env = ops.make_env(lh)
    before = lc.tree_snapshot(lh.home)
    if how == "unreadable":
        original_mode = bad.stat().st_mode & 0o777
        bad.chmod(0)
    try:
        with pytest.raises(engine.OperationError) as exc:
            engine.install(env)
        assert str(bad) in exc.value.message and "left unchanged" in exc.value.message
        assert not lh.runtime.exists()
    finally:
        if how == "unreadable":
            bad.chmod(original_mode)
    assert lc.tree_snapshot(lh.home) == before


def test_downgrade_is_refused_unless_allowed(lifecycle_home):
    lh = lifecycle_home
    lc.make_installed(lh, component_version="9.0.0", version_dir_name="9.0.0")
    env = ops.make_env(lh)
    before = lc.tree_snapshot(lh.root)
    with pytest.raises(engine.OperationError, match="newer"):
        engine.install(env)
    assert lc.tree_snapshot(lh.root) == before
    result = engine.install(ops.make_env(lh, allow_downgrade=True))
    assert result.manifest["component_version"] == sd.COMPONENT_VERSION


def test_upgrade_needs_an_install(lifecycle_home):
    env = ops.make_env(lifecycle_home)
    before = lc.tree_snapshot(lifecycle_home.root)
    with pytest.raises(engine.OperationError, match="not installed; run `quoin streamdeck install`"):
        engine.upgrade(env)
    assert lc.tree_snapshot(lifecycle_home.root) == before


def test_upgrade_over_tombstones_only_counts_as_not_installed(lifecycle_home):
    from quoin.streamdeck import shims
    lh = lifecycle_home
    (lh.runtime / "bin").mkdir(parents=True)
    (lh.runtime / "bin" / "quoin-deck-hook").write_text(shims.render_tombstone("sh"))
    (lh.runtime / "uninstalled.json").write_text(json.dumps({"tombstones": [str(lh.runtime / "bin" / "quoin-deck-hook")]}))
    with pytest.raises(engine.OperationError, match="not installed"):
        engine.upgrade(ops.make_env(lh))


def test_a_busy_lock_refuses_without_writing_a_marker(lifecycle_home):
    import fcntl
    lh = lifecycle_home
    lh.runtime.mkdir(parents=True)
    fd = os.open(str(lh.runtime / ".install.lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(engine.OperationError, match="another quoin streamdeck operation is running"):
            engine.install(ops.make_env(lh))
    finally:
        os.close(fd)
    assert not (lh.runtime / "operation.json").exists()


def test_stale_staging_is_swept_when_no_marker(lifecycle_home):
    lh = lifecycle_home
    lc.make_installed(lh)
    (lh.runtime / "staging" / "leftover").mkdir(parents=True)
    (lh.runtime / "versions" / "9.9.9-abc.staging-xyz").mkdir()
    engine.install(ops.make_env(lh))
    assert not (lh.runtime / "staging" / "leftover").exists()
    assert not (lh.runtime / "versions" / "9.9.9-abc.staging-xyz").exists()


# ------------------------------------------------------------------ marker model

def test_marker_writer_requires_op_and_phase(tmp_path):
    store = engine.MarkerStore(str(tmp_path / "operation.json"))
    with pytest.raises(TypeError):
        engine.open_marker(store, phase="committed", completing=False, carry=None, prev_hooks=[], adoption=False,
                           draft_now={})
    with pytest.raises(TypeError):
        engine.open_marker(store, op="install", completing=False, carry=None, prev_hooks=[], adoption=False,
                           draft_now={})
    assert not (tmp_path / "operation.json").exists()


def test_open_marker_fresh_run_is_staging_with_this_runs_draft(tmp_path):
    store = engine.MarkerStore(str(tmp_path / "operation.json"))
    data = engine.open_marker(store, op="install", phase="staging", completing=False, carry=engine.Carry(),
                              prev_hooks=[], adoption=False, previous={"current_target": None},
                              draft_now={"binaries": {"python": {"path": "/p"}}, "mode": "active",
                                         "legacy_owned_events": {}, "version_dir": "/v"})
    assert data["phase"] == "staging" and data["draft"]["binaries"] == {"python": {"path": "/p"}}
    assert data["draft"]["mode"] == "active" and data["draft"]["wired"] is False
    assert json.loads((tmp_path / "operation.json").read_text()) == data


def test_open_marker_completing_run_inherits_the_predecessor(tmp_path):
    predecessor = committed_marker(
        rollback_dir="/rb", runtimes=None, adoption=True,
        draft={"hooks": [hook_record("Stop", True)], "legacy_owned_events": {"claude": ["Stop"]}, "mode": "active",
               "binaries": {"node": {"mode": "external", "path": "/n"}}, "wired": True},
        carried_hooks=[hook_record("SessionStart", True)],
        stale={"rollback_dirs": ["/old-rb"], "plugin_asides": ["/aside"], "version_dir_asides": []},
        previous={"plugin_aside": {"path": "/pa", "kept": False}, "version_dir_aside": "/va"})
    carry = engine.carry_from_marker(predecessor, [hook_record("Stop", False), hook_record("Notification", False)])
    assert carry.completing and carry.adoption is True
    store = engine.MarkerStore(str(tmp_path / "operation.json"))
    data = engine.open_marker(store, op="install", phase="committed", completing=True, carry=carry,
                              prev_hooks=carry.prev_hooks, adoption=carry.adoption,
                              draft_now={"binaries": {}, "mode": None, "legacy_owned_events": {"claude": []}})
    assert data["phase"] == "committed"
    by_event = {r["event"]: r["created_event_key"] for r in data["carried_hooks"]}
    assert by_event == {"Stop": True, "SessionStart": True, "Notification": False}
    assert sorted(data["stale"]["rollback_dirs"]) == ["/old-rb", "/rb"]
    assert sorted(data["stale"]["plugin_asides"]) == ["/aside", "/pa"] and data["stale"]["version_dir_asides"] == ["/va"]
    assert data["draft"]["legacy_owned_events"] == {"claude": ["Stop"]}
    assert data["draft"]["binaries"] == {"node": {"mode": "external", "path": "/n"}}
    assert data["draft"]["mode"] == "active" and data["draft"]["wired"] is True
    assert data["draft"]["hooks"] == [] and data["adoption"] is True


def test_completing_marker_merges_forward_and_honours_deactivation(tmp_path):
    predecessor = committed_marker(draft={"hooks": [], "mode": "dormant", "binaries": {"python": {"path": "/old"}},
                                          "wired": False, "legacy_owned_events": {}})
    carry = engine.carry_from_marker(predecessor, [])
    store = engine.MarkerStore(str(tmp_path / "operation.json"))
    data = engine.open_marker(store, op="install", phase="committed", completing=True, carry=carry, prev_hooks=[],
                              adoption=False, draft_now={"binaries": {"node": {"mode": "bundled", "path": None}},
                                                         "mode": "active", "legacy_owned_events": {}})
    assert data["draft"]["mode"] == "active"
    assert data["draft"]["binaries"] == {"python": {"path": "/old"}, "node": {"mode": "bundled", "path": None}}
    deactivated = engine.open_marker(store, op="install", phase="committed", completing=True, carry=carry,
                                     prev_hooks=[], adoption=False,
                                     draft_now={"mode": "dormant", "deactivated": True, "binaries": {}})
    assert deactivated["draft"]["mode"] == "dormant" and deactivated["draft"]["deactivated"] is True


def test_a_completing_run_is_never_written_as_staging(tmp_path):
    store = engine.MarkerStore(str(tmp_path / "operation.json"))
    with pytest.raises(ValueError):
        engine.open_marker(store, op="install", phase="staging", completing=True, carry=engine.Carry(),
                           prev_hooks=[], adoption=False, draft_now={})


@pytest.mark.parametrize("marker", [
    committed_marker(phase="weird"),
    {k: v for k, v in committed_marker().items() if k != "op"},
    committed_marker(op=5),
    {k: v for k, v in staging_marker().items() if k != "previous"},
    committed_marker(draft=[]),
    committed_marker(steps_done="x"),
    committed_marker(runtimes="codex"),
    {"phase": "unreadable"},
])
def test_mis_shaped_markers_are_treated_as_unreadable(lifecycle_home, monkeypatch, marker):
    lh = lifecycle_home
    lc.make_installed(lh)
    write_marker(lh, marker)
    calls = []
    monkeypatch.setattr(engine, "rollback", lambda *a, **k: calls.append(a))
    run = engine._Run(ops.make_env(lh), "install")
    state = state_of(run.env)
    assert state.marker_kind == "unreadable"
    carry = run._recover(state)
    assert calls == [] and carry.completing and "created event keys unknown" in run.lines[-1]
    assert {r["event"] for r in carry.prev_hooks} == set(hooks_merge.EVENTS["claude"]) | set(hooks_merge.EVENTS["codex"])


# ------------------------------------------------------------------ recovery

def test_staging_marker_is_rolled_back_and_cleared(lifecycle_home):
    lh = lifecycle_home
    lc.make_installed(lh)
    aside = lh.plugins / ".quoin-plugin-old-abc"
    os.rename(str(lh.plugin_dir), str(aside))
    lh.plugin_dir.mkdir()
    (lh.plugin_dir / "marker-new").write_text("new")
    os.unlink(str(lh.runtime / "current"))
    os.symlink("versions/temp", str(lh.runtime / "current"))
    write_marker(lh, staging_marker(previous=staging_marker()["previous"] | {
        "current_target": "versions/1.0.0", "plugin_aside": {"path": str(aside), "kept": False}}))
    run = engine._Run(ops.make_env(lh), "install")
    carry = run._recover(state_of(run.env))
    assert not carry.completing and not (lh.runtime / "operation.json").exists()
    assert os.readlink(str(lh.runtime / "current")) == "versions/1.0.0"
    assert (lh.plugin_dir / "manifest.json").exists() and not (lh.plugin_dir / "marker-new").exists()
    assert not aside.exists() and "rolled back an interrupted operation" in run.lines


def test_committed_marker_unions_hook_records_with_or_ed_flags(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    inst = lc.make_installed(lh)
    manifest_rec = {(r["runtime"], r["event"]): r for r in inst.manifest["hooks"]}
    stop = manifest_rec[("claude", "Stop")]
    write_marker(lh, committed_marker(
        draft={"hooks": [dict(stop, created_event_key=True)], "mode": "active", "binaries": {}, "wired": True},
        carried_hooks=[hook_record("Weird", True, runtime="claude")]))
    calls = []
    monkeypatch.setattr(engine, "rollback", lambda *a, **k: calls.append(a))
    run = engine._Run(ops.make_env(lh), "install")
    carry = run._recover(state_of(run.env))
    assert calls == [] and carry.completing and "completing an interrupted operation" in run.lines
    flags = {(r["runtime"], r["event"]): r["created_event_key"] for r in carry.prev_hooks}
    assert flags[("claude", "Stop")] is True and flags[("claude", "Weird")] is True


@pytest.mark.parametrize("phase", ["staging", "committed"])
def test_uninstall_markers_are_never_rolled_back(lifecycle_home, monkeypatch, phase):
    lh = lifecycle_home
    lc.make_installed(lh)
    write_marker(lh, {"op": "uninstall", "phase": phase, "steps_done": [], "runtimes": None,
                      "draft": {"hooks": [hook_record("Stop", True)]}, "carried_hooks": [], "previous": {}})
    calls = []
    monkeypatch.setattr(engine, "rollback", lambda *a, **k: calls.append(a))
    before = lc.tree_snapshot(lh.runtime)
    run = engine._Run(ops.make_env(lh), "install")
    state = state_of(run.env)
    assert state.marker_kind == "uninstall"
    carry = run._recover(state)
    assert calls == [] and carry.completing
    assert any(r["event"] == "Stop" and r["created_event_key"] for r in carry.prev_hooks)
    assert lc.tree_snapshot(lh.runtime) == before


def test_rollback_refuses_an_uninstall_marker(lifecycle_home):
    with pytest.raises(RuntimeError):
        engine.rollback(lifecycle_home.resolve(), {"op": "uninstall", "phase": "committed"})


def test_dry_run_with_each_marker_kind_reports_and_changes_nothing(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    lc.make_installed(lh)
    expectations = [
        (staging_marker(), "dry run: would roll back an interrupted operation"),
        (committed_marker(), "dry run: would complete an interrupted operation"),
        ({"op": "uninstall", "phase": "committed", "steps_done": [], "previous": {}}, "would complete"),
        ("{garbage", "dry run: would complete an interrupted operation"),
    ]
    calls = []
    monkeypatch.setattr(engine._Run, "_recover", lambda *a, **k: calls.append(1))
    for marker, line in expectations:
        write_marker(lh, marker)
        env = ops.make_env(lh, dry_run=True)
        before = lc.tree_snapshot(lh.root)
        result = engine.install(env)
        assert any(line in text for text in result.lines)
        assert lc.tree_snapshot(lh.root) == before
    assert calls == [] and not (lh.runtime / ".install.lock").exists()


def test_upgrade_refuses_over_an_interrupted_install_with_no_manifest(lifecycle_home):
    lh = lifecycle_home
    write_marker(lh, committed_marker())
    before = marker_bytes(lh)
    with pytest.raises(engine.OperationError, match="an interrupted install is pending; run `quoin streamdeck install`"):
        engine.upgrade(ops.make_env(lh))
    assert marker_bytes(lh) == before and not (lh.runtime / ".install.lock").exists()


def test_upgrade_refuses_over_an_interrupted_uninstall(lifecycle_home):
    lh = lifecycle_home
    write_marker(lh, {"op": "uninstall", "phase": "committed", "steps_done": [], "runtimes": None, "previous": {}})
    with pytest.raises(engine.OperationError, match="interrupted uninstall is pending"):
        engine.upgrade(ops.make_env(lh))


# ------------------------------------------------------------------ route

def test_route_is_taken_from_the_marker_not_from_detection(lifecycle_home):
    lh = lifecycle_home
    lc.make_installed(lh)
    os.unlink(str(lh.runtime / "install-manifest.json"))  # our own plugin dir and `current`, no manifest
    paths = lh.resolve()
    facts = engine.collect_facts(paths)
    assert facts.detection == "prototype" and facts.own_evidence
    assert engine.route_adoption(facts, None) is True
    assert engine.route_adoption(facts, committed_marker(adoption=False)) is False
    assert engine.route_adoption(facts, committed_marker(adoption=True)) is True
    assert engine.route_adoption(facts, {"op": "uninstall", "phase": "committed", "steps_done": [], "adoption": False}) is False
    assert engine.route_adoption(facts, {"op": "uninstall", "phase": "committed", "steps_done": [], "adoption": True}) is True
    assert engine.route_adoption(facts, {"phase": "unreadable"}) is False  # own `current` wins over detection
    os.unlink(str(lh.runtime / "current"))
    os.unlink(str(lh.runtime / "bin" / "quoin-deck-hook"))
    facts = engine.collect_facts(paths)
    assert not facts.own_evidence and engine.route_adoption(facts, {"phase": "unreadable"}) is True


def test_adoption_pending_manifest_always_routes_to_adoption():
    facts = engine.Facts("adoption-pending", "managed", True)
    assert engine.route_adoption(facts, None) is True and engine.route_adoption(facts, committed_marker()) is True


# ------------------------------------------------------------------ partial uninstall preflight

@pytest.mark.parametrize("marker,fragment", [
    (committed_marker(), "an interrupted install is pending; run `quoin streamdeck install` to complete it"),
    ({"op": "uninstall", "phase": "committed", "steps_done": [], "runtimes": None, "previous": {}},
     "an interrupted uninstall is pending; run `quoin streamdeck uninstall` to complete it"),
    ({"op": "uninstall", "phase": "committed", "steps_done": [], "runtimes": ["claude"], "previous": {}},
     "run `quoin streamdeck uninstall --runtimes claude`"),
    ("{garbage", "an interrupted operation is pending"),
])
def test_partial_uninstall_refuses_before_the_lock_over_a_marker_it_does_not_cover(lifecycle_home, marker, fragment):
    lh = lifecycle_home
    lc.make_installed(lh)
    write_marker(lh, marker)
    env = ops.make_env(lh, runtimes=("codex",))
    before = lc.tree_snapshot(lh.root)
    with pytest.raises(engine.OperationError) as exc:
        engine.uninstall(env)
    assert fragment in exc.value.message
    assert lc.tree_snapshot(lh.root) == before and not (lh.runtime / ".install.lock").exists()


def test_partial_uninstall_may_complete_its_own_marker(lifecycle_home):
    marker = {"op": "uninstall", "phase": "committed", "steps_done": [], "runtimes": ["codex"], "previous": {}}
    assert engine.check_partial_uninstall(marker, ("codex",)) is None
    assert engine.check_partial_uninstall(marker, ("claude", "codex")) is None
    assert "uninstall --runtimes codex" in engine.check_partial_uninstall(marker, ("claude",))
    assert engine.check_partial_uninstall(None, ("codex",)) is None
    assert engine.check_partial_uninstall(staging_marker(), ("codex",)) is None
