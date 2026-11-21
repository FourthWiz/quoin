"""Harvest: model replies become inbox candidates, near-duplicates are dropped,
nothing reaches lessons-learned.md or CLAUDE.md without an accept, and
sessions that ended without a harvest are picked up at the next start."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PKG / "core" / "scripts"))
import harvest as hv  # noqa: E402

SCRIPT = PKG / "core" / "scripts" / "harvest.py"

GOTCHA = json.dumps({"candidates": [{
    "kind": "lesson",
    "happened": "The integration tests hung until the fake payment server was started on port 8125.",
    "text": "Start the fake payment server on port 8125 before running the integration tests; they hang otherwise.",
    "applies_to": "integration tests",
}]})
SAME_GOTCHA_REWORDED = json.dumps({"candidates": [{
    "kind": "lesson",
    "happened": "Integration tests hung because the fake payment server on port 8125 was not started.",
    "text": "Before the integration tests, start the fake payment server on port 8125, or the tests hang.",
}]})
RULE = json.dumps({"candidates": [{
    "kind": "instruction", "target": "CLAUDE.md",
    "text": "Run database migrations through make migrate, never by calling alembic directly.",
}]})


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOIN_ARTIFACT_ROOT", raising=False)
    root = tmp_path / "proj"
    (root / ".workflow_artifacts" / "memory").mkdir(parents=True)
    return root


def _inbox(proj):
    return (proj / ".workflow_artifacts" / "memory" / "inbox.md").read_text()


def test_reply_becomes_one_pending_candidate(proj):
    out = hv.add(str(proj), "s1", "feat/pay", "compaction", "Here you go:\n" + GOTCHA)
    assert out == {"added": [1], "dropped": 0}
    [entry] = hv.list_entries(str(proj))
    assert (entry["kind"], entry["status"], entry["task"], entry["session"], entry["source"]) == (
        "lesson", "pending", "feat/pay", "s1", "compaction")
    assert entry["target"] == ".workflow_artifacts/memory/lessons-learned.md"
    assert "port 8125" in entry["text"]


def test_repeating_the_gotcha_adds_nothing(proj):
    hv.add(str(proj), "s1", "", "compaction", GOTCHA)
    assert hv.add(str(proj), "s2", "", "deferred", SAME_GOTCHA_REWORDED) == {"added": [], "dropped": 1}
    assert len(hv.list_entries(str(proj), include_all=True)) == 1


def test_a_lesson_already_learned_is_not_added(proj):
    (proj / ".workflow_artifacts" / "memory" / "lessons-learned.md").write_text(
        "# Lessons\n\n## 2026-11-01 — pay\n**Lesson:** Start the fake payment server on port 8125 "
        "before running the integration tests; they hang otherwise.\n")
    assert hv.add(str(proj), "s1", "", "pr", GOTCHA)["added"] == []


def test_none_and_malformed_replies_add_nothing(proj):
    assert hv.add(str(proj), "s1", "", "pr", '{"candidates": []}')["added"] == []
    assert hv.add(str(proj), "s2", "", "pr", "I found nothing worth keeping.")["added"] == []
    assert hv.add(str(proj), "s3", "", "pr", '{"candidates": [{"kind": "secret", "text": "x"}]}')["added"] == []
    assert not (proj / ".workflow_artifacts" / "memory" / "inbox.md").exists()


def test_nothing_reaches_always_loaded_files_without_accept(proj):
    hv.add(str(proj), "s1", "", "compaction", GOTCHA)
    hv.add(str(proj), "s1", "", "compaction", RULE)
    assert not (proj / ".workflow_artifacts" / "memory" / "lessons-learned.md").exists()
    assert not (proj / "CLAUDE.md").exists()
    assert len(hv.list_entries(str(proj))) == 2


def test_accept_appends_in_the_lessons_format_and_reject_settles(proj):
    hv.add(str(proj), "s1", "feat/pay", "compaction", GOTCHA)
    hv.add(str(proj), "s1", "feat/pay", "compaction", RULE)
    assert hv.accept(str(proj), 1)["ok"]
    lessons = (proj / ".workflow_artifacts" / "memory" / "lessons-learned.md").read_text()
    assert "— feat/pay\n**What happened:** The integration tests hung" in lessons
    assert "**Lesson:** Start the fake payment server" in lessons
    assert "**Applies to:** integration tests" in lessons
    assert hv.reject(str(proj), 2)["ok"]
    assert not (proj / "CLAUDE.md").exists()
    assert hv.list_entries(str(proj)) == []
    assert "## 1 · accepted · lesson" in _inbox(proj) and "## 2 · rejected · instruction" in _inbox(proj)
    assert hv.accept(str(proj), 1)["ok"] is False


def test_accepted_instruction_goes_to_its_file_and_odd_targets_fall_back(proj):
    odd = json.dumps({"candidates": [{"kind": "instruction", "target": "../../etc/passwd",
                                      "text": "Always run make lint before committing changes."}]})
    hv.add(str(proj), "s1", "", "pr", odd)
    [entry] = hv.list_entries(str(proj))
    assert entry["target"] == "CLAUDE.md"
    assert hv.accept(str(proj), 1)["ok"]
    assert "Always run make lint" in (proj / "CLAUDE.md").read_text()


def _transcript(path, prompts):
    rows = []
    for i, text in enumerate(prompts):
        rows.append({"type": "user", "message": {"role": "user", "content": text}})
        rows.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}})
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": f"answer {i}"}]}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_pending_picks_idle_unharvested_sessions_with_enough_turns(proj, tmp_path):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    old = time.time() - 3600
    _transcript(tdir / "long.jsonl", [f"prompt {i}" for i in range(6)])
    _transcript(tdir / "short.jsonl", ["one", "two"])
    _transcript(tdir / "current.jsonl", [f"p{i}" for i in range(9)])
    _transcript(tdir / "fresh.jsonl", [f"p{i}" for i in range(9)])
    for name in ("long", "short", "current"):
        os.utime(tdir / f"{name}.jsonl", (old, old))
    hv._save_ledger(proj / ".workflow_artifacts", {"since": old - 60, "sessions": {}})

    due = hv.pending(str(proj), str(tdir), "current")
    assert [d["session"] for d in due] == ["long"]
    ledger = hv._load_ledger(proj / ".workflow_artifacts")
    assert ledger["sessions"]["short"]["result"] == "skipped-short"

    hv.add(str(proj), "long", "", "deferred", '{"candidates": []}')
    assert hv.pending(str(proj), str(tdir), "current") == []


def test_first_run_does_not_reach_back_into_old_history(proj, tmp_path):
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _transcript(tdir / "ancient.jsonl", [f"p{i}" for i in range(8)])
    week_ago = time.time() - 7 * 86400
    os.utime(tdir / "ancient.jsonl", (week_ago, week_ago))
    assert hv.pending(str(proj), str(tdir), "x") == []


def test_compaction_summaries_and_command_echoes_are_not_turns(tmp_path):
    path = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "real prompt"}},
            {"type": "user", "message": {"role": "user", "content": "<command-name>/compact</command-name>"}},
            {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued"}}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert hv.user_turns(path) == 1
    assert "continued" not in hv.transcript_text(path)


def test_transcript_text_keeps_the_conversation(tmp_path):
    path = tmp_path / "t.jsonl"
    _transcript(path, ["fix the build", "and the docs"])
    text = hv.transcript_text(path)
    assert "Person: fix the build" in text and "Assistant: answer 1" in text
    assert "tool_result" not in text


def test_command_line(proj):
    run = lambda *a, stdin="": subprocess.run(  # noqa: E731
        [sys.executable, str(SCRIPT), *a], input=stdin, capture_output=True, text=True, check=True).stdout
    assert "Reply with JSON only" in run("question")
    assert json.loads(run("add", "--cwd", str(proj), "--session", "c1", "--source", "pr", stdin=GOTCHA))["added"] == [1]
    assert len(json.loads(run("list", "--cwd", str(proj)))) == 1
    assert json.loads(run("reject", "--cwd", str(proj), "--id", "1"))["ok"] is True
