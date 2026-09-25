"""OpenCode M1a generator tests.

Grown task by task as the pure generator (`generate.py`), its helper
modules (`frontmatter.py`, `scripts.py`) and their supporting data
land. Expected id and path sets are built locally inside each test
function, or read from the manifest at test time, never kept as a
module-level ALL-CAPS collection (mirrors `test_opencode_manifest.py`,
which the registration roster census would otherwise pick up).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from quoin.opencode_adapter import frontmatter, generate, scripts

# Mirrors generate.ARTIFACT_ROOT, added when the generator module lands;
# a later task replaces this literal with that import directly.
_ARTIFACT_ROOT = ".workflow_artifacts"

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"


# --- frontmatter: emit/parse round trip ---


def test_frontmatter_round_trip_preserves_key_order_and_special_keys():
    fields = {
        "name": "quoin-plan",
        "*": "deny",
        "quoin-*": "allow",
        "%s/*" % _ARTIFACT_ROOT: "allow",
        "quotes and \\backslash\\": 'has "quotes" and \\backslashes\\',
        "metadata": {
            "canonical_id": "plan",
            "source_digest": "sha256:abc123",
            "generator": "quoin",
        },
    }
    text = frontmatter.emit(fields)
    parsed_fields, body = frontmatter.parse(text)
    assert parsed_fields == fields
    assert list(parsed_fields.keys()) == list(fields.keys())
    assert list(parsed_fields["metadata"].keys()) == list(fields["metadata"].keys())
    assert body == ""


def test_frontmatter_emit_is_pure_ascii_and_deterministic():
    fields = {"description": "unicode: éè☃, and a snowman"}
    first = frontmatter.emit(fields)
    second = frontmatter.emit(fields)
    assert first == second
    first.encode("ascii")  # raises UnicodeEncodeError if any byte is non-ASCII


def test_frontmatter_emit_matches_yaml_safe_load_for_tricky_strings():
    yaml = pytest.importorskip("yaml")
    fields = {
        "a": "line one\u0085line two",  # NEL
        "b": "para sep",  # LINE SEPARATOR
        "c": "﻿bom-prefixed",  # BOM
    }
    text = frontmatter.emit(fields)
    text.encode("ascii")
    inner = text[len("---\n") : -len("---\n")]
    loaded = yaml.safe_load(inner)
    assert loaded == fields


def test_frontmatter_parse_returns_body_after_closing_fence():
    text = '---\nname: "x"\n---\nbody line one\nbody line two\n'
    fields, body = frontmatter.parse(text)
    assert fields == {"name": "x"}
    assert body == "body line one\nbody line two\n"


# --- frontmatter: emit rejects ---


def test_frontmatter_emit_rejects_non_dict_top_level():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit("not-a-dict")


def test_frontmatter_emit_rejects_empty_map():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({})


def test_frontmatter_emit_rejects_nested_empty_map():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({"a": {}})


def test_frontmatter_emit_rejects_empty_key():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({"": "value"})


def test_frontmatter_emit_rejects_non_str_non_dict_value():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({"a": 1})
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({"a": True})
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({"a": None})
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({"a": ["x"]})


def test_frontmatter_emit_rejects_control_characters_in_value():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.emit({"a": "bad\x01char"})


# --- frontmatter: parse rejects ---


def test_frontmatter_parse_rejects_missing_opening_fence():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('name: "x"\n---\n')


def test_frontmatter_parse_rejects_missing_closing_fence():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('---\nname: "x"\n')


def test_frontmatter_parse_rejects_flow_map():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('---\na: {b: "c"}\n---\n')


def test_frontmatter_parse_rejects_unquoted_scalar():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("---\na: bare\n---\n")


def test_frontmatter_parse_rejects_tabs():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('---\n\ta: "b"\n---\n')


def test_frontmatter_parse_rejects_anchors():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('---\na: &anchor "b"\n---\n')


def test_frontmatter_parse_rejects_block_scalar():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse("---\na: |\n  b\n---\n")


def test_frontmatter_parse_rejects_crlf():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('---\r\na: "b"\r\n---\r\n')


def test_frontmatter_parse_rejects_duplicate_keys():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('---\na: "b"\na: "c"\n---\n')


def test_frontmatter_parse_rejects_wrong_indent_step():
    with pytest.raises(frontmatter.FrontmatterError):
        frontmatter.parse('---\na:\n   b: "c"\n---\n')


def test_frontmatter_parse_error_names_line_number():
    try:
        frontmatter.parse('---\na: "b"\na: "c"\n---\n')
    except frontmatter.FrontmatterError as exc:
        assert "line 3" in str(exc)
    else:
        raise AssertionError("expected FrontmatterError")


# --- frontmatter: module purity ---


def test_frontmatter_module_imports_only_stdlib():
    import ast
    import inspect

    source = inspect.getsource(frontmatter)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and not node.module.split(".")[0] in ("json", "re", "typing", "__future__"):
                raise AssertionError("unexpected import from %r" % node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ("json", "re", "typing"):
                    raise AssertionError("unexpected import %r" % alias.name)


def test_frontmatter_round_trips_with_json_module_for_scalar_fidelity():
    fields = {"a": json.dumps("already-json-looking-but-a-plain-string")}
    text = frontmatter.emit(fields)
    parsed, _ = frontmatter.parse(text)
    assert parsed == fields


# --- scripts: allowlist resolution ---


def test_script_path_resolves_every_allowlisted_name_to_an_existing_file():
    for name in scripts.ALLOWED_SCRIPTS:
        path = scripts.script_path(SOURCE_DIR, name)
        assert path.is_file(), path


def test_script_path_rejects_unknown_name():
    with pytest.raises(ValueError):
        scripts.script_path(SOURCE_DIR, "evil")


def _has_main_guard(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
            continue
        left, right = test.left, test.comparators[0]
        names = {n for n in (left, right) if isinstance(n, ast.Name)}
        constants = {n.value for n in (left, right) if isinstance(n, ast.Constant)}
        if any(n.id == "__name__" for n in names) and "__main__" in constants:
            return True
    return False


def test_every_allowlisted_script_has_a_top_level_main_guard():
    for name in scripts.ALLOWED_SCRIPTS:
        path = scripts.script_path(SOURCE_DIR, name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert _has_main_guard(tree), "%s has no top-level __main__ guard" % name


# --- scripts: AST write census ---

_WRITE_ATTR_NAMES = {"rename", "unlink", "write_text", "write_bytes", "mkdir", "makedirs", "rmtree", "move"}
_WRITE_MODE_CHARS = set("wax+")


def _mode_indicates_write(mode_node) -> bool:
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return any(ch in mode_node.value for ch in _WRITE_MODE_CHARS)
    return True  # a non-literal mode is treated as a write


def _tree_has_write_call(tree: ast.Module) -> bool:
    found = False

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            nonlocal found
            func = node.func
            is_open_builtin = isinstance(func, ast.Name) and func.id == "open"
            is_open_attr = isinstance(func, ast.Attribute) and func.attr == "open"
            if is_open_builtin or is_open_attr:
                mode_node = None
                for kw in node.keywords:
                    if kw.arg == "mode":
                        mode_node = kw.value
                if mode_node is None:
                    idx = 1 if is_open_builtin else 0
                    if len(node.args) > idx:
                        mode_node = node.args[idx]
                if mode_node is not None and _mode_indicates_write(mode_node):
                    found = True
            elif isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr in ("replace", "remove"):
                    found = True
                elif isinstance(func.value, ast.Name) and func.value.id == "shutil":
                    found = True
                elif func.attr in _WRITE_ATTR_NAMES:
                    found = True
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def test_write_census_flags_exactly_the_write_capable_scripts():
    flagged = []
    for name in scripts.ALLOWED_SCRIPTS:
        path = scripts.script_path(SOURCE_DIR, name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _tree_has_write_call(tree):
            flagged.append(name)
    assert set(flagged) == set(scripts.WRITE_CAPABLE_SCRIPTS)


def test_write_census_ignores_write_calls_mentioned_only_in_a_docstring():
    source = '''
"""This script never calls os.replace(a, b) or shutil.move(a, b), it only talks about it."""


def f():
    return 1
'''
    tree = ast.parse(source)
    assert _tree_has_write_call(tree) is False


def test_write_census_open_with_no_mode_counts_as_a_read():
    tree = ast.parse("open('x')")
    assert _tree_has_write_call(tree) is False


def test_write_census_flags_open_with_write_mode():
    tree = ast.parse("open('x', 'w')")
    assert _tree_has_write_call(tree) is True


# --- scripts: referenced_scripts ---


def test_referenced_scripts_finds_names_in_prose():
    text = "Run `quoin opencode script path_resolve --task t` to resolve the path."
    assert scripts.referenced_scripts(text) == ["path_resolve"]


def test_referenced_scripts_is_sorted_and_unique():
    text = "quoin opencode script validate_artifact x, then quoin opencode script path_resolve y, then quoin opencode script path_resolve z again."
    assert scripts.referenced_scripts(text) == ["path_resolve", "validate_artifact"]


def test_referenced_scripts_ignores_near_misses():
    text = "quoin opencode scripts x and quoin opencode script * are not real references."
    assert scripts.referenced_scripts(text) == []


# --- generate: role permission maps and the delegation graph ---
#
# A small last-match permission evaluator, local to this test module, mirrors
# OpenCode's own wildcard matcher and evaluator closely enough to exercise
# `generate.role_permissions`: `Wildcard.match` turns `*` into `.*`
# (packages/core/src/util/wildcard.ts), a trailing " *" on a shell pattern
# makes the argument tail (including the separating space) optional
# (BashArity.prefix, packages/opencode/src/permission/arity.ts L1-9), and
# `Permission.evaluate` is last-match-wins over an ordered ruleset followed
# by the approved list (packages/opencode/src/permission/index.ts L28-37,
# L67-73, L186-198). It layers built-in defaults (agent.ts L119-136), then
# the role's own map, then an optional approved list, mirroring the "empty
# user config" middle layer the plan describes.

_ALL_ROLES = ("coordinator", "investigator", "architect", "planner", "implementer", "critic", "reviewer", "gate")
_SHELL_ROLES = ("architect", "planner", "gate", "coordinator", "implementer", "investigator")

_BUILTIN_DEFAULTS = {
    "*": "allow",
    "doom_loop": "ask",
    "external_directory": "ask",
    "question": "deny",
    "plan_enter": "deny",
    "plan_exit": "deny",
    "read": {"*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow"},
}


def _wildcard_regex(pattern: str):
    import re

    if pattern.endswith(" *"):
        head = pattern[:-2]
        body = re.escape(head).replace(r"\*", ".*")
        return re.compile(r"^%s( .*)?$" % body)
    body = re.escape(pattern).replace(r"\*", ".*")
    return re.compile(r"^%s$" % body)


def _evaluate_ruleset(rule, text):
    if isinstance(rule, str):
        return rule
    action = None
    for pattern, value in rule.items():
        if _wildcard_regex(pattern).match(text):
            action = value
    return action


def evaluate(role, permission_type, text, approved=None):
    perms = generate.role_permissions(role)
    if permission_type in perms:
        action = _evaluate_ruleset(perms[permission_type], text)
        if action is None:
            action = _evaluate_ruleset(_BUILTIN_DEFAULTS.get(permission_type, _BUILTIN_DEFAULTS["*"]), text)
    elif "*" in perms:
        action = _evaluate_ruleset(perms["*"], text)
    else:
        action = _evaluate_ruleset(_BUILTIN_DEFAULTS.get(permission_type, _BUILTIN_DEFAULTS["*"]), text)
    for a_type, a_pattern, a_action in approved or ():
        if a_type == permission_type and _wildcard_regex(a_pattern).match(text):
            action = a_action
    return action


def _is_hidden(role, permission_type):
    """Mirrors `Permission.disabled` (permission/index.ts L204-214): a tool
    is hidden when the LAST rule for its permission type — in the role's own
    ruleset, falling back to the role's own catch-all `"*"` entry when the
    type is absent — has the literal pattern `"*"` and action `deny`. A bare
    action string behaves as a single `"*"`-pattern rule."""
    perms = generate.role_permissions(role)
    rule = perms.get(permission_type)
    if rule is None:
        rule = perms.get("*")
    if rule is None:
        return False
    if isinstance(rule, str):
        return rule == "deny"
    last_pattern = next(reversed(rule))
    return last_pattern == "*" and rule[last_pattern] == "deny"


def test_read_only_roles_deny_write_shell_task_and_network_by_default():
    for role in ("critic", "reviewer"):
        for permission_type in ("edit", "bash", "task", "webfetch", "websearch", "todowrite", "an_invented_mcp_tool"):
            assert evaluate(role, permission_type, "anything") == "deny", (role, permission_type)
        assert evaluate(role, "read", "src/a.py") == "allow"
        assert evaluate(role, "read", ".env") == "ask"
        assert evaluate(role, "skill", "quoin-plan") == "allow"
        assert evaluate(role, "skill", "plan") == "deny"
        perms = generate.role_permissions(role)
        assert list(perms.keys())[0] == "*"
        assert perms["*"] == "deny"


def test_artifact_root_roles_edit_and_bash_evaluation():
    for role in ("architect", "planner", "gate", "investigator"):
        assert evaluate(role, "edit", "src/a.py") == "deny"
        assert evaluate(role, "edit", "%s/t/plan.md" % generate.ARTIFACT_ROOT) == "allow"
        assert evaluate(role, "edit", "sub/%s/t/plan.md" % generate.ARTIFACT_ROOT) == "allow"
        assert evaluate(role, "bash", "quoin opencode script path_resolve --task t") == "allow"
        assert evaluate(role, "bash", "rm -rf build") == "ask"


def test_coordinator_edit_asks_by_default_and_allows_artifact_paths():
    assert evaluate("coordinator", "edit", "src/a.py") == "ask"
    assert evaluate("coordinator", "edit", "%s/t/plan.md" % generate.ARTIFACT_ROOT) == "allow"
    assert evaluate("coordinator", "edit", "sub/%s/t/plan.md" % generate.ARTIFACT_ROOT) == "allow"


def test_implementer_emits_no_edit_key_at_all():
    assert "edit" not in generate.role_permissions("implementer")


def test_role_scripts_names_are_all_allowlisted():
    for role_scripts in generate.ROLE_SCRIPTS.values():
        for name in role_scripts:
            assert name in scripts.ALLOWED_SCRIPTS, name


def test_write_capable_scripts_scoped_to_the_investigator_only():
    for role, role_scripts in generate.ROLE_SCRIPTS.items():
        for name in role_scripts:
            if name in scripts.WRITE_CAPABLE_SCRIPTS:
                assert role == "investigator", (role, name)


def test_script_scoping_matches_the_role_scripts_table():
    for role in _SHELL_ROLES:
        for name in scripts.ALLOWED_SCRIPTS:
            text = "quoin opencode script %s --x" % name
            expected = "allow" if name in generate.ROLE_SCRIPTS.get(role, ()) else "ask"
            assert evaluate(role, "bash", text) == expected, (role, name)


def test_no_role_bash_map_contains_an_argument_wide_script_allow():
    for role in _SHELL_ROLES:
        rule = generate.role_permissions(role)["bash"]
        assert "quoin opencode script *" not in rule


_REDIRECTION_FORMS = (
    "> src/app.py",
    ">> src/app.py",
    "2>&1",
    "2>/dev/null",
    "&> out.txt",
    ">| out.txt",
    "< .env",
    "<<EOF",
)


def test_redirection_after_an_allowed_script_still_asks():
    base = "quoin opencode script path_resolve --task t"
    for role in _SHELL_ROLES:
        assert evaluate(role, "bash", base) == "allow", role
        for suffix in _REDIRECTION_FORMS:
            text = "%s %s" % (base, suffix)
            assert evaluate(role, "bash", text) == "ask", (role, suffix)


def test_shell_map_last_two_keys_are_the_redirect_rules():
    for role in _SHELL_ROLES:
        rule = generate.role_permissions(role)["bash"]
        keys = list(rule.keys())
        assert keys[-2:] == ["*>*", "*<*"], (role, keys)
        assert rule["*>*"] == "ask"
        assert rule["*<*"] == "ask"


def test_no_role_emits_a_broad_allow_for_write_shell_task_or_network_permissions():
    watched_types = (
        "*",
        "edit",
        "bash",
        "task",
        "webfetch",
        "websearch",
        "external_directory",
        "todowrite",
        "question",
        "doom_loop",
    )
    for role in _ALL_ROLES:
        perms = generate.role_permissions(role)
        for permission_type in watched_types:
            rule = perms.get(permission_type)
            if rule is None:
                continue
            if isinstance(rule, str):
                assert rule != "allow", "%s/%s emits a bare allow" % (role, permission_type)
            else:
                assert rule.get("*") != "allow", "%s/%s emits an allow at '*'" % (role, permission_type)


def _posture_tuples(role):
    """(role, permission_type, pattern, action) triples for every entry of
    `role_permissions(role)` whose action is `allow` or `ask`. A bare-string
    rule is treated as a single `"*"`-pattern entry."""
    tuples = []
    for permission_type, rule in generate.role_permissions(role).items():
        if isinstance(rule, str):
            if rule in ("allow", "ask"):
                tuples.append((role, permission_type, "*", rule))
            continue
        for pattern, action in rule.items():
            if action in ("allow", "ask"):
                tuples.append((role, permission_type, pattern, action))
    return tuples


def test_posture_pin_full_allow_and_ask_set():
    expected = set()
    for role in ("critic", "reviewer"):
        expected |= {
            (role, "read", "*", "allow"),
            (role, "read", "*.env", "ask"),
            (role, "read", "*.env.*", "ask"),
            (role, "read", "*.env.example", "allow"),
            (role, "glob", "*", "allow"),
            (role, "grep", "*", "allow"),
            (role, "lsp", "*", "allow"),
            (role, "skill", "quoin-*", "allow"),
        }

    expected |= {
        ("investigator", "edit", "%s/*" % generate.ARTIFACT_ROOT, "allow"),
        ("investigator", "edit", "*/%s/*" % generate.ARTIFACT_ROOT, "allow"),
        ("investigator", "bash", "*", "ask"),
        ("investigator", "bash", "quoin opencode script generate_discovery_map *", "allow"),
        ("investigator", "bash", "quoin opencode script path_resolve *", "allow"),
        ("investigator", "bash", "*>*", "ask"),
        ("investigator", "bash", "*<*", "ask"),
        ("investigator", "skill", "quoin-*", "allow"),
    }

    for role in ("architect", "planner"):
        expected |= {
            (role, "edit", "%s/*" % generate.ARTIFACT_ROOT, "allow"),
            (role, "edit", "*/%s/*" % generate.ARTIFACT_ROOT, "allow"),
            (role, "bash", "*", "ask"),
            (role, "bash", "quoin opencode script path_resolve *", "allow"),
            (role, "bash", "quoin opencode script validate_artifact *", "allow"),
            (role, "bash", "*>*", "ask"),
            (role, "bash", "*<*", "ask"),
            (role, "task", "quoin-investigator", "allow"),
            (role, "task", "quoin-critic", "allow"),
            (role, "skill", "quoin-*", "allow"),
        }

    expected |= {
        ("gate", "edit", "%s/*" % generate.ARTIFACT_ROOT, "allow"),
        ("gate", "edit", "*/%s/*" % generate.ARTIFACT_ROOT, "allow"),
        ("gate", "bash", "*", "ask"),
        ("gate", "bash", "quoin opencode script path_resolve *", "allow"),
        ("gate", "bash", "quoin opencode script validate_artifact *", "allow"),
        ("gate", "bash", "*>*", "ask"),
        ("gate", "bash", "*<*", "ask"),
        ("gate", "skill", "quoin-*", "allow"),
    }

    expected |= {
        ("coordinator", "edit", "*", "ask"),
        ("coordinator", "edit", "%s/*" % generate.ARTIFACT_ROOT, "allow"),
        ("coordinator", "edit", "*/%s/*" % generate.ARTIFACT_ROOT, "allow"),
        ("coordinator", "bash", "*", "ask"),
        ("coordinator", "bash", "quoin opencode script checkpoint_picker *", "allow"),
        ("coordinator", "bash", "quoin opencode script classify_critic_issues *", "allow"),
        ("coordinator", "bash", "quoin opencode script handoff_validate *", "allow"),
        ("coordinator", "bash", "quoin opencode script path_resolve *", "allow"),
        ("coordinator", "bash", "quoin opencode script validate_artifact *", "allow"),
        ("coordinator", "bash", "*>*", "ask"),
        ("coordinator", "bash", "*<*", "ask"),
        ("coordinator", "task", "quoin-investigator", "allow"),
        ("coordinator", "task", "quoin-critic", "allow"),
        ("coordinator", "task", "quoin-reviewer", "allow"),
        ("coordinator", "skill", "quoin-*", "allow"),
    }

    expected |= {
        ("implementer", "bash", "*", "ask"),
        ("implementer", "bash", "quoin opencode script path_resolve *", "allow"),
        ("implementer", "bash", "quoin opencode script validate_artifact *", "allow"),
        ("implementer", "bash", "*>*", "ask"),
        ("implementer", "bash", "*<*", "ask"),
        ("implementer", "skill", "quoin-*", "allow"),
    }

    actual = set()
    for role in _ALL_ROLES:
        actual |= set(_posture_tuples(role))

    assert actual == expected


def test_evaluator_mirrors_the_full_builtin_default_set():
    assert evaluate("architect", "*", "anything") == "allow"
    assert evaluate("architect", "doom_loop", "anything") == "ask"
    assert evaluate("architect", "external_directory", "/tmp/x") == "ask"
    assert evaluate("architect", "plan_enter", "anything") == "deny"
    assert evaluate("architect", "plan_exit", "anything") == "deny"
    assert evaluate("architect", "read", ".env") == "ask"
    assert evaluate("architect", "read", ".env.example") == "allow"
    for role in _ALL_ROLES:
        assert evaluate(role, "question", "anything") == "deny", role


def test_guard_rail_pin_an_always_approval_outranks_the_role_map():
    approved = [("edit", "*", "allow")]
    assert evaluate("architect", "edit", "src/a.py", approved=approved) == "allow"

    approved = [("bash", "python3 *", "allow")]
    assert evaluate("planner", "bash", "python3 -c x > src/a.py", approved=approved) == "allow"


def test_permission_disabled_mirror_hides_only_the_documented_tools():
    for role in ("critic", "reviewer"):
        assert _is_hidden(role, "edit")
        assert _is_hidden(role, "bash")
        assert _is_hidden(role, "task")
    for role in ("investigator", "gate", "implementer"):
        assert _is_hidden(role, "task")
    for role in ("architect", "planner", "coordinator"):
        assert not _is_hidden(role, "task")
    for role in ("architect", "planner", "gate", "investigator", "coordinator"):
        assert not _is_hidden(role, "edit")
    for role in _SHELL_ROLES:
        assert not _is_hidden(role, "bash")
    assert not _is_hidden("implementer", "edit")


def test_check_task_graph_is_empty_for_the_real_roles():
    roles_by_name = {
        "coordinator": {"mode": "primary"},
        "investigator": {"mode": "all"},
        "architect": {"mode": "primary"},
        "planner": {"mode": "primary"},
        "implementer": {"mode": "primary"},
        "critic": {"mode": "subagent"},
        "reviewer": {"mode": "subagent"},
        "gate": {"mode": "primary"},
    }
    assert generate.check_task_graph(roles_by_name) == []


def test_check_task_graph_flags_an_edge_from_a_non_primary_source(monkeypatch):
    real = generate.role_permissions

    def fake(role):
        perms = real(role)
        if role == "investigator":
            perms = dict(perms)
            perms["task"] = {"*": "deny", "quoin-critic": "allow"}
        return perms

    monkeypatch.setattr(generate, "role_permissions", fake)
    roles_by_name = {
        "investigator": {"mode": "all"},
        "critic": {"mode": "subagent"},
    }
    errors = generate.check_task_graph(roles_by_name)
    assert any("investigator" in e and "critic" in e for e in errors)


def test_check_task_graph_flags_an_unknown_target(monkeypatch):
    real = generate.role_permissions

    def fake(role):
        perms = real(role)
        if role == "architect":
            perms = dict(perms)
            perms["task"] = dict(perms["task"])
            perms["task"]["quoin-ghost"] = "allow"
        return perms

    monkeypatch.setattr(generate, "role_permissions", fake)
    roles_by_name = {
        "architect": {"mode": "primary"},
        "critic": {"mode": "subagent"},
        "investigator": {"mode": "all"},
    }
    errors = generate.check_task_graph(roles_by_name)
    assert any("quoin-ghost" in e for e in errors)


def test_check_task_graph_flags_a_target_whose_mode_is_primary(monkeypatch):
    real = generate.role_permissions

    def fake(role):
        perms = real(role)
        if role == "architect":
            perms = dict(perms)
            perms["task"] = dict(perms["task"])
            perms["task"]["quoin-planner"] = "allow"
        return perms

    monkeypatch.setattr(generate, "role_permissions", fake)
    roles_by_name = {
        "architect": {"mode": "primary"},
        "planner": {"mode": "primary"},
        "critic": {"mode": "subagent"},
        "investigator": {"mode": "all"},
    }
    errors = generate.check_task_graph(roles_by_name)
    assert any("planner" in e and "primary" in e for e in errors)
