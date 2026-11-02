"""What a purge leaves behind for the next command, and start ordering."""
from __future__ import annotations

import json
import os

import pytest

from quoin import cli
from quoin.opencode_adapter import profile_dirs, retention, runstore

from _opencode_run_helpers import InstalledProject

REPLAY = ("replay", {"fixture": "plain-complete.jsonl"})

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    project = InstalledProject(tmp_path, monkeypatch, REPLAY)
    monkeypatch.setattr(cli, "_make_opencode_driver", project.driver_factory())
    monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
    for key, value in project.world.env.items():
        monkeypatch.setenv(key, value)
    project.capsys = capsys
    project.mp = monkeypatch
    yield project
    project.cleanup()


def main(project, *argv):
    code = cli.main(list(argv))
    captured = project.capsys.readouterr()
    return code, captured.out, captured.err


def run_phase(project):
    return main(project, "run", "demo", "--runtime", "opencode", "--profile", "work", "--phase", "plan",
                "--project-root", str(project.root), "--halt-on-abort")


def status_json(project):
    code, out, _err = main(project, "opencode", "status", "--task", "demo", "--project-root", str(project.root), "--json")
    return code, json.loads(out)


def purge(project):
    code, out, _err = main(project, "opencode", "purge", "--profile", "work", "--project-root", str(project.root),
                           "--yes", "--json")
    return code, json.loads(out)


def test_status_reports_a_purged_run_as_missing_without_error(world):
    assert run_phase(world)[0] == 0
    code, report = status_json(world)
    assert code == 0 and report["state"] == "completed" and report["run_id"]
    assert purge(world)[1]["outcome"] == "purged"
    code, report = status_json(world)
    assert code == 0
    assert report["state"] == "none" and report["display_state"] == "no run" and report["run_id"] is None


def test_a_launch_after_a_purge_is_not_refused_by_the_profile_stamp(world):
    assert run_phase(world)[0] == 0
    assert purge(world)[1]["outcome"] == "purged"
    code, out, err = run_phase(world)
    summary = json.loads(out.strip().splitlines()[-1])
    assert code == 0 and summary["outcome"] == "COMPLETED" and summary["refusal"] is None, err
    assert "profile-stamp-mismatch" not in out + err
    adapter = profile_dirs.adapter_root(world.world.env, world.world.home)
    root = profile_dirs.profile_root(adapter, "work")
    assert (root / "profile.json").is_file()


# ---- a continuation whose run was purged --------------------------------------------

import _opencode_handoff_helpers as hh  # noqa: E402
from quoin.opencode_adapter import handoff  # noqa: E402

BASE_POLICY = {
    "classification": "work", "sharing": "deny", "external_writes": "deny", "isolation": "managed",
    "cross_profile_fallback": False, "allowed_providers": [], "denied_providers": [],
    "allowed_hosts": [], "denied_hosts": [], "fallback": "stop", "web_tools": "deny",
    "package_install": "deny", "mcp.local": "deny", "mcp.remote": "deny", "mcp.allowed": [],
    "lsp.mode": "deny", "lsp.allowed": [], "formatter.mode": "deny", "formatter.allowed": [],
    "network.identity_check": "required", "network.allowed_addresses": ["a"], "network.pins": ["p"],
    "repositories.allowed_roots": ["~/r"], "logging.content": "metadata",
    "retention.runs_days": 30, "retention.cache_days": 30, "retention.logs_days": 30,
    "retention.crash_days": 30, "retention.exports_days": 30,
}


def test_a_continuation_of_a_purged_run_refuses_as_unverifiable(tmp_path, monkeypatch, capsys):
    project = hh.Project(tmp_path / "hh", monkeypatch, passed=False)
    project.seed_gate("architect", None, "PASS")
    project.record("plan", stage=1)
    project.seed_gate("plan", 1, "PASS")
    (item,) = [c for c in project.build()["completed"] if c["run_id"]]
    directory = project.directory()
    record = runstore.load_record(directory, item["run_id"])
    record["request"]["profile"] = "work"
    record.setdefault("prepared", {})["effective_config"] = {"profile": "work", "policy": BASE_POLICY}
    runstore.write_record(directory, record)

    adapter = tmp_path / "adapter"
    adapter.mkdir(mode=0o700)
    root = adapter / "work"
    root.mkdir(mode=0o700)
    dirs = profile_dirs.ProfileDirs.at(root)
    for sub in ("data", "state", "cache", "tmp"):
        (root / sub).mkdir(mode=0o700)
    (root / "profile.json").write_text("{}")
    profile_dirs.record_project(dirs, project.root)
    monkeypatch.setattr(profile_dirs, "adapter_root", lambda env, home: adapter)

    handoff.write(project.root, project.task, project.build(), source_dir=project.source)
    scope = hh.fixed_scope()

    def load():
        return handoff.load_for_continuation(
            project.root, project.task, source_dir=project.source, requested_scope=scope,
            requested_policy=BASE_POLICY, policy_profile="work",
        )

    assert load()["task"] == project.task
    code = cli.main(["opencode", "purge", "--profile", "work", "--project-root", str(project.root), "--yes", "--json"])
    body = json.loads(capsys.readouterr().out)
    assert code == 0 and body["outcome"] == "purged"
    assert runstore.load_record(directory, item["run_id"]) is None
    with pytest.raises(handoff.HandoffRefused) as info:
        load()
    assert info.value.code == "scope-unverifiable"


# ---- start ordering ---------------------------------------------------------------------


@pytest.fixture
def start_world(world):
    world.launches = []
    world.mp.setattr(cli, "_opencode_tui_launcher", lambda argv, cwd, env: world.launches.append(argv) or 0)
    world.sweeps = []
    real = retention.sweep

    def spy(**kw):
        dirs = kw["dirs"]
        world.sweeps.append({
            "records_at_sweep": sorted(p.name for p in dirs.records.glob("start-*.json")),
            "launched_yet": bool(world.launches),
        })
        return real(**kw)

    world.mp.setattr(retention, "sweep", spy)
    return world


def start(project, *extra):
    return main(project, "opencode", "start", "--profile", "work", "--project-root", str(project.root), *extra)


def test_a_dry_run_start_never_sweeps(start_world):
    assert start(start_world, "--dry-run")[0] == 0
    assert start_world.sweeps == [] and start_world.launches == []


def test_a_real_start_sweeps_after_the_start_record_is_written_and_before_the_tui(start_world):
    assert start(start_world)[0] == 0
    assert len(start_world.sweeps) == 1
    assert start_world.sweeps[0]["records_at_sweep"], "the start record must exist before the sweep"
    assert start_world.sweeps[0]["launched_yet"] is False
    assert len(start_world.launches) == 1


def test_a_start_without_a_project_store_still_sweeps_the_profile_classes(start_world):
    adapter = profile_dirs.adapter_root(start_world.world.env, start_world.world.home)
    dirs = profile_dirs.ProfileDirs.at(profile_dirs.profile_root(adapter, "work"))
    old = dirs.exports / "old-export"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("x")
    long_ago = 1_000_000_000
    os.utime(str(old), (long_ago, long_ago))
    assert runstore.inspect_store(start_world.root) is None
    assert start(start_world)[0] == 0
    assert len(start_world.sweeps) == 1
    assert not old.exists()
    assert runstore.inspect_store(start_world.root) is None
