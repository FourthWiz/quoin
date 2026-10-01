"""`quoin opencode status`: a strictly read-only report on the latest phase run."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from quoin import cli
from quoin.opencode_adapter import proctree, runstore, status

from _opencode_run_helpers import InstalledProject

REPLAY = ("replay", {"fixture": "plain-complete.jsonl"})
KEYS = set(status.REPORT_KEYS)


def info(pid, ppid=1, pgid=None, state="S", start="Mon Jan  1 00:00:00 2026"):
    return proctree.ProcInfo(pid, ppid, pid if pgid is None else pgid, state, start)


def memory(project):
    path = project / ".workflow_artifacts" / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def seed(project, task="demo", state="running", driver_pid=77, driver_start="d", **extra):
    directory = runstore.store_dir(project, create=True)
    run_id, _ = runstore.reserve_run_id(directory)
    record = runstore.new_run_record(
        run_id, task, {"task": task, "stage": None, "phase": "plan", "profile": "work"}, {})
    record["state"] = state
    record["attempts"] = [{
        "attempt": 1, "pid": 4242, "pgid": 4242, "driver_pid": driver_pid,
        "driver_start": driver_start, "child_start": "c", "state": "running",
    }]
    record.update(extra)
    runstore.write_record(directory, record)
    runstore.write_pointer(directory, runstore.new_pointer(task, run_id))
    return directory, run_id


def lock(project, task="demo", **data):
    payload = {"pid": 4321, "task": task, "runtime": "opencode"}
    payload.update(data)
    path = memory(project) / ("run-supervisor-%s.pid" % task)
    path.write_text(json.dumps(payload))
    return path


def collect(project, table, **kw):
    kw.setdefault("task", "demo")
    return status.collect(
        project, lock_reader=cli._opencode_lock_reader(project),
        proc=type("P", (), {"snapshot": staticmethod(lambda: table)}), **kw)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def no_signals(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("status must never signal a process")

    monkeypatch.setattr(os, "kill", boom)
    monkeypatch.setattr(os, "killpg", boom)


def snapshot_tree(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        st = path.lstat()
        out[str(path)] = (st.st_size, st.st_mtime_ns)
    return out


def test_no_run_by_task_and_run_id(project, no_signals):
    report = collect(project, {1: info(1)})
    assert set(report) == KEYS and report["state"] == "none" and report["display_state"] == "no run"
    assert report["superseded_running"] == [] and report["lock"]["present"] is False
    report = collect(project, {1: info(1)}, task=None, run_id="oc-20260101T000000Z-aaaaaaaa")
    assert report["state"] == "none"


def test_orphaned_run_reads_driver_lost_without_touching_the_store(project, no_signals):
    directory, run_id = seed(project)
    before = snapshot_tree(project)
    table = {4242: info(4242, start="c"), 1: info(1)}
    report = collect(project, table)
    assert set(report) == KEYS
    assert report["display_state"] == "running (driver lost)" and report["state"] == "running"
    assert report["child"]["alive"] is True and report["run_id"] == run_id
    assert snapshot_tree(project) == before


def test_running_with_a_live_driver(project, no_signals):
    seed(project)
    report = collect(project, {77: info(77, start="d"), 4242: info(4242)})
    assert report["display_state"] == "running"


@pytest.mark.parametrize("table,alive,stale", [
    ({4321: info(4321)}, True, False),
    ({1: info(1)}, False, True),
    (None, None, None),
])
def test_opencode_lock_liveness(project, no_signals, table, alive, stale):
    lock(project)
    report = collect(project, table)
    assert report["lock"] == {
        "present": True, "pid": 4321, "runtime": "opencode", "alive": alive, "stale": stale}


def test_lock_without_runtime_reads_claude_and_garbage_reads_unknown(project, no_signals):
    path = memory(project) / "run-supervisor-demo.pid"
    path.write_text(json.dumps({"pid": 4321}))
    assert collect(project, {1: info(1)})["lock"]["runtime"] == "claude"
    path.write_text(json.dumps({"pid": 4321, "runtime": "x|y"}))
    assert collect(project, {1: info(1)})["lock"]["runtime"] == "unknown"
    assert collect(project, {1: info(1)})["lock"]["stale"] is False


def test_symlinked_lock_is_present_without_pid_or_runtime(project, tmp_path, no_signals):
    target = tmp_path / "elsewhere"
    target.write_text(json.dumps({"pid": 99, "runtime": "opencode"}))
    os.symlink(str(target), str(memory(project) / "run-supervisor-demo.pid"))
    report = collect(project, {1: info(1)})
    assert report["lock"]["present"] is True and report["lock"]["pid"] is None
    assert report["lock"]["runtime"] is None and report["lock"]["stale"] is None


def test_run_id_form_reports_the_task_lock(project, no_signals):
    _directory, run_id = seed(project)
    lock(project)
    report = collect(project, {4321: info(4321)}, task=None, run_id=run_id)
    assert report["task"] == "demo" and report["lock"]["alive"] is True


def test_wrong_owner_store_is_refused(project, monkeypatch):
    seed(project)
    real = os.lstat
    store = str(runstore.store_dir(project))

    def fake(path, *a, **k):
        st = real(path, *a, **k)
        if str(path) == store:
            values = list(st)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return st

    monkeypatch.setattr(os, "lstat", fake)
    with pytest.raises(status.StatusError):
        collect(project, {})


def test_invalid_names_are_errors(project):
    with pytest.raises(status.StatusError):
        collect(project, {}, task="../x")
    with pytest.raises(status.StatusError):
        collect(project, {}, task=None, run_id="nope")


def test_checkpoint_invalid_shows_the_blocked_resume_text(project, no_signals):
    seed(project, state="interrupted", resume_blocked="checkpoint-invalid",
         resume_hint="quoin run ... --new-run")
    report = collect(project, {1: info(1)})
    assert report["resume_blocked"] == "checkpoint-invalid"
    text = status.render_text(report)
    assert "resume is blocked (checkpoint-invalid)" in text and "--new-run" in text


def test_superseded_running_record_is_listed(project, no_signals):
    _d, old = seed(project, driver_pid=77, driver_start="d")
    _d, new = seed(project, state="interrupted")
    report = collect(project, {1: info(1)})
    assert report["run_id"] == new
    assert [r["run_id"] for r in report["superseded_running"]] == [old]
    assert report["superseded_running"][0]["driver_lost"] is True
    text = status.render_text(report)
    assert "kill -TERM -4242" in text and "--new-run" in text


def test_kill_hints_skip_unsafe_ids(project, no_signals):
    directory, run_id = seed(project)
    record = runstore.load_record(directory, run_id)
    record["attempts"][0]["pid"] = 1
    record["attempts"][0]["pgid"] = 0
    runstore.write_record(directory, record)
    text = status.render_text(collect(project, {1: info(1)}))
    assert "kill -TERM" not in text


def test_torn_sidecar_is_reported_and_not_repaired(project, no_signals):
    directory, run_id = seed(project)
    sidecar = runstore.run_paths(directory, run_id).sidecar
    sidecar.write_bytes(b'{"half": ')
    before = sidecar.read_bytes()
    report = collect(project, {1: info(1)})
    assert report["torn_tail"] is True and report["last_event"] is None
    assert sidecar.read_bytes() == before


def test_status_never_reconciles(project, monkeypatch):
    seed(project)
    import quoin.opencode_adapter.driver as driver

    for name in ("reconcile_task", "reconcile_run"):
        if hasattr(driver.OpenCodeDriver, name):
            monkeypatch.setattr(driver.OpenCodeDriver, name, lambda *a, **k: pytest.fail("reconciled"))
    before = snapshot_tree(project)
    collect(project, {1: info(1)})
    assert snapshot_tree(project) == before


def test_secret_shaped_record_text_is_redacted(project):
    seed(project, resume_hint="run with sk-abcdefghijklmnopqrstuvwxyz0123456789")
    report = collect(project, {1: info(1)})
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(report)


# --- against a real fake-binary run, through the CLI ------------------------------


def test_cli_json_and_text_after_a_completed_run(tmp_path, monkeypatch, capsys):
    holder = InstalledProject(tmp_path, monkeypatch, REPLAY)
    try:
        monkeypatch.setattr(cli, "_make_opencode_driver", holder.driver_factory())
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
        code = cli.main(["run", "demo", "--runtime", "opencode", "--profile", "work", "--phase", "plan",
                         "--project-root", str(holder.root)])
        assert code == 0
        capsys.readouterr()
        assert cli.main(["opencode", "status", "--task", "demo", "--project-root", str(holder.root),
                         "--json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert set(report) == KEYS and report["state"] == "completed" and report["phase"] == "plan"
        assert report["last_event"] is not None and report["lock"]["present"] is False
        assert cli.main(["opencode", "status", "--run-id", report["run_id"],
                         "--project-root", str(holder.root)]) == 0
        assert "state: completed" in capsys.readouterr().out
    finally:
        holder.cleanup()


def test_cli_requires_task_or_run_id_and_reports_bad_names(project, capsys):
    with pytest.raises(SystemExit):
        cli.main(["opencode", "status", "--project-root", str(project)])
    capsys.readouterr()
    assert cli.main(["opencode", "status", "--task", "../x", "--project-root", str(project)]) == 2
    assert "Traceback" not in capsys.readouterr().err
