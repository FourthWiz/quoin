#!/usr/bin/env python3
"""Measure the Claude adapter's always-on, per-session and per-prompt cost.

Dev tooling only: not deployed, no network, no model call. Stdlib only so it
runs on the system python3 (3.8) as well as the repo venv.

Typical use (from the git root):

    .venv/bin/python quoin/dev/scripts/adapter_baseline.py \\
        --ref 906e072f --label before \\
        --out-json baseline-before.json --out-md baseline.md

    .venv/bin/python quoin/dev/scripts/adapter_baseline.py \\
        --worktree --label after \\
        --out-json baseline-after.json --out-md baseline.md

`--ref` materialises the quoin package at a commit (hooks plus scripts) and
times those hooks; `--worktree` times the working tree. Hooks are never run
from ~/.claude. `--home-claude` (default ~/.claude) is read-only input for
footprints, the skill listing and the settings.json hook listing.

Every measured item carries one of four labels: always-on, per-session,
per-prompt, on-demand.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_ROOT = HERE.parents[1]  # <git-root>/quoin
GIT_ROOT = SRC_ROOT.parent
DEFAULT_REF = "906e072f"
LABELS = ("always-on", "per-session", "per-prompt", "on-demand")
LARGE_BYTES = 20 * 1024 * 1024

QUOIN_HOOK_BASENAMES = frozenset({
    "userpromptsubmit.sh", "precompact.sh", "postcompact.sh",
    "sessionstart.sh", "sessionend.sh", "stop.sh", "worktreecreate.sh",
})


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _tool_version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:  # noqa: BLE001 - version probes are best effort
        return "unavailable"


def _stats(samples_ms):
    s = sorted(samples_ms)
    n = len(s)

    def pct(p):
        return s[min(n - 1, max(0, int(round(p * (n - 1)))))]

    return {
        "n": n,
        "median_ms": round(statistics.median(s), 2),
        "p10_ms": round(pct(0.10), 2),
        "p90_ms": round(pct(0.90), 2),
        "min_ms": round(s[0], 2),
        "max_ms": round(s[-1], 2),
    }


def materialise(ref, worktree, tmp):
    """Return a directory laid out as <dir>/quoin/{hooks,scripts}."""
    dest = Path(tmp) / "pkg"
    dest.mkdir(parents=True)
    if worktree:
        for sub in ("hooks", "scripts", "core", "adapters/claude/skills"):
            src = SRC_ROOT / sub
            if src.is_dir():
                shutil.copytree(
                    str(src), str(dest / "quoin" / sub),
                    ignore=shutil.ignore_patterns("__pycache__"))
        return dest
    archive = subprocess.run(
        ["git", "-C", str(GIT_ROOT), "archive", ref, "quoin"],
        capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    # the archive of path "quoin" yields <dest>/quoin/<pkg>; the package lives in
    # a nested quoin/quoin directory in this repo layout
    nested = dest / "quoin" / "hooks"
    if not nested.is_dir():
        raise SystemExit("baseline: archive has no quoin/hooks at %s" % ref)
    return dest


def _jsonl_lines(target_bytes):
    """Realistic assistant/user transcript lines up to ~target_bytes."""
    user = json.dumps({"type": "user", "message": {"role": "user", "content": "please look at the file and explain " * 6}})
    asst = json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant", "model": "claude-sonnet-5",
            "usage": {"input_tokens": 120, "cache_read_input_tokens": 30000,
                      "cache_creation_input_tokens": 200, "output_tokens": 80},
            "content": [
                {"type": "text", "text": "Here is what I found in the file. " * 8},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/x/y.py"}},
            ],
        },
    })
    chunk = (user + "\n" + asst + "\n")
    return chunk, len(chunk.encode("utf-8"))


def write_transcript(path, target_bytes):
    chunk, size = _jsonl_lines(target_bytes)
    reps = max(1, target_bytes // size)
    with open(str(path), "w", encoding="utf-8") as fh:
        for _ in range(reps):
            fh.write(chunk)


class Fixture:
    def __init__(self, root, name, transcript_bytes, armed=False, subagent=False):
        self.name = name
        self.root = Path(root) / name
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.proj = self.root / "proj"
        self.mem = self.proj / ".workflow_artifacts" / "memory"
        for d in (self.home / ".claude", self.tmp, self.mem):
            d.mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        tdir = self.root / "transcripts"
        if subagent:
            tdir = tdir / "sid" / "subagents"
        tdir.mkdir(parents=True, exist_ok=True)
        self.transcript = tdir / "t.jsonl"
        write_transcript(self.transcript, transcript_bytes)
        self.sid = "bench-" + name
        if armed:
            (self.mem / ("run-continue-arm-%s.txt" % self.sid)).write_text("x", encoding="utf-8")

    def payload(self, source=None, prompt="please continue"):
        data = {"session_id": self.sid, "transcript_path": str(self.transcript),
                "cwd": str(self.proj), "prompt": prompt, "hook_event_name": "UserPromptSubmit"}
        if source:
            data["source"] = source
            data["hook_event_name"] = "SessionStart"
        return json.dumps(data)

    def env(self, fresh_tmp=None):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["TMPDIR"] = str(fresh_tmp or self.tmp)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["QUOIN_DISABLE_NESTED_ROOT_CHECK"] = "1"
        return env

    def rearm(self):
        (self.mem / ("run-continue-arm-%s.txt" % self.sid)).write_text("x", encoding="utf-8")


def run_hook(hook, payload, env, cwd):
    t0 = time.perf_counter()
    proc = subprocess.run(["sh", str(hook)], input=payload, capture_output=True,
                          text=True, env=env, cwd=str(cwd), timeout=120)
    return (time.perf_counter() - t0) * 1000.0, proc


def time_hook(hook, fx, runs, warmup, cold, rearm=False, source=None):
    samples = []
    last = None
    for i in range(runs + warmup):
        if rearm:
            fx.rearm()
        if cold:
            fresh = tempfile.mkdtemp(prefix="cold-", dir=str(fx.tmp.parent))
            env = fx.env(fresh)
        else:
            env = fx.env()
        ms, proc = run_hook(hook, fx.payload(source=source), env, fx.proj)
        last = proc
        if i >= warmup:
            samples.append(ms)
        if cold:
            shutil.rmtree(env["TMPDIR"], ignore_errors=True)
    out = _stats(samples)
    out["stdout_bytes"] = len((last.stdout or "").encode("utf-8")) if last else 0
    return out, last


# ---------------------------------------------------------------------------
# measurements
# ---------------------------------------------------------------------------

def measure_hooks(pkg, runs, warmup, work):
    hooks = pkg / "quoin" / "hooks"
    ups = hooks / "userpromptsubmit.sh"
    results = {}
    fixtures = {
        "idle": Fixture(work, "idle", 50 * 1024),
        "large": Fixture(work, "large", LARGE_BYTES),
        "armed": Fixture(work, "armed", 50 * 1024, armed=True),
        "subagent": Fixture(work, "subagent", 50 * 1024, subagent=True),
    }
    for name, fx in fixtures.items():
        cold, _ = time_hook(ups, fx, runs, warmup, cold=True, rearm=(name == "armed"))
        warm, proc = time_hook(ups, fx, runs, warmup, cold=False, rearm=(name == "armed"))
        results["prompt_" + name] = {
            "label": "per-prompt", "hook": "userpromptsubmit.sh", "fixture": name,
            "cold": cold, "warm": warm,
            "stdout_bytes": warm["stdout_bytes"],
            "transcript_bytes": fx.transcript.stat().st_size,
        }
    idle = fixtures["idle"]
    for hook_name, src in (("sessionstart.sh", "startup"), ("sessionend.sh", None)):
        hp = hooks / hook_name
        if not hp.is_file():
            results["session_" + hook_name] = {"label": "per-session", "error": "missing"}
            continue
        stats, proc = time_hook(hp, idle, runs, warmup, cold=False, source=src)
        stats["label"] = "per-session"
        stats["hook"] = hook_name
        stats["source"] = src
        results["session_" + hook_name] = stats
    return results, fixtures


def _additional_bytes(stdout):
    try:
        data = json.loads(stdout)
    except Exception:  # noqa: BLE001
        return {"stdout_bytes": len(stdout.encode("utf-8")), "additionalContext_bytes": 0, "systemMessage_bytes": 0}
    spec = data.get("hookSpecificOutput", {}) if isinstance(data, dict) else {}
    return {
        "stdout_bytes": len(stdout.encode("utf-8")),
        "additionalContext_bytes": len(str(spec.get("additionalContext", "")).encode("utf-8")),
        "systemMessage_bytes": len(str(data.get("systemMessage", "")).encode("utf-8")) if isinstance(data, dict) else 0,
    }


def measure_sessionstart_outputs(pkg, work):
    hook = pkg / "quoin" / "hooks" / "sessionstart.sh"
    out = {}
    if not hook.is_file():
        return out

    def one(name, source, setup=None):
        fx = Fixture(work, "ss-" + name, 4096)
        if setup:
            setup(fx)
        _, proc = run_hook(hook, fx.payload(source=source), fx.env(), fx.proj)
        rec = _additional_bytes(proc.stdout or "")
        rec["label"] = "per-session"
        out[name] = rec

    def run_state(fx):
        rec = {"schema": 1, "active": True, "task": "bench-task", "phase": "implement",
               "subphase": "tasks", "step": "3", "at_stage_boundary": False,
               "next_action": "/run --resume bench-task", "resume_command": "/run --resume bench-task",
               "session_id": fx.sid}
        (fx.mem / "run-state-bench-task.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")

    def stale_eod(fx):
        (fx.mem / "sessions").mkdir(exist_ok=True)
        (fx.mem / "sessions" / "2020-01-01-bench.md").write_text("## Cost\n- end_of_day_due: yes\n", encoding="utf-8")

    one("startup", "startup")
    one("resume", "resume")
    one("compact_reentry", "compact", run_state)
    one("missing_eod", "startup", stale_eod)
    out["run_continuation"] = {"label": "per-session", "note": "not measured: requires live supervisor"}
    return out


MARKER_START = "# === DEV WORKFLOW START ==="
MARKER_END = "# === DEV WORKFLOW END ==="


def marker_footprint(path):
    """Return (marker_block, pre_marker) sizes for a deployed rules file.

    marker_block spans the START line through the END line inclusive and is
    None when either marker is absent; pre_marker counts the chars before START.
    """
    if not path.is_file():
        return None, None
    text = path.read_bytes().decode("utf-8", errors="replace")
    start = text.find(MARKER_START)
    end = text.find(MARKER_END)
    if start < 0 or end < start:
        return None, (len(text) if start < 0 else None)
    end_line = text.find("\n", end)
    block = text[start:(end_line if end_line >= 0 else len(text))]
    return ({"chars": len(block), "bytes": len(block.encode("utf-8"))}, len(text[:start]))


SECTION0_PRIME_PREFIXES = ("## \u00a70'", "## \u00a70\u2033", "## \u00a70\u2034")
SECTION0_END_MARKER = "<!-- \u00a70-end -->"
VERIFY_END_RE = re.compile(r"<!-- \u00a7V-[a-z]+-end -->")


def generated_span_lines(lines):
    """Return (section0, verification) as sets of 0-based line indexes.

    Section 0: a "## section-0 " heading through the line holding the end
    marker; the prime, double-prime and triple-prime sections up to the next
    H2. Verification: a "## section-V " heading through its end marker.
    Hand-written section 0a and 0b blocks are not generated.
    """
    s0, ver = set(), set()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if line.startswith("## \u00a70 "):
            end = next((j for j in range(i, n) if SECTION0_END_MARKER in lines[j]), None)
            stop = (end + 1) if end is not None else _next_h2(lines, i)
            s0.update(range(i, stop))
            i = stop
        elif line.startswith(SECTION0_PRIME_PREFIXES):
            stop = _next_h2(lines, i)
            s0.update(range(i, stop))
            i = stop
        elif line.startswith("## \u00a7V "):
            end = next((j for j in range(i, n) if VERIFY_END_RE.search(lines[j])), None)
            stop = (end + 1) if end is not None else _next_h2(lines, i)
            ver.update(range(i, stop))
            i = stop
        else:
            i += 1
    return s0, ver


def _next_h2(lines, start):
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            return j
    return len(lines)


def measure_skill_sources(pkg):
    """Per-skill SKILL.md bytes and lines with the generated/hand-written split."""
    root = Path(pkg) / "quoin" / "adapters" / "claude" / "skills"
    skills = {}
    tot = {"total_bytes": 0, "total_lines": 0, "generated_bytes": 0, "generated_lines": 0,
           "section0_lines": 0, "verification_lines": 0, "handwritten_bytes": 0}
    for f in sorted(root.glob("*/SKILL.md")):
        text = f.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        s0, ver = generated_span_lines([l.rstrip("\r\n") for l in lines])
        gen_bytes = sum(len(lines[k].encode("utf-8")) for k in s0 | ver)
        total_bytes = len(text.encode("utf-8"))
        row = {"total_bytes": total_bytes, "total_lines": len(lines),
               "generated_bytes": gen_bytes, "generated_lines": len(s0 | ver),
               "section0_lines": len(s0), "verification_lines": len(ver),
               "handwritten_bytes": total_bytes - gen_bytes}
        skills[f.parent.name] = row
        for k in tot:
            tot[k] += row[k]
    return {"label": "on-demand", "skills": skills, "totals": tot, "files": len(skills)}


def measure_footprints(home_claude):
    fr = _load_module("_fr_baseline", SRC_ROOT / "scripts" / "footprint_report.py")
    snap = fr.capture_snapshot(home_claude)
    src = SRC_ROOT / "CLAUDE.md"
    dep = home_claude / "CLAUDE.md"

    def cb(p):
        if not p.is_file():
            return None
        raw = p.read_bytes()
        return {"chars": len(raw.decode("utf-8", errors="replace")), "bytes": len(raw)}

    cats = snap["categories"]
    marker_block, pre_marker = marker_footprint(dep)
    return {
        "claude_md": {"label": "always-on", "source": cb(src), "deployed": cb(dep),
                      "marker_block": marker_block, "pre_marker": pre_marker},
        "memory": {"label": "on-demand", "total_bytes": cats["memory"]["total_bytes"],
                   "files": len(cats["memory"]["files"])},
        "skill_bodies": {"label": "on-demand", "total_bytes": cats["skills"]["total_bytes"],
                         "files": len(cats["skills"]["files"])},
    }


def measure_skill_listing(home_claude):
    census = _load_module("_census_baseline", HERE / "census_descriptions.py")
    try:
        import yaml  # noqa: F401
    except ImportError:
        return {"label": "always-on", "error": "pyyaml unavailable"}
    skills = home_claude / "skills"
    overrides = {}
    settings = home_claude / "settings.json"
    if settings.is_file():
        try:
            overrides = json.loads(settings.read_text(encoding="utf-8")).get("skillOverrides", {}) or {}
        except Exception:  # noqa: BLE001
            overrides = {}
    raw_total = adj_total = n = 0
    name_only = 0
    if skills.is_dir():
        for d in sorted(p for p in skills.iterdir() if p.is_dir()):
            md = d / "SKILL.md"
            if not md.is_file():
                continue
            import yaml as _y
            try:
                block = census._frontmatter_block(md.read_text(encoding="utf-8"), md)
                data = _y.safe_load(block) or {}
            except SystemExit:
                continue
            name_b = len(str(data.get("name", d.name)).encode("utf-8"))
            desc_b = len(str(data.get("description", "")).encode("utf-8"))
            n += 1
            raw_total += name_b + desc_b
            if overrides.get(d.name) == "name-only":
                name_only += 1
                adj_total += name_b
            else:
                adj_total += name_b + desc_b
    return {"label": "always-on", "skills": n, "raw_bytes": raw_total,
            "adjusted_bytes": adj_total, "name_only_skills": name_only}


def list_settings_hooks(home_claude):
    settings = home_claude / "settings.json"
    rows = []
    if not settings.is_file():
        return rows
    try:
        hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {}) or {}
    except Exception:  # noqa: BLE001
        return rows
    hooks_dir = (home_claude / "hooks")
    for event, stanzas in hooks.items():
        for st in stanzas or []:
            for h in st.get("hooks", []) or []:
                cmd = str(h.get("command", ""))
                first = cmd.split()[0] if cmd.split() else ""
                p = Path(os.path.expanduser(first))
                owned = p.name in QUOIN_HOOK_BASENAMES and p.parent == hooks_dir
                rows.append({"event": event, "matcher": st.get("matcher", ""),
                             "command": cmd, "quoin_owned": owned,
                             "per_prompt": event == "UserPromptSubmit",
                             "label": "per-prompt" if event == "UserPromptSubmit" else "per-session"})
    return rows


def versions():
    return {
        "sh": _tool_version(["sh", "--version"]) if platform.system() != "Darwin" else _tool_version(["uname", "-a"]),
        "jq": _tool_version(["jq", "--version"]),
        "python": sys.version.split()[0],
        "machine": platform.platform() + " " + platform.machine(),
    }


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def render_md(label, data):
    L = ["## %s" % label, ""]
    L.append("Ref: `%s`; runs %s, warm-up %s; %s." % (
        data["meta"]["ref"], data["meta"]["runs"], data["meta"]["warmup"], data["meta"]["versions"]["machine"]))
    L.append("")
    L.append("### Per-prompt hook time (userpromptsubmit.sh, ms)")
    L.append("")
    L.append("| fixture | transcript bytes | cold median | warm median | warm p10 | warm p90 | stdout bytes |")
    L.append("|---|---|---|---|---|---|---|")
    for k, v in data["hooks"].items():
        if not k.startswith("prompt_"):
            continue
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            v["fixture"], v["transcript_bytes"], v["cold"]["median_ms"], v["warm"]["median_ms"],
            v["warm"]["p10_ms"], v["warm"]["p90_ms"], v["stdout_bytes"]))
    L += ["", "### Per-session hook time (idle fixture, ms)", "",
          "| hook | median | p10 | p90 | min | max |", "|---|---|---|---|---|---|"]
    for k, v in data["hooks"].items():
        if k.startswith("session_") and "median_ms" in v:
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                v["hook"], v["median_ms"], v["p10_ms"], v["p90_ms"], v["min_ms"], v["max_ms"]))
    L += ["", "### SessionStart output bytes (per-session)", "",
          "| path | stdout | additionalContext | systemMessage |", "|---|---|---|---|"]
    for k, v in data["sessionstart_outputs"].items():
        if "note" in v:
            L.append("| %s | %s | | |" % (k, v["note"]))
        else:
            L.append("| %s | %s | %s | %s |" % (k, v["stdout_bytes"], v["additionalContext_bytes"], v["systemMessage_bytes"]))
    fp = data["footprints"]
    cm = fp["claude_md"]
    L += ["", "### Footprints", "",
          "- CLAUDE.md (always-on): source %s, deployed %s" % (cm["source"], cm["deployed"]),
          "- CLAUDE.md marker block %s, pre-marker chars %s" % (cm.get("marker_block"), cm.get("pre_marker")),
          "- memory files (on-demand): %s bytes in %s files" % (fp["memory"]["total_bytes"], fp["memory"]["files"]),
          "- SKILL.md bodies (on-demand): %s bytes in %s files" % (fp["skill_bodies"]["total_bytes"], fp["skill_bodies"]["files"])]
    ss = data.get("skill_sources")
    if ss:
        L += ["", "### SKILL.md sources (on-demand)", "",
              "| skill | total | generated | hand-written |", "|---|---|---|---|"]
        for name, r in sorted(ss["skills"].items(), key=lambda kv: -kv[1]["handwritten_bytes"]):
            L.append("| %s | %s | %s | %s |" % (name, r["total_bytes"], r["generated_bytes"], r["handwritten_bytes"]))
        t = ss["totals"]
        L.append("| **total (%s skills)** | %s | %s | %s |" % (ss["files"], t["total_bytes"], t["generated_bytes"], t["handwritten_bytes"]))
    sl = data["skill_listing"]
    if "error" in sl:
        L.append("- skill listing (always-on): %s" % sl["error"])
    else:
        L.append("- skill listing (always-on): %s skills, raw %s bytes, adjusted %s bytes (%s name-only)" % (
            sl["skills"], sl["raw_bytes"], sl["adjusted_bytes"], sl["name_only_skills"]))
    L += ["", "### settings.json hooks", "",
          "| event | matcher | command | quoin-owned | per-prompt |", "|---|---|---|---|---|"]
    for r in data["settings_hooks"]:
        L.append("| %s | %s | `%s` | %s | %s |" % (r["event"], r["matcher"], r["command"], r["quoin_owned"], r["per_prompt"]))
    L.append("")
    return "\n".join(L)


def write_md_section(path, label, section):
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    pat = re.compile(r"^## %s\n.*?(?=^## |\Z)" % re.escape(label), re.S | re.M)
    block = section.rstrip("\n") + "\n\n"
    if pat.search(text):
        text = pat.sub(lambda m: block, text, count=1)
    else:
        text = text + ("" if not text or text.endswith("\n\n") else "\n") + block
    path.write_text(text, encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default=None)
    ap.add_argument("--worktree", action="store_true")
    ap.add_argument("--label", choices=("before", "after"), default="before")
    ap.add_argument("--out-json")
    ap.add_argument("--out-md")
    ap.add_argument("--runs", type=int, default=31)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--home-claude", default=os.path.expanduser("~/.claude"))
    args = ap.parse_args(argv)
    ref = args.ref or DEFAULT_REF
    home_claude = Path(args.home_claude)

    with tempfile.TemporaryDirectory(prefix="adapter-baseline-") as tmp:
        pkg = materialise(ref, args.worktree, tmp)
        work = Path(tmp) / "work"
        work.mkdir()
        hooks, _ = measure_hooks(pkg, args.runs, args.warmup, work)
        data = {
            "meta": {"label": args.label, "ref": "worktree" if args.worktree else ref,
                     "runs": args.runs, "warmup": args.warmup, "versions": versions()},
            "hooks": hooks,
            "sessionstart_outputs": measure_sessionstart_outputs(pkg, work),
            "footprints": measure_footprints(home_claude),
            "skill_sources": measure_skill_sources(pkg),
            "skill_listing": measure_skill_listing(home_claude),
            "settings_hooks": list_settings_hooks(home_claude),
        }
    for item in list(data["hooks"].values()) + list(data["sessionstart_outputs"].values()):
        assert item.get("label") in LABELS, item
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.out_md:
        write_md_section(args.out_md, args.label, render_md(args.label, data))
    if not args.out_json and not args.out_md:
        print(json.dumps(data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
