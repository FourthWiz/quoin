#!/usr/bin/env python3
# CLAUDE-ADAPTER-OWNED — this file locates, prices, and attributes cost for
# nested Claude Code subagent transcripts. Do NOT import this module from any
# file in quoin/core/. The portable cost-event schema (parse_attribution,
# CostEvent, format_row) is at quoin/core/scripts/cost_event.py. Adding
# runtime-neutral functionality belongs there, not here.
#
# agent_transcript_cost.py — resolves the on-disk nested subagent transcript
# for a given top-level session UUID (sid) + agentId (primary) or toolUseId
# (secondary), prices it by reusing cost_from_jsonl.py's parser (no
# re-implemented token->cost math), and produces the col-8 attribution
# micro-map string consumed by core.cost_event.parse_attribution:
#   "usd=<float>;tok=<int>;src=nested_jsonl"   — fully priced
#   "tok=<int>;src=unresolved"                 — located + flushed, unpriceable
#   "src=unresolved"                           — not located / not flushed
#
# On-disk layout (confirmed by the S-2 spike, see
# .workflow_artifacts/ivg-111-cost-attribution/stage-2/spike-findings.md):
#   ~/.claude/projects/<project-hash>/<sid>/subagents/agent-<agentId>.jsonl
#   ~/.claude/projects/<project-hash>/<sid>/subagents/agent-<agentId>.meta.json
# The sidecar .meta.json's "model" key is a SHORT ALIAS ("sonnet"/"haiku"/
# "opus"), NOT a PRICES key — it is never consulted for pricing here (see
# stage-2/current-plan.md D-2). "spawnDepth" is absolute from the top level,
# never a direct-child filter.
#
# Fail-open everywhere: every public function in this module returns a safe
# sentinel (None / False / "src=unresolved" / the empty-priced dict) instead
# of raising, so a resolver failure never crashes the spawning orchestrator.

import importlib.util
import json
import os
import pathlib
import sys


def _load_sibling(name: str):
    """Load a sibling script from the same directory via spec_from_file_location.

    Copied verbatim (same-dir sibling pattern) from dashboard_cost.py. A bare
    `from cost_from_jsonl import ...` would pass the test suite (tests inject
    SCRIPTS_DIR onto sys.path) but raise ModuleNotFoundError in the deployed
    flat ~/.claude/scripts/ dir, which has no package structure (see
    stage-2/current-plan.md R-S2d / D-1).
    """
    path = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        raise ImportError(f"Cannot load {name}: {path} not found")
    module_key = f"_agent_transcript_cost_{name}"
    if module_key in sys.modules:
        return sys.modules[module_key]
    spec = importlib.util.spec_from_file_location(module_key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {name} at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


_cfj = _load_sibling("cost_from_jsonl")
PRICES = _cfj.PRICES
parse_session = _cfj.parse_session
project_hash = _cfj.project_hash
cost_for_entry = _cfj.cost_for_entry
is_priced = _cfj.is_priced


# ---------------------------------------------------------------------------
# Locate (T-02)
# ---------------------------------------------------------------------------

def subagents_dir(project_path=None, sid=None, home=None):
    """Return <home>/.claude/projects/<project_hash(project_path)>/<sid>/subagents.

    project_path defaults to os.getcwd(); home defaults to pathlib.Path.home().
    Both are overridable so callers (and tests) can pin a fixture tree, mirroring
    cost_from_jsonl.jsonl_path_for's `home` param.
    """
    project_path = project_path if project_path is not None else os.getcwd()
    home = home or pathlib.Path.home()
    return home / ".claude" / "projects" / project_hash(project_path) / sid / "subagents"


def resolve_by_agent_id(sid, agent_id, project_path=None, home=None):
    """PRIMARY resolver: build subagents_dir/agent-<agent_id>.jsonl and return it
    iff it exists, else None. Deterministic — no mtime, no window; immune to
    depth-mixing because agentId names the exact file (spawnDepth is absolute,
    never a filter). Fail-open: any error -> None, never raise."""
    try:
        aid = agent_id
        if aid and aid.startswith("agent-"):
            aid = aid[len("agent-"):]
        d = subagents_dir(project_path=project_path, sid=sid, home=home)
        candidate = d / f"agent-{aid}.jsonl"
        return candidate if candidate.exists() else None
    except (OSError, AttributeError, TypeError):
        return None


def resolve_by_tooluse(sid, tool_use_id, project_path=None, home=None):
    """SECONDARY resolver: scan subagents_dir/*.meta.json for a sidecar whose
    toolUseId == tool_use_id; return the paired agent-<id>.jsonl. tool_use_id
    is unique per spawn -> at most one match expected; 0 or >1 (ambiguous) ->
    None, never guess. No spawnDepth filter. Fail-open: any error -> None."""
    try:
        d = subagents_dir(project_path=project_path, sid=sid, home=home)
        if not d.exists() or not d.is_dir():
            return None
        matches = []
        for meta_path in sorted(d.glob("*.meta.json")):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(meta, dict) and meta.get("toolUseId") == tool_use_id:
                stem = meta_path.name
                if stem.endswith(".meta.json"):
                    jsonl_name = stem[: -len(".meta.json")] + ".jsonl"
                    matches.append(meta_path.parent / jsonl_name)
        if len(matches) != 1:
            return None
        jf = matches[0]
        return jf if jf.exists() else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Pricer (T-03)
# ---------------------------------------------------------------------------

def price_agent_jsonl(path):
    """Return {"usd": float|None, "tok": int, "priceable": bool, "models": [...]}
    for a nested subagent transcript. Reuses parse_session (ccusage-parity,
    per-line malformed-tolerant) for the primary pass; the known-model set is
    derived from parse_session's already-tolerant `entries` (truthy-model rows
    only), so only ONE additional raw scan is needed — for the model-less
    guard, since model-less usage rows are invisible in `entries`. That scan
    mirrors parse_session's per-line JSONDecodeError-skip so a single junk
    line cannot flip an otherwise-priceable transcript to unresolved.

    usd is None (never 0.0-as-if-real) whenever not priceable. tok is always
    parse_session's totalTokens (durable, kept even when unpriceable).
    Fail-open: missing file / read error -> the all-empty sentinel dict,
    never raise."""
    try:
        s = parse_session(path)
    except (OSError, UnicodeDecodeError):
        return {"usd": None, "tok": 0, "priceable": False, "models": []}

    models = sorted({e["model"] for e in s["entries"]})

    modelless = False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # one junk line must NOT flip the transcript
                msg = row.get("message")
                if isinstance(msg, dict) and msg.get("usage") and not msg.get("model"):
                    modelless = True
                    break
    except (OSError, UnicodeDecodeError):
        # Fail-open on the guard-only rescan: priceability still determined by
        # parse_session's already-successful primary pass above.
        pass

    # IVG-249 root-cause note: the locate path (resolve_attribution above) was
    # never broken. Sidecar attribution returned "src=unresolved" purely
    # because a model seen in the transcript wasn't resolvable via
    # is_priced (e.g. the Claude 5 family before this stage added it, or a
    # normalizable slug before IVG-260's resolver) — this `all(is_priced ...)`
    # check is where that absence flips `priceable` False and drops `usd`.
    priceable = (
        (bool(s["entries"]) or s.get("sentinel_rows", 0) > 0)
        and all(is_priced(m) for m in models)
        and not modelless
    )
    return {
        "usd": s["totalCost"] if priceable else None,
        "tok": s["totalTokens"],
        "priceable": priceable,
        "models": models,
    }


# ---------------------------------------------------------------------------
# Flush guard (T-04)
# ---------------------------------------------------------------------------

def last_row_usage_present(path):
    """R-08 guard for the Agent-return race: a child transcript may not have
    flushed its final usage row yet when the parent orchestrator reads it.

    Return False if the file is missing/empty, or if the last non-empty line
    fails to parse as JSON (truncated / mid-write). Otherwise require >=1
    message.usage row present ANYWHERE in the file. Deliberately does NOT
    require the last LINE to be a usage row — real completed transcripts
    legitimately end on a control/result row (over-rejecting would manufacture
    false unresolved; spike confirms completed files always carry usage).
    Fail-open: any OSError -> False (treat as not-yet-flushed)."""
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return False
        last_line = None
        has_usage = False
        with open(p, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                last_line = raw
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg = row.get("message")
                if isinstance(msg, dict) and msg.get("usage"):
                    has_usage = True
        if last_line is None:
            return False  # empty file (no non-empty lines)
        try:
            json.loads(last_line)
        except json.JSONDecodeError:
            return False  # truncated / mid-write
        return has_usage
    except (OSError, UnicodeDecodeError):
        return False


# ---------------------------------------------------------------------------
# Public entry (T-05)
# ---------------------------------------------------------------------------

def resolve_attribution(sid, agent_id=None, tool_use_id=None, project_path=None, home=None):
    """Compose locate -> flush-guard -> price into the col-8 attribution
    micro-map string. The single public entry the orchestrator-wiring stage
    calls. Never writes a ledger row and never mutates state (pure). Always
    returns a valid, ';'-delimited k=v micro-map string (never raises).

    Output invariants (match the stage-1 core schema): keys usd?/tok?/src!,
    no pipes, no spaces; usd rounded to <=6 dp; tok is a bare int."""
    try:
        jf = resolve_by_agent_id(sid, agent_id, project_path, home) if agent_id else None
        if jf is None and tool_use_id:
            jf = resolve_by_tooluse(sid, tool_use_id, project_path, home)
        if jf is None:
            return "src=unresolved"  # no transcript located
        if not last_row_usage_present(jf):
            return "src=unresolved"  # R-08 flush/truncation guard
        r = price_agent_jsonl(jf)
        if r["priceable"] and r["usd"] is not None:
            return f"usd={round(r['usd'], 6)};tok={r['tok']};src=nested_jsonl"
        if r["tok"] > 0:
            return f"tok={r['tok']};src=unresolved"  # unknown-model: keep tok, no usd
        return "src=unresolved"
    except Exception:
        return "src=unresolved"  # fail-open: never crash the spawning orchestrator


# ---------------------------------------------------------------------------
# CLI entrypoint (T-01, stage 3) — mirrors get_session_uuid.py's fail-open
# shell-out contract so orchestrator SKILL.md one-liners can call this as a
# subprocess. ALWAYS exits 0; never raises; never prints a traceback to
# stdout. On any error (including a missing/garbage --sid) prints exactly
# "src=unresolved" and returns 0.
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    try:
        ap = argparse.ArgumentParser()
        ap.add_argument("--sid", required=True)
        ap.add_argument("--agent-id", default=None)
        ap.add_argument("--tool-use-id", default=None)
        ap.add_argument("--project-path", default=None)
        a = ap.parse_args(argv)
        print(resolve_attribution(a.sid, agent_id=a.agent_id,
              tool_use_id=a.tool_use_id, project_path=a.project_path))
    except SystemExit:
        # argparse error (e.g. missing --sid): still fail-open, never crash
        # the calling one-liner.
        print("src=unresolved")
    except Exception:
        print("src=unresolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
