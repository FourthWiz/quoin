"""Tests for `quoin opencode config explain` and `quoin opencode config compile`
driven through `cli.main`, with the real environment replaced and the clock
frozen. Everything is offline."""
from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import MANAGED_STRICT, NOW, PROFILE_PERSONAL, World, fixture
from quoin import cli
from quoin.opencode_adapter import compiler, paths


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture(autouse=True)
def offline_traps(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network or subprocess use is not allowed in this module")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "check_output"),
    ):
        monkeypatch.setattr(owner, name, boom)
    monkeypatch.setattr(cli, "datetime", _Frozen)


def activate(monkeypatch, world):
    monkeypatch.setenv("HOME", str(world.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", world.env["XDG_CONFIG_HOME"])
    monkeypatch.setenv("XDG_STATE_HOME", world.env["XDG_STATE_HOME"])
    if "QUOIN_OPENCODE_MANAGED_POLICY" in world.env:
        monkeypatch.setenv("QUOIN_OPENCODE_MANAGED_POLICY", world.env["QUOIN_OPENCODE_MANAGED_POLICY"])
    else:
        monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)


@pytest.fixture
def work(tmp_path, monkeypatch):
    world = World(tmp_path)
    activate(monkeypatch, world)
    return world


def run(capsys, *argv):
    code = cli.main(["opencode", "config", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def compile_args(world, *extra):
    return ("compile", "--profile", "work", "--project-root", str(world.root), *extra)


def default_dir(world):
    return world.tmp / "state" / "quoin" / "opencode" / "work" / paths.project_key(world.root)


# ------------------------------------------------------------------ explain


def test_explain_text(work, capsys):
    code, out, err = run(capsys, "explain", "--profile", "work", "--project-root", str(work.root))
    assert code == 0 and err == ""
    assert "Configuration explanation" in out and "digest: sha256:" in out
    assert "https://gateway.example.invalid/v1" in out


def test_explain_json_and_redact(work, capsys):
    code, out, err = run(capsys, "explain", "--json", "--redact", "--profile", "work", "--project-root", str(work.root))
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert doc["explain_format"] == 1
    assert "gateway.example.invalid" not in out
    assert doc["output_location"].startswith("$XDG_STATE_HOME/quoin/opencode/work/")


def test_explain_uses_the_project_profile_when_none_is_given(work, capsys):
    code, out, _ = run(capsys, "explain", "--project-root", str(work.root))
    assert code == 0 and "name: work" in out


def test_explain_exits_one_with_blocking_findings(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, project=fixture("valid/project-unclassified.json"))
    activate(monkeypatch, world)
    code, out, err = run(capsys, "explain", "--profile", "work", "--project-root", str(world.root))
    assert code == 1 and "missing-classification" in out and err == ""


def test_explain_missing_profile_is_a_load_error(work, capsys):
    code, out, err = run(capsys, "explain", "--profile", "absent", "--project-root", str(work.root))
    assert code == 2 and out == "" and "profile-not-found" in err


# ------------------------------------------------------------------ compile


def test_compile_then_check_then_stale(work, capsys):
    code, out, err = run(capsys, *compile_args(work))
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == "compiled: %s" % (default_dir(work) / "opencode.json")
    assert lines[1].startswith("digest: sha256:") and lines[2] == "launchable: true"
    assert (default_dir(work) / "quoin-compile.json").is_file()
    assert run(capsys, *compile_args(work, "--check")) == (0, "up to date\n", "")
    work.profile["providers"]["corp-gw"]["name"] = "Renamed"
    work.write()
    code, out, _ = run(capsys, *compile_args(work, "--check"))
    assert code == 1 and out == "stale: stale\n"


def test_compile_with_output_directory(work, capsys):
    target = work.tmp / "chosen"
    code, out, _ = run(capsys, *compile_args(work, "--output", str(target)))
    assert code == 0 and (target / "opencode.json").is_file()
    assert run(capsys, *compile_args(work, "--output", str(target), "--check"))[0] == 0


def test_compile_blocked_prints_findings_and_writes_nothing(work, capsys):
    (work.root / ".opencode" / "agents" / "quoin-critic.md").unlink()
    code, out, err = run(capsys, *compile_args(work))
    assert code == 1 and out == ""
    assert "agent-file-missing:" in err and "[critic]" in err
    assert not default_dir(work).exists()


def test_check_when_blocked_exits_one(work, capsys):
    run(capsys, *compile_args(work))
    (work.root / ".opencode" / "agents" / "quoin-critic.md").unlink()
    code, out, err = run(capsys, *compile_args(work, "--check"))
    assert code == 1 and "agent-file-missing" in err


def test_compile_requires_a_profile(work, capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["opencode", "config", "compile", "--project-root", str(work.root)])
    assert info.value.code == 2


def test_allow_unqualified_is_refused_for_work_under_a_managed_policy(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, managed=MANAGED_STRICT)
    activate(monkeypatch, world)
    code, out, err = run(capsys, *compile_args(world, "--allow-unqualified"))
    assert code == 2 and out == ""
    assert "managed policy" in err
    assert not default_dir(world).exists()


def test_allow_unqualified_compiles_with_launchable_false(tmp_path, monkeypatch, capsys):
    world = World(tmp_path)
    world.write_records(now=NOW - timedelta(days=40))
    activate(monkeypatch, world)
    assert run(capsys, *compile_args(world))[0] == 1
    code, out, _ = run(capsys, *compile_args(world, "--allow-unqualified"))
    assert code == 0 and out.splitlines()[2] == "launchable: false"
    assert run(capsys, *compile_args(world, "--allow-unqualified", "--check"))[0] == 0
    code, out, err = run(capsys, *compile_args(world, "--check"))
    assert code == 1 and out == "stale: flag-mismatch\n"
    assert "blocked" not in err


def test_output_inside_the_project_is_refused(work, capsys):
    code, out, err = run(capsys, *compile_args(work, "--output", str(work.root / "out")))
    assert code == 2 and out == "" and "inside the project" in err and str(work.root) not in err
    assert not (work.root / "out").exists()


def test_output_naming_a_file_is_refused(work, capsys):
    code, _, err = run(capsys, *compile_args(work, "--output", str(work.tmp / "new" / "opencode.json")))
    assert code == 2 and "names a directory" in err
    assert not (work.tmp / "new").exists()


def test_loose_output_directory_is_refused(work, capsys):
    target = work.tmp / "loose"
    target.mkdir()
    os.chmod(target, 0o770)
    code, _, err = run(capsys, *compile_args(work, "--output", str(target)))
    assert code == 2 and "not private enough" in err
    assert list(target.iterdir()) == []


def test_invalid_profile_prints_one_error_per_block(work, capsys):
    profile_file = paths.profile_path("work", work.env, work.home)
    profile_file.write_text('{"schema_version": 1, "runtime": "opencode", "profile": "work"', encoding="utf-8")
    code, out, err = run(capsys, *compile_args(work))
    assert code == 2 and out == "" and "invalid-json" in err


def test_missing_adapter_data_gets_the_reinstall_text(work, capsys, monkeypatch):
    monkeypatch.setattr(paths, "adapter_data_dir", lambda: None)
    for argv in (compile_args(work), ("explain", "--profile", "work", "--project-root", str(work.root))):
        code, out, err = run(capsys, *argv)
        assert code == 2 and out == ""
        assert err == "quoin: packaged adapter data not found; reinstall quoin\n"


# ---------------------------------------------------------------- dispatch


def test_config_group_without_a_subcommand_prints_help(capsys):
    assert cli.main(["opencode", "config"]) == 1
    assert "explain" in capsys.readouterr().out


def test_unknown_config_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["opencode", "config", "nope"])
    assert info.value.code == 2


def test_opencode_group_help_still_lists_the_older_commands(capsys):
    assert cli.main(["opencode"]) == 1
    out = capsys.readouterr().out
    for name in ("uninstall", "script", "config"):
        assert name in out


# ------------------------------------------------------------ environment


def test_only_the_configured_environment_names_reach_the_pipeline(work, monkeypatch, capsys):
    monkeypatch.setenv("QUOIN_CORP_GW_API_KEY", helpers.SEEDED_SECRET)
    monkeypatch.setenv("QUOIN_CORP_GW_B_API_KEY", helpers.SEEDED_SECRET)
    monkeypatch.setenv("OPENROUTER_API_KEY", helpers.SEEDED_SECRET)
    seen = []
    real = compiler.evaluate

    def spy(**kw):
        seen.append(dict(kw["env"]))
        return real(**kw)

    monkeypatch.setattr(compiler, "evaluate", spy)
    assert run(capsys, "explain", "--profile", "work", "--project-root", str(work.root))[0] == 0
    assert run(capsys, *compile_args(work))[0] == 0
    assert len(seen) == 2
    for env in seen:
        assert set(env) == {k for k in cli.CONFIG_ENV_KEYS if k in os.environ}
        assert set(env) <= set(cli.CONFIG_ENV_KEYS)
        assert not [k for k in env if k.endswith("_API_KEY")]
        assert helpers.SEEDED_SECRET not in json.dumps(env)
    assert cli.CONFIG_ENV_KEYS == ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "QUOIN_OPENCODE_MANAGED_POLICY")


def test_personal_profile_compiles(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, profile=PROFILE_PERSONAL)
    activate(monkeypatch, world)
    code, out, _ = run(
        capsys, "compile", "--profile", "personal", "--project-root", str(world.root)
    )
    assert code == 0 and out.splitlines()[2] == "launchable: true"


# --------------------------------------------- invalid fixtures, no leakage

from test_opencode_config_errors import SHAPES  # noqa: E402
from test_opencode_runtime_config import CASES, run_case  # noqa: E402


@pytest.mark.parametrize("case", CASES["invalid"], ids=[c["id"] for c in CASES["invalid"]])
def test_invalid_fixtures_through_compile_print_no_secret(case, tmp_path, monkeypatch, capsys):
    env, _ = run_case(case, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    if "QUOIN_OPENCODE_MANAGED_POLICY" in env:
        monkeypatch.setenv("QUOIN_OPENCODE_MANAGED_POLICY", env["QUOIN_OPENCODE_MANAGED_POLICY"])
    else:
        monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
    for name in ("QUOIN_CORP_GW_API_KEY", "QUOIN_CORP_GW_B_API_KEY", "QUOIN_LOCAL_GW_API_KEY",
                 "QUOIN_OPENROUTER_API_KEY", "LOCAL_GW_TOKEN", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, helpers.SEEDED_SECRET)
    profile = case.get("select") or case.get("install_as") or "work"
    code = cli.main(
        ["opencode", "config", "compile", "--profile", profile, "--project-root", str(tmp_path / "project")]
    )
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code in (1, 2), case["id"]
    for form in helpers.secret_forms(helpers.SEEDED_SECRET) + list(SHAPES.values()):
        assert form not in text, case["id"]
    assert not (tmp_path / "state").exists()
