"""Read-only status report: fixtures, exit codes, source of truth, no writes."""
import builtins
import copy
import json
import os

import pytest

import _streamdeck_lifecycle as lc
import quoin.streamdeck as sd
from _streamdeck_lifecycle import deck_home, forbid_dangerous_subprocess, lifecycle_home  # noqa: F401
from quoin.streamdeck import hooks_merge, macos, manifest, status

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
FINGERPRINT = sd.payload_fingerprint()


def run(lh, platform=DARWIN, flags=None, environ=None, fingerprint=FINGERPRINT, now=lc.NOW):
    report = status.collect(lh.resolve(flags, environ), package_version=sd.COMPONENT_VERSION,
                            package_fingerprint=fingerprint, platform_info=platform, now=now)
    status.finalize(report)
    json.loads(status.render_json(report))
    status.render_text(report)
    return report


def codes(report, key="problems"):
    return {p["code"] for p in report[key]}


def rewrite_manifest(installed, fn):
    data = copy.deepcopy(installed.manifest)
    fn(data)
    manifest.write(installed.paths.manifest, data)
    return data


def settings(installed, kind="claude"):
    path = installed.paths.claude_settings if kind == "claude" else installed.paths.codex_hooks
    return path, json.load(open(path))


def drop_owned(data, event):
    data["hooks"][event] = [g for g in data["hooks"][event] if not any(
        hooks_merge.classify(h.get("command"), runtime_names=("QuoinStreamDeck",)) for h in g["hooks"])]


# ------------------------------------------------------------- AC-2.3 fixture table

def scenario_not_installed(lh):
    return 10, None


def scenario_healthy(lh):
    lc.make_installed(lh)
    return 0, None


def scenario_tampered(lh):
    inst = lc.make_installed(lh)
    with open(lh.runtime / "versions" / "1.0.0" / "monitor.py", "a") as handle:
        handle.write("# edit\n")
    return 11, "FILE_MODIFIED"


def scenario_hook_missing(lh):
    inst = lc.make_installed(lh)
    path, data = settings(inst)
    drop_owned(data, "Stop")
    lc.write_json(path, data)
    return 11, "HOOK_MISSING"


def scenario_duplicate(lh):
    inst = lc.make_installed(lh)
    path, data = settings(inst)
    data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": hooks_merge.managed_command(str(lh.runtime), "claude"), "timeout": 3}]})
    lc.write_json(path, data)
    return 11, "HOOK_DUPLICATE"


def scenario_interrupted(lh):
    inst = lc.make_installed(lh)
    lc.write_json(inst.paths.operation_marker, {"op": "upgrade", "phase": "staging", "steps_done": ["stage"], "started_at": "t"})
    return 12, "OPERATION_INCOMPLETE"


@pytest.mark.parametrize("scenario", [scenario_not_installed, scenario_healthy, scenario_tampered, scenario_hook_missing,
                                      scenario_duplicate, scenario_interrupted])
def test_fixture_table_exit_codes_and_read_only(lifecycle_home, scenario):
    expected, code = scenario(lifecycle_home)
    before = lc.tree_snapshot(lifecycle_home.root)
    report = run(lifecycle_home)
    assert report["exit_code"] == expected
    if code:
        assert code in codes(report)
    assert lc.tree_snapshot(lifecycle_home.root) == before


def test_no_writes_during_collect(lifecycle_home, monkeypatch):
    lc.make_installed(lifecycle_home)
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, mode="r", *a, **kw):
        if any(c in str(mode) for c in "wax+"):
            raise AssertionError("write open: %r" % (file,))
        return real_open(file, mode, *a, **kw)

    def guarded_os_open(path, flags, *a, **kw):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError("write os.open: %r" % (path,))
        return real_os_open(path, flags, *a, **kw)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    report = status.collect(lifecycle_home.resolve(), package_version=sd.COMPONENT_VERSION, package_fingerprint=FINGERPRINT,
                            platform_info=DARWIN, now=lc.NOW)
    assert not report["problems"]


# ------------------------------------------------------------- other states

def test_tombstones_only(lifecycle_home):
    lh = lifecycle_home
    lh.runtime.mkdir(parents=True)
    (lh.runtime / "monitor.py").write_text("# %s\n" % manifest.TOMBSTONE_MARKER)
    lc.write_json(lh.runtime / "uninstalled.json", {"tombstones": ["monitor.py"]})
    report = run(lh)
    assert report["exit_code"] == 10 and report["state"] == "tombstones-only"
    assert "not installed (1 inert hook stubs remain; remove with uninstall --purge)" in status.render_text(report)


def test_prototype_is_unadopted(lifecycle_home):
    lc.make_prototype(lifecycle_home)
    before = lc.tree_snapshot(lifecycle_home.root)
    report = run(lifecycle_home)
    assert report["exit_code"] == 11 and "PROTOTYPE_UNADOPTED" in codes(report)
    assert "UNMANAGED_LEFTOVER" in codes(report, "info")
    assert lc.tree_snapshot(lifecycle_home.root) == before


def test_dormant_and_adoption_pending(lifecycle_home):
    lc.make_installed(lifecycle_home, mode="dormant")
    report = run(lifecycle_home)
    assert report["exit_code"] == 11 and "MODE_DORMANT" in codes(report) and "CURRENT_MISSING" not in codes(report)


def test_adoption_pending(lifecycle_home):
    lc.make_installed(lifecycle_home, mode="adoption-pending")
    report = run(lifecycle_home)
    assert report["exit_code"] == 11 and "MODE_ADOPTION_PENDING" in codes(report)
    assert "CURRENT_MISSING" not in codes(report)


def test_invalid_manifest(lifecycle_home):
    lh = lifecycle_home
    lh.runtime.mkdir(parents=True)
    (lh.runtime / "install-manifest.json").write_text("{}")
    report = run(lh)
    assert report["exit_code"] == 11 and "MANIFEST_INVALID" in codes(report)


def test_malformed_settings_named_and_untouched(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    inst.paths.claude_settings
    open(inst.paths.claude_settings, "w").write("{broken")
    before = open(inst.paths.claude_settings, "rb").read()
    report = run(lifecycle_home)
    assert report["exit_code"] == 11
    assert any(p["code"] == "SETTINGS_MALFORMED" and inst.paths.claude_settings in p["detail"] for p in report["problems"])
    assert open(inst.paths.claude_settings, "rb").read() == before


def test_vanished_binary_and_bundled_node(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    os.unlink(inst.manifest["binaries"]["zellij"]["path"])
    report = run(lifecycle_home)
    assert report["exit_code"] == 11 and any("zellij" in p["detail"] for p in report["problems"] if p["code"] == "BINARY_VANISHED")
    os.unlink(inst.manifest["binaries"]["node"]["path"])
    rewrite_manifest(inst, lambda d: d["binaries"].update(node={"mode": "bundled", "path": None}))
    open(inst.manifest["binaries"]["zellij"]["path"], "w").write("#!/bin/sh\n")
    os.chmod(inst.manifest["binaries"]["zellij"]["path"], 0o755)
    again = run(lifecycle_home)
    assert "BINARY_VANISHED" not in codes(again)


def write_health(inst, body):
    with open(inst.paths.health, "w") as handle:
        json.dump(body, handle)


def test_health_stale_vs_fresh(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    assert run(lifecycle_home)["exit_code"] == 0
    now_ms = int(lc.NOW.timestamp() * 1000)
    write_health(inst, {"updated": now_ms - 400_000, "agents": 1})
    stale = run(lifecycle_home)
    assert stale["exit_code"] == 11 and "HEALTH_STALE" in codes(stale)
    assert "is Stream Deck running?" in json.dumps(stale)
    write_health(inst, {"updated": now_ms - 299_000, "agents": 1})
    assert run(lifecycle_home)["exit_code"] == 0
    write_health(inst, {"updated": now_ms, "error_type": "TimeoutError"})
    assert "PLUGIN_SNAPSHOT_ERROR" in codes(run(lifecycle_home), "info")
    os.unlink(inst.paths.health)
    never = run(lifecycle_home)
    assert never["exit_code"] == 0 and "PLUGIN_NEVER_RAN" in codes(never, "info")


def test_declined_feature_is_not_a_problem(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    rewrite_manifest(inst, lambda d: (d["prereqs"].update(zellij={"state": "declined"}), d.update(features_unavailable=["focus"])))
    report = run(lifecycle_home)
    assert report["exit_code"] == 0 and "PREREQ_FEATURE_UNAVAILABLE" in codes(report, "info")
    assert "Not installed by choice" in status.render_text(report)
    rewrite_manifest(inst, lambda d: d["prereqs"].update(node={"state": "missing"}))
    assert "PREREQ_CORE_MISSING" in codes(run(lifecycle_home))


def test_optional_tool_missing_is_info_only(lifecycle_home):
    lc.make_installed(lifecycle_home)
    report = run(lifecycle_home)
    assert report["exit_code"] == 0 and "OPTIONAL_TOOL_MISSING" in codes(report, "info")


def test_upgrade_available_info(lifecycle_home):
    lc.make_installed(lifecycle_home)
    report = run(lifecycle_home, fingerprint="0" * 64)
    assert report["exit_code"] == 0 and "UPGRADE_AVAILABLE" in codes(report, "info")


def test_unsupported_platform(lifecycle_home):
    report = run(lifecycle_home, platform=macos.PlatformInfo("linux", "", "x86_64", False))
    assert report["state"] == "unsupported-platform" and report["exit_code"] == 0


def test_recorded_paths_win_over_the_shell_environment(lifecycle_home):
    lh = lifecycle_home
    alt = lh.root / "alt"
    flags = {"state_root": str(lh.root / "alt-state")}
    inst = lc.make_installed(lh, flags=flags, environ={"HOME": str(lh.home), "CLAUDE_CONFIG_DIR": str(alt)})
    assert not lh.claude_settings.exists()
    report = run(lh)  # a shell without CLAUDE_CONFIG_DIR and with the default state root
    assert report["exit_code"] == 0
    differ = [i["detail"] for i in report["info"] if i["code"] == "PATHS_DIFFER"]
    assert any(d.startswith("claude_settings") for d in differ) and any(d.startswith("state:") for d in differ)
    assert report["paths"]["claude_settings"] == str(alt / "settings.json") and "paths_resolved" in report
    assert not lh.claude_settings.exists()


def test_claude_only_manifest_has_no_codex_lines(lifecycle_home):
    lc.make_installed(lifecycle_home, claude_only=True)
    report = run(lifecycle_home)
    assert report["exit_code"] == 0 and set(report["hooks"]) == {"claude"}
    assert not report["codex_trust"] and "CODEX_TRUST" not in codes(report, "info")
    assert not any("codex:" in p["detail"] for p in report["problems"])


def test_active_without_hooks(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    rewrite_manifest(inst, lambda d: d.update(hooks=[]))
    report = run(lifecycle_home)
    assert report["exit_code"] == 11 and "HOOKS_NONE_RECORDED" in codes(report)


def test_current_missing_and_mismatch(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    other = lifecycle_home.runtime / "versions" / "9.9.9"
    other.mkdir()
    os.unlink(inst.paths.current)
    os.symlink("versions/9.9.9", inst.paths.current)
    report = run(lifecycle_home)
    assert report["exit_code"] == 11 and "CURRENT_MISMATCH" in codes(report)
    assert "9.9.9" in json.dumps(report["problems"]) and "1.0.0" in json.dumps(report["problems"])
    os.unlink(inst.paths.current)
    assert "CURRENT_MISSING" in codes(run(lifecycle_home))


@pytest.mark.parametrize("scan_fallback", [False, True])
def test_codex_trust_hint(lifecycle_home, monkeypatch, scan_fallback):
    if scan_fallback:
        monkeypatch.setattr(status, "_tomllib", None)
    inst = lc.make_installed(lifecycle_home)
    key = "%s:session_start:0:0" % inst.paths.codex_hooks
    config = lifecycle_home.codex_home / "config.toml"
    config.write_text('[hooks.state."%s"]\ntrusted_hash = "x"\n' % key)
    report = run(lifecycle_home)
    states = {t["event"]: t["present"] for t in report["codex_trust"]}
    assert states["SessionStart"] is True and states["Stop"] is False
    assert report["exit_code"] == 0
    assert any("no trust record - review in Codex /hooks" in i["detail"] for i in report["info"])
    config.unlink()
    assert any("no Codex config" in i["detail"] for i in run(lifecycle_home)["info"])


def test_unreadable_manifest_is_a_problem_code(lifecycle_home):
    lh = lifecycle_home
    lh.runtime.mkdir(parents=True)
    (lh.runtime / "install-manifest.json").mkdir()
    report = run(lh)
    assert report["exit_code"] == 11 and "MANIFEST_INVALID" in codes(report)
    assert any("unreadable" in p["detail"] for p in report["problems"])


def test_unreadable_recorded_settings_path_is_a_problem_code(lifecycle_home):
    inst = lc.make_installed(lifecycle_home)
    os.remove(inst.paths.claude_settings)
    os.mkdir(inst.paths.claude_settings)  # a directory where the settings file should be
    report = run(lifecycle_home)
    assert report["exit_code"] == 11
    assert any(p["code"] == "SETTINGS_UNREADABLE" and inst.paths.claude_settings in p["detail"] for p in report["problems"])


def test_unreadable_settings_without_manifest_is_info_only(lifecycle_home):
    lh = lifecycle_home
    paths = lh.resolve()
    os.makedirs(os.path.dirname(paths.claude_settings), exist_ok=True)
    os.mkdir(paths.claude_settings)
    report = run(lh)
    assert "SETTINGS_UNREADABLE" in codes(report, "info")
