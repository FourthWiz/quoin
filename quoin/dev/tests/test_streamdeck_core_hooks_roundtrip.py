"""The component's hook entries coexist with the core installer's `deploy_hooks`.

Core `_append_stanza` moves its own stanzas to the end of each event list on
every run, so only membership and the relative order of foreign items are
asserted, not the absolute index of our object (Claude has no position-keyed
trust).
"""
import json
import pathlib

import pytest

import _streamdeck_lifecycle as lc
from _streamdeck_lifecycle import deck_home, forbid_dangerous_subprocess, lifecycle_home  # noqa: F401
from quoin.installer import deploy_hooks
from quoin.streamdeck import hooks_merge as hm, jsonfile

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess")
QUOIN_SRC = pathlib.Path(__file__).resolve().parents[3] / "quoin"


def component_merge(settings, runtime):
    created = []
    cmd = hm.managed_command(runtime, "claude")

    def mutate(data):
        new, report = hm.merge(data, "claude", cmd, runtime_names=("QuoinStreamDeck",))
        created.extend(report.created_event_keys)
        return new

    jsonfile.update(settings, mutate, create_parents=True)
    return created


def non_owned(settings):
    data = json.loads(settings.read_text())
    return {ev: [g for g in lst if not any(
        hm.classify(h.get("command"), runtime_names=("QuoinStreamDeck",)) for h in g.get("hooks", []))]
        for ev, lst in data["hooks"].items()}


def owned_count(settings):
    data = json.loads(settings.read_text())
    return {ev: sum(1 for g in lst for h in g["hooks"]
                    if hm.classify(h.get("command"), runtime_names=("QuoinStreamDeck",)))
            for ev, lst in data["hooks"].items()}


def same_membership(a, b):
    return {k: sorted(json.dumps(g, sort_keys=True) for g in v) for k, v in a.items() if v} == \
           {k: sorted(json.dumps(g, sort_keys=True) for g in v) for k, v in b.items() if v}


def test_component_then_core(lifecycle_home, capsys):
    dest = lifecycle_home.home / ".claude"
    settings = dest / "settings.json"
    lc.make_settings(settings, foreign=True, core=False)
    runtime = str(lifecycle_home.runtime)
    component_merge(settings, runtime)
    deploy_hooks(QUOIN_SRC, dest)
    capsys.readouterr()
    counts = owned_count(settings)
    assert all(counts[e] == 1 for e in hm.EVENTS["claude"])
    cmd = hm.managed_command(runtime, "claude")
    data = json.loads(settings.read_text())
    assert all(h["command"] == cmd for e in hm.EVENTS["claude"] for g in data["hooks"][e] for h in g["hooks"]
               if hm.classify(h["command"], runtime_names=("QuoinStreamDeck",)))
    assert any("userpromptsubmit.sh" in h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"])


def test_core_component_core_upgrade_remove(lifecycle_home, capsys):
    dest = lifecycle_home.home / ".claude"
    settings = dest / "settings.json"
    lc.make_settings(settings, foreign=True, core=False)
    settings_before = json.loads(settings.read_text())
    deploy_hooks(QUOIN_SRC, dest)
    after_core = non_owned(settings)
    created = component_merge(settings, str(lifecycle_home.runtime))
    assert same_membership(non_owned(settings), after_core)
    deploy_hooks(QUOIN_SRC, dest)
    capsys.readouterr()
    after_core2 = non_owned(settings)
    new_runtime = str(lifecycle_home.root / "elsewhere" / "QuoinStreamDeck")
    component_merge(settings, new_runtime)  # upgrade: replaces in place, creates no keys
    assert same_membership(non_owned(settings), after_core2)
    assert sum(owned_count(settings).values()) == len(hm.EVENTS["claude"])

    def drop(data):
        return hm.remove(data, "claude", runtime_names=("QuoinStreamDeck",), removable_event_keys=created)[0]

    jsonfile.update(settings, drop)
    final = json.loads(settings.read_text())
    assert sum(owned_count(settings).values()) == 0
    assert same_membership(non_owned(settings), after_core2)
    assert final["theme"] == settings_before["theme"]
    assert set(settings_before["permissions"]["deny"]) <= set(final["permissions"]["deny"])
    assert any("stop.sh" in h["command"] for g in final["hooks"]["Stop"] for h in g["hooks"])
    for ev in created:
        assert ev not in final["hooks"]
