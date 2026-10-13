"""Manifest schema, validation, detection, and the contract with the payload's deckenv."""
import copy
import datetime
import json
import os

import pytest

import _streamdeck_lifecycle as lc
from _streamdeck_lifecycle import deck_home, forbid_dangerous_subprocess, lifecycle_home  # noqa: F401
from _streamdeck_helpers import payload  # noqa: F401
from quoin.streamdeck import manifest, payload_build

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess")


@pytest.fixture
def installed(lifecycle_home):
    return lc.make_installed(lifecycle_home)


def problems(data):
    return manifest.validate(data)


def mutated(installed, fn):
    data = copy.deepcopy(installed.manifest)
    fn(data)
    return data


def test_round_trip(lifecycle_home, installed):
    assert manifest.load(installed.paths.manifest) == installed.manifest
    assert problems(installed.manifest) == []
    assert oct(os.stat(installed.paths.manifest).st_mode & 0o777) == "0o644"


def test_absent_and_malformed_load(lifecycle_home):
    paths = lifecycle_home.resolve()
    assert manifest.load(paths.manifest) is None
    os.makedirs(paths.runtime)
    open(paths.manifest, "w").write("{")
    with pytest.raises(manifest.ManifestError):
        manifest.load(paths.manifest)
    open(paths.manifest, "w").write("{}")
    with pytest.raises(manifest.ManifestError) as exc:
        manifest.load(paths.manifest)
    assert exc.value.problems


def test_constants_match_the_payload(deck_home, payload):
    deckenv = payload("deckenv")
    assert manifest.PLUGIN_UUID == payload_build.PLUGIN_UUID
    assert manifest.ACTION_UUID == payload_build.ACTION_UUID
    assert manifest.OPTIONAL_TOOL_KEYS == deckenv.TOOL_BINS
    assert {"zellij", "osascript", "open", "python", "agentdesk"} <= set(manifest.CORE_BINARY_KEYS)


@pytest.mark.parametrize("name,fn,fragment", [
    ("schema", lambda d: d.update(schema=2), "schema"),
    ("component", lambda d: d.update(component="x"), "component"),
    ("mode", lambda d: d.update(mode="weird"), "mode"),
    ("relative path", lambda d: d["paths"].update(runtime="rel/x"), "paths.runtime"),
    ("missing path", lambda d: d["paths"].pop("agentdesk"), "paths.agentdesk"),
    ("state invariant", lambda d: d["paths"].update(state="/elsewhere/state"), "state-dir invariant"),
    ("config basename", lambda d: d["paths"].update(config="/a/other.json", state="/a/state"), "config.json"),
    ("versions path", lambda d: d["paths"].update(plugin_dir="/a/versions/1/p"), "versions/"),
    ("relative binary", lambda d: d["binaries"].update(zellij={"path": "zellij"}), "binaries.zellij"),
    ("unknown optional", lambda d: d["binaries"].update(emacs={"path": "/x/emacs"}), "binaries.emacs"),
    ("external node null path", lambda d: d["binaries"].update(node={"mode": "external", "path": None}), "binaries.node"),
    ("bundled node with path", lambda d: d["binaries"].update(node={"mode": "bundled", "path": "/x/node"}), "bundled"),
    ("bad node mode", lambda d: d["binaries"].update(node={"mode": "magic", "path": "/x"}), "binaries.node.mode"),
    ("file rel dotdot", lambda d: d["files"][0].update(rel="../x"), "files[0].rel"),
    ("file abs rel", lambda d: d["files"][0].update(rel="/x"), "files[0].rel"),
    ("file root", lambda d: d["files"][0].update(root="home"), "files[0].root"),
    ("file sha", lambda d: d["files"][0].update(sha256="abc"), "files[0].sha256"),
    ("file mode", lambda d: d["files"][0].update(mode="644"), "files[0].mode"),
    ("file kind", lambda d: d["files"][0].update(kind="data"), "files[0].kind"),
    ("hook runtime", lambda d: d["hooks"][0].update(runtime="gemini"), "hooks[0].runtime"),
    ("hook event", lambda d: d["hooks"][0].update(event="Nope"), "hooks[0].event"),
    ("hook settings", lambda d: d["hooks"][0].update(settings_path="rel"), "settings_path"),
    ("hook versions", lambda d: d["hooks"][0].update(command="/rt/versions/1/x claude"), "versions/"),
    ("hook timeout", lambda d: d["hooks"][0].update(timeout="3"), "timeout"),
    ("hook created", lambda d: d["hooks"][0].update(created_event_key=None), "created_event_key"),
])
def test_validate_rules_fail(installed, name, fn, fragment):
    found = problems(mutated(installed, fn))
    assert any(fragment in p for p in found), (name, found)


def test_bundled_node_with_null_path_is_valid(installed):
    data = mutated(installed, lambda d: d["binaries"].update(node={"mode": "bundled", "path": None}))
    assert problems(data) == []


def test_same_except_verified(installed):
    other = copy.deepcopy(installed.manifest)
    other["last_verified_at"] = "2030-01-01T00:00:00Z"
    assert manifest.same_except_verified(installed.manifest, other)
    other["mode"] = "dormant"
    assert not manifest.same_except_verified(installed.manifest, other)


def test_optional_tools_recorded_unresolved(tmp_path):
    real = tmp_path / "real" / "claude"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(real)
    table = {"claude": str(link), "codex": "relative/codex", "ccr": None}
    out = manifest.record_optional_tools(table.get)
    assert out == {"claude": {"path": str(link)}}


def test_detection_matrix(lifecycle_home):
    lh = lifecycle_home

    def detect():
        paths = lh.resolve()
        claude, _ = (jsonload(paths.claude_settings))
        codex, _ = (jsonload(paths.codex_hooks))
        return manifest.detect(paths, claude, codex)

    def jsonload(path):
        try:
            return json.load(open(path)), None
        except OSError:
            return {}, None

    assert detect().state == "not-installed"
    lh.runtime.mkdir(parents=True)
    (lh.runtime / "monitor.py").write_text("# real script\n")
    assert detect().state == "prototype"
    (lh.runtime / "monitor.py").unlink()
    assert detect().state == "not-installed"
    lh.plugin_dir.mkdir(parents=True)
    assert detect().state == "prototype"
    lh.plugin_dir.rmdir()
    lc.write_json(lh.claude_settings, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": lc.legacy_command(lh.runtime)}]}]}})
    result = detect()
    assert result.state == "prototype" and result.legacy_hook_count == 1
    lh.claude_settings.unlink()
    (lh.runtime / "monitor.py").write_text("# %s\n" % manifest.TOMBSTONE_MARKER)
    (lh.runtime / "uninstalled.json").write_text(json.dumps({"tombstones": ["monitor.py"]}))
    result = detect()
    assert result.state == "tombstones-only" and result.tombstones
    assert manifest.is_tombstone(str(lh.runtime / "monitor.py"))
    assert manifest.read_uninstalled(lh.resolve()) == ["monitor.py"]
    (lh.runtime / "install-manifest.json").write_text("{}")
    assert detect().state == "managed"


def test_operation_marker(lifecycle_home):
    lh = lifecycle_home
    paths = lh.resolve()
    assert manifest.read_operation_marker(paths) is None
    lh.runtime.mkdir(parents=True)
    lc.write_json(paths.operation_marker, {"op": "install", "phase": "staging", "steps_done": ["a"], "started_at": "t"})
    assert manifest.read_operation_marker(paths)["phase"] == "staging"
    open(paths.operation_marker, "w").write("{broken")
    assert manifest.read_operation_marker(paths) == {"phase": "unreadable"}


def deckenv_environ(lh, **extra):
    env = {"HOME": str(lh.home), "QUOIN_DECK_RUNTIME": str(lh.runtime), "PATH": "/usr/bin:/bin"}
    env.update(extra)
    return env


def test_contract_with_deckenv_both_binary_forms(lifecycle_home, payload):
    deckenv = payload("deckenv")
    installed = lc.make_installed(lifecycle_home)
    paths = installed.paths
    env = deckenv.load(environ=deckenv_environ(lifecycle_home))
    assert env.config_path == paths.config and env.state_dir == paths.state
    rec = installed.manifest["binaries"]
    for name in ("python", "zellij", "osascript", "open"):
        assert env.bins[name] == rec[name]["path"]
    assert env.tools["claude"] == rec["claude"]["path"]
    assert env.socket_dir == installed.manifest["zellij_socket_dir"]
    assert env.agentdesk == rec["agentdesk"]["path"]
    plain = copy.deepcopy(installed.manifest)
    plain["binaries"] = {k: (v["path"] if isinstance(v, dict) and k not in ("node",) else v) for k, v in rec.items()}
    manifest.write(paths.manifest, plain)
    again = deckenv.load(environ=deckenv_environ(lifecycle_home))
    assert again.bins["python"] == rec["python"]["path"] and again.tools == env.tools


@pytest.mark.parametrize("alt", [False, True])
def test_contract_state_dir_agrees_with_plugin_export(lifecycle_home, payload, alt):
    deckenv = payload("deckenv")
    flags = {"state_root": str(lifecycle_home.root / "alt-root")} if alt else {}
    paths = lifecycle_home.resolve(flags)
    data = lc.make_installed(lifecycle_home).manifest
    data = manifest.new_manifest(
        paths=paths, component_version="1.0.0", payload_fingerprint="f", quoin_version="0", platform={},
        mode="active", binaries=data["binaries"], zellij_socket_dir=data["zellij_socket_dir"], files=[],
        version_dir=data["version_dir"], previous_version_dir=None, hooks=[], plugin={}, prereqs={},
        features_unavailable=[], adopted_from=None, legacy_owned_events=[], now=lc.NOW)
    manifest.write(paths.manifest, data)
    manifest_only = deckenv.load(environ=deckenv_environ(lifecycle_home))
    exported = payload_build.plugin_config(data["binaries"]["python"]["path"], paths.runtime, paths.state_root)["state_root"]
    with_export = deckenv.load(environ=deckenv_environ(lifecycle_home, QUOIN_DECK_STATE_ROOT=exported))
    assert manifest_only.state_dir == with_export.state_dir == paths.state
    assert manifest_only.config_path == with_export.config_path == paths.config
