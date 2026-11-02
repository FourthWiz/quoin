"""The trusted managed policy layer: one test per state at the fixed path,
the shared managed-layers loader and its wiring into the probe and the role
rule. The fixture root lives under ``tmp_path``; the owner is the test user and
the invoking user is made to differ, so nothing here needs root.
"""
from __future__ import annotations

import ast
import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import NOW, PROFILE_WORK, PROJECT_WORK, fixture, loaded, make_layer
from quoin.opencode_adapter import config, errors, jsonio, merge, paths, probe_cli, qualification
from quoin.opencode_adapter.errors import ConfigErrors
from quoin.opencode_adapter.roles import AllowUnqualifiedRefused, resolve_all

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
UID = os.getuid()
POLICY = {"schema_version": 1, "policy": {"web_tools": "allow"}}


class Root:
    """A fake filesystem root holding ``etc/quoin/opencode/managed-policy.json``."""

    def __init__(self, tmp_path):
        self.top = Path(tmp_path) / "root"
        self.directory = self.top / "etc" / "quoin" / "opencode"
        self.directory.mkdir(parents=True)
        for path in (self.top, self.top / "etc", self.top / "etc" / "quoin", self.directory):
            os.chmod(path, 0o755)
        self.path = self.directory / "managed-policy.json"

    def write(self, data=POLICY, mode=0o644):
        text = data if isinstance(data, str) else json.dumps(data)
        self.path.write_text(text, encoding="utf-8")
        os.chmod(self.path, mode)
        return self

    def source(self, *, owner=UID, invoking=UID + 1):
        return config.TrustedSource(self.path, owner, invoking, self.top)


@pytest.fixture
def root(tmp_path):
    return Root(tmp_path)


# ------------------------------------------------------ one test per row


def test_nothing_at_the_path_is_absent(root):
    assert config.load_trusted_managed(root.source()) == (None, "absent")


def test_a_file_under_a_missing_directory_is_absent(tmp_path):
    source = config.TrustedSource(tmp_path / "no" / "such" / "p.json", UID, UID + 1, tmp_path)
    assert config.load_trusted_managed(source) == (None, "absent")


def test_a_clean_file_is_trusted(root):
    layer, state = config.load_trusted_managed(root.write().source())
    assert state == "trusted" and layer.file == config.TRUSTED_MANAGED_LABEL
    assert layer.data["policy"]["web_tools"] == "allow"


def test_a_symlink_is_refused_not_followed(root, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(POLICY), encoding="utf-8")
    os.symlink(target, root.path)
    with pytest.raises(ConfigErrors) as info:
        config.load_trusted_managed(root.source())
    assert [(e.rejection_class, e.message_id, e.file) for e in info.value.errors] == [
        ("invalid-json", "trusted-policy-unreadable", "trusted managed policy")
    ]


def test_a_fifo_is_refused_without_blocking(root):
    os.mkfifo(root.path)
    with pytest.raises(ConfigErrors) as info:
        config.load_trusted_managed(root.source())
    assert info.value.errors[0].message_id == "trusted-policy-unreadable"


def test_a_directory_at_the_path_is_refused(root):
    root.path.mkdir()
    with pytest.raises(ConfigErrors) as info:
        config.load_trusted_managed(root.source())
    assert info.value.errors[0].message_id == "trusted-policy-unreadable"


def test_a_group_writable_file_narrows_only(root):
    layer, state = config.load_trusted_managed(root.write(mode=0o664).source())
    assert layer is not None and state == "untrusted:file-writable"


def test_the_reader_reports_a_file_owned_by_someone_else(root):
    root.write()
    tree, token = jsonio.read_trusted_json(root.path, file_label="x", owner_uid=UID + 5)
    assert tree["policy"]["web_tools"] == "allow" and token == "file-not-owned"


def test_the_chain_check_wins_over_the_file_check(root):
    state = config.load_trusted_managed(root.write().source(owner=UID + 5))[1]
    assert state == "untrusted:chain-not-owned"


def test_a_group_writable_ancestor_narrows_only(root):
    root.write()
    os.chmod(root.top / "etc", 0o775)
    assert config.load_trusted_managed(root.source())[1] == "untrusted:chain-writable"


def test_a_symlinked_ancestor_is_not_trusted(tmp_path):
    real = Root(tmp_path)
    real.write()
    link = tmp_path / "link"
    os.symlink(real.directory, link)
    source = config.TrustedSource(link / "managed-policy.json", UID, UID + 1, tmp_path)
    assert config.load_trusted_managed(source)[1] == "untrusted:chain-not-directory"


def test_an_ancestor_that_is_not_a_directory_is_reported(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "etc").write_text("x", encoding="utf-8")
    token = jsonio.check_root_owned_chain(
        tmp_path / "root" / "etc" / "quoin" / "p.json", owner_uid=UID, top=tmp_path / "root"
    )
    assert token == "chain-not-directory"


def test_the_chain_check_is_clean_for_a_good_chain(root):
    assert jsonio.check_root_owned_chain(root.path, owner_uid=UID, top=root.top) is None


def test_an_invoking_user_equal_to_the_owner_narrows_only(root):
    state = config.load_trusted_managed(root.write().source(invoking=UID))[1]
    assert state == "untrusted:invoking-user-is-root"


@pytest.mark.skipif(os.getuid() == 0, reason="root can read a mode 0000 file")
def test_an_unreadable_file_is_refused(root):
    root.write(mode=0o000)
    with pytest.raises(ConfigErrors) as info:
        config.load_trusted_managed(root.source())
    assert info.value.errors[0].message_id == "trusted-policy-unreadable"


def test_an_inode_swap_between_lstat_and_open_is_refused(root):
    root.write()
    real = os.lstat

    def swapped(path):
        info = real(path)
        if str(path) == str(root.path):
            return SimpleNamespace(
                st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1,
                st_uid=info.st_uid, st_size=info.st_size,
            )
        return info

    with pytest.raises(ConfigErrors) as info:
        jsonio.read_trusted_json(root.path, file_label="x", owner_uid=UID, lstat=swapped)
    assert info.value.errors[0].message_id == "trusted-policy-unreadable"


@pytest.mark.parametrize(
    "text, message_id",
    [("{", "malformed-json"), ('{"schema_version": 1, "schema_version": 1}', "duplicate-key")],
)
def test_invalid_json_is_refused_with_the_trusted_label(root, text, message_id):
    root.write(text)
    with pytest.raises(ConfigErrors) as info:
        config.load_trusted_managed(root.source())
    err = info.value.errors[0]
    assert err.message_id == message_id and err.file == "trusted managed policy"
    assert str(root.top) not in str(info.value)


def test_an_invalid_untrusted_file_still_refuses(root):
    root.write({"schema_version": 1, "bogus": True}, mode=0o666)
    with pytest.raises(ConfigErrors) as info:
        config.load_trusted_managed(root.source())
    assert info.value.errors[0].rejection_class == "unknown-key"


# ------------------------------------------------------ path and purity


@pytest.mark.parametrize(
    "platform, expected",
    [
        ("darwin", "/Library/Application Support/quoin/opencode/managed-policy.json"),
        ("linux", "/etc/quoin/opencode/managed-policy.json"),
    ],
)
def test_platform_path_table(platform, expected):
    assert paths.trusted_policy_path(platform) == Path(expected)
    assert {str(Path(d) / paths.TRUSTED_POLICY_NAME) for d in paths.TRUSTED_POLICY_DIRS} == {
        "/Library/Application Support/quoin/opencode/managed-policy.json",
        "/etc/quoin/opencode/managed-policy.json",
    }


def test_the_trusted_path_takes_no_environment_input():
    tree = ast.parse((SRC_DIR / "paths.py").read_text(encoding="utf-8"))
    [func] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "trusted_policy_path"]
    assert [a.arg for a in func.args.args] == ["platform"]
    for node in ast.walk(func):
        if isinstance(node, ast.Name):
            assert node.id not in ("env", "environ")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "QUOIN_" not in node.value


def test_new_trust_code_reads_no_quoin_variable():
    for name in ("config", "jsonio"):
        tree = ast.parse((SRC_DIR / (name + ".py")).read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef) and ("trusted" in func.name or func.name == "check_root_owned_chain"):
                for node in ast.walk(func):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        assert "QUOIN_" not in node.value, func.name


def test_the_system_source_is_fixed():
    source = config.TrustedSource.system()
    assert source.path == paths.trusted_policy_path()
    assert (source.owner_uid, source.top) == (0, Path("/"))


def test_trust_tokens_are_the_closed_subject_set():
    assert errors.make_finding("trusted-policy-untrusted", False, "file-writable").subject == ("file-writable",)
    with pytest.raises(ValueError):
        errors.make_finding("trusted-policy-untrusted", False, "something-else")


# ------------------------------------------------------ shared loader


def test_the_shared_loader_returns_both_layers(root, tmp_path):
    managed_file = tmp_path / "m.json"
    managed_file.write_text(json.dumps({"schema_version": 1, "policy": {"sharing": "deny"}}), encoding="utf-8")
    env = {paths.ENV_MANAGED_POLICY: str(managed_file)}
    layers = config.load_managed_layers(env, root.write().source())
    assert layers.managed is not None and layers.trusted_managed is not None
    assert layers.trusted_state == "trusted"
    assert config.load_managed_layers({}, root.source()).managed is None


def test_a_direct_loaded_config_means_no_trusted_file():
    base = loaded(PROFILE_WORK, PROJECT_WORK)
    assert (base.trusted_managed, base.trusted_state) == (None, "absent")


# ------------------------------------------------------ probe and roles


def test_the_probe_sees_the_trusted_ceiling_before_any_runner_call(tmp_path, root):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "XDG_STATE_HOME": str(tmp_path / "state")}
    home = tmp_path / "home"
    profile = {
        "schema_version": 1, "runtime": "opencode", "profile": "probeprof", "classification": "personal",
        "providers": {"fake": {
            "kind": "openai-compatible", "endpoint_family": "chat-completions",
            "base_url": "https://gateway.example.invalid/v1", "credential_ref": "env:QUOIN_TEST_FAKE_KEY",
        }},
        "models": {"fake-model": {"provider": "fake", "model_id": "m", "qualification_ref": "local:fake-model"}},
        "default_model": "fake-model",
    }
    target = paths.profile_path("probeprof", env, home)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(profile), encoding="utf-8")
    root.write({"schema_version": 1, "policy": {"denied_providers": ["fake"]}})

    def runner(*args, **kwargs):
        raise AssertionError("no runner call may happen once the provider is excluded")

    class NoProbe:
        def __getattr__(self, name):
            raise AssertionError("the probe module must not be used")

    err = io.StringIO()
    code = probe_cli.run(
        profile="probeprof", model=None, project_root=None, synthetic_only=True, env=env,
        environ={"QUOIN_TEST_FAKE_KEY": "x"}, home=home, now=NOW, platform="linux", runner=runner,
        stderr=err, probe_module=NoProbe(), trusted=root.source(),
    )
    assert code == 2
    assert probe_cli.PROBE_MESSAGES["provider-excluded"] in err.getvalue()


def test_work_with_only_a_trusted_file_refuses_allow_unqualified(root, tmp_path):
    base = loaded(PROFILE_WORK, PROJECT_WORK)
    layer, state = config.load_trusted_managed(root.write().source())
    full = config.LoadedConfig(base.profile, base.project, None, layer, state)
    eff = merge.merge(full)
    assert eff.managed_present is True and eff.trusted_state == "trusted"
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "XDG_STATE_HOME": str(tmp_path / "state")}
    quals = qualification.evaluate_all(
        eff, env=env, home=tmp_path / "home", now=NOW, pinned_version=qualification.pinned_version()
    )
    with pytest.raises(AllowUnqualifiedRefused):
        resolve_all(eff, quals, allow_unqualified=True)
