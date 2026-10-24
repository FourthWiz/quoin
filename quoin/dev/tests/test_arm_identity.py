"""Tests for the arm identity checks.

The stream fixtures under fixtures/benchmark_streams are cut from recorded
CLI sessions: init_contaminated_plain is the first events of a plain-arm
session that inherited the operator's config (two SessionStart hooks, then
init); init_quoin_main is the same events from a session with quoin installed;
init_isolated_plain is the init event of a session run against an isolated,
empty config directory, which still lists the CLI's built-in `run` skill.
Operator paths in the fixtures are the placeholder {CONFIG_DIR}.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from quoin.benchmarks.harness import arm_identity as ai
from quoin.benchmarks.scripts import run_three_arm_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "benchmark_streams"


def _installer():
    return gate._load_arm_installer(REPO_ROOT)


def _canonical():
    return tuple(_installer().CANONICAL_SKILLS)


def _load(name, config_dir):
    events = []
    for line in (FIXTURES / (name + ".jsonl")).read_text().splitlines():
        events.append(json.loads(line.replace("{CONFIG_DIR}", str(config_dir))))
    return events


def _identity(mode, config_dir):
    canonical = _canonical()
    return ai.ArmIdentity(
        mode=mode,
        config_dir=config_dir,
        canonical=canonical,
        distinctive=ai.distinctive_quoin_skills(canonical),
        arm_config_mode="symmetric",
    )


def _feed(identity, events):
    check = ai.InitIdentityCheck(identity)
    for index, event in enumerate(events):
        reason = check.observe(event)
        if reason:
            return reason, index, check
    return None, None, check


def test_contaminated_plain_trips_on_first_hook_event(tmp_path):
    events = _load("init_contaminated_plain", tmp_path)
    reason, index, _ = _feed(_identity(ai.QUOIN_FREE, tmp_path), events)
    assert reason == "plain-arm-contaminated"
    assert index == 0


def test_contaminated_plain_without_hooks_trips_on_skills(tmp_path):
    events = [e for e in _load("init_contaminated_plain", tmp_path) if e.get("subtype") == "init"]
    reason, _, check = _feed(_identity(ai.QUOIN_FREE, tmp_path), events)
    assert reason == "plain-arm-contaminated"
    assert "thorough_plan" in check.record()["forbidden_names"]


def test_isolated_plain_passes_even_though_it_lists_run(tmp_path):
    events = _load("init_isolated_plain", tmp_path)
    assert "run" in events[0]["skills"]
    reason, _, check = _feed(_identity(ai.QUOIN_FREE, tmp_path), events)
    assert reason is None
    assert check.record()["init_seen"] is True


_BUILTIN_PLUGINS = [
    {"name": "cc-plugin-agents-md", "path": "builtin", "source": "cc-plugin-agents-md@builtin"},
    {"name": "cc-plugin-telemetry", "path": "builtin", "source": "cc-plugin-telemetry@builtin"},
]


def test_isolated_plain_passes_with_builtin_plugins(tmp_path):
    events = _load("init_isolated_plain", tmp_path)
    events[0]["plugins"] = copy.deepcopy(_BUILTIN_PLUGINS)
    reason, _, check = _feed(_identity(ai.QUOIN_FREE, tmp_path), events)
    assert reason is None
    assert check.record()["builtin_plugins"] == ["cc-plugin-agents-md", "cc-plugin-telemetry"]


@pytest.mark.parametrize(
    "plugin",
    [
        {"name": "demo", "path": "/home/u/.claude/plugins/cache/demo", "source": "demo@market"},
        "demo",
    ],
)
def test_isolated_plain_trips_on_installed_plugin(tmp_path, plugin):
    events = _load("init_isolated_plain", tmp_path)
    events[0]["plugins"] = copy.deepcopy(_BUILTIN_PLUGINS) + [plugin]
    reason, _, check = _feed(_identity(ai.QUOIN_FREE, tmp_path), events)
    assert reason == "plain-arm-contaminated"
    assert check.record()["failure_detail"] == "plugins-listed:demo"


def test_builtin_collisions_cover_isolated_fixture(tmp_path):
    names = set(_load("init_isolated_plain", tmp_path)[0]["skills"])
    collisions = names & set(_canonical())
    assert collisions
    assert collisions <= ai.BUILTIN_NAME_COLLISIONS


def test_plain_trips_on_foreign_memory_path(tmp_path):
    events = _load("init_isolated_plain", tmp_path)
    events[0]["memory_paths"] = {"auto": "/somewhere/else/memory/"}
    reason, _, _ = _feed(_identity(ai.QUOIN_FREE, tmp_path), events)
    assert reason == "plain-arm-contaminated"


def test_var_and_private_var_do_not_trip(tmp_path):
    real = Path(os.path.realpath(tmp_path))
    link = tmp_path.parent / (tmp_path.name + "_link")
    link.symlink_to(real)
    events = _load("init_isolated_plain", link)
    reason, _, _ = _feed(_identity(ai.QUOIN_FREE, real), events)
    assert reason is None


def test_quoin_fixture_passes_and_missing_skill_trips(tmp_path):
    events = _load("init_quoin_main", tmp_path)
    init = next(e for e in events if e.get("subtype") == "init")
    init["skills"] = list(_canonical()) + ["debug"]
    reason, _, _ = _feed(_identity(ai.QUOIN_REQUIRED, tmp_path), copy.deepcopy(events))
    assert reason is None
    init["skills"] = [s for s in init["skills"] if s != "thorough_plan"]
    reason, _, check = _feed(_identity(ai.QUOIN_REQUIRED, tmp_path), events)
    assert reason == "quoin-arm-missing-quoin"
    assert check.record()["missing_names"] == ["thorough_plan"]


def test_asymmetric_quoin_ignores_memory_paths():
    events = _load("init_quoin_main", "/anywhere")
    init = next(e for e in events if e.get("subtype") == "init")
    init["skills"] = list(_canonical())
    reason, _, _ = _feed(_identity(ai.QUOIN_REQUIRED, None), events)
    assert reason is None


@pytest.mark.parametrize("mode", [ai.QUOIN_FREE, ai.QUOIN_REQUIRED])
def test_assistant_before_init_trips(tmp_path, mode):
    check = ai.InitIdentityCheck(_identity(mode, tmp_path))
    assert check.observe({"type": "assistant"}) is not None
    assert check.record()["failure_detail"] == "no-init-event"


def test_unreadable_skills_trip(tmp_path):
    check = ai.InitIdentityCheck(_identity(ai.QUOIN_FREE, tmp_path))
    assert check.observe({"type": "system", "subtype": "init"}) == "plain-arm-contaminated"
    assert check.record()["failure_detail"] == "init-skills-unreadable"


def test_trip_reported_once(tmp_path):
    check = ai.InitIdentityCheck(_identity(ai.QUOIN_FREE, tmp_path))
    assert check.observe({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"})
    assert check.observe({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"}) is None


def test_distinctive_set():
    distinctive = ai.distinctive_quoin_skills(_canonical())
    assert {"thorough_plan", "end_of_task", "architect"} <= distinctive
    assert not (distinctive & ai.BUILTIN_NAME_COLLISIONS)


# ---------- static checks ----------

def _plain_home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    return home


def test_plain_sandbox_clean_passes(tmp_path):
    assert ai.check_plain_sandbox(_plain_home(tmp_path)) == []


def test_plain_sandbox_failures(tmp_path):
    home = _plain_home(tmp_path)
    (home / ".claude" / "skills" / "x").mkdir(parents=True)
    assert ai.check_plain_sandbox(home)
    home = _plain_home(tmp_path / "b")
    (home / ".claude" / "CLAUDE.md").write_text("x")
    assert ai.check_plain_sandbox(home)
    home = _plain_home(tmp_path / "c")
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{}]}}))
    assert any("hooks" in p for p in ai.check_plain_sandbox(home))
    home = _plain_home(tmp_path / "d")
    (home / ".claude.json").write_text("{}")
    assert ai.check_plain_sandbox(home)


def test_workdir_ancestor_claude_md(tmp_path):
    work = tmp_path / "a" / "b"
    work.mkdir(parents=True)
    assert [p for p in ai.check_workdir(work) if str(tmp_path) in p] == []
    (tmp_path / "a" / "CLAUDE.md").write_text("x")
    assert any("CLAUDE.md" in p for p in ai.check_workdir(work))


def _fake_quoin_home(tmp_path, extra=()):
    installer = _installer()
    home = tmp_path / "qhome"
    claude = home / ".claude"
    for name in list(installer.CANONICAL_SKILLS) + list(extra):
        (claude / "skills" / name).mkdir(parents=True)
        (claude / "skills" / name / "SKILL.md").write_text("x")
    claude.joinpath("CLAUDE.md").write_text("\n".join(ai.RULES_MARKERS))
    hooks = {}
    for event, matcher, base in gate.arm_registered_stanzas(REPO_ROOT):
        hooks.setdefault(event, []).append(
            {"matcher": matcher, "hooks": [{"type": "command", "command": f"{claude}/hooks/{base}"}]}
        )
    (claude / "settings.json").write_text(json.dumps({"hooks": hooks}))
    (claude / "hooks").mkdir()
    for name in gate.HOOK_SCRIPTS:
        src = REPO_ROOT / "quoin" / "hooks" / name
        (claude / "hooks" / name).write_bytes(installer.expected_deployed_content(src, claude))
    return home


def test_quoin_sandbox_passes_and_detects_problems(tmp_path):
    home = _fake_quoin_home(tmp_path)
    assert ai.check_quoin_sandbox(home, REPO_ROOT) == []
    missing = _canonical()[0]
    import shutil

    shutil.rmtree(home / ".claude" / "skills" / missing)
    assert any(missing in p for p in ai.check_quoin_sandbox(home, REPO_ROOT))


def test_quoin_sandbox_markers_stripped(tmp_path):
    home = _fake_quoin_home(tmp_path)
    (home / ".claude" / "CLAUDE.md").write_text("no markers")
    assert any("markers" in p for p in ai.check_quoin_sandbox(home, REPO_ROOT))


def test_quoin_sandbox_extras_exact_vs_subset(tmp_path):
    extras = ("context-tracker", "next_steps 2", "test-model-check")
    home = _fake_quoin_home(tmp_path, extra=extras)
    assert ai.check_quoin_sandbox(home, REPO_ROOT, exact=True)
    assert ai.check_quoin_sandbox(home, REPO_ROOT, exact=False) == []
    assert ai.extra_skills(home, REPO_ROOT) == sorted(extras)


def test_expected_set_is_the_constant_not_the_source_tree():
    on_disk = {p.name for p in (REPO_ROOT / "quoin" / "skills").iterdir() if p.is_dir()}
    stray = on_disk - set(_canonical())
    # A stray source directory (a sync-conflict copy) does not change what is expected.
    assert set(_canonical()) <= on_disk | set(_canonical())
    assert not (stray & set(_canonical()))
