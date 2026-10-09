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
