"""Runtime-driver findings of the OpenCode doctor, and the read-only store
helpers they rely on. Nothing here may create, write or signal anything."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import _opencode_helpers as helpers

from quoin import cli
from quoin.opencode_adapter import doctor, generate, install, launch_env, proctree, runstore

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTO_RESUME = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"
RUNTIME_IDS = {
    "runtime-process-groups-unsupported", "runtime-sidecar-dir-unwritable", "runtime-orphan-run",
    "runtime-stale-lock", "runtime-command-agent-not-primary", "runtime-plugin-directory-present",
}


class _Sink:
    def __init__(self):
        self.buf = []

    def write(self, text):
        self.buf.append(text)


def _which_none(_name):
    return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    source_dir = helpers.copy_source_subset(tmp_path / "src")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(shutil, "which", _which_none)
    env = {"HOME": str(home)}
    rendered = generate.render_source_dir(source_dir)
    err = []
    code = install.run_install(project, source_dir, None, False, _Sink(), _Sink(), rendered=rendered)
    assert code == 0, err
    return SimpleNamespace(home=home, project=project, source=source_dir, env=env)


def host(world, table="unset", **kw):
    snapshot = None
    if table != "unset":
        snapshot = lambda: table  # noqa: E731
    return doctor.run_host(world.project, world.source, world.env, world.home, _which_none, None,
                           proc_snapshot=snapshot, **kw)


def ids(findings):
    return {f.id for f in findings}


def runtime_ids(findings):
    return {f.id for f in findings} & RUNTIME_IDS


def info(pid, ppid=1, pgid=None, state="S", start="Mon Jan  1 00:00:00 2026"):
    return proctree.ProcInfo(pid, ppid, pid if pgid is None else pgid, state, start)


def seed_running(world, driver_pid=77, driver_start="x", task="demo"):
    directory = runstore.store_dir(world.project, create=True)
    run_id, _ = runstore.reserve_run_id(directory)
    record = runstore.new_run_record(
        run_id, task, {"task": task, "stage": None, "phase": "plan", "profile": "work"}, {})
    record["state"] = "running"
    record["attempts"] = [{
        "attempt": 1, "pid": 4242, "pgid": 4242, "driver_pid": driver_pid,
        "driver_start": driver_start, "child_start": "c", "state": "running",
    }]
    runstore.write_record(directory, record)
    return directory, run_id


def write_lock(world, task="demo", **data):
    memory = world.project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    payload = {"pid": 4321, "writer": "cli", "task": task, "runtime": "opencode"}
    payload.update(data)
    path = memory / ("run-supervisor-%s.pid" % task)
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture
def no_signals(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the doctor must never signal a process")

    monkeypatch.setattr(os, "kill", boom)
    monkeypatch.setattr(os, "killpg", boom)


# --- clean host ---------------------------------------------------------------


def test_clean_installed_project_has_no_runtime_findings(world):
    def explode():
        raise AssertionError("no process table is needed on a clean project")

    findings = doctor.run_host(world.project, world.source, world.env, world.home, _which_none, None,
                               proc_snapshot=explode)
    assert runtime_ids(findings) == set()


def test_every_runtime_message_has_a_category_and_a_remediation():
    from quoin.opencode_adapter import categories

    for finding_id in RUNTIME_IDS:
        assert finding_id in doctor.MESSAGES
        assert categories.category_for(finding_id) in categories.CATEGORIES
        assert doctor._REMEDIATIONS.get(finding_id)


# --- process groups -----------------------------------------------------------


def test_process_groups_unsupported_warns(world, monkeypatch):
    monkeypatch.setattr(proctree, "SUPPORTED", False)
    findings = host(world)
    found = [f for f in findings if f.id == "runtime-process-groups-unsupported"]
    assert found and found[0].severity == "warn"


# --- run store ----------------------------------------------------------------


def test_missing_store_is_silent(world):
    assert "runtime-sidecar-dir-unwritable" not in ids(host(world))


def test_symlinked_store_warns(world, tmp_path):
    base = world.project / ".workflow_artifacts" / "memory" / "runtime"
    base.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (base / "opencode").symlink_to(target)
    assert "runtime-sidecar-dir-unwritable" in ids(host(world))


@pytest.mark.skipif(os.name != "posix" or os.getuid() == 0, reason="permission bits")
def test_unwritable_store_warns(world):
    directory = runstore.store_dir(world.project, create=True)
    os.chmod(str(directory), 0o500)
    try:
        assert "runtime-sidecar-dir-unwritable" in ids(host(world))
    finally:
        os.chmod(str(directory), 0o700)


def test_wrong_owner_store_warns(world, monkeypatch):
    directory = runstore.store_dir(world.project, create=True)
    real = os.lstat

    def fake(path, *a, **k):
        result = real(path, *a, **k)
        if str(path) == str(directory):
            return SimpleNamespace(st_mode=result.st_mode, st_uid=os.getuid() + 1)
        return result

    monkeypatch.setattr(os, "lstat", fake)
    assert "runtime-sidecar-dir-unwritable" in ids(host(world))


# --- orphaned runs ------------------------------------------------------------


def test_orphan_run_is_reported_when_the_driver_is_gone(world, no_signals):
    seed_running(world)
    findings = host(world, table={4242: info(4242)})
    found = [f for f in findings if f.id == "runtime-orphan-run"]
    assert len(found) == 1 and found[0].severity == "warn" and "1 OpenCode" in found[0].message


def test_orphan_run_silent_when_the_driver_is_alive(world, no_signals):
    seed_running(world, driver_pid=77, driver_start="x")
    table = {77: info(77, start="x"), 4242: info(4242)}
    assert "runtime-orphan-run" not in ids(host(world, table=table))


def test_orphan_run_silent_when_the_table_is_unavailable(world, no_signals):
    seed_running(world)
    assert "runtime-orphan-run" not in ids(host(world, table=None))


def test_finished_runs_never_need_the_process_table(world):
    directory, run_id = seed_running(world)
    record = runstore.load_record(directory, run_id)
    record["state"] = "completed"
    runstore.write_record(directory, record)

    def explode():
        raise AssertionError("no running record, no snapshot")

    findings = doctor.run_host(world.project, world.source, world.env, world.home, _which_none, None,
                               proc_snapshot=explode)
    assert "runtime-orphan-run" not in ids(findings)


# --- stale locks --------------------------------------------------------------


def test_stale_opencode_lock_is_reported(world, no_signals):
    write_lock(world)
    findings = host(world, table={1: info(1)})
    found = [f for f in findings if f.id == "runtime-stale-lock"]
    assert len(found) == 1 and "demo" in found[0].message


def test_lock_with_a_listed_pid_is_not_stale(world, no_signals):
    write_lock(world)
    assert "runtime-stale-lock" not in ids(host(world, table={4321: info(4321)}))


def test_lock_whose_pid_is_a_zombie_is_stale(world, no_signals):
    write_lock(world)
    assert "runtime-stale-lock" in ids(host(world, table={4321: info(4321, state="Z")}))


def test_lock_is_never_stale_without_a_table(world, no_signals):
    write_lock(world)
    assert "runtime-stale-lock" not in ids(host(world, table=None))


@pytest.mark.parametrize("runtime", ["claude", None, "x|y"])
def test_other_runtime_locks_are_ignored(world, runtime, no_signals):
    path = write_lock(world)
    data = json.loads(path.read_text())
    if runtime is None:
        del data["runtime"]
    else:
        data["runtime"] = runtime
    path.write_text(json.dumps(data))

    def explode():
        raise AssertionError("not an opencode lock")

    findings = doctor.run_host(world.project, world.source, world.env, world.home, _which_none, None,
                               proc_snapshot=explode)
    assert "runtime-stale-lock" not in ids(findings)


def test_symlinked_lock_is_not_read(world, tmp_path):
    target = tmp_path / "real.pid"
    target.write_text(json.dumps({"pid": 4321, "runtime": "opencode"}))
    memory = world.project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "run-supervisor-demo.pid").symlink_to(target)
    assert "runtime-stale-lock" not in ids(host(world, table={1: info(1)}))


def test_lock_scan_is_bounded(world, monkeypatch):
    memory = world.project / ".workflow_artifacts" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    for i in range(doctor._MAX_LOCKS + 5):
        (memory / ("run-supervisor-t%04d.pid" % i)).write_text("{}")
    assert len(doctor._read_locks(memory)) <= doctor._MAX_LOCKS


def test_lock_template_parity():
    spec = importlib.util.spec_from_file_location("auto_resume_parity", AUTO_RESUME)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert doctor._LOCK_TEMPLATE == cli._SUPERVISOR_LOCK_TEMPLATE == module.LOCK_TEMPLATE
    assert doctor._LOCK_GLOB == doctor._LOCK_TEMPLATE.replace("{task}", "*")


# --- phase commands -----------------------------------------------------------


def _first_command(world):
    meta = install.load_metadata(world.project)
    return sorted(r for r, rec in meta.owned.items() if rec["kind"] == "command")[0]


def test_command_without_a_primary_agent_is_an_error(world):
    rel = _first_command(world)
    path = world.project / rel
    path.write_text("---\ndescription: x\n---\n\nbody\n")
    found = [f for f in host(world) if f.id == "runtime-command-agent-not-primary"]
    assert found and found[0].severity == "error" and found[0].path == rel


def test_missing_command_file_is_left_to_the_owned_missing_finding(world):
    rel = _first_command(world)
    (world.project / rel).unlink()
    findings = host(world)
    assert "owned-missing" in ids(findings)
    assert "runtime-command-agent-not-primary" not in ids(findings)


# --- plugin directories -------------------------------------------------------


def test_plugin_directory_with_a_script_warns(world):
    plugins = world.project / ".opencode" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "hook.ts").write_text("export default {}")
    found = [f for f in host(world) if f.id == "runtime-plugin-directory-present"]
    assert found and found[0].severity == "warn"
    assert ".opencode/plugins" in found[0].path and str(world.project) not in found[0].path
    assert "XDG_CONFIG_HOME" in found[0].remediation


def test_plugin_directory_without_scripts_is_silent(world):
    plugins = world.project / ".opencode" / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "notes.md").write_text("x")
    (plugins / "sub.ts").mkdir()
    assert "runtime-plugin-directory-present" not in ids(host(world))


@pytest.mark.skipif(os.name != "posix" or os.getuid() == 0, reason="permission bits")
def test_unreadable_plugin_directory_uses_the_config_unreadable_finding(world):
    plugins = world.project / ".opencode" / "plugin"
    plugins.mkdir(parents=True, exist_ok=True)
    os.chmod(str(plugins), 0)
    try:
        findings = host(world)
    finally:
        os.chmod(str(plugins), 0o700)
    assert "config-unreadable" in ids(findings)
    assert "runtime-plugin-directory-present" not in ids(findings)


@pytest.mark.skipif(os.name != "posix" or os.getuid() == 0, reason="permission bits")
@pytest.mark.parametrize("populated_first", [True, False])
def test_plugin_helper_order_matches_the_launch_check(world, populated_first):
    populated = world.project / ".opencode" / ("plugin" if populated_first else "plugins")
    unreadable = world.project / ".opencode" / ("plugins" if populated_first else "plugin")
    populated.mkdir(parents=True)
    (populated / "a.js").write_text("x")
    unreadable.mkdir(parents=True)
    os.chmod(str(unreadable), 0)
    try:
        managed = Path(world.home / "no-managed")
        entries = launch_env.plugin_directories(world.project, world.env, world.home, managed)
        with pytest.raises(launch_env.LaunchRefused) as raised:
            launch_env._check_markdown(  # noqa: SLF001
                world.project, world.env, world.home, doctor._worktree_root(world.project),
                doctor._xdg_config_dir(world.env, world.home), managed, {}, {}, str,
            )
    finally:
        os.chmod(str(unreadable), 0o700)
    first_state = entries[0][1]
    expected = "plugin-directory-present" if first_state == "populated" else "config-layer-unreadable"
    assert raised.value.code == expected
    assert [state for _p, state in entries] == (
        ["populated", "unreadable"] if populated_first else ["unreadable", "populated"]
    )


# --- read-only contract -------------------------------------------------------


def _snapshot_tree(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        info_ = os.lstat(str(path))
        out[str(path)] = (info_.st_mtime_ns, info_.st_size, stat.S_IMODE(info_.st_mode))
    return out


def test_doctor_run_never_signals_and_never_changes_the_project(world, no_signals):
    seed_running(world)
    write_lock(world)
    before = _snapshot_tree(world.project)
    findings = host(world, table={1: info(1)})
    assert {"runtime-orphan-run", "runtime-stale-lock"} <= ids(findings)
    assert _snapshot_tree(world.project) == before


# --- store helpers ------------------------------------------------------------


def test_inspect_store_missing_and_present(world, tmp_path):
    assert runstore.inspect_store(world.project) is None
    directory = runstore.store_dir(world.project, create=True)
    assert runstore.inspect_store(world.project) == directory


def test_inspect_store_never_creates(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    assert runstore.inspect_store(root) is None
    assert not (root / ".workflow_artifacts").exists()


def test_inspect_store_refuses_a_file_component(tmp_path):
    root = tmp_path / "p"
    (root / ".workflow_artifacts").mkdir(parents=True)
    (root / ".workflow_artifacts" / "memory").write_text("x")
    with pytest.raises(runstore.RunStoreError):
        runstore.inspect_store(root)


def test_list_records_filters_bounds_and_skips_symlinks(world, tmp_path):
    directory, first = seed_running(world, task="a")
    _, second = seed_running(world, task="b")
    records, skipped = runstore.list_records(directory)
    assert {r["run_id"] for r in records} == {first, second} and skipped == 0
    only_a, _ = runstore.list_records(directory, task="a")
    assert [r["run_id"] for r in only_a] == [first]
    limited, skipped = runstore.list_records(directory, limit=1)
    assert len(limited) == 1 and skipped == 1
    outside = tmp_path / "x.json"
    outside.write_text(json.dumps({"schema_version": 1, "task": "a"}))
    (directory / "oc-20260101T000000Z-deadbeef.run.json").symlink_to(outside)
    records, skipped = runstore.list_records(directory)
    assert len(records) == 2 and skipped == 1


def test_pid_alive_rule():
    table = {5: info(5), 6: info(6, state="Z")}
    assert runstore.pid_alive(5, table) is True
    assert runstore.pid_alive(6, table) is False
    assert runstore.pid_alive(7, table) is False
    assert runstore.pid_alive(True, table) is False
    assert runstore.pid_alive(5, None) is None
