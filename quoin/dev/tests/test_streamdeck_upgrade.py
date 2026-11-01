"""Upgrades, downgrade refusal, the schema chain and revived tombstones."""
import json
import os
import shutil

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
from quoin.streamdeck import engine, hooks_merge, macos, manifest, migrations, shims, status
import quoin.streamdeck as sd

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
NAMES = ("QuoinStreamDeck",)


def collect(lh, now=lc.NOW):
    report = status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN, now=now)
    return status.finalize(report)


def load(path):
    with open(str(path)) as handle:
        return json.load(handle)


def codex_path(lh):
    return lh.codex_home / "hooks.json"


def older_install(lh):
    """An installed 0.9.0 component with an older payload, a stale owned file and older-form hooks."""
    installed = lc.make_installed(lh, component_version="0.9.0", version_dir_name="0.9.0")
    target = installed.version_dir / "monitor.py"
    target.write_text(target.read_text() + "\n# older payload\n")
    data = load(lh.runtime / "install-manifest.json")
    for entry in data["files"]:
        if entry["rel"] == "versions/0.9.0/monitor.py" or entry["rel"].endswith("0.9.0/monitor.py"):
            entry["sha256"] = engine._sha256_file(str(target))
    stale = lh.runtime / "bin" / "quoin-deck-legacy-helper"
    stale.write_text("#!/bin/sh\nexit 0\n")
    stale.chmod(0o755)
    data["files"].append({"rel": "bin/quoin-deck-legacy-helper", "root": "runtime",
                          "sha256": engine._sha256_file(str(stale)), "mode": 0o755, "kind": "shim"})
    data["files"].sort(key=lambda e: (e["root"], e["rel"]))
    manifest.write(str(lh.runtime / "install-manifest.json"), data)
    return installed


def test_upgrade_from_an_older_install(lifecycle_home):
    lh = lifecycle_home
    older_install(lh)
    # foreign entries mixed into the managed settings, hooks in an older form
    for kind, path in (("claude", lh.claude_settings), ("codex", codex_path(lh))):
        settings = load(path)
        settings["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "/usr/local/bin/notify"}]})
        for groups in settings["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    if hooks_manages(hook):
                        hook["timeout"] = 10
        lc.write_json(path, settings)
    lh.state_root.mkdir(parents=True, exist_ok=True)
    lc.write_json(lh.state_root / "config.json", {"project": "/p"})
    config_before = (lh.state_root / "config.json").read_bytes()
    result = engine.upgrade(ops.make_env(lh))
    data = load(lh.runtime / "install-manifest.json")
    assert manifest.validate(data) == [] and data["component_version"] == sd.COMPONENT_VERSION
    assert data["version_dir"].endswith(sd.COMPONENT_VERSION + "-" + sd.payload_fingerprint()[:12])
    assert data["previous_version_dir"].endswith("0.9.0") and os.path.isdir(data["previous_version_dir"])
    assert os.readlink(str(lh.runtime / "current")).startswith("versions/" + sd.COMPONENT_VERSION)
    assert not (lh.runtime / "bin" / "quoin-deck-legacy-helper").exists()
    for kind, path in (("claude", lh.claude_settings), ("codex", codex_path(lh))):
        checked = hooks_merge.inspect(load(path), kind, hooks_merge.managed_command(str(lh.runtime), kind),
                                      runtime_names=NAMES)
        assert set(checked.values()) == {"ok"}
        assert any(h["command"] == "/usr/local/bin/notify" for g in load(path)["hooks"]["Stop"] for h in g["hooks"])
    assert (lh.state_root / "config.json").read_bytes() == config_before
    assert collect(lh)["exit_code"] == 0
    assert "upgrading from 0.9.0 to %s" % sd.COMPONENT_VERSION in result.lines
    # a second run is a no-op
    first = ops.snapshot_owned(lh)
    first_manifest = load(lh.runtime / "install-manifest.json")
    again = engine.upgrade(ops.make_env(lh, now=ops.LATER))
    second_manifest = load(lh.runtime / "install-manifest.json")
    assert ops.snapshot_owned(lh) == first and again.changed is False
    differing = {k for k in second_manifest if second_manifest[k] != first_manifest.get(k)}
    assert differing <= {"last_verified_at"}


def hooks_manages(hook):
    return hooks_merge.classify(hook.get("command"), runtime_names=NAMES) == "managed"


def test_an_older_schema_is_migrated_once(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    lc.make_installed(lh)
    monkeypatch.setattr(manifest, "SCHEMA", 2)
    calls = []

    def one_to_two(data):
        calls.append(1)
        data["schema"] = 2
        return data

    monkeypatch.setattr(migrations, "SCHEMA_MIGRATIONS", [(1, 2, one_to_two)])
    engine.upgrade(ops.make_env(lh, now=ops.LATER))
    assert load(lh.runtime / "install-manifest.json")["schema"] == 2
    calls.clear()
    engine.upgrade(ops.make_env(lh, now=ops.LATER))
    assert calls == []


def test_a_newer_installed_version_is_refused_unless_downgrade_is_allowed(lifecycle_home):
    lh = lifecycle_home
    lc.make_installed(lh, component_version="9.0.0", version_dir_name="9.0.0")
    before = ops.snapshot_owned(lh)
    for op in (engine.install, engine.upgrade):
        with pytest.raises(engine.OperationError) as exc:
            op(ops.make_env(lh))
        assert exc.value.code == 1 and "newer" in str(exc.value)
        assert ops.snapshot_owned(lh) == before
    engine.upgrade(ops.make_env(lh, allow_downgrade=True, now=ops.LATER))
    assert load(lh.runtime / "install-manifest.json")["component_version"] == sd.COMPONENT_VERSION


# ------------------------------------------------------------------ tombstones

def tombstone_fixture(lh):
    lc.make_installed(lh)
    # turn the installed component into the state `uninstall` leaves behind
    data = load(lh.runtime / "install-manifest.json")
    os.unlink(str(lh.runtime / "install-manifest.json"))
    shutil.rmtree(str(lh.runtime / "versions"))
    os.unlink(str(lh.runtime / "current"))
    shutil.rmtree(str(lh.plugin_dir))
    os.unlink(str(lh.runtime / "bin" / "quoin-deck-terminal"))
    hook = lh.runtime / "bin" / "quoin-deck-hook"
    hook.write_text(shims.render_tombstone("sh"))
    monitor = lh.runtime / "monitor.py"
    monitor.write_text(shims.render_tombstone("python"))
    monitor.chmod(0o755)
    lc.write_json(lh.runtime / "uninstalled.json", {"tombstones": [str(hook), str(monitor)],
                                                    "uninstalled_at": "2026-10-13T12:00:00Z"})
    settings = load(lh.claude_settings)
    settings["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": lc.legacy_command(lh.runtime).replace(
        "/usr/bin/python3", lc.fixture_python(lh))}]})
    lc.write_json(lh.claude_settings, settings)
    return data


def test_reinstall_over_tombstones_revives_the_entry_points(lifecycle_home):
    lh = lifecycle_home
    tombstone_fixture(lh)
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    engine.install(ops.make_env(lh))
    data = load(lh.runtime / "install-manifest.json")
    assert not (lh.runtime / "uninstalled.json").exists() and data["adopted_from"] is None
    assert not manifest.is_tombstone(str(lh.runtime / "bin" / "quoin-deck-hook"))
    assert not manifest.is_tombstone(str(lh.runtime / "monitor.py"))
    assert {e["rel"] for e in data["files"] if e["kind"] == "forwarder"} == {"monitor.py"}
    assert collect(lh)["exit_code"] == 0
    ops.assert_hooks_inert(ops.run_hook_commands(lh))


def test_a_staging_fault_restores_the_tombstones(lifecycle_home):
    lh = lifecycle_home
    tombstone_fixture(lh)
    before = {rel: lc.tree_snapshot(lh.runtime).get(rel) for rel in ("bin/quoin-deck-hook", "monitor.py", "uninstalled.json")}
    with pytest.raises(engine.OperationError):
        engine.install(ops.make_env(lh, faults={"swap_current:after": RuntimeError("boom")}))
    assert not os.path.lexists(str(lh.runtime / "current")) and not (lh.runtime / "operation.json").exists()
    after = lc.tree_snapshot(lh.runtime)
    for rel, value in before.items():
        assert after.get(rel) == value, rel
    ops.assert_hooks_inert(ops.run_hook_commands(lh))


# ------------------------------------------------------------------ a second runtime

def other_runtime_env(lh):
    flags = {"runtime_dir": str(lh.root / "other-runtime")}
    env = ops.make_env(lh, flags=flags)
    env.paths = lh.resolve(flags)
    return env


def test_installing_over_another_runtimes_hooks_is_refused(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    other = lh.root / "other-runtime"
    before = ops.snapshot_owned(lh)
    settings_before = lh.claude_settings.read_bytes()
    with pytest.raises(engine.OperationError) as exc:
        engine.install(other_runtime_env(lh))
    message = str(exc.value)
    assert exc.value.code == 1 and str(lh.runtime) in message and "uninstall" in message and "--runtime-dir" in message
    assert lh.claude_settings.read_bytes() == settings_before and not other.exists()
    assert ops.snapshot_owned(lh) == before


def test_stale_entries_of_a_runtime_without_a_manifest_are_replaced_in_place(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    os.unlink(str(lh.runtime / "install-manifest.json"))
    other = lh.root / "other-runtime"
    result = engine.install(other_runtime_env(lh))
    command = hooks_merge.managed_command(str(other), "claude")
    owned = hooks_merge.owned_commands(load(lh.claude_settings), "claude", runtime_names=NAMES)
    assert len(owned) == len(hooks_merge.EVENTS["claude"])
    assert all(command in load(lh.claude_settings)["hooks"][event][gi]["hooks"][hi]["command"]
               for event, gi, hi in owned)
    assert any(line.startswith("warning: replacing the hook command") for line in result.lines)
