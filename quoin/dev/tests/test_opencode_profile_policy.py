"""Profile isolation policy: the pure model, the validation of the new
fields, and (further down) the merge through the public loader.

Unit part first: every row of the merge algebra, the defaults table and the
typed view. The semantic checks that depend on the cited built-in id sets are
covered here with the module constants swapped in place.
"""
from __future__ import annotations

import ast
import json
from types import SimpleNamespace

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import PROFILE_WORK, fixture
from quoin.opencode_adapter import config, errors, merge, profile_policy as pp
from quoin.opencode_adapter.errors import ConfigErrors

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"


# ------------------------------------------------------------- enum rows

ENUM_ROWS = [
    # field, layers, default, value, origin, ignored
    ("web_tools", {}, "deny", "deny", ("default",), ()),
    ("web_tools", {"profile": "allow"}, "deny", "allow", ("profile",), ()),
    ("web_tools", {"profile": "allow", "trusted-managed": "deny"}, "deny", "deny", ("trusted-managed",), ("profile",)),
    ("web_tools", {"trusted-managed": "allow"}, "deny", "allow", ("trusted-managed",), ()),
    ("web_tools", {"managed": "allow"}, "deny", "deny", ("default",), ("managed",)),
    ("web_tools", {"project": "allow"}, "deny", "deny", ("default",), ("project",)),
    ("web_tools", {"profile": "allow", "project": "deny"}, "deny", "deny", ("project",), ("profile",)),
    ("web_tools", {"project": "deny"}, "allow", "deny", ("project",), ()),
    ("web_tools", {"profile": "deny", "project": "deny"}, "allow", "deny", ("profile", "project"), ()),
    ("mcp.local", {"profile": "any", "managed": "allow-listed"}, "deny", "allow-listed", ("managed",), ("profile",)),
    ("mcp.local", {"profile": "allow-listed", "project": "any"}, "deny", "allow-listed", ("profile",), ("project",)),
    ("fallback", {"profile": "same-profile", "trusted-managed": "stop"}, "stop", "stop", ("trusted-managed",), ("profile",)),
    ("network.identity_check", {"profile": "off", "managed": "required"}, "required", "required", ("managed",), ("profile",)),
    ("logging.content", {"project": "metadata"}, "summary", "metadata", ("project",), ()),
    ("lsp.mode", {"profile": "builtin", "project": "allow-listed"}, "deny", "allow-listed", ("project",), ("profile",)),
]


@pytest.mark.parametrize("row", ENUM_ROWS, ids=lambda r: "%s-%s" % (r[0], "-".join(sorted(r[1])) or "none"))
def test_merge_enum_rows(row):
    field, layers, default, value, origin, ignored = row
    result = pp.merge_enum(field, layers, default)
    assert result == (value, origin, ignored, ())


def test_merge_enum_ignores_values_outside_the_order():
    assert pp.merge_enum("web_tools", {"profile": "maybe"}, "deny")[0] == "deny"


# ------------------------------------------------------------- grant lists

GRANT_ROWS = [
    ({}, (), ("default",), ()),
    ({"profile": ["a", "b"]}, ("a", "b"), ("profile",), ()),
    ({"trusted-managed": ["a"]}, ("a",), ("trusted-managed",), ()),
    ({"profile": ["a", "b"], "trusted-managed": ["a"]}, ("a",), ("profile", "trusted-managed"), ()),
    ({"profile": ["a", "b"], "managed": ["b", "c"]}, ("b",), ("profile", "managed"), ()),
    ({"profile": ["a", "b"], "project": ["a"]}, ("a",), ("profile", "project"), ()),
    ({"profile": ["a", "b"], "project": ["c"]}, (), ("profile", "project"), (0,)),
    ({"project": ["a"]}, (), ("project",), (0,)),
    ({"managed": ["a"]}, (), ("managed",), ()),
]


@pytest.mark.parametrize("row", GRANT_ROWS, ids=lambda r: "-".join(sorted(r[0])) + "-" + ",".join(
    "".join(v) for v in r[0].values()))
def test_merge_grant_list_rows(row):
    layers, value, origin, error_indices = row
    assert pp.merge_grant_list(layers) == (value, origin, (), error_indices)


# ------------------------------------------------------- restriction lists

RESTRICTION_ROWS = [
    ({}, None, ("default",), ()),
    ({"profile": ["a", "b"]}, ("a", "b"), ("profile",), ()),
    ({"managed": ["a"]}, ("a",), ("managed",), ()),
    ({"project": ["a"]}, ("a",), ("project",), ()),
    ({"profile": ["a", "b"], "project": ["a"]}, ("a",), ("profile", "project"), ()),
    ({"profile": ["10.0.0.0/8"], "project": ["10.1.0.0/16"]}, (), ("profile", "project"), (0,)),
    ({"profile": ["a", "b"], "trusted-managed": ["b", "c"]}, ("b",), ("profile", "trusted-managed"), ()),
    ({"profile": [], "project": ["a"]}, (), ("profile", "project"), (0,)),
]


@pytest.mark.parametrize("row", RESTRICTION_ROWS, ids=lambda r: "-".join(sorted(r[0])) + str(r[1]))
def test_merge_restriction_list_rows(row):
    layers, value, origin, error_indices = row
    assert pp.merge_restriction_list(layers) == (value, origin, (), error_indices)


# --------------------------------------------------------------- retention

RETENTION_ROWS = [
    ({}, 30, (30, 30), (("default",), ("default",)), ()),
    ({}, None, (None, None), (("default",), ("default",)), ()),
    ({"profile": 60}, 30, (60, 60), (("profile",), ("profile",)), ()),
    ({"profile": 60, "trusted-managed": 30, "project": 7}, 30, (30, 7), (("trusted-managed",), ("project",)), ()),
    ({"profile": 60, "trusted-managed": 30, "project": 90}, 30, (30, 30), (("trusted-managed",), ("trusted-managed",)), ("project",)),
    ({"profile": 60, "managed": 20}, 30, (20, 20), (("managed",), ("managed",)), ()),
    ({"project": 14}, None, (None, 14), (("default",), ("project",)), ()),
]


@pytest.mark.parametrize("row", RETENTION_ROWS, ids=lambda r: str(r[:3]))
def test_merge_retention_rows(row):
    layers, default, value, origin, ignored = row
    assert pp.merge_retention(layers, default) == (value, origin, ignored, ())


# ----------------------------------------------------- managed combination


def test_combine_managed_rows():
    env = {
        "allowed_providers": ["a", "b"], "denied_providers": ["x"], "allowed_hosts": ["H.example.invalid"],
        "sharing": "manual", "external_writes": "deny", "cross_profile_fallback": False,
    }
    trusted = {
        "allowed_providers": ["b", "c"], "denied_providers": ["y"], "allowed_hosts": ["h.example.invalid", "z"],
        "sharing": "deny", "isolation": "managed", "web_tools": "allow",
    }
    out = pp.combine_managed(env, trusted, host_key=str.lower)
    assert out == {
        "allowed_providers": ["b"], "denied_providers": ["x", "y"], "allowed_hosts": ["H.example.invalid"],
        "sharing": "deny", "external_writes": "deny", "isolation": "managed", "cross_profile_fallback": False,
    }
    assert pp.combine_managed(env, None) == {
        "allowed_providers": ["a", "b"], "denied_providers": ["x"], "allowed_hosts": ["H.example.invalid"],
        "sharing": "manual", "external_writes": "deny", "cross_profile_fallback": False,
    }
    assert pp.combine_managed(None, None) == {}


def test_combine_limits_takes_the_minimum():
    assert pp.combine_limits({"max_run_seconds": 100, "max_tool_calls": 5}, {"max_run_seconds": 50}) == {
        "max_run_seconds": 50, "max_tool_calls": 5,
    }


def test_combine_flat_is_a_ceiling():
    first = {"web_tools": "allow", "retention.runs_days": 60, "mcp.allowed": ("a", "b"), "fallback": "stop"}
    second = {"web_tools": "deny", "retention.runs_days": 30, "mcp.allowed": ("b", "c"), "logging.content": "summary"}
    assert pp.combine_flat(first, second) == {
        "web_tools": "deny", "retention.runs_days": 30, "mcp.allowed": ("b",),
        "fallback": "stop", "logging.content": "summary",
    }


def test_flatten_policy_reads_the_new_fields_only():
    policy = {
        "allowed_hosts": ["h.example.invalid"], "web_tools": "allow",
        "mcp": {"local": "any", "allowed": ["b", "a"]},
        "network": {"allowed_addresses": ["10.0.0.0/8"], "pins": ["A" * 64], "identity_check": "off"},
        "retention": {"runs_days": 5}, "repositories": {"allowed_roots": ["/x"]},
    }
    assert pp.flatten_policy(policy) == {
        "web_tools": "allow", "mcp.local": "any", "mcp.allowed": ("a", "b"),
        "network.identity_check": "off", "network.allowed_addresses": ("10.0.0.0/8",),
        "network.pins": ("a" * 64,), "retention.runs_days": 5, "repositories.allowed_roots": ("/x",),
    }
    assert pp.flatten_policy(None) == {}


# ---------------------------------------------------------------- defaults

WORK = {
    "fallback": "stop", "web_tools": "deny", "package_install": "deny",
    "mcp.local": "deny", "mcp.remote": "deny", "mcp.allowed": (),
    "lsp.mode": "deny", "lsp.allowed": (), "formatter.mode": "deny", "formatter.allowed": (),
    "network.identity_check": "required", "network.allowed_addresses": None, "network.pins": None,
    "logging.content": "metadata", "repositories.allowed_roots": None,
}
PERSONAL = {
    "fallback": "same-profile", "web_tools": "allow", "package_install": "allow",
    "mcp.local": "any", "mcp.remote": "any", "mcp.allowed": (),
    "lsp.mode": "deny", "lsp.allowed": (), "formatter.mode": "deny", "formatter.allowed": (),
    "network.identity_check": "off", "network.allowed_addresses": None, "network.pins": None,
    "logging.content": "summary", "repositories.allowed_roots": None,
}


def test_defaults_table_equals_the_profile_model():
    work = dict(pp.CLASSIFICATION_DEFAULTS["work"])
    personal = dict(pp.CLASSIFICATION_DEFAULTS["personal"])
    for name in pp.RETENTION_FIELDS:
        assert work.pop(name) == 30
        assert personal.pop(name) is None
    assert work == WORK
    assert personal == PERSONAL
    assert set(WORK) | set(pp.RETENTION_FIELDS) == set(pp.POLICY_FIELDS)


def test_every_default_is_inside_its_order():
    for table in pp.CLASSIFICATION_DEFAULTS.values():
        for name, order in pp.ENUM_ORDER.items():
            assert table[name] in order


# -------------------------------------------------------------------- view


def _effective(classification, values=None):
    return SimpleNamespace(classification=classification, values=values or {})


def test_view_reads_defaults_and_is_never_approved_for_defaults():
    view = pp.view(_effective("personal"))
    assert view.web_tools == "allow" and view.fallback == "same-profile"
    assert view.retention["runs_days"] is None
    for name in pp.POLICY_FIELDS:
        assert view.approved(name) is False
    work = pp.view(_effective("work"))
    assert work.web_tools == "deny" and work.retention["runs_days"] == 30
    assert work.repository_roots is None and work.mcp_allowed == ()


def test_view_round_trip_and_approval_by_origin():
    values = {
        "policy.web_tools": SimpleNamespace(value="allow", origin=("trusted-managed",)),
        "policy.repositories.allowed_roots": SimpleNamespace(value=("/a",), origin=("project",)),
        "policy.network.pins": SimpleNamespace(value=("0" * 64,), origin=("profile", "project")),
        "policy.retention.runs_days": SimpleNamespace(value=30, origin=("managed",)),
        "policy.retention.project.runs_days": SimpleNamespace(value=7, origin=("project",)),
    }
    view = pp.view(_effective("work", values))
    assert view.web_tools == "allow" and view.approved("web_tools")
    assert view.repository_roots == ("/a",) and not view.approved("repositories.allowed_roots")
    assert view.pins == ("0" * 64,) and view.approved("network.pins")
    assert view.retention["runs_days"] == 30 and view.project_retention["runs_days"] == 7
    assert not view.approved("retention.runs_days")


# --------------------------------------------------------- id sets and pairs


def test_builtin_id_sets_are_sorted_unique_and_pattern_safe():
    import re

    pattern = json.loads(
        (helpers.OPENCODE_DIR / "schemas" / "runtime-config.schema.json").read_text(encoding="utf-8")
    )["$defs"]["builtin_id"]["pattern"]
    for ids in (pp.LSP_BUILTIN_IDS, pp.FORMATTER_BUILTIN_IDS):
        assert list(ids) == sorted(set(ids))
        for item in ids:
            assert re.search(pattern, item), item
    assert "php intelephense" in pp.LSP_BUILTIN_IDS
    assert all(set(pair) <= set(pp.FORMATTER_BUILTIN_IDS) for pair in pp.FORMATTER_PAIRED_IDS)


# --------------------------------------------- validation of the new fields


def _validate(policy, label="profiles/work.json"):
    data = fixture(PROFILE_WORK)
    data["policy"] = policy
    return config.validate_layer_data(data, "profile", file_label=label, expected_profile="work")


def test_unverifiable_allowlist_modes_are_refused(monkeypatch):
    monkeypatch.setattr(pp, "LSP_ALLOWLIST_VERIFIED", False)
    with pytest.raises(ConfigErrors) as info:
        _validate({"lsp": {"mode": "allow-listed"}})
    assert [(e.rejection_class, e.message_id, e.json_path) for e in info.value.errors] == [
        ("invalid-type", "lsp-allowlist-unverifiable", "$.policy.lsp.mode")
    ]
    monkeypatch.setattr(pp, "FORMATTER_ALLOWLIST_VERIFIED", False)
    with pytest.raises(ConfigErrors) as info:
        _validate({"formatter": {"mode": "allow-listed"}})
    assert [e.message_id for e in info.value.errors] == ["formatter-allowlist-unverifiable"]
    _validate({"lsp": {"mode": "deny"}, "formatter": {"mode": "builtin"}})


@pytest.mark.parametrize("value", [1, 30, 3650])
def test_retention_inside_the_range_is_accepted(value):
    _validate({"retention": {"runs_days": value}})


def test_pair_member_alone_is_refused_and_both_accepted():
    with pytest.raises(ConfigErrors) as info:
        _validate({"formatter": {"allowed": ["uv"]}})
    assert [e.rejection_class for e in info.value.errors] == ["formatter-allowlist-invalid"]
    _validate({"formatter": {"allowed": ["ruff", "uv", "prettier"]}})


# --------------------------------------------------------- module hygiene

_FORBIDDEN = ("socket", "http", "urllib.request", "ssl", "subprocess", "quoin.cli")


@pytest.mark.parametrize("name", ["profile_policy"])
def test_policy_module_hygiene(name):
    src = (SRC_DIR / (name + ".py")).read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 10))
    tree = ast.parse(src)
    first = tree.body[1] if isinstance(tree.body[0], ast.Expr) else tree.body[0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(_FORBIDDEN), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no adapter imports"
            assert not (node.module or "").startswith(_FORBIDDEN)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                assert node.attr not in ("environ", "getenv", "getenvb")


# =====================================================================
# Merge through the public loader
# =====================================================================

import copy
import os
from pathlib import Path

from _opencode_merge_helpers import (
    MANAGEDS,
    PROFILE_PERSONAL,
    PROFILE_WORK,
    PROFILES,
    PROJECT_PERSONAL,
    PROJECT_WORK,
    PROJECTS,
    World,
    loaded,
)
from quoin.opencode_adapter import compiler, explain, paths

UID = os.getuid()
GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "opencode_merge_golden.json").read_text(encoding="utf-8")
)
PIN = "0123456789abcdef" * 4


def nested(field, value):
    """``("mcp.local", "any")`` -> ``{"mcp": {"local": "any"}}``."""
    parts = field.split(".")
    out = value
    for part in reversed(parts):
        out = {part: out}
    return out


def deep_update(target, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_update(target[key], value)
        else:
            target[key] = value
    return target


def with_policy(base, **extra):
    data = copy.deepcopy(fixture(base) if isinstance(base, str) else base)
    deep_update(data.setdefault("policy", {}), copy.deepcopy(extra))
    return data


class Fs:
    """On-disk layers for `config.load_all`: profile, project, the
    environment-pointed managed file and the trusted file."""

    def __init__(self, tmp_path):
        self.tmp = Path(tmp_path)
        self.env = {"XDG_CONFIG_HOME": str(self.tmp / "xdg"), "XDG_STATE_HOME": str(self.tmp / "state")}
        self.home = self.tmp / "home"
        self.root = self.tmp / "project"
        self.top = self.tmp / "root"
        self.directory = self.top / "etc" / "quoin" / "opencode"
        self.directory.mkdir(parents=True)
        for path in (self.top, self.top / "etc", self.top / "etc" / "quoin", self.directory):
            os.chmod(path, 0o755)
        self.trusted_path = self.directory / "managed-policy.json"

    def load(self, profile=PROFILE_WORK, project=None, env_managed=None, trusted=None,
             trusted_mode=0o644, invoking=UID + 1):
        data = copy.deepcopy(fixture(profile) if isinstance(profile, str) else profile)
        target = paths.profile_path(data["profile"], self.env, self.home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")
        if project is not None:
            (self.root / ".quoin").mkdir(parents=True, exist_ok=True)
            (self.root / ".quoin" / "runtime.json").write_text(
                json.dumps(fixture(project) if isinstance(project, str) else project), encoding="utf-8"
            )
        if env_managed is not None:
            file = self.tmp / "managed.json"
            file.write_text(json.dumps(env_managed), encoding="utf-8")
            self.env[paths.ENV_MANAGED_POLICY] = str(file)
        if trusted is not None:
            self.trusted_path.write_text(json.dumps(trusted), encoding="utf-8")
            os.chmod(self.trusted_path, trusted_mode)
        source = config.TrustedSource(self.trusted_path, UID, invoking, self.top)
        return config.load_all(
            project_root=self.root, profile=data["profile"], env=self.env, home=self.home, trusted=source
        )

    def merge(self, **kwargs):
        return merge.merge(self.load(**kwargs))


@pytest.fixture
def fs(tmp_path):
    return Fs(tmp_path)


WORK_PROJECT = {"schema_version": 1, "runtime": "opencode", "profile": "work", "classification": "work"}
PERSONAL_PROJECT = {"schema_version": 1, "runtime": "opencode", "profile": "personal", "classification": "personal"}


def project_with(base, **policy):
    data = copy.deepcopy(base)
    data["policy"] = policy
    return data


def value(eff, name):
    return eff.values["policy." + name]


def ignored(eff, token, layer):
    return any(
        f.code == "less-restrictive-ignored" and f.subject == (token, layer) for f in eff.findings
    )


def errs_of(exc):
    return [(e.rejection_class, e.file, e.json_path, e.message_id) for e in exc.value.errors]


# ------------------------------------------------------------- defaults


def test_work_profile_silent_gets_every_work_default(fs):
    eff = fs.merge(project=WORK_PROJECT)
    assert eff.classification == "work"
    for name, expected in pp.CLASSIFICATION_DEFAULTS["work"].items():
        assert value(eff, name).value == expected, name
        assert value(eff, name).origin == ("default",), name
    for kind in pp.RETENTION_KINDS:
        assert eff.values["policy.retention.project." + kind].value == 30


def test_personal_profile_silent_gets_every_personal_default(fs):
    eff = fs.merge(profile=PROFILE_PERSONAL, project=PERSONAL_PROJECT)
    assert eff.classification == "personal"
    for name, expected in pp.CLASSIFICATION_DEFAULTS["personal"].items():
        assert value(eff, name).value == expected, name
        assert value(eff, name).origin == ("default",), name


def test_a_personal_project_cannot_pick_personal_defaults_for_a_work_profile(fs):
    eff = fs.merge(project=dict(WORK_PROJECT, classification="personal"))
    assert eff.classification == "work"
    assert value(eff, "web_tools").value == "deny"


def test_a_personal_profile_cannot_serve_a_work_project(fs):
    with pytest.raises(ConfigErrors) as info:
        fs.merge(profile=PROFILE_PERSONAL, project=dict(WORK_PROJECT, profile="personal"))
    assert [e[0] for e in errs_of(info)] == ["personal-profile-for-work"]


# --------------------------------------------------------- enumerated rows

WIDER = [
    ("fallback", "same-profile"), ("web_tools", "allow"), ("package_install", "allow"),
    ("mcp.local", "any"), ("mcp.remote", "any"), ("lsp.mode", "builtin"), ("formatter.mode", "builtin"),
    ("network.identity_check", "off"), ("logging.content", "summary"),
]
NARROWER = [
    ("fallback", "stop"), ("web_tools", "deny"), ("package_install", "deny"),
    ("mcp.local", "deny"), ("mcp.remote", "deny"), ("network.identity_check", "required"),
    ("logging.content", "metadata"),
]


@pytest.mark.parametrize("field, wide", WIDER)
def test_work_project_cannot_widen_an_enumerated_field(fs, field, wide):
    project = project_with(WORK_PROJECT, **nested(field, wide))
    eff = fs.merge(project=project)
    assert value(eff, field).value == pp.CLASSIFICATION_DEFAULTS["work"][field]
    assert value(eff, field).origin == ("default",)
    assert ignored(eff, field.replace(".", "-"), "project")


@pytest.mark.parametrize("field, narrow", NARROWER)
def test_personal_project_can_narrow_an_enumerated_field(fs, field, narrow):
    project = project_with(PERSONAL_PROJECT, **nested(field, narrow))
    eff = fs.merge(profile=PROFILE_PERSONAL, project=project)
    assert value(eff, field).value == narrow
    assert value(eff, field).origin == ("project",)


def test_profile_allow_is_narrowed_by_a_trusted_deny(fs):
    profile = with_policy(PROFILE_WORK, web_tools="allow")
    eff = fs.merge(profile=profile, trusted={"schema_version": 1, "policy": {"web_tools": "deny"}})
    assert value(eff, "web_tools").value == "deny"
    assert value(eff, "web_tools").origin == ("trusted-managed",)
    assert ignored(eff, "web_tools", "profile")


def test_trusted_allow_is_an_approval_when_the_profile_is_silent(fs):
    eff = fs.merge(trusted={"schema_version": 1, "policy": {"web_tools": "allow"}})
    assert value(eff, "web_tools").value == "allow"
    assert pp.view(eff).approved("web_tools")


def test_env_managed_allow_cannot_widen_the_default(fs):
    eff = fs.merge(env_managed={"schema_version": 1, "policy": {"web_tools": "allow"}})
    assert value(eff, "web_tools").value == "deny"
    assert ignored(eff, "web_tools", "managed")
    assert not pp.view(eff).approved("web_tools")


# ------------------------------------------------------------ list rows


def test_project_only_repository_roots_are_set_but_not_approved(fs):
    eff = fs.merge(project=project_with(WORK_PROJECT, repositories={"allowed_roots": ["/work/a"]}))
    assert value(eff, "repositories.allowed_roots").value == ("/work/a",)
    assert not pp.view(eff).approved("repositories.allowed_roots")


def test_env_managed_only_repository_roots_are_not_approved(fs):
    eff = fs.merge(env_managed={"schema_version": 1, "policy": {"repositories": {"allowed_roots": ["/work/a"]}}})
    assert value(eff, "repositories.allowed_roots").value == ("/work/a",)
    assert not pp.view(eff).approved("repositories.allowed_roots")


def test_profile_repository_roots_are_approved(fs):
    eff = fs.merge(profile=with_policy(PROFILE_WORK, repositories={"allowed_roots": ["/work/a"]}))
    assert pp.view(eff).approved("repositories.allowed_roots")


def test_project_only_mcp_allowed_is_broadening(fs):
    with pytest.raises(ConfigErrors) as info:
        fs.merge(project=project_with(WORK_PROJECT, mcp={"allowed": ["docs"]}))
    assert errs_of(info) == [
        ("allowlist-broadening", ".quoin/runtime.json", "$.policy.mcp.allowed[0]", "allowlist-broadening")
    ]


def test_project_may_narrow_a_profile_grant_list_and_not_widen_it(fs):
    profile = with_policy(PROFILE_WORK, mcp={"allowed": ["a", "b"]})
    eff = fs.merge(profile=profile, project=project_with(WORK_PROJECT, mcp={"allowed": ["a"]}))
    assert value(eff, "mcp.allowed").value == ("a",)
    with pytest.raises(ConfigErrors) as info:
        Fs(fs.tmp / "second").merge(profile=profile, project=project_with(WORK_PROJECT, mcp={"allowed": ["c"]}))
    assert errs_of(info)[0][2] == "$.policy.mcp.allowed[0]"


def test_trusted_list_narrows_the_profile_list(fs):
    profile = with_policy(PROFILE_WORK, mcp={"allowed": ["a", "b"]})
    eff = fs.merge(profile=profile, trusted={"schema_version": 1, "policy": {"mcp": {"allowed": ["a"]}}})
    assert value(eff, "mcp.allowed").value == ("a",)


def test_subnet_narrowing_is_exact_member_only(fs):
    profile = with_policy(PROFILE_WORK, network={"allowed_addresses": ["10.0.0.0/8"]})
    with pytest.raises(ConfigErrors) as info:
        fs.merge(profile=profile, project=project_with(WORK_PROJECT, network={"allowed_addresses": ["10.1.0.0/16"]}))
    assert errs_of(info)[0][2] == "$.policy.network.allowed_addresses[0]"


@pytest.mark.parametrize("project_days, scoped, flagged", [(7, 7, False), (90, 30, True)])
def test_retention_keeps_the_strictest_and_the_project_view(fs, project_days, scoped, flagged):
    profile = with_policy(PROFILE_WORK, retention={"runs_days": 60})
    eff = fs.merge(
        profile=profile,
        project=project_with(WORK_PROJECT, retention={"runs_days": project_days}),
        trusted={"schema_version": 1, "policy": {"retention": {"runs_days": 30}}},
    )
    assert value(eff, "retention.runs_days").value == 30
    assert eff.values["policy.retention.project.runs_days"].value == scoped
    assert ignored(eff, "retention-runs_days", "project") is flagged


# ----------------------------------------------------- hosts and trust


def codes(eff):
    return [f.code for f in eff.findings]


def test_work_without_allowed_hosts_is_a_non_blocking_finding(fs):
    profile = copy.deepcopy(fixture(PROFILE_WORK))
    del profile["policy"]["allowed_hosts"]
    eff = fs.merge(profile=profile, project=WORK_PROJECT)
    [finding] = [f for f in eff.findings if f.code == "allowed-hosts-unset"]
    assert finding.blocking is False
    assert merge.compile_blockers(eff) == ()


def test_allowed_hosts_in_the_profile_or_trusted_policy_clear_the_finding(fs):
    assert "allowed-hosts-unset" not in codes(fs.merge())
    profile = copy.deepcopy(fixture(PROFILE_WORK))
    del profile["policy"]["allowed_hosts"]
    eff = Fs(fs.tmp / "t").merge(
        profile=profile,
        trusted={"schema_version": 1, "policy": {"allowed_hosts": ["gateway.example.invalid"]}},
    )
    assert "allowed-hosts-unset" not in codes(eff)


def test_env_managed_allowed_hosts_do_not_clear_the_finding(fs):
    profile = copy.deepcopy(fixture(PROFILE_WORK))
    del profile["policy"]["allowed_hosts"]
    eff = fs.merge(
        profile=profile,
        env_managed={"schema_version": 1, "policy": {"allowed_hosts": ["gateway.example.invalid"]}},
    )
    assert "allowed-hosts-unset" in codes(eff)


def test_personal_profile_never_gets_the_hosts_finding(fs):
    assert "allowed-hosts-unset" not in codes(fs.merge(profile=PROFILE_PERSONAL, project=PERSONAL_PROJECT))


def test_untrusted_file_narrows_but_grants_nothing(fs):
    eff = fs.merge(
        trusted={"schema_version": 1, "policy": {"web_tools": "allow", "allowed_providers": ["corp-gw"]}},
        trusted_mode=0o666,
    )
    assert value(eff, "web_tools").value == "deny"
    assert eff.effective_providers == ("corp-gw",)
    assert [f.subject for f in eff.findings if f.code == "trusted-policy-untrusted"] == [("file-writable",)]
    assert eff.trusted_state == "untrusted:file-writable"


def test_trusted_file_alone_counts_as_a_managed_policy(fs):
    eff = fs.merge(trusted={"schema_version": 1, "policy": {"sharing": "deny"}})
    assert "no-managed-policy" not in codes(eff)
    assert eff.managed_present is True and eff.trusted_state == "trusted"


def test_without_any_managed_file_the_finding_stays(fs):
    eff = fs.merge()
    assert "no-managed-policy" in codes(eff)
    assert eff.managed_present is False and eff.trusted_state == "absent"


def test_trusted_limit_ceiling_below_the_profile_limit_is_refused(fs):
    with pytest.raises(ConfigErrors) as info:
        fs.merge(trusted={"schema_version": 1, "limits": {"max_run_seconds": 100}})
    [(cls, _file, path, _msg)] = errs_of(info)
    assert (cls, path) == ("limit-above-ceiling", "$.limits.max_run_seconds")


def test_trusted_ceiling_on_existing_fields_combines_with_the_env_file(fs):
    eff = fs.merge(
        env_managed={"schema_version": 1, "policy": {"allowed_providers": ["corp-gw", "corp-gw-b"]}},
        trusted={"schema_version": 1, "policy": {"allowed_providers": ["corp-gw"]}},
    )
    assert eff.effective_providers == ("corp-gw",)


# ------------------------------------------- nothing changes without a file

NEW_CODES = {"allowed-hosts-unset", "trusted-policy-untrusted"}


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_existing_combinations_keep_their_canonical_form(key):
    profile, project, managed = key.split("|")
    expected = GOLDEN[key]
    try:
        eff = merge.merge(
            loaded(PROFILES[profile], PROJECTS[project], MANAGEDS[managed])
        )
    except ConfigErrors as exc:
        assert "errors" in expected, key
        assert [[e.rejection_class, e.file, e.json_path, e.message_id] for e in exc.errors] == expected["errors"]
        return
    assert "canonical" in expected, key
    doc = merge.canonical(eff)
    assert doc.pop("trusted_state") == "absent"
    old = expected["canonical"]
    doc["values"] = {k: v for k, v in doc["values"].items() if k in old["values"]}
    doc["findings"] = [f for f in doc["findings"] if f["code"] not in NEW_CODES]
    assert doc == old
    assert {k for k in eff.values if k.startswith("policy.")} >= {"policy." + n for n in pp.POLICY_FIELDS}


# ---------------------------------------------------------------- explain


def explain_doc(world, redact):
    return json.loads(explain.render(world.evaluate(), as_json=True, redact=redact))


def test_explain_masks_the_new_lists_only_under_redact(tmp_path):
    profile = with_policy(
        PROFILE_WORK,
        repositories={"allowed_roots": ["/work/secret-root"]},
        network={"allowed_addresses": ["10.9.8.0/24"], "pins": [PIN]},
    )
    world = World(tmp_path, profile=profile)
    plain = explain_doc(world, False)["values"]
    masked = explain_doc(world, True)["values"]
    assert plain["policy.repositories.allowed_roots"]["value"] == ["/work/secret-root"]
    assert plain["policy.network.allowed_addresses"]["value"] == ["10.9.8.0/24"]
    for key in ("policy.repositories.allowed_roots", "policy.network.allowed_addresses", "policy.network.pins"):
        assert masked[key]["value"] == ["***"]
    assert "secret-root" not in json.dumps(masked)


@pytest.mark.parametrize("profile", [PROFILE_WORK, PROFILE_PERSONAL])
def test_explain_redact_keeps_unset_lists_null(tmp_path, profile):
    world = World(tmp_path, profile=profile)
    for redact in (False, True):
        values = explain_doc(world, redact)["values"]
        for key in ("policy.repositories.allowed_roots", "policy.network.allowed_addresses", "policy.network.pins"):
            assert values[key]["value"] is None


def test_hosts_finding_does_not_change_launchability(tmp_path):
    profile = copy.deepcopy(fixture(PROFILE_WORK))
    del profile["policy"]["allowed_hosts"]
    with_finding = World(tmp_path / "a", profile=profile).evaluate()
    without = World(tmp_path / "b").evaluate()
    assert any(f.code == "allowed-hosts-unset" for f in with_finding.effective.findings)
    assert compiler.launchable(with_finding) == compiler.launchable(without)


# ------------------------------------------------------------- secrets


def test_seeded_secret_never_reaches_the_new_surfaces(tmp_path):
    secret = helpers.SEEDED_SECRET
    profile = with_policy(
        PROFILE_WORK, repositories={"allowed_roots": ["/work/a"]}, network={"pins": [PIN]}
    )
    world = World(tmp_path, profile=profile)
    for name in ("QUOIN_CORP_GW_API_KEY", "QUOIN_CORP_GW_B_API_KEY"):
        world.env[name] = secret
    ev = world.evaluate()
    texts = [json.dumps(merge.canonical(ev.effective))]
    for redact in (False, True):
        texts.append(explain.render(ev, as_json=True, redact=redact))
    texts += [repr(f) for f in ev.effective.findings]
    for text in texts:
        for form in helpers.secret_forms(secret):
            assert form not in text
