"""Uninstall from every starting mode: inert stubs, foreign entries kept, interruptions converge."""
import fcntl
import json
import os
import shutil

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
from quoin.streamdeck import engine, hooks_merge, macos, manifest, payload_build, prereqs, shims, status
import quoin.streamdeck as sd

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
LINUX = macos.PlatformInfo("linux", "", "x86_64", False)
NAMES = ("QuoinStreamDeck",)
SEAMS = ("uninstall:claude_settings", "uninstall:codex_settings", "uninstall:tombstones",
         "uninstall:remove_files", "uninstall:manifest")


def collect(lh, now=lc.NOW):
    report = status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN, now=now)
    return status.finalize(report)


def load(path):
    with open(str(path)) as handle:
        return json.load(handle)


def codex_path(lh):
    return lh.codex_home / "hooks.json"


def marker_of(lh):
    return load(lh.runtime / "operation.json")


def foreign_fixtures(lh):
    lc.make_settings(lh.claude_settings, foreign=True, core=False)
    lc.write_json(codex_path(lh), {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/n"}]}]}})


def settings_now(lh):
    return load(lh.claude_settings), load(codex_path(lh))


def spy(monkeypatch):
    calls = []
    real_rollback = engine.rollback
    monkeypatch.setattr(engine, "rollback", lambda *a, **k: (calls.append("rollback"), real_rollback(*a, **k))[1])
    return calls


def guard_backups(monkeypatch, lh):
    real = shutil.copy2

    def guarded(src, dst, *a, **kw):
        if os.fspath(src).startswith(str(lh.runtime / "backups")):
            raise AssertionError("settings must never be restored from a backup")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "copy2", guarded)


def empty_path(env, lh, *, node_ok=False):
    env.fake_ctx.which_table.clear()
    if node_ok:
        node = str(lh.root / "legacy" / "node")
        env.fake_ctx.probes[(node, "-e", prereqs.WEBSOCKET_PROBE)] = (0, "", "")
        env.fake_ctx.probes[(node, "--version")] = (0, "v24.1.0\n", "")
    return env


def legacy_command(lh):
    return lc.legacy_command(lh.runtime).replace("/usr/bin/python3", lc.fixture_python(lh))


def foreign_only(data):
    found = [(event, [h for g in groups for h in g["hooks"]
                      if hooks_merge.classify(h.get("command"), runtime_names=NAMES) is None])
             for event, groups in sorted((data.get("hooks") or {}).items())]
    return [(event, hooks) for event, hooks in found if hooks]


def legacy_extra(lh):
    return [legacy_command(lh)] if (lh.runtime / "monitor.py").exists() else []


# ------------------------------------------------------------------ from an active install

def test_uninstall_after_install_and_upgrade_restores_the_settings(lifecycle_home):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    engine.install(ops.make_env(lh))
    engine.upgrade(ops.make_env(lh, now=ops.LATER))
    result = engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before
    assert "hooks" in settings_now(lh)[1]
    for gone in ("current", "versions", "install-manifest.json", "profile", "staging", "operation.json",
                 os.path.join("bin", "quoin-deck-terminal")):
        assert not os.path.lexists(str(lh.runtime / gone)), gone
    assert not lh.plugin_dir.exists()
    assert manifest.is_tombstone(str(lh.runtime / "bin" / "quoin-deck-hook"))
    assert not (lh.runtime / "monitor.py").exists()
    assert manifest.read_uninstalled(lh.resolve()) == [str(lh.runtime / "bin" / "quoin-deck-hook")]
    report = collect(lh)
    assert report["exit_code"] == 10 and "inert hook stubs" in status.render_text(report)
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    snapshot = lc.tree_snapshot(lh.root)
    again = engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert any(line.startswith("nothing to remove") and "inert hook stub" in line for line in again.lines)
    assert lc.tree_snapshot(lh.root) == snapshot


def test_unrelated_user_edits_survive_upgrade_and_uninstall_without_a_backup_restore(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    foreign_fixtures(lh)
    engine.install(ops.make_env(lh))
    guard_backups(monkeypatch, lh)
    settings = load(lh.claude_settings)
    settings["theme"] = "light"
    settings["hooks"]["Notification"].append({"hooks": [{"type": "command", "command": "/usr/local/bin/late"}]})
    lc.write_json(lh.claude_settings, settings)
    engine.upgrade(ops.make_env(lh, now=ops.LATER))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    after = load(lh.claude_settings)
    assert after["theme"] == "light"
    assert after["hooks"]["Notification"] == [{"hooks": [{"type": "command", "command": "/usr/local/bin/late"}]}]


def test_uninstall_on_a_machine_without_the_component_creates_nothing(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh)
    before = lc.tree_snapshot(lh.root)
    result = engine.uninstall(env)
    assert result.exit_code == 0 and "nothing to remove" in result.lines[0]
    assert lc.tree_snapshot(lh.root) == before and not lh.runtime.exists()


def test_dry_run_changes_nothing_and_lists_the_removals(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    before = ops.snapshot_owned(lh)
    result = engine.uninstall(ops.make_env(lh, dry_run=True))
    assert ops.snapshot_owned(lh) == before
    text = "\n".join(result.lines)
    assert "would remove the claude hook entries" in text and str(lh.plugin_dir) in text


def test_uninstall_on_another_platform_explains_and_changes_nothing(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, platform=LINUX)
    before = lc.tree_snapshot(lh.root)
    result = engine.uninstall(env)
    assert result.exit_code == 0 and any("macOS only" in line for line in env.lines)
    assert lc.tree_snapshot(lh.root) == before


# ------------------------------------------------------------------ other starting modes

def prototype_pending(lh):
    """An adoption-pending install over the prototype fixture."""
    ops.make_adoption_fixture(lh)
    result = engine.install(empty_path(ops.make_env(lh, app_version="7.0"), lh))
    assert result.mode == "adoption-pending"


@pytest.mark.parametrize("pending", [False, True])
def test_uninstall_a_prototype_or_an_adoption_pending_install(lifecycle_home, pending):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    before = {k: load(p) for k, p in (("claude", lh.claude_settings), ("codex", codex_path(lh)))}
    buttons = lc.tree_snapshot(lh.runtime / "Buttons")
    backups = lc.tree_snapshot(lh.runtime / "backups")
    if pending:
        engine.install(empty_path(ops.make_env(lh, app_version="7.0"), lh))
        assert (lh.runtime / "versions").is_dir()
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    for key, path in (("claude", lh.claude_settings), ("codex", codex_path(lh))):
        after = load(path)
        assert foreign_only(after) == foreign_only(before[key])
        assert hooks_merge.owned_commands(after, key, runtime_names=NAMES) == []
        assert after["theme" if key == "claude" else "hooks"] is not None
    assert not lh.plugin_dir.exists() and not (lh.runtime / "versions").exists()
    assert manifest.is_tombstone(str(lh.runtime / "monitor.py"))
    for name in ("dashboard.py", "controller.py", "terminal.zsh"):
        assert not (lh.runtime / name).exists()
    assert lc.tree_snapshot(lh.runtime / "Buttons") == buttons
    after_backups = lc.tree_snapshot(lh.runtime / "backups")
    for rel, value in backups.items():
        assert after_backups[rel] == value
    assert all(rel.split(os.sep)[0].startswith("quoin-") for rel in after_backups if rel not in backups)
    assert any(rel not in backups for rel in after_backups)
    ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=legacy_extra(lh)))


def test_a_prototype_plugin_without_a_manifest_is_kept_with_a_warning(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh, plugin_manifest=False)
    plugin_before = lc.tree_snapshot(lh.plugin_dir)
    result = engine.uninstall(ops.make_env(lh))
    assert lc.tree_snapshot(lh.plugin_dir) == plugin_before
    assert any("warning" in line and str(lh.plugin_dir) in line for line in result.lines)
    report = collect(lh)
    assert report["exit_code"] == 11 and {p["code"] for p in report["problems"]} == {"PROTOTYPE_UNADOPTED"}
    snapshot = lc.tree_snapshot(lh.root)
    again = engine.uninstall(ops.make_env(lh))
    assert again.exit_code == 0 and any(line.startswith("nothing to remove") for line in again.lines)
    assert any("warning" in line and str(lh.plugin_dir) in line for line in again.lines)
    assert lc.tree_snapshot(lh.root) == snapshot


def test_a_foreign_uuid_plugin_dir_and_planted_files_are_not_deleted(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    outside = lh.root / "outside.txt"
    outside.write_text("keep me")
    (lh.runtime / "link").symlink_to(outside)
    planted = lh.plugins / "com.example.other.sdPlugin"
    planted.mkdir()
    (planted / "x").write_text("x")
    data = load(lh.runtime / "install-manifest.json")
    data["files"].append({"rel": "link", "root": "runtime", "sha256": "0" * 64, "mode": 420, "kind": "code"})
    data["files"].sort(key=lambda e: (e["root"], e["rel"]))
    manifest.write(str(lh.runtime / "install-manifest.json"), data)
    plugin = load(lh.plugin_dir / "manifest.json")
    plugin["UUID"] = "com.example.foreign"
    (lh.plugin_dir / "manifest.json").write_text(json.dumps(plugin))
    foreign_dir = lc.tree_snapshot(lh.plugin_dir)
    result = engine.uninstall(ops.make_env(lh))
    assert outside.read_text() == "keep me" and (planted / "x").read_text() == "x"
    assert lc.tree_snapshot(lh.plugin_dir) == foreign_dir
    assert any("warning" in line and str(lh.plugin_dir) in line for line in result.lines)


# ------------------------------------------------------------------ --runtimes

def test_runtimes_codex_on_an_active_install_keeps_the_rest(lifecycle_home):
    lh = lifecycle_home
    foreign_fixtures(lh)
    engine.install(ops.make_env(lh))
    plugin = lc.tree_snapshot(lh.plugin_dir)
    claude_before = lh.claude_settings.read_bytes()
    result = engine.uninstall(ops.make_env(lh, runtimes=("codex",), now=ops.LATER))
    assert hooks_merge.owned_commands(load(codex_path(lh)), "codex", runtime_names=NAMES) == []
    assert "PermissionRequest" not in load(codex_path(lh))["hooks"]
    assert lh.claude_settings.read_bytes() == claude_before and lc.tree_snapshot(lh.plugin_dir) == plugin
    data = load(lh.runtime / "install-manifest.json")
    assert manifest.validate(data) == [] and {h["runtime"] for h in data["hooks"]} == {"claude"}
    assert (lh.runtime / "current").is_symlink() and not (lh.runtime / "operation.json").exists()
    assert collect(lh, now=ops.LATER)["exit_code"] == 0


def test_a_malformed_claude_file_blocks_a_full_uninstall_but_not_runtimes_codex(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    lh.claude_settings.write_text("{broken")
    broken = lh.claude_settings.read_bytes()
    with pytest.raises(engine.OperationError) as exc:
        engine.uninstall(ops.make_env(lh))
    assert str(lh.claude_settings) in str(exc.value)
    engine.uninstall(ops.make_env(lh, runtimes=("codex",)))
    assert lh.claude_settings.read_bytes() == broken
    assert hooks_merge.owned_commands(load(codex_path(lh)), "codex", runtime_names=NAMES) == []


def test_runtimes_codex_from_adoption_pending_and_from_a_prototype(lifecycle_home):
    lh = lifecycle_home
    prototype_pending(lh)
    claude_before = lh.claude_settings.read_bytes()
    versions = lc.tree_snapshot(lh.runtime / "versions")
    engine.uninstall(ops.make_env(lh, runtimes=("codex",), now=ops.LATER))
    assert hooks_merge.owned_commands(load(codex_path(lh)), "codex", runtime_names=NAMES) == []
    data = load(lh.runtime / "install-manifest.json")
    assert "codex" not in data["legacy_owned_events"] and data["mode"] == "adoption-pending"
    assert manifest.validate(data) == []
    assert lh.claude_settings.read_bytes() == claude_before and lc.tree_snapshot(lh.runtime / "versions") == versions


def test_runtimes_codex_from_an_unadopted_prototype(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    before = ops.prototype_snapshot(lh)
    claude_before = lh.claude_settings.read_bytes()
    result = engine.uninstall(ops.make_env(lh, runtimes=("codex",)))
    after = ops.prototype_snapshot(lh)
    assert hooks_merge.owned_commands(load(codex_path(lh)), "codex", runtime_names=NAMES) == []
    assert lh.claude_settings.read_bytes() == claude_before
    for key, value in before.items():
        if not key.startswith("settings/codex") and not key.startswith("runtime/backups"):
            assert after.get(key) == value, key
    assert any("prototype stays in place" in line for line in result.lines)


# ------------------------------------------------------------------ interrupted install, then uninstall

@pytest.mark.parametrize("fault", ["claude_settings:after", "codex_settings:after", "finalize", "state_migration"])
def test_uninstall_after_an_interrupted_install_restores_the_settings(lifecycle_home, fault):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={fault: RuntimeError("boom")}))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before
    assert collect(lh)["exit_code"] == 10
    ops.assert_hooks_inert(ops.run_hook_commands(lh))


def test_uninstall_after_two_interrupted_installs_uses_the_carried_flags(lifecycle_home):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("one")}))
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"finalize": RuntimeError("two")}, now=ops.LATER))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before and collect(lh)["exit_code"] == 10


def test_uninstall_after_a_fault_past_the_commit_point_leaves_the_hooks_inert(lifecycle_home):
    lh = lifecycle_home
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"plugin_swap:after": RuntimeError("boom")}))
    engine.uninstall(ops.make_env(lh))
    assert collect(lh)["exit_code"] == 10
    ops.assert_hooks_inert(ops.run_hook_commands(lh))


# ------------------------------------------------------------------ interrupted uninstall

@pytest.mark.parametrize("seam", SEAMS)
def test_an_interrupted_uninstall_converges_when_rerun(lifecycle_home, monkeypatch, seam):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    engine.install(ops.make_env(lh))
    calls = spy(monkeypatch)
    with pytest.raises(engine.OperationError):
        engine.uninstall(ops.make_env(lh, faults={seam + ":after": RuntimeError("boom")}))
    marker = marker_of(lh)
    assert marker["op"] == "uninstall" and marker["phase"] == "committed" and marker["runtimes"] is None
    assert collect(lh)["exit_code"] == 12
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before and collect(lh)["exit_code"] == 10
    assert not os.path.lexists(str(lh.runtime / "current")) and "rollback" not in calls
    assert not (lh.runtime / "operation.json").exists()


@pytest.mark.parametrize("seam", SEAMS)
def test_install_after_an_interrupted_uninstall_keeps_the_created_flags(lifecycle_home, monkeypatch, seam):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    created = {k: set(hooks_merge.EVENTS[k]) - set(before[i].get("hooks", {})) for i, k in enumerate(("claude", "codex"))}
    engine.install(ops.make_env(lh))
    with pytest.raises(engine.OperationError):
        engine.uninstall(ops.make_env(lh, faults={seam + ":after": RuntimeError("boom")}))
    marker = marker_of(lh)
    flagged = {(r["runtime"], r["event"]) for r in marker["draft"]["hooks"] if r["created_event_key"]}
    assert flagged == {(k, e) for k in created for e in created[k]}
    seen = []
    real = engine._Run._recover

    def recording(self, state):
        carry = real(self, state)
        seen.append((state, carry))
        return carry

    monkeypatch.setattr(engine._Run, "_recover", recording)
    calls = spy(monkeypatch)
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    state, carry = seen[0]
    if seam == "uninstall:manifest":
        assert state.manifest is None
    carried = {(r["runtime"], r["event"]) for r in carry.prev_hooks if r["created_event_key"]}
    assert carried == flagged
    assert result.mode == "active" and collect(lh, now=ops.LATER)["exit_code"] == 0 and "rollback" not in calls
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before


def test_an_install_fault_over_an_interrupted_uninstall_is_never_rolled_back(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    engine.install(ops.make_env(lh))
    with pytest.raises(engine.OperationError):
        engine.uninstall(ops.make_env(lh, faults={"uninstall:remove_files:after": RuntimeError("boom")}))
    uninstall_flags = {(r["runtime"], r["event"]): r["created_event_key"] for r in marker_of(lh)["draft"]["hooks"]}
    calls = spy(monkeypatch)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"swap_current:after": RuntimeError("two")}, now=ops.LATER))
    marker = marker_of(lh)
    assert marker["op"] == "install" and marker["phase"] == "committed" and "rollback" not in calls
    carried = {(r["runtime"], r["event"]): r["created_event_key"] for r in marker["carried_hooks"]}
    assert all(carried[key] == value for key, value in uninstall_flags.items())
    report = collect(lh)
    assert report["exit_code"] == 12 and "install" in [p["detail"] for p in report["problems"]][0]
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before


def test_upgrade_is_refused_while_an_uninstall_is_pending(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    with pytest.raises(engine.OperationError):
        engine.uninstall(ops.make_env(lh, faults={"uninstall:manifest:after": RuntimeError("boom")}))
    before = lc.tree_snapshot(lh.root)
    with pytest.raises(engine.OperationError) as exc:
        engine.upgrade(ops.make_env(lh))
    assert "interrupted uninstall is pending" in str(exc.value) and exc.value.code == 1
    assert lc.tree_snapshot(lh.root) == before
    engine.install(ops.make_env(lh, now=ops.LATER))
    assert collect(lh, now=ops.LATER)["exit_code"] == 0


def test_a_partial_uninstall_names_itself_in_the_status_and_completes(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    with pytest.raises(engine.OperationError):
        engine.uninstall(ops.make_env(lh, runtimes=("codex",), faults={"uninstall:codex_settings:after": RuntimeError("x")}))
    assert marker_of(lh)["runtimes"] == ["codex"]
    report = collect(lh)
    assert report["exit_code"] == 12 and "quoin streamdeck uninstall --runtimes codex" in report["problems"][0]["detail"]
    result = engine.uninstall(ops.make_env(lh, runtimes=("codex",), now=ops.LATER))
    assert "completing an interrupted operation" in result.lines
    assert not (lh.runtime / "operation.json").exists()
    assert hooks_merge.owned_commands(load(lh.claude_settings), "claude", runtime_names=NAMES)
    assert (lh.runtime / "current").is_symlink() and collect(lh, now=ops.LATER)["exit_code"] == 0


# ------------------------------------------------------------------ partial uninstall over a broader marker

def assert_refused_and_unchanged(lh, runtimes, text):
    before = lc.tree_snapshot(lh.root)
    with pytest.raises(engine.OperationError) as exc:
        engine.uninstall(ops.make_env(lh, runtimes=runtimes))
    assert text in str(exc.value) and exc.value.code == 1
    assert lc.tree_snapshot(lh.root) == before


def test_a_partial_uninstall_refuses_over_a_committed_install_marker(lifecycle_home):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("boom")}))
    created = {(r["runtime"], r["event"]) for r in marker_of(lh)["draft"]["hooks"] if r["created_event_key"]}
    assert_refused_and_unchanged(lh, ("codex",), "an interrupted install is pending; run `quoin streamdeck install` "
                                                  "to complete it")
    assert collect(lh)["exit_code"] == 12
    assert created == {(r["runtime"], r["event"]) for r in marker_of(lh)["draft"]["hooks"] if r["created_event_key"]}
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before and collect(lh)["exit_code"] == 10


def test_a_partial_uninstall_refuses_over_a_full_uninstall_marker(lifecycle_home):
    lh = lifecycle_home
    foreign_fixtures(lh)
    before = settings_now(lh)
    engine.install(ops.make_env(lh))
    with pytest.raises(engine.OperationError):
        engine.uninstall(ops.make_env(lh, faults={"uninstall:tombstones:after": RuntimeError("boom")}))
    assert_refused_and_unchanged(lh, ("codex",), "an interrupted uninstall is pending; run `quoin streamdeck "
                                                  "uninstall` to complete it")
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    assert settings_now(lh) == before and collect(lh)["exit_code"] == 10


def test_a_partial_uninstall_completes_only_a_marker_it_covers(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    with pytest.raises(engine.OperationError):
        engine.uninstall(ops.make_env(lh, runtimes=("claude",), faults={"uninstall:claude_settings:after": RuntimeError("x")}))
    assert_refused_and_unchanged(lh, ("codex",), "quoin streamdeck uninstall --runtimes claude")
    engine.uninstall(ops.make_env(lh, runtimes=("claude", "codex"), now=ops.LATER))
    assert not (lh.runtime / "operation.json").exists()
    assert hooks_merge.owned_commands(load(lh.claude_settings), "claude", runtime_names=NAMES) == []
    assert hooks_merge.owned_commands(load(codex_path(lh)), "codex", runtime_names=NAMES) == []


def test_the_coverage_check_is_repeated_under_the_lock(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    foreign_fixtures(lh)
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("boom")}))
    marker_bytes = (lh.runtime / "operation.json").read_bytes()
    (lh.runtime / "operation.json").unlink()   # a clean tree at preflight time
    real_lock = engine._Run._lock

    def swapping(self, paths):
        fd = real_lock(self, paths)
        (lh.runtime / "operation.json").write_bytes(marker_bytes)   # a concurrent install interrupted meanwhile
        return fd

    monkeypatch.setattr(engine._Run, "_lock", swapping)
    # a settings entry keeps the preflight from concluding there is nothing to do
    with pytest.raises(engine.OperationError) as exc:
        engine.uninstall(ops.make_env(lh, runtimes=("codex",)))
    assert "an interrupted install is pending" in str(exc.value)
    assert (lh.runtime / "operation.json").read_bytes() == marker_bytes
    fd = os.open(str(lh.runtime / ".install.lock"), os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the lock was released with the refusal
    finally:
        os.close(fd)


# ------------------------------------------------------------------ second runtime, reinstall, purge

def test_uninstalling_one_runtime_never_removes_the_others_entries(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    other = lh.root / "other-runtime"
    settings = load(lh.claude_settings)
    settings["hooks"]["Stop"].append({"hooks": [hooks_merge.canonical(hooks_merge.managed_command(str(other), "claude"))]})
    lc.write_json(lh.claude_settings, settings)
    flags = {"runtime_dir": str(other)}
    env = ops.make_env(lh, flags=flags)
    env.paths = lh.resolve(flags)
    engine.uninstall(env)
    commands = [h["command"] for g in load(lh.claude_settings)["hooks"]["Stop"] for h in g["hooks"]]
    assert hooks_merge.managed_command(str(other), "claude") not in commands
    assert hooks_merge.managed_command(str(lh.runtime), "claude") in commands


def test_reinstall_after_uninstall_revives_the_entry_points(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    engine.install(ops.make_env(lh, now=ops.LATER))
    assert not manifest.is_tombstone(str(lh.runtime / "bin" / "quoin-deck-hook"))
    assert not (lh.runtime / "uninstalled.json").exists() and collect(lh, now=ops.LATER)["exit_code"] == 0
    ops.assert_hooks_inert(ops.run_hook_commands(lh))


def test_purge_removes_stubs_and_state_but_not_profiles(lifecycle_home):
    lh = lifecycle_home
    ops.make_adoption_fixture(lh)
    profile = lh.plugins.parent / "ProfilesV2" / "x.sdProfile"
    profile_hash = lc.tree_snapshot(profile)
    engine.install(ops.make_env(lh))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    env = ops.make_env(lh, purge=True, now=ops.LATER)
    result = engine.uninstall(env)
    text = "\n".join(result.lines)
    assert "restart these agents now" in text
    assert not (lh.runtime / "bin").exists() and not (lh.runtime / "monitor.py").exists()
    assert not (lh.runtime / "uninstalled.json").exists() and not (lh.runtime / ".install.lock").exists()
    assert not lh.state_root.exists()
    assert (lh.runtime / "backups").is_dir() and lc.tree_snapshot(profile) == profile_hash


@pytest.mark.parametrize("start", ["active", "pending", "prototype", "incomplete"])
def test_hooks_are_inert_after_uninstall_from_every_mode(lifecycle_home, start):
    lh = lifecycle_home
    if start == "active":
        engine.install(ops.make_env(lh))
    elif start == "pending":
        prototype_pending(lh)
    elif start == "prototype":
        ops.make_adoption_fixture(lh)
    else:
        with pytest.raises(engine.OperationError):
            engine.install(ops.make_env(lh, faults={"codex_settings:after": RuntimeError("boom")}))
    engine.uninstall(ops.make_env(lh, now=ops.LATER))
    ops.assert_hooks_inert(ops.run_hook_commands(lh, extra=legacy_extra(lh)))
