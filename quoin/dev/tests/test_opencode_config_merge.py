"""Tests for merging configuration layers: preference origins, the security
algebra for allow and deny lists, enumerated settings, limits, project
classification, determinism and the redaction boundary."""
from __future__ import annotations

import ast
import copy
import os
import socket
import subprocess
from pathlib import Path

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import (
    MANAGED_STRICT,
    MANAGED_WORK,
    PROFILE_MINIMAL,
    PROFILE_PERSONAL,
    PROFILE_WORK,
    PROJECT_PERSONAL,
    PROJECT_UNCLASSIFIED,
    PROJECT_WORK,
    PROJECT_WORK_NARROW,
    fixture,
    loaded,
)
from quoin.opencode_adapter import config, jsonio, merge
from quoin.opencode_adapter.errors import ConfigErrors, OVERRIDE_LABEL

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
PROJECT_LABEL = ".quoin/runtime.json"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("merge must not use the network, a subprocess or the environment")

    for owner, name in (
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (subprocess, "run"),
        (subprocess, "Popen"),
        (os, "getenv"),
    ):
        monkeypatch.setattr(owner, name, boom)


def errs_of(exc):
    return [(e.rejection_class, e.file, e.json_path, e.message_id) for e in exc.value.errors]


def profile(**policy):
    """The work profile with its policy allow lists replaced by `policy`."""
    data = fixture(PROFILE_WORK)
    data["policy"] = dict(policy)
    return data


def project(**policy):
    data = fixture(PROJECT_WORK)
    data["policy"] = dict(policy)
    data.pop("limits", None)
    data.pop("roles", None)
    return data


def managed(**policy):
    return {"schema_version": 1, "policy": dict(policy)}


# -------------------------------------------------------------- preferences


def test_preference_origins_profile_project_override():
    eff = merge.merge(loaded(PROFILE_WORK))
    assert eff.values["roles.coordinator.model"] == merge.Value("work-planner", ("profile",))
    assert eff.values["roles.gate.effort"] == merge.Value("low", ("profile",))
    assert eff.values["default_model"] == merge.Value("work-coder", ("profile",))
    assert eff.values["auxiliary_model"] == merge.Value("work-planner", ("profile",))
    assert eff.values["limits.max_tool_calls"] == merge.Value(200, ("profile",))

    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK))
    assert eff.values["roles.implementer.model"].origin == ("project",)
    assert eff.values["limits.max_tool_calls"] == merge.Value(100, ("project",))

    overrides = {
        "implementer": merge.RoleOverride(model="work-planner", effort="max"),
        "critic": merge.RoleOverride(effort="low"),
    }
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK), overrides=overrides)
    assert eff.values["roles.implementer.model"] == merge.Value("work-planner", ("override",))
    assert eff.values["roles.implementer.effort"] == merge.Value("max", ("override",))
    assert eff.values["roles.critic.model"] == merge.Value("work-planner", ("profile",))
    assert eff.values["roles.critic.effort"] == merge.Value("low", ("override",))


def test_roles_without_any_setting_have_no_value():
    data = fixture(PROFILE_MINIMAL)
    eff = merge.merge(loaded(data))
    assert not any(key.startswith("roles.") for key in eff.values)
    assert "auxiliary_model" not in eff.values


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"wizard": merge.RoleOverride(model="work-coder")},
         [("unknown-role", OVERRIDE_LABEL, "$.roles.wizard", "unknown-override-role")]),
        ({"planner": merge.RoleOverride(model="missing-model")},
         [("dangling-reference", OVERRIDE_LABEL, "$.roles.planner.model", "override-dangling-model")]),
        ({"planner": merge.RoleOverride(model="Bad Name")},
         [("dangling-reference", OVERRIDE_LABEL, "$.roles.planner.model", "override-dangling-model")]),
        ({"planner": merge.RoleOverride(effort="extreme")},
         [("invalid-effort", OVERRIDE_LABEL, "$.roles.planner.effort", "invalid-effort")]),
    ],
)
def test_override_validation(overrides, expected):
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(), overrides=overrides)
    assert errs_of(exc) == expected


def test_override_validation_collects_every_problem():
    overrides = {
        "planner": merge.RoleOverride(model="missing-model", effort="extreme"),
        "wizard": merge.RoleOverride(),
    }
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(), overrides=overrides)
    assert len(exc.value.errors) == 3


# ---------------------------------------------------------- provider allow


COR, CORB = "corp-gw", "corp-gw-b"
BOTH = (COR, CORB)
NP = "not-allowed"
MNP = "managed-not-allowed"

# (profile list, managed list, project list) -> (effective, excluded)
# ABSENT means the layer exists without the key; NO_LAYER means no layer.
ABSENT, NO_LAYER = "absent", "no-layer"

PROVIDER_ROWS = [
    (ABSENT, NO_LAYER, NO_LAYER, BOTH, {}),
    (ABSENT, ABSENT, ABSENT, BOTH, {}),
    ([COR], NO_LAYER, NO_LAYER, (COR,), {CORB: NP}),
    ([COR, CORB], NO_LAYER, NO_LAYER, BOTH, {}),
    (ABSENT, [COR], NO_LAYER, (COR,), {CORB: MNP}),
    (ABSENT, [], NO_LAYER, (), {COR: MNP, CORB: MNP}),
    ([], NO_LAYER, NO_LAYER, (), {COR: NP, CORB: NP}),
    ([], [COR], NO_LAYER, (), {COR: NP, CORB: NP}),
    (ABSENT, NO_LAYER, [COR], (COR,), {CORB: NP}),
    (ABSENT, NO_LAYER, [], (), {COR: NP, CORB: NP}),
    (ABSENT, [COR], [COR], (COR,), {CORB: MNP}),
    (ABSENT, [], [], (), {COR: MNP, CORB: MNP}),
    (ABSENT, NO_LAYER, [COR, CORB], BOTH, {}),
    ([COR, CORB], [COR, CORB], [CORB], (CORB,), {COR: NP}),
]


def _layer_data(kind, value):
    if value == NO_LAYER:
        return None
    build = {"profile": profile, "project": project, "managed": managed}[kind]
    return build() if value == ABSENT else build(allowed_providers=list(value))


@pytest.mark.parametrize("prof,man,proj,effective,excluded", PROVIDER_ROWS)
def test_provider_allow_algebra(prof, man, proj, effective, excluded):
    eff = merge.merge(
        loaded(_layer_data("profile", prof), _layer_data("project", proj), _layer_data("managed", man))
    )
    assert eff.effective_providers == effective
    assert dict(eff.excluded_providers) == excluded
    assert eff.values["policy.allowed_providers"].value == effective
    exclusion_findings = {f.subject for f in eff.findings if f.code == "provider-excluded"}
    assert exclusion_findings == set(excluded.items())


@pytest.mark.parametrize(
    "prof,man,proj,path",
    [
        ([COR, CORB], [COR], [CORB], "$.policy.allowed_providers[0]"),
        ([COR], NO_LAYER, [CORB], "$.policy.allowed_providers[0]"),
        ([COR, CORB], [COR], [COR, CORB], "$.policy.allowed_providers[1]"),
    ],
)
def test_project_allow_list_broadening_is_an_error(prof, man, proj, path):
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(
            loaded(_layer_data("profile", prof), _layer_data("project", proj), _layer_data("managed", man))
        )
    assert errs_of(exc) == [("allowlist-broadening", PROJECT_LABEL, path, "allowlist-broadening")]


# --------------------------------------------------------------- host allow

H1, H2 = "gateway.example.invalid", "gateway-b.example.invalid"
HNP = "host-not-allowed"

HOST_ROWS = [
    (ABSENT, NO_LAYER, NO_LAYER, BOTH, {}),
    ([H1], NO_LAYER, NO_LAYER, (COR,), {CORB: HNP}),
    ([], NO_LAYER, NO_LAYER, (), {COR: HNP, CORB: HNP}),
    (ABSENT, [H1], NO_LAYER, (COR,), {CORB: HNP}),
    (ABSENT, [], NO_LAYER, (), {COR: HNP, CORB: HNP}),
    (ABSENT, NO_LAYER, [H1], (COR,), {CORB: HNP}),
    (ABSENT, NO_LAYER, [], (), {COR: HNP, CORB: HNP}),
    (["GATEWAY.Example.Invalid"], NO_LAYER, NO_LAYER, (COR,), {CORB: HNP}),
    ([H1, H2], [H1, H2], [H2], (CORB,), {COR: HNP}),
]


def _host_layer(kind, value):
    if value == NO_LAYER:
        return None
    build = {"profile": profile, "project": project, "managed": managed}[kind]
    return build() if value == ABSENT else build(allowed_hosts=list(value))


@pytest.mark.parametrize("prof,man,proj,effective,excluded", HOST_ROWS)
def test_host_allow_algebra(prof, man, proj, effective, excluded):
    eff = merge.merge(
        loaded(_host_layer("profile", prof), _host_layer("project", proj), _host_layer("managed", man))
    )
    assert eff.effective_providers == effective
    assert dict(eff.excluded_providers) == excluded


def test_project_host_broadening_is_an_error():
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(profile(), project(allowed_hosts=["gateway-c.example.invalid"])))
    assert errs_of(exc) == [
        ("allowlist-broadening", PROJECT_LABEL, "$.policy.allowed_hosts[0]", "allowlist-broadening-host")
    ]


def test_host_broadening_is_measured_against_the_stricter_layers():
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(profile(), project(allowed_hosts=[H2]), managed(allowed_hosts=[H1])))
    assert [e.json_path for e in exc.value.errors] == ["$.policy.allowed_hosts[0]"]


# -------------------------------------------------------------- deny lists


@pytest.mark.parametrize("layer", ["profile", "project", "managed"])
def test_denied_provider_from_each_layer_alone(layer):
    prof = profile(denied_providers=[CORB]) if layer == "profile" else profile()
    proj = project(denied_providers=[CORB]) if layer == "project" else None
    man = managed(denied_providers=[CORB]) if layer == "managed" else None
    eff = merge.merge(loaded(prof, proj, man))
    assert eff.effective_providers == (COR,)
    assert dict(eff.excluded_providers) == {CORB: "denied"}
    assert CORB in eff.values["policy.denied_providers"].value
    assert layer in eff.values["policy.denied_providers"].origin


@pytest.mark.parametrize("layer", ["profile", "project", "managed"])
def test_denied_host_from_each_layer_alone(layer):
    prof = profile(denied_hosts=[H2]) if layer == "profile" else profile()
    proj = project(denied_hosts=[H2]) if layer == "project" else None
    man = managed(denied_hosts=[H2]) if layer == "managed" else None
    eff = merge.merge(loaded(prof, proj, man))
    assert eff.effective_providers == (COR,)
    assert dict(eff.excluded_providers) == {CORB: "host-denied"}
    assert eff.values["policy.denied_hosts"].value == (H2,)


def test_deny_wins_over_allow_and_first_reason_is_kept():
    eff = merge.merge(loaded(profile(allowed_providers=[COR, CORB], denied_providers=[COR])))
    assert dict(eff.excluded_providers) == {COR: "denied"}
    eff = merge.merge(loaded(profile(), managed=managed(allowed_providers=[COR], denied_providers=[CORB])))
    assert dict(eff.excluded_providers) == {CORB: "managed-not-allowed"}


def _with_url(url, **policy):
    data = profile(**policy)
    data["providers"][COR]["base_url"] = url
    return data


@pytest.mark.parametrize(
    "url,policy,reason",
    [
        ("https://gateway.example.invalid./v1", {"denied_hosts": [H1]}, "host-denied"),
        ("https://GATEWAY.example.invalid/v1", {"denied_hosts": [H1]}, "host-denied"),
        ("https://[::ffff:127.0.0.1]/v1", {"denied_hosts": ["127.0.0.1"]}, "host-denied"),
        ("https://[::ffff:7f00:1]/v1", {"denied_hosts": ["127.0.0.1"]}, "host-denied"),
        ("https://[::1]/v1", {"denied_hosts": ["0:0:0:0:0:0:0:1"]}, "host-denied"),
        ("https://gateway.example.invalid./v1", {"allowed_hosts": [H1, H2]}, None),
        ("https://GATEWAY.example.invalid/v1", {"allowed_hosts": [H1, H2]}, None),
        ("https://[::ffff:127.0.0.1]/v1", {"allowed_hosts": ["127.0.0.1", H2]}, None),
        ("https://gateway.example.invalid/v1", {"allowed_hosts": ["GATEWAY.EXAMPLE.INVALID", H2]}, None),
    ],
)
def test_host_comparison_uses_canonical_keys(url, policy, reason):
    eff = merge.merge(loaded(_with_url(url, **policy)))
    assert dict(eff.excluded_providers).get(COR) == reason


def test_empty_present_lists_are_never_read_as_absent():
    for policy in ({"allowed_providers": []}, {"allowed_hosts": []}):
        eff = merge.merge(loaded(profile(**policy)))
        assert eff.effective_providers == ()


# ------------------------------------------------------------- enumerated


def _findings(eff, code):
    return [f.subject for f in eff.findings if f.code == code]


def test_enumerated_defaults_when_no_layer_sets_them():
    eff = merge.merge(loaded(profile()))
    for key, value in (("sharing", "deny"), ("external_writes", "deny"), ("isolation", "convenience")):
        assert eff.values["policy." + key] == merge.Value(value, ("default",))
    assert eff.values["policy.cross_profile_fallback"] == merge.Value(False, ("default",))
    assert _findings(eff, "isolation-unverified") == []


@pytest.mark.parametrize(
    "prof,proj,man,key,expected,origin,ignored",
    [
        ({"external_writes": "approval-required"}, {}, {}, "external_writes", "approval-required",
         ("profile",), []),
        ({"external_writes": "approval-required"}, {"external_writes": "deny"}, {}, "external_writes",
         "deny", ("project",), [("external_writes", "profile")]),
        ({"external_writes": "deny"}, {"external_writes": "approval-required"}, {}, "external_writes",
         "deny", ("profile",), [("external_writes", "project")]),
        ({"sharing": "manual"}, {}, {"sharing": "deny"}, "sharing", "deny", ("managed",),
         [("sharing", "profile")]),
        ({"sharing": "manual"}, {"sharing": "manual"}, {}, "sharing", "manual", ("profile", "project"), []),
        ({}, {"sharing": "manual"}, {}, "sharing", "deny", ("default",), [("sharing", "project")]),
        ({}, {"external_writes": "approval-required"}, {}, "external_writes", "deny", ("default",),
         [("external_writes", "project")]),
        ({}, {"sharing": "deny"}, {}, "sharing", "deny", ("project",), []),
        ({"sharing": "manual"}, {"sharing": "deny"}, {}, "sharing", "deny", ("project",),
         [("sharing", "profile")]),
        ({}, {"isolation": "managed"}, {}, "isolation", "managed", ("project",), []),
        ({"isolation": "convenience"}, {}, {"isolation": "managed"}, "isolation", "managed",
         ("managed",), [("isolation", "profile")]),
        ({"isolation": "managed"}, {"isolation": "convenience"}, {}, "isolation", "managed",
         ("profile",), [("isolation", "project")]),
        # The project asked for the very value a stricter layer produced: no
        # finding about the project.
        ({}, {"sharing": "manual"}, {"sharing": "manual"}, "sharing", "manual", ("managed",), []),
        ({}, {"external_writes": "approval-required"}, {"external_writes": "approval-required"},
         "external_writes", "approval-required", ("managed",), []),
    ],
)
def test_enumerated_settings_take_the_most_restrictive_value(prof, proj, man, key, expected, origin, ignored):
    eff = merge.merge(loaded(profile(**prof), project(**proj), managed(**man)))
    assert eff.values["policy." + key] == merge.Value(expected, origin)
    assert _findings(eff, "less-restrictive-ignored") == ignored
    assert (_findings(eff, "isolation-unverified") == [()]) == (key == "isolation" and expected == "managed")


# ----------------------------------------------------------------- limits


def test_limit_ceiling_error_per_layer():
    data = fixture(PROFILE_WORK)
    data["limits"]["max_run_seconds"] = 5000
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(data, managed=fixture(MANAGED_WORK)))
    assert errs_of(exc) == [
        ("limit-above-ceiling", "profiles/work.json", "$.limits.max_run_seconds", "limit-above-ceiling")
    ]
    proj = project()
    proj["limits"] = {"max_run_seconds": 3600 + 1}
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(PROFILE_WORK, proj, MANAGED_WORK))
    assert errs_of(exc) == [
        ("limit-above-ceiling", PROJECT_LABEL, "$.limits.max_run_seconds", "limit-above-ceiling")
    ]


def test_a_value_above_both_profile_and_ceiling_reports_only_the_ceiling():
    proj = project()
    proj["limits"] = {"max_run_seconds": 7200}
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(PROFILE_WORK, proj, MANAGED_WORK))
    assert errs_of(exc) == [
        ("limit-above-ceiling", PROJECT_LABEL, "$.limits.max_run_seconds", "limit-above-ceiling")
    ]


def test_project_limit_above_profile_without_a_managed_layer():
    proj = project()
    proj["limits"] = {"max_tool_calls": 500}
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(PROFILE_WORK, proj))
    assert errs_of(exc) == [
        ("limit-above-ceiling", PROJECT_LABEL, "$.limits.max_tool_calls", "limit-above-profile")
    ]


def test_project_limit_the_profile_leaves_unset_is_accepted_and_narrowing_wins():
    data = fixture(PROFILE_WORK)
    del data["limits"]["max_tool_calls"]
    proj = project()
    proj["limits"] = {"max_tool_calls": 500}
    eff = merge.merge(loaded(data, proj))
    assert eff.values["limits.max_tool_calls"] == merge.Value(500, ("project",))
    proj["limits"] = {"max_tool_calls": 100}
    eff = merge.merge(loaded(PROFILE_WORK, proj))
    assert eff.values["limits.max_tool_calls"] == merge.Value(100, ("project",))
    proj["limits"] = {"max_tool_calls": 200}
    assert merge.merge(loaded(PROFILE_WORK, proj)).values["limits.max_tool_calls"].value == 200


def test_managed_ceiling_is_the_default_when_no_layer_sets_a_limit():
    data = fixture(PROFILE_WORK)
    del data["limits"]
    eff = merge.merge(loaded(data, managed=fixture(MANAGED_WORK)))
    assert eff.values["limits.max_run_seconds"] == merge.Value(3600, ("managed",))
    assert "limits.max_tool_calls" not in eff.values


def test_managed_strict_ceiling_leaves_valid_fixtures_alone():
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK, MANAGED_STRICT))
    assert eff.values["limits.max_tool_calls"] == merge.Value(100, ("project",))


# --------------------------------------------------------- classification

CLASS_ROWS = [
    # profile, project fixture/data, effective, blocker (path, message id) or None
    (PROFILE_WORK, PROJECT_WORK, "work", None),
    (PROFILE_WORK, PROJECT_PERSONAL, "work", None),
    (PROFILE_WORK, PROJECT_UNCLASSIFIED, "work", ("$", "missing-classification")),
    (PROFILE_WORK, None, "work", ("$", "no-project-file")),
    (PROFILE_PERSONAL, PROJECT_PERSONAL, "personal", None),
    (PROFILE_PERSONAL, PROJECT_UNCLASSIFIED, "personal", ("$", "missing-classification")),
    (PROFILE_PERSONAL, None, "personal", ("$", "no-project-file")),
]


def _unknown_project():
    data = fixture(PROJECT_WORK)
    data["classification"] = "restricted"
    return data


@pytest.mark.parametrize("prof,proj,effective,blocker", CLASS_ROWS)
def test_classification_matrix(prof, proj, effective, blocker):
    project_data = proj
    if isinstance(proj, str) and proj != PROJECT_WORK and prof == PROFILE_PERSONAL:
        project_data = dict(fixture(proj), profile="personal")
    eff = merge.merge(loaded(prof, project_data))
    assert eff.classification == effective
    got = merge.compile_blockers(eff)
    if blocker is None:
        assert got == ()
        merge.ensure_compilable(eff)
    else:
        assert [(e.json_path, e.message_id, e.file, e.rejection_class) for e in got] == [
            (blocker[0], blocker[1], PROJECT_LABEL, "missing-classification")
        ]
        with pytest.raises(ConfigErrors):
            merge.ensure_compilable(eff)
        assert any(f.blocking and f.code == "missing-classification" for f in eff.findings)


def test_unknown_project_classification():
    eff = merge.merge(loaded(PROFILE_WORK, _unknown_project()))
    (err,) = merge.compile_blockers(eff)
    assert (err.json_path, err.message_id) == ("$.classification", "unknown-classification")
    assert eff.project_state == "unknown" and eff.classification == "work"


def test_project_states_are_reported():
    assert merge.merge(loaded(PROFILE_WORK, PROJECT_WORK)).project_state == "work"
    assert merge.merge(loaded(PROFILE_WORK, PROJECT_UNCLASSIFIED)).project_state == "missing"
    assert merge.merge(loaded(PROFILE_WORK)).project_state == "absent"
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_PERSONAL))
    assert (eff.profile_classification, eff.project_state, eff.classification) == (
        "work", "personal", "work"
    )


def test_personal_profile_for_work_is_a_hard_error():
    proj = dict(fixture(PROJECT_WORK), profile="personal")
    proj.pop("roles"), proj.pop("policy"), proj.pop("limits")
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(PROFILE_PERSONAL, proj))
    assert errs_of(exc) == [
        ("personal-profile-for-work", PROJECT_LABEL, "$.classification", "personal-profile-for-work")
    ]


def _openrouter(*provider_ids):
    data = fixture(PROFILE_WORK)
    for pid in provider_ids:
        data["providers"][pid]["kind"] = "openrouter"
        data["providers"][pid]["endpoint_family"] = "chat-completions"
    return data


def test_openrouter_under_work_from_project_and_from_profile():
    data = _openrouter(COR)
    for project_source in (PROJECT_WORK, None):
        with pytest.raises(ConfigErrors) as exc:
            merge.merge(loaded(data, project_source))
        assert errs_of(exc) == [
            ("personal-provider-kind-for-work", "profiles/work.json", "$.providers.corp-gw.kind",
             "personal-provider-kind-for-work")
        ]


def test_openrouter_check_covers_every_declared_provider_not_only_survivors():
    data = _openrouter(CORB)
    with pytest.raises(ConfigErrors) as exc:
        merge.merge(loaded(data, PROJECT_WORK))  # the project allows only corp-gw
    assert [e.json_path for e in exc.value.errors] == ["$.providers.corp-gw-b.kind"]


def test_openrouter_is_fine_for_a_personal_profile():
    eff = merge.merge(loaded(PROFILE_PERSONAL, dict(fixture(PROJECT_PERSONAL))))
    assert eff.classification == "personal" and eff.effective_providers == ("openrouter",)


# ---------------------------------------------------------- standing findings


def test_standing_findings():
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK))
    assert _findings(eff, "no-managed-policy") == [()]
    assert _findings(eff, "provider-ids-are-labels") == []
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK, MANAGED_WORK))
    assert _findings(eff, "no-managed-policy") == []
    assert _findings(eff, "provider-ids-are-labels") == [()]
    assert eff.managed_present
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK, managed(sharing="deny")))
    assert _findings(eff, "provider-ids-are-labels") == []


def test_project_narrow_and_managed_strict_fixtures():
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK_NARROW))
    assert eff.effective_providers == (COR,)
    assert dict(eff.excluded_providers) == {CORB: "host-not-allowed"}
    assert eff.values["roles.planner.model"] == merge.Value("work-planner", ("project",))
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK_NARROW, MANAGED_STRICT))
    assert dict(eff.excluded_providers) == {CORB: "denied"}
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK, MANAGED_STRICT))
    assert dict(eff.excluded_providers) == {CORB: "not-allowed"}
    assert eff.effective_providers == (COR,)


def test_effective_allowed_hosts_exclude_denied_hosts():
    eff = merge.merge(loaded(profile(denied_hosts=[H2])))
    assert H2 not in eff.values["policy.allowed_hosts"].value
    assert H1 in eff.values["policy.allowed_hosts"].value
    assert H2 in eff.values["policy.denied_hosts"].value


def test_integrations_enabled_origin_reflects_a_narrowing_project():
    data = fixture(PROFILE_WORK)
    data["integrations"] = {"enabled": ["jira", "mail"]}
    proj = fixture(PROJECT_PERSONAL)
    proj["profile"] = "work"
    proj["integrations"] = {"enabled": ["jira"]}
    eff = merge.merge(loaded(data, proj))
    assert eff.values["integrations.enabled"] == merge.Value(("jira",), ("profile", "project"))
    proj["integrations"] = {"enabled": ["jira", "mail"]}
    eff = merge.merge(loaded(data, proj))
    assert eff.values["integrations.enabled"].origin == ("profile",)


def test_integrations_merge_never_widens():
    data = fixture(PROFILE_WORK)
    proj = fixture(PROJECT_PERSONAL)
    proj["profile"] = "work"
    proj["integrations"] = {"backend": "other", "enabled": ["jira", "mail"], "mode": "approved-write"}
    eff = merge.merge(loaded(data, proj))
    assert eff.values["integrations.enabled"].value == ("jira",)
    assert eff.values["integrations.mode"].value == "read-only"
    assert eff.values["integrations.backend"].value == "corp-gw"
    assert sorted(_findings(eff, "integrations-value-ignored")) == [
        ("integrations-backend", "project"),
        ("integrations-enabled", "project"),
        ("integrations-mode", "project"),
    ]
    data["integrations"]["mode"] = "approved-write"
    proj["integrations"] = {"mode": "read-only"}
    eff = merge.merge(loaded(data, proj))
    assert eff.values["integrations.mode"].value == "read-only"
    assert _findings(eff, "integrations-value-ignored") == []
    del data["integrations"]
    proj["integrations"] = {"enabled": ["slack"]}
    eff = merge.merge(loaded(data, proj))
    assert eff.values["integrations.enabled"].value == ()
    assert _findings(eff, "integrations-value-ignored") == [("integrations-enabled", "project")]


# ------------------------------------------------- immutability, determinism


def test_merge_never_mutates_layers_and_returns_immutables():
    lc = loaded(PROFILE_WORK, PROJECT_WORK, MANAGED_WORK)
    before = [copy.deepcopy(layer.data) for layer in (lc.profile, lc.project, lc.managed)]
    eff = merge.merge(lc)
    after = [layer.data for layer in (lc.profile, lc.project, lc.managed)]
    assert before == after
    with pytest.raises(TypeError):
        eff.values["x"] = 1
    with pytest.raises(TypeError):
        eff.providers["x"] = 1
    with pytest.raises(TypeError):
        eff.models["x"] = 1
    with pytest.raises(TypeError):
        eff.excluded_providers["x"] = "y"
    assert isinstance(eff.effective_providers, tuple) and isinstance(eff.findings, tuple)
    for value in eff.values.values():
        assert not isinstance(value.value, (list, dict, set))


def test_merge_is_deterministic():
    dumps = {
        jsonio.dump_canonical(merge.canonical(merge.merge(loaded(PROFILE_WORK, PROJECT_WORK, MANAGED_WORK))))
        for _ in range(3)
    }
    assert len(dumps) == 1


# --------------------------------------------------------- redaction boundary


def test_canonical_and_reprs_hold_no_urls_or_credential_refs():
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK, MANAGED_WORK))
    dump = jsonio.dump_canonical(merge.canonical(eff)).decode("utf-8")
    profile_data = fixture(PROFILE_WORK)
    leaks = [
        entry[key]
        for entry in profile_data["providers"].values()
        for key in ("base_url", "credential_ref")
    ] + ["https://", "keychain:"]
    for leak in leaks:
        assert leak not in dump, leak
        assert leak not in repr(eff), leak
        for view in eff.providers.values():
            assert leak not in repr(view), leak
    assert "QUOIN_CORP_GW_API_KEY" in dump  # the env variable name is part of the identity
    assert "gateway.example.invalid" in dump  # host keys are
    assert "env:" not in dump


def test_provider_accessors_and_field_rules():
    eff = merge.merge(loaded(PROFILE_WORK))
    view = eff.providers[COR]
    assert merge.provider_base_url(view) == "https://gateway.example.invalid/v1"
    assert merge.provider_credential_ref(view) == "keychain:quoin/work-gateway"
    assert view.credential_env == config.provider_env_name(COR)
    assert view.host_key == "gateway.example.invalid"
    assert eff.providers[CORB].use_env_proxy is True
    assert eff.providers[COR].name == "Corporate gateway"
    same = merge.ProviderView(
        id=view.id, kind=view.kind, endpoint_family=view.endpoint_family, host_key=view.host_key,
        credential_env=view.credential_env, name=view.name, base_url="https://other.example.invalid",
    )
    assert same == view  # base URL and reference never take part in comparison
    with pytest.raises(TypeError):
        merge.ProviderView(COR, "k", "f", "h", "e")  # keyword-only


def test_model_views():
    eff = merge.merge(loaded(PROFILE_WORK))
    model = eff.models["work-coder"]
    assert (model.provider, model.model_id, model.qualification_ref) == (
        "corp-gw", "example-vendor/example-coder", "local:work-coder"
    )


@pytest.mark.parametrize("module", ["merge.py", "roles.py", "qualification.py"])
def test_provider_handling_modules_avoid_dataclass_conversion_helpers(module):
    tree = ast.parse((SRC_DIR / module).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not names & {"asdict", "astuple"}


def test_merge_imports_are_limited():
    tree = ast.parse((SRC_DIR / "merge.py").read_text(encoding="utf-8"))
    relative = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            relative |= {node.module} if node.module else {a.name for a in node.names}
    assert relative <= {"config", "errors", "generate", "install", "jsonio"}
