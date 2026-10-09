"""Profile storage, hardening flags, containment and credential kinds in the launch environment."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from quoin.opencode_adapter import launch_env, paths, profile_dirs
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
