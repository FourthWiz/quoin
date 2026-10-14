"""Schema chain, legacy harvest and the state-store migrations."""
import fcntl
import json
import os
import stat
import time

import pytest

import _streamdeck_helpers as h
import _streamdeck_lifecycle as lc
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home)
from quoin.streamdeck import hooks_merge, jsonfile, manifest, migrations as mg

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")


# ------------------------------------------------------------------ schema chain

def test_schema_chain_runs_once_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(manifest, "SCHEMA", 2)
    calls = []

    def one_to_two(data):
        calls.append(1)
        data["added"] = True
        return data

    monkeypatch.setattr(mg, "SCHEMA_MIGRATIONS", [(1, 2, one_to_two)])
    old = {"schema": 1, "x": 1}
    new = mg.migrate_manifest(old)
    assert new["schema"] == 2 and new["added"] is True and old == {"schema": 1, "x": 1}
    assert mg.migrate_manifest(new) == new and calls == [1]


def test_schema_chain_gap_and_newer_are_errors(monkeypatch):
    monkeypatch.setattr(manifest, "SCHEMA", 3)
    monkeypatch.setattr(mg, "SCHEMA_MIGRATIONS", [(2, 3, lambda d: d)])
    with pytest.raises(mg.MigrationError, match="from schema 1"):
        mg.migrate_manifest({"schema": 1})
    with pytest.raises(mg.MigrationError, match="newer"):
        mg.migrate_manifest({"schema": 4})
    with pytest.raises(mg.MigrationError):
        mg.migrate_manifest({"schema": "1"})
    with pytest.raises(mg.MigrationError):
        mg.migrate_manifest([])


def test_current_schema_needs_no_chain():
    data = {"schema": manifest.SCHEMA, "a": 1}
    assert mg.migrate_manifest(data) == data and mg.ADOPTION_FROM == 0


# ------------------------------------------------------------------ legacy harvest

def test_harvest_from_the_exact_prototype_command_form(lifecycle_home):
    lh = lifecycle_home
    python = lc.fixture_python(lh)
    lc.make_prototype(lh, python=python)
    paths = lh.resolve()
    claude, _ = jsonfile.read(paths.claude_settings)
    codex, _ = jsonfile.read(paths.codex_hooks)
    assert " " in paths.runtime
    assert mg.harvest_legacy(paths, claude, codex) == {"python": python, "node": "/opt/x/bin/node"}


@pytest.mark.parametrize("line,expected", [
    ("exec '/opt/x y/node' plugin.js \"$@\"", "/opt/x y/node"),
    ("exec /opt/x/node plugin.js \"$@\"", "/opt/x/node"),
    ("echo nothing", None),
])
def test_harvest_node_from_launch_sh(lifecycle_home, line, expected):
    lh = lifecycle_home
    lc.make_prototype(lh, python=lc.fixture_python(lh))
    (lh.plugin_dir / "launch.sh").write_text("#!/bin/sh\n%s\n" % line)
    paths = lh.resolve()
    assert mg.harvest_legacy(paths, {}, {})["node"] == expected


def test_harvest_without_anything(lifecycle_home):
    assert mg.harvest_legacy(lifecycle_home.resolve(), {}, {}) == {"python": None, "node": None}


def test_legacy_owned_per_runtime(lifecycle_home):
    lh = lifecycle_home
    lc.make_prototype(lh)
    paths = lh.resolve()
    claude, _ = jsonfile.read(paths.claude_settings)
    codex, _ = jsonfile.read(paths.codex_hooks)
    claude["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "/x/foreign"}]})
    owned = mg.legacy_owned(claude, codex, ("QuoinStreamDeck",))
    assert "Stop" not in owned["claude"] and "SessionStart" in owned["claude"]
    assert owned["codex"] == list(hooks_merge.EVENTS["codex"])
    assert mg.legacy_owned({}, {}, ("QuoinStreamDeck",)) == {"claude": [], "codex": []}


# ------------------------------------------------------------------ codex detection

CODEX_LEGACY = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 '/x/QuoinStreamDeck/monitor.py' codex"}]}]}}


@pytest.mark.parametrize("signal", ["which", "env", "dir", "hooks", "manifest", "none"])
def test_codex_detection_matrix(lifecycle_home, signal):
    paths = lifecycle_home.resolve()
    which = (lambda name: "/opt/codex" if signal == "which" else None)
    environ = {"CODEX_HOME": "/x"} if signal == "env" else {}
    if signal == "dir":
        os.makedirs(paths.codex_home)
    data = CODEX_LEGACY if signal == "hooks" else {}
    manifest_data = {"hooks": [{"runtime": "codex"}]} if signal == "manifest" else None
    found, reason = mg.codex_detected(paths, environ, which, data, manifest_data)
    assert found is (signal != "none") and reason


def test_relative_codex_on_path_does_not_count(lifecycle_home):
    found, _ = mg.codex_detected(lifecycle_home.resolve(), {}, lambda n: "codex", {}, None)
    assert found is False


# ------------------------------------------------------------------ state dirs

def test_ensure_state_dirs_creates_private_dirs_and_is_idempotent(lifecycle_home):
    paths = lifecycle_home.resolve()
    assert mg.ensure_state_dirs(paths) == []
    for directory in (paths.state_root, paths.state, paths.agents_dir):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert mg.ensure_state_dirs(paths) == []


def test_loose_state_dir_is_tightened_and_reported(lifecycle_home):
    paths = lifecycle_home.resolve()
    os.makedirs(paths.state)
    os.chmod(paths.state, 0o755)
    notes = mg.ensure_state_dirs(paths)
    assert len(notes) == 1 and paths.state in notes[0]
    assert stat.S_IMODE(os.stat(paths.state).st_mode) == 0o700


def test_a_directory_owned_by_someone_else_is_refused(lifecycle_home, monkeypatch):
    paths = lifecycle_home.resolve()
    os.makedirs(paths.state)
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        result = real_stat(path, *a, **kw)
        if os.fspath(path) == paths.state:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", fake_stat)
    with pytest.raises(mg.MigrationError, match=paths.state):
        mg.ensure_state_dirs(paths)


# ------------------------------------------------------------------ agents mover

def put(directory, name, **fields):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, name), "w") as handle:
        json.dump(fields, handle)


def legacy_dir(paths):
    return os.path.join(paths.state_root, "agents")


def read(path):
    with open(path) as handle:
        return json.load(handle)


@pytest.fixture
def paths(lifecycle_home):
    p = lifecycle_home.resolve()
    mg.ensure_state_dirs(p)
    return p


def test_merge_newer_wins_in_both_directions(paths):
    put(legacy_dir(paths), "old.json", id="old", updated=5.0)
    put(paths.agents_dir, "old.json", id="old-new", updated=9.0)
    put(legacy_dir(paths), "fresh.json", id="fresh", updated=9.0)
    put(paths.agents_dir, "fresh.json", id="fresh-new", updated=5.0)
    put(legacy_dir(paths), "tie.json", id="tie-legacy", updated=7.0)
    put(paths.agents_dir, "tie.json", id="tie-new", updated=7.0)
    put(legacy_dir(paths), "solo.json", id="solo", updated=1.0)
    report = mg.merge_agents(paths)
    assert read(os.path.join(paths.agents_dir, "old.json"))["id"] == "old-new"
    assert read(os.path.join(paths.agents_dir, "fresh.json"))["id"] == "fresh"
    assert read(os.path.join(paths.agents_dir, "tie.json"))["id"] == "tie-new"
    assert read(os.path.join(paths.agents_dir, "solo.json"))["id"] == "solo"
    assert sorted(report["moved"]) == ["fresh.json", "solo.json"]
    assert sorted(report["dropped"]) == ["old.json", "tie.json"]
    assert os.listdir(legacy_dir(paths)) == []
    for name in report["moved"]:
        assert stat.S_IMODE(os.stat(os.path.join(paths.agents_dir, name)).st_mode) == 0o600


def test_socket_inferred_is_added_only_when_absent(paths):
    put(legacy_dir(paths), "a.json", id="a", updated=1.0)
    put(legacy_dir(paths), "b.json", id="b", updated=1.0, socket_inferred=False, socket="/s")
    mg.merge_agents(paths)
    assert read(os.path.join(paths.agents_dir, "a.json"))["socket_inferred"] is True
    b = read(os.path.join(paths.agents_dir, "b.json"))
    assert b["socket_inferred"] is False and b["socket"] == "/s"


def test_unparsable_source_is_left_and_an_unparsable_target_is_replaced(paths):
    os.makedirs(legacy_dir(paths), exist_ok=True)
    with open(os.path.join(legacy_dir(paths), "junk.json"), "w") as handle:
        handle.write("{not json")
    put(legacy_dir(paths), "heal.json", id="heal", updated=1.0)
    with open(os.path.join(paths.agents_dir, "heal.json"), "w") as handle:
        handle.write("{broken")
    report = mg.merge_agents(paths)
    assert report["left"] == ["junk.json"] and os.path.exists(os.path.join(legacy_dir(paths), "junk.json"))
    assert read(os.path.join(paths.agents_dir, "heal.json"))["id"] == "heal"


def test_a_crash_between_copy_and_delete_converges_on_rerun(paths, monkeypatch):
    put(legacy_dir(paths), "a.json", id="a", updated=1.0)
    real_unlink = os.unlink
    state = {"armed": True}

    def flaky(path, *a, **kw):
        if state["armed"] and os.fspath(path).startswith(legacy_dir(paths)):
            state["armed"] = False
            raise OSError("simulated crash")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(mg.os, "unlink", flaky)
    with pytest.raises(OSError):
        mg.merge_agents(paths)
    assert os.path.exists(os.path.join(legacy_dir(paths), "a.json"))
    assert os.path.exists(os.path.join(paths.agents_dir, "a.json"))
    mg.merge_agents(paths)
    assert os.listdir(legacy_dir(paths)) == []
    assert [n for n in os.listdir(paths.agents_dir) if n.endswith(".json")] == ["a.json"]
    assert read(os.path.join(paths.agents_dir, "a.json"))["id"] == "a"


def test_a_held_lock_defers_the_record_to_the_next_run(paths, monkeypatch):
    monkeypatch.setattr(mg, "LOCK_WAIT_S", 0.2)
    put(legacy_dir(paths), "a.json", id="a", updated=1.0)
    lock = os.path.join(paths.state_root, "monitor.lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        report = mg.merge_agents(paths)
    finally:
        os.close(fd)
    assert report["skipped"] == ["a.json"] and os.path.exists(os.path.join(legacy_dir(paths), "a.json"))
    assert mg.merge_agents(paths)["moved"] == ["a.json"]


# ------------------------------------------------------------------ reverse sweep

def test_reverse_sweep_copies_newer_and_unmatched_records(paths):
    put(legacy_dir(paths), "older.json", id="older", updated=1.0)
    put(paths.agents_dir, "older.json", id="older", updated=5.0, via="managed", socket_inferred=False)
    put(legacy_dir(paths), "newer.json", id="newer-legacy", updated=9.0)
    put(paths.agents_dir, "newer.json", id="newer-new", updated=2.0)
    put(paths.agents_dir, "unmatched.json", id="unmatched", updated=3.0)
    report = mg.reverse_sweep(paths)
    assert sorted(report["copied"]) == ["older.json", "unmatched.json"]
    assert read(os.path.join(legacy_dir(paths), "older.json"))["updated"] == 5.0
    assert read(os.path.join(legacy_dir(paths), "newer.json"))["id"] == "newer-legacy"
    assert sorted(os.listdir(paths.agents_dir)) == ["newer.json", "older.json", "unmatched.json"]


def test_reverse_swept_records_are_accepted_by_the_frozen_prototype(lifecycle_home, deck_home, monkeypatch):
    frozen = h.load_frozen(monkeypatch, deck_home, {"list-sessions --no-formatting": "s [Created 1m ago]"})
    p = lifecycle_home.resolve({"state_root": str(frozen.legacy)})
    mg.ensure_state_dirs(p)
    put(p.agents_dir, "r.json", id="r", kind="claude", session="s", pane="3", socket=str(deck_home.zsock),
        socket_inferred=False, status="working", pending={}, event="Stop", created=1.0, updated=time.time(),
        pid=4242, process_started="Tue Oct 13 12:16:19 2026", via="managed", project="/p")
    assert mg.reverse_sweep(p)["copied"] == ["r.json"]
    monkeypatch.setattr(frozen.dashboard, "panes", lambda session, socket=None: [{"id": 3, "is_plugin": False,
                                                                                   "exited": False, "title": "x"}])
    monkeypatch.setattr(frozen.dashboard.monitor, "process_identity", lambda pid: "Tue Oct 13 12:16:19 2026")
    entries = frozen.dashboard.snapshot()
    assert [e["id"] for e in entries] == ["r"]


def test_reverse_sweep_skips_when_the_prototype_monitor_holds_its_lock(paths, monkeypatch):
    monkeypatch.setattr(mg, "LOCK_WAIT_S", 0.3)
    put(paths.agents_dir, "x.json", id="x", updated=3.0)
    fd = os.open(os.path.join(paths.state_root, "monitor.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        report = mg.reverse_sweep(paths)
        assert time.monotonic() - start < 2.5
    finally:
        os.close(fd)
    assert report == {"copied": [], "skipped": ["x.json"]}
    assert not os.path.exists(os.path.join(legacy_dir(paths), "x.json"))


# ------------------------------------------------------------------ agents listing

def test_collect_agents_reads_both_layouts(paths):
    put(paths.agents_dir, "a.json", kind="claude", session="s1", pane="3", via="managed")
    put(legacy_dir(paths), "b.json", kind="codex", session="s2", pane="4")
    put(legacy_dir(paths), "a.json", kind="claude", session="shadowed", pane="9")
    found = {(a["session"], a["via"]) for a in mg.collect_agents(paths)}
    assert found == {("s1", "managed"), ("s2", "legacy")}


# ------------------------------------------------------------------ surfaces and logs

def write_config(paths, **fields):
    os.makedirs(os.path.dirname(paths.config), exist_ok=True)
    with open(paths.config, "w") as handle:
        json.dump(fields, handle)


def test_surfaces_split_writes_the_new_file_before_touching_config(paths, monkeypatch):
    write_config(paths, project="/p", session="s", projects={"s": "/p"}, surfaces={"s": "surf-1", "t": "surf-2"})
    put(paths.state, "surfaces.json", **{"/sock\ts": "kept"})
    real_update = jsonfile.update

    def failing(*a, **kw):
        raise OSError("simulated crash between the two writes")

    monkeypatch.setattr(jsonfile, "update", failing)
    with pytest.raises(OSError):
        mg.split_surfaces(paths, "/sock")
    surfaces = read(os.path.join(paths.state, "surfaces.json"))
    assert surfaces == {"/sock\ts": "kept", "/sock\tt": "surf-2"}
    assert "surfaces" in read(paths.config)
    monkeypatch.setattr(jsonfile, "update", real_update)
    mg.split_surfaces(paths, "/sock")
    config = read(paths.config)
    assert "surfaces" not in config and config["project"] == "/p" and config["projects"] == {"s": "/p"}
    assert read(os.path.join(paths.state, "surfaces.json")) == surfaces
    assert mg.split_surfaces(paths, "/sock")["moved"] == 0


def test_surfaces_split_tolerates_a_malformed_config(paths):
    os.makedirs(os.path.dirname(paths.config), exist_ok=True)
    with open(paths.config, "w") as handle:
        handle.write("{oops")
    assert mg.split_surfaces(paths, "/sock")["skipped"]
    assert open(paths.config).read() == "{oops"


def test_errors_log_is_appended_bounded_and_the_legacy_file_removed(paths):
    legacy = os.path.join(paths.state_root, "errors.log")
    new = os.path.join(paths.state, "errors.log")
    with open(new, "w") as handle:
        handle.write("new line\n")
    with open(legacy, "w") as handle:
        handle.write("legacy line\n")
    assert mg.move_errors_log(paths) is True
    assert open(new).read() == "new line\nlegacy line\n" and not os.path.exists(legacy)
    assert stat.S_IMODE(os.stat(new).st_mode) == 0o600
    with open(legacy, "wb") as handle:
        handle.write(b"x" * (mg.LOG_CAP + 100))
    mg.move_errors_log(paths)
    assert os.path.getsize(new) == mg.LOG_CAP and not os.path.exists(legacy)
    assert mg.move_errors_log(paths) is False
