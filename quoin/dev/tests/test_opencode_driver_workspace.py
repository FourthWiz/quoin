"""Driver workspaces and the non-interactive marker: prepare checks against
the workspace, argv, request keys, resume matching and the child state root."""
from __future__ import annotations

import io
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

import _opencode_driver_helpers as h
import _opencode_helpers as helpers
from _opencode_merge_helpers import NOW, World
from _opencode_run_helpers import InstalledProject
from quoin.opencode_adapter import driver, install, launch_env, paths, runstore
from quoin.opencode_adapter import secrets as credential_refs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")


class _Resolver:
    def resolve(self, ref):
        return credential_refs.SecretValue("sk-test-0000000000")


def _trap(*args, **kwargs):
    raise AssertionError("prepare must never spawn a process")


class Setup:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.world = World(tmp_path, agents=False)
        self.root = self.world.root
        (self.root / ".git").mkdir(exist_ok=True)
        assert install.run_install(str(self.root), helpers.SOURCE_DIR, None, False, io.StringIO(), io.StringIO()) == 0
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: self.world.home))
        scenario = h.fake.write_scenario(tmp_path / "s.json", h.fake.SCENARIOS["record_only"]())
        self.shim = h.fake.write_shim(tmp_path / "bin", scenario, tmp_path / "fake-state")
        monkeypatch.setattr(
            driver, "subprocess",
            types.SimpleNamespace(
                Popen=_trap, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
                TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError,
            ),
        )

    def make_workspace(self, name="ws", git=True, installed=True):
        ws = self.tmp / name
        ws.mkdir()
        if git:
            (ws / ".git").mkdir()
        if installed:
            assert install.run_install(str(ws), helpers.SOURCE_DIR, None, False, io.StringIO(), io.StringIO()) == 0
        return ws

    def driver(self):
        return driver.OpenCodeDriver(
            self.root,
            env=dict(self.world.env, PATH=os.environ.get("PATH", "")),
            home=self.world.home, which=lambda name: str(self.shim),
            version_runner=lambda path: "1.18.32",
            resolver_factory=lambda environ, platform: _Resolver(), clock=lambda: NOW.timestamp(),
        )

    def request(self, **overrides):
        kw = dict(project_root=self.root, task="demo", stage=None, phase="plan", profile="work")
        kw.update(overrides)
        return driver.RunRequest(**kw)

    def refused(self, code, request, **kw):
        with pytest.raises(driver.PrepareRefused) as info:
            self.driver().prepare(request, **kw)
        assert info.value.code == code, info.value.message
        return info.value


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def _plain_prepared(env):
    return env.driver().prepare(env.request())


def test_workspace_sets_cwd_and_keeps_bookkeeping_on_the_root(env):
    ws = env.make_workspace()
    prepared = env.driver().prepare(env.request(workspace=ws))
    assert prepared.cwd == ws
    directory = runstore.store_dir(env.root)
    record = runstore.load_record(directory, prepared.run_id)
    assert record["prepared"]["cwd"] == str(ws) and record["prepared"]["workspace"] == str(ws)
    assert record["prepared"]["non_interactive"] is False
    assert record["request"]["workspace"] == str(ws)
    assert runstore.load_pointer(directory, "demo")["run_id"] == prepared.run_id
    plain = env.driver().prepare(env.request())
    assert plain.input_hashes == prepared.input_hashes
    assert [dict(r) for r in plain.repo_revisions] == [dict(r) for r in prepared.repo_revisions]


def test_owned_file_drift_in_the_workspace_refuses(env):
    ws = env.make_workspace()
    command = ws / ".opencode" / "commands" / "quoin-plan.md"
    command.write_text(command.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
    env.refused("owned-file-drift", env.request(workspace=ws))


def test_missing_command_file_in_the_workspace_refuses(env):
    ws = env.make_workspace(installed=False)
    env.refused("workspace-not-installed", env.request(workspace=ws))


def test_workspace_config_layer_with_permission_refuses(env):
    ws = env.make_workspace()
    (ws / "opencode.json").write_text(json.dumps({"permission": {"bash": "allow"}}), encoding="utf-8")
    err = env.refused("protected-key-overridden", env.request(workspace=ws))
    assert err.category == "policy-denial"


def test_workspace_without_its_own_git_root_refuses(env):
    ws = env.make_workspace(git=False)
    env.refused("workspace-not-isolated", env.request(workspace=ws))


def test_workspace_inside_the_project_or_equal_to_it_refuses(env):
    inside = env.root / "nested"
    inside.mkdir()
    env.refused("workspace-invalid", env.request(workspace=inside))
    env.refused("workspace-invalid", env.request(workspace=env.root))


def test_relative_and_symlinked_workspaces_refuse(env, tmp_path):
    env.refused("workspace-invalid", env.request(workspace=Path("ws")))
    ws = env.make_workspace()
    link = tmp_path / "link"
    link.symlink_to(ws)
    env.refused("workspace-invalid", env.request(workspace=link))
    env.refused("workspace-invalid", env.request(workspace=tmp_path / "absent"))


def test_marker_only_when_non_interactive(env):
    plain = env.driver().prepare(env.request())
    assert plain.argv[1:] == ("run", "--format", "json", "--command", "quoin-plan", "--", "demo")
    marked = env.driver().prepare(env.request(non_interactive=True))
    assert marked.argv[-1] == "demo" + driver.NON_INTERACTIVE_MARKER
    staged = env.driver().prepare(env.request(non_interactive=True, stage="2"))
    assert staged.argv[-1] == "stage 2 of demo (non-interactive run)"
    record = runstore.load_record(runstore.store_dir(env.root), marked.run_id)
    assert record["request"]["non_interactive"] is True and record["prepared"]["non_interactive"] is True


def test_an_old_record_without_the_new_keys_resumes_under_a_default_request(env):
    prepared = _plain_prepared(env)
    directory = runstore.store_dir(env.root)
    record = runstore.load_record(directory, prepared.run_id)
    del record["request"]["workspace"], record["request"]["non_interactive"]
    runstore.write_record(directory, record)
    again = env.driver().prepare(env.request(), resume_run_id=prepared.run_id)
    assert again.run_id == prepared.run_id


def test_workspace_and_root_runs_do_not_resume_each_other(env):
    ws = env.make_workspace()
    in_root = _plain_prepared(env)
    env.refused("resume-request-mismatch", env.request(workspace=ws), resume_run_id=in_root.run_id)
    in_ws = env.driver().prepare(env.request(workspace=ws))
    env.refused("resume-request-mismatch", env.request(), resume_run_id=in_ws.run_id)
    env.refused("resume-request-mismatch", env.request(workspace=ws, non_interactive=True), resume_run_id=in_ws.run_id)
    assert env.driver().prepare(env.request(workspace=ws), resume_run_id=in_ws.run_id).run_id == in_ws.run_id


def test_resume_of_a_run_whose_workspace_is_gone_refuses(env):
    ws = env.make_workspace()
    prepared = env.driver().prepare(env.request(workspace=ws))
    for child in sorted(ws.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    ws.rmdir()
    env.refused("workspace-missing", env.request(workspace=ws), resume_run_id=prepared.run_id)


def test_state_root_and_the_child_state_variable(env):
    drv = env.driver()
    assert drv.state_root == paths.state_dir(env.world.env, env.world.home)
    prepared = drv.prepare(env.request())
    assert paths.ENV_STATE_DIR in prepared.env_names
    assert "XDG_STATE_HOME" not in prepared.env_names
    assert prepared.launch_env.materialize()[paths.ENV_STATE_DIR] == str(drv.state_root)


def test_build_env_state_dir_keyword(tmp_path):
    def build(**kw):
        return launch_env.build_env(
            ambient={"PATH": "/bin"}, compile_sidecar={}, providers=[], resolver=None,
            data_dir=tmp_path / "d", config_path=tmp_path / "c.json", **kw,
        ).materialize()

    assert build(state_dir=tmp_path / "s")[paths.ENV_STATE_DIR] == str(tmp_path / "s")
    assert paths.ENV_STATE_DIR not in build()
    assert set(launch_env.LAUNCHER_ENV_NAMES) == {"OPENCODE_CONFIG", "XDG_DATA_HOME", paths.ENV_STATE_DIR}
    assert launch_env.allowed_ambient("PATH") and launch_env.allowed_ambient("LC_ALL")
    assert not launch_env.allowed_ambient("XDG_STATE_HOME")


def test_launcher_state_dir_reader():
    assert paths.launcher_state_dir({paths.ENV_STATE_DIR: "/a/b"}) == Path("/a/b")
    assert paths.launcher_state_dir({paths.ENV_STATE_DIR: "rel"}) is None
    assert paths.launcher_state_dir({}) is None


def test_the_fake_sees_the_state_variable_and_no_operator_state_home(tmp_path, monkeypatch):
    project = InstalledProject(tmp_path, monkeypatch, "record_only")
    try:
        drv = project.driver_factory()(project.root)
        prepared = drv.prepare(
            driver.RunRequest(project_root=project.root, task="demo", stage=None, phase="plan", profile="work")
        )
        h.run_to_end(drv, prepared)
        names = project.invocations()[-1]["env_names"]
        assert paths.ENV_STATE_DIR in names and "XDG_STATE_HOME" not in names
    finally:
        project.cleanup()
