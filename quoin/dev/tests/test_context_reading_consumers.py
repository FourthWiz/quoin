"""Consumers of the real context reading: the prompt hook, the checkpoint call
shape and the phase-boundary guard all see the corrected number.

Import-free / bare-checkout runnable: subprocess calls on file paths only.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK = REPO_ROOT / "quoin" / "hooks" / "userpromptsubmit.sh"
LIB_SH = REPO_ROOT / "quoin" / "hooks" / "_lib.sh"
CORE_GUARD = REPO_ROOT / "quoin" / "core" / "scripts" / "context_budget_guard.py"
WRAPPER = REPO_ROOT / "quoin" / "scripts" / "context_budget_guard.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "context_reading"


def _env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C"}


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".workflow_artifacts" / "memory").mkdir(parents=True, exist_ok=True)
    return project


def _record(model: str, total: int) -> str:
    return json.dumps({"type": "assistant", "isSidechain": False, "message": {
        "model": model,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": total - 510,
                  "cache_creation_input_tokens": 500, "output_tokens": 5}}}) + "\n"


def _run_hook(tmp_path: Path, transcript: Path):
    project = _project(tmp_path)
    stdin = json.dumps({"session_id": "sess-consumers", "transcript_path": str(transcript),
                        "cwd": str(project), "prompt": "continue"})
    return subprocess.run(
        ["sh", str(HOOK)], input=stdin, capture_output=True, text=True,
        env=_env(tmp_path), cwd=str(project), timeout=60,
    )


def test_prompt_hook_advises_at_real_utilization(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_record("claude-opus-4-6", 150000), encoding="utf-8")
    proc = _run_hook(tmp_path, transcript)
    assert proc.returncode == 0
    assert "context at 75.00%" in proc.stdout


def test_prompt_hook_stays_quiet_when_a_big_tool_result_inflated_the_file(tmp_path):
    """A 1.3 MB earlier line made the byte estimate say BLOCK; the real reading is 17%."""
    transcript = tmp_path / "t.jsonl"
    padding = json.dumps({"type": "user", "isSidechain": False,
                          "message": {"role": "user", "content": "q" * 1_300_000}})
    transcript.write_text(padding + "\n" + _record("claude-opus-5-5", 170852), encoding="utf-8")
    proc = _run_hook(tmp_path, transcript)
    assert proc.returncode == 0
    assert "context at" not in proc.stdout
    assert not list((tmp_path / "project" / ".workflow_artifacts" / "memory").glob("pending-prompt-*"))


@pytest.mark.parametrize("shell", [s for s in ("zsh", "sh") if shutil.which(s)])
def test_checkpoint_call_shape_returns_real_bps(shell, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_record("claude-opus-4-6", 185000), encoding="utf-8")
    proc = subprocess.run(
        [shell, "-c", '. "$1" && read_constants && compute_utilization "$2"',
         "x", str(LIB_SH), str(transcript)],
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "9250"


def _guard(args, tmp_path, python=None):
    return subprocess.run(
        [python or sys.executable, str(WRAPPER), *args],
        capture_output=True, text=True, env=_env(tmp_path), timeout=60,
    )


def test_guard_reports_over_on_real_usage(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_record("claude-opus-5-5", 750000), encoding="utf-8")
    proc = _guard(["--transcript", str(transcript), "--threshold-bps", "7000"], tmp_path)
    assert proc.returncode == 1
    assert proc.stdout.startswith("OVER|7500|")


def test_guard_reports_ok_on_a_one_million_window(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_record("claude-opus-5-5", 170852), encoding="utf-8")
    proc = _guard(["--transcript", str(transcript), "--threshold-bps", "7000"], tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.startswith("OK|1708|")


def test_guard_source_parses_with_python_38_grammar():
    ast.parse(CORE_GUARD.read_text(encoding="utf-8"), feature_version=(3, 8))


def _python38():
    exe = shutil.which("python3.8")
    if not exe:
        return None
    probe = subprocess.run([exe, "--version"], capture_output=True, text=True)
    return exe if probe.returncode == 0 and "3.8" in (probe.stdout + probe.stderr) else None


@pytest.mark.skipif(_python38() is None, reason="python3.8 not available")
def test_guard_runs_under_python_38(tmp_path):
    real = FIXTURES / "opus55_live.jsonl"
    proc = _guard(["--transcript", str(real), "--threshold-bps", "9999"], tmp_path, python=_python38())
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("OK|1708|")
    assert "source=real" in proc.stderr
    byte_file = tmp_path / "x.jsonl"
    byte_file.write_bytes(b"x" * 200000)
    proc = _guard(["--transcript", str(byte_file), "--threshold-bps", "9999"], tmp_path, python=_python38())
    assert proc.returncode == 0, proc.stderr
    assert "source=estimated" in proc.stderr
