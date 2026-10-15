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


MODEL_HARNESS = r"""
const fs = require('node:fs');
const p = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const results = [];
const resolve = a => (a && typeof a === 'object' && !Array.isArray(a) && '$ref' in a) ? results[a.$ref] : a;
for (const [fn, ...args] of input) {
  if (fn === '$frozen') {
    const items = results[args[0]];
    results.push(items.every(i => Object.isFrozen(i) && (i.agents === undefined || Object.isFrozen(i.agents))));
  } else results.push(p[fn](...args.map(resolve)));
}
console.log(JSON.stringify(results));
"""


def model(tmp_path, calls):
    harness = tmp_path / "model.js"
    harness.write_text(MODEL_HARNESS)
    data = tmp_path / "model.json"
    data.write_text(json.dumps(calls))
    result = subprocess.run([h.NODE, str(harness), str(PLUGIN), str(data)], capture_output=True, text=True,
                            timeout=60, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def ref(i):
    return {"$ref": i}


def many(session, count, socket="/z/s", status="idle", project="/w/p", first=1):
    return [rec("%s-%d" % (session, n), session=session, pane=n, socket=socket, status=status, project=project)
            for n in range(first, first + count)]


def test_group_agents_orders_sessions_and_keeps_snapshot_order_inside(tmp_path):
    snapshot = [many("b", 1)[0], many("a", 3)[0], many("c", 2)[0], many("a", 3)[1], many("c", 2)[1], many("a", 3)[2]]
    items, frozen = model(tmp_path, [["groupAgents", snapshot], ["$frozen", 0]])
    assert [i["session"] for i in items] == ["a", "b", "c"]
    assert [len(i["agents"]) for i in items] == [3, 1, 2]
    assert [a["id"] for a in items[0]["agents"]] == ["a-1", "a-2", "a-3"]
    assert frozen is True


def test_same_session_name_on_two_sockets_gives_two_groups(tmp_path):
    snapshot = [rec("x", session="main", socket="/z/2"), rec("y", session="main", socket="/z/1")]
    (items,) = model(tmp_path, [["groupAgents", snapshot]])
    assert [(i["socket"], i["session"]) for i in items] == [("/z/1", "main"), ("/z/2", "main")]
    assert items[0]["key"] != items[1]["key"]


def test_session_sort_is_plain_comparison_and_standalone_come_last(tmp_path):
    snapshot = [{"id": "s1", "kind": "codex", "status": "idle"}, rec("a", session="a"), rec("b", session="B"),
                rec("e", session=""), rec("w", session="  "), rec("n", session=7), "junk", None, 4]
    (items,) = model(tmp_path, [["groupAgents", snapshot]])
    assert [(i["type"], i.get("session")) for i in items] == [
        ("session", "B"), ("session", "a"), ("agent", None), ("agent", None), ("agent", None), ("agent", None)]
    assert [i["agent"]["id"] for i in items[2:]] == ["s1", "e", "w", "n"]
    assert model(tmp_path, [["groupAgents", {"not": "a list"}], ["groupAgents", None]]) == [[], []]


def test_session_project_is_shared_only_when_every_agent_agrees(tmp_path):
    same = many("one", 2, project="/w/quoin")
    mixed = [rec("m1", session="two", project="/w/quoin"), rec("m2", session="two", project="/w/other")]
    partial = [rec("p1", session="three", project="/w/quoin"), rec("p2", session="three", project="")]
    items, = model(tmp_path, [["groupAgents", same + mixed + partial]])
    assert [(i["session"], i["project"]) for i in items] == [("one", "quoin"), ("three", ""), ("two", "")]


def test_group_key_is_a_collision_free_pair(tmp_path):
    key_a, key_b, key_c = model(tmp_path, [
        ["groupKey", {"socket": "x", "session": 'y","z'}],
        ["groupKey", {"socket": 'x","y', "session": "z"}],
        ["groupKey", {"session": "main"}]])
    assert key_a != key_b
    assert key_c == json.dumps(["", "main"], separators=(",", ":"))


@pytest.mark.parametrize("higher,lower", [("attention", "done"), ("done", "working"), ("working", "idle"),
                                          ("idle", "unknown"), ("unknown", "offline")])
def test_aggregate_status_priority_pairs(tmp_path, higher, lower):
    forward, backward = model(tmp_path, [
        ["aggregateStatus", [{"status": higher}, {"status": lower}]],
        ["aggregateStatus", [{"status": lower}, {"status": higher}]]])
    assert forward == backward == higher


def test_aggregate_status_mixed_and_unrecognised(tmp_path):
    mixed, odd, odd_vs_offline, empty = model(tmp_path, [
        ["aggregateStatus", [{"status": "idle"}, {"status": "done"}, {"status": "working"}]],
        ["aggregateStatus", [{"status": "bogus"}]],
        ["aggregateStatus", [{"status": "bogus"}, {"status": "offline"}]],
        ["aggregateStatus", []]])
    assert (mixed, odd, odd_vs_offline, empty) == ("done", "unknown", "unknown", "unknown")


def test_collapsed_paging_of_twelve_sessions(tmp_path):
    twelve = [r for n in range(12) for r in many("s%02d" % n, 1)]
    view = {"mode": "collapsed", "collapsedPage": 0, "expandedKey": None, "expandedPage": 0}
    (items,) = model(tmp_path, [["groupAgents", twelve]])
    count, first, second = model(tmp_path, [
        ["pageCount", view, items], ["layoutSlots", view, items], ["layoutSlots", dict(view, collapsedPage=1), items]])
    assert count == 2
    assert [s["group"]["session"] for s in first] == ["s%02d" % n for n in range(9)]
    assert [s and s["group"]["session"] for s in second] == ["s09", "s10", "s11"] + [None] * 6


def test_expanded_layout_paging_and_press_results(tmp_path):
    ten = many("big", 10)
    (items,) = model(tmp_path, [["groupAgents", ten]])
    key = items[0]["key"]
    expanded = {"mode": "expanded", "collapsedPage": 0, "expandedKey": key, "expandedPage": 0}
    page2 = dict(expanded, expandedPage=1)
    collapsed_slots, slots1, slots2, count_expanded, empty_count = model(tmp_path, [
        ["layoutSlots", {"mode": "collapsed", "collapsedPage": 0, "expandedKey": None, "expandedPage": 0}, items],
        ["layoutSlots", expanded, items],
        ["layoutSlots", page2, items],
        ["pageCount", expanded, items],
        ["pageCount", {"mode": "collapsed", "collapsedPage": 0, "expandedKey": None, "expandedPage": 0}, []]])
    assert collapsed_slots[0]["type"] == "session" and collapsed_slots[1:] == [None] * 8
    assert [s["type"] if s else None for s in slots1] == ["back"] + ["agent"] * 8
    assert [s["agent"]["id"] for s in slots1[1:]] == ["big-%d" % n for n in range(1, 9)]
    assert [s["type"] if s else None for s in slots2] == ["back", "agent", "agent"] + [None] * 6
    assert count_expanded == 2 and empty_count == 1


def test_press_behaviour(tmp_path):
    ten = many("big", 10)
    (items,) = model(tmp_path, [["groupAgents", ten]])
    collapsed = {"mode": "collapsed", "collapsedPage": 3, "expandedKey": None, "expandedPage": 0}
    (slots,) = model(tmp_path, [["layoutSlots", dict(collapsed, collapsedPage=0), items]])
    pressed = model(tmp_path, [["press", collapsed, slots[0]]])[0]
    assert pressed["focus"] is None
    assert pressed["view"] == {"mode": "expanded", "collapsedPage": 3, "expandedKey": items[0]["key"], "expandedPage": 0}
    single = many("solo", 1)
    (solo,) = model(tmp_path, [["groupAgents", single]])
    (solo_slots,) = model(tmp_path, [["layoutSlots", dict(collapsed, collapsedPage=0), solo]])
    solo_press = model(tmp_path, [["press", collapsed, solo_slots[0]]])[0]
    assert solo_press["view"]["mode"] == "expanded" and solo_press["focus"] is None
    expanded = pressed["view"]
    (open_slots,) = model(tmp_path, [["layoutSlots", expanded, items]])
    back = model(tmp_path, [["press", expanded, open_slots[0]]])[0]
    assert back == {"view": {"mode": "collapsed", "collapsedPage": 3, "expandedKey": None, "expandedPage": 0}, "focus": None}
    agent = model(tmp_path, [["press", expanded, open_slots[2]]])[0]
    assert agent["view"] == expanded and agent["focus"] == ten[1]
    nothing = model(tmp_path, [["press", expanded, None]])[0]
    assert nothing == {"view": expanded, "focus": None}


def test_reconcile_next_page_and_current_page(tmp_path):
    twelve = [r for n in range(12) for r in many("s%02d" % n, 1)]
    (items,) = model(tmp_path, [["groupAgents", twelve]])
    ghost = {"mode": "expanded", "collapsedPage": 1, "expandedKey": "nope", "expandedPage": 4}
    far = {"mode": "collapsed", "collapsedPage": 9, "expandedKey": None, "expandedPage": 0}
    live = {"mode": "expanded", "collapsedPage": 0, "expandedKey": items[0]["key"], "expandedPage": 5}
    fallback, clamped, clamped_open, wrap_collapsed, wrap_open, page_c, page_e = model(tmp_path, [
        ["reconcile", ghost, items], ["reconcile", far, items], ["reconcile", live, items],
        ["nextPage", {"mode": "collapsed", "collapsedPage": 1, "expandedKey": None, "expandedPage": 0}, items],
        ["nextPage", dict(live, expandedPage=0), items],
        ["currentPage", far], ["currentPage", live]])
    assert fallback == {"mode": "collapsed", "collapsedPage": 1, "expandedKey": None, "expandedPage": 0}
    assert clamped["collapsedPage"] == 1
    assert clamped_open["mode"] == "expanded" and clamped_open["expandedPage"] == 0
    assert wrap_collapsed["collapsedPage"] == 0 and wrap_open["expandedPage"] == 0
    assert (page_c, page_e) == (9, 5)


def test_session_faces(tmp_path):
    cases = [("quoin", 3, "idle", 2000), ("x" * 30, 3, "idle", 2000), ("n" * 30, 12, "idle", 2000),
             ("w", 2, "working", 2000), ("a", 2, "attention", 2000), ("a", 2, "attention", 3000),
             ("d", 1, "done", 2000), ("o", 1, "offline", 2000), ("q", 1, "unknown", 2000)]
    calls = []
    for name, count, status, now in cases:
        calls.append(["groupAgents", many(name, count, status=status, project="/w/quoin")])
    calls_len = len(calls)
    for idx, (name, count, status, now) in enumerate(cases):
        calls.append(["layoutSlots", {"mode": "collapsed", "collapsedPage": 0, "expandedKey": None, "expandedPage": 0}, ref(idx)])
    results = model(tmp_path, calls)
    targets = [results[calls_len + i][0] for i in range(len(cases))]
    faces = model(tmp_path, [["face", t, now] for t, (_, _, _, now) in zip(targets, cases)])
    assert faces[0] == {"lines": ["quoin · 3", "quoin", "", "IDLE"], "color": GREY}
    assert faces[1]["lines"][0] == "x" * 8 + " · 3" and len(faces[1]["lines"][0]) == 12
    assert faces[2]["lines"][0] == "n" * 7 + " · 12" and len(faces[2]["lines"][0]) == 12
    assert faces[3] == {"lines": ["w · 2", "quoin", "", "WORKING"], "color": BLUE}
    assert faces[4]["color"] == ORANGE and faces[4]["lines"][3] == "NEEDS YOU"
    assert faces[5]["color"] == ORANGE_BLINK
    assert faces[6]["color"] == "#227648" and faces[6]["lines"][3] == "DONE"
    assert faces[7]["color"] == "#453b3b" and faces[7]["lines"][3] == "OFFLINE"
    assert faces[8]["color"] == NOSIG and faces[8]["lines"][3] == "NO SIGNAL"


def test_back_empty_agent_and_attention_faces(tmp_path):
    group = {"type": "back", "group": {"session": "a-very-long-session-name"}}
    bare = {"type": "agent", "agent": {"kind": "codex", "status": "idle"}}
    claude = {"type": "agent", "agent": rec("a1", "claude", "quoin-main-session", 3, "/w/quoin", "working")}
    back, empty, standalone, agent, att_none, att_some = model(tmp_path, [
        ["face", group, 2000], ["face", None, 2000], ["face", bare, 2000], ["face", claude, 2000],
        ["attentionFace", 0, 0, 1], ["attentionFace", 2, 1, 3]])
    assert back == {"lines": ["← BACK", "a-very-long-sess", "", ""], "color": NEUTRAL}
    assert empty == {"lines": ["—", "", "", ""], "color": EMPTY}
    assert standalone == {"lines": ["CODEX", "", " · ", "IDLE"], "color": GREY}
    assert agent == {"lines": ["CLAUDE", "quoin", "n-session · 3", "WORKING"], "color": BLUE}
    assert att_none == {"lines": ["ATTENTION", "0", "Page 1/1", "Hold: next page"], "color": QUIET}
    assert att_some == {"lines": ["ATTENTION", "2", "Page 2/3", "Hold: next page"], "color": ORANGE}
