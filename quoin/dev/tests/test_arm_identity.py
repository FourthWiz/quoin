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
LEGACY_ARM = Path(__file__).parent / "fixtures" / "legacy_arm"


def _installer():
    return gate._load_arm_installer(LEGACY_ARM)


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
    for event, matcher, base in gate.arm_registered_stanzas(LEGACY_ARM):
        hooks.setdefault(event, []).append(
            {"matcher": matcher, "hooks": [{"type": "command", "command": f"{claude}/hooks/{base}"}]}
        )
    (claude / "settings.json").write_text(json.dumps({"hooks": hooks}))
    (claude / "hooks").mkdir()
    for name in gate.HOOK_SCRIPTS:
        src = LEGACY_ARM / "quoin" / "hooks" / name
        (claude / "hooks" / name).write_bytes(installer.expected_deployed_content(src, claude))
    return home


def test_quoin_sandbox_passes_and_detects_problems(tmp_path):
    home = _fake_quoin_home(tmp_path)
    assert ai.check_quoin_sandbox(home, LEGACY_ARM) == []
    missing = _canonical()[0]
    import shutil

    shutil.rmtree(home / ".claude" / "skills" / missing)
    assert any(missing in p for p in ai.check_quoin_sandbox(home, LEGACY_ARM))


def test_quoin_sandbox_markers_stripped(tmp_path):
    home = _fake_quoin_home(tmp_path)
    (home / ".claude" / "CLAUDE.md").write_text("no markers")
    assert any("markers" in p for p in ai.check_quoin_sandbox(home, LEGACY_ARM))


def test_quoin_sandbox_extras_exact_vs_subset(tmp_path):
    extras = ("context-tracker", "next_steps 2", "test-model-check")
    home = _fake_quoin_home(tmp_path, extra=extras)
    assert ai.check_quoin_sandbox(home, LEGACY_ARM, exact=True)
    assert ai.check_quoin_sandbox(home, LEGACY_ARM, exact=False) == []
    assert ai.extra_skills(home, LEGACY_ARM) == sorted(extras)


def test_expected_set_is_the_constant_not_the_source_tree():
    on_disk = {
        p.name
        for p in (REPO_ROOT / "quoin" / "adapters" / "claude" / "skills").iterdir()
        if p.is_dir()
    }
    stray = on_disk - set(_canonical())
    # A stray source directory (a sync-conflict copy) does not change what is expected.
    assert set(_canonical()) <= on_disk | set(_canonical())
    assert not (stray & set(_canonical()))


def _plugin_arm(tmp_path):
    """A minimal plugin-era arm: an installer naming the plugin and its runtime paths."""
    arm = tmp_path / "parm"
    (arm / "src" / "quoin").mkdir(parents=True)
    (arm / "src" / "quoin" / "installer.py").write_text(
        'PLUGIN_ID = "quoin@quoin"\n'
        'PLUGIN_PATHS = (".claude-plugin", "adapters/claude/skills", "hooks")\n'
    )
    src = arm / "quoin"
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text('{"name": "quoin", "version": "1.0.0"}')
    for name in ("run", "review"):
        (src / "adapters" / "claude" / "skills" / name).mkdir(parents=True)
        (src / "adapters" / "claude" / "skills" / name / "SKILL.md").write_text(name)
    (src / "adapters" / "claude" / "skills" / "notes").mkdir()
    (src / "hooks").mkdir()
    (src / "hooks" / "rules.sh").write_text("echo rules")
    (src / "dev").mkdir()
    (src / "dev" / "not-shipped.txt").write_text("x")
    return arm


def _plugin_home(tmp_path, arm):
    """A sandbox home laid out as `quoin install` and `claude plugin install` leave it."""
    import shutil

    home = tmp_path / "phome"
    claude = home / ".claude"
    stage = home / ".local" / "share" / "quoin" / "plugin"
    install = claude / "plugins" / "cache" / "quoin" / "quoin" / "1.0.0-abc"
    for copy in (stage, install):
        for rel in (".claude-plugin", "adapters/claude/skills", "hooks"):
            shutil.copytree(arm / "quoin" / rel, copy / rel)
        (copy / ".claude-plugin" / "plugin.json").write_text('{"name": "quoin", "version": "1.0.0+abc"}')
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"quoin@quoin": [{"scope": "user", "installPath": str(install)}]}}
    ))
    (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"quoin": {"source": {"source": "directory", "path": str(stage)}, "installLocation": str(stage)}}
    ))
    (claude / "settings.json").write_text(json.dumps({
        "enabledPlugins": {"quoin@quoin": True},
        "extraKnownMarketplaces": {"quoin": {"source": {"source": "directory", "path": str(stage)}}},
    }))
    return home, install


def test_plugin_tree_canonical_names_are_namespaced(tmp_path):
    arm = _plugin_arm(tmp_path)
    assert ai.canonical_skills(arm) == ("quoin:review", "quoin:run")
    assert ai.uses_plugin_skills(ai.canonical_skills(arm))
    # Namespaced names never collide with the CLI's own skills.
    assert ai.distinctive_quoin_skills(ai.canonical_skills(arm)) == {"quoin:review", "quoin:run"}
    assert ai.canonical_skills(LEGACY_ARM) == _canonical()
    assert not ai.uses_plugin_skills(_canonical())
    repo = ai.canonical_skills(REPO_ROOT)
    assert "quoin:run" in repo and all(name.startswith("quoin:") for name in repo)


def test_plugin_sandbox_passes_and_detects_problems(tmp_path):
    arm = _plugin_arm(tmp_path)
    home, install = _plugin_home(tmp_path, arm)
    assert ai.check_quoin_sandbox(home, arm) == []

    (install / "hooks" / "rules.sh").write_text("echo changed")
    assert any("differ" in p for p in ai.check_quoin_sandbox(home, arm))
    (install / "hooks" / "rules.sh").write_text("echo rules")
    (install / "adapters" / "claude" / "skills" / "run" / "SKILL.md").unlink()
    assert any("missing" in p for p in ai.check_quoin_sandbox(home, arm))


def test_plugin_sandbox_checks_the_marketplace_copy_and_manifest(tmp_path):
    arm = _plugin_arm(tmp_path)
    home, install = _plugin_home(tmp_path, arm)
    stage = home / ".local" / "share" / "quoin" / "plugin"
    (stage / "hooks" / "rules.sh").write_text("echo changed")
    assert any(p.startswith("marketplace copy: plugin files differ") for p in ai.check_quoin_sandbox(home, arm))
    (stage / "hooks" / "rules.sh").write_text("echo rules")
    (install / ".mcp.json").write_text("{}")
    assert any("does not ship: .mcp.json" in p for p in ai.check_quoin_sandbox(home, arm))
    (install / ".mcp.json").unlink()
    (install / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "quoin", "version": "9", "mcpServers": {"x": {}}}')
    assert any("plugin.json differs" in p for p in ai.check_quoin_sandbox(home, arm))
    (install / ".claude-plugin" / "plugin.json").write_text('{"name": "quoin", "version": "2.0.0+zz"}')
    (install / "hooks" / "__pycache__").mkdir()
    (install / "hooks" / "__pycache__" / "x.pyc").write_bytes(b"0")
    assert ai.check_quoin_sandbox(home, arm) == []


def test_plugin_sandbox_requires_enabled_in_sandbox_install(tmp_path):
    arm = _plugin_arm(tmp_path)
    home, install = _plugin_home(tmp_path, arm)
    settings = home / ".claude" / "settings.json"
    original = settings.read_text()
    settings.write_text(json.dumps({"enabledPlugins": {"quoin@quoin": False}}))
    assert any("does not enable" in p for p in ai.check_quoin_sandbox(home, arm))
    settings.write_text(json.dumps({"enabledPlugins": {"quoin@quoin": True, "other@m": True}}))
    assert any("unexpected plugins" in p for p in ai.check_quoin_sandbox(home, arm))
    assert ai.check_quoin_sandbox(home, arm, exact=False) == []
    settings.write_text(original)
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text())
    data["plugins"]["other@m"] = [{"scope": "user", "installPath": str(install)}]
    registry.write_text(json.dumps(data))
    assert any("unexpected plugins installed: other@m" in p for p in ai.check_quoin_sandbox(home, arm))
    del data["plugins"]["other@m"]
    registry.write_text(json.dumps(data))
    known = home / ".claude" / "plugins" / "known_marketplaces.json"
    markets = json.loads(known.read_text())
    known.write_text(json.dumps(dict(markets, evil={"installLocation": "/x"})))
    assert any("unexpected marketplaces registered: evil" in p for p in ai.check_quoin_sandbox(home, arm))
    known.write_text(json.dumps(markets))
    elsewhere = tmp_path / "elsewhere"
    import shutil

    shutil.copytree(install, elsewhere)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"quoin@quoin": [{"scope": "user", "installPath": str(elsewhere)}]}}
    ))
    assert any("outside the sandbox" in p for p in ai.check_quoin_sandbox(home, arm))


def test_plugin_sandbox_flags_user_level_skills(tmp_path):
    arm = _plugin_arm(tmp_path)
    home, _ = _plugin_home(tmp_path, arm)
    (home / ".claude" / "skills" / "run").mkdir(parents=True)
    assert any("unexpected skills: run" in p for p in ai.check_quoin_sandbox(home, arm))
    assert ai.check_quoin_sandbox(home, arm, exact=False) == []
    assert ai.extra_skills(home, arm) == ["run"]


def test_init_check_accepts_plugin_skill_names(tmp_path):
    canonical = ("quoin:review", "quoin:run")
    identity = ai.ArmIdentity(
        mode=ai.QUOIN_REQUIRED, config_dir=None, canonical=canonical,
        distinctive=ai.distinctive_quoin_skills(canonical), arm_config_mode="symmetric",
    )
    init = {"type": "system", "subtype": "init", "skills": ["run", "review", "quoin:run"]}
    check = ai.InitIdentityCheck(identity)
    assert check.observe(init) == ai.QUOIN_REASON
    assert "quoin:review" in check.detail
    check = ai.InitIdentityCheck(identity)
    assert check.observe(dict(init, skills=["run", "quoin:run", "quoin:review"])) is None
    plain = ai.InitIdentityCheck(ai.ArmIdentity(
        mode=ai.QUOIN_FREE, config_dir=None, canonical=canonical,
        distinctive=ai.distinctive_quoin_skills(canonical), arm_config_mode="symmetric",
    ))
    assert plain.observe(dict(init, skills=["run", "quoin:run"])) == ai.PLAIN_REASON


def test_init_check_requires_the_quoin_plugin_from_the_sandbox(tmp_path):
    canonical = ("quoin:run",)
    box = tmp_path / "box"
    identity = ai.ArmIdentity(
        mode=ai.QUOIN_REQUIRED, config_dir=box / ".claude", canonical=canonical,
        distinctive=ai.distinctive_quoin_skills(canonical), arm_config_mode="symmetric",
    )
    builtin = {"name": "cc-plugin-x", "path": "builtin", "source": "cc-plugin-x@builtin"}
    ours = {"name": "quoin", "path": str(box / ".local" / "share" / "quoin" / "plugin")}
    init = {"type": "system", "subtype": "init", "skills": ["quoin:run"], "plugins": [ours, builtin]}
    assert ai.InitIdentityCheck(identity).observe(init) is None
    check = ai.InitIdentityCheck(identity)
    assert check.observe(dict(init, plugins=[ours, {"name": "other", "path": str(box)}])) == ai.QUOIN_REASON
    assert check.detail == "unexpected-plugin:other"
    check = ai.InitIdentityCheck(identity)
    assert check.observe(dict(init, plugins=[{"name": "quoin", "path": "/operator/plugin"}])) == ai.QUOIN_REASON
    assert check.detail.startswith("plugin-loaded-outside-sandbox")
