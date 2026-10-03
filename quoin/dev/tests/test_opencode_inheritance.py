"""Rule evaluation and the child-inheritance check run at generation time."""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import boundaries, generate, names

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"

_ROLES = ("coordinator", "investigator", "architect", "planner", "implementer", "critic", "reviewer", "gate")


def _maps():
    return {role: generate.role_permissions(role) for role in _ROLES}


def _graph():
    return {role: generate.task_targets(role) for role in _ROLES}


def _patch_role(monkeypatch, role, **changes):
    real = generate.role_permissions

    def fake(name):
        perms = real(name)
        if name == role:
            perms = dict(perms)
            perms.update(changes)
        return perms

    monkeypatch.setattr(generate, "role_permissions", fake)


# --- evaluate_rule ---


def test_evaluate_rule_bare_action_and_last_match_wins():
    assert boundaries.evaluate_rule("deny", "anything") == "deny"
    rule = {"*": "ask", "quoin opencode script x *": "allow", "*>*": "ask"}
    assert boundaries.evaluate_rule(rule, "quoin opencode script x a b") == "allow"
    assert boundaries.evaluate_rule(rule, "quoin opencode script x") == "allow"
    assert boundaries.evaluate_rule(rule, "quoin opencode script x a > out") == "ask"
    assert boundaries.evaluate_rule(rule, "ls") == "ask"
    assert boundaries.evaluate_rule({"a": "allow"}, "b") is None


def _local_wildcard(pattern):
    if pattern.endswith(" *"):
        body = re.escape(pattern[:-2]).replace(r"\*", ".*")
        return re.compile(r"^%s( .*)?$" % body)
    return re.compile(r"^%s$" % re.escape(pattern).replace(r"\*", ".*"))


def _local_evaluate(rule, text):
    if isinstance(rule, str):
        return rule
    action = None
    for pattern, value in rule.items():
        if _local_wildcard(pattern).match(text):
            action = value
    return action


def test_evaluate_rule_agrees_with_the_generator_test_evaluator_on_every_role_map():
    subjects = [
        "src/a.py", ".env", "a.env.example", ".workflow_artifacts/t/plan.md", "x/.workflow_artifacts/t/plan.md",
        "quoin-plan", "plan", "quoin-critic", "ls -la", "quoin opencode script path_resolve a",
        "quoin opencode script path_resolve", "quoin opencode script path_resolve a > b", "echo hi < c",
    ]
    for role in _ROLES:
        for key, rule in generate.role_permissions(role).items():
            for subject in subjects:
                assert boundaries.evaluate_rule(rule, subject) == _local_evaluate(rule, subject), (role, key, subject)


# --- check_inheritance ---


def test_identical_maps_pass():
    rule = {"*": "ask", "quoin opencode script path_resolve *": "allow", "*>*": "ask"}
    maps = {"coordinator": {"bash": dict(rule)}, "critic": {"bash": dict(rule)}}
    assert boundaries.check_inheritance(maps, {"coordinator": ["quoin-critic"]}) == []


def test_real_role_set_has_no_findings():
    assert boundaries.check_inheritance(_maps(), _graph()) == []


@pytest.mark.parametrize("parent", ["architect", "planner", "coordinator"])
def test_investigator_and_read_only_children_pass_under_each_parent(parent):
    graph = {parent: ["quoin-investigator", "quoin-critic"]}
    maps = {role: generate.role_permissions(role) for role in (parent, "investigator", "critic")}
    assert boundaries.check_inheritance(maps, graph) == []


def test_reviewer_under_coordinator_passes():
    maps = {role: generate.role_permissions(role) for role in ("coordinator", "reviewer")}
    assert boundaries.check_inheritance(maps, {"coordinator": ["quoin-reviewer"]}) == []


def test_skill_map_bound_follows_the_later_overlapping_pattern():
    skill = {"*": "deny", "quoin-*": "allow"}
    maps = {"architect": {"skill": dict(skill)}, "critic": {"skill": {"*": "deny", "quoin-*": "allow"}}}
    assert boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]}) == []
    maps["critic"]["skill"] = {"*": "allow"}
    findings = boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]})
    assert findings and "skill" in findings[0] and "critic" in findings[0] and "architect" in findings[0]


def test_a_more_specific_parent_deny_after_a_broad_allow_caps_the_child():
    maps = {
        "architect": {"webfetch": "deny"},
        "critic": {"webfetch": "allow"},
    }
    findings = boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]})
    assert len(findings) == 1 and "webfetch" in findings[0]
    maps = {
        "architect": {"bash": {"*": "allow", "rm *": "deny"}},
        "critic": {"bash": {"*": "allow"}},
    }
    assert boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]})


def test_absent_parent_key_resolves_to_the_parent_catch_all():
    maps = {"architect": {"*": "deny"}, "critic": {"read": "allow"}}
    assert boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]})


def test_script_allow_exemption():
    parent_bash = {"*": "ask", "quoin opencode script path_resolve *": "allow", "*>*": "ask"}
    child_bash = {"*": "ask", "quoin opencode script validate_artifact *": "allow", "*>*": "ask"}
    maps = {"architect": {"bash": parent_bash, "edit": "deny"}, "critic": {"bash": child_bash, "edit": "deny"}}
    assert boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]}) == []
    write_bash = {"*": "ask", "quoin opencode script generate_discovery_map *": "allow", "*>*": "ask"}
    maps = {"architect": {"bash": parent_bash, "edit": {"*": "deny"}}, "critic": {"bash": write_bash, "edit": "deny"}}
    assert boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]})
    maps["architect"]["edit"] = generate.artifact_edit_rule("deny")
    assert boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]}) == []


def test_non_script_child_allow_is_not_exempt():
    parent_bash = {"*": "ask"}
    child_bash = {"*": "ask", "git status *": "allow"}
    maps = {"architect": {"bash": parent_bash}, "critic": {"bash": child_bash}}
    assert boundaries.check_inheritance(maps, {"architect": ["quoin-critic"]})


def test_unknown_edge_target_is_left_to_the_graph_check():
    assert boundaries.check_inheritance({"architect": {}}, {"architect": ["quoin-ghost"]}) == []


# --- generate.render ---


def test_the_real_role_set_renders():
    files = generate.render_source_dir(SOURCE_DIR)
    assert any(path.endswith("quoin-investigator.md") for path in files)


def test_render_fails_when_a_child_may_edit_more_than_its_parent(monkeypatch):
    _patch_role(monkeypatch, "investigator", edit="allow")
    with pytest.raises(generate.GenerationError) as exc:
        generate.render_source_dir(SOURCE_DIR)
    text = str(exc.value)
    assert "investigator" in text and "architect" in text and "edit" in text


def test_render_fails_when_a_child_loosens_an_action_the_parent_denies(monkeypatch):
    real = generate.role_permissions

    def fake(role):
        perms = dict(real(role))
        if role == "architect":
            perms["webfetch"] = "deny"
        if role == "critic":
            perms["webfetch"] = "allow"
        return perms

    monkeypatch.setattr(generate, "role_permissions", fake)
    with pytest.raises(generate.GenerationError, match="webfetch"):
        generate.render_source_dir(SOURCE_DIR)


def test_render_fails_on_a_non_exempt_script_allow(monkeypatch):
    real = generate.role_permissions

    def fake(role):
        perms = real(role)
        if role == "critic":
            perms = dict(perms)
            perms["bash"] = {"*": "ask", "git push *": "allow"}
        return perms

    monkeypatch.setattr(generate, "role_permissions", fake)
    with pytest.raises(generate.GenerationError, match="bash"):
        generate.render_source_dir(SOURCE_DIR)


def test_generate_discovery_map_passes_under_every_parent():
    for parent in ("architect", "planner", "coordinator"):
        maps = {role: generate.role_permissions(role) for role in (parent, "investigator")}
        assert "generate_discovery_map" in str(maps["investigator"]["bash"])
        assert boundaries.check_inheritance(maps, {parent: ["quoin-investigator"]}) == []


def test_a_depth_two_graph_is_still_refused_by_the_graph_check(monkeypatch):
    real = generate.role_permissions

    def fake(role):
        perms = real(role)
        if role == "investigator":
            perms = dict(perms)
            perms["task"] = {"*": "deny", "quoin-critic": "allow"}
        return perms

    monkeypatch.setattr(generate, "role_permissions", fake)
    with pytest.raises(generate.GenerationError):
        generate.render_source_dir(SOURCE_DIR)


def test_agent_files_carry_no_model_key_so_every_role_shares_one_profile():
    files = generate.render_source_dir(SOURCE_DIR)
    agent_files = {p: f for p, f in files.items() if "/agents/" in p}
    assert agent_files
    for rendered in agent_files.values():
        assert not re.search(r"^model:", rendered.content.decode("utf-8"), re.M)
    injected = dict(files)
    path = next(iter(agent_files))
    original = agent_files[path]
    broken = original.content.decode("utf-8").replace("\n---\n", "\nmodel: x/y\n---\n", 1)
    injected[path] = dataclasses.replace(original, content=broken.encode("utf-8"))
    assert generate.check_rendered(injected)
    assert names.role_agent_name("critic") == "quoin-critic"
