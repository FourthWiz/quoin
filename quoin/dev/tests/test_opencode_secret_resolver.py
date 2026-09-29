"""Tests for use-time secret resolution: the redacting value wrapper, the
environment and keychain backends, the registry and the error hygiene of
every failure path (no value, no subprocess output, no chained exception)."""
from __future__ import annotations

import ast
import copy
import json
import pickle
import subprocess
import traceback
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from quoin.opencode_adapter import secrets
from quoin.opencode_adapter.secrets import (
    CredentialRef,
    CredentialResolver,
    EnvBackend,
    MacKeychainBackend,
    SecretResolutionError,
    SecretValue,
    default_resolver,
    parse,
)
from test_opencode_runtime_config import RecordingEnv

SEED = helpers.SEEDED_SECRET
FORMS = helpers.secret_forms(SEED)
SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
KEYCHAIN_REF = "keychain:quoin/work-gateway"


def has_seed(text):
    return any(form in text for form in FORMS)


# ------------------------------------------------------------ SecretValue


def test_secret_value_never_prints():
    v = SecretValue(SEED)
    for text in (
        repr(v), str(v), "%s" % v, "%r" % v, f"{v}", f"{v!r}", f"{v:>20}",
        format(v, ""), "{}".format(v), "{!r}".format(v), str([v]), str({"k": v}),
    ):
        assert not has_seed(text)
        assert "redacted" in text
    assert v.reveal() == SEED


def test_secret_value_json_pickle_copy_and_vars():
    v = SecretValue(SEED)
    with pytest.raises(TypeError):
        json.dumps({"k": v})
    assert json.loads(json.dumps({"k": v}, default=str)) == {"k": "<redacted>"}
    for protocol in range(0, 6):
        with pytest.raises(TypeError):
            pickle.dumps(v, protocol=protocol)
    with pytest.raises(TypeError):
        copy.copy(v)
    with pytest.raises(TypeError):
        copy.deepcopy(v)
    with pytest.raises(TypeError):
        vars(v)
    assert not hasattr(v, "__dict__")


def test_secret_value_equality_is_identity_based():
    a, b = SecretValue(SEED), SecretValue(SEED)
    assert a != b and a == a
    assert hash(a) != hash(b) or a is not b
    assert {a: 1}[a] == 1


# ------------------------------------------------------------ env backend


def test_env_backend_reads_only_the_named_variable():
    env = RecordingEnv({"A_KEY": SEED, "OTHER": "x"})
    value = EnvBackend(env).resolve(parse("env:A_KEY"))
    assert value.reveal() == SEED
    assert env.reads == ["A_KEY"]


@pytest.mark.parametrize("env,code", [({}, "env-missing"), ({"A_KEY": ""}, "env-empty")])
def test_env_backend_errors(env, code):
    ref = parse("env:A_KEY")
    with pytest.raises(SecretResolutionError) as info:
        EnvBackend(env).resolve(ref)
    err = info.value
    assert err.code == code and err.ref == ref
    assert "A_KEY" in str(err)
    assert err.__cause__ is None and err.__context__ is None


# ------------------------------------------------------------- mock runner


class Recorder:
    def __init__(self, rc=0, out=b"", raises=None):
        self.calls = []
        self._rc, self._out, self._raises = rc, out, raises

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self._raises is not None:
            raise self._raises()
        return self._rc, self._out


def backend(runner, platform="darwin", **kw):
    return MacKeychainBackend(runner=runner, platform=platform, **kw)


def test_keychain_success_strips_exactly_one_newline():
    rec = Recorder(0, SEED.encode() + b"\n")
    value = backend(rec, timeout=7.0).resolve(parse(KEYCHAIN_REF))
    assert value.reveal() == SEED
    assert rec.calls == [
        (["/usr/bin/security", "find-generic-password", "-s", "quoin", "-a", "work-gateway", "-w"], 7.0)
    ]
    assert backend(Recorder(0, b"abc\n\n")).resolve(parse(KEYCHAIN_REF)).reveal() == "abc\n"
    assert backend(Recorder(0, b"abc")).resolve(parse(KEYCHAIN_REF)).reveal() == "abc"


def test_multi_slash_account_is_one_argv_element():
    rec = Recorder(0, b"v\n")
    backend(rec).resolve(parse("keychain:svc/team/work/key"))
    argv = rec.calls[0][0]
    assert argv[argv.index("-a") + 1] == "team/work/key"
    assert argv[argv.index("-s") + 1] == "svc"


def test_non_darwin_is_unavailable_and_runner_untouched():
    rec = Recorder(0, b"v\n")
    with pytest.raises(SecretResolutionError) as info:
        backend(rec, platform="linux").resolve(parse(KEYCHAIN_REF))
    assert info.value.code == "backend-unavailable"
    assert "use env:NAME or run on macOS" in str(info.value)
    assert rec.calls == []


@pytest.mark.parametrize("ref", ["keychain:-svc/acct", "keychain:svc/-acct"])
def test_leading_hyphen_is_refused_before_the_runner(ref):
    rec = Recorder(0, b"v\n")
    with pytest.raises(SecretResolutionError) as info:
        backend(rec).resolve(parse(ref))
    assert info.value.code == "unsafe-reference"
    assert rec.calls == []


def test_render_hides_the_account_only_when_redacting():
    err = SecretResolutionError("keychain-failed", parse("keychain:svc/acct-name"))
    assert "acct-name" not in err.render(True) and "acct-name" not in str(err)
    assert "acct-name" not in repr(err)
    assert "keychain:svc/acct-name" in err.render(False)
    env_err = SecretResolutionError("env-missing", parse("env:A_KEY"))
    assert "env:A_KEY" in env_err.render(True) and "env:A_KEY" in env_err.render(False)


# (label, runner factory) for every keychain failure path
def _failure_cases():
    seed_bytes = SEED.encode()

    def held_timeout(argv, timeout):
        held = seed_bytes  # a runner whose own frame holds the secret
        raise secrets._RunnerTimeout()

    def held_unavailable(argv, timeout):
        held = seed_bytes
        raise secrets._RunnerUnavailable()

    return [
        ("rc1", Recorder(1, seed_bytes + b"\n"), "keychain-failed"),
        ("rc44", Recorder(44, seed_bytes + b"\n"), "keychain-not-found"),
        ("rc0-empty", Recorder(0, b""), "keychain-failed"),
        ("rc0-newline-only", Recorder(0, b"\n"), "keychain-failed"),
        ("rc0-bad-utf8", Recorder(0, seed_bytes + b"\xff"), "keychain-failed"),
        ("bad-shape", lambda a, t: seed_bytes, "keychain-failed"),
        ("timeout", held_timeout, "keychain-timeout"),
        ("unavailable", held_unavailable, "backend-unavailable"),
        ("timeout-recorder", Recorder(raises=secrets._RunnerTimeout), "keychain-timeout"),
    ]


FAILURES = _failure_cases()


def capture(call):
    try:
        call()
    except SecretResolutionError as exc:
        return exc
    raise AssertionError("expected SecretResolutionError")


def _direct(runner):
    return lambda: backend(runner).resolve(parse(KEYCHAIN_REF))


def _through_registry(runner):
    return lambda: default_resolver({}, platform="darwin", runner=runner).resolve(KEYCHAIN_REF)


def _assert_clean(err):
    assert err.__cause__ is None and err.__context__ is None
    for text in (str(err), repr(err), err.render(True), err.render(False)):
        assert not has_seed(text)
    formatted = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    assert not has_seed(formatted)
    tb = err.__traceback__
    while tb is not None:
        for name, value in tb.tb_frame.f_locals.items():
            assert not has_seed(repr(value)), (tb.tb_frame.f_code.co_name, name)
        tb = tb.tb_next
    captured = "".join(
        traceback.TracebackException(type(err), err, err.__traceback__, capture_locals=True).format()
    )
    assert not has_seed(captured)


@pytest.mark.parametrize("label,runner,code", FAILURES, ids=[f[0] for f in FAILURES])
@pytest.mark.parametrize("via", [_direct, _through_registry], ids=["backend", "registry"])
def test_keychain_failures_are_clean(label, runner, code, via):
    err = capture(via(runner))
    assert err.code == code
    _assert_clean(err)


@pytest.mark.parametrize(
    "result,outcome",
    [
        ((0, b"v\n"), "value"),
        ((0, b"v"), "value"),
        ((0, bytearray(b"v\n")), "value"),
        ((44, b""), "keychain-not-found"),
        ((44, b"junk"), "keychain-not-found"),
        ((1, b"v"), "keychain-failed"),
        ((0, b""), "keychain-failed"),
        ((0, b"\n"), "keychain-failed"),
        ((0, b"\xff\xfe"), "keychain-failed"),
        (None, "keychain-failed"),
        ((0,), "keychain-failed"),
        ((0, b"v", b"x"), "keychain-failed"),
        (["0", b"v"], "keychain-failed"),
        (("0", b"v"), "keychain-failed"),
        ((True, b"v"), "keychain-failed"),
        ((0, "v"), "keychain-failed"),
        ((0, None), "keychain-failed"),
        (object(), "keychain-failed"),
    ],
)
def test_keychain_outcome_table_never_raises(result, outcome):
    got = secrets._keychain_outcome(result)
    if outcome == "value":
        assert isinstance(got, SecretValue) and got.reveal() == "v"
    else:
        assert got == outcome


def test_injected_runner_exceptions_other_than_private_ones_propagate():
    class Boom(Exception):
        pass

    def runner(argv, timeout):
        raise Boom()

    with pytest.raises(Boom):
        backend(runner).resolve(parse(KEYCHAIN_REF))


# --------------------------------------------------------- default runner


def _patch_run(monkeypatch, fake):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return fake(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_default_runner_contract(monkeypatch):
    argv = ["/usr/bin/security", "find-generic-password", "-s", "a", "-a", "b", "-w"]
    calls = _patch_run(
        monkeypatch, lambda a, **k: subprocess.CompletedProcess(a, 0, stdout=b"value\n")
    )
    assert secrets._default_runner(argv, 10.0) == (0, b"value\n")
    (got_argv, kwargs), = calls
    assert got_argv == argv
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["shell"] is False and kwargs["check"] is False and kwargs["timeout"] == 10.0
    assert "env" not in kwargs


def test_default_runner_maps_timeout_and_oserror_without_context(monkeypatch):
    def timeout(a, **k):
        raise subprocess.TimeoutExpired(a, 10, output=SEED.encode(), stderr=SEED.encode())

    _patch_run(monkeypatch, timeout)
    with pytest.raises(secrets._RunnerTimeout) as info:
        secrets._default_runner(["x"], 10.0)
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert info.value.args == ()

    def missing(a, **k):
        raise OSError(2, "no such file")

    _patch_run(monkeypatch, missing)
    with pytest.raises(secrets._RunnerUnavailable) as info:
        secrets._default_runner(["x"], 10.0)
    assert info.value.__context__ is None and info.value.args == ()


def test_default_runner_is_not_called_when_a_runner_is_injected(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("default runner must not run")

    monkeypatch.setattr(secrets, "_default_runner", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert backend(Recorder(0, b"v\n")).resolve(parse(KEYCHAIN_REF)).reveal() == "v"


def test_default_path_end_to_end_timeout_with_partial_output(monkeypatch):
    def timeout(a, **k):
        raise subprocess.TimeoutExpired(a, 10, output=SEED.encode())

    _patch_run(monkeypatch, timeout)
    err = capture(lambda: MacKeychainBackend(platform="darwin").resolve(parse(KEYCHAIN_REF)))
    assert err.code == "keychain-timeout"
    _assert_clean(err)


def test_default_path_end_to_end_success(monkeypatch):
    _patch_run(monkeypatch, lambda a, **k: subprocess.CompletedProcess(a, 0, stdout=SEED.encode() + b"\n"))
    value = MacKeychainBackend(platform="darwin").resolve(parse(KEYCHAIN_REF))
    assert value.reveal() == SEED


def test_default_path_missing_binary_is_unavailable(monkeypatch):
    def missing(a, **k):
        raise FileNotFoundError(2, "no such file")

    _patch_run(monkeypatch, missing)
    err = capture(lambda: MacKeychainBackend(platform="darwin").resolve(parse(KEYCHAIN_REF)))
    assert err.code == "backend-unavailable"
    _assert_clean(err)


# --------------------------------------------------------------- registry


def test_default_resolver_dispatches_by_scheme():
    env = RecordingEnv({"A_KEY": SEED})
    rec = Recorder(0, b"from-keychain\n")
    resolver = default_resolver(env, platform="darwin", runner=rec)
    assert resolver.resolve("env:A_KEY").reveal() == SEED
    assert resolver.resolve(parse(KEYCHAIN_REF)).reveal() == "from-keychain"
    assert len(rec.calls) == 1
    with pytest.raises(ValueError):
        resolver.resolve("vault:x/y")
    with pytest.raises(ValueError):
        resolver.resolve("plain-secret-text")


def test_registry_lists_the_known_backends_and_reports_missing_ones():
    assert set(secrets.BACKENDS) == {"env", "keychain"}
    assert secrets.BACKENDS["env"] is EnvBackend
    assert issubclass(secrets.BACKENDS["keychain"], secrets.SecretResolver)
    err = capture(lambda: CredentialResolver({}).resolve("env:A_KEY"))
    assert err.code == "backend-unavailable"


def test_error_codes_are_closed():
    assert secrets.RESOLUTION_CODES == {
        "env-missing", "env-empty", "keychain-not-found", "keychain-failed",
        "keychain-timeout", "backend-unavailable", "unsafe-reference",
    }


# ------------------------------------------------------------ AST hygiene


def _subprocess_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "subprocess" for a in node.names):
            yield node
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "subprocess":
            yield node
        elif isinstance(node, ast.Name) and node.id == "subprocess":
            yield node


def test_subprocess_is_named_only_inside_default_runner():
    tree = ast.parse((SRC_DIR / "secrets.py").read_text(encoding="utf-8"))
    runner = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_default_runner"]
    assert len(runner) == 1
    inside = {id(n) for n in ast.walk(runner[0])}
    hits = list(_subprocess_nodes(tree))
    assert hits
    assert all(id(n) in inside for n in hits)
    assert any(isinstance(n, ast.Import) for n in hits)
