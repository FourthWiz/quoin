"""Pure hook merge / remove / inspect rules."""
import copy
import json
import random
import shlex

import pytest

from quoin.streamdeck import hooks_merge as hm

RT = "/Users/x/Library/Application Support/QuoinStreamDeck"
RT2 = "/other/rt/QuoinStreamDeck"
NAMES = ("QuoinStreamDeck",)
CMD = hm.managed_command(RT, "claude")
LEGACY = "/usr/bin/python3 %s claude" % shlex.quote(RT + "/monitor.py")

# Frozen copy of the prototype's event lists (base events plus per-runtime extras), in its order.
PROTO_BASE = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
              "PostToolUse", "Stop", "PreCompact", "PostCompact")
PROTO_CLAUDE = PROTO_BASE + ("PostToolUseFailure", "Notification", "StopFailure", "Elicitation", "ElicitationResult")
PROTO_CODEX = PROTO_BASE + ("Interrupt",)


def foreign(command="/usr/local/bin/other"):
    return {"hooks": [{"type": "command", "command": command}]}


def owned_of(data, event):
    return [h for g in data["hooks"].get(event, []) for h in g.get("hooks", []) if isinstance(h, dict)
            and hm.classify(h.get("command"), runtime_names=NAMES) is not None]


def test_registry_matches_prototype_order():
    assert hm.EVENTS["claude"] == PROTO_CLAUDE and hm.EVENTS["codex"] == PROTO_CODEX
    assert "Interrupt" in hm.EVENTS["codex"] and "Interrupt" not in hm.EVENTS["claude"]


def test_managed_command_is_quoted_and_stable():
    assert CMD == shlex.quote(RT + "/bin/quoin-deck-hook") + " claude"
    assert shlex.split(CMD) == [RT + "/bin/quoin-deck-hook", "claude"]
    assert hm.managed_command(RT, "claude") == CMD


def test_merge_preserves_settings_and_is_idempotent():
    old = {"theme": "dark", "hooks": {"Stop": [foreign("existing-hook")]}}
    snapshot = copy.deepcopy(old)
    once, report = hm.merge(old, "claude", CMD, runtime_names=NAMES)
    assert old == snapshot
    again, second = hm.merge(once, "claude", CMD, runtime_names=NAMES)
    assert again == once and second.changed is False and report.changed is True
    assert once["hooks"]["Stop"][0] == old["hooks"]["Stop"][0]
    assert once["theme"] == "dark"
    assert all(len(owned_of(once, e)) == 1 for e in hm.EVENTS["claude"])
    assert "Stop" not in report.created_event_keys and "SessionStart" in report.created_event_keys


def test_property_foreign_items_survive_merge_upgrade_and_remove():
    rng = random.Random(7)
    events = list(hm.EVENTS["claude"]) + ["CustomEvent"]
    for _ in range(200):
        data = {"hooks": {}}
        for ev in rng.sample(events, rng.randint(0, 5)):
            data["hooks"][ev] = [
                rng.choice([foreign("f%d" % rng.randint(0, 99)),
                            {"matcher": "Bash", "hooks": [{"type": "command", "command": "m%d" % rng.randint(0, 9)}]}])
                for _ in range(rng.randint(0, 3))]

        def foreigners(d):
            return {ev: [g for g in lst if not any(
                hm.classify(h.get("command"), runtime_names=NAMES) for h in g.get("hooks", []))]
                for ev, lst in d["hooks"].items()}

        base = {ev: lst for ev, lst in foreigners(data).items() if lst}
        merged, rep = hm.merge(data, "claude", CMD, runtime_names=NAMES)
        assert {ev: lst for ev, lst in foreigners(merged).items() if lst} == base
        upgraded, _ = hm.merge(merged, "claude", hm.managed_command(RT2, "claude"), runtime_names=("QuoinStreamDeck",))
        assert {ev: lst for ev, lst in foreigners(upgraded).items() if lst} == base
        removed, _ = hm.remove(upgraded, "claude", runtime_names=NAMES, removable_event_keys=rep.created_event_keys)
        assert removed["hooks"] == data["hooks"] or {ev: lst for ev, lst in removed["hooks"].items() if lst} == base


def test_replace_in_place_keeps_position_when_runtime_changes():
    data = {"hooks": {"Stop": [foreign("a"), {"hooks": [{"type": "command", "command": CMD, "timeout": 3}]}, foreign("b")]}}
    new, _ = hm.merge(data, "claude", hm.managed_command(RT2, "claude"), runtime_names=NAMES)
    assert new["hooks"]["Stop"][1]["hooks"][0]["command"] == hm.managed_command(RT2, "claude")
    assert [g["hooks"][0]["command"] for g in new["hooks"]["Stop"]][::2] == ["a", "b"]


def test_legacy_entries_replaced_in_place_and_duplicates_collapsed():
    data = {"hooks": {"Stop": [foreign("a"),
                               {"hooks": [{"type": "command", "command": LEGACY, "timeout": 3},
                                          {"type": "command", "command": "keep-me"}]},
                               {"hooks": [{"type": "command", "command": CMD}]}]}}
    new, _ = hm.merge(data, "claude", CMD, runtime_names=NAMES)
    stop = new["hooks"]["Stop"]
    assert [g["hooks"][0]["command"] for g in stop] == ["a", CMD]
    assert stop[1]["hooks"][1]["command"] == "keep-me"
    assert len(owned_of(new, "Stop")) == 1


def test_owned_under_non_registry_event_is_removed_but_key_kept():
    data = {"hooks": {"Interrupt": [{"hooks": [{"type": "command", "command": CMD}]}]}}
    new, report = hm.merge(data, "claude", CMD, runtime_names=NAMES)
    assert new["hooks"]["Interrupt"] == [] and report.stale_removed == ["Interrupt"]


@pytest.mark.parametrize("data", [{"hooks": []}, {"hooks": {"Stop": {}}}])
def test_shape_errors_raise(data):
    with pytest.raises(hm.HooksShapeError):
        hm.merge(data, "claude", CMD, runtime_names=NAMES)


def test_malformed_groups_are_foreign_and_left_identical():
    weird = ["str", {"nohooks": 1}, {"hooks": "x"}, {"hooks": [7, None, "s", {"command": 5}]}]
    data = {"hooks": {"Stop": copy.deepcopy(weird)}}
    new, _ = hm.merge(data, "claude", CMD, runtime_names=NAMES)
    assert new["hooks"]["Stop"][:4] == weird and len(new["hooks"]["Stop"]) == 5
    gone, _ = hm.remove(new, "claude", runtime_names=NAMES)
    assert gone["hooks"]["Stop"] == weird
    states = hm.inspect(data, "claude", CMD, runtime_names=NAMES)
    assert states["Stop"] == "missing"


def test_garbage_and_lookalike_commands_are_foreign():
    assert hm.classify("a 'b", runtime_names=NAMES) is None
    assert hm.classify("/x/some-tool claude", runtime_names=NAMES) is None
    assert hm.classify("/usr/bin/python3 /else/monitor.py claude", runtime_names=NAMES) is None
    assert hm.classify(LEGACY, runtime_names=NAMES) == "legacy"
    assert hm.classify(CMD, runtime_names=NAMES) == "managed"
    assert hm.classify("custom cmd", runtime_names=NAMES, recorded=("custom cmd",)) == "recorded"


def test_remove_drops_only_created_keys_and_reports_shifts():
    data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": CMD}, {"type": "command", "command": "f"}]}],
                      "SessionStart": [{"hooks": [{"type": "command", "command": CMD}]}]}}
    new, report = hm.remove(data, "claude", runtime_names=NAMES, removable_event_keys=["SessionStart"])
    assert "SessionStart" not in new["hooks"] and report.removed_event_keys == ["SessionStart"]
    assert new["hooks"]["Stop"][0]["hooks"] == [{"type": "command", "command": "f"}]
    assert report.shifted_events == ["Stop"]
    kept, _ = hm.remove(data, "claude", runtime_names=NAMES)
    assert kept["hooks"]["SessionStart"] == []


def test_inspect_states():
    merged, _ = hm.merge({"hooks": {}}, "claude", CMD, runtime_names=NAMES)
    assert set(hm.inspect(merged, "claude", CMD, runtime_names=NAMES).values()) == {"ok"}
    broken = copy.deepcopy(merged)
    del broken["hooks"]["Stop"]
    broken["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 10
    broken["hooks"]["SessionEnd"].append({"hooks": [{"type": "command", "command": CMD, "timeout": 3}]})
    broken["hooks"]["PreToolUse"][0]["matcher"] = "Bash"
    broken["hooks"]["PostToolUse"][0]["hooks"].append({"type": "command", "command": "f"})
    broken["hooks"]["Interrupt"] = [{"hooks": [{"type": "command", "command": LEGACY}]}]
    states = hm.inspect(broken, "claude", CMD, runtime_names=NAMES)
    assert states["Stop"] == "missing" and states["SessionStart"] == "stale"
    assert states["SessionEnd"] == "duplicate" and states["PreToolUse"] == "shadowed"
    assert states["PostToolUse"] == "shadowed" and states["Interrupt"] == "stale"
    assert states["UserPromptSubmit"] == "ok"
    legacy_state = hm.inspect({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": LEGACY, "timeout": 3}]}]}},
                              "claude", CMD, runtime_names=NAMES)
    assert legacy_state["Stop"] == "stale"


def test_legacy_only_events():
    data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": LEGACY}]}],
                      "SessionStart": [{"hooks": [{"type": "command", "command": LEGACY}, {"command": "x"}]}]}}
    assert hm.legacy_only_events(data, "claude", NAMES) == ["Stop"]
