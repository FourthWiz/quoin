"""Profile storage, hardening flags, containment and credential kinds in the launch environment."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from test_opencode_driver_workspace import Setup
from quoin.opencode_adapter import driver, launch_codes, launch_env, paths, profile_dirs, runstore
from quoin.opencode_adapter import secrets as credential_refs
from quoin.opencode_adapter.launch_env import LaunchRefused
from quoin.opencode_adapter.merge import ProviderView

SECRET = "sk-test-SEEDED-SECRET-0000"


class _Resolver:
    def __init__(self, table):
        self.table = table

    def resolve(self, ref):
        return credential_refs.SecretValue(self.table[ref])


def _view(ref="env:PROV_KEY", pid="prov"):
    return ProviderView(
        id=pid, kind="openai-compatible", endpoint_family="x", host_key="h",
        credential_env="PROV_KEY", use_env_proxy=False, credential_ref=ref,
    )


SIDECAR = {"credential_env": {"PROV_KEY": "prov"}}


def _build(tmp_path, ambient=None, adapter=None, profile=None, **extra):
    return launch_env.build_env(
        ambient=ambient or {"PATH": "/bin", "HOME": "/h"},
        compile_sidecar=SIDECAR, providers=[_view()],
        resolver=_Resolver({"env:PROV_KEY": SECRET}),
        data_dir=tmp_path / "d", config_path=tmp_path / "c.json", **extra,
    ).materialize()


# ------------------------------------------------------------- hardening


def _h(classification, sharing="deny", package_install="deny"):
    return launch_env.hardening_env(
        classification=classification, sharing=sharing, package_install=package_install,
        npm_cache="/p/cache/npm", npm_userconfig="/p/state/npmrc",
    )


def test_work_hardening_rows():
    out = _h("work")
    assert set(out) == set(launch_env.HARDENING_ENV_NAMES) | {
        "npm_config_cache", "npm_config_userconfig", "npm_config_offline"}
    assert all(out[n] == "1" for n in launch_env.HARDENING_ENV_NAMES)
    assert out["npm_config_offline"] == "true"
    assert out["npm_config_userconfig"] == "/p/state/npmrc"
    assert "npm_config_offline" not in _h("work", package_install="allow")
    assert _h("work", sharing="manual")["OPENCODE_DISABLE_SHARE"] == "1"


def test_personal_hardening_rows():
    out = _h("personal")
    assert set(out) == {
        "OPENCODE_DISABLE_MODELS_FETCH", "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_SHARE", "npm_config_cache"}
    assert "OPENCODE_DISABLE_SHARE" not in _h("personal", sharing="manual")
    assert "npm_config_offline" not in _h("personal", package_install="deny")
    assert "npm_config_userconfig" not in out


# ----------------------------------------------------------- build_env


def test_storage_overrides_ambient_tmpdir_and_drops_ambient_storage(tmp_path):
    dirs = profile_dirs.ProfileDirs.at(tmp_path / "a" / "work")
    ambient = {
        "PATH": "/bin", "TMPDIR": "/tmp/x", "XDG_STATE_HOME": "/s", "XDG_CACHE_HOME": "/c",
        "npm_config_cache": "/n", "npm_config_userconfig": "/u", "npm_config_offline": "false",
        "OPENCODE_PURE": "0", "OPENCODE_DISABLE_SHARE": "0",
    }
    plain = _build(tmp_path, ambient)
    for name in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "npm_config_cache", "npm_config_userconfig",
                 "npm_config_offline", "OPENCODE_PURE", "OPENCODE_DISABLE_SHARE"):
        assert name not in plain
    assert plain["TMPDIR"] == "/tmp/x"
    values = _build(tmp_path, ambient, storage=profile_dirs.child_storage_env(dirs))
    assert values["TMPDIR"] == str(dirs.tmp)
    assert values["XDG_STATE_HOME"] == str(dirs.state)
    assert values["XDG_CACHE_HOME"] == str(dirs.cache)
    assert values["npm_config_cache"] == str(dirs.npm_cache)


def test_hardening_reaches_the_child(tmp_path):
    values = _build(tmp_path, hardening=_h("work"))
    assert values["OPENCODE_PURE"] == "1" and values["npm_config_offline"] == "true"


def test_build_env_without_storage_is_unchanged(tmp_path):
    values = _build(tmp_path)
    assert sorted(values) == ["HOME", "OPENCODE_CONFIG", "PATH", "PROV_KEY", "XDG_DATA_HOME"]


# ---------------------------------------------------------- containment


def _contained(tmp_path, ambient):
    adapter = tmp_path / "adapter"
    root = adapter / "work"
    dirs = profile_dirs.ProfileDirs.at(root)
    return _build(
        tmp_path, ambient, adapter_root=adapter, profile_root=root,
        storage=profile_dirs.child_storage_env(dirs), hardening=_h("work"),
    )


def test_containment_refuses_another_profile_by_variable_name(tmp_path):
    other = tmp_path / "adapter" / "personal" / "cfg"
    with pytest.raises(LaunchRefused) as err:
        _contained(tmp_path, {"PATH": "/bin", "XDG_CONFIG_HOME": str(other)})
    assert err.value.code == "env-path-outside-profile"
    assert err.value.category == "invalid-configuration"
    assert "XDG_CONFIG_HOME" in err.value.message and str(other) not in err.value.message


def test_containment_refuses_a_path_component_in_the_adapter_root(tmp_path):
    inside = tmp_path / "adapter" / "bin"
    with pytest.raises(LaunchRefused) as err:
        _contained(tmp_path, {"PATH": "/bin" + os.pathsep + str(inside)})
    assert "PATH" in err.value.message


def test_containment_passes_ancestors_and_outside_values(tmp_path):
    values = _contained(tmp_path, {"PATH": "/bin", "HOME": str(tmp_path), "XDG_CONFIG_HOME": "/elsewhere"})
    assert values["HOME"] == str(tmp_path)


def test_containment_does_not_refuse_a_symlinked_adapter_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    adapter = tmp_path / "link" / "adapter"
    root = adapter / "work"
    dirs = profile_dirs.ProfileDirs.at(root)
    values = _build(
        tmp_path, adapter_root=adapter, profile_root=root,
        storage=profile_dirs.child_storage_env(dirs), hardening=_h("work"),
        state_dir=root,
    )
    assert values["XDG_STATE_HOME"].startswith(str(root))


# ----------------------------------------------------- credential kinds


def test_credential_kinds_mask_refs():
    got = launch_env.credential_kinds(SIDECAR, [_view()])
    assert got == [{"provider": "prov", "ref_masked": credential_refs.parse("env:PROV_KEY").masked(),
                    "kind": "static-api-key"}]
    keychain = launch_env.credential_kinds(SIDECAR, [_view(ref="keychain:svc/secretaccount")])
    assert "secretaccount" not in keychain[0]["ref_masked"]
    bad = launch_env.credential_kinds(SIDECAR, [_view(ref="nonsense")])
    assert bad[0]["ref_masked"] == "unparseable"
    assert launch_env.credential_kinds({}, []) == []


def test_name_tuples():
    assert set(launch_env.HARDENING_ENV_NAMES) <= set(launch_env.LAUNCHER_ENV_NAMES)
    assert not set(launch_env.PROFILE_STORAGE_ENV_NAMES) & set(launch_env.LAUNCHER_ENV_NAMES)
    assert paths.ENV_STATE_DIR in launch_env.LAUNCHER_ENV_NAMES


# ------------------------------------------------------------- driver part


@pytest.fixture(autouse=True)
def _no_launcher_state(monkeypatch):
    monkeypatch.delenv(paths.ENV_STATE_DIR, raising=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Setup(tmp_path, monkeypatch)


def _adapter_root(env):
    return paths.state_dir(env.world.env, env.world.home)


def test_child_environment_for_a_work_run(env):
    prepared = env.driver().prepare(env.request())
    root = _adapter_root(env) / "work"
    values = prepared.launch_env.materialize()
    assert values[paths.ENV_STATE_DIR] == str(root)
    assert values["XDG_DATA_HOME"] == str(root / "data")
    assert values["XDG_STATE_HOME"] == str(root / "state")
    assert values["XDG_CACHE_HOME"] == str(root / "cache")
    assert values["TMPDIR"] == str(root / "tmp")
    assert values["npm_config_cache"] == str(root / "cache" / "npm")
    assert values["OPENCODE_PURE"] == "1" and values["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert os.stat(root).st_mode & 0o777 == 0o700
    index = (root / "projects.json").read_text()
    assert os.path.realpath(env.root) in index


def test_one_profile_per_process(env):
    drv = env.driver()
    drv.prepare(env.request())
    drv.prepare(env.request(task="again"))
    with pytest.raises(driver.PrepareRefused) as info:
        drv.prepare(env.request(profile="other"))
    assert info.value.code == "profile-switch-in-process"
    assert info.value.category == launch_codes.category("profile-switch-in-process")


def test_a_classification_change_is_refused(env):
    session = profile_dirs.LaunchSession(("work", "personal"))
    drv = driver.OpenCodeDriver(
        env.root, env=dict(env.world.env, PATH=os.environ.get("PATH", "")), home=env.world.home,
        which=lambda name: str(env.shim), version_runner=lambda path: "1.18.32",
        resolver_factory=lambda environ, platform: _Resolver2(),
        session=session,
    )
    with pytest.raises(driver.PrepareRefused) as info:
        drv.prepare(env.request())
    assert info.value.code == "profile-switch-in-process"


class _Resolver2:
    def resolve(self, ref):
        return credential_refs.SecretValue("sk-test-0000000000")


def test_stamp_mismatch_is_refused(env):
    profile_dirs.prepare(_adapter_root(env), "work", "personal")
    env.refused("profile-stamp-mismatch", env.request())


def test_plain_driver_compiles_where_paths_says(env):
    prepared = env.driver().prepare(env.request())
    expected = paths.compiled_output_dir("work", env.root, env.world.env, env.world.home)
    assert prepared.config_path.parent == expected


def test_a_launcher_built_driver_compiles_beside_the_profile_not_inside_it(env):
    root = profile_dirs.prepare(_adapter_root(env), "work", "work").root
    nested = dict(
        env.world.env, PATH=os.environ.get("PATH", ""),
        XDG_STATE_HOME=str(root / "state"), **{paths.ENV_STATE_DIR: str(root)},
    )
    drv = driver.OpenCodeDriver(
        env.root, env=nested, home=env.world.home, which=lambda name: str(env.shim),
        version_runner=lambda path: "1.18.32", resolver_factory=lambda environ, platform: _Resolver2(),
    )
    prepared = drv.prepare(env.request())
    assert prepared.config_path.parent == root / paths.project_key(env.root)
    assert not (root / "state" / "quoin").exists()
    assert prepared.launch_env.materialize()["OPENCODE_CONFIG"] == str(prepared.config_path)
    with pytest.raises(driver.PrepareRefused) as info:
        drv.prepare(env.request(profile="other"))
    assert info.value.code == "profile-switch-in-process"


def _interrupted(env):
    prepared = env.driver().prepare(env.request())
    directory = runstore.store_dir(env.root)
    record = runstore.load_record(directory, prepared.run_id)
    attempt = runstore.new_attempt(
        1, pid=1, pgid=1, child_start="x", driver_pid=os.getpid(), driver_start=None, resume_mode="fresh",
    )
    attempt.update(state="interrupted", reason="signal")
    record["attempts"].append(attempt)
    record["state"] = "interrupted"
    runstore.write_record(directory, record)
    return prepared, directory


def test_resume_under_another_profile_is_refused_with_a_profile_detail(env):
    prepared, directory = _interrupted(env)
    err = env.refused("resume-request-mismatch", env.request(profile="other"), resume_run_id=prepared.run_id)
    assert err.detail == "profile"
    record = runstore.load_record(directory, prepared.run_id)
    assert record["state"] == "interrupted"
    assert record["refusal"] == {
        "category": "workflow-validation", "code": "resume-request-mismatch",
        "message": "the request names a different profile than the run being resumed", "detail": "profile",
    }


def test_resume_under_another_task_has_no_detail(env):
    prepared, directory = _interrupted(env)
    err = env.refused("resume-request-mismatch", env.request(task="other-task"), resume_run_id=prepared.run_id)
    assert err.detail is None
    record = runstore.load_record(directory, prepared.run_id)
    assert "detail" not in record["refusal"] and record["state"] == "interrupted"


def test_credentials_are_recorded_masked(env):
    prepared = env.driver().prepare(env.request())
    record = runstore.load_record(runstore.store_dir(env.root), prepared.run_id)["prepared"]
    assert record["static_key_in_use"] is True
    assert record["credentials"] and all(c["kind"] == "static-api-key" for c in record["credentials"])
    assert record["profile_root"] == "<state>/work"
    assert str(env.tmp) not in record["profile_root"]


def test_new_codes_are_registered_in_their_categories():
    assert launch_codes.category("profile-switch-in-process") == "policy-denial"
    assert launch_codes.category("profile-stamp-mismatch") == "policy-denial"
    assert launch_codes.category("profile-dir-unsafe") == "invalid-configuration"
    assert launch_codes.category("env-path-outside-profile") == "invalid-configuration"


# ------------------------------------------------- coordinator, snapshot, test command


def _coordinator(env, profile="work"):
    from quoin.opencode_adapter import phase_loop, workflow

    return workflow.Coordinator(
        env.driver(), env.root, "demo", workflow.WorkflowOptions(profile=profile),
        source_dir=None, cancel=phase_loop.CancelToken(), scope={"profile": profile}, scope_source="test",
    )


def test_the_coordinator_keeps_its_state_under_the_profile_root(env):
    from quoin.opencode_adapter import testrun

    coord = _coordinator(env)
    root = _adapter_root(env) / "work"
    assert coord.state_root == root
    testrun.configure(env.root, "demo", command=["true"], state_root=coord.state_root)
    pin_dirs = [p for p in root.rglob("*") if p.is_file()]
    assert pin_dirs and all(root in p.parents for p in pin_dirs)
    for name in ("tests", "workflow", "snapshots"):
        assert not (_adapter_root(env) / name).exists()


def test_a_critic_snapshot_is_created_under_the_profile_root(env, monkeypatch):
    from quoin.opencode_adapter import snapshot

    seen = {}

    def fake_create(root, task, stage, **kwargs):
        seen["state_root"] = kwargs["state_root"]
        raise snapshot.SnapshotRefused("stop-here")

    monkeypatch.setattr(snapshot, "create", fake_create)
    out = snapshot.run_in_snapshot(
        env.driver(), env.request(phase="critic"), kind="critic", source_dir=None, cancel=None, max_relaunch=0,
    )
    assert out.refusal == "stop-here"
    assert seen["state_root"] == _adapter_root(env) / "work"


# ------------------------------------------------------------ consumer pins

_SRC = Path(__file__).resolve().parents[3] / "src" / "quoin"

# Every call to these helpers in production code, as (file, enclosing function, callee).
# A new caller or a vanished one fails the test, so the storage-root census stays honest.
ALLOWED_STATE_DIR_CALLS = {
    ("opencode_adapter/profile_dirs.py", "adapter_root", "state_dir"):
        "the one place a plain environment becomes the adapter root",
    ("opencode_adapter/paths.py", "compiled_output_dir", "state_dir"):
        "kept for callers that hold a plain environment",
    ("opencode_adapter/launch_env.py", "data_dir", "state_dir"):
        "pins the data directory location for existing sessions",
}
ALLOWED_COMPILED_CALLS = {
    ("opencode_adapter/compiler.py", "resolve_output_dir", "compiled_output_dir"):
        "reached only when no output directory is given",
}


def _calls(names):
    import ast

    found = {}
    files = sorted((_SRC / "opencode_adapter").glob("*.py")) + [_SRC / "cli.py"]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                    if callee in names:
                        found[(str(path.relative_to(_SRC)), func.name, callee)] = node
    return found


def test_state_dir_is_called_only_at_the_census_sites():
    found = set(_calls({"state_dir"}))
    assert found == set(ALLOWED_STATE_DIR_CALLS), (found ^ set(ALLOWED_STATE_DIR_CALLS))


def test_compiled_output_dir_is_called_only_at_the_census_site():
    found = set(_calls({"compiled_output_dir"}))
    assert found == set(ALLOWED_COMPILED_CALLS), (found ^ set(ALLOWED_COMPILED_CALLS))


def test_no_launch_or_cli_path_asks_the_compiler_for_its_default_output():
    for key, node in _calls({"resolve_output_dir"}).items():
        if key[0] in ("cli.py", "opencode_adapter/driver.py"):
            kws = {k.arg: k.value for k in node.keywords}
            import ast

            assert "output" in kws and not (isinstance(kws["output"], ast.Constant) and kws["output"].value is None), key


def test_commands_that_read_no_profile_state_do_not_name_a_state_root():
    for name in ("gate.py", "evidence.py", "handoff.py"):
        text = (_SRC / "opencode_adapter" / name).read_text(encoding="utf-8")
        assert "state_root" not in text and "ENV_STATE_DIR" not in text, name
