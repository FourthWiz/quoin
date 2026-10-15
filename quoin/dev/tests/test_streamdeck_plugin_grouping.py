"""Characterisation and behaviour tests for the Stream Deck plugin panel.

The plugin runs unmodified under Node with a fake WebSocket, a fake clock, a fake
python and an intercepted sound player, so nothing here reaches the real home,
the live runtime or the speakers.
"""
import json
import subprocess

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess  # noqa: F401

pytestmark = [pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess"),
              pytest.mark.skipif(h.NODE is None, reason="node not found on the developer PATH")]

PLUGIN = h.PAYLOAD_DIR / "plugin" / "plugin.js"

START_HARNESS = r"""
const fs = require('node:fs'), path = require('node:path'), cp = require('node:child_process');
const plugin = process.argv[2], scenarioPath = process.argv[3];
const scenario = JSON.parse(fs.readFileSync(scenarioPath, 'utf8'));
const dir = path.dirname(scenarioPath);
const fakePython = path.join(dir, 'fake-python');
const realExit = process.exit.bind(process);
const fail = (msg, code) => { process.stderr.write(msg + '\n'); realExit(code || 3); };

const clock = {t: 2000};
class FakeDate extends Date { static now() { return clock.t; } }
const intervals = {};
const fakeSetInterval = (fn, ms) => { intervals[ms] = fn; return ms; };
const sent = [], listeners = {};
let sounds = 0, renames = 0, exited = false;
class FakeWebSocket {
  constructor(url) { this.url = url; this.readyState = 1; }
  send(s) { sent.push(JSON.parse(s)); }
  addEventListener(t, fn) { listeners[t] = fn; }
}
const spawnProxy = (bin, argv, opts) => {
  if (bin === '/usr/bin/afplay') {
    sounds++;
    const stub = {on() { return stub; }};
    return stub;
  }
  if (bin === fakePython) return cp.spawn(bin, argv, opts);
  process.stderr.write('unexpected spawn: ' + bin + '\n');
  realExit(2);
};
const fsProxy = {...fs, renameSync(a, b) { const r = fs.renameSync(a, b); if (String(b).endsWith('health.json')) renames++; return r; }};
const injected = {'node:child_process': {...cp, spawn: spawnProxy}, 'node:fs': fsProxy};
const argv = [process.argv[0], plugin, '-port', '1', '-pluginUUID', 'u', '-registerEvent', 'r'];
const processProxy = new Proxy(process, {get(t, k) {
  if (k === 'argv') return argv;
  if (k === 'exit') return () => { exited = true; };
  const v = t[k];
  return typeof v === 'function' ? v.bind(t) : v;
}});
const module_ = {exports: {}};
const req = name => injected[name] || require(name);
req.main = module_;
const src = fs.readFileSync(plugin, 'utf8');
new Function('require', 'module', 'exports', '__filename', '__dirname', 'process', 'WebSocket', 'setInterval', 'Date', src)(
  req, module_, module_.exports, plugin, dir, processProxy, FakeWebSocket, fakeSetInterval, FakeDate);

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(cond) {
  for (let i = 0; i < 500; i++) { if (cond()) return; await sleep(10); }
  fail('timed out waiting', 4);
}
function decode(img) {
  const svg = Buffer.from(img.split(',')[1], 'base64').toString();
  const un = s => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&amp;/g, '&');
  return {color: /<rect[^>]*fill="([^"]+)"/.exec(svg)[1], lines: [...svg.matchAll(/<text[^>]*>(.*?)<\/text>/g)].map(m => un(m[1]))};
}
const message = m => listeners.message({data: JSON.stringify(m)});
const contexts = [];
const out = {faces: [], marks: []};
(async () => {
  for (const step of scenario.steps) {
    if (step.op === 'appear') {
      contexts.push(step.ctx);
      await message({event: 'willAppear', context: step.ctx, payload: {settings: {role: step.role, slot: step.slot}}});
    } else if (step.op === 'open') {
      await listeners.open();
      if (scenario.cfg) await waitFor(() => renames >= 1);
    } else if (step.op === 'snapshot') {
      fs.writeFileSync(path.join(dir, 'snapshot.json'), JSON.stringify(step.list));
      const n = renames;
      await intervals[2500]();
      await waitFor(() => renames > n);
    } else if (step.op === 'tick') {
      await intervals[1000]();
    } else if (step.op === 'clock') {
      clock.t = step.t;
    } else if (step.op === 'down') {
      await message({event: 'keyDown', context: step.ctx, payload: {}});
    } else if (step.op === 'up') {
      await message({event: 'keyUp', context: step.ctx, payload: {}});
    } else if (step.op === 'mark') {
      out.marks.push({sounds});
    } else if (step.op === 'faces') {
      const entry = {};
      for (const ctx of (step.ctxs || contexts)) {
        const images = sent.filter(e => e.event === 'setImage' && e.context === ctx);
        entry[ctx] = images.length ? decode(images[images.length - 1].payload.image) : null;
      }
      out.faces.push(entry);
    } else fail('unknown step ' + step.op, 5);
  }
  out.sounds = sounds;
  out.exited = exited;
  console.log(JSON.stringify(out));
  realExit(0);
})();
"""

BLUE, GREY, NOSIG, ORANGE, ORANGE_BLINK = "#175583", "#3c4859", "#4c4559", "#bd4d10", "#e27916"
NEUTRAL, EMPTY, QUIET = "#213a4e", "#18202a", "#273b43"


def rec(id_, kind="claude", session="s", pane=1, project="/w/p", status="idle", socket="/z/s"):
    return {"id": id_, "kind": kind, "session": session, "pane": pane, "socket": socket,
            "project": project, "status": status}


def run_scenario(tmp_path, steps, initial=(), cfg=True, python=None):
    work = tmp_path / "deck"
    work.mkdir(exist_ok=True)
    fake = work / "fake-python"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '{\"action\":\"%%s\",\"arg\":%%s}\\n' \"$4\" \"${5:-null}\" >> '%s/calls.log'\n"
        "if [ \"$4\" = snapshot ]; then cat '%s/snapshot.json'; fi\n"
        "exit 0\n" % (work, work))
    fake.chmod(0o755)
    (work / "snapshot.json").write_text(json.dumps(list(initial)))
    (work / "calls.log").write_text("")
    if cfg:
        (work / "plugin-config.json").write_text(json.dumps(
            {"python": python or str(fake), "runtime": str(work / "rt"), "state_root": str(work / "sr")}))
    scenario = work / "scenario.json"
    scenario.write_text(json.dumps({"cfg": cfg, "steps": steps}))
    harness = work / "start.js"
    harness.write_text(START_HARNESS)
    result = subprocess.run([h.NODE, str(harness), str(PLUGIN), str(scenario)], capture_output=True, text=True,
                            timeout=60, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    if result.returncode != 0:
        pytest.fail("harness exit %s: %s" % (result.returncode, result.stderr))
    out = json.loads(result.stdout)
    out["calls"] = [json.loads(line) for line in (work / "calls.log").read_text().splitlines() if line]
    return out, result


def actions(out):
    return [c for c in out["calls"] if c["action"] != "snapshot"]


def appear(*specs):
    return [{"op": "appear", "ctx": c, "role": r, "slot": s} for c, r, s in specs]


def test_agent_faces_and_empty_slot(tmp_path):
    agents = [
        rec("a1", "claude", "quoin-main-session", 3, "/w/quoin", "working"),
        rec("a2", "codex", "alpha", 1, "", "idle"),
        rec("a3", "claude", "gamma", 2, "/w/g", "weird"),
    ]
    steps = appear(("c0", "agent", 0), ("c1", "agent", 1), ("c2", "agent", 2), ("c4", "agent", 4)) + [
        {"op": "open"}, {"op": "faces"}]
    out, _ = run_scenario(tmp_path, steps, initial=agents)
    f = out["faces"][0]
    assert f["c0"] == {"lines": ["CLAUDE", "quoin", "n-session · 3", "WORKING"], "color": BLUE}
    assert f["c1"] == {"lines": ["CODEX", "alpha", "alpha · 1", "IDLE"], "color": GREY}
    assert f["c2"] == {"lines": ["CLAUDE", "g", "gamma · 2", "NO SIGNAL"], "color": NOSIG}
    assert f["c4"] == {"lines": ["—", "Agent 5", "", ""], "color": EMPTY}


def test_attention_agent_blinks_with_the_clock(tmp_path):
    steps = appear(("c0", "agent", 0)) + [
        {"op": "open"}, {"op": "clock", "t": 2000}, {"op": "tick"}, {"op": "faces"},
        {"op": "clock", "t": 3000}, {"op": "tick"}, {"op": "faces"}]
    out, _ = run_scenario(tmp_path, steps, initial=[rec("a1", status="attention")])
    assert out["faces"][0]["c0"]["color"] == ORANGE
    assert out["faces"][1]["c0"]["color"] == ORANGE_BLINK
    assert out["faces"][1]["c0"]["lines"][3] == "NEEDS YOU"


def test_attention_key_counts_waiting_agents(tmp_path):
    agents = [rec("a%d" % i, session="s%02d" % i, pane=i, status="attention" if i in (2, 7) else "idle")
              for i in range(1, 13)]
    steps = appear(("att", "attention", 0)) + [{"op": "open"}, {"op": "faces"},
                                               {"op": "snapshot", "list": [dict(a, status="idle") for a in agents]},
                                               {"op": "faces"}]
    out, _ = run_scenario(tmp_path, steps, initial=agents)
    assert out["faces"][0]["att"] == {"lines": ["ATTENTION", "2", "Page 1/2", "Hold: next page"], "color": ORANGE}
    assert out["faces"][1]["att"]["color"] == QUIET
    assert out["faces"][1]["att"]["lines"][1] == "0"


def test_bottom_row_faces(tmp_path):
    steps = appear(("d", "desk", 0), ("c", "claude", 0), ("x", "codex", 0), ("p", "prev-tab", 0),
                   ("n", "next-tab", 0), ("u", "mystery", 0)) + [{"op": "open"}, {"op": "faces"}]
    out, _ = run_scenario(tmp_path, steps)
    f = out["faces"][0]
    assert f["d"] == {"lines": ["DESK", "2 Codex + Shell", "", "Hold: menu"], "color": NEUTRAL}
    assert f["c"] == {"lines": ["+ CLAUDE", "New pane"], "color": NEUTRAL}
    assert f["x"] == {"lines": ["+ CODEX", "New pane"], "color": NEUTRAL}
    assert f["p"] == {"lines": ["← TAB", "Ghostty"], "color": NEUTRAL}
    assert f["n"] == {"lines": ["TAB →", "Ghostty"], "color": NEUTRAL}
    assert f["u"] == {"lines": ["Agent Desk"], "color": NEUTRAL}


def test_no_config_shows_a_notice_on_every_key(tmp_path):
    steps = appear(("a", "agent", 0), ("att", "attention", 0), ("d", "desk", 0)) + [{"op": "open"}, {"op": "faces"}]
    out, _ = run_scenario(tmp_path, steps, cfg=False)
    for ctx in ("a", "att", "d"):
        assert out["faces"][0][ctx] == {"lines": ["NO CONFIG", "plugin-config.json"], "color": "#6b2b2b"}
    assert out["calls"] == []


def test_agent_key_press_focuses_the_record(tmp_path):
    agents = [rec("a1", session="one"), rec("a2", session="two", status="working")]
    steps = appear(("c0", "agent", 0), ("c1", "agent", 1)) + [
        {"op": "open"}, {"op": "down", "ctx": "c1"}, {"op": "up", "ctx": "c1"}]
    out, _ = run_scenario(tmp_path, steps, initial=agents)
    assert actions(out) == [{"action": "focus", "arg": agents[1]}]


def test_attention_short_presses_cycle_and_wrap(tmp_path):
    agents = [rec("a1", session="a", status="attention"), rec("a2", session="b"),
              rec("a3", session="c", status="attention"), rec("a4", session="d", status="attention")]
    steps = appear(("att", "attention", 0)) + [{"op": "open"}] + [
        s for _ in range(4) for s in ({"op": "down", "ctx": "att"}, {"op": "up", "ctx": "att"})]
    out, _ = run_scenario(tmp_path, steps, initial=agents)
    assert [c["arg"]["id"] for c in actions(out)] == ["a1", "a3", "a4", "a1"]
    assert {c["action"] for c in actions(out)} == {"focus"}


def test_attention_hold_turns_the_page(tmp_path):
    agents = [rec("a%d" % i, session="sess%02d" % i, pane=i, project="/w/proj%02d" % i) for i in range(1, 13)]
    steps = appear(("att", "attention", 0), ("c0", "agent", 0), ("c2", "agent", 2), ("c3", "agent", 3)) + [
        {"op": "open"}, {"op": "down", "ctx": "att"}, {"op": "clock", "t": 2700}, {"op": "up", "ctx": "att"},
        {"op": "faces"}]
    out, _ = run_scenario(tmp_path, steps, initial=agents)
    f = out["faces"][0]
    assert f["c0"]["lines"] == ["CLAUDE", "proj10", "sess10 · 10", "IDLE"]
    assert f["c2"]["lines"] == ["CLAUDE", "proj12", "sess12 · 12", "IDLE"]
    assert f["c3"] == {"lines": ["—", "Agent 13", "", ""], "color": EMPTY}
    assert f["att"]["lines"] == ["ATTENTION", "0", "Page 2/2", "Hold: next page"]
    assert actions(out) == []


@pytest.mark.parametrize("role,held,action", [
    ("desk", False, "desk"), ("desk", True, "menu"), ("claude", False, "claude"),
    ("codex", False, "codex"), ("prev-tab", False, "prev-tab"), ("next-tab", False, "next-tab"),
])
def test_bottom_row_presses_call_their_action(tmp_path, role, held, action):
    steps = appear(("k", role, 0)) + [{"op": "open"}, {"op": "down", "ctx": "k"}]
    if held:
        steps.append({"op": "clock", "t": 2700})
    steps.append({"op": "up", "ctx": "k"})
    out, _ = run_scenario(tmp_path, steps)
    assert [c["action"] for c in actions(out)] == [action]


def test_glass_plays_once_per_new_waiting_agent(tmp_path):
    base = [rec("a1", session="a"), rec("a2", session="b"), rec("a3", session="c")]

    def with_waiting(*ids):
        return [dict(a, status="attention" if a["id"] in ids else "idle") for a in base]

    steps = appear(("c0", "agent", 0)) + [
        {"op": "open"}, {"op": "mark"},
        {"op": "snapshot", "list": with_waiting("a2")}, {"op": "mark"},
        {"op": "snapshot", "list": with_waiting("a2")}, {"op": "mark"},
        {"op": "snapshot", "list": with_waiting("a2", "a3")}, {"op": "mark"},
        {"op": "snapshot", "list": with_waiting("a3")}, {"op": "mark"}]
    out, _ = run_scenario(tmp_path, steps, initial=base)
    assert [m["sounds"] for m in out["marks"]] == [0, 1, 1, 2, 2]
    assert out["sounds"] == 2


def test_key_swap_keeps_the_target_captured_at_key_down(tmp_path):
    first, second = rec("a1", session="one"), rec("a2", session="two", status="working")
    steps = appear(("c0", "agent", 0)) + [
        {"op": "open"}, {"op": "down", "ctx": "c0"},
        {"op": "snapshot", "list": [second, first]}, {"op": "faces"},
        {"op": "up", "ctx": "c0"}]
    out, _ = run_scenario(tmp_path, steps, initial=[first, second])
    assert out["faces"][0]["c0"]["lines"][2] == "two · 1"
    assert actions(out) == [{"action": "focus", "arg": first}]


def test_harness_refuses_unexpected_spawns(tmp_path):
    steps = appear(("c0", "agent", 0)) + [{"op": "open"}]
    with pytest.raises(pytest.fail.Exception, match="harness exit 2: unexpected spawn: /bin/echo"):
        run_scenario(tmp_path, steps, python="/bin/echo")
