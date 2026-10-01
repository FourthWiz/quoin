#!/usr/bin/env python3
"""plan_tasks.py - deterministic pending-task check for a plan's `## Tasks` section.

Usage: plan_tasks.py status --plan PATH

Prints exactly one line and exits with a matching code:
  ALLDONE|<total>                  exit 0  at least one task line, all done
  PENDING|<n>|<comma-separated ids> exit 1  at least one task is not done
  UNKNOWN|<reason>                 exit 2  file unreadable, no `## Tasks`
                                           section, zero task lines, usage error

A task counts as done only when its status glyph is a check mark (U+2713 or
U+2705). Every other glyph, no glyph, or disagreeing prefix and postfix
glyphs read as pending, so an unrecognised plan shape can only cause an
extra dispatch, never a skipped one. Callers key on the stdout prefix.

Only unindented lines inside the `## Tasks` section (up to the next `## `
heading, fenced code blocks skipped) can be task lines, and a task line needs
a prefix (heading marker, list number, bullet, checkbox or a leading glyph).
Stdlib only; no writes, no network.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from typing import List, NamedTuple, Optional

DONE_GLYPHS = ("✓", "✅")
_VS16 = "️"

_GLYPH = r"[^\sA-Za-z0-9*`\[\]#-]{1,3}"
_TASK_RE = re.compile(
    r"^(?P<prefix>(?:#{2,4}\s+|\d+[.)]\s+|[-*+]\s+))?"
    r"(?P<checkbox>\[[ xX]\]\s+)?"
    r"(?:(?P<glyph>" + _GLYPH + r")\s+)?"
    r"(?:\*\*|`)?"
    r"T-(?P<num>\d+)(?P<suf>[a-z])?(?=\W|$)"
    r"(?:\*\*|`)?:?\s*(?P<post>" + _GLYPH + r")?"
)
_STATUS_LINE = re.compile(
    r"^(?:#{2,4}\s+|\d+[.)]\s+|[-*+]\s+)?(?:\[[ xX]\]\s+)?"
    r"(?:✓|✅|⏳|🚫|✗|❌)"
)
_TASKS_HEADING = re.compile(r"^##\s+Tasks\s*$")
_NEXT_HEADING = re.compile(r"^##\s")
_FENCE = re.compile(r"^\s*(```|~~~)")


class PlanStatus(NamedTuple):
    kind: str
    total: int
    pending_ids: List[str]
    reason: str


def _norm(glyph: Optional[str]) -> Optional[str]:
    if glyph is None:
        return None
    return glyph.replace(_VS16, "")


def _status_only(glyph: Optional[str]) -> Optional[str]:
    """Keep a postfix glyph only when every char is a symbol (category So),
    so dashes, brackets and arrows after the task id are not read as status."""
    norm = _norm(glyph)
    if norm and all(unicodedata.category(ch) == "So" for ch in norm):
        return norm
    return None


def scan_plan_text(text: str) -> PlanStatus:
    in_tasks = False
    seen_heading = False
    in_fence = False
    ambiguous = False
    done_by_id: dict = {}
    order: List[str] = []
    for line in text.splitlines():
        if _FENCE.match(line):
            if in_tasks:
                in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not in_tasks:
            if _TASKS_HEADING.match(line):
                if seen_heading:
                    ambiguous = True
                in_tasks = True
                seen_heading = True
            continue
        if _NEXT_HEADING.match(line):
            in_tasks = False
            continue
        m = _TASK_RE.match(line)
        if not m:
            if _STATUS_LINE.match(line):
                ambiguous = True
            continue
        if not (m.group("prefix") or m.group("checkbox") or m.group("glyph")):
            continue
        tid = "T-" + m.group("num") + (m.group("suf") or "")
        lead = _norm(m.group("glyph"))
        post = _status_only(m.group("post"))
        eff = lead if lead is not None else post
        done = eff in DONE_GLYPHS and (post is None or post in DONE_GLYPHS)
        if lead is not None and post is not None and lead != post and not (
            lead in DONE_GLYPHS and post in DONE_GLYPHS
        ):
            done = False
        if tid not in done_by_id:
            order.append(tid)
            done_by_id[tid] = done
        else:
            done_by_id[tid] = done_by_id[tid] and done
    if not seen_heading:
        return PlanStatus("UNKNOWN", 0, [], "no Tasks section")
    if not order:
        return PlanStatus("UNKNOWN", 0, [], "no task lines")
    if ambiguous or in_fence:
        return PlanStatus("UNKNOWN", 0, [], "ambiguous tasks section")
    pending = [t for t in order if not done_by_id[t]]
    if pending:
        return PlanStatus("PENDING", len(order), pending, "")
    return PlanStatus("ALLDONE", len(order), [], "")


def _clean(line: str) -> str:
    cleaned = "".join(ch for ch in line if ch.isprintable())
    return cleaned.encode("utf-8")[:400].decode("utf-8", "ignore")


def _emit(line: str) -> None:
    sys.stdout.write(_clean(line) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "status" or args[1] != "--plan":
        _emit("UNKNOWN|usage")
        return 2
    try:
        with open(args[2], "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        _emit("UNKNOWN|plan unreadable")
        return 2
    status = scan_plan_text(text)
    if status.kind == "ALLDONE":
        _emit("ALLDONE|%d" % status.total)
        return 0
    if status.kind == "PENDING":
        _emit("PENDING|%d|%s" % (len(status.pending_ids), ",".join(status.pending_ids)))
        return 1
    _emit("UNKNOWN|" + status.reason)
    return 2


if __name__ == "__main__":
    sys.exit(main())
