"""Interactive launches admit Quoin's pinned lifecycle plugin, and nothing else,
when the profile enables it."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import compiler, doctor, driver, events, inventory, plugin_artifact
from quoin.opencode_adapter import profile_policy as pp

from _opencode_run_helpers import InstalledProject

REPLAY = ("replay", {"fixture": "plain-complete.jsonl"})
ARTIFACT = helpers.SOURCE_DIR / "adapters" / "opencode" / "plugin" / plugin_artifact.PLUGIN_FILE

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture
def world(tmp_path, monkeypatch):
    project = InstalledProject(tmp_path, monkeypatch, REPLAY)
    project.make = project.driver_factory()
    yield project
    project.cleanup()


def enable(project, value="allow"):
    project.world.profile.setdefault("policy", {})["plugin"] = {"lifecycle": value}
    project.world.write()


def test_pinned_identity_matches_the_shipped_artifact():
    data = ARTIFACT.read_bytes()
    assert hashlib.sha256(data).hexdigest() == plugin_artifact.PLUGIN_SHA256
    assert plugin_artifact.version_of(data) == plugin_artifact.PLUGIN_VERSION
    assert plugin_artifact.status().state == "ok"


def test_a_work_profile_loads_no_plugin_by_default(world):
    launch = world.make(world.root).prepare_interactive("work")
    env = launch.launch_env.materialize()
    assert launch.config_path.name == compiler.NATIVE_FILE
    assert launch.run_id is None and launch.plugin_log is None
    assert env["OPENCODE_PURE"] == "1"
    assert not set(plugin_artifact.PLUGIN_ENV_NAMES) & set(env)
    assert "plugin" not in launch.effective_config


def test_an_enabled_profile_admits_exactly_the_pinned_plugin(world):
    enable(world)
    drv = world.make(world.root)
    launch = drv.prepare_interactive("work")
    env = launch.launch_env.materialize()

    assert launch.config_path.name == plugin_artifact.INTERACTIVE_CONFIG_FILE
    assert env["OPENCODE_CONFIG"] == str(launch.config_path)
    doc = json.loads(launch.config_path.read_text(encoding="utf-8"))
    copy = launch.profile_root / "lifecycle-plugin" / plugin_artifact.PLUGIN_FILE
    assert doc.pop("plugin") == [copy.as_uri()]
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == plugin_artifact.PLUGIN_SHA256
    base = launch.config_path.parent / compiler.NATIVE_FILE
    assert doc == json.loads(base.read_text(encoding="utf-8"))

    assert events.RUN_ID_RE.match(launch.run_id)
    assert env[plugin_artifact.ENV_RUN_ID] == launch.run_id
    log = Path(env[plugin_artifact.ENV_LOG])
    assert log == launch.plugin_log and log.is_absolute()
    assert log.parent.parent == launch.profile_root and log.name == launch.run_id + ".jsonl"
    assert "OPENCODE_PURE" not in env
    assert env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "1"

    section = launch.effective_config["plugin"]
    assert section["version"] == plugin_artifact.PLUGIN_VERSION
    assert section["sha256"] == plugin_artifact.PLUGIN_SHA256
    assert launch.effective_config["policy"]["plugin.lifecycle"] == "allow"

    # The headless driver is unchanged: same compiled file, no plugin variables.
    headless = drv.prepare(driver.RunRequest(
        project_root=world.root, task="demo", stage=None, phase="plan", profile="work"))
    assert headless.config_path == base
    assert not set(plugin_artifact.PLUGIN_ENV_NAMES) & set(headless.env_names)
    assert "OPENCODE_PURE" in headless.env_names


@pytest.mark.parametrize("layers, default, expected", [
    ({"project": "allow"}, "deny", "deny"),
    ({"project": "deny"}, "allow", "deny"),
    ({"profile": "allow", "managed": "deny"}, "deny", "deny"),
])
def test_only_the_profile_or_trusted_policy_can_turn_the_plugin_on(layers, default, expected):
    assert pp.merge_enum("plugin.lifecycle", layers, default)[0] == expected


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _global_dir(project):
    return inventory.xdg_config_dir(project.world.env, project.world.home)


OTHER_PLUGIN = json.dumps({"plugin": ["file:///tmp/other.js"]})


@pytest.mark.parametrize("plant, code", [
    (lambda p: _write(p.root / "opencode.json", OTHER_PLUGIN), "protected-key-overridden"),
    (lambda p: _write(_global_dir(p) / "opencode.json", OTHER_PLUGIN), "protected-key-overridden"),
    (lambda p: _write(p.root / ".opencode" / "plugins" / "other.js", "export {}"), "plugin-directory-present"),
    (lambda p: _write(p.root / "tui.json", OTHER_PLUGIN), "tui-plugin-present"),
    (lambda p: _write(_global_dir(p) / "tui.jsonc", OTHER_PLUGIN), "tui-plugin-present"),
])
def test_every_other_plugin_is_still_refused(world, plant, code):
    enable(world)
    plant(world)
    with pytest.raises(driver.PrepareRefused) as info:
        world.make(world.root).prepare_interactive("work")
    assert info.value.code == code


def test_a_packaged_artifact_that_differs_from_the_pin_is_refused(world, monkeypatch):
    enable(world)
    monkeypatch.setattr(plugin_artifact, "PLUGIN_SHA256", "0" * 64)
    with pytest.raises(driver.PrepareRefused) as info:
        world.make(world.root).prepare_interactive("work")
    assert (info.value.category, info.value.code) == ("invalid-configuration", "plugin-artifact-changed")


def test_a_missing_packaged_artifact_is_refused(world, monkeypatch, tmp_path):
    enable(world)
    monkeypatch.setattr(plugin_artifact, "packaged_path", lambda: tmp_path / "absent.js")
    with pytest.raises(driver.PrepareRefused) as info:
        world.make(world.root).prepare_interactive("work")
    assert (info.value.category, info.value.code) == ("workflow-validation", "plugin-artifact-missing")


def test_a_profile_copy_that_reads_back_differently_is_refused(world, monkeypatch):
    enable(world)
    real = plugin_artifact._read  # noqa: SLF001
    monkeypatch.setattr(plugin_artifact, "_read",
                        lambda path: b"tampered" if "lifecycle-plugin" in str(path) else real(path))
    with pytest.raises(driver.PrepareRefused) as info:
        world.make(world.root).prepare_interactive("work")
    assert info.value.code == "plugin-artifact-changed"


def test_doctor_reports_the_plugin_version_and_its_compatibility(monkeypatch):
    found = doctor._lifecycle_plugin_findings(helpers.SOURCE_DIR)  # noqa: SLF001
    assert [(f.id, f.severity) for f in found] == [("runtime-lifecycle-plugin", "info")]
    assert plugin_artifact.PLUGIN_VERSION in found[0].message
    assert plugin_artifact.PLUGIN_OPENCODE_VERSION in found[0].message

    monkeypatch.setattr(plugin_artifact, "PLUGIN_OPENCODE_VERSION", "0.0.1")
    assert [f.id for f in doctor._lifecycle_plugin_findings(helpers.SOURCE_DIR)] == [  # noqa: SLF001
        "runtime-lifecycle-plugin-incompatible"]

    monkeypatch.setattr(plugin_artifact, "PLUGIN_SHA256", "0" * 64)
    assert [f.id for f in doctor._lifecycle_plugin_findings(helpers.SOURCE_DIR)] == [  # noqa: SLF001
        "runtime-lifecycle-plugin-invalid"]
