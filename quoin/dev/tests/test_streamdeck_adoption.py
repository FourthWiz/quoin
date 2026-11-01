"""Adopting the prototype install: nothing running is interrupted, nothing of the user's is lost."""
import json
import os

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
from quoin.streamdeck import engine, hooks_merge, macos, manifest, payload_build, prereqs, shims, status
import quoin.streamdeck as sd

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
NAMES = ("QuoinStreamDeck",)


def collect(lh, now=lc.NOW):
    report = status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN, now=now)
    return status.finalize(report)


def codes(report):
    return {p["code"] for p in report["problems"]}


def load(path):
    with open(str(path)) as handle:
        return json.load(handle)


def marker_of(lh):
    return load(lh.runtime / "operation.json")


def codex_path(lh):
    return lh.codex_home / "hooks.json"


def empty_path(env, lh, *, legacy_node_ok=True):
    """No tool is found on PATH; only absolute paths harvested from the prototype can be probed."""
    env.fake_ctx.which_table.clear()
    LEGACY_NODE = str(lh.root / "legacy" / "node")
    if legacy_node_ok:
        env.fake_ctx.probes[(LEGACY_NODE, "-e", prereqs.WEBSOCKET_PROBE)] = (0, "", "")
        env.fake_ctx.probes[(LEGACY_NODE, "--version")] = (0, "v24.1.0\n", "")
    return env


def classify_all(data, kind, lh):
    """[(event, group index, hook index, class)] for every hook of the file."""
    out = []
    for event, groups in (data.get("hooks") or {}).items():
        for gi, group in enumerate(groups):
            for hi, hook in enumerate(group.get("hooks") or []):
                cls = hooks_merge.classify(hook.get("command"), runtime_names=NAMES)
                out.append((event, gi, hi, cls or "foreign"))
    return out


def foreign_sequence(data):
    return [(event, [hook for group in groups for hook in group["hooks"]
                     if hooks_merge.classify(hook.get("command"), runtime_names=NAMES) is None])
            for event, groups in sorted((data.get("hooks") or {}).items())]


def owned_positions(data):
    return {(event, gi, hi): cls for event, gi, hi, cls in classify_all(data, None, None) if cls != "foreign"}


def legacy_command(lh):
    return lc.legacy_command(lh.runtime).replace("/usr/bin/python3", lc.fixture_python(lh))


def spy_rollbacks(monkeypatch):
    calls = []
    real = engine.rollback
    monkeypatch.setattr(engine, "rollback", lambda *a, **k: (calls.append("rollback"), real(*a, **k))[1])
    return calls


# ------------------------------------------------------------------ full adoption

def test_full_adoption_keeps_everything_and_swaps_hooks_in_place(lifecycle_home):
    lh = lifecycle_home
    profile = ops.make_adoption_fixture(lh)
    before = {k: load(p) for k, p in (("claude", lh.claude_settings), ("codex", codex_path(lh)))}
    legacy_positions = {k: owned_positions(v) for k, v in before.items()}
    plugin_manifest = load(lh.plugin_dir / "manifest.json")
    profile_snapshot = lc.tree_snapshot(profile)
    backups_before = lc.tree_snapshot(lh.runtime / "backups")
    kept = {rel: lc.tree_snapshot(lh.runtime / rel) if (lh.runtime / rel).is_dir() else (lh.runtime / rel).read_bytes()
            for rel in ("Buttons", ".DS_Store", "health.json")}
    result = engine.install(ops.make_env(lh))
    data = load(lh.runtime / "install-manifest.json")
    assert manifest.validate(data) == [] and data["mode"] == "active" and data["adopted_from"] == "prototype"
    for kind, path in (("claude", lh.claude_settings), ("codex", codex_path(lh))):
        after = load(path)
        positions = owned_positions(after)
        assert set(positions) == set(legacy_positions[kind]) and set(positions.values()) == {"managed"}
        assert len(positions) == len({e for e, _, _ in positions})
        assert foreign_sequence(after) == foreign_sequence(before[kind])
        assert after["theme"] == "dark"
    config = load(lh.state_root / "config.json")
    assert config == {"project": "/work/p", "session": "main", "projects": {"main": "/work/p"}}
    socket = str(lh.root / "zsock")
    assert load(lh.state_root / "state" / "surfaces.json") == {socket + "\tmain": "surface-1"}
    assert sorted(os.listdir(str(lh.state_root / "state" / "agents"))) == ["rec0.json", "rec1.json", "rec2.json"]
    assert not (lh.state_root / "agents").exists() or not os.listdir(str(lh.state_root / "agents"))
    new_manifest = load(lh.plugin_dir / "manifest.json")
    assert new_manifest["UUID"] == plugin_manifest["UUID"]
    assert [a["UUID"] for a in new_manifest["Actions"]] == [a["UUID"] for a in plugin_manifest["Actions"]]
    assert lc.tree_snapshot(profile) == profile_snapshot
    after_backups = lc.tree_snapshot(lh.runtime / "backups")
    for rel, value in backups_before.items():
        assert after_backups[rel] == value
    added = {rel.split(os.sep)[0] for rel in after_backups if rel not in backups_before}
    assert added and all(name.startswith("quoin-") for name in added)
    for rel, value in kept.items():
        current = lc.tree_snapshot(lh.runtime / rel) if (lh.runtime / rel).is_dir() else (lh.runtime / rel).read_bytes()
        assert current == value, rel
    forwarders = {e["rel"] for e in data["files"] if e["kind"] == "forwarder"}
    assert forwarders == set(shims.FORWARDER_NAMES)
    assert collect(lh)["exit_code"] == 0
    text = "\n".join(result.lines)
    assert "Codex hook definitions changed once (prototype -> managed); review them in Codex /hooks" in text
    assert "restart these agents to pick up managed hooks:" in text
    assert text.count("claude session s") == 3
    assert data["legacy_owned_events"]["claude"] and data["legacy_owned_events"]["codex"]


# ------------------------------------------------------------------ nothing running is stopped

def test_adoption_never_kills_quits_or_restarts_anything(lifecycle_home, subprocess_calls):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    env = ops.make_env(lh, running=True)
    engine.install(env)
    bad = ("kill", "pkill", "killall", "osascript", "launchctl", "zellij", "open")
    argvs = list(env.fake_sys.probe_calls) + list(env.fake_sys.run_calls) + list(subprocess_calls)
    for argv in argvs:
        assert os.path.basename(argv[0]) not in bad, argv
    assert all(os.path.basename(a[0]) == "pgrep" for a in env.fake_sys.probe_calls)
    assert env.fake_sys.run_calls == []


def test_the_legacy_command_stays_inert_at_every_step_boundary(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    legacy = legacy_command(lh)
    seen = []

    def check(name):
        seen.append(name)
        ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=[legacy]))

    check("before")
    engine.install(ops.make_env(lh, on_step=check))
    assert seen[1:] == ["stage_payload", "set_aside", "swap_current", "write_entry_points", "plugin_swap", "commit",
                        "state_migration", "claude_settings", "codex_settings", "finalize"]
    check("after")
    # an agent that started before the adoption keeps the legacy command it read at start
    result = ops.run_hook_commands(lh, extra=[legacy])
    assert any(cmd == legacy for cmd, _, _, _ in result)


def test_a_rolled_back_adoption_restores_the_prototype_and_keeps_records_written_in_the_window(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    before = ops.prototype_snapshot(lh)
    legacy = legacy_command(lh)
    window = {}

    def during(name):
        ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=[legacy]))
        if name == "write_entry_points":
            # the forwarders are live: the new monitor writes into the new store, newer than anything legacy
            lc.write_json(lh.state_root / "state" / "agents" / "window.json", {
                "kind": "claude", "session": "w", "pane": "9", "updated": 5000.0, "id": "window"})
            window["written"] = True

    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, on_step=during, faults={"plugin_swap:after": RuntimeError("boom")}))
    assert window
    assert ops.prototype_snapshot(lh) == before
    assert load(lh.state_root / "agents" / "window.json")["id"] == "window"
    ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=[legacy]))
    assert not (lh.runtime / "operation.json").exists()
    assert collect(lh)["exit_code"] == 11


# ------------------------------------------------------------------ prerequisites missing

def test_an_emptied_path_still_adopts_with_the_harvested_binaries(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    env = empty_path(ops.make_env(lh, app_version="7.0"), lh)
    result = engine.install(env)
    data = load(lh.runtime / "install-manifest.json")
    assert data["mode"] == "active" and data["adopted_from"] == "prototype"
    assert data["binaries"]["python"]["path"] == lc.fixture_python(lh)
    assert data["binaries"]["node"] == {"mode": "external", "path": str(lh.root / "legacy" / "node"), "version": "v24.1.0"}
    report = collect(lh)
    assert report["exit_code"] == 0, report["problems"]


def test_failing_probes_leave_the_prototype_untouched_until_they_pass(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    before = ops.prototype_snapshot(lh)
    env = empty_path(ops.make_env(lh, app_version="7.0"), lh, legacy_node_ok=False)
    result = engine.install(env)
    assert result.mode == "adoption-pending"
    after = ops.prototype_snapshot(lh)
    for key, value in before.items():
        if key.startswith(("plugin/", "settings/")) or key in {k for k in before if k.startswith("runtime/") and
                                                               not k.startswith(("runtime/versions", "runtime/install-manifest", "runtime/operation", "runtime/.install"))}:
            assert after.get(key) == value, key
    data = load(lh.runtime / "install-manifest.json")
    assert data["mode"] == "adoption-pending" and data["hooks"] == [] and data["legacy_owned_events"]["claude"]
    assert not (lh.runtime / "current").exists() and not (lh.runtime / "bin").exists()
    report = collect(lh)
    assert report["exit_code"] == 11 and "MODE_ADOPTION_PENDING" in codes(report)
    ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=[legacy_command(lh)]))
    # later, with the probes passing, the adoption completes (dispatch by mode, harvest re-run)
    result = engine.install(empty_path(ops.make_env(lh, app_version="7.0", now=ops.LATER), lh))
    data = load(lh.runtime / "install-manifest.json")
    assert data["mode"] == "active" and data["adopted_from"] == "prototype" and data["hooks"]
    assert collect(lh)["exit_code"] == 0


@pytest.mark.parametrize("fault", ["finalize", "finalize:after"])
def test_an_adoption_pending_fault_in_finalize_is_completed_never_rolled_back(lifecycle_home, monkeypatch, fault):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    before = ops.prototype_snapshot(lh)
    calls = spy_rollbacks(monkeypatch)
    with pytest.raises(engine.OperationError):
        engine.install(empty_path(ops.make_env(lh, app_version="7.0", faults={fault: RuntimeError("boom")}),
                                  lh, legacy_node_ok=False))
    marker = marker_of(lh)
    assert marker["phase"] == "committed" and "rollback" not in calls
    assert collect(lh)["exit_code"] == 12
    result = engine.install(empty_path(ops.make_env(lh, app_version="7.0", now=ops.LATER), lh, legacy_node_ok=False))
    assert result.mode == "adoption-pending" and "rollback" not in calls
    assert load(lh.runtime / "install-manifest.json")["mode"] == "adoption-pending"
    assert not (lh.runtime / "operation.json").exists()
    after = ops.prototype_snapshot(lh)
    for key in before:
        if key.startswith(("plugin/", "settings/", "runtime/monitor.py", "runtime/dashboard.py")):
            assert after[key] == before[key], key


@pytest.mark.parametrize("rc,adopted", [(1, False), (0, True)])
def test_a_harvested_system_python_is_probed_only_when_the_clt_exist(lifecycle_home, rc, adopted):
    lh = lifecycle_home
    lc.make_prototype(lh, python="/usr/bin/python3", plugin_manifest=True)
    env = ops.make_env(lh, app_version="7.6")
    env.fake_ctx.which_table.clear()
    env.fake_ctx.probes[("xcode-select", "-p")] = (rc, "/Library/Developer/CommandLineTools\n" if rc == 0 else "", "")
    env.fake_ctx.probes[("/usr/bin/python3", "-E", "-s", "-c", prereqs.PY_PROBE)] = (0, "3.9.6\n", "")
    result = engine.install(env)
    probed = [c for c in env.fake_ctx.probe_calls if c[:1] == ("/usr/bin/python3",)]
    if adopted:
        assert result.mode == "active" and probed
        assert load(lh.runtime / "install-manifest.json")["binaries"]["python"]["path"] == "/usr/bin/python3"
    else:
        assert result.mode == "adoption-pending" and not probed


# ------------------------------------------------------------------ interrupted after the commit point

@pytest.mark.parametrize("fault", ["state_migration:after", "claude_settings:after"])
def test_an_interrupted_adoption_is_completed_as_an_adoption(lifecycle_home, fault):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    clean = engine.install(ops.make_env(lh))
    reference = load(lh.runtime / "install-manifest.json")
    # a second, identical machine interrupted mid-way
    import shutil
    for path in (lh.runtime, lh.plugins.parent, lh.state_root.parent, lh.home / ".claude", lh.codex_home):
        shutil.rmtree(str(path), ignore_errors=True)
    ops.make_adoption_fixture(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={fault: RuntimeError("boom")}))
    assert marker_of(lh)["adoption"] is True
    engine.install(ops.make_env(lh, now=ops.LATER))
    data = load(lh.runtime / "install-manifest.json")
    assert data["adopted_from"] == "prototype" and data["legacy_owned_events"] == reference["legacy_owned_events"]
    assert {e["rel"] for e in data["files"] if e["kind"] == "forwarder"} == {
        e["rel"] for e in reference["files"] if e["kind"] == "forwarder"}
    assert data["mode"] == "active" and collect(lh)["exit_code"] == 0
    ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=[legacy_command(lh)]))


def test_a_completing_adoption_with_core_now_unverifiable_stays_active(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("boom")}))
    predecessor = marker_of(lh)
    assert predecessor["draft"]["wired"] is True
    plugin_before = lc.tree_snapshot(lh.plugin_dir)
    settings_before = (lh.claude_settings.read_bytes(), codex_path(lh).read_bytes())
    env = empty_path(ops.make_env(lh, app_version="7.0", now=ops.LATER), lh, legacy_node_ok=False)
    env.fake_ctx.probes.clear()
    result = engine.install(env)
    data = load(lh.runtime / "install-manifest.json")
    assert data["adopted_from"] == "prototype" and data["mode"] == "active" and result.mode == "active"
    assert {h["runtime"] for h in data["hooks"]} == {"claude", "codex"}
    created = {(h["runtime"], h["event"]): h["created_event_key"] for h in data["hooks"]}
    drafted = {(h["runtime"], h["event"]): h["created_event_key"] for h in predecessor["draft"]["hooks"]}
    assert all(created[key] == value for key, value in drafted.items())
    assert data["binaries"]["python"] == predecessor["draft"]["binaries"]["python"]
    assert data["binaries"]["node"] == predecessor["draft"]["binaries"]["node"]
    assert lc.tree_snapshot(lh.plugin_dir) == plugin_before
    assert (lh.claude_settings.read_bytes(), codex_path(lh).read_bytes()) == settings_before
    assert {e["rel"] for e in data["files"] if e["kind"] == "forwarder"} == set(shims.FORWARDER_NAMES)
    report = collect(lh)
    assert report["exit_code"] == 11 and "PREREQ_CORE_MISSING" in codes(report)
    assert "MODE_ADOPTION_PENDING" not in codes(report)
    assert not (lh.runtime / "operation.json").exists()
    ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=[legacy_command(lh)]))


# ------------------------------------------------------------------ plugin dir that is not ours

def test_a_prototype_plugin_without_a_manifest_is_kept_aside_and_restored_on_rollback(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh, plugin_manifest=False)
    original = lc.tree_snapshot(lh.plugin_dir)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"plugin_swap:after": RuntimeError("boom")}))
    assert lc.tree_snapshot(lh.plugin_dir) == original
    assert not [n for n in os.listdir(str(lh.plugins)) if n.startswith(".quoin-plugin-")]
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    kept = [n for n in os.listdir(str(lh.plugins)) if n.startswith(".quoin-plugin-kept-")]
    assert len(kept) == 1 and lc.tree_snapshot(lh.plugins / kept[0]) == original
    assert any("warning" in line and kept[0] in line for line in result.lines)
    assert load(lh.plugin_dir / "manifest.json")["UUID"] == payload_build.PLUGIN_UUID


# ------------------------------------------------------------------ --runtimes

def test_adopting_only_claude_leaves_the_codex_prototype_hooks_reaching_the_monitor(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    codex_before = codex_path(lh).read_bytes()
    result = engine.install(ops.make_env(lh, runtimes=("claude",)))
    assert codex_path(lh).read_bytes() == codex_before
    data = load(lh.runtime / "install-manifest.json")
    assert {h["runtime"] for h in data["hooks"]} == {"claude"}
    assert data["legacy_owned_events"]["codex"]
    assert any("Codex was left on the legacy hooks" in line for line in result.lines)
    assert (lh.runtime / "monitor.py").exists() and data["adopted_from"] == "prototype"
    ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=[legacy_command(lh)]))


# ------------------------------------------------------------------ an active deck is never deactivated silently

def installed_without_node(lh):
    engine.install(ops.make_env(lh))
    return ops.snapshot_owned(lh)


def test_an_active_install_survives_a_failed_reverification_without_a_tty(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    plugin_before = lc.tree_snapshot(lh.plugin_dir)
    settings_before = (lh.claude_settings.read_bytes(), codex_path(lh).read_bytes())
    env = empty_path(ops.make_env(lh, app_version="7.0", now=ops.LATER, missing=("node",)), lh, legacy_node_ok=False)
    env.fake_ctx.which_table.clear()
    engine.upgrade(env)
    data = load(lh.runtime / "install-manifest.json")
    assert data["mode"] == "active" and "node" in data["mode_reasons"]
    assert lc.tree_snapshot(lh.plugin_dir) == plugin_before
    assert (lh.claude_settings.read_bytes(), codex_path(lh).read_bytes()) == settings_before
    report = collect(lh)
    assert report["exit_code"] == 11 and "PREREQ_CORE_MISSING" in codes(report)


@pytest.mark.parametrize("answer,deactivated", [("n", False), ("y", True)])
def test_a_tty_user_decides_whether_to_deactivate(lifecycle_home, answer, deactivated):
    lh = lifecycle_home
    lc.make_settings(lh.claude_settings, foreign=True, core=True)
    foreign_before = [hook for hook in (load(lh.claude_settings)["hooks"]["Stop"])]
    engine.install(ops.make_env(lh))
    env = ops.make_env(lh, app_version="7.0", missing=("node",), tty=True, answers=[answer], now=ops.LATER,
                       prereqs="none")
    result = engine.upgrade(env)
    assert any("Deactivate the deck" in p for p in env.fake_ctx.prompts)
    data = load(lh.runtime / "install-manifest.json")
    if deactivated:
        assert data["mode"] == "dormant" and not lh.plugin_dir.exists() and data["hooks"] == []
        assert hooks_merge.owned_commands(load(lh.claude_settings), "claude", runtime_names=NAMES) == []
        stop = load(lh.claude_settings)["hooks"]["Stop"]
        assert [g for g in stop if g in foreign_before] == foreign_before
    else:
        assert data["mode"] == "active" and lh.plugin_dir.is_dir() and data["hooks"]
