"""Seeded-secret scan across every surface that touches configuration,
credentials or the probe, plus the boundary that keeps secret reads in one
place.

Every scan first asserts the surface produced something (a scan of empty
output proves nothing), then asserts no form of the seeded secret appears.
Everything runs offline against a fake provider on the loopback interface.
"""
from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import NOW, World
from test_opencode_config_errors import SHAPES
from test_opencode_runtime_config import CASES, run_case
from quoin import cli
from quoin.opencode_adapter import compiler, config, jsonio, paths, probe_cli, secrets

fake_server = helpers.load_module(
    helpers.OPENCODE_DIR / "fake_openai_server.py", "fake_openai_server_secret_scan"
)

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
ENV_REF_NAME = "QUOIN_SCAN_ENV_KEY"
KEYCHAIN_REF = "keychain:quoin-scan-service/scan-account-7f3a"
SEEDED_ENV_NAMES = (
    ENV_REF_NAME, "QUOIN_FAKE_API_KEY", "QUOIN_KC_API_KEY", "QUOIN_CORP_API_KEY", "OPENROUTER_API_KEY",
    "QUOIN_CORP_GW_API_KEY", "QUOIN_CORP_GW_B_API_KEY", "QUOIN_LOCAL_GW_API_KEY", "LOCAL_GW_TOKEN",
)
SLUGS = {"opus": "vendor-a/model-large", "sonnet": "vendor-a/model-medium", "haiku": "vendor-b/model-small"}


def environ_snapshot():
    return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}


@pytest.fixture(scope="module", autouse=True)
def environment_is_unchanged_by_the_module():
    before = environ_snapshot()
    yield
    assert environ_snapshot() == before


@pytest.fixture(autouse=True)
def loopback_guard(monkeypatch):
    helpers.install_loopback_guard(monkeypatch)


@pytest.fixture(scope="module", params=[helpers.SEEDED_SECRET, helpers.SEEDED_SECRET_ESCAPED], ids=["plain", "escaped"])
def secret(request):
    return request.param


@pytest.fixture(scope="module")
def server(secret):
    srv = fake_server.FakeProviderServer(expected_token=secret)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def wrong_server():
    srv = fake_server.FakeProviderServer(expected_token="some-other-token-value")
    srv.start()
    yield srv
    srv.stop()


def clean(text, secret, label=""):
    assert text, "empty surface: " + label
    for form in helpers.secret_forms(secret):
        assert form not in text, label


def scan_profile(base_url):
    def provider(url, ref):
        return {
            "kind": "openai-compatible", "endpoint_family": "chat-completions",
            "base_url": url, "credential_ref": ref,
        }

    def model(provider_id, model_id, ref):
        return {"provider": provider_id, "model_id": model_id, "qualification_ref": "local:" + ref}

    return {
        "schema_version": 1, "runtime": "opencode", "profile": "scanwork", "classification": "work",
        "providers": {
            "fake": provider(base_url, "env:" + ENV_REF_NAME),
            "kc": provider(base_url, KEYCHAIN_REF),
            "corp": provider("https://corp.example.invalid/v1", KEYCHAIN_REF),
        },
        "models": {
            "fake-model": model("fake", "default_ok", "fake-model"),
            "kc-model": model("kc", "default_ok", "kc-model"),
            "corp-model": model("corp", "vendor/corp-model", "corp-model"),
        },
        "default_model": "fake-model",
        "auxiliary_model": "fake-model",
    }


@pytest.fixture
def world(tmp_path, server, monkeypatch, secret):
    world = World(tmp_path, profile=scan_profile(server.base_url))
    server.clear_requests()
    monkeypatch.setenv("HOME", str(world.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", world.env["XDG_CONFIG_HOME"])
    monkeypatch.setenv("XDG_STATE_HOME", world.env["XDG_STATE_HOME"])
    monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
    for name in SEEDED_ENV_NAMES:
        monkeypatch.setenv(name, secret)
    monkeypatch.setattr(cli, "datetime", _frozen_datetime())
    return world


def _frozen_datetime():
    from datetime import datetime

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    return Frozen


def cli_run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def project_args(world):
    return ("--profile", "scanwork", "--project-root", str(world.root))


# ------------------------------------------------------------------ explain


def test_explain_surfaces_carry_no_secret(world, capsys, secret):
    texts = {}
    for label, extra in (("plain", ()), ("redact", ("--redact",)), ("json", ("--json",))):
        code, out, err = cli_run(capsys, "opencode", "config", "explain", *project_args(world), *extra)
        assert code in (0, 1), err
        texts[label] = out + err
        clean(texts[label], secret, "explain " + label)
    for hidden in ("scan-account-7f3a", "corp.example.invalid", "127.0.0.1"):
        assert hidden not in texts["redact"], hidden
    assert "corp.example.invalid" in texts["plain"] or "127.0.0.1" in texts["plain"]


# ------------------------------------------------------------------ compile


def test_compile_surfaces_carry_no_secret(world, capsys, secret):
    code, out, err = cli_run(capsys, "opencode", "config", "compile", *project_args(world))
    assert code == 0, err
    clean(out, secret, "compile stdout")
    directory = paths.compiled_output_dir("scanwork", world.root, world.env, world.home)
    native = (directory / "opencode.json").read_text(encoding="utf-8")
    sidecar = (directory / "quoin-compile.json").read_text(encoding="utf-8")
    clean(native, secret, "native file")
    clean(sidecar, secret, "sidecar")
    code, out, err = cli_run(capsys, "opencode", "config", "compile", *project_args(world), "--check")
    assert code == 0
    clean(out + err, secret, "compile --check")
    ev = world.evaluate()
    blob = jsonio.dump_canonical(compiler.digest_input(ev, compiler.build_document(ev))).decode("utf-8")
    clean(blob, secret, "digest input")


@pytest.mark.parametrize("case", CASES["invalid"], ids=[c["id"] for c in CASES["invalid"]])
def test_invalid_fixtures_print_no_secret(case, tmp_path, monkeypatch, capsys, secret):
    env, _ = run_case(case, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", env["XDG_CONFIG_HOME"])
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    if "QUOIN_OPENCODE_MANAGED_POLICY" in env:
        monkeypatch.setenv("QUOIN_OPENCODE_MANAGED_POLICY", env["QUOIN_OPENCODE_MANAGED_POLICY"])
    else:
        monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
    for name in SEEDED_ENV_NAMES:
        monkeypatch.setenv(name, secret)
    profile = case.get("select") or case.get("install_as") or "work"
    for sub in ("explain", "compile"):
        code = cli.main(["opencode", "config", sub, "--profile", profile, "--project-root", str(tmp_path / "project")])
        captured = capsys.readouterr()
        text = captured.out + captured.err
        assert code in (0, 1, 2), case["id"]
        for form in helpers.secret_forms(secret) + list(SHAPES.values()):
            assert form not in text, (case["id"], sub)


# -------------------------------------------------------------------- probe


def set_probe_model(world, model_id):
    world.profile["models"]["fake-model"]["model_id"] = model_id
    world.write()


def record_text(world):
    record = paths.qualification_path("fake-model", world.env, world.home)
    return record.read_text(encoding="utf-8") if record.exists() else ""


@pytest.mark.parametrize(
    "scenario", ["default_ok", "server_error_503", "echo_secret_in_error", "echo_secret_straddle"]
)
def test_probe_surfaces_carry_no_secret(scenario, world, server, capsys, secret):
    set_probe_model(world, scenario)
    code, out, err = cli_run(capsys, "opencode", "probe", "--profile", "scanwork", "--synthetic-only", "--model", "fake-model")
    assert code in (0, 1, 2)
    if scenario == "default_ok":
        assert code == 0 and out.strip() == "qualified"
    requests = server.snapshot_requests()
    assert requests, "the probe never reached the gateway"
    assert all(r["auth_matches_expected"] is True for r in requests)  # the secret really was sent
    clean(out + err, secret, "probe output " + scenario)
    clean(record_text(world), secret, "probe record " + scenario)


def test_a_rejected_credential_run_carries_no_secret(tmp_path, wrong_server, monkeypatch, capsys, secret):
    world = World(tmp_path, profile=scan_profile(wrong_server.base_url))
    for name, value in (("HOME", str(world.home)), ("XDG_CONFIG_HOME", world.env["XDG_CONFIG_HOME"]),
                        ("XDG_STATE_HOME", world.env["XDG_STATE_HOME"])):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
    monkeypatch.setenv(ENV_REF_NAME, secret)
    wrong_server.clear_requests()
    code, out, err = cli_run(capsys, "opencode", "probe", "--profile", "scanwork", "--synthetic-only", "--model", "fake-model")
    assert code == 2
    requests = wrong_server.snapshot_requests()
    assert requests and all(r["auth_matches_expected"] is False for r in requests)
    clean(out + err, secret, "rejected run output")
    clean(record_text(world), secret, "rejected run record")


def test_a_crashing_probe_carries_no_secret(world, capsys, secret):
    class Stub:
        ProbeConfig = probe_cli.load_probe_module().ProbeConfig

        @staticmethod
        def execute(config_, env_, extra_headers=None, now=None, nonce_factory=None):
            raise RuntimeError("boom " + secret)

    code = probe_cli.run(
        profile="scanwork", model="fake-model", project_root=None, synthetic_only=True, env=world.env,
        environ={ENV_REF_NAME: secret}, home=world.home, now=NOW, platform="linux", probe_module=Stub,
    )
    out = capsys.readouterr()
    assert code == 2 and probe_cli.UNEXPECTED_TEXT % "RuntimeError" in out.err
    clean(out.out + out.err, secret, "crash output")


def test_a_keychain_probe_carries_no_secret(world, server, capsys, secret):
    seen = []

    def runner(argv, timeout):
        seen.append(list(argv))
        return 0, (secret + "\n").encode("utf-8")

    code = probe_cli.run(
        profile="scanwork", model="kc-model", project_root=None, synthetic_only=True, env=world.env,
        environ={}, home=world.home, now=NOW, platform="darwin", runner=runner,
    )
    out = capsys.readouterr()
    assert code == 0 and seen and server.snapshot_requests()
    assert all(r["auth_matches_expected"] is True for r in server.snapshot_requests())
    assert secret not in " ".join(seen[0])
    clean(out.out + out.err, secret, "keychain probe output")
    record = paths.qualification_path("kc-model", world.env, world.home).read_text(encoding="utf-8")
    clean(record, secret, "keychain probe record")
    assert "scan-account-7f3a" not in out.out + out.err


def test_probe_refusals_and_credential_failures_carry_no_secret(world, capsys, secret):
    for kwargs in (
        dict(synthetic_only=False),
        dict(model="nonexistent"),
        dict(environ={}),
        dict(model="kc-model", platform="linux"),
    ):
        args = dict(
            profile="scanwork", model="fake-model", project_root=None, synthetic_only=True, env=world.env,
            environ={ENV_REF_NAME: secret}, home=world.home, now=NOW, platform="linux",
        )
        args.update(kwargs)
        assert probe_cli.run(**args) == 2
        out = capsys.readouterr()
        clean(out.out + out.err, secret, "refusal %s" % sorted(kwargs))
        assert "scan-account-7f3a" not in out.err


# ----------------------------------------------------------- import preview


def test_import_preview_surfaces_carry_no_secret(tmp_path, monkeypatch, capsys, secret):
    home = tmp_path / "home"
    (home / ".config" / "quoin").mkdir(parents=True)
    (home / ".config" / "quoin" / "models.json").write_text(json.dumps(SLUGS), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdgcfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "xdgstate"))
    monkeypatch.delenv("QUOIN_OPENCODE_MANAGED_POLICY", raising=False)
    for name in SEEDED_ENV_NAMES:
        monkeypatch.setenv(name, secret)
    boom_calls = []

    def trap(*args, **kwargs):
        boom_calls.append(1)
        raise AssertionError("a credential was resolved")

    for owner, name in ((secrets.EnvBackend, "resolve"), (secrets.MacKeychainBackend, "resolve"),
                        (secrets.CredentialResolver, "resolve")):
        monkeypatch.setattr(owner, name, trap)
    code, out, err = cli_run(capsys, "opencode", "config", "import-preview")
    assert code == 0
    clean(out + err, secret, "preview")
    ids = sorted(set(SLUGS.values()))
    args = ["opencode", "config", "import-preview", "--apply"]
    for one in ids:
        args += ["--confirm-model-id", one]
    code, out, err = cli_run(capsys, *args)
    assert code == 0, err
    clean(out + err, secret, "apply")
    target = paths.profile_path("personal", {"XDG_CONFIG_HOME": str(home / "xdgcfg")}, home)
    clean(target.read_text(encoding="utf-8"), secret, "applied profile")
    assert not boom_calls


# ---------------------------------------------------------- resolver errors


def _runner_failures(secret):
    held = secret.encode("utf-8")
    return {
        "keychain-failed": lambda argv, timeout: (1, held),
        "keychain-not-found": lambda argv, timeout: (44, held),
        "garbage": lambda argv, timeout: held,
    }


def test_resolution_errors_never_render_the_secret(secret):
    refs = [secrets.parse("env:" + ENV_REF_NAME), secrets.parse(KEYCHAIN_REF)]
    rendered = []
    for code in sorted(secrets.RESOLUTION_CODES):
        for ref in refs:
            exc = secrets.SecretResolutionError(code, ref)
            rendered += [str(exc), repr(exc), exc.render(True), exc.render(False)]
    for runner in _runner_failures(secret).values():
        try:
            secrets.default_resolver({}, platform="darwin", runner=runner).resolve(refs[1])
        except secrets.SecretResolutionError as exc:
            rendered += [str(exc), repr(exc), exc.render(True), exc.render(False)]
    for missing in ({}, {ENV_REF_NAME: ""}):
        try:
            secrets.default_resolver(missing, platform="linux").resolve(refs[0])
        except secrets.SecretResolutionError as exc:
            rendered += [str(exc), repr(exc), exc.render(True), exc.render(False)]
    clean("\n".join(rendered), secret, "resolution errors")
    masked = [secrets.SecretResolutionError("keychain-failed", refs[1]).render(True), str(secrets.SecretResolutionError("keychain-failed", refs[1]))]
    assert all("scan-account-7f3a" not in text for text in masked)


def test_a_resolved_secret_value_never_renders(secret):
    value = secrets.SecretValue(secret)
    clean("%s %r %s" % (value, value, "{}".format(value)), secret, "secret value")


# ---------------------------------------------------------------- log records


def test_log_records_carry_no_secret(world, capsys, caplog, secret):
    with caplog.at_level(logging.DEBUG):
        cli_run(capsys, "opencode", "config", "explain", *project_args(world))
        cli_run(capsys, "opencode", "config", "compile", *project_args(world))
        cli_run(capsys, "opencode", "probe", "--profile", "scanwork", "--synthetic-only", "--model", "fake-model")
    assert "quoin.opencode.roles" in {r.name for r in caplog.records}
    clean(caplog.text + "\n".join(str(r.args) for r in caplog.records), secret, "log records")


# ---------------------------------------------------------- secret boundary


RESOLVER_NAMES = {"default_resolver", "CredentialResolver", "EnvBackend", "MacKeychainBackend"}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))


def test_only_the_probe_wiring_resolves_and_reveals_secrets():
    modules = sorted(SRC_DIR.glob("*.py"))
    assert len(modules) > 15
    reveal_calls = {}
    for path in modules:
        tree = _tree(path)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        used = set()
        reveals = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in RESOLVER_NAMES:
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                if node.attr in RESOLVER_NAMES:
                    used.add(node.attr)
                if node.attr == "reveal":
                    reveals += 1
        if path.name == "secrets.py":
            assert RESOLVER_NAMES <= defined
            continue
        assert not (defined & RESOLVER_NAMES), path.name
        if path.name != "probe_cli.py":
            assert not used, (path.name, used)
            assert reveals == 0, path.name
        reveal_calls[path.name] = reveals
    assert reveal_calls["probe_cli.py"] == 1
    assert {name for name, count in reveal_calls.items() if count} == {"probe_cli.py"}
    probe_uses = {
        node.attr for node in ast.walk(_tree(SRC_DIR / "probe_cli.py")) if isinstance(node, ast.Attribute)
    } & RESOLVER_NAMES
    assert probe_uses == {"default_resolver"}
