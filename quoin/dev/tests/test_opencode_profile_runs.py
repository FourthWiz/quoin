"""Two profiles in one world, run through the real driver and a fake opencode:
storage roots, credentials, permissions, continuation and nested launches."""
from __future__ import annotations

import json
import os
import sys

import pytest

from _opencode_merge_helpers import PROFILE_PERSONAL, _install, write_probe_records
from _opencode_run_helpers import InstalledProject
from quoin.opencode_adapter import config, driver, merge, paths, phase_loop, profile_dirs, runstore
from fakes import fake_opencode as fake

SEEDED = "sk-FAKEECHOSECRET0123456789"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

DUMP = (
    "import json, os, sys; "
    "keys = ['XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'TMPDIR', 'QUOIN_OPENCODE_STATE_DIR', "
    "'OPENCODE_CONFIG', 'OPENCODE_PURE', 'OPENCODE_DISABLE_SHARE', 'npm_config_offline', 'npm_config_cache']; "
    "d = {k: os.environ.get(k) for k in keys}; "
    "d['_env'] = dict(os.environ); "
    "open(sys.argv[1], 'a').write(json.dumps(d) + '\\n')"
)


MK = (
    "import os, sys; "
    "[os.makedirs(os.path.join(os.environ[k], 'opencode', 'sub'), exist_ok=True) or "
    "open(os.path.join(os.environ[k], 'opencode', 'sub', 'f.txt'), 'w').write('x') "
    "for k in ('XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'TMPDIR')]"
)


def dump_scenario(out, *extra):
    steps = [
        fake._step_start("prt_s1"),
        {"do": "run_cmd", "argv": [sys.executable, "-c", DUMP, str(out)]},
        *extra,
        fake._step_finish("prt_f1", "stop"),
        {"do": "exit", "code": 0},
    ]
    return fake._scenario(*steps)


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in (paths.ENV_STATE_DIR, "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    holder = {}

    def make(scenario_builder):
        out = tmp_path / "env-dump.jsonl"
        project = InstalledProject(tmp_path, monkeypatch, scenario_builder(out))
        w = project.world
        # A personal-classified project accepts both profiles.
        (project.root / ".quoin" / "runtime.json").write_text(json.dumps(
            {"schema_version": 1, "runtime": "opencode", "profile": "work", "classification": "personal"}))
        _install(w.env, w.home, PROFILE_PERSONAL)
        for name in ("work", "personal"):
            loaded = config.load_all(project_root=project.root, profile=name, env=w.env, home=w.home)
            write_probe_records(w.env, w.home, merge.merge(loaded))
        holder["project"] = project
        project.dump = out
        project.state = paths.state_dir(w.env, w.home)
        return project

    yield make
    if "project" in holder:
        holder["project"].cleanup()


def request(project, profile, task="demo", **kw):
    return driver.RunRequest(project_root=project.root, task=task, stage=None, phase="plan", profile=profile, **kw)


def dumps(project):
    return [json.loads(line) for line in project.dump.read_text().splitlines()]


def run(project, profile, task="demo", drv=None, **kw):
    drv = drv or project.driver_factory()(project.root)
    return phase_loop.run_phase(
        drv, request(project, profile, task=task), max_relaunch=kw.pop("max_relaunch", 0),
        cancel=phase_loop.CancelToken(), backoff_fn=lambda n: 0, **kw,
    )


def under(path, root):
    return os.path.realpath(path) == os.path.realpath(root) or os.path.realpath(root) + os.sep in os.path.realpath(path) + os.sep


STORAGE = ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "TMPDIR")


def test_each_profile_gets_its_own_roots_and_sees_nothing_of_the_other(world):
    project = world(dump_scenario)
    assert run(project, "work", task="one").outcome == "COMPLETED"
    assert run(project, "personal", task="two").outcome == "COMPLETED"
    work, personal = dumps(project)
    work_root, personal_root = project.state / "work", project.state / "personal"
    assert str(work_root) != str(personal_root) and not under(work_root, personal_root)
    for entry, mine, other in ((work, work_root, personal_root), (personal, personal_root, work_root)):
        assert entry["QUOIN_OPENCODE_STATE_DIR"] == str(mine)
        for name in STORAGE:
            assert under(entry[name], mine) and not under(entry[name], other)
        blob = json.dumps(entry["_env"])
        assert str(other) not in blob
        assert "OPENCODE_AUTH_CONTENT" not in entry["_env"]
        assert "QUOIN_OPENCODE_MANAGED_POLICY" not in entry["_env"]
    assert work["OPENCODE_PURE"] == "1" and work["npm_config_offline"] == "true"
    assert personal["OPENCODE_PURE"] is None and personal["npm_config_offline"] is None
    assert "QUOIN_CORP_GW_API_KEY" in work["_env"] and "QUOIN_OPENROUTER_API_KEY" not in work["_env"]
    assert "QUOIN_OPENROUTER_API_KEY" in personal["_env"] and "QUOIN_CORP_GW_API_KEY" not in personal["_env"]


def test_the_work_profile_prepares_only_its_gateway_and_records_static_keys(world):
    project = world(dump_scenario)
    result = run(project, "work")
    record = project.record(result.run_id)
    prepared = record["prepared"]
    assert prepared["static_key_in_use"] is True
    assert prepared["profile_root"].startswith("~") or prepared["profile_root"].endswith("work")
    assert all(item["kind"] == "static-api-key" for item in prepared["credentials"])
    assert SEEDED not in json.dumps(record)
    document = json.loads(
        (project.state / "work" / paths.project_key(project.root) / "opencode.json").read_text())
    assert document["enabled_providers"] and "openrouter" not in document["enabled_providers"]


def test_a_continuation_in_the_same_profile_keeps_the_same_roots(world):
    def builder(out):
        dump = {"do": "run_cmd", "argv": [sys.executable, "-c", DUMP, str(out)]}
        first = [fake._step_start("prt_s1"), dump, fake._step_finish("prt_f1", "tool-calls"),
                 {"do": "crash", "signal": "SIGKILL"}]
        second = [fake._step_start("prt_s2"), dump, fake._step_finish("prt_f2", "stop"),
                  {"do": "exit", "code": 0}]
        return {"attempts": [{"steps": first}, {"steps": second}], "require_session_after_first": True}

    project = world(builder)
    result = run(project, "work", max_relaunch=2)
    assert result.outcome == "COMPLETED" and result.attempts == 2
    first, second = dumps(project)
    assert {k: first[k] for k in STORAGE} == {k: second[k] for k in STORAGE}


def test_a_cross_profile_request_starts_a_new_run_in_its_own_roots(world):
    def builder(out):
        dump = {"do": "run_cmd", "argv": [sys.executable, "-c", DUMP, str(out)]}
        first = [fake._step_start("prt_s1"), dump, fake._step_finish("prt_f1", "tool-calls"),
                 {"do": "crash", "signal": "SIGKILL"}]
        second = [fake._step_start("prt_s2"), dump, fake._step_finish("prt_f2", "stop"),
                  {"do": "exit", "code": 0}]
        return {"attempts": [{"steps": first}, {"steps": second}]}

    project = world(builder)
    interrupted = run(project, "work", max_relaunch=0)
    assert interrupted.outcome == "INTERRUPTED"
    stored = project.record(interrupted.run_id)
    assert stored["state"] == "interrupted"
    assert phase_loop._same_request(stored, request(project, "work"))
    assert not phase_loop._same_request(stored, request(project, "personal"))
    result = run(project, "personal", new_run=False)
    assert result.outcome == "COMPLETED" and result.run_id != interrupted.run_id
    after = project.record(interrupted.run_id)
    assert after["state"] == "interrupted" and not after.get("refusal")
    work, personal = dumps(project)
    for name in STORAGE:
        assert under(personal[name], project.state / "personal")
        assert not under(personal[name], project.state / "work")
    modes = [a.get("resume_mode") for a in project.record(result.run_id)["attempts"]]
    assert set(modes) <= {"fresh", "new", None}


def test_everything_a_child_writes_under_its_roots_is_private(world):
    def builder(out):
        return fake._scenario(
            fake._step_start("prt_s1"),
            {"do": "run_cmd", "argv": [sys.executable, "-c", MK]},
            fake._step_finish("prt_f1", "stop"), {"do": "exit", "code": 0})

    project = world(builder)
    assert run(project, "work").outcome == "COMPLETED"
    assert run(project, "personal", task="two").outcome == "COMPLETED"
    for profile in ("work", "personal"):
        root = project.state / profile
        count = 0
        for current, dirs, files in os.walk(root):
            for name in dirs:
                assert os.lstat(os.path.join(current, name)).st_mode & 0o777 == 0o700, os.path.join(current, name)
            for name in files:
                assert os.lstat(os.path.join(current, name)).st_mode & 0o777 == 0o600, os.path.join(current, name)
                count += 1
        assert count >= 4


def test_a_nested_launch_in_the_same_profile_reads_the_parent_directories(world):
    project = world(dump_scenario)
    assert run(project, "work", task="parent").outcome == "COMPLETED"
    env = dict(dumps(project)[0]["_env"])
    compiled = project.state / "work" / paths.project_key(project.root)
    factory_env = dict(env, PATH=os.environ.get("PATH", ""))
    drv = project.driver_factory()(project.root)
    drv._env = factory_env
    drv._session = profile_dirs.LaunchSession.from_launcher_env(factory_env)
    assert drv.state_root == project.state
    result = run(project, "work", task="nested-check", drv=drv)
    assert result.outcome == "COMPLETED"
    nested_env = dumps(project)[-1]["_env"]
    assert nested_env["OPENCODE_CONFIG"] == str(compiled / "opencode.json")
    assert not (project.state / "work" / "state" / "quoin").exists()


def test_a_nested_launch_naming_the_other_profile_is_refused(world):
    project = world(dump_scenario)
    assert run(project, "work", task="parent").outcome == "COMPLETED"
    env = dict(dumps(project)[0]["_env"], PATH=os.environ.get("PATH", ""))
    drv = project.driver_factory()(project.root)
    drv._env = env
    drv._session = profile_dirs.LaunchSession.from_launcher_env(env)
    result = run(project, "personal", task="nested-check", drv=drv)
    assert result.outcome == "REFUSED" and result.refusal["code"] == "profile-switch-in-process"


def test_the_single_run_hook_receives_the_profile_root(world, monkeypatch, capsys):
    from quoin import cli
    from quoin.opencode_adapter import run_hooks

    project = world(dump_scenario)
    seen = {}
    real = run_hooks.after_phase_run

    def capture(*args, **kwargs):
        seen["state_root"] = kwargs.get("state_root")
        return real(*args, **kwargs)

    monkeypatch.setattr(run_hooks, "after_phase_run", capture)
    monkeypatch.setattr(cli, "_make_opencode_driver", project.driver_factory())
    monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
    code = cli.main(["run", "demo", "--runtime", "opencode", "--profile", "work", "--phase", "plan",
                     "--project-root", str(project.root)])
    capsys.readouterr()
    assert code == 0
    assert seen["state_root"] == project.state / "work"
