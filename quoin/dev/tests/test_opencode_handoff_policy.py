"""The adapter-side policy comparison when a continuation is loaded."""
from __future__ import annotations

import copy
import shutil

import pytest

import _opencode_handoff_helpers as hh
from quoin.opencode_adapter import handoff, runstore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

BASE = {
    "classification": "personal", "sharing": "deny", "external_writes": "deny", "isolation": "managed",
    "cross_profile_fallback": False, "allowed_providers": [], "denied_providers": [],
    "allowed_hosts": [], "denied_hosts": [], "fallback": "stop", "web_tools": "deny",
    "package_install": "deny", "mcp.local": "deny", "mcp.remote": "deny", "mcp.allowed": [],
    "lsp.mode": "deny", "lsp.allowed": [], "formatter.mode": "deny", "formatter.allowed": [],
    "network.identity_check": "required", "network.allowed_addresses": ["a"], "network.pins": ["p"],
    "repositories.allowed_roots": ["~/r"], "logging.content": "metadata",
    "retention.runs_days": 30, "retention.cache_days": 30, "retention.logs_days": 30,
    "retention.crash_days": 30, "retention.exports_days": 30,
}


def policy(**overrides):
    out = copy.deepcopy(BASE)
    out.update(overrides)
    return out


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    project = hh.Project(tmp_path, monkeypatch, passed=False)
    project.seed_gate("architect", None, "PASS")
    project.record("plan", stage=1)
    project.seed_gate("plan", 1, "PASS")
    return project


def completed(fx):
    return [c for c in fx.build()["completed"] if c["run_id"]]


def stamp_run(fx, run_id, recorded, *, profile="personal", updated_at=None, ended_at=None):
    directory = fx.directory()
    record = runstore.load_record(directory, run_id)
    if recorded is not None:
        record.setdefault("prepared", {})["effective_config"] = {"profile": profile, "policy": recorded}
    if updated_at:
        record["updated_at"] = updated_at
    if ended_at:
        record["attempts"] = [{"attempt": 1, "ended_at": ended_at}]
    runstore.write_record(directory, record)


def load(fx, requested, **kw):
    handoff.write(fx.root, fx.task, fx.build(), source_dir=fx.source)
    return handoff.load_for_continuation(
        fx.root, fx.task, source_dir=fx.source, requested_scope=hh.fixed_scope(), requested_policy=requested, **kw,
    )


def refused(fx, requested, **kw):
    with pytest.raises(handoff.HandoffRefused) as info:
        load(fx, requested, **kw)
    return info.value


def seed(fx, recorded=BASE):
    (item,) = completed(fx)
    stamp_run(fx, item["run_id"], recorded)
    return item["run_id"]


def test_an_equal_policy_passes(fx):
    seed(fx)
    assert load(fx, policy())["task"] == "t1"


WIDENINGS = [
    ("web_tools", {"web_tools": "allow"}),
    ("mcp.allowed", {"mcp.allowed": ["x"]}),
    ("allowed_hosts", {"allowed_hosts": ["h-1"]}),
    ("logging.content", {"logging.content": "summary"}),
    ("retention.runs_days", {"retention.runs_days": 90}),
    ("network.pins", {"network.pins": None}),
    ("repositories.allowed_roots", {"repositories.allowed_roots": ["~/r", "~/other"]}),
]


@pytest.mark.parametrize("field,change", WIDENINGS)
def test_each_widening_class_is_refused_with_its_field(fx, field, change):
    seed(fx)
    exc = refused(fx, policy(**change))
    assert exc.code == "policy-widened" and exc.reasons == ("effective_config.%s" % field,)


def test_narrowing_passes_including_work_after_a_personal_record(fx):
    seed(fx, policy(web_tools="allow", **{"retention.runs_days": None}))
    assert load(fx, policy(classification="work"))["task"] == "t1"


def test_a_pre_upgrade_run_record_is_unverifiable_and_names_the_command(fx):
    seed(fx, None)
    exc = refused(fx, policy(), resume_hint="quoin run X --continue")
    assert exc.code == "scope-unverifiable"
    assert "quoin run X --continue --accept-unverified-policy" in exc.message


def test_a_missing_run_record_is_unverifiable(fx):
    (item,) = completed(fx)
    for path in runstore.run_paths(fx.directory(), item["run_id"]).record.parent.glob("*"):
        if item["run_id"] in path.name:
            path.unlink()
    assert refused(fx, policy()).code == "scope-unverifiable"


def test_a_run_record_for_another_profile_is_unverifiable(fx):
    (item,) = completed(fx)
    stamp_run(fx, item["run_id"], BASE, profile="other")
    assert refused(fx, policy()).code == "scope-unverifiable"


def test_a_record_with_no_completed_run_ids_passes(fx):
    record = fx.build()
    record["completed"] = [c for c in record["completed"] if not c["run_id"]]
    handoff.write(fx.root, fx.task, record, source_dir=fx.source)
    assert handoff.load_for_continuation(
        fx.root, fx.task, source_dir=fx.source, requested_policy=policy(),
    )["task"] == "t1"


def test_a_record_from_another_runtime_is_not_compared(fx):
    record = fx.build()
    record["origin_runtime"] = "other-runtime"
    handoff.write(fx.root, fx.task, record, source_dir=fx.source)
    assert handoff.load_for_continuation(
        fx.root, fx.task, source_dir=fx.source, requested_policy=policy(web_tools="allow"),
    )["origin_runtime"] == "other-runtime"


def test_the_latest_finished_run_is_the_one_compared(tmp_path, monkeypatch):
    project = hh.Project(tmp_path, monkeypatch, passed=False)
    project.record("architect", stage=None)
    project.seed_gate("architect", None, "PASS")
    project.record("plan", stage=1)
    project.seed_gate("plan", 1, "PASS")
    items = {c["phase"]: c["run_id"] for c in completed(project)}
    wide = policy(web_tools="allow")
    # the plan run finished last (a rerun): only its record is compared
    stamp_run(project, items["architect"], BASE, updated_at="2026-01-01T00:00:00Z")
    stamp_run(project, items["plan"], wide, updated_at="2026-01-02T00:00:00Z")
    assert load(project, wide)["task"] == "t1"
    # an attempt end outranks the record's last update: the architect run is now the latest
    stamp_run(project, items["architect"], BASE, ended_at="2026-02-01T00:00:00Z")
    assert refused(project, wide).code == "policy-widened"


def test_the_accepted_run_passes_and_a_newer_run_is_checked_again(fx):
    run_id = seed(fx, None)
    outcome = {}
    assert load(fx, policy(), accept_unverified=run_id, outcome=outcome)["task"] == "t1"
    assert outcome == {"unverified_accepted": run_id}
    assert refused(fx, policy(), accept_unverified="some-other-run").code == "scope-unverifiable"


def test_accepting_an_unverified_run_never_bypasses_a_widening(fx):
    run_id = seed(fx)
    assert refused(fx, policy(web_tools="allow"), accept_unverified=run_id).code == "policy-widened"
    assert refused(fx, policy(web_tools="allow"), accept_unverified=True).code == "policy-widened"


def test_a_callable_policy_is_only_evaluated_when_something_is_compared(fx):
    calls = []

    def lazy():
        calls.append(1)
        return policy()

    handoff.write(fx.root, fx.task, fx.build(), source_dir=fx.source)
    seed(fx)
    handoff.load_for_continuation(fx.root, fx.task, source_dir=fx.source, requested_policy=lazy)
    assert calls == [1]


def _calls(path, name):
    import ast
    from pathlib import Path

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                yield node, {kw.arg for kw in node.keywords}


def test_every_production_continuation_load_and_coordinator_passes_the_requested_policy():
    from quoin import cli
    from quoin.opencode_adapter import workflow

    seen_loads = seen_coordinators = 0
    for path in (cli.__file__, workflow.__file__):
        for _, keywords in _calls(path, "load_for_continuation"):
            if "requested_scope" in keywords:
                seen_loads += 1
                assert "requested_policy" in keywords
        for _, keywords in _calls(path, "Coordinator"):
            seen_coordinators += 1
            assert "requested_policy" in keywords
    assert seen_loads >= 2 and seen_coordinators >= 1


def _profile(fx, requested=None):
    return handoff.resolve_profile(fx.root, fx.task, requested, evaluate=lambda name: {"profile": name})


def test_the_coordinators_workflow_profile_resolves_before_the_legacy_key(fx):
    fx.settings(workflow={"profile": "from-workflow"}, profile="legacy")
    assert _profile(fx) == ({"profile": "from-workflow"}, "workflow-state")


def test_the_legacy_profile_key_still_resolves(fx):
    fx.settings(profile="legacy")
    assert _profile(fx) == ({"profile": "legacy"}, "workflow-state")


def test_with_neither_key_the_run_record_is_used(fx):
    fx.seed_run("plan", 1, "completed", profile="from-run")
    scope, source = _profile(fx)
    assert scope == {"profile": "from-run"} and source == "run-record"
