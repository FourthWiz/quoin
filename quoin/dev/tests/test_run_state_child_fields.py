"""Run-state record: optional child-tracking fields (set_child_fields,
carry-forward across every writer path, shell extractor compatibility)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "quoin" / "core" / "scripts" / "run_state.py"
LIB = REPO_ROOT / "quoin" / "hooks" / "_lib.sh"

U = "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
U2 = "1b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e9"


@pytest.fixture(scope="module")
def rs():
    spec = importlib.util.spec_from_file_location("_rs_child_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, rs):
    root = tmp_path
    mem = root / ".workflow_artifacts" / "memory"

    def write(*extra):
        assert rs.main(["--write", "--project-root", str(root), "--task", "demo",
                        "--phase", "implement", "--next-action", "go", *extra]) == 0

    write("--session-id", "sess-1")
    return root, mem, mem / "run-state-demo.json", write


def _keys(path):
    return list(json.loads(path.read_text()).keys())


def test_record_without_child_keys_has_16_keys(env):
    _, _, path, _ = env
    assert len(_keys(path)) == 16
    assert "child_session_id" not in path.read_text()


def test_set_child_fields_keeps_progress_state(env, rs):
    root, mem, path, _ = env
    os.utime(str(path), (1_000_000_000, 1_000_000_000))
    before = json.loads(path.read_text())
    notes = mem / "run-notes-demo.md"
    notes_before = notes.read_bytes()
    assert rs.set_child_fields(mem, "demo", U, "/repo dir", "2026-09-30T10:00:00Z") is True
    after = json.loads(path.read_text())
    assert len(after) == 19
    assert after["updated_at"] == before["updated_at"]
    assert after["child_session_id"] == U and after["child_cwd"] == "/repo dir"
    assert int(path.stat().st_mtime) == 1_000_000_000
    assert notes.read_bytes() == notes_before


def test_fields_survive_every_writer_path(env, rs):
    root, mem, path, write = env
    rs.set_child_fields(mem, "demo", U, "/repo", "T")

    def has():
        d = json.loads(path.read_text())
        return d.get("child_session_id") == U and d.get("child_cwd") == "/repo"

    write("--step", "a")
    assert has()
    write("--require-existing", "--step", "b")
    assert has()
    write("--require-existing", "--adopt-session", "--session-id", "sess-2")
    assert has()
    assert json.loads(path.read_text())["session_id"] == "sess-2"
    assert rs.main(["--clear", "--project-root", str(root), "--task", "demo"]) == 0
    d = json.loads(path.read_text())
    assert d["active"] is False and d["child_session_id"] == U


def test_clear_child_fields_returns_to_16_keys(env, rs):
    _, mem, path, _ = env
    rs.set_child_fields(mem, "demo", U, "/repo", "T")
    assert rs.set_child_fields(mem, "demo", "", "", "") is True
    assert len(_keys(path)) == 16


def test_refusals_leave_file_untouched(env, rs):
    root, mem, path, _ = env
    snap = path.read_bytes()
    assert rs.set_child_fields(mem, "demo", "not-a-uuid", "/r", "T") is False
    assert rs.set_child_fields(mem, "demo", U.upper(), "/r", "T") is False
    assert rs.set_child_fields(mem, "../x", U, "/r", "T") is False
    assert rs.set_child_fields(mem, "absent", U, "/r", "T") is False
    assert path.read_bytes() == snap
    rs.main(["--clear", "--project-root", str(root), "--task", "demo"])
    snap = path.read_bytes()
    assert rs.set_child_fields(mem, "demo", U, "/r", "T") is False
    assert path.read_bytes() == snap


def test_cwd_storage_rules(env, rs):
    _, mem, path, _ = env
    rs.set_child_fields(mem, "demo", U, '/a"b', "T")
    assert "child_cwd" not in json.loads(path.read_text())  # stored empty -> omitted
    rs.set_child_fields(mem, "demo", U, "/it's a dir", "T")
    assert json.loads(path.read_text())["child_cwd"] == "/it's a dir"


def test_shell_extractor_finds_keys_by_name(env, rs):
    _, mem, path, _ = env
    rs.set_child_fields(mem, "demo", U, "/repo", "T")
    out = subprocess.run(
        ["sh", "-c", f'. "{LIB}"; run_state_fields "{path}" next_action notes_path child_session_id'],
        capture_output=True, text=True, timeout=30,
    ).stdout.splitlines()
    assert out[0] == "next_action=go"
    assert out[1].startswith("notes_path=") and out[1].endswith("run-notes-demo.md")
    assert out[2] == f"child_session_id={U}"


def test_read_cli_prints_child_field(env, rs, capsys):
    root, mem, _, _ = env
    rs.set_child_fields(mem, "demo", U, "/repo", "T")
    assert rs.main(["--read", "--project-root", str(root), "--task", "demo",
                    "--fields", "child_session_id"]) == 0
    assert capsys.readouterr().out.strip() == f"child_session_id={U}"
