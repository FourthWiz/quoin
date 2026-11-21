#!/usr/bin/env python3
"""Harvest reusable lessons and instruction edits from sessions into an inbox.

One question is asked of a session (``QUESTION``): in a live session by the
capture mod at compaction and when a PR is opened, and for sessions that ended
without it (killed, read-only, or run where mods cannot load) over their
transcripts at the next session start. The answers are candidates, written to
``.workflow_artifacts/memory/inbox.md`` with their kind, task and source
session. Nothing reaches ``lessons-learned.md``, ``CLAUDE.md`` or
``.claude/rules/`` until a candidate is accepted.

Subcommands (JSON on stdout unless noted):

  question            print the harvest question (plain text)
  add                 parse a model reply (stdin) into candidates and add the
                      new ones; near-duplicates of inbox or lessons entries are
                      dropped; marks the session harvested
  pending             transcripts of this project not yet harvested
  transcript-text     a transcript reduced to the conversation (plain text)
  mark                record a session as harvested or skipped
  list                inbox entries (pending only unless --all)
  accept / reject     settle one inbox entry; accept appends it to its target
                      through append_artifact.py

Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from lessons_guard import _jaccard, _keywords  # noqa: E402
from path_resolve import resolve_artifact_root  # noqa: E402

MIN_TURNS = 5
IDLE_SECONDS = 15 * 60
MAX_PER_START = 3
FIRST_RUN_LOOKBACK = 24 * 3600
TRANSCRIPT_CHARS = 80_000
DEFAULT_DUP_THRESHOLD = 0.5
INBOX_HEADER = (
    "# Inbox\n\n"
    "Candidates harvested from sessions. Nothing here is in effect until accepted; "
    "accept or reject them with /quoin-inbox.\n"
)
TARGETS = {"lesson": ".workflow_artifacts/memory/lessons-learned.md"}
INSTRUCTION_TARGETS = re.compile(r"^(CLAUDE\.md|\.claude/rules/[A-Za-z0-9._-]+\.md)$")

QUESTION = """Look back over this session for anything worth keeping beyond it:

- lesson: a reusable engineering takeaway the session showed (a gotcha that cost time, a non-obvious pattern in this codebase, the reason for a decision that will be forgotten) that would help a later task in this project;
- instruction: a standing rule for agents working in this project, for CLAUDE.md or a file under .claude/rules/.

Leave out task progress, one-off facts, personal preferences and anything obvious from the code. Most sessions have nothing; then say so.

Give each fact once: a standing rule as an instruction only, anything else as a lesson only.

Reply with JSON only, no prose:
{"candidates": [{"kind": "lesson", "happened": "<one sentence: what happened>", "text": "<the takeaway>", "applies_to": "<where it matters>"}, {"kind": "instruction", "target": "CLAUDE.md", "text": "<the rule as it should read>"}]}
or {"candidates": []}"""


KNOWN_ITEMS = 40
KNOWN_CHARS = 240


def question(cwd: str | None = None) -> str:
    """The harvest question, listing what the project already has so it is not offered again."""
    if cwd is None:
        return QUESTION
    art = artifacts(cwd)
    known = [e["text"] for e in parse_inbox(_read_inbox(art))]
    for body in _lessons_bodies(art):
        lesson = re.search(r"\*\*Lesson:\*\*\s*(.+)", body)
        known.append(lesson.group(1) if lesson else " ".join(body.split()))
    known = [" ".join(k.split())[:KNOWN_CHARS] for k in known if k.strip()][-KNOWN_ITEMS:]
    if not known:
        return QUESTION
    listed = "\n".join(f"- {k}" for k in known)
    return (
        f"{QUESTION}\n\nAlready recorded for this project. Do not offer these again, "
        f"in these words or others, as a lesson or as an instruction:\n{listed}"
    )


# --- locations -------------------------------------------------------------


def artifacts(cwd: str | os.PathLike) -> Path:
    return resolve_artifact_root(cwd) / ".workflow_artifacts"


def inbox_path(art: Path) -> Path:
    return art / "memory" / "inbox.md"


def _ledger_path(art: Path) -> Path:
    return art / ".capture" / "harvested.json"


def _load_ledger(art: Path) -> dict:
    try:
        data = json.loads(_ledger_path(art).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"since": time.time() - FIRST_RUN_LOOKBACK, "sessions": {}}


def _save_ledger(art: Path, ledger: dict) -> None:
    path = _ledger_path(art)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not (path.parent / ".gitignore").exists():
        (path.parent / ".gitignore").write_text("*\n", encoding="utf-8")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    tmp.replace(path)


def mark(cwd: str, session: str, result: str) -> None:
    art = artifacts(cwd)
    ledger = _load_ledger(art)
    ledger["sessions"][session] = {"at": time.time(), "result": result}
    _save_ledger(art, ledger)


# --- inbox -----------------------------------------------------------------

_ENTRY_RE = re.compile(r"^## (\d+) · (pending|accepted|rejected) · (lesson|instruction)\s*$", re.M)


def parse_inbox(text: str) -> list[dict]:
    entries = []
    matches = list(_ENTRY_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip("\n")
        fields = dict(re.findall(r"^- ([A-Za-z ]+): (.*)$", body, re.M))
        prose = re.sub(r"^- [A-Za-z ]+: .*$\n?", "", body, flags=re.M).strip()
        entries.append({
            "id": int(m.group(1)),
            "status": m.group(2),
            "kind": m.group(3),
            "task": fields.get("Task", ""),
            "session": fields.get("Session", ""),
            "source": fields.get("Source", ""),
            "target": fields.get("Target", ""),
            "happened": fields.get("Happened", ""),
            "applies_to": fields.get("Applies to", ""),
            "text": prose,
            "span": (m.start(), end),
        })
    return entries


def _read_inbox(art: Path) -> str:
    try:
        return inbox_path(art).read_text(encoding="utf-8")
    except OSError:
        return ""


def _lessons_bodies(art: Path) -> list[str]:
    try:
        text = (art / "memory" / "lessons-learned.md").read_text(encoding="utf-8")
    except OSError:
        return []
    return [part for part in re.split(r"^## ", text, flags=re.M) if part.strip()]


def _threshold() -> float:
    try:
        return float(os.environ.get("QUOIN_INBOX_DUP_THRESHOLD", DEFAULT_DUP_THRESHOLD))
    except ValueError:
        return DEFAULT_DUP_THRESHOLD


def parse_reply(reply: str) -> list[dict]:
    """Candidates from a model reply; anything malformed yields none."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(reply[start:end + 1])
    except ValueError:
        return []
    out = []
    for item in data.get("candidates") or [] if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        text = " ".join(str(item.get("text") or "").split())
        if kind not in ("lesson", "instruction") or len(text) < 12:
            continue
        cand = {"kind": kind, "text": text[:1200]}
        if kind == "lesson":
            cand["target"] = TARGETS["lesson"]
            cand["happened"] = " ".join(str(item.get("happened") or "").split())[:400]
            cand["applies_to"] = " ".join(str(item.get("applies_to") or "").split())[:200]
        else:
            target = str(item.get("target") or "CLAUDE.md").strip()
            cand["target"] = target if INSTRUCTION_TARGETS.match(target) else "CLAUDE.md"
        out.append(cand)
    return out


def _task_of(art: Path, session: str) -> str:
    try:
        import task_record

        state = task_record.load_state(art, session) or {}
    except Exception:  # noqa: BLE001 - the task is a label, not required
        return ""
    return state.get("key") or ""


def add(cwd: str, session: str, task: str, source: str, reply: str) -> dict:
    art = artifacts(cwd)
    task = task or _task_of(art, session)
    text = _read_inbox(art)
    existing = parse_inbox(text)
    seen = [_keywords(e["text"] + " " + e["happened"]) for e in existing]
    seen += [_keywords(body) for body in _lessons_bodies(art)]
    threshold = _threshold()
    next_id = max((e["id"] for e in existing), default=0) + 1
    added, dropped = [], 0
    blocks = []
    for cand in parse_reply(reply):
        words = _keywords(cand["text"] + " " + cand.get("happened", ""))
        if not words or any(_jaccard(words, other) >= threshold for other in seen):
            dropped += 1
            continue
        seen.append(words)
        lines = [f"## {next_id} · pending · {cand['kind']}", "", f"- Task: {task or 'none'}",
                 f"- Session: {session}", f"- Source: {source}", f"- Target: {cand['target']}"]
        if cand.get("happened"):
            lines.append(f"- Happened: {cand['happened']}")
        if cand.get("applies_to"):
            lines.append(f"- Applies to: {cand['applies_to']}")
        lines += ["", cand["text"], ""]
        blocks.append("\n".join(lines))
        added.append(next_id)
        next_id += 1
    if blocks:
        path = inbox_path(art)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = text if text.strip() else INBOX_HEADER
        if not body.endswith("\n"):
            body += "\n"
        path.write_text(body + "\n" + "\n".join(blocks), encoding="utf-8")
    mark(cwd, session, f"{source}:{len(added)}")
    return {"added": added, "dropped": dropped}


def list_entries(cwd: str, *, include_all: bool = False) -> list[dict]:
    entries = parse_inbox(_read_inbox(artifacts(cwd)))
    out = [{k: v for k, v in e.items() if k != "span"} for e in entries]
    return out if include_all else [e for e in out if e["status"] == "pending"]


def _set_status(art: Path, entry_id: int, status: str) -> dict | None:
    text = _read_inbox(art)
    for entry in parse_inbox(text):
        if entry["id"] == entry_id:
            start = entry["span"][0]
            line_end = text.index("\n", start) if "\n" in text[start:] else len(text)
            new_head = f"## {entry_id} · {status} · {entry['kind']}"
            inbox_path(art).write_text(text[:start] + new_head + text[line_end:], encoding="utf-8")
            entry["status"] = status
            return entry
    return None


def _render(entry: dict) -> str:
    if entry["kind"] == "lesson":
        date = _dt.date.today().isoformat()
        parts = [f"## {date} — {entry['task'] if entry['task'] not in ('', 'none') else 'session'}"]
        if entry["happened"]:
            parts.append(f"**What happened:** {entry['happened']}")
        parts.append(f"**Lesson:** {entry['text']}")
        if entry["applies_to"]:
            parts.append(f"**Applies to:** {entry['applies_to']}")
        return "\n".join(parts)
    return entry["text"]


def accept(cwd: str, entry_id: int) -> dict:
    art = artifacts(cwd)
    entry = next((e for e in parse_inbox(_read_inbox(art)) if e["id"] == entry_id), None)
    if entry is None or entry["status"] != "pending":
        return {"ok": False, "error": f"no pending inbox entry {entry_id}"}
    target = entry["target"] or TARGETS.get(entry["kind"], "CLAUDE.md")
    if entry["kind"] == "instruction" and not INSTRUCTION_TARGETS.match(target):
        return {"ok": False, "error": f"refusing target {target}"}
    dest = art.parent / target
    cmd = [sys.executable, str(_HERE / "append_artifact.py"), "--blank-line", "--text", _render(entry), str(dest)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip() or "append failed"}
    _set_status(art, entry_id, "accepted")
    return {"ok": True, "id": entry_id, "target": str(dest)}


def reject(cwd: str, entry_id: int) -> dict:
    entry = _set_status(artifacts(cwd), entry_id, "rejected")
    return {"ok": entry is not None, "id": entry_id}


# --- transcripts -----------------------------------------------------------


def _rows(path: Path):
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for raw in handle:
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


_NOT_PROMPTS = ("<system-reminder", "<local-command", "<command-", "<task-notification")


def _is_prompt(row: dict) -> bool:
    """A row the person typed: not a tool result, a compaction summary or a command echo."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain") or row.get("isCompactSummary"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = _text_of(content).strip()
    return bool(text) and not text.startswith(_NOT_PROMPTS)


def user_turns(path: Path) -> int:
    return sum(1 for row in _rows(path) if _is_prompt(row))


def transcript_text(path: Path, limit: int = TRANSCRIPT_CHARS) -> str:
    parts = []
    for row in _rows(path):
        kind = row.get("type")
        if kind == "user" and not _is_prompt(row):
            continue
        if kind != "user" and (kind != "assistant" or row.get("isSidechain")):
            continue
        text = _text_of((row.get("message") or {}).get("content")).strip()
        if not text:
            continue
        parts.append(f"{'Person' if kind == 'user' else 'Assistant'}: {text}")
    joined = "\n\n".join(parts)
    return joined[-limit:]


def _idle_seconds() -> float:
    try:
        return float(os.environ.get("QUOIN_HARVEST_IDLE_SECONDS", IDLE_SECONDS))
    except ValueError:
        return IDLE_SECONDS


def pending(cwd: str, transcript_dir: str, current: str, *, now: float | None = None) -> list[dict]:
    """Transcripts in ``transcript_dir`` worth harvesting now, newest first."""
    art = artifacts(cwd)
    ledger = _load_ledger(art)
    now = now or time.time()
    found = []
    for path in Path(transcript_dir).glob("*.jsonl"):
        sid = path.stem
        if sid == current or sid in ledger["sessions"]:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < ledger["since"] or now - mtime < _idle_seconds():
            continue
        found.append((mtime, sid, path))
    found.sort(reverse=True)
    out = []
    for _mtime, sid, path in found:
        if user_turns(path) < MIN_TURNS:
            ledger["sessions"][sid] = {"at": now, "result": "skipped-short"}
            continue
        out.append({"session": sid, "path": str(path)})
        if len(out) >= MAX_PER_START:
            break
    _save_ledger(art, ledger)
    return out


# --- command line ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("question")
    p.add_argument("--cwd", default=None, help="list what this project already has")
    p = sub.add_parser("add")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--session", required=True)
    p.add_argument("--task", default="")
    p.add_argument("--source", required=True, choices=["compaction", "pr", "deferred"])
    p = sub.add_parser("pending")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--transcript-dir", required=True)
    p.add_argument("--current", default="")
    p = sub.add_parser("transcript-text")
    p.add_argument("--path", required=True)
    p = sub.add_parser("mark")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--session", required=True)
    p.add_argument("--result", default="harvested")
    p = sub.add_parser("list")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--all", action="store_true")
    for name in ("accept", "reject"):
        p = sub.add_parser(name)
        p.add_argument("--cwd", default=os.getcwd())
        p.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "question":
        print(question(args.cwd))
        return 0
    if args.cmd == "transcript-text":
        print(transcript_text(Path(args.path)))
        return 0
    if args.cmd == "add":
        out = add(args.cwd, args.session, args.task, args.source, sys.stdin.read())
    elif args.cmd == "pending":
        out = pending(args.cwd, args.transcript_dir, args.current)
    elif args.cmd == "mark":
        mark(args.cwd, args.session, args.result)
        out = {"ok": True}
    elif args.cmd == "list":
        out = list_entries(args.cwd, include_all=args.all)
    elif args.cmd == "accept":
        out = accept(args.cwd, args.id)
    else:
        out = reject(args.cwd, args.id)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
