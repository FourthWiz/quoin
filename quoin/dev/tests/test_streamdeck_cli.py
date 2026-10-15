"""`quoin streamdeck ...` wiring: help, exit codes, status through the CLI, platform gate."""
import json
import os
import subprocess
import sys

import pytest

import _streamdeck_lifecycle as lc
import _streamdeck_ops as ops
from _streamdeck_ops import (deck_home, forbid_dangerous_subprocess, forbid_real_home_io, forbid_real_seams,  # noqa: F401
                             lifecycle_home, subprocess_calls)
from quoin import cli
from quoin.streamdeck import cli as sdcli, hooks_merge, macos, status

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
LINUX = macos.PlatformInfo("linux", "", "x86_64", False)


@pytest.fixture
def deck(lifecycle_home, monkeypatch):
    """The CLI's three seams point at the recording fakes of an engine env."""
    lh = lifecycle_home

    def build(platform=DARWIN, **kwargs):
        env = ops.make_env(lh, **kwargs)
        monkeypatch.setattr(sdcli, "_ctx_factory", env.ctx_factory)
        monkeypatch.setattr(sdcli, "_sysops", lambda: env.sysops)
        monkeypatch.setattr(sdcli, "_platform", lambda: platform)
        monkeypatch.setattr(sdcli, "_now", lambda: kwargs.get("now", lc.NOW))
        monkeypatch.setenv("ZELLIJ_SOCKET_DIR", env.environ["ZELLIJ_SOCKET_DIR"])
        return env

    build.lh = lh
    return build


def run(argv, capsys):
    rc = cli.main(["streamdeck"] + argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def load(path):
    with open(str(path)) as handle:
        return json.load(handle)


# ------------------------------------------------------------------ help

def test_the_group_and_every_verb_have_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0 and "streamdeck" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli.main(["streamdeck", "--help"])
    group = capsys.readouterr().out
    assert exc.value.code == 0 and all(verb in group for verb in ("install", "upgrade", "status", "uninstall"))
    assert "opt-in" in group and "never installs it" in group
    expected = {"install": ("--runtimes", "--python", "--node", "--prereqs", "--dry-run", "--open-profile", "--no-open-profile",
                            "--allow-downgrade", "--runtime-dir", "--agentdesk"),
                "upgrade": ("--runtimes", "--allow-downgrade", "--plugins-dir"),
                "uninstall": ("--runtimes", "--purge", "--dry-run", "--state-root"),
                "status": ("--json", "--claude-settings", "--codex-home")}
    for verb, flags in expected.items():
        with pytest.raises(SystemExit) as exc:
            cli.main(["streamdeck", verb, "--help"])
        text = capsys.readouterr().out
        assert exc.value.code == 0 and all(flag in text for flag in flags), verb
    with pytest.raises(SystemExit):
        cli.main(["streamdeck", "status", "--help"])
    text = capsys.readouterr().out
    for code in status.PROBLEM_CODES:
        assert code in text
    assert "SETTINGS_UNREADABLE" in text and "12" in text


def test_a_bare_group_prints_help_and_fails(capsys):
    rc = cli.main(["streamdeck"])
    assert rc == 1 and "install" in capsys.readouterr().out


def test_an_unknown_runtime_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["streamdeck", "install", "--runtimes", "claude,vim"])
    assert exc.value.code == 2


def test_importing_the_cli_does_not_import_the_component(tmp_path):
    code = "import sys, quoin.cli; print('quoin.streamdeck' in sys.modules)"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "PYTHONPATH": os.pathsep.join(sys.path)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env,
                         cwd=str(tmp_path))
    assert out.stdout.strip() == "False", out.stderr


# ------------------------------------------------------------------ operations and status

def test_install_then_status_json_is_healthy(deck, capsys):
    lh = deck.lh
    deck()
    rc, out, err = run(["install"], capsys)
    assert rc == 0, err
    assert "Codex" in out and "/hooks" in out
    before = lc.tree_snapshot(lh.root)
    rc, out, err = run(["status", "--json"], capsys)
    report = json.loads(out)
    assert rc == 0 and report["problems"] == [] and report["exit_code"] == 0
    assert lc.tree_snapshot(lh.root) == before


def status_exit(deck, capsys):
    before = lc.tree_snapshot(deck.lh.root)
    rc, out, _ = run(["status", "--json"], capsys)
    assert lc.tree_snapshot(deck.lh.root) == before
    return rc, json.loads(out)


def test_status_through_the_cli_for_each_fixture_class(deck, capsys):
    lh = deck.lh
    deck()
    rc, report = status_exit(deck, capsys)
    assert rc == 10 and report["state"] == "not-installed"
    lc.make_installed(lh)
    rc, report = status_exit(deck, capsys)
    assert rc == 0 and report["problems"] == []
    target = next(lh.runtime.glob("versions/*/monitor.py"))
    original = target.read_text()
    target.write_text(original + "# tampered\n")
    rc, report = status_exit(deck, capsys)
    assert rc == 11 and "FILE_MODIFIED" in {p["code"] for p in report["problems"]}
    target.write_text(original)
    settings = load(lh.claude_settings)
    saved = json.loads(json.dumps(settings))
    settings["hooks"]["Stop"] = [g for g in settings["hooks"]["Stop"]
                                 if not any("quoin-deck-hook" in h["command"] for h in g["hooks"])]
    lc.write_json(lh.claude_settings, settings)
    rc, report = status_exit(deck, capsys)
    assert rc == 11 and "HOOK_MISSING" in {p["code"] for p in report["problems"]}
    duplicated = json.loads(json.dumps(saved))
    duplicated["hooks"]["Stop"] = duplicated["hooks"]["Stop"] * 2
    lc.write_json(lh.claude_settings, duplicated)
    rc, report = status_exit(deck, capsys)
    assert rc == 11 and "HOOK_DUPLICATE" in {p["code"] for p in report["problems"]}
    lc.write_json(lh.claude_settings, saved)
    lc.write_json(lh.runtime / "operation.json", {"op": "install", "phase": "committed", "steps_done": []})
    rc, report = status_exit(deck, capsys)
    assert rc == 12 and "OPERATION_INCOMPLETE" in {p["code"] for p in report["problems"]}


def test_a_platform_other_than_macos_is_refused_without_writes(deck, capsys):
    lh = deck.lh
    deck(platform=LINUX)
    before = lc.tree_snapshot(lh.root)
    for verb in ("install", "upgrade"):
        rc, out, err = run([verb], capsys)
        assert rc == 2 and "macOS only" in err
    rc, out, err = run(["status"], capsys)
    assert rc == 0 and "unsupported platform" in out
    rc, out, err = run(["uninstall"], capsys)
    assert rc == 0 and "macOS only" in out
    assert lc.tree_snapshot(lh.root) == before


def test_malformed_settings_name_the_file_and_exit_one(deck, capsys):
    lh = deck.lh
    deck()
    lh.claude_settings.parent.mkdir(parents=True)
    lh.claude_settings.write_text("{nope")
    rc, out, err = run(["install"], capsys)
    assert rc == 1 and str(lh.claude_settings) in err and lh.claude_settings.read_text() == "{nope"


def test_uninstall_and_dry_run_through_the_cli(deck, capsys):
    lh = deck.lh
    deck()
    assert run(["install"], capsys)[0] == 0
    snapshot = ops.snapshot_owned(lh)
    rc, out, _ = run(["uninstall", "--dry-run"], capsys)
    assert rc == 0 and "dry run" in out and ops.snapshot_owned(lh) == snapshot
    rc, out, _ = run(["uninstall", "--runtimes", "codex"], capsys)
    assert rc == 0 and hooks_merge.owned_commands(load(lh.codex_home / "hooks.json"), "codex",
                                                  runtime_names=("QuoinStreamDeck",)) == []
    rc, out, _ = run(["uninstall"], capsys)
    assert rc == 0
    rc, out, _ = run(["uninstall"], capsys)
    assert rc == 0 and "nothing to remove" in out
