"""Compaction telemetry rows carry the real token reading next to the byte estimate.

Runs the real precompact and postcompact hooks against a temporary project and
reads the rows they append. Subprocess calls on file paths only, so the test
runs from a bare checkout.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRE = REPO_ROOT / "quoin" / "hooks" / "precompact.sh"
POST = REPO_ROOT / "quoin" / "hooks" / "postcompact.sh"
TELEMETRY = REPO_ROOT / "quoin" / "core" / "scripts" / "compaction_telemetry.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "context_reading"

NEW_KEYS_PRE = ("real_tokens_before", "real_window", "reading_source")
NEW_KEYS_POST = ("real_tokens_after", "real_window", "reading_source")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".workflow_artifacts" / "memory").mkdir(parents=True, exist_ok=True)
    return project


def _run(hook: Path, tmp_path: Path, transcript, process_cwd: Path = None):
    project = _project(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    stdin = json.dumps({"trigger": "auto", "session_id": "sess-tel", "cwd": str(project),
                        "transcript_path": str(transcript)})
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C"}
    proc = subprocess.run(["sh", str(hook)], input=stdin, capture_output=True, text=True,
                          env=env, cwd=str(process_cwd or project), timeout=60)
    assert proc.returncode == 0, proc.stderr
    sink = project / ".workflow_artifacts" / "memory" / "telemetry" / "compaction-events.jsonl"
    rows = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "no telemetry row written"
    return rows[-1], sink


def test_pre_row_records_a_real_reading(tmp_path):
    transcript = FIXTURES / "opus55_live.jsonl"
    row, _ = _run(PRE, tmp_path, transcript)
    assert row["half"] == "pre"
    assert row["real_tokens_before"] == 170852
    assert row["real_window"] == 1000000
    assert row["reading_source"] == "real"
    assert row["bytes_before"] == transcript.stat().st_size
    assert row["est_tokens_before"] == int(transcript.stat().st_size / 8.0)


def test_pre_row_without_usage_falls_back_to_the_estimate(tmp_path):
    transcript = FIXTURES / "zero_usage.jsonl"
    row, _ = _run(PRE, tmp_path, transcript)
    assert row["real_tokens_before"] is None
    assert row["real_window"] is None
    assert row["reading_source"] == "estimated"
    assert row["est_tokens_before"] == int(transcript.stat().st_size / 8.0)


def test_pre_row_with_unreadable_transcript_leaves_every_figure_null(tmp_path):
    row, _ = _run(PRE, tmp_path, tmp_path / "missing.jsonl")
    assert row["bytes_before"] is None and row["est_tokens_before"] is None
    assert all(row[k] is None for k in NEW_KEYS_PRE)


def test_post_row_leaves_real_value_null_while_the_boundary_is_not_yet_written(tmp_path):
    """The newest reading is the pre-compaction usage record: recording it as the
    post-compaction value would be wrong, so the row falls back to the estimate."""
    transcript = FIXTURES / "opus55_live.jsonl"
    row, _ = _run(POST, tmp_path, transcript)
    assert row["half"] == "post"
    assert row["real_tokens_after"] is None
    assert row["real_window"] is None
    assert row["reading_source"] == "estimated"
    assert row["est_tokens_after"] == int(transcript.stat().st_size / 8.0)


def test_post_row_records_the_post_compaction_tokens_once_the_boundary_exists(tmp_path):
    transcript = FIXTURES / "compact_newer.jsonl"
    row, _ = _run(POST, tmp_path, transcript)
    assert row["real_tokens_after"] == 17869
    assert row["real_window"] == 1000000
    assert row["reading_source"] == "real"


def test_existing_keys_are_unchanged_and_consumer_still_parses(tmp_path):
    transcript = FIXTURES / "compact_newer.jsonl"
    pre, sink = _run(PRE, tmp_path, transcript)
    post, _ = _run(POST, tmp_path, transcript)
    assert list(pre)[:5] == ["v", "half", "session_id", "event_seq", "ts"]
    assert pre["v"] == 1 and post["v"] == 1
    for key in ("bytes_before", "est_tokens_before", "task", "phase", "subphase", "step"):
        assert key in pre
    for key in ("bytes_after", "est_tokens_after", "trigger", "compact_summary_len"):
        assert key in post
    proc = subprocess.run([sys.executable, str(TELEMETRY), "--project-root", str(sink.parents[2]), "--format", "json"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_project_settings_are_found_through_the_hook_input_cwd(tmp_path):
    """A [1m] project setting widens the window of an unknown-model reading; the
    hook process runs elsewhere, so only the cwd in the hook input can find it."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "isSidechain": False, "message": {
        "model": "claude-opus-4-6", "usage": {"input_tokens": 100000}}}) + "\n", encoding="utf-8")
    project = _project(tmp_path)
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"model": "claude-opus-4-6[1m]"}), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    pre, _ = _run(PRE, tmp_path, transcript, process_cwd=elsewhere)
    assert pre["real_window"] == 1000000
    post_transcript = tmp_path / "p.jsonl"
    post_transcript.write_text(
        transcript.read_text(encoding="utf-8")
        + json.dumps({"type": "system", "subtype": "compact_boundary",
                      "compactMetadata": {"preTokens": 100000, "postTokens": 9000}}) + "\n",
        encoding="utf-8")
    post, _ = _run(POST, tmp_path, post_transcript, process_cwd=elsewhere)
    assert post["real_window"] == 1000000
