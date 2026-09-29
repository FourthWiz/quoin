"""Tests for the profile-driven probe wiring and the `quoin opencode probe`
command. Everything talks to a fake provider on the loopback interface."""
from __future__ import annotations

import ast
import contextlib
import copy
import json
import os
import stat
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import NOW
from quoin import cli
from quoin.opencode_adapter import compiler, config, jsonio, merge, paths, probe_cli, qualification
from quoin.opencode_adapter.errors import ConfigErrors

fake_server = helpers.load_module(helpers.OPENCODE_DIR / "fake_openai_server.py", "fake_openai_server_probe_cli")

SECRET = helpers.SEEDED_SECRET
ENV_NAME = "QUOIN_TEST_FAKE_KEY"
PROFILE = "fakeprof"
DATA_DIR = helpers.OPENCODE_DIR


@pytest.fixture(autouse=True)
def loopback_guard(monkeypatch):
    helpers.install_loopback_guard(monkeypatch)


@pytest.fixture(scope="module")
def server():
    srv = fake_server.FakeProviderServer(expected_token=SECRET)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def fresh_requests(server):
    server.clear_requests()
    yield


@contextlib.contextmanager
def env_writes_trapped():
    """Any attempt to write the process environment fails while inside."""

    def guard(real):
        def wrapper(key, *args, **kwargs):
            if bytes(key) == b"PYTEST_CURRENT_TEST":
                return real(key, *args, **kwargs)
            raise AssertionError("the process environment must not be written")

        return wrapper

    with mock.patch.object(os, "putenv", guard(os.putenv)), mock.patch.object(os, "unsetenv", guard(os.unsetenv)):
        yield


def environ_snapshot():
    return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}


@pytest.fixture(autouse=True)
def environment_is_unchanged():
    before = environ_snapshot()
    yield
    assert environ_snapshot() == before


def run_cli(argv):
    with env_writes_trapped():
        return cli.main(argv)


class Box:
    """A temporary configuration home holding one profile that points at the
    fake provider."""

    def __init__(self, tmp_path, server, *, model_id="default_ok", credential_ref="env:" + ENV_NAME,
                 family="chat-completions", policy=None, classification="personal"):
        self.tmp = Path(tmp_path)
        self.env = {"XDG_CONFIG_HOME": str(self.tmp / "xdg"), "XDG_STATE_HOME": str(self.tmp / "state")}
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.server = server
        self.profile = {
            "schema_version": 1,
            "runtime": "opencode",
            "profile": PROFILE,
            "classification": classification,
            "providers": {
                "fake": {
                    "kind": "openai-compatible",
                    "endpoint_family": family,
                    "base_url": server.base_url,
                    "credential_ref": credential_ref,
                }
            },
            "models": {
                "fake-model": {"provider": "fake", "model_id": model_id, "qualification_ref": "local:fake-model"},
                "other-model": {"provider": "fake", "model_id": "default_ok", "qualification_ref": "local:other-model"},
            },
            "default_model": "fake-model",
        }
        if policy is not None:
            self.profile["policy"] = policy
        self.write()

    def write(self):
        target = paths.profile_path(PROFILE, self.env, self.home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.profile), encoding="utf-8")

    @property
    def record(self):
        return paths.qualification_path("fake-model", self.env, self.home)

    def call(self, **overrides):
        kwargs = dict(
            profile=PROFILE, model=None, project_root=None, synthetic_only=True,
            env=self.env, environ={ENV_NAME: SECRET}, home=self.home, now=NOW, platform="linux",
        )
        kwargs.update(overrides)
        with env_writes_trapped():
            return probe_cli.run(**kwargs)

    def state(self, model="fake-model"):
        loaded = config.LoadedConfig(config.load_profile(PROFILE, env=self.env, home=self.home), None, None)
        eff = merge.merge(loaded)
        return qualification.evaluate(
            eff.models[model], eff.providers[eff.models[model].provider],
            env=self.env, home=self.home, now=NOW, pinned_version=qualification.pinned_version(),
        )


@pytest.fixture
def box(tmp_path, server):
    return Box(tmp_path, server)


def tree_files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


# ------------------------------------------------------------- happy path


def test_a_qualified_run_writes_a_private_record(box, capsys):
    code = box.call()
    out = capsys.readouterr()
    assert code == 0
    assert out.out.strip() == "qualified"
    assert out.err.count(probe_cli.LIVE_NOTICE) == 1
    assert box.record.is_file()
    assert stat.S_IMODE(box.record.stat().st_mode) == 0o600
    assert stat.S_IMODE(box.record.parent.stat().st_mode) == 0o700
    record = json.loads(box.record.read_text(encoding="utf-8"))
    assert record["key"]["runtime"]["version"] == qualification.pinned_version()
    assert record["key"]["endpoint"] == config.endpoint_identity(box.server.base_url)
    assert record["key"]["model_id"] == "default_ok"
    assert SECRET not in json.dumps(record)


def test_the_record_is_read_back_as_qualified_end_to_end(box):
    assert box.call() == 0
    assert box.state().state == "qualified"
    eff = merge.merge(config.LoadedConfig(config.load_profile(PROFILE, env=box.env, home=box.home), None, None))
    quals = qualification.evaluate_all(
        eff, env=box.env, home=box.home, now=NOW, pinned_version=qualification.pinned_version(),
    )
    assert quals["fake-model"].state == "qualified" and quals["other-model"].state == "missing"


def test_the_secret_really_reaches_the_gateway(box, server):
    assert box.call() == 0
    requests = server.snapshot_requests()
    assert requests and all(r["auth_matches_expected"] is True for r in requests)


def test_model_selects_a_non_default_model(box, server):
    assert box.call(model="other-model") == 0
    assert paths.qualification_path("other-model", box.env, box.home).is_file()
    assert not box.record.exists()


def test_no_project_root_needs_no_classification(box):
    assert box.call(project_root=None) == 0


# ---------------------------------------------------------------- refusals


def _assert_nothing_sent(box, server, capsys):
    out = capsys.readouterr()
    assert server.snapshot_requests() == []
    assert probe_cli.LIVE_NOTICE not in out.err
    return out


def test_refuses_without_synthetic_only_before_touching_anything(box, server, capsys):
    before = tree_files(box.tmp)
    assert box.call(synthetic_only=False) == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.PROBE_MESSAGES["synthetic-only-required"] in out.err
    assert tree_files(box.tmp) == before


def test_refusal_does_not_even_read_a_missing_profile(tmp_path, server, capsys):
    with env_writes_trapped():
        got = probe_cli.run(
            profile="nothing-here", model=None, project_root=None, synthetic_only=False,
            env={"XDG_CONFIG_HOME": str(tmp_path / "x")}, environ={}, home=tmp_path, now=NOW,
            platform="linux",
        )
    assert got == 2
    assert probe_cli.PROBE_MESSAGES["synthetic-only-required"] in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_responses_family_is_refused(tmp_path, server, capsys):
    box = Box(tmp_path, server, family="responses")
    assert box.call() == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.PROBE_MESSAGES["responses-unsupported"] in out.err
    assert not box.record.parent.exists() or not box.record.exists()


def test_unknown_model_is_refused(box, server, capsys):
    assert box.call(model="no-such-model") == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.PROBE_MESSAGES["unknown-model"] in out.err


def test_a_provider_excluded_by_the_profile_is_refused(tmp_path, server, capsys):
    box = Box(tmp_path, server, policy={"allowed_hosts": ["gateway.example.invalid"]})
    assert box.call() == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.PROBE_MESSAGES["provider-excluded"] in out.err
    assert "host-not-allowed" in out.err


def test_a_provider_excluded_by_managed_policy_is_refused(box, server, capsys, tmp_path):
    managed = tmp_path / "managed.json"
    managed.write_text(json.dumps({"schema_version": 1, "policy": {"denied_providers": ["fake"]}}), encoding="utf-8")
    env = dict(box.env, QUOIN_OPENCODE_MANAGED_POLICY=str(managed))
    assert box.call(env=env) == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.PROBE_MESSAGES["provider-excluded"] in out.err


def _project(tmp_path, data):
    root = tmp_path / "project"
    (root / ".quoin").mkdir(parents=True, exist_ok=True)
    if data is not None:
        (root / ".quoin" / "runtime.json").write_text(json.dumps(data), encoding="utf-8")
    return root


def test_a_work_project_with_a_personal_profile_is_a_configuration_error(box, tmp_path):
    root = _project(tmp_path, {"schema_version": 1, "runtime": "opencode", "profile": PROFILE, "classification": "work"})
    with pytest.raises(ConfigErrors) as info:
        box.call(project_root=root)
    assert "personal-profile-for-work" in {e.rejection_class for e in info.value.errors}


def test_a_project_root_without_a_runtime_file_needs_a_classification(box, server, tmp_path, capsys):
    assert box.call(project_root=_project(tmp_path, None)) == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.PROBE_MESSAGES["classification-required"] in out.err


def test_an_unclassified_project_needs_a_classification(box, server, tmp_path, capsys):
    root = _project(tmp_path, {"schema_version": 1, "runtime": "opencode", "profile": PROFILE})
    assert box.call(project_root=root) == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.PROBE_MESSAGES["classification-required"] in out.err


def test_a_classified_project_runs(box, tmp_path):
    root = _project(
        tmp_path,
        {"schema_version": 1, "runtime": "opencode", "profile": PROFILE, "classification": "personal"},
    )
    assert box.call(project_root=root) == 0


# -------------------------------------------------------------- credentials


def test_a_missing_credential_stops_before_any_notice_or_request(box, server, capsys):
    assert box.call() == 0  # seed a record
    seeded = box.record.read_bytes()
    server.clear_requests()
    capsys.readouterr()
    assert box.call(environ={}) == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert "env:" + ENV_NAME in out.err and "not set" in out.err
    assert box.record.read_bytes() == seeded  # a mistyped credential does not de-qualify a working model


def test_a_keychain_reference_on_another_platform_is_unavailable(tmp_path, server, capsys):
    box = Box(tmp_path, server, credential_ref="keychain:quoin-test-service/test-account")
    assert box.call(platform="linux") == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert "keychain backend unavailable" in out.err
    assert "test-account" not in out.err


def test_a_keychain_reference_resolves_through_the_runner(tmp_path, server, capsys):
    box = Box(tmp_path, server, credential_ref="keychain:quoin-test-service/test-account")
    calls = []

    def runner(argv, timeout):
        calls.append(list(argv))
        return 0, (SECRET + "\n").encode()

    assert box.call(platform="darwin", runner=runner, environ={}) == 0
    assert calls and SECRET not in " ".join(calls[0])
    assert box.state().state == "qualified"


# -------------------------------------------------------- fail-closed cases


def test_a_failing_gateway_overwrites_the_record_and_fails_the_model(tmp_path, server):
    box = Box(tmp_path, server)
    assert box.call() == 0
    box.profile["models"]["fake-model"]["model_id"] = "server_error_503"
    box.write()
    assert box.call() == 2
    assert json.loads(box.record.read_text(encoding="utf-8"))["verdict"]["status"] == "could_not_run"
    got = box.state()
    assert (got.state, got.reason) == ("failed", "could-not-run")


class _Stub:
    def __init__(self, execute):
        self.ProbeConfig = probe_cli.load_probe_module().ProbeConfig
        self.execute = execute


def test_a_crashing_probe_leaves_the_model_missing_and_prints_only_the_type(box, capsys):
    assert box.call() == 0
    assert box.state().state == "qualified"
    capsys.readouterr()

    def crash(config_, env_, extra_headers=None, now=None, nonce_factory=None):
        raise RuntimeError(SECRET)

    assert box.call(probe_module=_Stub(crash)) == 2
    err = capsys.readouterr().err
    assert probe_cli.UNEXPECTED_TEXT % "RuntimeError" in err
    for form in helpers.secret_forms(SECRET):
        assert form not in err
    assert box.state().state == "missing"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes")
def test_an_unwritable_record_directory_leaves_no_stale_record(box, capsys):
    assert box.call() == 0
    real = probe_cli.load_probe_module()
    directory = box.record.parent

    def lock_then_run(config_, env_, extra_headers=None, now=None, nonce_factory=None):
        os.chmod(directory, 0o500)
        return real.execute(config_, env_, extra_headers=extra_headers, now=now, nonce_factory=nonce_factory)

    try:
        assert box.call(probe_module=_Stub(lock_then_run)) == 2
    finally:
        os.chmod(directory, 0o700)
    assert not box.record.exists()
    assert box.state().state == "missing"


def test_a_record_that_cannot_be_removed_stops_the_run(box, server, capsys, monkeypatch):
    assert box.call() == 0
    seeded = box.record.read_bytes()
    server.clear_requests()
    capsys.readouterr()

    def boom(path, *args, **kwargs):
        raise PermissionError("/hidden/path")

    monkeypatch.setattr(probe_cli.os, "unlink", boom)
    assert box.call() == 2
    out = _assert_nothing_sent(box, server, capsys)
    assert probe_cli.RECORD_REMOVE_TEXT in out.err and "/hidden/path" not in out.err
    assert box.record.read_bytes() == seeded


def test_a_symlink_at_the_record_path_is_removed_not_followed(box, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("keep me", encoding="utf-8")
    box.record.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(box.record.parent, 0o700)
    box.record.symlink_to(outside)
    assert box.call() == 0
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert not box.record.is_symlink() and box.record.is_file()
    assert box.state().state == "qualified"


# ------------------------------------------------- probe module loading


def test_loading_leaves_only_the_private_module_name():
    module = probe_cli.load_probe_module()
    assert callable(module.execute)
    assert probe_cli.PRIVATE_NAME in sys.modules
    assert "probe_gateway" not in sys.modules
    assert probe_cli.load_probe_module() is module  # cached


def test_loading_writes_no_bytecode_next_to_the_script():
    def pycaches():
        return sorted(str(p) for p in DATA_DIR.rglob("__pycache__"))

    before = pycaches()
    probe_cli._LOADED.clear()
    probe_cli.load_probe_module()
    assert pycaches() == before == []


def _stub_dir(tmp_path, source):
    data = tmp_path / "data"
    data.mkdir()
    (data / "probe_gateway.py").write_text(source, encoding="utf-8")
    return data


_STUB_HEAD = (
    "from dataclasses import dataclass\n"
    "@dataclass\n"
    "class ProbeConfig:\n"
    "    base_url: str\n    model: str\n    credential_env: str\n    provider: str\n"
    "    runtime_version: str = 'x'\n    ca_file: str = None\n    use_env_proxy: bool = False\n    output: str = None\n"
)


def test_a_script_that_does_not_match_is_refused_and_not_cached(tmp_path):
    data = _stub_dir(tmp_path, _STUB_HEAD + "def execute(config, env, extra_headers=None, nonce_factory=None):\n    return 0\n")
    for _ in range(2):
        with pytest.raises(paths.AdapterDataMissing, match="does not match"):
            probe_cli.load_probe_module(data)
        assert probe_cli.PRIVATE_NAME not in sys.modules
    assert os.path.realpath(str(data / "probe_gateway.py")) not in probe_cli._LOADED


def test_a_conforming_script_loads_from_another_directory(tmp_path):
    data = _stub_dir(
        tmp_path,
        _STUB_HEAD + "def execute(config, env, extra_headers=None, now=None, nonce_factory=None):\n    return 0\n",
    )
    module = probe_cli.load_probe_module(data)
    assert module.execute(None, {}) == 0
    assert not list(data.rglob("__pycache__"))


def test_a_script_that_raises_while_loading_is_not_registered(tmp_path):
    data = _stub_dir(tmp_path, "raise RuntimeError('boom')\n")
    with pytest.raises(RuntimeError):
        probe_cli.load_probe_module(data)
    assert probe_cli.PRIVATE_NAME not in sys.modules


def test_missing_probe_script_is_adapter_data_missing(tmp_path):
    with pytest.raises(paths.AdapterDataMissing):
        probe_cli.load_probe_module(tmp_path)


def test_no_adapter_data_directory(monkeypatch):
    monkeypatch.setattr(paths, "adapter_data_dir", lambda: None)
    with pytest.raises(paths.AdapterDataMissing):
        probe_cli.load_probe_module()


def test_a_bytecode_setting_is_restored():
    old = sys.dont_write_bytecode
    probe_cli._LOADED.clear()
    probe_cli.load_probe_module()
    assert sys.dont_write_bytecode == old


def test_the_probe_env_name_is_not_a_provider_credential_name():
    assert probe_cli.PROBE_ENV_NAME == "QUOIN_PROBE_CREDENTIAL"
    for pid in ("fake", "probe", "probe-credential", "corp-gw"):
        assert config.provider_env_name(pid) != probe_cli.PROBE_ENV_NAME


def test_the_probe_script_is_not_modified_by_wiring():
    result = subprocess.run(
        ["git", "diff", "--stat", "4368f830..HEAD", "--", "quoin/adapters/opencode/probe_gateway.py"],
        cwd=str(helpers.SOURCE_DIR.parent), capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip("git history unavailable")
    assert result.stdout.strip() == ""


# ------------------------------------------------------------------ the CLI


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture
def cli_box(box, monkeypatch):
    monkeypatch.setattr(cli, "datetime", _Frozen)
    monkeypatch.setenv("HOME", str(box.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", box.env["XDG_CONFIG_HOME"])
    monkeypatch.setenv("XDG_STATE_HOME", box.env["XDG_STATE_HOME"])
    monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
    monkeypatch.setenv(ENV_NAME, SECRET)
    return box


def probe_argv(*extra, profile=PROFILE):
    return ["opencode", "probe", "--profile", profile, *extra]


def test_cli_happy_path(cli_box, capsys):
    assert run_cli(probe_argv("--synthetic-only")) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "qualified"
    assert probe_cli.LIVE_NOTICE in out.err
    assert cli_box.record.is_file()


def test_cli_requires_synthetic_only(cli_box, capsys):
    assert run_cli(probe_argv()) == 2
    assert probe_cli.PROBE_MESSAGES["synthetic-only-required"] in capsys.readouterr().err
    assert not cli_box.record.exists()


def test_cli_requires_a_profile(capsys):
    with pytest.raises(SystemExit) as info:
        run_cli(["opencode", "probe", "--synthetic-only"])
    assert info.value.code == 2


def test_cli_model_flag(cli_box):
    assert run_cli(probe_argv("--synthetic-only", "--model", "other-model")) == 0
    assert paths.qualification_path("other-model", cli_box.env, cli_box.home).is_file()


def test_cli_project_root_must_be_classified(cli_box, tmp_path, capsys):
    root = _project(tmp_path, {"schema_version": 1, "runtime": "opencode", "profile": PROFILE})
    assert run_cli(probe_argv("--synthetic-only", "--project-root", str(root))) == 2
    assert probe_cli.PROBE_MESSAGES["classification-required"] in capsys.readouterr().err


def test_cli_unknown_profile_is_a_configuration_error(cli_box, capsys):
    assert run_cli(probe_argv("--synthetic-only", profile="missing")) == 2
    err = capsys.readouterr().err
    assert "profile-not-found" in err and probe_cli.LIVE_NOTICE not in err


def test_cli_missing_adapter_data(cli_box, monkeypatch, capsys):
    monkeypatch.setattr(paths, "adapter_data_dir", lambda: None)
    assert run_cli(probe_argv("--synthetic-only")) == 2
    assert "packaged adapter data not found; reinstall quoin" in capsys.readouterr().err


def test_cli_unsafe_directory(cli_box, capsys):
    qdir = cli_box.record.parent
    qdir.mkdir(parents=True)
    os.chmod(qdir, 0o777)
    try:
        assert run_cli(probe_argv("--synthetic-only")) == 2
    finally:
        os.chmod(qdir, 0o700)
    err = capsys.readouterr().err
    assert "not private enough" in err and str(qdir) not in err


def test_cli_filesystem_errors_print_fixed_text(cli_box, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise PermissionError("/hidden/path")

    monkeypatch.setattr(probe_cli, "run", boom)
    assert run_cli(probe_argv("--synthetic-only")) == 2
    err = capsys.readouterr().err
    assert "/hidden/path" not in err and err.strip()


def test_cli_passes_only_configuration_paths_and_a_read_only_environment(cli_box, monkeypatch):
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(probe_cli, "run", spy)
    before = environ_snapshot()
    assert run_cli(probe_argv("--synthetic-only")) == 0
    assert set(seen["env"]) <= set(cli.CONFIG_ENV_KEYS)
    assert isinstance(seen["environ"], types.MappingProxyType)
    assert seen["environ"] is not os.environ
    with pytest.raises(TypeError):
        seen["environ"]["X"] = "1"
    assert environ_snapshot() == before
    assert seen["synthetic_only"] is True and seen["platform"] == sys.platform


def test_cli_help_lists_probe_and_keeps_the_other_commands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["opencode", "--help"])
    out = capsys.readouterr().out
    for word in ("probe", "uninstall", "script", "config"):
        assert word in out
    with pytest.raises(SystemExit):
        cli.main(["opencode", "config", "--help"])
    assert "explain" in capsys.readouterr().out


# ------------------------------------------------------------------ source


def test_module_source_rules():
    src = (helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter" / "probe_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 10))
    assert src.startswith('"""') and "from __future__ import annotations" in src
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "putenv", "unsetenv"):
            raise AssertionError("the module must not touch the process environment")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in ("socket", "http", "ssl", "subprocess", "urllib")
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in ("socket", "http", "ssl", "subprocess", "urllib", "quoin")
