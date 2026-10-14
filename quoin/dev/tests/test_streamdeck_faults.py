"""Fault injection at every pipeline step: rollback before the commit point, roll-forward after it."""
import copy
import datetime
import json
import os
import shutil

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
from quoin.streamdeck import engine, hooks_merge, jsonfile, macos, manifest, migrations, payload_build, status
import quoin.streamdeck as sd

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
LATER2 = lc.NOW + datetime.timedelta(hours=2)
PRE_COMMIT_STEPS = ("stage_payload", "set_aside", "swap_current", "write_entry_points", "plugin_swap")
STEP_POINTS = [(step, suffix) for step in engine.STEPS for suffix in ("", ":after")]
COLUMNS = ("fresh", "repair", "upgrade")


def pre_commit(step, suffix):
    return step in PRE_COMMIT_STEPS or (step == "commit" and suffix == "")


def collect(lh, now=lc.NOW):
    report = status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN, now=now)
    return status.finalize(report)


def load(path):
    with open(str(path)) as handle:
        return json.load(handle)


def marker_of(lh):
    return load(lh.runtime / "operation.json")


def prepare(lh, column):
    if column == "repair":
        engine.install(ops.make_env(lh))
        return "install"
    if column == "upgrade":
        lc.make_installed(lh, component_version="0.9.0", version_dir_name="0.9.0")
        return "upgrade"
    return "install"


def run_op(op, env):
    return getattr(engine, op)(env)


def guard_backups(monkeypatch, lh):
    real = shutil.copy2

    def guarded(src, dst, *a, **kw):
        if os.fspath(src).startswith(str(lh.runtime / "backups")):
            raise AssertionError("settings must never be restored from a backup")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", guarded)


def no_leftovers(lh):
    plugins = os.listdir(str(lh.plugins)) if lh.plugins.exists() else []
    assert not [n for n in plugins if n.startswith(".quoin-plugin-old-")]
    staging = lh.runtime / "staging"
    assert not (staging.exists() and [n for n in os.listdir(str(staging)) if n.startswith("rollback-")])


def spy_rollbacks(monkeypatch):
    calls = []
    real_rollback, real_sweep = engine.rollback, migrations.reverse_sweep
    monkeypatch.setattr(engine, "rollback", lambda *a, **k: (calls.append("rollback"), real_rollback(*a, **k))[1])
    monkeypatch.setattr(migrations, "reverse_sweep", lambda *a, **k: (calls.append("sweep"), real_sweep(*a, **k))[1])
    return calls


# ------------------------------------------------------------------ the matrix

@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("step,suffix", STEP_POINTS, ids=["%s%s" % sp for sp in STEP_POINTS])
def test_fault_matrix(lifecycle_home, monkeypatch, column, step, suffix):
    lh = lifecycle_home
    op = prepare(lh, column)
    pre = ops.snapshot_owned(lh)
    pre_manifest = load(lh.runtime / "install-manifest.json") if (lh.runtime / "install-manifest.json").exists() else None
    guard_backups(monkeypatch, lh)
    calls = spy_rollbacks(monkeypatch)
    env = ops.make_env(lh, faults={step + suffix: RuntimeError("boom")}, now=ops.LATER)
    with pytest.raises(engine.OperationError) as exc:
        run_op(op, env)
    assert exc.value.code == 1
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    marker_path = lh.runtime / "operation.json"
    if pre_commit(step, suffix):
        assert not marker_path.exists()
        assert ops.snapshot_owned(lh) == pre
        if column == "fresh":
            assert collect(lh)["exit_code"] == 10
            assert not lh.claude_settings.exists() and not (lh.codex_home / "hooks.json").exists()
        assert "rollback" in calls
        return
    marker = marker_of(lh)
    assert marker["phase"] == "committed" and marker["op"] == op
    assert (step + suffix == "commit:after") or step in ("commit", "state_migration", "claude_settings",
                                                        "codex_settings", "finalize")
    assert collect(lh)["exit_code"] == 12
    assert "rollback" not in calls
    calls.clear()
    result = run_op(op, ops.make_env(lh, now=LATER2))
    assert not marker_path.exists()
    report = collect(lh)
    assert report["exit_code"] == 0 and report["problems"] == [], report["problems"]
    data = load(lh.runtime / "install-manifest.json")
    assert data["adopted_from"] is None and data["legacy_owned_events"] == {}
    for name in shims_names():
        assert not (lh.runtime / name).exists()
    assert "restart these agents" not in "\n".join(result.lines) and "prototype" not in "\n".join(result.lines)
    assert calls == []
    no_leftovers(lh)
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    again = run_op(op, ops.make_env(lh, now=LATER2))
    assert again.changed is False


def shims_names():
    from quoin.streamdeck import shims
    return shims.FORWARDER_NAMES


# ------------------------------------------------------------------ specific recovery cases

def test_hard_crash_at_plugin_swap_is_rolled_back_by_the_next_run(lifecycle_home):
    lh = lifecycle_home
    pre = ops.snapshot_owned(lh)
    env = ops.make_env(lh, faults={"plugin_swap": SystemExit(9)})
    with pytest.raises(SystemExit):
        engine.install(env)
    assert marker_of(lh)["phase"] == "staging"
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    env = ops.make_env(lh, now=ops.LATER)
    result = engine.install(env)
    assert "rolled back an interrupted operation" in result.lines
    assert collect(lh)["exit_code"] == 0 and not (lh.runtime / "operation.json").exists()
    assert pre != ops.snapshot_owned(lh)


def test_real_interrupted_commit_then_unreadable_marker_rolls_forward(lifecycle_home):
    lh = lifecycle_home
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("boom")}))
    (lh.runtime / "operation.json").write_text("{garbage")
    assert collect(lh)["exit_code"] == 12
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    assert any("completing an interrupted operation (created event keys unknown)" in l for l in result.lines)
    assert collect(lh)["exit_code"] == 0
    assert load(lh.runtime / "install-manifest.json")["adopted_from"] is None


def test_cross_volume_plugin_swap_uses_the_copy_path_and_an_old_aside(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    lc.make_installed(lh, component_version="0.9.0", version_dir_name="0.9.0")
    renames = []
    real = os.rename
    state = {"failed": False}

    def spy(src, dst, *a, **kw):
        renames.append((os.fspath(src), os.fspath(dst)))
        if os.fspath(dst) == str(lh.plugin_dir) and "/staging/" in os.fspath(src) and not state["failed"]:
            state["failed"] = True
            raise OSError(18, "Invalid cross-device link")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", spy)
    result = engine.upgrade(ops.make_env(lh))
    assert state["failed"]
    asides = [dst for src, dst in renames if src == str(lh.plugin_dir)]
    assert len(asides) == 1 and os.path.dirname(asides[0]) == str(lh.plugins)
    assert os.path.basename(asides[0]).startswith(".quoin-plugin-old-") and not os.path.basename(asides[0]).endswith(".sdPlugin")
    assert (lh.plugin_dir / "plugin-config.json").exists() and collect(lh)["exit_code"] == 0
    assert not [n for n in os.listdir(str(lh.plugins)) if n.startswith(".quoin-plugin-")]


def test_a_foreign_plugin_dir_is_kept_aside_and_restored_on_rollback(lifecycle_home):
    lh = lifecycle_home
    lc.make_installed(lh)
    data = json.loads((lh.plugin_dir / "manifest.json").read_text())
    data["UUID"] = "com.example.foreign"
    (lh.plugin_dir / "manifest.json").write_text(json.dumps(data))
    original = lc.tree_snapshot(lh.plugin_dir)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"plugin_swap:after": RuntimeError("boom")}))
    assert lc.tree_snapshot(lh.plugin_dir) == original
    assert not [n for n in os.listdir(str(lh.plugins)) if n.startswith(".quoin-plugin-")]
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    kept = [n for n in os.listdir(str(lh.plugins)) if n.startswith(".quoin-plugin-kept-")]
    assert len(kept) == 1 and any(kept[0] in line for line in result.lines)
    assert lc.tree_snapshot(lh.plugins / kept[0]) == original
    assert json.loads((lh.plugin_dir / "manifest.json").read_text())["UUID"] == payload_build.PLUGIN_UUID


# ------------------------------------------------------------------ created-event-key survival

CLAUDE_MISSING = ("PermissionRequest", "Elicitation")


def foreign_fixtures(lh):
    lc.make_settings(lh.claude_settings, foreign=True, core=False)
    lc.write_json(lh.codex_home / "hooks.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/n"}]}]}})


def pre_install_events(lh):
    out = {}
    for kind, path in (("claude", lh.claude_settings), ("codex", lh.codex_home / "hooks.json")):
        out[kind] = set(load(path).get("hooks", {}))
    return out


def expected_created(before):
    return {kind: set(hooks_merge.EVENTS[kind]) - before[kind] for kind in before}


def draft_flags(marker):
    records = (marker["draft"]["hooks"] or []) + (marker.get("carried_hooks") or [])
    flags = {}
    for r in records:
        key = (r["runtime"], r["event"])
        flags[key] = flags.get(key, False) or r["created_event_key"]
    return flags


@pytest.mark.parametrize("fault", ["claude_settings:after", "codex_settings:after"])
def test_created_event_keys_survive_an_interruption(lifecycle_home, fault):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = pre_install_events(lh)
    created = expected_created(before)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={fault: RuntimeError("boom")}))
    flags = draft_flags(marker_of(lh))
    kinds = ["claude"] if fault.startswith("claude") else ["claude", "codex"]
    for kind in kinds:
        for event in hooks_merge.EVENTS[kind]:
            assert flags[(kind, event)] is (event in created[kind]), (kind, event)
    engine.install(ops.make_env(lh, now=ops.LATER))
    data = load(lh.runtime / "install-manifest.json")
    for record in data["hooks"]:
        assert record["created_event_key"] is (record["event"] in created[record["runtime"]]), record
    assert data["adopted_from"] is None


SECOND_FAULTS = ["finalize", "finalize:after"] + [s + x for s in PRE_COMMIT_STEPS for x in ("", ":after")]


@pytest.mark.parametrize("second", SECOND_FAULTS)
def test_created_keys_survive_a_second_interruption_in_the_completing_run(lifecycle_home, monkeypatch, second):
    lh = lifecycle_home
    foreign_fixtures(lh)
    created = expected_created(pre_install_events(lh))
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("one")}))
    calls = spy_rollbacks(monkeypatch)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={second: RuntimeError("two")}, now=ops.LATER))
    assert "rollback" not in calls
    marker = marker_of(lh)
    assert marker["op"] == "install" and marker["phase"] == "committed"
    flags = draft_flags(marker)
    for kind in ("claude", "codex"):
        for event in hooks_merge.EVENTS[kind]:
            assert flags[(kind, event)] is (event in created[kind]), (kind, event, second)
    assert collect(lh)["exit_code"] == 12
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    engine.install(ops.make_env(lh, now=LATER2))
    data = load(lh.runtime / "install-manifest.json")
    for record in data["hooks"]:
        assert record["created_event_key"] is (record["event"] in created[record["runtime"]]), record
    assert data["adopted_from"] is None and collect(lh)["exit_code"] == 0
    assert "rollback" not in calls


def test_the_completing_run_writes_a_committed_marker_carrying_the_predecessor(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    foreign_fixtures(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}))
    predecessor = marker_of(lh)
    written = []
    real = engine.open_marker
    monkeypatch.setattr(engine, "open_marker", lambda *a, **k: (written.append(real(*a, **k)), written[-1])[1])
    engine.install(ops.make_env(lh, now=ops.LATER))
    first = written[0]
    assert first["phase"] == "committed"
    assert first["carried_hooks"] and {(r["runtime"], r["event"]) for r in first["carried_hooks"]} == {
        (r["runtime"], r["event"]) for r in predecessor["draft"]["hooks"]}
    assert first["stale"]["rollback_dirs"] == [predecessor["rollback_dir"]]
    for key in ("binaries", "mode", "wired", "legacy_owned_events"):
        assert first["draft"][key] == predecessor["draft"][key], key


def test_a_completing_run_with_a_core_prerequisite_gone_stays_active(lifecycle_home):
    lh = lifecycle_home
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("boom")}))
    predecessor = marker_of(lh)
    plugin_before = lc.tree_snapshot(lh.plugin_dir)
    claude_before = lh.claude_settings.read_bytes()
    result = engine.install(ops.make_env(lh, missing=("node",), app_version="7.0", now=ops.LATER))
    data = load(lh.runtime / "install-manifest.json")
    assert data["mode"] == "active" and result.mode == "active"
    assert lc.tree_snapshot(lh.plugin_dir) == plugin_before and lh.claude_settings.read_bytes() == claude_before
    assert data["binaries"]["node"] == predecessor["draft"]["binaries"]["node"]
    assert data["prereqs"]["node"]["state"] == "missing" and "node" in data["mode_reasons"]
    assert {(h["runtime"], h["event"]): h["created_event_key"] for h in data["hooks"] if h["runtime"] == "claude"} == {
        (r["runtime"], r["event"]): r["created_event_key"] for r in predecessor["draft"]["hooks"] if r["runtime"] == "claude"}
    report = collect(lh)
    assert report["exit_code"] == 11 and {p["code"] for p in report["problems"]} == {"PREREQ_CORE_MISSING"}
    assert not (lh.runtime / "operation.json").exists()
    ops.assert_hooks_inert(ops.run_hook_commands(lh))


def test_two_interruptions_keep_a_wired_deck_active_when_a_prerequisite_returns_and_leaves_again(lifecycle_home):
    lh = lifecycle_home
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, missing=("node",), app_version="7.0", faults={"claude_settings:after": RuntimeError("a")}))
    assert marker_of(lh)["draft"]["mode"] == "dormant"
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, now=ops.LATER, faults={"codex_settings:after": RuntimeError("b")}))
    second = marker_of(lh)
    assert second["draft"]["mode"] == "active" and second["draft"]["binaries"]["node"]["mode"] == "external"
    result = engine.install(ops.make_env(lh, missing=("node",), app_version="7.0", now=LATER2))
    data = load(lh.runtime / "install-manifest.json")
    assert data["mode"] == "active" and lh.plugin_dir.is_dir() and data["binaries"]["node"] == second["draft"]["binaries"]["node"]
    assert owned_hooks(lh) > 0


def owned_hooks(lh):
    return len(hooks_merge.owned_commands(load(lh.claude_settings), "claude", runtime_names=("QuoinStreamDeck",)))


def test_a_completing_run_prefers_the_binaries_the_interrupted_run_verified(lifecycle_home):
    lh = lifecycle_home
    lc.make_installed(lh, component_version="0.9.0", version_dir_name="0.9.0")
    stored = load(lh.runtime / "install-manifest.json")
    stored["binaries"]["node"]["path"] = str(lh.root / "toolbin" / "older-node")
    manifest.write(str(lh.runtime / "install-manifest.json"), stored)
    with pytest.raises(engine.OperationError):
        engine.upgrade(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("boom")}))
    fresh_node = load(lh.runtime / "operation.json")["draft"]["binaries"]["node"]
    old_node = load(lh.runtime / "install-manifest.json")["binaries"]["node"]
    assert fresh_node["path"] != old_node["path"]
    env = ops.make_env(lh, now=ops.LATER)
    run = engine._Run(env, "upgrade")
    state = engine.read_state(env, "upgrade")
    carry = engine.carry_from_marker(state.marker, state.manifest["hooks"])
    recorded = run._recorded_binaries(state, carry)
    assert recorded["node"] == fresh_node
    result = engine.upgrade(env)
    assert result.manifest["binaries"]["node"]["path"] == fresh_node["path"]


def test_deactivation_in_a_completing_chain_is_honoured(lifecycle_home):
    lh = lifecycle_home
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"claude_settings:after": RuntimeError("a")}))
    # the user explicitly deactivates while completing; that run is interrupted too
    env = ops.make_env(lh, missing=("node",), app_version="7.0", tty=True, answers=["n", "y"], now=ops.LATER,
                       faults={"finalize": RuntimeError("b")})
    with pytest.raises(engine.OperationError):
        engine.install(env)
    marker = marker_of(lh)
    assert marker["draft"]["mode"] == "dormant" and marker["draft"]["deactivated"] is True
    # the node comes back, but the explicit choice survives the chain until a fresh install
    result = engine.install(ops.make_env(lh, now=LATER2))
    assert result.mode == "dormant" and not lh.plugin_dir.exists()
    assert owned_hooks(lh) == 0
    again = engine.install(ops.make_env(lh, now=LATER2 + datetime.timedelta(hours=1)))
    assert again.mode == "active" and lh.plugin_dir.is_dir()


def test_created_flags_reach_the_marker_before_the_settings_file_is_written(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    foreign_fixtures(lh)
    created = expected_created(pre_install_events(lh))
    real = jsonfile.update

    def crash_right_after_the_write(path, mutate, **kwargs):
        result = real(path, mutate, **kwargs)
        if str(path) == str(lh.claude_settings):
            raise RuntimeError("crash right after the settings write")
        return result

    monkeypatch.setattr(jsonfile, "update", crash_right_after_the_write)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh))
    flags = draft_flags(marker_of(lh))
    for event in hooks_merge.EVENTS["claude"]:
        assert flags[("claude", event)] is (event in created["claude"]), event
