#!/usr/bin/env python3
"""Measure how a /run orchestrator's context grows phase by phase.

Dev tooling only: not deployed, no network, no model call, stdlib only. It
reads local Claude Code transcripts (read-only) and writes nothing except the
two files named by --out-json and --out-md.

For every main-session transcript that contains a /run start it:

* splits the transcript into /run spans and tags each span autonomous or
  interactive from positive evidence only (the --autonomous token, an
  autonomous-run marker write, or a tool result that shows the marker's own
  "autonomous: true" line);
* finds the inline gate segments (a Read of the gate SKILL.md or a Skill(gate)
  call, up to the audit-log finalize), the fast-route triage segments, the
  checkpoint summary turns and the spawned phases;
* prices each inline segment as it ran (actual) and as a same-tier subagent
  would have run it (counterfactual), charging the orchestrator turns a spawn
  adds (launch, wait, verdict and ledger turns) and the returned summary.

The saving is a modelled counterfactual on recorded usage rows, not a measured
re-run. Typical use (from the git root):

    .venv/bin/python quoin/dev/scripts/run_context_census.py \\
        --exclude-session <current-session-id> --sensitivity \\
        --out-json census.json --out-md census.md
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_ROOT = HERE.parents[1]  # <git-root>/quoin
COST_SCRIPT = SRC_ROOT / "scripts" / "cost_from_jsonl.py"

RUN_TAG = "<command-name>/run</command-name>"
NOTIFICATION = "<task-notification>"
ASYNC_ACK = "Async agent launched"
SPAWN_TOOLS = ("Agent", "Task")
PHASE_KEYWORDS = ("end_of_task", "thorough_plan", "implement", "review",
                  "architect", "specify", "enrich", "discover", "gate")
COMPACTION_DROP = 0.30
MIN_SEGMENTS = 10
MIN_GATE_SUBAGENTS = 10
MIN_GATE_SPAWNS = 10
DEFAULT_RETURN_TOKENS = 2000
# Anthropic's prompt cache lives for at most an hour between hits; a spawn
# whose return arrives later than this re-writes the orchestrator's prefix.
CACHE_LIFETIME_MINUTES = 60
DEFAULT_M_BEFORE = 2
DEFAULT_M_AFTER = 1
DECLARED_GATE_MODEL = "sonnet"
FALLBACK_MODEL = "opus"
CATEGORIES = ("post-implement", "post-review", "gate-other",
              "fast-route-triage")
STRATA = ("all", "autonomous", "interactive")

AGENT_ID_RE = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
TASK_ID_RE = re.compile(r"<task-id>\s*([A-Za-z0-9_-]+)\s*</task-id>")
# Subagent hand-backs delivered as a message from the subagent session.
AGENT_MESSAGE_RE = re.compile(r'<agent-message from="([A-Za-z0-9_-]+)"')
GATE_LOG_RE = re.compile(r"gate-[^/\"'\s]*\.md$")


# ---------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Prices:
    """Per-model price ratios in input-token equivalents."""

    def __init__(self, cost_module=None):
        self.mod = cost_module or _load_module("_census_cost", COST_SCRIPT)
        self.fallback_turns = 0
        self.fallback_row = self.mod.resolve_prices(
            self.mod.ALIAS_TO_MODEL[FALLBACK_MODEL])

    def row(self, model, count=True):
        found = self.mod.resolve_prices(model)
        if found is None:
            if count:
                self.fallback_turns += 1
            return self.fallback_row
        return found

    def ratios(self, model, count=False):
        row = self.row(model, count=count)
        inp = row["input"]
        return {"w": row["cache_create"] / inp, "r": row["cache_read"] / inp,
                "o": row["output"] / inp, "input": inp}

    def declared(self, alias):
        return self.mod.resolve_prices(self.mod.ALIAS_TO_MODEL[alias])


# ---------------------------------------------------------------------------
# transcript parsing
# ---------------------------------------------------------------------------

class Turn:
    __slots__ = ("pos", "msg_id", "model", "inp", "cc", "cr", "out",
                 "tool_uses", "texts", "ts")

    def __init__(self, pos, msg_id, model, usage, ts):
        self.pos = pos
        self.msg_id = msg_id
        self.model = model
        self.inp = int(usage.get("input_tokens") or 0)
        self.cc = int(usage.get("cache_creation_input_tokens") or 0)
        self.cr = int(usage.get("cache_read_input_tokens") or 0)
        self.out = int(usage.get("output_tokens") or 0)
        self.tool_uses = []
        self.texts = []
        self.ts = ts

    @property
    def ctx(self):
        return self.inp + self.cc + self.cr

    def bash_commands(self):
        return [str((t.get("input") or {}).get("command") or "")
                for t in self.tool_uses if t.get("name") == "Bash"]

    def text(self):
        return "\n".join(self.texts)


class UserEvent:
    __slots__ = ("pos", "texts", "results", "notification_id", "ts")

    def __init__(self, pos, ts):
        self.pos = pos
        self.texts = []
        self.results = []  # (tool_use_id, text)
        self.notification_id = None
        self.ts = ts

    @property
    def human(self):
        return bool(self.texts) or self.notification_id is not None


def _block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_transcript(path: Path, dedup=True, sidechain=False):
    """Return (events, malformed_lines). Events are Turn, UserEvent or the
    string "compact", in file order. Sidechain entries are skipped in a main
    transcript; a subagent transcript is all sidechain, so it keeps them."""
    events = []
    malformed = 0
    by_id = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(e, dict) or (e.get("isSidechain") and not sidechain):
                continue
            etype = e.get("type")
            if etype == "system" and e.get("subtype") == "compact_boundary":
                events.append("compact")
                continue
            msg = e.get("message") or {}
            content = msg.get("content")
            ts = _parse_ts(e.get("timestamp"))
            if etype == "assistant":
                usage = msg.get("usage")
                model = msg.get("model") or ""
                if not usage or model == "<synthetic>":
                    continue
                mid = msg.get("id") or e.get("uuid")
                turn = by_id.get(mid) if dedup else None
                if turn is None:
                    turn = Turn(len(events), mid, model, usage, ts)
                    by_id[mid] = turn
                    events.append(turn)
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        turn.tool_uses.append({"name": b.get("name"),
                                               "id": b.get("id"),
                                               "input": b.get("input") or {}})
                    elif b.get("type") == "text":
                        turn.texts.append(str(b.get("text") or ""))
            elif etype == "user":
                ev = UserEvent(len(events), ts)
                if isinstance(content, str):
                    ev.texts.append(content)
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_result":
                            ev.results.append((b.get("tool_use_id"),
                                               _block_text(b.get("content"))))
                        elif b.get("type") == "text":
                            ev.texts.append(str(b.get("text") or ""))
                joined = "\n".join(ev.texts)
                hand_back = AGENT_MESSAGE_RE.search(joined)
                if joined.lstrip().startswith(NOTIFICATION):
                    m = TASK_ID_RE.search(joined)
                    ev.notification_id = m.group(1) if m else ""
                    ev.texts = []
                elif hand_back:
                    ev.notification_id = hand_back.group(1)
                    ev.texts = []
                events.append(ev)
    return events, malformed


def start_form(ev):
    """'tag' or 'plain' if this user event starts a /run, else None."""
    if not isinstance(ev, UserEvent) or ev.notification_id is not None:
        return None
    text = "\n".join(ev.texts)
    if RUN_TAG in text:
        return "tag"
    stripped = text.strip()
    if stripped == "/run" or stripped.startswith("/run "):
        return "plain"
    return None


# ---------------------------------------------------------------------------
# run mode
# ---------------------------------------------------------------------------

def _names_marker(obj):
    blob = json.dumps(obj, sort_keys=True)
    return "autonomous-run-" in blob and ".marker" in blob


def span_mode(events, lo, hi, widen=False):
    """Return (mode, evidence) for events[lo:hi]; events[lo] is the start."""
    if "--autonomous" in "\n".join(events[lo].texts):
        return "autonomous", "token"
    marker_uses = set()
    for ev in events[lo:hi]:
        if not isinstance(ev, Turn):
            continue
        for tu in ev.tool_uses:
            if tu.get("name") == "Bash":
                cmd = str(tu["input"].get("command") or "")
                if ("write-atomic" in cmd and "autonomous-run-" in cmd
                        and ".marker" in cmd):
                    return "autonomous", "marker-write"
            if _names_marker(tu.get("input")):
                marker_uses.add(tu.get("id"))
    if widen and marker_uses:
        return "autonomous", "marker-content"
    for ev in events[lo:hi]:
        if not isinstance(ev, UserEvent):
            continue
        for tuid, text in ev.results:
            if tuid in marker_uses and any(
                    ln.strip() == "autonomous: true"
                    for ln in text.splitlines()):
                return "autonomous", "marker-content"
    return "interactive", "none"


# ---------------------------------------------------------------------------
# subagents
# ---------------------------------------------------------------------------

def _first_tool_uses(path: Path, limit_turns=40):
    """Return (turns, tool_uses) from the head of a subagent transcript."""
    events, _ = parse_transcript(path, sidechain=True)
    turns = [e for e in events if isinstance(e, Turn)][:limit_turns]
    uses = [tu for t in turns for tu in t.tool_uses]
    return turns, uses


def _reads_gate_skill(tu):
    return (tu.get("name") == "Read" and str(
        tu["input"].get("file_path") or "").endswith("skills/gate/SKILL.md"))


class SubagentIndex:
    """Lazy view of <projects>/<project>/<session>/subagents/."""

    def __init__(self, gate_by_read_only=False):
        self.cache = {}
        self.gate_by_read_only = gate_by_read_only

    def info(self, path: Path):
        key = str(path)
        if key in self.cache:
            return self.cache[key]
        turns, uses = _first_tool_uses(path)
        first = uses[0] if uses else None
        phase = None
        for tu in uses:
            if tu.get("name") == "Skill":
                phase = str(tu["input"].get("skill") or "").strip("/") or None
                break
        if self.gate_by_read_only:
            is_gate = bool(first) and _reads_gate_skill(first)
        else:
            is_gate = bool(first) and (
                (first.get("name") == "Skill"
                 and str(first["input"].get("skill") or "").strip("/") == "gate")
                or _reads_gate_skill(first))
        if phase is None and is_gate:
            phase = "gate"
        out = {"turns": turns, "first_ctx": turns[0].ctx if turns else None,
               "phase": phase, "is_gate": is_gate}
        self.cache[key] = out
        return out


def load_meta(session_dir: Path):
    """Map toolUseId -> agent id from agent-*.meta.json files."""
    out = {}
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return out
    for meta in sorted(sub.glob("agent-*.meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tuid = data.get("toolUseId")
        if tuid:
            out[tuid] = meta.name[len("agent-"):-len(".meta.json")]
    return out


def keyword_phase(text):
    for kw in PHASE_KEYWORDS:
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(kw) + r"(?![A-Za-z0-9_])",
                     text, re.IGNORECASE):
            return kw
    return "other"


# ---------------------------------------------------------------------------
# analysis of one transcript
# ---------------------------------------------------------------------------

def _is_stopper(turn):
    text = turn.text()
    if "Phase complete:" in text:
        return True
    for tu in turn.tool_uses:
        if tu.get("name") in SPAWN_TOOLS or tu.get("name") == "AskUserQuestion":
            return True
    return any("run_state.py --write" in c for c in turn.bash_commands())


def _gate_end_marker(turn, legacy_end=False):
    """Return the audit-log path when this turn finalizes a gate log."""
    if legacy_end:
        for c in turn.bash_commands():
            if "run_state.py --write" in c:
                return c
        return None
    for c in turn.bash_commands():
        if "fsops.py finalize" in c and ("gate-implement-" in c
                                         or "gate-review-" in c):
            return c
    for tu in turn.tool_uses:
        if tu.get("name") == "Write":
            fp = str(tu["input"].get("file_path") or "")
            if GATE_LOG_RE.search(fp):
                return fp
    return None


def _opens_gate(turn):
    for tu in turn.tool_uses:
        name = tu.get("name")
        if name == "Read" and str(tu["input"].get("file_path") or "").endswith(
                ".claude/skills/gate/SKILL.md"):
            return True
        if name == "Skill" and str(tu["input"].get("skill") or "").strip(
                "/") == "gate":
            return True
    return False


def _opens_triage(turn):
    for tu in turn.tool_uses:
        blob = json.dumps(tu.get("input"), sort_keys=True)
        if "triage-decision.md" in blob:
            return True
        if tu.get("name") == "Read" and "/triage" in str(
                tu["input"].get("file_path") or ""):
            return True
    return False


def _label_from_path(text):
    if "gate-implement-" in text:
        return "post-implement"
    if "gate-review-" in text:
        return "post-review"
    return None


class Options:
    def __init__(self, **kw):
        self.dedup = kw.get("dedup", True)
        self.compaction_aware = kw.get("compaction_aware", True)
        self.legacy_end = kw.get("legacy_end", False)
        self.force_interactive = kw.get("force_interactive", False)
        self.widen_marker = kw.get("widen_marker", False)
        self.return_at_ack = kw.get("return_at_ack", False)
        self.gate_by_read_only = kw.get("gate_by_read_only", False)
        self.drop_overhead = kw.get("drop_overhead", False)


def analyse(path: Path, rel: str, subagents: SubagentIndex, opts: Options):
    events, malformed = parse_transcript(path, dedup=opts.dedup)
    starts = [(i, start_form(e)) for i, e in enumerate(events)
              if start_form(e)]
    row = {"file": rel, "session": path.stem, "malformed": malformed,
           "selected": bool(starts), "spans": [], "segments": [],
           "spawns": [], "summaries": [], "setup": None, "turns": 0,
           "compactions": 0}
    if not starts:
        return row, events
    turns = [e for e in events if isinstance(e, Turn)]
    row["turns"] = len(turns)

    # compaction points: positions where a new context begins
    compact_pos = []
    prev = None
    for e in events:
        if e == "compact":
            compact_pos.append(None)
        elif isinstance(e, Turn):
            if compact_pos and compact_pos[-1] is None:
                compact_pos[-1] = e.pos
            elif prev is not None and prev.ctx > 0 and \
                    e.ctx < prev.ctx * (1 - COMPACTION_DROP):
                compact_pos.append(e.pos)
            prev = e
    compact_pos = [p for p in compact_pos if p is not None]
    row["compactions"] = len(compact_pos)

    # spans
    span_of = {}
    bounds = [s for s, _ in starts] + [len(events)]
    for k, (s, form) in enumerate(starts):
        hi = bounds[k + 1]
        if opts.force_interactive:
            mode, evidence = "interactive", "none"
        else:
            mode, evidence = span_mode(events, s, hi, widen=opts.widen_marker)
        row["spans"].append({"start": s, "end": hi, "form": form,
                             "mode": mode, "evidence": evidence,
                             "ts": events[s].ts.isoformat()
                             if events[s].ts else None})
        for p in range(s, hi):
            span_of[p] = k

    def mode_at(pos):
        k = span_of.get(pos)
        return row["spans"][k]["mode"] if k is not None else None

    def remaining_turns(pos):
        stop = len(events)
        if opts.compaction_aware:
            later = [p for p in compact_pos if p > pos]
            if later:
                stop = later[0]
        return sum(1 for t in turns if pos < t.pos < stop)

    # results and notifications
    results = {}
    notif = {}
    for e in events:
        if isinstance(e, UserEvent):
            for tuid, text in e.results:
                results.setdefault(tuid, (e, text))
            if e.notification_id:
                notif.setdefault(e.notification_id, e)
    meta = load_meta(path.parent / path.stem)

    # spawns
    spawns = []
    for t in turns:
        for tu in t.tool_uses:
            if tu.get("name") not in SPAWN_TOOLS:
                continue
            res = results.get(tu.get("id"))
            agent_id = None
            ret = None
            ack = None
            if res and ASYNC_ACK in res[1]:
                m = AGENT_ID_RE.search(res[1])
                agent_id = m.group(1) if m else None
                ack = res[0]
                ret = notif.get(agent_id) if agent_id else None
            else:
                agent_id = meta.get(tu.get("id"))
                ret = res[0] if res else None
            phase = None
            if agent_id:
                sp = path.parent / path.stem / "subagents" / f"agent-{agent_id}.jsonl"
                if sp.is_file():
                    phase = subagents.info(sp)["phase"]
            linked = phase is not None
            if phase is None:
                inp = tu.get("input") or {}
                first_line = str(inp.get("prompt") or "").splitlines()[:1]
                phase = keyword_phase(" ".join(
                    [str(inp.get("description") or "")] + first_line))
            ret_at = ack if (opts.return_at_ack and ack is not None) else ret
            growth = None
            if ret_at is not None:
                before = [x for x in turns if x.pos < ret_at.pos]
                after = [x for x in turns if x.pos > ret_at.pos]
                if before and after:
                    growth = after[0].ctx - before[-1].ctx
            gap = None
            if ret is not None and ret.ts and t.ts:
                gap = (ret.ts - t.ts).total_seconds() / 60.0
            spawns.append({"pos": t.pos, "agent_id": agent_id, "phase": phase,
                           "linked": linked, "async": ack is not None,
                           "return_pos": ret.pos if ret else None,
                           "return_growth": growth, "gap_minutes": gap,
                           "mode": mode_at(t.pos),
                           "description": str((tu.get("input") or {}).get(
                               "description") or "")})
    spawn_pos = sorted(s["pos"] for s in spawns)
    claimed = set()

    # gate spawn overhead turns
    for sp in spawns:
        if sp["phase"] != "gate" or sp["mode"] is None:
            continue
        before, after = [], []
        stop_first = None
        for e in events[sp["pos"]:]:
            if sp["return_pos"] is not None and e is events[sp["return_pos"]]:
                break
            if isinstance(e, UserEvent) and e.human:
                stop_first = e.pos
                break
            if isinstance(e, Turn):
                before.append(e)
        if sp["return_pos"] is not None:
            window = []
            for e in events[sp["return_pos"] + 1:]:
                if not isinstance(e, Turn):
                    continue
                txt = e.text()
                if ("Phase complete:" in txt or "Checkpoint" in txt
                        or any(tu.get("name") in SPAWN_TOOLS + ("AskUserQuestion",)
                               for tu in e.tool_uses)):
                    break
                window.append(e)
            last = -1
            for k, e in enumerate(window):
                for c in e.bash_commands():
                    if ("agent_transcript_cost.py" in c or "cost-ledger.md" in c
                            or (sp["agent_id"] and sp["agent_id"] in c)):
                        last = k
            after = window[:last + 1]
        if stop_first is None and sp["return_pos"] is None:
            before = before[:1]
        sp["overhead_before"] = [x.pos for x in before]
        sp["overhead_after"] = [x.pos for x in after]
        sp["overhead_turns"] = [(x.inp, x.cc, x.cr, x.out, x.model)
                                for x in before + after]
        claimed.update(sp["overhead_before"] + sp["overhead_after"])

    # inline gate and triage segments
    latest_phase = []
    for sp in spawns:
        if sp["phase"] in ("implement", "review"):
            latest_phase.append((sp["pos"], sp["phase"]))

    def segments(opener, kind):
        out = []
        open_seg = None
        for t in turns:
            if t.pos in claimed and open_seg is None:
                continue
            k_span = span_of.get(t.pos)
            if open_seg is not None and span_of.get(t.pos) != open_seg["span"]:
                open_seg["truncated"] = True
                out.append(open_seg)
                open_seg = None
            if open_seg is None:
                if k_span is not None and opener(t):
                    open_seg = {"kind": kind, "span": k_span, "turns": [t],
                                "truncated": False, "end_marker": None}
                    if kind == "gate":
                        mk = _gate_end_marker(t, opts.legacy_end)
                        if mk:
                            open_seg["end_marker"] = mk
                            out.append(open_seg)
                            open_seg = None
                continue
            if kind == "gate":
                mk = _gate_end_marker(t, opts.legacy_end)
                if mk:
                    open_seg["turns"].append(t)
                    open_seg["end_marker"] = mk
                    out.append(open_seg)
                    open_seg = None
                    continue
                if not opts.legacy_end and _is_stopper(t):
                    open_seg["end_marker"] = "stopper"
                    out.append(open_seg)
                    open_seg = None
                    continue
            else:
                if _is_stopper(t) and (
                        any("run_state.py --write" in c for c in t.bash_commands())
                        or any(tu.get("name") in SPAWN_TOOLS + ("AskUserQuestion",)
                               for tu in t.tool_uses)):
                    open_seg["end_marker"] = "stopper"
                    out.append(open_seg)
                    open_seg = None
                    continue
            open_seg["turns"].append(t)
        if open_seg is not None:
            open_seg["truncated"] = True
            out.append(open_seg)
        return out

    raw = segments(_opens_gate, "gate") + segments(_opens_triage, "triage")
    for seg in raw:
        ts = seg["turns"]
        first = ts[0]
        if seg["kind"] == "triage":
            label = "fast-route-triage"
        else:
            label = _label_from_path(seg["end_marker"] or "") \
                if not opts.legacy_end else None
            if label is None:
                prior = [p for pos, p in latest_phase if pos < first.pos]
                label = {"implement": "post-implement",
                         "review": "post-review"}.get(
                    prior[-1] if prior else None, "gate-other")
        end_pos = ts[-1].pos
        claimed.update(x.pos for x in ts)
        row["segments"].append({
            "category": label, "mode": row["spans"][seg["span"]]["mode"],
            "truncated": seg["truncated"], "start_pos": first.pos,
            "end_pos": end_pos, "n_turns": len(ts),
            "start_ctx": first.ctx, "growth": ts[-1].ctx - first.ctx,
            "remaining_turns": remaining_turns(end_pos),
            "turns": [(x.inp, x.cc, x.cr, x.out, x.model) for x in ts],
            "start_ts": first.ts.isoformat() if first.ts else None})

    # summaries and setup
    for t in turns:
        if t.pos in claimed or t.tool_uses:
            continue
        if "Phase complete:" in t.text():
            row["summaries"].append({"pos": t.pos, "out": t.out,
                                     "mode": mode_at(t.pos)})
    setup = [t for t in turns if t.pos > starts[0][0]
             and (not spawn_pos or t.pos < spawn_pos[0])]
    if setup:
        row["setup"] = {"n_turns": len(setup),
                        "growth": setup[-1].ctx - setup[0].ctx}
    row["spawns"] = spawns
    return row, events


# ---------------------------------------------------------------------------
# cost model
# ---------------------------------------------------------------------------

def _median(xs, default=None):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else default


def _p75(xs, default=None):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return default
    if len(xs) == 1:
        return xs[0]
    return statistics.quantiles(xs, n=4, method="inclusive")[2]


def overhead_cost(c0, ratios, O, S):
    """Orchestrator cost a spawned gate adds, for a segment starting at c0."""
    if not O:
        return 0.0
    r, w = ratios["r"], ratios["w"]
    return (O["fresh"] * (O["m_before"] + O["m_after"])
            + r * (c0 + S) * O["m_after"] + r * c0 * O["m_before"]
            + O.get("long_share", 0.0) * (w - r) * (c0 + S))


def segment_costs(seg, prices: Prices, B, P, S, O, declared_ratio=1.0,
                  o_scale=1.0):
    """Return (actual, counterfactual, counterfactual_declared)."""
    ts = seg["turns"]
    ratios = prices.ratios(ts[0][4])
    w, r = ratios["w"], ratios["r"]
    c0 = seg["start_ctx"]
    G = seg["growth"]
    R = seg["remaining_turns"]
    actual = 0.0
    sub_sum = 0.0
    for inp, cc, cr, _out, model in ts:
        tr = prices.ratios(model)
        actual += inp + tr["w"] * cc + tr["r"] * cr
        sub_sum += inp + tr["w"] * cc + tr["r"] * max(0, cr - (c0 - B))
    actual += r * G * R
    orch = o_scale * overhead_cost(c0, ratios, O, S) + w * S + r * S * R
    sub = w * B + P + sub_sum
    return actual, sub + orch, sub * declared_ratio + orch


# ---------------------------------------------------------------------------
# corpus
# ---------------------------------------------------------------------------

def _rel(path: Path, root: Path):
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def gate_subagent_inputs(root: Path, excluded, subagents: SubagentIndex,
                         prices: Prices):
    gate_first, all_first, p_costs = [], [], []
    for sp in sorted(root.glob("*/*/subagents/agent-*.jsonl")):
        if sp.parent.parent.name in excluded:
            continue
        info = subagents.info(sp)
        if info["first_ctx"] is None:
            continue
        all_first.append(info["first_ctx"])
        if not info["is_gate"]:
            continue
        gate_first.append(info["first_ctx"])
        cost = 0.0
        for t in info["turns"]:
            blob = json.dumps(t.tool_uses, sort_keys=True)
            if not ("preamble.md" in blob or "dispatch_config.py" in blob
                    or "worktree_isolation.py" in blob):
                continue
            tr = prices.ratios(t.model)
            cost += t.inp + tr["w"] * t.cc + tr["r"] * t.cr
        p_costs.append(cost)
    fallback = len(gate_first) < MIN_GATE_SUBAGENTS
    pool = all_first if fallback else gate_first
    return {"B": _median(pool, 0), "B_p75": _p75(pool, 0),
            "B_n": len(pool), "gate_subagents": len(gate_first),
            "B_fallback": fallback, "P": _median(p_costs, 0.0),
            "P_n": len(p_costs)}


def overhead_inputs(rows, prices: Prices):
    gate_spawns = [s for r in rows for s in r["spawns"]
                   if s["phase"] == "gate" and s["mode"] is not None
                   and "overhead_turns" in s]
    all_spawns = [s for r in rows for s in r["spawns"]]
    S_vals = [s["return_growth"] for s in gate_spawns]
    S = _median(S_vals)
    S_fallback = S is None
    if S is None:
        S = DEFAULT_RETURN_TOKENS

    def fresh(turn):
        inp, cc, _cr, out, model = turn
        tr = prices.ratios(model)
        return inp + tr["w"] * cc + tr["o"] * out

    per_spawn_fresh = [statistics.mean(fresh(t) for t in s["overhead_turns"])
                       for s in gate_spawns if s["overhead_turns"]]
    long_share = (sum(1 for s in gate_spawns if s["gap_minutes"] is not None
                      and s["gap_minutes"] > CACHE_LIFETIME_MINUTES)
                  / len(gate_spawns)) if gate_spawns else 0.0
    if len(gate_spawns) >= MIN_GATE_SPAWNS:
        O = {"fresh": _median(per_spawn_fresh, 0.0),
             "m_before": _median([len(s["overhead_before"]) for s in gate_spawns]),
             "m_after": _median([len(s["overhead_after"]) for s in gate_spawns]),
             "long_share": long_share, "n": len(gate_spawns), "fallback": False}
    else:
        follow = [s.get("follow_fresh") for s in all_spawns
                  if s.get("follow_fresh") is not None]
        O = {"fresh": _median(follow, _median(per_spawn_fresh, 0.0)),
             "m_before": DEFAULT_M_BEFORE, "m_after": DEFAULT_M_AFTER,
             "long_share": long_share, "n": len(gate_spawns), "fallback": True}
    return S, S_fallback, len([v for v in S_vals if v is not None]), O


def aggregate(segs, prices, inputs, declared_ratio_for, conservative):
    complete = [s for s in segs if not s["truncated"]]
    rows = []
    for s in complete:
        dr = declared_ratio_for(s["turns"][0][4])
        a, c, cd = segment_costs(s, prices, inputs["B"], inputs["P"],
                                 inputs["S"], inputs["O"], dr)
        cons = None
        if conservative:
            _, cc_, _ = segment_costs(s, prices, inputs["B_p75"], inputs["P"],
                                      2 * inputs["S"], inputs["O"], dr,
                                      o_scale=2.0)
            cons = a - cc_
        rows.append((s, a, c, cd, cons))
    out = {"segments": len(complete),
           "truncated": len(segs) - len(complete)}
    if not rows:
        return out
    savings = [a - c for _, a, c, _, _ in rows]
    rel = [(a - c) / a if a else 0.0 for _, a, c, _, _ in rows]
    out.update({
        "median_turns": _median([s["n_turns"] for s, *_ in rows]),
        "median_start_ctx": _median([s["start_ctx"] for s, *_ in rows]),
        "median_growth": _median([s["growth"] for s, *_ in rows]),
        "median_actual": _median([a for _, a, *_ in rows]),
        "median_cf_same": _median([c for _, _, c, _, _ in rows]),
        "median_cf_declared": _median([cd for _, _, _, cd, _ in rows]),
        "median_saving": _median(savings),
        "median_relative": _median(rel),
        "share_positive": sum(1 for x in savings if x > 0) / len(savings),
    })
    if conservative:
        out["median_saving_conservative"] = _median([r[4] for r in rows])
    return out


def rule(agg):
    """Numeric conditions (a)-(d) of the pre-registered decision rule."""
    a = agg.get("segments", 0) >= MIN_SEGMENTS
    b = (agg.get("median_relative") or 0) >= 0.15
    c = (agg.get("share_positive") or 0) >= 0.75
    d = (agg.get("median_saving_conservative") or 0) > 0
    return {"a_segments": a, "b_relative": b, "c_share": c,
            "d_conservative": d, "numeric_pass": a and b and c and d}


def run_census(root: Path, excluded=(), since=None, return_tokens=None,
               sensitivity=False, opts=None, cost_module=None):
    opts = opts or Options()
    prices = Prices(cost_module)
    subagents = SubagentIndex(gate_by_read_only=opts.gate_by_read_only)
    excluded = set(excluded)
    files = sorted(root.glob("*/*.jsonl"))
    rows = []
    corpus = {"files_scanned": 0, "files_selected": 0, "files_excluded": 0,
              "turns": 0, "compactions": 0, "malformed_lines": 0,
              "starts_tag": 0, "starts_plain": 0,
              "evidence": {"token": 0, "marker-write": 0,
                           "marker-content": 0, "none": 0}}
    for f in files:
        if f.stem in excluded:
            corpus["files_excluded"] += 1
            continue
        corpus["files_scanned"] += 1
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "/run" not in raw:
            continue
        row, events = analyse(f, _rel(f, root), subagents, opts)
        if not row["selected"]:
            continue
        if since and row["spans"][0]["ts"] and row["spans"][0]["ts"][:10] < since:
            continue
        turns = [e for e in events if isinstance(e, Turn)]
        for sp in row["spawns"]:
            after = [t for t in turns if t.pos > sp["pos"]]
            if after:
                t = after[0]
                tr = prices.ratios(t.model)
                sp["follow_fresh"] = t.inp + tr["w"] * t.cc + tr["o"] * t.out
        for t in turns:
            prices.row(t.model, count=True)
        corpus["files_selected"] += 1
        corpus["turns"] += row["turns"]
        corpus["compactions"] += row["compactions"]
        corpus["malformed_lines"] += row["malformed"]
        for sp in row["spans"]:
            corpus["starts_" + sp["form"]] += 1
            corpus["evidence"][sp["evidence"]] += 1
        rows.append(row)
    sub = gate_subagent_inputs(root, excluded, subagents, prices)
    S, S_fallback, S_n, O = overhead_inputs(rows, prices)
    if return_tokens is not None:
        S, S_fallback = return_tokens, False
    if opts.drop_overhead:
        O = None
    inputs = dict(sub, S=S, S_fallback=S_fallback, S_n=S_n, O=O)
    declared = prices.declared(DECLARED_GATE_MODEL)

    def declared_ratio_for(model):
        return declared["input"] / prices.row(model, count=False)["input"]

    segs = [s for r in rows for s in r["segments"]]
    aggregates = {}
    for stratum in STRATA:
        aggregates[stratum] = {}
        for cat in CATEGORIES:
            cs = [s for s in segs if s["category"] == cat
                  and (stratum == "all" or s["mode"] == stratum)]
            agg = aggregate(cs, prices, inputs, declared_ratio_for, sensitivity)
            if sensitivity:
                agg["rule"] = rule(agg)
            aggregates[stratum][cat] = agg
        summ = [s for r in rows for s in r["summaries"]
                if stratum == "all" or s["mode"] == stratum]
        aggregates[stratum]["summaries"] = {
            "turns": len(summ), "median_output": _median([s["out"] for s in summ])}
    growth = {"setup": _median([r["setup"]["growth"] for r in rows if r["setup"]])}
    for ph in sorted({s["phase"] for r in rows for s in r["spawns"]}):
        growth["return:" + ph] = _median([s["return_growth"] for r in rows
                                          for s in r["spawns"] if s["phase"] == ph])
    for cat in CATEGORIES:
        growth["segment:" + cat] = _median([s["growth"] for s in segs
                                            if s["category"] == cat])
    corpus["fallback_price_turns"] = prices.fallback_turns
    for r in rows:
        for s in r["segments"]:
            s.pop("turns", None)
        for s in r["spawns"]:
            s.pop("overhead_turns", None)
    return {"corpus": corpus, "inputs": inputs, "growth": growth,
            "aggregates": aggregates, "rows": rows}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def render_md(result, sensitivity):
    c, inp = result["corpus"], result["inputs"]
    O = inp.get("O") or {}
    lines = ["# /run orchestrator context census", "",
             "## Method", "",
             "Offline read of local Claude Code transcripts. Inline gate segments run from the "
             "gate SKILL.md Read (or Skill(gate)) to the audit-log finalize; later turns stay "
             "with the orchestrator. Costs are input-token equivalents at each turn's model "
             "price. The counterfactual prices the same work as a spawned subagent, including "
             "the orchestrator turns a spawn adds (O), the returned summary (S) and the "
             "subagent's own start context (B) and preamble work (P). It is a modelled "
             "counterfactual, not a measured re-run. The after-return term assumes the "
             f"prompt cache survives the wait (cache lifetime {CACHE_LIFETIME_MINUTES} minutes); "
             "spawns whose return took longer are charged a cache write.", "",
             "## Corpus", ""]
    for k in ("files_scanned", "files_excluded", "files_selected", "turns",
              "compactions", "malformed_lines", "starts_tag", "starts_plain",
              "fallback_price_turns"):
        lines.append(f"- {k}: {_fmt(c[k])}")
    lines.append("- spans by evidence: " + ", ".join(
        f"{k} {v}" for k, v in sorted(c["evidence"].items())))
    lines += ["", "## Inputs", "",
              f"- B (subagent first-turn context): {_fmt(inp['B'])} (n={inp['B_n']}, "
              f"gate subagents {inp['gate_subagents']}, fallback {_fmt(inp['B_fallback'])}); "
              f"p75 {_fmt(inp['B_p75'])}",
              f"- P (preamble and dispatch turns): {_fmt(inp['P'])} (n={inp['P_n']})",
              f"- S (gate return growth): {_fmt(inp['S'])} (n={inp['S_n']}, "
              f"fallback {_fmt(inp['S_fallback'])})"]
    if O:
        lines.append(f"- O: fresh/turn {_fmt(O['fresh'])}, m_before {_fmt(O['m_before'])}, "
                     f"m_after {_fmt(O['m_after'])}, long-wait share "
                     f"{_fmt(O['long_share'])} (gate spawns n={O['n']}, "
                     f"fallback {_fmt(O['fallback'])})")
    else:
        lines.append("- O: not charged")
    lines += ["", "## Growth by phase (median context tokens)", "",
              "| phase | median growth |", "|---|---|"]
    for k, v in result["growth"].items():
        lines.append(f"| {k} | {_fmt(v)} |")
    cols = ["segments", "truncated", "median_turns", "median_start_ctx",
            "median_growth", "median_actual", "median_cf_same",
            "median_cf_declared", "median_saving", "median_relative",
            "share_positive"]
    if sensitivity:
        cols.append("median_saving_conservative")
    for stratum in STRATA:
        lines += ["", f"## Cost by category ({stratum} runs)", "",
                  "| category | " + " | ".join(cols) + " |",
                  "|---|" + "---|" * len(cols)]
        for cat in CATEGORIES:
            agg = result["aggregates"][stratum][cat]
            lines.append(f"| {cat} | " + " | ".join(
                _fmt(agg.get(k)) for k in cols) + " |")
        sm = result["aggregates"][stratum]["summaries"]
        lines.append(f"\nSummaries: {sm['turns']} checkpoint turns, median output "
                     f"{_fmt(sm['median_output'])} tokens.")
        if sensitivity:
            lines += ["", "| category | (a) >=10 | (b) >=15% | (c) >=75% | (d) conservative > 0 |",
                      "|---|---|---|---|---|"]
            for cat in CATEGORIES:
                rl = result["aggregates"][stratum][cat].get("rule", {})
                lines.append(f"| {cat} | {_fmt(rl.get('a_segments'))} | "
                             f"{_fmt(rl.get('b_relative'))} | {_fmt(rl.get('c_share'))} | "
                             f"{_fmt(rl.get('d_conservative'))} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects-root", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--exclude-session", action="append", default=[])
    ap.add_argument("--since", help="YYYY-MM-DD, on the first /run start")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--return-tokens", type=int)
    ap.add_argument("--sensitivity", action="store_true")
    args = ap.parse_args(argv)
    result = run_census(Path(args.projects_root).expanduser(),
                        excluded=args.exclude_session, since=args.since,
                        return_tokens=args.return_tokens,
                        sensitivity=args.sensitivity)
    Path(args.out_json).write_text(json.dumps(result, sort_keys=True, indent=1,
                                              default=str) + "\n", encoding="utf-8")
    Path(args.out_md).write_text(render_md(result, args.sensitivity),
                                 encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
