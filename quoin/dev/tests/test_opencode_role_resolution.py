"""Tests for role resolution: precedence, no fall-through, auxiliary routing,
effort gating, launchability and metadata-only logging."""
from __future__ import annotations

import ast
import dataclasses
import itertools
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import _opencode_helpers as helpers
from _opencode_merge_helpers import (
    MANAGED_STRICT,
    MANAGED_WORK,
    PROFILE_MINIMAL,
    PROFILE_PERSONAL,
    PROFILE_WORK,
    PROJECT_PERSONAL,
    PROJECT_WORK,
    PROJECT_WORK_NARROW,
    fixture,
    loaded,
)
from quoin.opencode_adapter import config, merge, paths, qualification, roles
from quoin.opencode_adapter.errors import ConfigErrors
from quoin.opencode_adapter.generate import ROLES
from quoin.opencode_adapter.qualification import QualificationResult
from quoin.opencode_adapter.roles import AUXILIARY, AllowUnqualifiedRefused, resolve_all
from _opencode_merge_helpers import (
    ALLOWED_ENV_READS,
    COMBINATIONS,
    MANAGEDS,
    NOW,
    PROFILES,
    PROJECTS,
    Outcome,
    probe_module,
    run_pipeline,
    write_probe_records,
)
from test_opencode_runtime_config import RecordingEnv, run_case

SRC_DIR = helpers.SOURCE_DIR.parent / "src" / "quoin" / "opencode_adapter"
STAMP = "2026-09-28T12:00:00Z"
PLANNER_ROLES = ("coordinator", "architect", "planner", "critic", "reviewer")
CODER_ROLES = ("investigator", "implementer", "gate")


def quals(eff, *, reasoning=False, **states):
    """Qualification results for every profile model; `states` overrides the
    state of named models (names use `_` for `-`)."""
    out = {}
    for name in eff.models:
        state = states.get(name.replace("-", "_"), "qualified")
        ok = state == "qualified"
        out[name] = QualificationResult(
            name, state, None if ok else "not-qualified", reasoning and ok, STAMP if ok else None
        )
    return out


def open_project():
    """A classified work project that narrows nothing."""
    data = fixture(PROJECT_WORK)
    del data["policy"]
    return data


def by_role(res):
    return {r.role: r for r in res.roles + res.auxiliary}


def resolve(profile=PROFILE_WORK, project=None, managed=None, overrides=None, **kw):
    eff = merge.merge(loaded(profile, project, managed), overrides=overrides)
    reasoning = kw.pop("reasoning", False)
    qs = kw.pop("qualifications", None) or quals(eff, reasoning=reasoning, **kw.pop("states", {}))
    return eff, resolve_all(eff, qs, allow_unqualified=kw.pop("allow_unqualified", False))


# ---------------------------------------------------------------- constants


def test_default_effort_table_covers_exactly_the_roles():
    assert set(roles.ROLE_DEFAULT_EFFORT) == set(ROLES)
    assert {r for r, e in roles.ROLE_DEFAULT_EFFORT.items() if e == "high"} == {
        "architect", "planner", "critic", "reviewer"
    }
    assert {r for r, e in roles.ROLE_DEFAULT_EFFORT.items() if e == "medium"} == {
        "coordinator", "investigator", "implementer"
    }
    assert roles.ROLE_DEFAULT_EFFORT["gate"] == "low"
    assert AUXILIARY == ("title", "compaction", "summary")
    assert ("openai-compatible", "responses") not in roles.EFFORT_OPTIONS
    assert all("max" not in path for path in roles.EFFORT_OPTIONS.values())


def test_resolutions_come_in_role_order():
    _, res = resolve(project=open_project())
    assert tuple(r.role for r in res.roles) == ROLES
    assert tuple(r.role for r in res.auxiliary) == AUXILIARY
    assert all(r.auxiliary for r in res.auxiliary) and not any(r.auxiliary for r in res.roles)


# --------------------------------------------------------------- precedence


def test_model_and_effort_precedence_with_origins():
    project = open_project()
    project["roles"] = {"implementer": {"model": "work-planner", "effort": "low"}, "planner": {"effort": "medium"}}
    overrides = {
        "critic": merge.RoleOverride(model="work-coder", effort="max"),
        "planner": merge.RoleOverride(effort="low"),
    }
    _, res = resolve(project=project, overrides=overrides)
    got = by_role(res)
    assert (got["coordinator"].model, got["coordinator"].reason, got["coordinator"].origin) == (
        "work-planner", "role-mapping", "profile"
    )
    assert (got["coordinator"].effort, got["coordinator"].effort_origin) == ("high", "profile")
    assert (got["implementer"].model, got["implementer"].reason, got["implementer"].origin) == (
        "work-planner", "role-mapping", "project"
    )
    assert (got["implementer"].effort, got["implementer"].effort_origin) == ("low", "project")
    assert (got["critic"].model, got["critic"].reason, got["critic"].origin) == (
        "work-coder", "override", "override"
    )
    assert (got["critic"].effort, got["critic"].effort_origin) == ("max", "override")
    assert (got["planner"].model, got["planner"].effort, got["planner"].effort_origin) == (
        "work-planner", "low", "override"
    )
    assert got["planner"].origin == "profile"


def test_missing_role_inherits_default_model_and_default_effort():
    data = fixture(PROFILE_WORK)
    del data["roles"]
    _, res = resolve(data)
    for role in ROLES:
        got = by_role(res)[role]
        assert (got.model, got.reason, got.origin) == ("work-coder", "default-model", "profile")
        assert (got.effort, got.effort_origin) == (roles.ROLE_DEFAULT_EFFORT[role], "default")


def test_resolution_carries_provider_metadata():
    _, res = resolve(project=open_project())
    got = by_role(res)["planner"]
    assert (got.provider_id, got.provider_kind, got.endpoint_family, got.model_id) == (
        "corp-gw-b", "openai-compatible", "responses", "example-vendor/example-planner"
    )
    assert got.status == "ok" and got.block_reason is None and got.qualification_state == "qualified"


# ------------------------------------------------------------ no fall-through


@pytest.mark.parametrize(
    "project,managed,reason",
    [
        (PROJECT_WORK_NARROW, None, "host-not-allowed"),
        (PROJECT_WORK, None, "not-allowed"),
        (PROJECT_WORK, MANAGED_WORK, "managed-not-allowed"),
        (None, MANAGED_WORK, "managed-not-allowed"),
        (None, MANAGED_STRICT, "denied"),
    ],
)
def test_excluded_provider_blocks_its_roles_without_fall_through(project, managed, reason):
    _, res = resolve(project=project, managed=managed)
    got = by_role(res)
    for role in PLANNER_ROLES + AUXILIARY:
        assert (got[role].status, got[role].block_reason) == ("blocked", reason), role
        assert got[role].model == "work-planner"
    for role in CODER_ROLES:
        assert got[role].status == "ok" and got[role].model == "work-coder"
    assert not res.launchable
    subjects = {f.subject for f in res.findings if f.code == "role-blocked"}
    assert ("planner", reason) in subjects and ("title", reason) in subjects
    assert all(f.blocking for f in res.findings if f.code == "role-blocked")


def test_explicit_project_mapping_is_blocked_while_default_model_would_pass():
    eff, res = resolve(project=PROJECT_WORK_NARROW)
    got = by_role(res)["planner"]
    assert (got.model, got.reason, got.origin, got.status, got.block_reason) == (
        "work-planner", "role-mapping", "project", "blocked", "host-not-allowed"
    )
    assert eff.values["default_model"].value == "work-coder"
    assert by_role(res)["implementer"].status == "ok"


def test_host_denied_reason():
    data = fixture(PROFILE_WORK)
    data["policy"]["denied_hosts"] = ["gateway-b.example.invalid"]
    _, res = resolve(data)
    assert by_role(res)["planner"].block_reason == "host-denied"


@pytest.mark.parametrize("state", ["missing", "failed", "stale", "mismatched", "malformed"])
def test_unqualified_role_model_stays_blocked_although_the_default_is_qualified(state):
    _, res = resolve(states={"work_planner": state})
    got = by_role(res)
    for role in PLANNER_ROLES + AUXILIARY:
        assert (got[role].status, got[role].block_reason) == ("blocked", "qualification-" + state)
        assert got[role].qualification_state == state
    assert got["implementer"].status == "ok"
    assert not res.launchable


def test_missing_qualification_entry_counts_as_missing():
    eff = merge.merge(loaded(PROFILE_WORK, open_project()))
    res = resolve_all(eff, {"work-coder": quals(eff)["work-coder"]})
    assert by_role(res)["planner"].block_reason == "qualification-missing"


def test_classification_incompatibility_is_defended_at_resolution():
    eff = merge.merge(loaded(PROFILE_PERSONAL, PROJECT_PERSONAL))
    forced = dataclasses.replace(eff, classification="work")
    res = resolve_all(forced, quals(forced))
    assert {r.block_reason for r in res.roles + res.auxiliary} == {"classification-incompatible"}
    eff = merge.merge(loaded(PROFILE_WORK, open_project()))
    personal = dataclasses.replace(eff, profile_classification="personal")
    res = resolve_all(personal, quals(personal))
    assert {r.block_reason for r in res.roles} == {"classification-incompatible"}


def test_excluded_provider_reason_wins_over_qualification():
    _, res = resolve(project=PROJECT_WORK, states={"work_planner": "failed"})
    assert by_role(res)["planner"].block_reason == "not-allowed"  # policy before qualification


# ---------------------------------------------------------------- auxiliary


def test_auxiliary_routing():
    _, res = resolve(project=open_project())
    for aux in res.auxiliary:
        assert (aux.model, aux.reason, aux.effort, aux.effort_options) == (
            "work-planner", "auxiliary-model", None, None
        )
    data = fixture(PROFILE_WORK)
    del data["auxiliary_model"]
    _, res = resolve(data, project=open_project())
    for aux in res.auxiliary:
        assert (aux.model, aux.reason) == ("work-coder", "default-model")
    assert sum(f.code == "summary-unused" for f in res.findings) == 1
    assert not any(f.code == "effort-omitted" and f.subject[0] in AUXILIARY for f in res.findings)


# ------------------------------------------------------------ effort gating


def _effort_profile(kind, family):
    data = fixture(PROFILE_PERSONAL)
    data["providers"]["openrouter"]["kind"] = kind
    data["providers"]["openrouter"]["endpoint_family"] = family
    return data


def _planner(kind, family, effort, supported):
    data = _effort_profile(kind, family)
    data["roles"] = {"planner": {"model": "personal-model", "effort": effort}}
    eff = merge.merge(loaded(data, PROJECT_PERSONAL))
    qs = {"personal-model": QualificationResult("personal-model", "qualified", None, supported, STAMP)}
    return by_role(resolve_all(eff, qs))["planner"], resolve_all(eff, qs)


EFFORT_ROWS = [
    # kind, family, effort, supported, expected options, expected diagnostic
    ("openai-compatible", "chat-completions", "high", True, {"reasoningEffort": "high"}, None),
    ("openai-compatible", "chat-completions", "low", True, {"reasoningEffort": "low"}, None),
    ("openai-compatible", "chat-completions", "medium", True, {"reasoningEffort": "medium"}, None),
    ("openai-compatible", "chat-completions", "max", True, None, "effort-max"),
    ("openai-compatible", "chat-completions", "high", False, None, "effort-no-capability"),
    ("openai-compatible", "chat-completions", "max", False, None, "effort-max"),
    ("openai-compatible", "responses", "high", True, None, "effort-no-mapping"),
    ("openai-compatible", "responses", "high", False, None, "effort-no-capability"),
    ("openrouter", "chat-completions", "high", True, {"reasoning": {"effort": "high"}}, None),
    ("openrouter", "chat-completions", "low", True, {"reasoning": {"effort": "low"}}, None),
    ("openrouter", "chat-completions", "max", True, None, "effort-max"),
    ("openrouter", "chat-completions", "high", False, None, "effort-no-capability"),
]


@pytest.mark.parametrize("kind,family,effort,supported,options,diagnostic", EFFORT_ROWS)
def test_effort_gating_matrix(kind, family, effort, supported, options, diagnostic):
    got, res = _planner(kind, family, effort, supported)
    assert got.effort == effort
    assert got.effort_options == options
    assert got.effort_diagnostic == diagnostic
    omitted = [f.subject for f in res.findings if f.code == "effort-omitted" and f.subject[0] == "planner"]
    assert omitted == ([] if diagnostic is None else [("planner", diagnostic)])
    if options is not None:
        with pytest.raises(TypeError):
            got.effort_options[next(iter(got.effort_options))] = 1


def test_max_is_never_emitted_for_any_kind_family_or_record():
    for kind, family in (("openai-compatible", "chat-completions"), ("openrouter", "chat-completions"),
                         ("openai-compatible", "responses")):
        for supported in (True, False):
            got, _ = _planner(kind, family, "max", supported)
            assert got.effort_options is None


def test_effort_is_omitted_for_roles_that_are_not_ok():
    _, res = resolve(states={"work_planner": "failed"}, reasoning=True)
    planner = by_role(res)["planner"]
    assert planner.effort_options is None and planner.effort_diagnostic == "effort-unqualified"
    assert by_role(res)["implementer"].effort_diagnostic in (None, "effort-no-mapping", "effort-max")


def test_real_probe_shaped_records_never_enable_effort():
    _, res = resolve(project=open_project())  # reasoning support unknown in every record
    assert {r.effort_diagnostic for r in res.roles} <= {"effort-no-capability", "effort-max"}
    assert all(r.effort_options is None for r in res.roles)


# ---------------------------------------------------- unqualified handling


def test_allow_unqualified_marks_and_is_never_launchable():
    _, res = resolve(project=open_project(), states={"work_planner": "missing"}, allow_unqualified=True)
    got = by_role(res)
    assert got["planner"].status == "unqualified"
    assert got["planner"].block_reason == "qualification-missing"
    assert got["implementer"].status == "ok"
    assert not res.launchable
    assert res.unqualified_models == ("work-planner",)
    assert ("planner", "qualification-missing") in {f.subject for f in res.findings if f.code == "role-unqualified"}
    assert not any(f.code == "role-blocked" for f in res.findings)


def test_allow_unqualified_does_not_unblock_policy_exclusions():
    _, res = resolve(project=PROJECT_WORK_NARROW, allow_unqualified=True)
    assert by_role(res)["planner"].status == "blocked"


def test_allow_unqualified_refused_for_work_under_managed_policy():
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK, MANAGED_WORK))
    with pytest.raises(AllowUnqualifiedRefused):
        resolve_all(eff, quals(eff), allow_unqualified=True)
    eff = merge.merge(loaded(PROFILE_WORK, PROJECT_WORK))
    resolve_all(eff, quals(eff), allow_unqualified=True)
    personal_managed = {"schema_version": 1, "policy": {"sharing": "deny"}}
    eff = merge.merge(loaded(PROFILE_PERSONAL, PROJECT_PERSONAL, personal_managed))
    resolve_all(eff, quals(eff), allow_unqualified=True)


# --------------------------------------------------------------- launchable


def test_launchable_requires_every_role_ok_and_no_blocking_merge_finding():
    _, res = resolve(project=open_project())
    assert res.launchable and not res.unqualified_models
    _, res = resolve()  # no project file: blocking classification finding
    assert all(r.status == "ok" for r in res.roles + res.auxiliary)
    assert not res.launchable


# ------------------------------------------------------------------ logging


LOG_FORMAT = re.compile(
    r"role=(\w+) model=([\w-]+) provider=([\w-]+) status=(ok|blocked|unqualified) "
    r"reason=([\w-]+) origin=(default|profile|project|override)"
)


def test_log_lines_carry_metadata_only(caplog):
    with caplog.at_level(logging.INFO, logger="quoin.opencode.roles"):
        _, res = resolve(project=PROJECT_WORK_NARROW, states={"work_coder": "stale"})
    lines = [r.getMessage() for r in caplog.records if r.name == "quoin.opencode.roles"]
    assert len(lines) == len(ROLES) + len(AUXILIARY)
    assert all(LOG_FORMAT.fullmatch(line) for line in lines)
    profile = fixture(PROFILE_WORK)
    forbidden = ["https://", "keychain:", "env:", "example.invalid", "QUOIN_", "_API_KEY", "example-vendor"]
    for provider in profile["providers"].values():
        forbidden += [provider["base_url"], provider["credential_ref"]]
    for model in profile["models"].values():
        forbidden.append(model["model_id"])
    text = "\n".join(lines)
    for token in forbidden:
        assert token not in text, token
    assert "role=planner model=work-planner provider=corp-gw-b status=blocked reason=role-mapping origin=project" in lines


def test_module_configures_no_handler():
    assert roles._LOG.handlers == []
    assert roles._LOG.name == "quoin.opencode.roles"


# ---------------------------------------------------------------------- AST


def test_roles_imports_are_limited():
    tree = ast.parse((SRC_DIR / "roles.py").read_text(encoding="utf-8"))
    relative = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            relative |= {node.module} if node.module else {a.name for a in node.names}
    assert relative <= {"config", "errors", "generate", "merge", "qualification"}


# =============================================================================
# Whole-pipeline matrix: profile x project x managed, through real files
# =============================================================================

# Attempts that must be refused outright, run from their invalid fixtures.
EXTRA_ROWS = {
    "personal-profile-for-work": ("merge-error", "personal-profile-for-work"),
    "personal-provider-kind-for-work": ("merge-error", "personal-provider-kind-for-work"),
}

WORK_PLANNER_GROUP = PLANNER_ROLES + AUXILIARY


def test_combinations_table_is_the_full_product():
    assert set(COMBINATIONS) == set(itertools.product(PROFILES, PROJECTS, MANAGEDS))
    assert len(COMBINATIONS) == 45


def _groups(profile_key):
    if profile_key == "work":
        return {"second": WORK_PLANNER_GROUP, "first": CODER_ROLES}
    return {"second": tuple(ROLES) + AUXILIARY, "first": ()}


@pytest.mark.parametrize("key", sorted(COMBINATIONS), ids=["-".join(k) for k in sorted(COMBINATIONS)])
def test_combination_outcome(key, tmp_path):
    expected = COMBINATIONS[key]
    out = run_pipeline(tmp_path, key)
    assert set(out.env.reads) <= ALLOWED_ENV_READS
    if expected[0] == "load-error":
        assert out.stage == "load" and out.error is not None
        assert {e.rejection_class for e in out.error.errors} == {expected[1]}
        return
    assert out.error is None and out.stage == "resolve"
    _, effective_ids, excluded, classification, launchable, blocked = expected
    eff, res = out.effective, out.resolutions
    assert eff.effective_providers == effective_ids
    assert dict(eff.excluded_providers) == excluded
    assert eff.classification == classification
    assert res.launchable is launchable
    got = by_role(res)
    groups = _groups(key[0])
    for role in groups["second"]:
        if blocked is None:
            assert (got[role].status, got[role].block_reason) == ("ok", None), role
        else:
            assert (got[role].status, got[role].block_reason) == ("blocked", blocked), role
    for role in groups["first"]:
        assert (got[role].status, got[role].block_reason) == ("ok", None), role
    if key[1] == "unclassified" or key[1] == "none":
        assert not res.launchable
        assert [e.message_id for e in merge.compile_blockers(eff)] == [
            "missing-classification" if key[1] == "unclassified" else "no-project-file"
        ]
    else:
        assert merge.compile_blockers(eff) == ()


def _all_outcomes(tmp_path_factory):
    return {key: run_pipeline(tmp_path_factory.mktemp("matrix"), key) for key in sorted(COMBINATIONS)}


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    return _all_outcomes(tmp_path_factory)


def test_work_never_reaches_a_personal_provider_or_profile(outcomes):
    checked = 0
    for key, out in outcomes.items():
        if out.stage != "resolve" or out.effective.classification != "work":
            continue
        profile_data = json.loads(
            (helpers.SOURCE_DIR / "adapters" / "opencode" / "fixtures" / "runtime-config" / PROFILES[key[0]]).read_text(
                encoding="utf-8"
            )
        )
        assert profile_data["classification"] == "work"
        resolutions = out.resolutions.roles + out.resolutions.auxiliary
        assert len(resolutions) == len(ROLES) + len(AUXILIARY)
        for r in resolutions:
            assert r.provider_kind != "openrouter"
            assert r.provider_id in profile_data["providers"]
            if r.status == "ok":
                assert r.provider_id in out.effective.effective_providers
            checked += 1
    assert checked > 0


def test_matrix_is_not_vacuous(outcomes):
    work_ok = [k for k, o in outcomes.items() if o.stage == "resolve" and o.effective.classification == "work"]
    personal_ok = [k for k, o in outcomes.items() if o.stage == "resolve" and o.effective.classification == "personal"]
    raised = [k for k, o in outcomes.items() if o.stage != "resolve"]
    assert len(work_ok) > 10 and len(personal_ok) > 5 and len(raised) == 12
    for managed_key in ("managed-work", "managed-strict"):
        ok_under_managed = [
            r
            for k, o in outcomes.items()
            if k[0] == "work" and k[2] == managed_key
            for r in o.resolutions.roles + o.resolutions.auxiliary
            if r.status == "ok"
        ]
        assert ok_under_managed, managed_key


@pytest.mark.parametrize("case_id", sorted(EXTRA_ROWS))
def test_attempt_fixtures_are_refused(case_id, tmp_path):
    from test_opencode_runtime_config import CASES

    case = next(c for c in CASES["invalid"] if c["id"] == case_id)
    env, load = run_case(case, tmp_path)
    with pytest.raises(ConfigErrors) as info:
        merge.merge(load())
    assert {e.rejection_class for e in info.value.errors} == {EXTRA_ROWS[case_id][1]}
