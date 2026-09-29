"""Tests for runtime configuration loading: fixtures, error enumeration,
semantic checks, cross-layer rules, offline traps and drift guards."""
from __future__ import annotations

import ast
import copy
import json
import socket
import subprocess
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from test_opencode_config_errors import SHAPES
from quoin.opencode_adapter import config, errors, generate, install, paths, secrets as refs
from quoin.opencode_adapter.errors import ConfigErrors

SOURCE_DIR = helpers.SOURCE_DIR
FIXTURES = SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "runtime-config"
SCHEMA_PATH = SOURCE_DIR / "adapters" / "opencode" / "schemas" / "runtime-config.schema.json"
SRC_DIR = SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
ALLOWED_ENV_READS = {"XDG_CONFIG_HOME", "XDG_STATE_HOME", "QUOIN_OPENCODE_MANAGED_POLICY"}
NEW_MODULES = ("errors", "paths", "jsonio", "schema_check", "secrets", "config")


class RecordingEnv(dict):
    """Environment mapping that records every key read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.reads.append(key)
        return super().__contains__(key)


def read_fixture(rel):
    return (FIXTURES / rel).read_text(encoding="utf-8")


def _sandbox(tmp_path):
    env = RecordingEnv(
        {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "XDG_STATE_HOME": str(tmp_path / "state")}
    )
    return env, tmp_path / "home", tmp_path / "project"


def _install_profile(env, home, text, name):
    target = paths.profile_path(name, env, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def run_case(case, tmp_path):
    """Place a fixture at its real location and call the public loader."""
    env, home, project = _sandbox(tmp_path)
    text = read_fixture(case["file"])
    splice = case.get("splice")
    if splice:
        text = text.replace(splice["marker"], SHAPES[{"sk": "sk-proj", "ghp": "ghp", "akia": "akia",
                            "xox": "xox", "bearer": "bearer", "jwt": "jwt"}[splice["shape"]]])
    for comp in (case.get("companions") or {}).values():
        comp_text = read_fixture(comp)
        _install_profile(env, home, comp_text, json.loads(comp_text)["profile"])
    layer = case["layer"]
    if layer == "managed":
        target = tmp_path / "managed.json"
        target.write_text(text, encoding="utf-8")
        env["QUOIN_OPENCODE_MANAGED_POLICY"] = str(target)
        return env, lambda: config.load_managed(env)
    if layer == "profile":
        name = case["install_as"]
        _install_profile(env, home, text, name)
        select = case.get("select") or name
    else:
        (project / ".quoin").mkdir(parents=True)
        (project / ".quoin" / "runtime.json").write_text(text, encoding="utf-8")
        select = case.get("select")
    return env, lambda: config.load_all(project_root=project, profile=select, env=env, home=home)


@pytest.fixture
def offline_traps(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network or subprocess use is not allowed while loading config")

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


# --------------------------------------------------------------- inventory


def test_cases_file_shape():
    assert CASES["version"] == 1
    ids = [c["id"] for c in CASES["valid"] + CASES["invalid"]]
    assert len(ids) == len(set(ids))
    for case in CASES["valid"]:
        assert set(case) <= {"id", "file", "layer", "jsonschema_valid", "install_as", "companions",
                             "classification_state", "select"}
        assert case["layer"] in ("profile", "project", "managed")
    for case in CASES["invalid"]:
        assert set(case) <= {"id", "file", "layer", "expected_class", "expected_path",
                             "expected_file_label", "jsonschema_valid", "install_as", "companions",
                             "select", "splice", "offending_value", "note"}
        assert case["jsonschema_valid"] in (True, False, None)
        assert case["layer"] in ("profile", "project", "managed")


def test_every_load_class_has_a_case_and_classes_are_known():
    used = {c["expected_class"] for c in CASES["invalid"]}
    assert errors.LOAD_CLASSES <= used
    assert used <= errors.REJECTION_CLASSES


def test_inventory_is_bidirectional():
    referenced = {c["file"] for c in CASES["valid"] + CASES["invalid"]}
    for case in CASES["valid"] + CASES["invalid"]:
        referenced |= set((case.get("companions") or {}).values())
    on_disk = {
        str(p.relative_to(FIXTURES)) for p in FIXTURES.rglob("*") if p.is_file()
    } - {"cases.json"}
    assert on_disk == referenced
    assert on_disk


def test_fixtures_hold_no_secret_shapes_and_are_plain_utf8():
    for path in FIXTURES.rglob("*.json"):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8")
        assert not errors.SECRET_SHAPE_RE.search(text), path


# ------------------------------------------------- enumeration and valid


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda c: c["id"])
def test_invalid_case(case, tmp_path, offline_traps):
    env, call = run_case(case, tmp_path)
    with pytest.raises(ConfigErrors) as info:
        call()
    exc = info.value
    errs = exc.errors
    assert [e.rejection_class for e in errs] == [case["expected_class"]]
    assert errs[0].json_path == case["expected_path"]
    assert errs[0].file == case["expected_file_label"]
    assert errs[0].fix
    text = str(errs[0])
    assert errs[0].file in text and errs[0].json_path in text and errs[0].fix in text
    secrets_seen = []
    if case.get("offending_value"):
        secrets_seen.append(case["offending_value"])
    if case.get("splice"):
        secrets_seen.append(SHAPES["sk-proj"])
    for value in secrets_seen:
        assert value not in str(exc)
        assert value not in repr(exc)
        for err in errs:
            for field_value in (err.rejection_class, err.file, err.json_path, err.message_id,
                                err.message, err.fix, str(err), repr(err), repr(err.params)):
                assert value not in field_value
    assert set(env.reads) <= ALLOWED_ENV_READS


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda c: c["id"])
def test_valid_case(case, tmp_path, offline_traps):
    env, call = run_case(case, tmp_path)
    result = call()
    layer = result if case["layer"] == "managed" else getattr(result, case["layer"])
    assert layer is not None
    if "classification_state" in case:
        assert layer.classification_state == case["classification_state"]
    assert set(env.reads) <= ALLOWED_ENV_READS
    for name in env.reads:
        assert not name.endswith("_API_KEY")


# ------------------------------------------------------------ unit helpers


def base_profile():
    return json.loads(read_fixture("valid/profile-work.json"))


def load_profile_data(tmp_path, data, name="work"):
    env, home, _ = _sandbox(tmp_path)
    _install_profile(env, home, json.dumps(data), name)
    return config.load_profile(name, env=env, home=home)


def failures(tmp_path, data, name="work"):
    with pytest.raises(ConfigErrors) as info:
        load_profile_data(tmp_path, data, name)
    return [(e.rejection_class, e.json_path) for e in info.value.errors]


def set_url(data, url):
    data["providers"]["corp-gw"]["base_url"] = url
    return data


def test_base_profile_loads(tmp_path):
    layer = load_profile_data(tmp_path, base_profile())
    assert layer.kind == "profile" and layer.file == "profiles/work.json"
    assert layer.classification_state == "work"


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda d: d["providers"]["corp-gw"].__setitem__("kind", "x"),
         [("invalid-type", "$.providers.corp-gw.kind")]),
        (lambda d: d.pop("default_model"), [("invalid-type", "$")]),
        (lambda d: d.__setitem__("classification", "other"), [("invalid-type", "$.classification")]),
        (lambda d: d["models"]["work-coder"].__setitem__("qualification_ref", "remote:x"),
         [("invalid-type", "$.models.work-coder.qualification_ref")]),
        (lambda d: d["policy"].__setitem__("sharing", "always"), [("invalid-type", "$.policy.sharing")]),
        (lambda d: d["integrations"].__setitem__("enabled", ["jira", "jira"]),
         [("invalid-type", "$.integrations.enabled")]),
        (lambda d: d["integrations"].__setitem__("mode", "write"), [("invalid-type", "$.integrations.mode")]),
        (lambda d: d["policy"].__setitem__("allowed_hosts", ["bad host"]),
         [("invalid-type", "$.policy.allowed_hosts[0]")]),
        (lambda d: d["policy"].__setitem__("allowed_hosts", ["gateway.example.invalid\n"]),
         [("invalid-type", "$.policy.allowed_hosts[0]")]),
        (lambda d: d["policy"].__setitem__("denied_providers", ["unknown-but-fine"]), []),
        (lambda d: d["providers"].__setitem__("Bad", copy.deepcopy(d["providers"]["corp-gw"])),
         [("invalid-type", "$.providers.Bad")]),
        (lambda d: d.__setitem__("default_model", "work-coder\n"), [("invalid-type", "$.default_model")]),
        (lambda d: d["providers"]["corp-gw"].__setitem__("credential_ref", "env:GOOD\n"),
         [("inline-credential", "$.providers.corp-gw.credential_ref")]),
    ],
)
def test_single_mutations(tmp_path, mutate, expected):
    data = base_profile()
    mutate(data)
    if not expected:
        load_profile_data(tmp_path, data)
    else:
        assert failures(tmp_path, data) == expected


def test_placeholder_outranks_other_findings(tmp_path):
    data = base_profile()
    data["providers"]["corp-gw"]["credential_ref"] = "{env:X}"
    assert failures(tmp_path, data) == [
        ("unresolved-placeholder", "$.providers.corp-gw.credential_ref")
    ]


def test_placeholder_markers(tmp_path):
    for marker in ("REPLACE_WITH_X", "${X}", "{env:X}", "{file:x}"):
        data = base_profile()
        data["providers"]["corp-gw"]["name"] = "a" + marker
        assert failures(tmp_path, data) == [
            ("unresolved-placeholder", "$.providers.corp-gw.name")
        ]
    data = base_profile()
    data["providers"]["corp-gw"]["{env:K}"] = 1
    assert failures(tmp_path, data)[0][0] == "unresolved-placeholder"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_secret_shapes_in_values_and_keys(tmp_path, shape):
    data = base_profile()
    data["providers"]["corp-gw"]["name"] = SHAPES[shape]
    assert failures(tmp_path, data) == [("inline-credential", "$.providers.corp-gw.name")]
    data = base_profile()
    data["providers"]["corp-gw"][SHAPES[shape]] = 1
    assert failures(tmp_path, data) == [("inline-credential", '$.providers.corp-gw["*"]')]


LOOPBACK_OK = ["http://127.0.0.1", "http://[::1]:8080", "http://LOCALHOST", "http://localhost:9/v1"]
LOOPBACK_BAD = ["http://127.0.0.2", "http://localhost.", "http://0.0.0.0", "http://[::ffff:127.0.0.1]"]


@pytest.mark.parametrize("url", LOOPBACK_OK)
def test_loopback_http_accepted(tmp_path, url):
    load_profile_data(tmp_path, set_url(base_profile(), url))


@pytest.mark.parametrize("url", LOOPBACK_BAD)
def test_non_loopback_http_rejected(tmp_path, url):
    assert failures(tmp_path, set_url(base_profile(), url)) == [
        ("insecure-http", "$.providers.corp-gw.base_url")
    ]


URL_CASES = [
    ("https://user@gateway.example.invalid/v1", "url-credentials"),
    ("https://gateway.example.invalid/v1?x=1", "url-credentials"),
    ("https://gateway.example.invalid/v1#frag", "url-credentials"),
    ("https://gateway.example.invalid:99999/v1", "invalid-url"),
    ("https://gateway.example.invalid:abc/v1", "invalid-url"),
    ("ftp://gateway.example.invalid/v1", "invalid-url"),
    ("https:///v1", "invalid-url"),
    ("https://gateway.example.invalid/v1 x", "invalid-url"),
    ("https://gateway.example.invalid/\tv1", "invalid-url"),
    ("gateway.example.invalid", "invalid-url"),
    ("https://evil.example.invalid\\gateway.example.invalid/v1", "invalid-url"),
    ("https://gateway.example.invalid/v1\\x", "invalid-url"),
    ("https://%65vil.example.invalid/v1", "invalid-url"),
    ("https://gateway.example.invalid:0/v1", "invalid-url"),
    ("https://gateway.example.invalid\x7f/v1", "invalid-url"),
    ("https://g\u00e4teway.example.invalid/v1", "invalid-url"),
    ("https://[fe80::1%25eth0]/v1", "invalid-url"),
    ("https://gateway_.example.invalid/v1", "invalid-url"),
]


@pytest.mark.parametrize("url,cls", URL_CASES)
def test_url_rejections(tmp_path, url, cls):
    assert failures(tmp_path, set_url(base_profile(), url)) == [
        (cls, "$.providers.corp-gw.base_url")
    ]


def test_qualification_ref_trailing_newline_rejected(tmp_path):
    data = base_profile()
    data["models"]["work-coder"]["qualification_ref"] = "local:work-coder\n"
    assert failures(tmp_path, data) == [("invalid-type", "$.models.work-coder.qualification_ref")]


def test_ipv6_literal_host_accepted(tmp_path):
    load_profile_data(tmp_path, set_url(base_profile(), "https://[2001:db8::1]:8443/v1"))


def test_userinfo_with_password_built_at_run_time(tmp_path):
    url = "https://" + "user" + ":" + "pw" + "@gateway.example.invalid/v1"
    assert failures(tmp_path, set_url(base_profile(), url)) == [
        ("url-credentials", "$.providers.corp-gw.base_url")
    ]


@pytest.fixture(scope="module")
def probe():
    return helpers.load_module(helpers.OPENCODE_DIR / "probe_gateway.py", "probe_gateway_config_parity")


PARITY_URLS = (
    LOOPBACK_OK + LOOPBACK_BAD
    + [u for u, _ in URL_CASES]
    + [
        "https://gateway.example.invalid/v1",
        "https://GW.example.invalid/v1/",
        "https://gateway.example.invalid:443/v1",
        "https://gateway.example.invalid:8443/v1",
        "https://gateway.example.invalid",
        "http://localhost",
    ]
)


def _probe_accepts(probe, url):
    cfg = probe.ProbeConfig(base_url=url, model="m", credential_env="X", provider="p")
    try:
        probe.validate_config(cfg)
    except probe.ProbeConfigError:
        return False
    return True


def test_url_parity_with_probe(probe):
    for url in PARITY_URLS:
        mine = config._check_base_url(url, ("x",), "l") is None
        theirs = _probe_accepts(probe, url)
        if mine:
            assert theirs, url
        if url.startswith("http://"):
            assert mine == theirs, url


def test_endpoint_normaliser_parity_with_probe(probe):
    for url in PARITY_URLS:
        try:
            expected = probe.endpoint_identity(url)
        except ValueError:
            with pytest.raises(ValueError):
                config._endpoint_identity(url)
            continue
        assert config._endpoint_identity(url) == expected, url


def test_env_name_collision_variants(tmp_path):
    data = base_profile()
    data["providers"]["a-b"] = copy.deepcopy(data["providers"]["corp-gw"])
    data["providers"]["a_b"] = copy.deepcopy(data["providers"]["corp-gw"])
    assert failures(tmp_path, data) == [("env-name-collision", "$.providers.a_b")]
    assert config.provider_env_name("corp-gw") == "QUOIN_CORP_GW_API_KEY"


def test_profile_stem_mismatch(tmp_path):
    data = base_profile()
    data["profile"] = "other"
    assert failures(tmp_path, data) == [("invalid-profile-name", "$.profile")]


def test_load_profile_name_checks(tmp_path):
    env, home, _ = _sandbox(tmp_path)
    with pytest.raises(ConfigErrors) as info:
        config.load_profile("Bad Name", env=env, home=home)
    assert info.value.errors[0].rejection_class == "invalid-profile-name"
    assert "Bad Name" not in str(info.value)
    with pytest.raises(ConfigErrors) as info:
        config.load_profile("ghost", env=env, home=home)
    assert info.value.errors[0].rejection_class == "profile-not-found"


@pytest.mark.parametrize(
    "value,state",
    [("work", "work"), ("personal", "personal"), ("other", "unknown"), (1, "unknown"),
     (None, "unknown"), ([], "unknown"), ({}, "unknown")],
)
def test_classification_state_matrix(tmp_path, value, state):
    env, home, project = _sandbox(tmp_path)
    (project / ".quoin").mkdir(parents=True)
    data = json.loads(read_fixture("valid/project-work.json"))
    data["classification"] = value
    path = project / ".quoin" / "runtime.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    layer = config.load_layer(path, "project", file_label=".quoin/runtime.json")
    assert layer.classification_state == state


def test_managed_and_project_layers_strip_endpoint_keys(tmp_path):
    env, home, project = _sandbox(tmp_path)
    (project / ".quoin").mkdir(parents=True)
    data = json.loads(read_fixture("valid/project-work.json"))
    data["providers"] = {"x": {}}
    data["default_model"] = "x"
    path = project / ".quoin" / "runtime.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    layer = config.load_layer(path, "project", file_label=".quoin/runtime.json")
    assert set(layer.declared_endpoint) == {"providers", "default_model"}
    assert "providers" not in layer.data


# ----------------------------------------------------------- endpoint matrix


def project_with(tmp_path, declared, name="work"):
    env, home, project = _sandbox(tmp_path)
    _install_profile(env, home, json.dumps(base_profile()), "work")
    (project / ".quoin").mkdir(parents=True, exist_ok=True)
    data = json.loads(read_fixture("valid/project-work.json"))
    data.update(declared)
    (project / ".quoin" / "runtime.json").write_text(json.dumps(data), encoding="utf-8")
    return env, home, project


def cross(tmp_path, declared):
    env, home, project = project_with(tmp_path, declared)
    try:
        config.load_all(project_root=project, env=env, home=home)
    except ConfigErrors as exc:
        return [(e.rejection_class, e.json_path) for e in exc.errors]
    return []


def provider_entry(**changes):
    entry = copy.deepcopy(base_profile()["providers"]["corp-gw"])
    entry.update(changes)
    return entry


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://gateway.example.invalid/v2"},
        {"base_url": "https://gateway.example.invalid/v1/"},
        {"base_url": "https://gateway.example.invalid:443/v1"},
        {"kind": "openrouter"},
        {"credential_ref": "env:OTHER_NAME"},
        {"base_url": "https://gateway.example.invalid:99999/v1"},
    ],
)
def test_identity_change_variants(tmp_path, changes):
    got = cross(tmp_path, {"providers": {"corp-gw": provider_entry(**changes)}})
    assert got == [("endpoint-identity-change", "$.providers.corp-gw")]


def test_identity_change_for_malformed_value(tmp_path):
    assert cross(tmp_path, {"providers": {"corp-gw": 5}}) == [
        ("endpoint-identity-change", "$.providers.corp-gw")
    ]


def test_host_case_is_not_an_identity_change(tmp_path):
    entry = provider_entry(base_url="https://GATEWAY.example.invalid/v1")
    assert cross(tmp_path, {"providers": {"corp-gw": entry}}) == [
        ("project-declares-endpoint", "$.providers.corp-gw")
    ]


def test_same_identity_new_id_and_empty_forms(tmp_path):
    assert cross(tmp_path, {"providers": {"corp-gw": provider_entry()}}) == [
        ("project-declares-endpoint", "$.providers.corp-gw")
    ]
    assert cross(tmp_path, {"providers": {"new-gw": provider_entry()}}) == [
        ("project-declares-endpoint", "$.providers.new-gw")
    ]
    assert cross(tmp_path, {"providers": {}}) == [("project-declares-endpoint", "$.providers")]
    assert cross(tmp_path, {"models": {}}) == [("project-declares-endpoint", "$.models")]
    assert cross(tmp_path, {"default_model": "work-coder"}) == [
        ("project-declares-endpoint", "$.default_model")
    ]


def test_secret_inside_redeclared_provider_fails_at_load(tmp_path):
    entry = provider_entry(name=SHAPES["sk-or"])
    got = cross(tmp_path, {"providers": {"corp-gw": entry}})
    assert got == [("inline-credential", "$.providers.corp-gw.name")]


def test_project_allow_list_and_roles_are_checked_against_profile(tmp_path):
    got = cross(tmp_path, {"policy": {"allowed_providers": ["missing"]}})
    assert got == [("dangling-reference", "$.policy.allowed_providers[0]")]
    got = cross(tmp_path, {"roles": {"critic": {"model": "missing"}}})
    assert got == [("dangling-reference", "$.roles.critic.model")]


def test_cross_layer_errors_are_reported_together(tmp_path):
    got = cross(
        tmp_path,
        {"roles": {"critic": {"model": "missing"}}, "models": {}},
    )
    assert sorted(got) == [
        ("dangling-reference", "$.roles.critic.model"),
        ("project-declares-endpoint", "$.models"),
    ]


# ---------------------------------------------------------------- load_all


def test_broken_project_stops_before_profile_is_read(tmp_path):
    env, home, project = _sandbox(tmp_path)
    (project / ".quoin").mkdir(parents=True)
    (project / ".quoin" / "runtime.json").write_text("{", encoding="utf-8")
    with pytest.raises(ConfigErrors) as info:
        config.load_all(project_root=project, env=env, home=home)
    assert info.value.errors[0].file == ".quoin/runtime.json"
    assert not any(r.startswith("QUOIN_") and r != "QUOIN_OPENCODE_MANAGED_POLICY" for r in env.reads)


def test_profile_argument_beats_project_field(tmp_path):
    env, home, project = project_with(tmp_path, {})
    other = base_profile()
    other["profile"] = "alt"
    _install_profile(env, home, json.dumps(other), "alt")
    loaded = config.load_all(project_root=project, profile="alt", env=env, home=home)
    assert loaded.profile.data["profile"] == "alt"
    assert config.load_all(project_root=project, env=env, home=home).profile.data["profile"] == "work"


def test_no_profile_selected(tmp_path):
    env, home, project = _sandbox(tmp_path)
    with pytest.raises(ConfigErrors) as info:
        config.load_all(project_root=project, env=env, home=home)
    err = info.value.errors[0]
    assert (err.rejection_class, err.message_id) == ("profile-not-found", "no-profile-selected")


def test_managed_unreadable_fails_closed(tmp_path):
    env = RecordingEnv({"QUOIN_OPENCODE_MANAGED_POLICY": str(tmp_path / "absent.json")})
    with pytest.raises(ConfigErrors) as info:
        config.load_managed(env)
    err = info.value.errors[0]
    assert (err.rejection_class, err.message_id, err.file) == (
        "invalid-json", "managed-policy-unreadable", "managed policy"
    )
    assert str(tmp_path) not in str(info.value)
    assert config.load_managed(RecordingEnv()) is None


def test_env_reads_are_limited(tmp_path):
    env, home, project = project_with(tmp_path, {})
    config.load_all(project_root=project, env=env, home=home)
    assert set(env.reads) <= ALLOWED_ENV_READS


# ----------------------------------------------------------- drift guards


def _schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_role_enum_matches_generator_roles():
    assert _schema()["$defs"]["role_name"]["enum"] == list(generate.ROLES)


def test_effort_enum_is_pinned():
    assert _schema()["$defs"]["effort"]["enum"] == ["low", "medium", "high", "max"]


IDENTIFIERS = ["a", "work", "a" * 63, "a" * 64, "Work", "-a", "_a", "a b", "a\n", "", "0a", "a_b-c"]


@pytest.mark.parametrize("name", IDENTIFIERS)
def test_identifier_pattern_matches_install_profile_re(name):
    import re

    pattern = _schema()["$defs"]["identifier"]["pattern"]
    schema_ok = bool(re.search(pattern, name))
    if name.endswith("\n"):
        assert schema_ok  # search-style anchors accept a trailing newline; config rechecks
        assert not install.PROFILE_RE.fullmatch(name)
    else:
        assert schema_ok == bool(install.PROFILE_RE.fullmatch(name))


def test_schema_pattern_for_credentials_matches_grammar():
    assert _schema()["$defs"]["credential_ref"]["pattern"] == refs.CREDENTIAL_REF_PATTERN


# -------------------------------------------------- module hygiene (AST)

_FORBIDDEN_IMPORTS = ("socket", "http", "urllib.request", "ssl", "subprocess", "quoin.cli")
_NEWER_NAMES = ("tomllib", "ExceptionGroup", "Self")


def _module_tree(name):
    src = (SRC_DIR / (name + ".py")).read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 10))
    return ast.parse(src)


@pytest.mark.parametrize("name", NEW_MODULES)
def test_module_python_floor_and_purity(name):
    tree = _module_tree(name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(_FORBIDDEN_IMPORTS) or alias.name == "urllib.parse", alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                assert not module.startswith(_FORBIDDEN_IMPORTS), module
            assert module not in _NEWER_NAMES
        elif isinstance(node, ast.Name):
            assert node.id not in _NEWER_NAMES
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                assert node.attr not in ("environ", "getenv", "getenvb")
            assert node.attr not in _NEWER_NAMES
    first = tree.body[1] if isinstance(tree.body[0], ast.Expr) else tree.body[0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"


def test_config_imports_are_limited():
    allowed_relative = {"jsonio", "paths", "schema_check", "secrets", "errors", "generate", "install"}
    tree = _module_tree("config")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names = {node.module} if node.module else {a.name for a in node.names}
            assert names <= allowed_relative, names
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert "probe_gateway" not in (node.module or "")
        if isinstance(node, ast.Import):
            assert all("probe_gateway" not in a.name for a in node.names)
