"""
session_state.py — Terminal-state precedence and session-error mapping for the
SWE-bench Verified path.

The legacy cells keep their own verdict logic; this module lets the Verified
path honour the CLI's budget-halt result subtype and tell an authentication
failure apart from a model failure, so neither is scored as a failed fix.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

PRECEDENCE = ["timeout", "budget_halt", "session_error"]
SYNTHETIC_MODEL = "<synthetic>"
AUTH_TEXT = "Not logged in"
BUDGET_ERROR_TEXT = "Reached maximum budget ($"


@dataclass
class SessionOutcome:
    terminal_state: str  # completed | timeout | budget_stopped | error
    verdict_if_terminal: Optional[str]
    failure_reason: Optional[str]
    session_errored: bool
    judge_required: bool
    api_key_source: Optional[str] = None
    genuine_assistant_turns: int = 0
    auth_text_seen: bool = False
    result_seen: bool = False
    result_subtype: Optional[str] = None
    terminal_reason: Optional[str] = None
    result_is_error: bool = False
    skipped_lines: int = 0
    precedence: list = field(default_factory=lambda: list(PRECEDENCE))

    def to_dict(self) -> dict:
        return asdict(self)


def iter_transcript_events(path, stats: Optional[dict] = None) -> Iterator[dict]:
    """Yield parsed events from a streamed transcript, skipping unparsable lines.

    A transcript cut off mid-write ends in a partial JSON line; those lines are
    counted in `stats["skipped_lines"]` instead of raising.
    """
    if stats is None:
        stats = {}
    stats.setdefault("skipped_lines", 0)
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                stats["skipped_lines"] += 1
                continue
            if isinstance(event, dict):
                yield event
            else:
                stats["skipped_lines"] += 1


def _text_blocks(message: dict) -> Iterator[str]:
    content = message.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    yield text


def classify_session(
    cell_verdict: Optional[str],
    cell_extra: Optional[dict],
    events_iter: Iterable,
    skipped_lines: int = 0,
) -> SessionOutcome:
    cell_extra = cell_extra or {}
    api_key_source = None
    genuine = 0
    auth_seen = False
    result: Optional[dict] = None
    for event in events_iter:
        if not isinstance(event, dict):
            skipped_lines += 1
            continue
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            api_key_source = event.get("apiKeySource", api_key_source)
        elif etype == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("model") != SYNTHETIC_MODEL:
                genuine += 1
            if any(AUTH_TEXT in t for t in _text_blocks(message)):
                auth_seen = True
        elif etype == "result":
            result = event

    result = result or {}
    subtype = result.get("subtype")
    terminal_reason = result.get("terminal_reason")
    is_error = bool(result.get("is_error"))
    errors = result.get("errors") or []
    result_seen = bool(result)

    def outcome(state, verdict, reason, judge=False):
        return SessionOutcome(
            terminal_state=state, verdict_if_terminal=verdict, failure_reason=reason,
            session_errored=is_error, judge_required=judge,
            api_key_source=api_key_source, genuine_assistant_turns=genuine,
            auth_text_seen=auth_seen, result_seen=result_seen, result_subtype=subtype,
            terminal_reason=terminal_reason, result_is_error=is_error,
            skipped_lines=skipped_lines,
        )

    if cell_verdict == "timeout":
        return outcome("timeout", "timeout", cell_extra.get("failure_reason"))
    budget_hit = (
        cell_verdict == "budget_stopped"
        or subtype == "error_max_budget_usd"
        or terminal_reason == "budget_exhausted"
        or any(isinstance(e, str) and BUDGET_ERROR_TEXT in e for e in errors)
    )
    if budget_hit:
        return outcome("budget_stopped", "budget_stopped", "budget-halt-detected")
    if cell_verdict == "error":
        return outcome("error", "error", cell_extra.get("failure_reason"))
    if genuine == 0:
        if auth_seen or (api_key_source == "none" and is_error):
            reason = "auth-failure"
        else:
            reason = "no-assistant-turn"
        return outcome("error", "error", reason)
    return outcome("completed", None, None, judge=True)


def write_session_json(t_dir, payload: dict) -> Path:
    t_dir = Path(t_dir)
    t_dir.mkdir(parents=True, exist_ok=True)
    data = {"schema_version": 1, "patch_file": "diff.patch"}
    data.update(payload)
    tmp = t_dir / "session.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, t_dir / "session.json")
    return t_dir / "session.json"


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_session_json(t_dir) -> Optional[dict]:
    return _read_json(Path(t_dir) / "session.json")


def is_judge_pending(t_dir) -> bool:
    session = read_session_json(t_dir)
    if not session or not session.get("judge_required"):
        return False
    judge = _read_json(Path(t_dir) / "judge.json")
    return not (judge and judge.get("judge_final") is True)
