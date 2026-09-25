"""The pure OpenCode generator.

Renders Quoin's OpenCode commands, skills, agents, instruction document and
config file from portable inputs (the skill catalog, the manifest, the
overlay data and the bundle contracts). Nothing in this module touches a
filesystem outside an explicit source directory it is given, and nothing
here runs a subprocess.

Filled in task by task: role permission maps and the delegation graph land
first, so the security posture is pinned by tests before anything renders
them into a file; the rest of the pipeline (`GeneratorInputs`, `load_inputs`,
`extract_sections`, `render`, `digest`, and the forbidden-output check)
follows in later tasks.
"""
from __future__ import annotations

from typing import Dict, List

from quoin.opencode_adapter import names

# The directory Quoin's own workflow artifacts live under, relative to a
# project root. Templates and overlays never spell this literal; they use
# the `{{ARTIFACT_ROOT}}` placeholder, expanded from this constant, so a
# rendered file can name the user's own workflow folder without any shipped
# source under this package ever containing it.
ARTIFACT_ROOT = ".workflow_artifacts"

# Every Quoin role may load any `quoin-*` skill and nothing else.
SKILL_RULE = {"*": "deny", "quoin-*": "allow"}

# OpenCode's own built-in read posture (`packages/opencode/src/agent/agent.ts`
# L119-136). A read-only role emits this instead of a bare `allow`, so it
# never loosens the built-in `.env` handling.
READ_RULE = {"*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow"}

# The helper scripts (`quoin/core/scripts/<name>.py`; see scripts.py) each
# role may run through `quoin opencode script <name> *`. Only the
# investigator holds a write-capable script (scripts.WRITE_CAPABLE_SCRIPTS):
# `generate_discovery_map`.
ROLE_SCRIPTS: Dict[str, tuple] = {
    "architect": ("path_resolve", "validate_artifact"),
    "planner": ("path_resolve", "validate_artifact"),
    "gate": ("path_resolve", "validate_artifact"),
    "implementer": ("path_resolve", "validate_artifact"),
    "coordinator": (
        "checkpoint_picker",
        "classify_critic_issues",
        "handoff_validate",
        "path_resolve",
        "validate_artifact",
    ),
    "investigator": ("generate_discovery_map", "path_resolve"),
    "critic": (),
    "reviewer": (),
}

# The last two keys of every shell map (last-match-wins), so a redirected,
# heredoc or process-substitution statement is asked about even when its
# command is an allowed script (D-03, R-13): OpenCode matches a shell
# permission pattern against the whole statement text, redirections
# included, and never path-checks a redirection target
# (`packages/opencode/src/tool/shell.ts` L99, L119-121).
REDIRECT_RULE = {"*>*": "ask", "*<*": "ask"}

_READ_ONLY_ROLES = ("critic", "reviewer")

# The eight Quoin roles this generator knows about. Used only for the
# unknown-role error message; nothing here iterates this tuple to build a
# role map.
ROLES = (
    "coordinator",
    "investigator",
    "architect",
    "planner",
    "implementer",
    "critic",
    "reviewer",
    "gate",
)


def artifact_edit_rule(base: str) -> Dict[str, str]:
    """The edit permission map shared by every role that may touch the
    artifact root: `base` for everything else, allow inside the artifact
    root whether it sits at the project root or nested under a sub-repo."""
    return {
        "*": base,
        "%s/*" % ARTIFACT_ROOT: "allow",
        "*/%s/*" % ARTIFACT_ROOT: "allow",
    }


def shell_rule(role: str) -> Dict[str, str]:
    """The bash permission map for `role`: ask by default, one allow per
    allowlisted helper script (argument tail included, never argument-wide),
    then the redirection ask rules, always last so they win under
    last-match evaluation."""
    rule: Dict[str, str] = {"*": "ask"}
    for name in sorted(ROLE_SCRIPTS.get(role, ())):
        rule["quoin opencode script %s *" % name] = "allow"
    rule.update(REDIRECT_RULE)
    return rule


def role_permissions(role: str) -> Dict[str, object]:
    """The OpenCode agent frontmatter `permission` map for `role`.

    Key insertion order is emission order (`frontmatter.emit` never sorts).
    Every call returns fresh mappings, so callers may mutate the result
    without affecting later calls. See the stage plan's Procedures section
    for the source table this mirrors.
    """
    if role in _READ_ONLY_ROLES:
        return {
            "*": "deny",
            "read": dict(READ_RULE),
            "glob": "allow",
            "grep": "allow",
            "lsp": "allow",
            "skill": dict(SKILL_RULE),
        }
    if role == "investigator":
        return {
            "edit": artifact_edit_rule("deny"),
            "bash": shell_rule(role),
            "task": "deny",
            "skill": dict(SKILL_RULE),
        }
    if role in ("architect", "planner"):
        return {
            "edit": artifact_edit_rule("deny"),
            "bash": shell_rule(role),
            "task": {
                "*": "deny",
                "quoin-investigator": "allow",
                "quoin-critic": "allow",
            },
            "skill": dict(SKILL_RULE),
        }
    if role == "gate":
        return {
            "edit": artifact_edit_rule("deny"),
            "bash": shell_rule(role),
            "task": "deny",
            "skill": dict(SKILL_RULE),
        }
    if role == "coordinator":
        return {
            "edit": artifact_edit_rule("ask"),
            "bash": shell_rule(role),
            "task": {
                "*": "deny",
                "quoin-investigator": "allow",
                "quoin-critic": "allow",
                "quoin-reviewer": "allow",
            },
            "skill": dict(SKILL_RULE),
        }
    if role == "implementer":
        return {
            "bash": shell_rule(role),
            "task": "deny",
            "skill": dict(SKILL_RULE),
        }
    raise ValueError("unknown role %r, expected one of %s" % (role, ROLES))


def task_targets(role: str) -> List[str]:
    """The sorted list of agent names `role` may Task-delegate to: the
    allowed patterns of its own `task` permission rule, excluding the
    catch-all. Empty when the role's `task` rule is a bare action (no
    delegation) or absent."""
    rule = role_permissions(role).get("task")
    if not isinstance(rule, dict):
        return []
    return sorted(name for name, action in rule.items() if name != "*" and action == "allow")


def check_task_graph(roles_by_name: Dict[str, dict]) -> List[str]:
    """Validate the delegation graph implied by every role's `task` rule.

    `roles_by_name` maps a role name to an object carrying at least a
    `"mode"` key (the manifest's own `roles` shape). Every allowed task
    target must be a role declared in `roles_by_name` whose own mode is
    `subagent` or `all` and whose own `task` rule denies further
    delegation, so depth never exceeds 1; every role with an allowed edge
    must itself be `primary`, because a role that can be a Task target
    cannot also be a source (D-01). Returns offender-naming messages, empty
    when the graph is clean.
    """
    agent_to_role = {names.role_agent_name(role): role for role in roles_by_name}
    errors: List[str] = []
    for role, role_def in roles_by_name.items():
        for target_agent in task_targets(role):
            target_role = agent_to_role.get(target_agent)
            if target_role is None:
                errors.append(
                    "role '%s' has an allowed task edge to unknown target '%s'" % (role, target_agent)
                )
                continue
            if role_def.get("mode") != "primary":
                errors.append(
                    "role '%s' (mode %r) has an allowed task edge to '%s' (role '%s'), "
                    "but only a 'primary' role may delegate"
                    % (role, role_def.get("mode"), target_agent, target_role)
                )
            target_def = roles_by_name.get(target_role, {})
            if target_def.get("mode") not in ("subagent", "all"):
                errors.append(
                    "role '%s' targets '%s' (role '%s'), whose mode is %r, expected 'subagent' or 'all'"
                    % (role, target_agent, target_role, target_def.get("mode"))
                )
            if task_targets(target_role):
                errors.append(
                    "role '%s' targets '%s' (role '%s'), which itself has an allowed task edge"
                    % (role, target_agent, target_role)
                )
    return errors
