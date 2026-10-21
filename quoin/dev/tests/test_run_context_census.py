"""Tests for the /run orchestrator context census (dev tooling).

Every fixture is a synthetic JSONL transcript built in tmp_path, shaped on the
entries real Claude Code sessions write (async agent launches, task
notifications, streamed duplicate assistant entries, compaction boundaries,
subagent transcripts with meta.json files).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "dev" / "scripts" / "run_context_census.py"
OPUS = "claude-opus-4-7"
MARKER = ".workflow_artifacts/memory/autonomous-run-t.marker"


@pytest.fixture(scope="module")
def census():
    spec = importlib.util.spec_from_file_location("run_context_census", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_context_census"] = mod
    spec.loader.exec_module(mod)
    return mod


class Transcript:
    """Builds the entries of one transcript in file order."""

    def __init__(self):
        self.lines = []
        self.t = datetime(2026, 10, 1, 10, 0, 0)
        self.n = 0

    def _ts(self, minutes):
        self.t += timedelta(minutes=minutes)
        return self.t.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def user(self, text, minutes=1, sidechain=False):
        e = {"type": "user", "timestamp": self._ts(minutes),
             "message": {"role": "user", "content": text}}
        if sidechain:
            e["isSidechain"] = True
        self.lines.append(e)

    def result(self, tool_id, text, minutes=0):
        self.lines.append({"type": "user", "timestamp": self._ts(minutes),
                           "message": {"role": "user", "content": [
                               {"type": "tool_result", "tool_use_id": tool_id,
                                "content": [{"type": "text", "text": text}]}]}})

    def notify(self, agent_id, minutes=5):
        self.user("<task-notification>\n<task-id>%s</task-id>\n"
                  "<status>completed</status>\n</task-notification>" % agent_id,
                  minutes=minutes)

    def asst(self, ctx, tools=(), text=None, inp=10, cc=100, out=50,
             model=OPUS, mid=None, minutes=0):
        self.n += 1
        mid = mid or "msg_%d" % self.n
        content = []
        if text:
            content.append({"type": "text", "text": text})
        ids = []
        for k, (name, inp_) in enumerate(tools):
            tid = "toolu_%d_%d" % (self.n, k)
            ids.append(tid)
            content.append({"type": "tool_use", "id": tid, "name": name,
                            "input": inp_})
        self.lines.append({"type": "assistant", "timestamp": self._ts(minutes),
                           "message": {"id": mid, "model": model,
                                       "content": content,
                                       "usage": {"input_tokens": inp,
                                                 "cache_creation_input_tokens": cc,
                                                 "cache_read_input_tokens": ctx - inp - cc,
                                                 "output_tokens": out}}})
        return ids[0] if len(ids) == 1 else ids

    def bash(self, ctx, command, result="ok", **kw):
        tid = self.asst(ctx, tools=[("Bash", {"command": command})], **kw)
        self.result(tid, result)
        return tid

    def spawn_async(self, ctx, agent_id, description, prompt="go"):
        tid = self.asst(ctx, tools=[("Agent", {"description": description,
                                               "prompt": prompt})])
        self.result(tid, "Async agent launched successfully.\nagentId: %s "
                    "(internal ID)\nThe agent is working in the background."
                    % agent_id)
        return tid

    def write(self, root, sid, extra_raw=()):
        d = root / "proj"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / ("%s.jsonl" % sid), "w") as fh:
            for e in self.lines:
                fh.write(json.dumps(e) + "\n")
            for raw in extra_raw:
                fh.write(raw + "\n")
        return d / ("%s.jsonl" % sid)


def subagent(root, sid, agent_id, first_tool, first_ctx, tool_use_id="x"):
    d = root / "proj" / sid / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    t = Transcript()
    t.asst(first_ctx, tools=[first_tool])
    t.asst(first_ctx + 500, text="done")
    with open(d / ("agent-%s.jsonl" % agent_id), "w") as fh:
        for e in t.lines:
            fh.write(json.dumps(e) + "\n")
    (d / ("agent-%s.meta.json" % agent_id)).write_text(json.dumps(
        {"description": "x", "toolUseId": tool_use_id, "model": "sonnet"}))


def run_start(t, args=""):
    t.user("<command-name>/run</command-name>\n<command-args>%s</command-args>"
           % args)


def finalize_cmd(kind):
    p = ".workflow_artifacts/t/gate-%s-2026-10-01.md" % kind
    return ('cd /p; python3 ~/.claude/scripts/fsops.py finalize "%s.tmp" "%s" '
            '--cleanup "%s.body.tmp" "%s.tmp"' % (p, p, p, p))


def inline_gate(t, ctx, kind):
    """A gate read in two partial Reads, checks, and the three-step log write."""
    gate = "/Users/u/.claude/skills/gate/SKILL.md"
    t.asst(ctx, tools=[("Read", {"file_path": gate, "limit": 200})])
    t.asst(ctx + 4000, tools=[("Read", {"file_path": gate, "offset": 200})])
    t.bash(ctx + 9000, "cat .workflow_artifacts/t/gate-%s-2026-09-01.md" % kind)
    t.bash(ctx + 12000, "python3 -m pytest -q")
    p = ".workflow_artifacts/t/gate-%s-2026-10-01.md" % kind
    t.asst(ctx + 14000, tools=[("Write", {"file_path": p + ".body.tmp", "content": "x"})])
    t.asst(ctx + 15000, tools=[("Write", {"file_path": p + ".tmp", "content": "x"})])
    t.bash(ctx + 16000, finalize_cmd(kind))


# ---------------------------------------------------------------------------
# corpus fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "projects"

    # Interactive run: implement spawn, post-implement inline gate, Checkpoint C,
    # review spawn, post-review inline gate, then a gate cut off by the end.
    t = Transcript()
    run_start(t, "medium: t")
    t.asst(20000, text="setup")
    t.asst(25000, text="setup 2")
    impl = t.spawn_async(30000, "aimpl", "Phase 4 implement", "[autonomous] /implement")
    t.asst(31000, text="implement is running")
    t.notify("aimpl")
    t.asst(36000, text="implement returned")
    inline_gate(t, 40000, "implement")
    t.asst(57000, text="Phase complete: implement. Checkpoint C")
    t.user("yes, continue")
    t.bash(58000, "python3 fsops.py write-atomic .workflow_artifacts/memory/implement.done")
    t.bash(59000, "python3 run_state.py --write --phase review")
    t.spawn_async(60000, "arev", "Phase 5 review")
    t.notify("arev")
    t.asst(64000, text="review returned")
    inline_gate(t, 66000, "review")
    t.asst(83000, text="Phase complete: review")
    t.asst(84000, tools=[("Read", {"file_path": "/Users/u/.claude/skills/gate/SKILL.md"})])
    t.asst(86000, text="still checking")
    t.write(root, "s-interactive")
    subagent(root, "s-interactive", "aimpl", ("Skill", {"skill": "implement"}), 50000, impl)
    subagent(root, "s-interactive", "arev", ("Skill", {"skill": "review"}), 52000)

    # Autonomous run by token, with an existing post-architect gate spawn.
    t = Transcript()
    run_start(t, "--autonomous t")
    t.asst(20000, text="setup")
    t.spawn_async(30000, "agate", "Post-architect gate")
    t.asst(30500, text="gate is running")
    t.notify("agate", minutes=20)
    t.bash(33000, "python3 ~/.claude/scripts/agent_transcript_cost.py --agent agate "
           ">> .workflow_artifacts/t/cost-ledger.md", text="Gate PASS")
    t.bash(33400, "tail -2 .workflow_artifacts/t/cost-ledger.md")
    t.asst(34000, text="Checkpoint B: architecture approved")
    t.write(root, "s-auto-token")
    subagent(root, "s-auto-token", "agate", ("Skill", {"skill": "gate"}), 54000)

    # Headless plain start whose span has no spawn: relaunch into an inline gate.
    t = Transcript()
    t.user("/run --resume --autonomous t")
    inline_gate(t, 90000, "implement")
    t.asst(110000, text="after gate")
    t.asst(111000, text="after gate 2")
    t.lines.append({"type": "system", "subtype": "compact_boundary",
                    "timestamp": t._ts(1)})
    t.asst(30000, text="post compact")
    t.asst(31000, text="post compact 2")
    t.write(root, "s-relaunch")

    # Two starts: the first interactive, the second autonomous by token.
    t = Transcript()
    run_start(t, "t")
    t.asst(1000, text="a")
    run_start(t, "--autonomous t")
    t.asst(2000, text="b")
    t.write(root, "s-two", extra_raw=["{not json"])

    # Not a /run session; /run only inside a tool_result; sidechain-only start.
    t = Transcript()
    t.user("please fix the bug")
    tid = t.asst(1000, tools=[("Bash", {"command": "grep run"})])
    t.result(tid, "/run --autonomous x")
    t.write(root, "s-none")
    t = Transcript()
    t.user("<command-name>/run</command-name>", sidechain=True)
    t.asst(1000, text="x")
    t.write(root, "s-sidechain")

    # Excluded session.
    t = Transcript()
    run_start(t, "t")
    t.asst(1000, text="x")
    t.write(root, "s-excluded")

    # A Read-first gate subagent and a large non-gate subagent elsewhere.
    subagent(root, "s-other", "agate2", ("Read", {"file_path": "/u/.claude/skills/gate/SKILL.md"}), 56000)
    subagent(root, "s-other", "abig", ("Read", {"file_path": "/u/src/main.py"}), 900000)
    return root


def mode_corpus(root):
    def session(name, build):
        t = Transcript()
        build(t)
        t.write(root, name)

    session("p-token", lambda t: (run_start(t, "--autonomous t"), t.asst(1000, text="x")))
    session("p-write", lambda t: (run_start(t, "t"), t.bash(
        1000, "python3 ~/.claude/scripts/fsops.py write-atomic %s --parents" % MARKER)))
    session("p-content", lambda t: (run_start(t, "--resume t"), t.bash(
        1000, "cat %s" % MARKER, "task: t\ntimestamp: x\nautonomous: true")))
    session("n-ls", lambda t: (run_start(t, "--resume t"), t.bash(
        1000, "ls -la %s" % MARKER, "ls: %s: No such file or directory" % MARKER)))
    session("n-absent", lambda t: (run_start(t, "--resume t"), t.bash(
        1000, 'cat %s 2>/dev/null || echo "(absent)"' % MARKER, "(absent)")))
    session("n-compound", lambda t: (run_start(t, "--resume t"), t.bash(
        1000, "cat %s run-notes-t.md 2>/dev/null" % MARKER, "notes: autonomous: false")))


def run(census, root, **kw):
    opts = census.Options(**kw.pop("opts", {}))
    return census.run_census(root, opts=opts, **kw)


def row(result, sid):
    return next(r for r in result["rows"] if r["session"] == sid)


# ---------------------------------------------------------------------------
# selection and mode
# ---------------------------------------------------------------------------

def test_selection_and_start_forms(census, corpus):
    res = run(census, corpus, excluded=["s-excluded"])
    c = res["corpus"]
    assert sorted(r["session"] for r in res["rows"]) == [
        "s-auto-token", "s-interactive", "s-relaunch", "s-two"]
    assert c["files_excluded"] == 1
    assert c["starts_tag"] == 4 and c["starts_plain"] == 1
    assert c["malformed_lines"] == 1


def test_two_starts_are_tagged_per_span(census, corpus):
    spans = row(run(census, corpus), "s-two")["spans"]
    assert [(s["mode"], s["evidence"]) for s in spans] == [
        ("interactive", "none"), ("autonomous", "token")]


def test_mode_tagging_positive_evidence(census, tmp_path):
    mode_corpus(tmp_path)
    res = run(census, tmp_path)
    got = {r["session"]: r["spans"][0]["evidence"] for r in res["rows"]}
    assert got["p-token"] == "token"
    assert got["p-write"] == "marker-write"
    assert got["p-content"] == "marker-content"
    for sid in ("p-token", "p-write", "p-content"):
        assert row(res, sid)["spans"][0]["mode"] == "autonomous"


def test_mode_tagging_negative_probes_stay_interactive(census, tmp_path):
    mode_corpus(tmp_path)
    res = run(census, tmp_path)
    for sid in ("n-ls", "n-absent", "n-compound"):
        assert row(res, sid)["spans"][0]["mode"] == "interactive", sid
    assert res["corpus"]["evidence"] == {"token": 1, "marker-write": 1,
                                         "marker-content": 1, "none": 3}


# ---------------------------------------------------------------------------
# spawns, segments, overhead
# ---------------------------------------------------------------------------

def test_spawn_return_measured_at_notification(census, corpus):
    spawns = row(run(census, corpus), "s-interactive")["spawns"]
    impl = next(s for s in spawns if s["agent_id"] == "aimpl")
    assert impl["phase"] == "implement" and impl["linked"] and impl["async"]
    assert impl["return_growth"] == 36000 - 31000


def test_unlinked_spawn_description_is_not_a_gate(census, tmp_path):
    t = Transcript()
    run_start(t, "t")
    tid = t.asst(1000, tools=[("Agent", {"description": "Investigate repo",
                                          "prompt": "look around"})])
    t.result(tid, "sync answer")
    t.asst(1500, text="done")
    t.write(tmp_path, "s")
    sp = row(run(census, tmp_path), "s")["spawns"][0]
    assert sp["phase"] == "other" and not sp["async"]
    assert sp["return_growth"] == 500


def test_inline_gate_segments_and_labels(census, corpus):
    r = row(run(census, corpus), "s-interactive")
    segs = r["segments"]
    assert [s["category"] for s in segs] == ["post-implement", "post-review",
                                              "post-review"]
    first = segs[0]
    # two partial Reads, cat of an earlier log, pytest, two Writes, finalize
    assert first["n_turns"] == 7 and not first["truncated"]
    assert first["growth"] == 16000
    assert segs[1]["n_turns"] == 7 and not segs[1]["truncated"]
    assert segs[2]["truncated"]
    # Checkpoint texts sit outside the segments and count once as summaries.
    assert len(r["summaries"]) == 2


def test_relaunch_segment_label_comes_from_the_log_path(census, corpus):
    segs = row(run(census, corpus), "s-relaunch")["segments"]
    assert len(segs) == 1 and segs[0]["category"] == "post-implement"
    assert segs[0]["mode"] == "autonomous"


def test_remaining_turns_stop_at_compaction(census, corpus):
    seg = row(run(census, corpus), "s-relaunch")["segments"][0]
    assert seg["remaining_turns"] == 2


def test_truncated_segments_are_excluded_from_aggregates(census, corpus):
    agg = run(census, corpus)["aggregates"]["interactive"]["post-review"]
    assert agg["segments"] == 1 and agg["truncated"] == 1


def test_gate_spawn_overhead_turns(census, corpus):
    res = run(census, corpus)
    sp = row(res, "s-auto-token")["spawns"][0]
    assert sp["phase"] == "gate"
    assert len(sp["overhead_before"]) == 2
    assert len(sp["overhead_after"]) == 2
    assert res["inputs"]["O"] is not None
    assert res["inputs"]["S"] == 33000 - 30500


def test_long_wait_spawn_is_flagged(census, corpus):
    sp = row(run(census, corpus), "s-auto-token")["spawns"][0]
    assert sp["gap_minutes"] == 20.0
    res = run(census, corpus)
    assert res["inputs"]["O"]["long_share"] == 0.0


def test_base_context_from_gate_subagents_only(census, corpus, monkeypatch):
    monkeypatch.setattr(census, "MIN_GATE_SUBAGENTS", 2)
    inputs = run(census, corpus)["inputs"]
    assert inputs["gate_subagents"] == 2 and not inputs["B_fallback"]
    assert inputs["B"] == (54000 + 56000) / 2


# ---------------------------------------------------------------------------
# parsing details
# ---------------------------------------------------------------------------

def test_streamed_duplicates_count_once(census, tmp_path):
    t = Transcript()
    run_start(t, "t")
    t.asst(1000, text="part one", mid="msg_same")
    t.asst(1000, tools=[("Bash", {"command": "ls"})], mid="msg_same")
    t.asst(1200, text="next")
    t.asst(1300, text="synthetic", model="<synthetic>")
    t.write(tmp_path, "s")
    assert row(run(census, tmp_path), "s")["turns"] == 2


def test_fallback_price_counter(census, tmp_path):
    t = Transcript()
    run_start(t, "t")
    t.asst(1000, text="x", model="mystery-model-1")
    t.write(tmp_path, "s")
    assert run(census, tmp_path)["corpus"]["fallback_price_turns"] == 1


def test_cost_formulas_on_hand_computed_numbers(census):
    prices = census.Prices()
    seg = {"turns": [(10, 100, 890, 5, OPUS), (20, 200, 1000, 5, OPUS)],
           "start_ctx": 1000, "growth": 220, "remaining_turns": 4}
    O = {"fresh": 30, "m_before": 2, "m_after": 2, "long_share": 0.0}
    actual, cf, cf_decl = census.segment_costs(seg, prices, 400, 50, 300, O)
    assert actual == pytest.approx(682.0)
    assert cf == pytest.approx(1024.0 + 1075.0)
    assert cf_decl == pytest.approx(cf)
    _, cons, _ = census.segment_costs(seg, prices, 400, 50, 300, O, o_scale=2.0)
    assert cons == pytest.approx(1024.0 + 1655.0)
    _, _, half = census.segment_costs(seg, prices, 400, 50, 300, O,
                                      declared_ratio=0.5)
    assert half == pytest.approx(512.0 + 1075.0)


def test_output_is_deterministic(census, corpus, tmp_path):
    outs = []
    for k in range(2):
        j, m = tmp_path / ("o%d.json" % k), tmp_path / ("o%d.md" % k)
        assert census.main(["--projects-root", str(corpus), "--sensitivity",
                            "--out-json", str(j), "--out-md", str(m)]) == 0
        outs.append((j.read_bytes(), m.read_bytes()))
    assert outs[0] == outs[1]
    assert b"## Cost by category (autonomous runs)" in outs[0][1]
