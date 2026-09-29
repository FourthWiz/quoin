"""IVG-280 T-02: run_state.py --note / append_note() unit tests.

Shape mirrors test_run_state_writer.py: subprocess the script against
tmp_path, assert on files and stdout/stderr. No LLM calls.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "quoin" / "core" / "scripts" / "run_state.py"

PY = sys.executable

_SPEC = importlib.util.spec_from_file_location("_run_state_under_test", SCRIPT)
assert _SPEC is not None
run_state = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(run_state)


def _run(args, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [PY, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        timeout=30,
        env=full_env,
    )


def test_note_appends_exactly_one_block(tmp_path):
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    result = _run([
        "--note", "continuation attempt 1",
        "--project-root", str(tmp_path),
        "--task", "demo-task",
    ])
    assert result.returncode == 0
    notes_path = memory_dir / "run-notes-demo-task.md"
    content = notes_path.read_text(encoding="utf-8")
    assert content.count("— auto-resume") == 1
    assert "- continuation attempt 1" in content


def test_note_does_not_touch_run_state_record(tmp_path):
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    # Create a run-state record first via --write.
    write_result = _run([
        "--write",
        "--project-root", str(tmp_path),
        "--task", "demo-task",
        "--phase", "implement",
    ])
    assert write_result.returncode == 0
    record_path = memory_dir / "run-state-demo-task.json"
    before = record_path.read_bytes()

    note_result = _run([
        "--note", "a note that must not touch the record",
        "--project-root", str(tmp_path),
        "--task", "demo-task",
    ])
    assert note_result.returncode == 0
    after = record_path.read_bytes()
    assert before == after
    assert b'"schema": 1' in after


def test_note_rotates_past_max_bytes(tmp_path):
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    memory_dir.mkdir(parents=True)
    notes_path = memory_dir / "run-notes-demo-task.md"
    notes_path.write_text("x" * 200, encoding="utf-8")

    appended = run_state.append_note(memory_dir, "demo-task", "second note", max_bytes=100)
    assert appended is True
    rotated = Path(str(notes_path) + ".1")
    assert rotated.exists()
    assert rotated.read_text(encoding="utf-8") == "x" * 200
    assert "second note" in notes_path.read_text(encoding="utf-8")


def test_note_refuses_symlinked_notes_file(tmp_path, capsys=None):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    real_target = tmp_path / "elsewhere.md"
    real_target.write_text("", encoding="utf-8")
    notes_path = memory_dir / "run-notes-demo-task.md"
    notes_path.symlink_to(real_target)

    appended = run_state.append_note(memory_dir, "demo-task", "should not land")
    # append_note is best-effort: it always reports "attempted" (True), but
    # the underlying write must be refused — nothing lands in either file.
    assert appended is True
    assert real_target.read_text(encoding="utf-8") == ""
    assert notes_path.is_symlink()


def test_note_combined_with_write_writes_nothing(tmp_path):
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    result = _run([
        "--write",
        "--note", "conflicting mode",
        "--project-root", str(tmp_path),
        "--task", "demo-task",
    ])
    assert result.returncode == 0
    assert not memory_dir.exists() or not any(memory_dir.iterdir())


def test_note_invalid_task_writes_nothing(tmp_path):
    memory_dir = tmp_path / ".workflow_artifacts" / "memory"
    appended = run_state.append_note(memory_dir, "../escape", "text")
    assert appended is False
    assert not memory_dir.exists()

    appended_empty = run_state.append_note(memory_dir, "", "text")
    assert appended_empty is False
    assert not memory_dir.exists()
