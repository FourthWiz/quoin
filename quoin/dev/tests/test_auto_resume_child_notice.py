"""Hand-off notices, halt hints and lock merging for the headless child's
session id (auto_resume.py surfaces)."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_PATH = REPO_ROOT / "quoin" / "core" / "scripts" / "auto_resume.py"
_HOOK = REPO_ROOT / "quoin" / "hooks" / "sessionstart.sh"
U = "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"


def _load_module():
    spec = importlib.util.spec_from_file_location("auto_resume_child_notice_under_test", _CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ar(monkeypatch, tmp_path):
    module = _load_module()
    monkeypatch.setattr(module, "_runtime_record_path", lambda: tmp_path / "absent-quoin-runtime.json")
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    return module


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _mk_project(tmp_path: Path, *parts: str) -> Path:
    root = tmp_path.joinpath(*(parts or ("project",)))
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


def _mem(root: Path) -> Path:
    return root / ".workflow_artifacts" / "memory"


def _marker(memory: Path, task="demo"):
    (memory / f"autonomous-run-{task}.marker").write_text(
        f"task: {task}\ntimestamp: 2026-09-29T00:00:00+00:00\nautonomous: true\n", encoding="utf-8")


def _record(memory: Path, task="demo", sid="sid-1", **extra):
    rec = {
        "schema": 1, "task": task, "session_id": sid, "active": True, "phase": "implement",
        "phase_index": 3, "subphase": "", "step": "", "at_stage_boundary": False, "route": "",
        "profile": "", "artifacts": [], "next_action": "", "resume_command": f"/run --resume {task}",
        "notes_path": str(memory / f"run-notes-{task}.md"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    rec.update(extra)
    (memory / f"run-state-{task}.json").write_text(json.dumps(rec), encoding="utf-8")


def _runtime_record(ar, tmp_path, project: Path):
    interp = tmp_path / "fakebin" / "python"
    interp.parent.mkdir(parents=True, exist_ok=True)
    interp.write_text("#!/bin/sh\necho QUOIN_VERSION=9.9.9\nexit 0\n")
    interp.chmod(interp.stat().st_mode | stat.S_IEXEC)
    path = ar._runtime_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": 1, "python": str(interp), "version": "9.9.9", "pythonpath": None,
        "quoin_file": "/fake/quoin/__init__.py", "source_dir": "/fake/source",
        "source_version": None, "installed_at": "2026-09-29T00:00:00Z",
    }))


def _handoff(ar, project, monkeypatch, capsys, pid=777):
    captured = []

    class _Proc:
        pass

    _Proc.pid = pid
    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: captured.append((argv, kw.get("env"))) or _Proc())
    rc = ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt=None))
    assert rc == 0
    return capsys.readouterr().out.strip(), captured


def _setup_record_handoff(ar, tmp_path, project):
    _marker(_mem(project))
    _record(_mem(project))
    _runtime_record(ar, tmp_path, project)


def test_record_handoff_carries_child_id(ar, tmp_path, monkeypatch, capsys):
    project = _mk_project(tmp_path)
    _setup_record_handoff(ar, tmp_path, project)
    out, captured = _handoff(ar, project, monkeypatch, capsys)
    env = captured[0][1]
    sid = env[ar._CHILD_ENV]
    assert ar._is_child_session_id(sid)
    assert out.startswith("HANDOFF|777|")
    assert out.split("|")[3] == sid
    notes = (_mem(project) / "run-notes-demo.md").read_text()
    assert f"child_session={sid} takeover: quoin run --takeover demo --project-root" in notes


def test_legacy_handoff_has_no_child_id(ar, tmp_path, monkeypatch, capsys):
    project = _mk_project(tmp_path)
    _marker(_mem(project))
    _record(_mem(project))
    monkeypatch.setattr(ar, "_which", lambda name: "/usr/bin/quoin")
    out, captured = _handoff(ar, project, monkeypatch, capsys)
    assert len(out.split("|")) == 3
    assert ar._CHILD_ENV not in captured[0][1]
    notes = (_mem(project) / "run-notes-demo.md").read_text()
    assert "takeover: quoin run --takeover demo" in notes and "child_session=" not in notes


def _start_setup(ar, project):
    memory = _mem(project)
    _marker(memory)
    _record(memory, sid="sid-owner-gone")
    (memory / "session-ended-sid-owner-gone.txt").touch()


def test_start_notice_includes_child_and_ignores_malformed(ar, tmp_path, monkeypatch, capsys):
    project = _mk_project(tmp_path)
    _start_setup(ar, project)
    monkeypatch.setattr(ar, "_do_handoff", lambda *a, **k: f"HANDOFF|55|1/10|{U}")
    assert ar._cmd_start(_Args(project_root=str(project), source="startup", session_id="new")) == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert f"child_session={U}" in ctx
    monkeypatch.setattr(ar, "_do_handoff", lambda *a, **k: "HANDOFF|55|1/10|../x")
    ar._cmd_start(_Args(project_root=str(project), source="startup", session_id="new"))
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "child_session=" not in ctx and "takeover:" in ctx


def _hook_sed_expr() -> str:
    m = re.search(r"sed -n '([^']*via=supervisor[^']*)'", _HOOK.read_text())
    assert m
    return m.group(1)


@pytest.mark.parametrize("parts", [("project",), ("My Drive", "p")])
def test_hook_sed_still_extracts_task(ar, tmp_path, monkeypatch, capsys, parts):
    project = _mk_project(tmp_path, *parts)
    _start_setup(ar, project)
    monkeypatch.setattr(ar, "_do_handoff", lambda *a, **k: f"HANDOFF|55|1/10|{U}")
    ar._cmd_start(_Args(project_root=str(project), source="startup", session_id="new"))
    stdout = capsys.readouterr().out
    got = subprocess.run(["sed", "-n", _hook_sed_expr()], input=stdout, capture_output=True, text=True).stdout
    assert got.strip() == "demo"


def test_task_equals_guard(ar):
    assert ar._handoff_notice_extra("demo", "/x/task=x/p", U) == ""
    assert ar._handoff_notice_extra("demo", "/x/p", U) != ""


def test_write_lock_merges_child_fields_for_same_token(ar, tmp_path):
    project = _mk_project(tmp_path)
    memory = _mem(project)
    lock = memory / "run-supervisor-demo.pid"
    lock.write_text(json.dumps({"pid": 1, "token": "tok", "child_session_id": U, "child_cwd": "/r", "child_pid": 9}))
    ar._write_lock(memory, "demo", 4242, 5, "handoff", token="tok")
    data = json.loads(lock.read_text())
    assert (data["child_session_id"], data["child_cwd"], data["child_pid"]) == (U, "/r", 9)
    assert data["pid"] == 4242 and data["granted"] == 5 and data["writer"] == "handoff" and data["token"] == "tok"
    lock.write_text(json.dumps({"pid": 1, "token": "other", "child_session_id": U}))
    ar._write_lock(memory, "demo", 4242, 5, "handoff", token="tok")
    assert "child_session_id" not in json.loads(lock.read_text())


def test_space_and_apostrophe_roots(ar, tmp_path, monkeypatch, capsys):
    spaced = _mk_project(tmp_path, "My Drive", "p")
    _setup_record_handoff(ar, tmp_path, spaced)
    _handoff(ar, spaced, monkeypatch, capsys)
    notes = (_mem(spaced) / "run-notes-demo.md").read_text()
    assert "takeover: quoin run --takeover demo --project-root '" in notes

    apos = _mk_project(tmp_path, "it's")
    _marker(_mem(apos))
    _record(_mem(apos))
    _handoff(ar, apos, monkeypatch, capsys)
    line = [l for l in (_mem(apos) / "run-notes-demo.md").read_text().splitlines() if "takeover:" in l][0]
    assert line.rstrip().endswith("takeover: quoin run --takeover demo")
    assert "--project-root" not in line

    for root in (spaced, apos):
        ar._write_halt(_mem(root), "demo", None, "x")
        halt = (_mem(root) / "autonomous-halt-demo.md").read_text()
        assert "takeover_hint: quoin run --takeover demo --project-root" in halt


def _halts(ar, project):
    memory = _mem(project)
    rec = json.loads((memory / "run-state-demo.json").read_text())
    out = {}
    ar._write_halt(memory, "demo", rec, "auto-resume cap")
    out["cap"] = (memory / "autonomous-halt-demo.md").read_text()
    (memory / "autonomous-halt-demo.md").unlink()
    ar._cmd_pause(_Args(project_root=str(project), task="demo", session_id="sid-1"))
    out["pause"] = (memory / "autonomous-halt-demo.md").read_text()
    (memory / "autonomous-halt-demo.md").unlink()
    return out


def test_halts_carry_takeover_hint_and_keep_resume_hint(ar, tmp_path):
    project = _mk_project(tmp_path)
    memory = _mem(project)
    _marker(memory)
    _record(memory, child_session_id=U)
    (memory / "run-supervisor-demo.pid").write_text(json.dumps({"pid": 2**22 + 12345}))
    for reason, text in _halts(ar, project).items():
        assert "resume_hint: /run --resume demo\n" in text
        assert f"takeover_hint: child_session={U} quoin run --takeover demo" in text
        assert "claude --resume" not in text


def test_halt_falls_back_to_lock_child_id(ar, tmp_path):
    project = _mk_project(tmp_path)
    memory = _mem(project)
    _record(memory)
    (memory / "run-supervisor-demo.pid").write_text(json.dumps({"pid": 1, "child_session_id": U}))
    ar._write_halt(memory, "demo", json.loads((memory / "run-state-demo.json").read_text()), "x")
    assert f"child_session={U}" in (memory / "autonomous-halt-demo.md").read_text()


def test_on_fail_halt_path_has_takeover_hint(ar, tmp_path, monkeypatch, capsys):
    project = _mk_project(tmp_path)
    _marker(_mem(project))
    _record(_mem(project))
    monkeypatch.setattr(ar, "_which", lambda name: None)
    ar._cmd_handoff(_Args(project_root=str(project), task="demo", reason="budget", on_fail_halt="context exhaustion"))
    halt = (_mem(project) / "autonomous-halt-demo.md").read_text()
    assert "takeover_hint: quoin run --takeover demo" in halt
    assert "reason: context exhaustion" in halt


def test_status_reports_child_id(ar, tmp_path, capsys):
    project = _mk_project(tmp_path)
    _record(_mem(project), child_session_id=U)
    ar._cmd_status(_Args(project_root=str(project), task="demo"))
    assert json.loads(capsys.readouterr().out)["child_session_id"] == U


def test_relative_project_root_yields_absolute_pointer(ar, tmp_path, monkeypatch, capsys):
    project = _mk_project(tmp_path, "rel")
    _setup_record_handoff(ar, tmp_path, project)
    monkeypatch.chdir(tmp_path)
    captured = []

    class _Proc:
        pid = 5

    monkeypatch.setattr(ar, "_popen", lambda argv, **kw: captured.append(argv) or _Proc())
    ar._cmd_handoff(_Args(project_root="rel", task="demo", reason="budget", on_fail_halt=None))
    notes = (_mem(project) / "run-notes-demo.md").read_text()
    assert f"--project-root {project.resolve()}" in notes
