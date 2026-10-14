"""Fresh, repeat and dormant installs through the engine."""
import copy
import dataclasses
import json
import os
import stat
import sys

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
from quoin.streamdeck import engine, hooks_merge, jsonfile, macos, manifest, migrations, payload_build, status
import quoin.streamdeck as sd

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)


def collect(lh, now=lc.NOW):
    report = status.collect(lh.resolve(), package_version=sd.COMPONENT_VERSION,
                            package_fingerprint=sd.payload_fingerprint(), platform_info=DARWIN, now=now)
    return status.finalize(report)


def codes(report):
    return {p["code"] for p in report["problems"]}


def load(path):
    with open(str(path)) as handle:
        return json.load(handle)


def codex_path(lh):
    return lh.codex_home / "hooks.json"


def managed_in(data, kind, runtime):
    return hooks_merge.inspect(data, kind, hooks_merge.managed_command(str(runtime), kind),
                               runtime_names=("QuoinStreamDeck",))


def owned_total(path, kind):
    return len(hooks_merge.owned_commands(load(path), kind, runtime_names=("QuoinStreamDeck",)))


# ------------------------------------------------------------------ clean install

def test_clean_install(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh)
    result = engine.install(env)
    data = load(lh.runtime / "install-manifest.json")
    assert manifest.validate(data) == [] and data["mode"] == "active" and result.mode == "active"
    for entry in data["files"]:
        base = lh.runtime if entry["root"] == "runtime" else lh.plugin_dir
        full = base / entry["rel"]
        assert engine._sha256_file(str(full)) == entry["sha256"]
        assert stat.S_IMODE(os.stat(str(full)).st_mode) == entry["mode"]
    assert {e["kind"] for e in data["files"]} == {"code", "shim", "plugin", "profile"}
    for kind, path in (("claude", lh.claude_settings), ("codex", codex_path(lh))):
        assert set(managed_in(load(path), kind, lh.runtime).values()) == {"ok"}
        assert owned_total(path, kind) == len(hooks_merge.EVENTS[kind])
    report = collect(lh)
    assert report["exit_code"] == 0 and report["problems"] == []
    config = load(lh.plugin_dir / "plugin-config.json")
    assert config["state_root"] + "/state" == data["paths"]["state"]
    assert "QUOIN_DECK_RUNTIME" in (lh.runtime / "bin" / "quoin-deck-hook").read_text()
    texts = [lh.claude_settings.read_text(), codex_path(lh).read_text(), (lh.runtime / "bin" / "quoin-deck-hook").read_text(),
             (lh.runtime / "bin" / "quoin-deck-terminal").read_text(), (lh.plugin_dir / "plugin-config.json").read_text(),
             (lh.plugin_dir / "launch.sh").read_text()]
    forbidden = [sys.prefix, os.environ.get("VIRTUAL_ENV") or sys.prefix, str(ops.h.REPO_ROOT), "site-packages", "/versions/"]
    for text in texts:
        for bad in forbidden:
            assert bad not in text
    assert os.readlink(str(lh.runtime / "current")).startswith("versions/")
    assert not (lh.runtime / "operation.json").exists()
    assert os.path.islink(str(lh.runtime / "current")) and os.path.isdir(str(lh.runtime / "versions"))


def test_hook_commands_are_inert_after_install(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    results = ops.run_hook_commands(lh)
    assert len(results) >= 1
    ops.assert_hooks_inert(results)


def test_install_output_lines(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh)
    result = engine.install(env)
    text = "\n".join(result.lines)
    assert "installed (active)" in text and "import it in Stream Deck" in text
    assert "Claude Code: start new sessions" in text
    assert "Codex: review the changed hooks in Codex /hooks (trust is never written by quoin)" in text
    assert "Stream Deck is running" not in text


# ------------------------------------------------------------------ repeat install

def test_repeat_install_is_a_no_op_except_the_verification_clock(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh, now=lc.NOW))
    first = load(lh.runtime / "install-manifest.json")
    before = ops.snapshot_owned(lh)
    settings_before = (lh.claude_settings.read_bytes(), codex_path(lh).read_bytes())
    backups_before = sorted(os.listdir(str(lh.runtime / "backups"))) if (lh.runtime / "backups").exists() else []
    result = engine.install(ops.make_env(lh, now=ops.LATER))
    second = load(lh.runtime / "install-manifest.json")
    assert result.changed is False
    assert ops.snapshot_owned(lh) == before
    assert second["last_verified_at"] == "2026-10-13T13:00:00Z" and first["last_verified_at"] == "2026-10-13T12:00:00Z"
    stripped = lambda d: {k: v for k, v in d.items() if k != "last_verified_at"}
    assert stripped(first) == stripped(second)
    assert first["installed_at"] == second["installed_at"] and first["updated_at"] == second["updated_at"]
    assert first["previous_version_dir"] == second["previous_version_dir"] is None
    assert {n: e["checked_at"] for n, e in first["prereqs"].items()} == {n: e["checked_at"] for n, e in second["prereqs"].items()}
    assert (lh.claude_settings.read_bytes(), codex_path(lh).read_bytes()) == settings_before
    backups_after = sorted(os.listdir(str(lh.runtime / "backups"))) if (lh.runtime / "backups").exists() else []
    assert backups_after == backups_before


def test_staging_copies_only_the_shipped_payload_files(lifecycle_home, tmp_path):
    payload = tmp_path / "payload-copy"
    import shutil
    shutil.copytree(sd.PAYLOAD_DIR, str(payload), ignore=shutil.ignore_patterns("__pycache__"))
    (payload / "__pycache__").mkdir()
    (payload / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (payload / ".DS_Store").write_bytes(b"junk")
    lh = lifecycle_home
    result = engine.install(ops.make_env(lh, payload_dir=str(payload)))
    version_dir = result.manifest["version_dir"]
    assert not os.path.exists(os.path.join(version_dir, "__pycache__")) and not os.path.exists(os.path.join(version_dir, ".DS_Store"))
    assert all("pyc" not in e["rel"] and "DS_Store" not in e["rel"] for e in result.manifest["files"])
    assert sorted(e["rel"] for e in result.manifest["files"] if e["kind"] == "code") == sorted(
        "versions/%s/%s" % (os.path.basename(version_dir), rel) for rel in sd.payload_files(str(payload)))


def test_entry_points_are_written_through_temp_and_rename(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    calls = []
    real = os.replace

    def spy(src, dst, *a, **kw):
        calls.append((os.fspath(src), os.fspath(dst)))
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    engine.install(ops.make_env(lh))
    shim, terminal = str(lh.runtime / "bin" / "quoin-deck-hook"), str(lh.runtime / "bin" / "quoin-deck-terminal")
    profile = str(lh.runtime / "profile" / "Quoin Live Agents.streamDeckProfile")
    for target in (shim, terminal, profile):
        hits = [src for src, dst in calls if dst == target]
        assert len(hits) == 1 and os.path.dirname(hits[0]) == os.path.dirname(target) and hits[0].endswith(".tmp")
    assert stat.S_IMODE(os.stat(shim).st_mode) == 0o755 and stat.S_IMODE(os.stat(terminal).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(profile).st_mode) == 0o644


# ------------------------------------------------------------------ foreign items

def test_foreign_entries_survive_install_in_place_and_order(lifecycle_home):
    lh = lifecycle_home
    lc.make_settings(lh.claude_settings, foreign=True, core=True)
    lc.write_json(codex_path(lh), {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/codex-notify"}]}]},
                                   "model": "m"})
    before_claude, before_codex = load(lh.claude_settings), load(codex_path(lh))
    engine.install(ops.make_env(lh))

    def foreign_only(data, kind):
        out = {}
        for event, groups in (data.get("hooks") or {}).items():
            kept = []
            for group in groups:
                hooks = [h for h in group["hooks"] if not hooks_merge.classify(h.get("command"), runtime_names=("QuoinStreamDeck",))]
                if hooks:
                    kept.append(dict(group, hooks=hooks))
            if kept:
                out[event] = kept
        return out

    assert foreign_only(load(lh.claude_settings), "claude") == foreign_only(before_claude, "claude")
    assert foreign_only(load(codex_path(lh)), "codex") == foreign_only(before_codex, "codex")
    assert load(lh.claude_settings)["theme"] == "dark" and load(codex_path(lh))["model"] == "m"
    assert owned_total(lh.claude_settings, "claude") == len(hooks_merge.EVENTS["claude"])


# ------------------------------------------------------------------ dormant

def test_missing_core_prerequisite_installs_dormant_then_activates(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, missing=("node",), app_version="7.0")
    result = engine.install(env)
    data = load(lh.runtime / "install-manifest.json")
    assert result.mode == "dormant" and data["mode"] == "dormant" and data["mode_reasons"] == ["node"]
    assert not lh.plugin_dir.exists() and not lh.claude_settings.exists() and not codex_path(lh).exists()
    assert (lh.runtime / "current").is_symlink() and (lh.runtime / "bin" / "quoin-deck-hook").exists()
    assert data["hooks"] == [] and "node" not in data["binaries"]
    assert any("installed DORMANT" in line for line in result.lines)
    report = collect(lh)
    assert report["exit_code"] == 11 and "MODE_DORMANT" in codes(report)
    detail = next(p["detail"] for p in report["problems"] if p["code"] == "MODE_DORMANT")
    assert "node" in detail
    ops.assert_hooks_inert(ops.run_hook_commands(lh))
    engine.install(ops.make_env(lh))
    data = load(lh.runtime / "install-manifest.json")
    assert data["mode"] == "active" and lh.plugin_dir.is_dir()
    assert owned_total(lh.claude_settings, "claude") == len(hooks_merge.EVENTS["claude"])
    assert collect(lh)["exit_code"] == 0


def test_declined_feature_still_installs_active(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, missing=("ghostty",), tty=True, answers=["n"])
    result = engine.install(env)
    data = load(lh.runtime / "install-manifest.json")
    assert result.mode == "active" and data["prereqs"]["ghostty"]["state"] == "declined"
    assert any("Ghostty" in f or "tab navigation" in f for f in data["features_unavailable"]) or data["features_unavailable"]
    report = collect(lh)
    assert report["exit_code"] == 0
    assert "Not installed by choice" in status.render_text(report)
    assert env.fake_ctx.runner_calls == []


def test_non_interactive_all_mode_only_prints_how_lines(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, missing=("zellij", "ghostty"), prereqs="all")
    result = engine.install(env)
    assert env.fake_ctx.runner_calls == [] and result.mode == "active"
    text = "\n".join(env.fake_ctx.lines)
    assert "how: " in text and "non-interactive" in text
    assert load(lh.runtime / "install-manifest.json")["prereqs"]["zellij"]["state"] == "missing"


# ------------------------------------------------------------------ runtimes

def test_codex_not_detected_creates_no_codex_file(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, missing=("codex",))
    result = engine.install(env)
    assert not codex_path(lh).exists() and not lh.codex_home.exists()
    assert "Codex not detected; skipping Codex hooks" in result.lines
    assert {h["runtime"] for h in result.manifest["hooks"]} == {"claude"}


def test_manifest_record_alone_counts_as_codex_detected(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    before = codex_path(lh).read_bytes()
    real = migrations.codex_detected

    def only_manifest(paths, environ, which, codex_data, manifest_data):
        return real(dataclasses.replace(paths, codex_home="/nonexistent/codex"), {}, lambda n: None, {}, manifest_data)

    monkeypatch.setattr(migrations, "codex_detected", only_manifest)
    result = engine.install(ops.make_env(lh, now=ops.LATER, missing=("codex",)))
    assert codex_path(lh).read_bytes() == before
    assert {h["runtime"] for h in result.manifest["hooks"]} == {"claude", "codex"}
    assert "Codex not detected; skipping Codex hooks" not in result.lines


def test_explicit_runtimes_removes_the_omitted_runtimes_entries(lifecycle_home):
    lh = lifecycle_home
    foreign = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/codex-notify"}]}]}}
    lc.write_json(codex_path(lh), copy.deepcopy(foreign))
    engine.install(ops.make_env(lh))
    assert owned_total(codex_path(lh), "codex") == len(hooks_merge.EVENTS["codex"])
    result = engine.install(ops.make_env(lh, runtimes=("claude",), now=ops.LATER))
    assert load(codex_path(lh)) == foreign
    assert {h["runtime"] for h in result.manifest["hooks"]} == {"claude"}
    assert owned_total(lh.claude_settings, "claude") == len(hooks_merge.EVENTS["claude"])


def test_default_runtimes_never_remove_anything(lifecycle_home):
    lh = lifecycle_home
    engine.install(ops.make_env(lh))
    before = codex_path(lh).read_bytes()
    engine.install(ops.make_env(lh, missing=("codex",), now=ops.LATER))
    assert codex_path(lh).read_bytes() == before


# ------------------------------------------------------------------ running Stream Deck and the profile

def test_running_stream_deck_gets_a_restart_note(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, running=True)
    result = engine.install(env)
    assert any("takes effect after you restart Stream Deck" in line for line in result.lines)
    assert env.fake_sys.run_calls == []
    assert any(call[0].endswith("pgrep") for call in env.fake_sys.probe_calls)


def test_open_profile_runs_open_with_the_profile_path(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, open_profile=True)
    engine.install(env)
    assert env.fake_sys.run_calls == [(str(lh.fakebin / "open"), str(lh.runtime / "profile" / "Quoin Live Agents.streamDeckProfile"))]


# ------------------------------------------------------------------ dry run

def test_dry_run_on_empty_home_and_on_an_install_changes_nothing(lifecycle_home):
    lh = lifecycle_home
    env = ops.make_env(lh, dry_run=True)
    before = lc.tree_snapshot(lh.root)
    result = engine.install(env)
    assert lc.tree_snapshot(lh.root) == before and not lh.runtime.exists()
    assert result.lines and all(line.startswith("dry run:") for line in result.lines)
    engine.install(ops.make_env(lh))
    env = ops.make_env(lh, dry_run=True, now=ops.LATER)
    before = lc.tree_snapshot(lh.root)
    result = engine.install(env)
    assert lc.tree_snapshot(lh.root) == before and all(line.startswith("dry run:") for line in result.lines)


# ------------------------------------------------------------------ intent log

def test_marker_draft_matches_the_settings_actually_written_after_a_conflict(lifecycle_home, monkeypatch):
    lh = lifecycle_home
    lc.write_json(lh.claude_settings, {"theme": "dark"})
    real = jsonfile.write_atomic
    seen = {"calls": 0, "draft_at_write": None, "written": None}

    def spying(path, data, **kw):
        if os.path.realpath(os.fspath(path)) == os.path.realpath(str(lh.claude_settings)):
            seen["calls"] += 1
            if seen["calls"] == 1:
                edited = load(lh.claude_settings)
                edited["hooks"] = {"Custom": [{"hooks": [{"type": "command", "command": "/x/custom"}]}]}
                lc.write_json(lh.claude_settings, edited)
                raise jsonfile._Conflict()
            seen["draft_at_write"] = load(lh.runtime / "operation.json")["draft"]["hooks"]
            seen["written"] = copy.deepcopy(data)
        return real(path, data, **kw)

    monkeypatch.setattr(jsonfile, "write_atomic", spying)
    engine.install(ops.make_env(lh, missing=("codex",)))
    assert seen["calls"] == 2
    written = seen["written"]
    created = [e for e in hooks_merge.EVENTS["claude"] if len(written["hooks"][e]) == 1]
    assert {r["event"]: r["created_event_key"] for r in seen["draft_at_write"]} == {e: True for e in hooks_merge.EVENTS["claude"]}
    assert set(created) == set(hooks_merge.EVENTS["claude"]) and "Custom" in written["hooks"]
