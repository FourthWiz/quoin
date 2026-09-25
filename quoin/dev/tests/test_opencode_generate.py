"""The pure OpenCode generator's tests.

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
import re
from pathlib import Path

import pytest

from quoin.opencode_adapter import frontmatter, generate, names, scripts

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"


# --- frontmatter: emit/parse round trip ---


def test_frontmatter_round_trip_preserves_key_order_and_special_keys():
    fields = {
        "name": "quoin-plan",
        "*": "deny",
        "quoin-*": "allow",
        "%s/*" % generate.ARTIFACT_ROOT: "allow",
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


# --- generator core: inputs, sections, rendering, digests, collisions ---


def _load_real_manifest():
    return json.loads((SOURCE_DIR / "adapters" / "opencode" / "feature-manifest.json").read_text())


def _supported_rows(manifest_data):
    return sorted(
        (row for row in manifest_data["catalog_entries"] if row["status"] == "supported"),
        key=lambda r: r["id"],
    )


# extract_sections


def test_extract_sections_splits_on_h2_outside_fences_and_keeps_h3_inside_parent():
    md = (
        "# Title\nlead-in prose ignored\n\n"
        "## First\nbody one\n### sub\nsub body\n\n"
        "## Second\n```\n## not a heading\n```\nbody two\n"
    )
    sections = generate.extract_sections(md)
    headings = [h for h, _ in sections]
    assert headings == ["First", "Second"]
    assert "### sub" in dict(sections)["First"]
    assert "## not a heading" in dict(sections)["Second"]


def test_extract_sections_on_real_contract_matches_grep_headings():
    text = (SOURCE_DIR / "core" / "skills" / "plan.md").read_text()
    sections = generate.extract_sections(text)
    headings = [h for h, _ in sections]
    assert headings == [
        "Purpose",
        "When to use",
        "Inputs",
        "Output",
        "Behavior contract",
        "Out of scope",
        "v3-format detection rule",
        "Notes",
    ]


# load_inputs on the real tree


def test_load_inputs_on_the_real_tree_succeeds():
    inputs = generate.load_inputs(SOURCE_DIR)
    assert inputs.pinned_version
    assert set(inputs.templates) == {"command", "skill", "agent", "instructions"}
    manifest_data = _load_real_manifest()
    supported_ids = {row["id"] for row in _supported_rows(manifest_data)}
    assert set(inputs.contracts) == supported_ids


def test_load_inputs_raises_generation_error_on_drifting_manifest(tmp_path):
    import shutil

    copy_dir = tmp_path / "quoin"
    shutil.copytree(SOURCE_DIR, copy_dir)
    manifest_path = copy_dir / "adapters" / "opencode" / "feature-manifest.json"
    data = json.loads(manifest_path.read_text())
    data["schema_version"] = 999
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(generate.GenerationError, match="manifest drift"):
        generate.load_inputs(copy_dir)


# render() on the real tree: file set, path families, bindings


def test_render_source_dir_renders_exactly_32_files_with_no_run_command():
    files = generate.render_source_dir(SOURCE_DIR)
    manifest_data = _load_real_manifest()
    supported = _supported_rows(manifest_data)
    expected_names = sorted(generate.names.normalize(row["id"]) for row in supported)

    command_paths = sorted(k for k in files if k.startswith(".opencode/commands/"))
    skill_paths = sorted(k for k in files if k.startswith(".opencode/skills/"))
    agent_paths = sorted(k for k in files if k.startswith(".opencode/agents/"))

    assert command_paths == [".opencode/commands/%s.md" % n for n in expected_names]
    assert skill_paths == [".opencode/skills/%s/SKILL.md" % n for n in expected_names]
    assert len(agent_paths) == len(manifest_data["roles"])
    assert len(files) == 32
    assert not any("quoin-run" in k for k in files)


def test_render_command_agent_binding_matches_manifest_rows():
    files = generate.render_source_dir(SOURCE_DIR)
    manifest_data = _load_real_manifest()
    for row in _supported_rows(manifest_data):
        name = generate.names.normalize(row["id"])
        expected_agent = "quoin-%s" % row["opencode"]["agent_role"]
        content = files[".opencode/commands/%s.md" % name].content.decode("utf-8")
        fm, _ = frontmatter.parse(content)
        assert fm["agent"] == expected_agent, row["id"]

    discover_row = next(r for r in manifest_data["catalog_entries"] if r["id"] == "discover")
    assert discover_row["opencode"]["agent_role"] == "investigator"
    investigator_agent = files[".opencode/agents/quoin-investigator.md"].content.decode("utf-8")
    fm, _ = frontmatter.parse(investigator_agent)
    assert fm["mode"] == "all"

    for skill_id in ("review", "critic"):
        row = next(r for r in manifest_data["catalog_entries"] if r["id"] == skill_id)
        assert row["opencode"]["agent_role"] == "coordinator"


def test_skill_names_equal_command_names_and_pass_name_error():
    files = generate.render_source_dir(SOURCE_DIR)
    command_names = {k.split("/")[-1][:-3] for k in files if k.startswith(".opencode/commands/")}
    skill_dirs = {k.split("/")[2] for k in files if k.startswith(".opencode/skills/")}
    assert command_names == skill_dirs
    for name in skill_dirs:
        assert generate.names.name_error(name) is None
        content = files[".opencode/skills/%s/SKILL.md" % name].content.decode("utf-8")
        fm, _ = frontmatter.parse(content)
        assert 1 <= len(fm["description"]) <= 1024


def test_rendered_path_families_equal_manifest_generated_outputs():
    files = generate.render_source_dir(SOURCE_DIR)
    manifest_data = _load_real_manifest()
    families = {row["path"] for row in manifest_data["generated_outputs"]}
    assert families == {
        ".opencode/commands/quoin-*.md",
        ".opencode/skills/quoin-*/SKILL.md",
        ".opencode/agents/quoin-*.md",
        ".opencode/quoin/instructions.md",
        ".opencode/opencode.jsonc",
    }
    assert any(k.startswith(".opencode/commands/") for k in files)
    assert any(k.startswith(".opencode/skills/") and k.endswith("/SKILL.md") for k in files)
    assert any(k.startswith(".opencode/agents/") for k in files)
    assert ".opencode/quoin/instructions.md" in files
    assert ".opencode/opencode.jsonc" in files


def test_config_file_parses_to_exactly_two_keys_and_names_a_rendered_file():
    files = generate.render_source_dir(SOURCE_DIR)
    cfg = files[".opencode/opencode.jsonc"].content.decode("utf-8")
    stripped = "\n".join(line for line in cfg.split("\n") if not line.strip().startswith("//"))
    obj = json.loads(stripped)
    assert set(obj) == {"$schema", "instructions"}
    assert obj["instructions"] == [generate.INSTRUCTIONS_PATH]
    assert generate.INSTRUCTIONS_PATH in files


# instruction document content


def test_instruction_document_contains_required_content():
    files = generate.render_source_dir(SOURCE_DIR)
    manifest_data = _load_real_manifest()
    instr = files[generate.INSTRUCTIONS_PATH].content.decode("utf-8")

    for row in _supported_rows(manifest_data):
        assert "/%s" % generate.names.normalize(row["id"]) in instr
    assert "/quoin-implement" in instr and "explicit user command" in instr

    for row in manifest_data["catalog_entries"]:
        if row["status"] != "supported":
            assert row["id"] in instr, row["id"]

    for role in manifest_data["roles"]:
        assert role in instr, role

    assert "enforced-natively" in instr
    assert "declared-not-enforced" in instr
    assert "separate context" in instr

    perm_match = re.search(r"## Permissions\n(.*?)(\n## |\Z)", instr, re.S)
    assert perm_match, "no Permissions section found"
    perm_body = perm_match.group(1)
    for phrase in ('any prompt, in any agent', 'not a security boundary', 'until OpenCode restarts', 'answer "once"'):
        assert phrase in perm_body, phrase
    assert "for the rest of the session" not in instr
    assert perm_body.count("boundary") == perm_body.count("not a security boundary")
    assert "model diversity" not in instr

    legacy_sections = dict(generate.extract_sections(instr))
    assert "Legacy discovery" in legacy_sections
    non_legacy = "\n".join(b for h, b in legacy_sections.items() if h != "Legacy discovery")
    assert ".claude" not in non_legacy
    assert ".claude" in legacy_sections["Legacy discovery"]


# slash translation: unit cases


def test_slash_translation_unit_cases():
    catalog_ids = {"architect", "plan", "revise", "revise-fast", "critic", "end_of_day", "gate", "review"}
    supported = {"architect", "plan", "critic", "gate", "review"}

    def translate(text):
        return generate._translate_slashes(text, catalog_ids, supported)

    unchanged = [
        "See /architecture.md for details.",
        "Read `<task_dir>/critic-response-*.md`.",
        "See `<task-name>/gate-{phase}-{date}`.",
        "Open /plan.md now.",
        "Check /review/x path.",
    ]
    for text in unchanged:
        got, dropped = translate(text)
        assert got == text, text
        assert dropped == []

    got, dropped = translate("Use /revise-fast here, not /revise.")
    assert got == "Use revise-fast here, not revise."
    assert dropped == ["revise", "revise-fast"]

    got, dropped = translate("End with /plan.")
    assert got == "End with /quoin-plan."
    assert dropped == []

    got, dropped = translate("Backtick `/plan` form.")
    assert got == "Backtick `/quoin-plan` form."
    assert dropped == []

    got, dropped = translate("A non-bundle /end_of_day reference.")
    assert got == "A non-bundle end_of_day reference."
    assert dropped == ["end_of_day"]

    got, dropped = translate("No non-bundle ids here at all.")
    assert dropped == []


def test_slash_translation_on_the_real_tree():
    files = generate.render_source_dir(SOURCE_DIR)
    arch = files[".opencode/skills/quoin-architect/SKILL.md"].content.decode("utf-8")
    critic = files[".opencode/skills/quoin-critic/SKILL.md"].content.decode("utf-8")
    gate = files[".opencode/skills/quoin-gate/SKILL.md"].content.decode("utf-8")
    assert "architecture.md" in arch
    assert "critic-response-" in critic
    assert "gate-" in gate

    command_names = {k.split("/")[-1][:-3] for k in files if k.startswith(".opencode/commands/")}
    quoin_token_re = re.compile(r"/(quoin-[a-z0-9-]+)")
    for k, rf in files.items():
        if not k.endswith("SKILL.md"):
            continue
        text = rf.content.decode("utf-8")
        assert "quoin-architecture" not in text
        assert "quoin-critic-response" not in text
        assert "quoin-gate-" not in text
        for m in quoin_token_re.finditer(text):
            assert m.group(1) in command_names, (k, m.group(0))


def test_script_coverage_every_referenced_script_is_in_the_bound_roles_allowlist():
    files = generate.render_source_dir(SOURCE_DIR)
    manifest_data = _load_real_manifest()
    role_by_id = {row["id"]: row["opencode"]["agent_role"] for row in _supported_rows(manifest_data)}
    for k, rf in files.items():
        if rf.kind != "skill":
            continue
        role = role_by_id[rf.source_id]
        text = rf.content.decode("utf-8")
        for ref in scripts.referenced_scripts(text):
            assert ref in generate.ROLE_SCRIPTS.get(role, ()), (k, role, ref)


# determinism and digest shape (fuller coverage lands separately below; this is a smoke check)


def test_double_render_is_byte_identical():
    files_a = generate.render_source_dir(SOURCE_DIR)
    files_b = generate.render_source_dir(SOURCE_DIR)
    assert files_a.keys() == files_b.keys()
    for k in files_a:
        assert files_a[k].content == files_b[k].content, k
        assert files_a[k].source_digest == files_b[k].source_digest, k


def test_every_rendered_file_is_utf8_lf_only_with_one_trailing_newline():
    files = generate.render_source_dir(SOURCE_DIR)
    for k, rf in files.items():
        text = rf.content.decode("utf-8")
        assert "\r" not in text, k
        assert text.endswith("\n") and not text.endswith("\n\n"), k
        assert re.match(r"^sha256:[0-9a-f]{64}$", rf.source_digest), k


def test_skill_frontmatter_metadata_matches_its_record():
    files = generate.render_source_dir(SOURCE_DIR)
    for k, rf in files.items():
        if rf.kind != "skill":
            continue
        text = rf.content.decode("utf-8")
        fm, _ = frontmatter.parse(text)
        assert fm["metadata"]["source_digest"] == rf.source_digest, k
        assert fm["metadata"]["canonical_id"] == rf.source_id, k
        assert fm["metadata"]["generator"] == "quoin"


# injected collision


def test_injected_collision_raises_name_collision_error():
    inputs = generate.load_inputs(SOURCE_DIR)
    manifest_data = json.loads(json.dumps(inputs.manifest))  # deep copy
    extra_row = {
        "id": "end-of-task",
        "status": "supported",
        "reason": "synthetic collision fixture for the injected-collision test",
        "target_milestone": manifest_data["catalog_entries"][0]["target_milestone"],
        "catalog": {"user_facing": True, "spawn_target": False},
        "assets": ["command", "skill"],
        "opencode": {
            "command": "quoin-end-of-task",
            "skill": "quoin-end-of-task",
            "agent_role": "coordinator",
        },
        "live_runtime_evidence": False,
        "evidence": [],
    }
    manifest_data["catalog_entries"] = sorted(
        manifest_data["catalog_entries"] + [extra_row], key=lambda r: r["id"]
    )

    overlays = json.loads(json.dumps(inputs.overlays))
    overlays["entries"]["end-of-task"] = json.loads(json.dumps(overlays["entries"]["end_of_task"]))

    contracts = dict(inputs.contracts)
    contracts["end-of-task"] = inputs.contracts["end_of_task"]

    mutated = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=manifest_data,
        pinned_version=inputs.pinned_version,
        overlays=overlays,
        templates=inputs.templates,
        contracts=contracts,
        rules=inputs.rules,
    )
    with pytest.raises(names.NameCollisionError) as exc_info:
        generate.render(mutated)
    message = str(exc_info.value)
    assert "end_of_task" in message
    assert "end-of-task" in message


# overlay drift


def _mutated_inputs(**overrides):
    inputs = generate.load_inputs(SOURCE_DIR)
    kwargs = dict(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=json.loads(json.dumps(inputs.overlays)),
        templates=dict(inputs.templates),
        contracts=dict(inputs.contracts),
        rules=inputs.rules,
    )
    kwargs.update(overrides)
    return generate.GeneratorInputs(**kwargs)


def test_overlay_drift_missing_entry_raises():
    inputs = _mutated_inputs()
    del inputs.overlays["entries"]["plan"]
    with pytest.raises(generate.GenerationError, match="missing entries"):
        generate._validate_overlays(inputs, sorted(inputs.overlays["entries"]) + ["plan"])


def test_overlay_drift_extra_entry_raises():
    inputs = _mutated_inputs()
    inputs.overlays["entries"]["bogus"] = dict(inputs.overlays["entries"]["plan"])
    supported_ids = [k for k in inputs.overlays["entries"] if k != "bogus"]
    with pytest.raises(generate.GenerationError, match="unknown entries"):
        generate._validate_overlays(inputs, supported_ids)


def test_overlay_drift_unknown_placeholder_token_raises():
    inputs = _mutated_inputs()
    inputs.overlays["entries"]["plan"]["description"] += " {{BOGUS_TOKEN}}"
    supported_ids = list(inputs.overlays["entries"])
    with pytest.raises(generate.GenerationError, match="unknown placeholder token"):
        generate._validate_overlays(inputs, supported_ids)


def test_overlay_drift_rewrite_from_absent_raises():
    manifest_data = _load_real_manifest()
    supported_ids = [row["id"] for row in _supported_rows(manifest_data)]
    contract_text = "## Purpose\nbody\n\n## When to use\nx\n\n## Inputs\nx\n\n## Output\nx\n\n## Behavior contract\nx\n"
    overlay_entry = {
        "description": "test entry",
        "command_note": "",
        "extra_sections": [],
        "notes": [],
        "rewrites": [{"section": "Purpose", "from": "not present anywhere", "to": "replacement"}],
    }
    with pytest.raises(generate.GenerationError, match="occur exactly once"):
        generate._assemble_contract("plan", contract_text, overlay_entry, {"plan"}, {"plan"})


def test_overlay_drift_rewrite_from_occurs_twice_raises():
    contract_text = "## Purpose\ndup dup\n\n## When to use\nx\n\n## Inputs\nx\n\n## Output\nx\n\n## Behavior contract\nx\n"
    overlay_entry = {
        "description": "test entry",
        "command_note": "",
        "extra_sections": [],
        "notes": [],
        "rewrites": [{"section": "Purpose", "from": "dup", "to": "one"}],
    }
    with pytest.raises(generate.GenerationError, match="occur exactly once"):
        generate._assemble_contract("plan", contract_text, overlay_entry, {"plan"}, {"plan"})


def test_overlay_drift_unknown_extra_section_raises():
    contract_text = "## Purpose\nbody\n\n## When to use\nx\n\n## Inputs\nx\n\n## Output\nx\n\n## Behavior contract\nx\n"
    overlay_entry = {
        "description": "test entry",
        "command_note": "",
        "extra_sections": ["Nonexistent Section"],
        "notes": [],
        "rewrites": [],
    }
    with pytest.raises(generate.GenerationError, match="missing required section"):
        generate._assemble_contract("plan", contract_text, overlay_entry, {"plan"}, {"plan"})


def test_overlay_drift_contract_missing_behavior_contract_raises():
    contract_text = "## Purpose\nbody\n\n## When to use\nx\n\n## Inputs\nx\n\n## Output\nx\n"
    overlay_entry = {
        "description": "test entry",
        "command_note": "",
        "extra_sections": [],
        "notes": [],
        "rewrites": [],
    }
    with pytest.raises(generate.GenerationError, match="Behavior contract"):
        generate._assemble_contract("plan", contract_text, overlay_entry, {"plan"}, {"plan"})


def test_overlay_drift_template_missing_required_placeholder_raises():
    templates = {
        "command": "# {{TITLE}}\n{{COMMAND_NOTE}}\n",  # missing {{SKILL_NAME}}
        "skill": generate.load_inputs(SOURCE_DIR).templates["skill"],
        "agent": generate.load_inputs(SOURCE_DIR).templates["agent"],
        "instructions": generate.load_inputs(SOURCE_DIR).templates["instructions"],
    }
    with pytest.raises(generate.GenerationError, match="missing required placeholder"):
        generate._validate_templates(templates)


def test_overlay_drift_template_unknown_placeholder_raises():
    base = generate.load_inputs(SOURCE_DIR).templates
    templates = dict(base)
    templates["command"] = base["command"] + "\n{{MYSTERY_TOKEN}}\n"
    with pytest.raises(generate.GenerationError, match="unknown placeholder"):
        generate._validate_templates(templates)


def test_load_inputs_on_temp_copy_with_drifting_manifest_raises_with_drift_message(tmp_path):
    import shutil

    copy_dir = tmp_path / "quoin"
    shutil.copytree(SOURCE_DIR, copy_dir)
    manifest_path = copy_dir / "adapters" / "opencode" / "feature-manifest.json"
    data = json.loads(manifest_path.read_text())
    data["roles"]["architect"]["mode"] = "bogus-mode"
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(generate.GenerationError) as exc_info:
        generate.load_inputs(copy_dir)
    assert "mode" in str(exc_info.value)


# --- check_rendered on the real tree ---


def test_check_rendered_of_real_render_is_empty():
    files = generate.render_source_dir(SOURCE_DIR)
    assert generate.check_rendered(files) == []


def test_claude_token_inside_instructions_legacy_section_is_accepted():
    files = generate.render_source_dir(SOURCE_DIR)
    instr = files[generate.INSTRUCTIONS_PATH].content.decode("utf-8")
    assert ".claude" in instr
    findings = generate.check_rendered(files)
    assert not any(generate.INSTRUCTIONS_PATH in f for f in findings)


# --- injected hazards, each through overlays in a temp copy ---


def _copy_source_tree(tmp_path):
    import shutil

    copy_dir = tmp_path / "quoin"
    shutil.copytree(SOURCE_DIR, copy_dir)
    return copy_dir


def _read_overlays(copy_dir):
    path = copy_dir / "adapters" / "opencode" / "overlays.json"
    return path, json.loads(path.read_text())


def _write_overlays(path, data):
    path.write_text(json.dumps(data))


def test_injected_ask_user_question_in_a_note_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["entries"]["plan"]["notes"].append("Never call AskUserQuestion from here.")
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match="AskUserQuestion"):
        generate.render_source_dir(copy_dir)


def test_injected_model_line_in_a_role_prompt_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["roles"]["architect"]["prompt"].append("model: sonnet")
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match="Claude tier"):
        generate.render_source_dir(copy_dir)


def test_injected_shell_expansion_in_a_command_note_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["entries"]["plan"]["command_note"] += " Run !`ls` first."
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match="shell-expansion"):
        generate.render_source_dir(copy_dir)


def test_injected_dollar_digit_in_a_command_note_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["entries"]["plan"]["command_note"] += " see $2 for detail."
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match=r"\$<digit>"):
        generate.render_source_dir(copy_dir)


def test_injected_file_reference_in_a_command_note_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["entries"]["plan"]["command_note"] += " see @src/x for detail."
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match="@ file reference"):
        generate.render_source_dir(copy_dir)


def test_injected_section_sign_plus_digit_in_a_note_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["entries"]["plan"]["notes"].append("see " + "§" + "1 for detail.")
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match="section-sign"):
        generate.render_source_dir(copy_dir)


def test_injected_claude_dir_in_a_skill_note_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["entries"]["plan"]["notes"].append("legacy config also lives under .claude/skills.")
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match="Claude home path"):
        generate.render_source_dir(copy_dir)


def test_injected_unlisted_script_reference_raises(tmp_path):
    copy_dir = _copy_source_tree(tmp_path)
    path, data = _read_overlays(copy_dir)
    data["entries"]["plan"]["notes"].append("Run quoin opencode script evil first.")
    _write_overlays(path, data)
    with pytest.raises(generate.GenerationError, match="evil"):
        generate.render_source_dir(copy_dir)


def test_injected_bad_permission_action_raises(monkeypatch):
    original = generate.role_permissions

    def fake(role):
        result = original(role)
        if role == "architect":
            result["edit"] = "allowed"
        return result

    monkeypatch.setattr(generate, "role_permissions", fake)
    with pytest.raises(generate.GenerationError, match="architect"):
        generate.render_source_dir(SOURCE_DIR)


def test_injected_map_value_on_an_action_only_permission_key_raises(monkeypatch):
    original = generate.role_permissions

    def fake(role):
        result = original(role)
        if role == "gate":
            result["webfetch"] = {"*": "allow"}
        return result

    monkeypatch.setattr(generate, "role_permissions", fake)
    with pytest.raises(generate.GenerationError, match="webfetch"):
        generate.render_source_dir(SOURCE_DIR)


def test_injected_invalid_agent_mode_raises():
    # "implementer" is never a Task target and never a Task source, so this
    # reaches check_rendered's own mode check instead of tripping
    # the delegation-graph check first.
    inputs = _mutated_inputs()
    inputs.manifest["roles"]["implementer"]["mode"] = "sub"
    with pytest.raises(generate.GenerationError, match="mode 'sub'"):
        generate.render(inputs)


# --- residue test over rendered skills (test-only, stricter than check_rendered) ---


def test_residue_over_rendered_skills():
    files = generate.render_source_dir(SOURCE_DIR)
    inputs = generate.load_inputs(SOURCE_DIR)
    catalog_ids = {c["name"] for c in inputs.catalog if isinstance(c, dict) and isinstance(c.get("name"), str)}
    slash_re = generate._slash_translate_pattern(catalog_ids)
    command_names = {k.split("/")[-1][:-3] for k in files if k.startswith(".opencode/commands/")}
    quoin_token_re = re.compile(r"/(quoin-[a-z0-9-]+)")
    tier_word_re = re.compile(r"\b(haiku|sonnet|opus)\b", re.IGNORECASE)
    script_py_names = tuple("%s.py" % n for n in scripts.ALLOWED_SCRIPTS)

    for relpath, rf in files.items():
        if rf.kind != "skill":
            continue
        text = rf.content.decode("utf-8")
        assert "JSONL" not in text, relpath
        assert not re.search(r"\bhooks?\b", text, re.IGNORECASE), relpath
        assert not tier_word_re.search(text), relpath
        for script_name in script_py_names:
            assert script_name not in text, (relpath, script_name)
        assert "branch-recovery.md" not in text, relpath
        untranslated = slash_re.search(text)
        assert untranslated is None, (relpath, untranslated)
        for m in quoin_token_re.finditer(text):
            assert m.group(1) in command_names, (relpath, m.group(0))


def test_gate_skill_contains_script_forms_and_check_rules():
    files = generate.render_source_dir(SOURCE_DIR)
    gate = files[".opencode/skills/quoin-gate/SKILL.md"].content.decode("utf-8")
    assert "quoin opencode script validate_artifact" in gate
    assert "quoin opencode script path_resolve" in gate
    assert "a failed check" in gate
    assert "partial, not as a pass" in gate


def test_review_and_critic_skills_state_calling_role_persists_findings():
    files = generate.render_source_dir(SOURCE_DIR)
    for name in ("quoin-review", "quoin-critic"):
        text = files[".opencode/skills/%s/SKILL.md" % name].content.decode("utf-8")
        assert "the calling role writes the artifact" in text


def test_every_referenced_script_resolves_under_the_source_dir():
    files = generate.render_source_dir(SOURCE_DIR)
    for relpath, rf in files.items():
        text = rf.content.decode("utf-8")
        for name in scripts.referenced_scripts(text):
            path = scripts.script_path(SOURCE_DIR, name)
            assert path.exists(), (relpath, name, path)


def test_no_script_reference_line_pairs_with_a_redirection_character():
    files = generate.render_source_dir(SOURCE_DIR)
    placeholder_re = re.compile(r"<[^<>]+>")
    for relpath, rf in files.items():
        text = rf.content.decode("utf-8")
        for line in text.splitlines():
            if "quoin opencode script" not in line:
                continue
            stripped = placeholder_re.sub("", line)
            assert ">" not in stripped and "<" not in stripped, (relpath, line)


def test_forbidden_patterns_and_model_ids_over_every_rendered_file():
    from test_opencode_docs import _FORBIDDEN_PATTERNS, _model_id_denylist

    files = generate.render_source_dir(SOURCE_DIR)
    model_id_patterns = _model_id_denylist()
    artifact_root_pattern = next(p for p in _FORBIDDEN_PATTERNS if "artifacts" in p.pattern)
    for relpath, rf in files.items():
        text = rf.content.decode("utf-8")
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern is artifact_root_pattern:
                continue
            match = pattern.search(text)
            assert match is None, "%s matched %r in %s" % (pattern.pattern, match, relpath)
        for pattern in model_id_patterns:
            match = pattern.search(text)
            assert match is None, "%s matched %r in %s" % (pattern.pattern, match, relpath)


# --- determinism and dependency-exact digests ---


def _digest_map(files):
    return {k: rf.source_digest for k, rf in files.items()}


def _content_map(files):
    return {k: rf.content for k, rf in files.items()}


def _changed_keys(before, after):
    return sorted(k for k in before if before.get(k) != after.get(k))


def test_render_from_a_full_copy_matches_the_worktree_render(tmp_path):
    import shutil

    copy_dir = tmp_path / "quoin"
    shutil.copytree(SOURCE_DIR, copy_dir)
    files_worktree = generate.render_source_dir(SOURCE_DIR)
    files_copy = generate.render_source_dir(copy_dir)
    assert files_worktree.keys() == files_copy.keys()
    for k in files_worktree:
        assert files_worktree[k].content == files_copy[k].content, k
        assert files_worktree[k].source_digest == files_copy[k].source_digest, k
        text = files_copy[k].content.decode("utf-8")
        assert str(copy_dir) not in text, k
        assert str(Path.home()) not in text, k


def test_dependency_exact_bundle_contract_edit_changes_only_that_skill():
    inputs = _mutated_inputs()
    before = generate.render(inputs)

    edited = dict(inputs.contracts)
    edited["plan"] = edited["plan"].replace(
        "## Purpose\n", "## Purpose\nThis sentence exists only for a dependency-exactness test.\n", 1
    )
    inputs_after = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=inputs.overlays,
        templates=inputs.templates,
        contracts=edited,
        rules=inputs.rules,
    )
    after = generate.render(inputs_after)

    changed = _changed_keys(_digest_map(before), _digest_map(after))
    assert changed == [".opencode/skills/quoin-plan/SKILL.md"]
    assert changed == _changed_keys(_content_map(before), _content_map(after))


def test_dependency_exact_rules_edit_changes_only_the_instructions_document():
    inputs = _mutated_inputs()
    before = generate.render(inputs)

    inputs_after = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=inputs.overlays,
        templates=inputs.templates,
        contracts=inputs.contracts,
        rules=inputs.rules + "\n\nExtra rule prose for the dependency-exactness test.\n",
    )
    after = generate.render(inputs_after)

    changed = _changed_keys(_digest_map(before), _digest_map(after))
    assert changed == [generate.INSTRUCTIONS_PATH]
    assert changed == _changed_keys(_content_map(before), _content_map(after))


def test_dependency_exact_command_template_edit_changes_only_the_commands():
    inputs = _mutated_inputs()
    before = generate.render(inputs)

    templates_after = dict(inputs.templates)
    templates_after["command"] = "A dependency-exactness marker.\n\n" + templates_after["command"]
    inputs_after = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=inputs.overlays,
        templates=templates_after,
        contracts=inputs.contracts,
        rules=inputs.rules,
    )
    after = generate.render(inputs_after)

    changed = set(_changed_keys(_digest_map(before), _digest_map(after)))
    expected = {k for k in before if k.startswith(".opencode/commands/")}
    assert changed == expected
    assert changed == set(_changed_keys(_content_map(before), _content_map(after)))


def test_dependency_exact_agent_template_edit_changes_only_the_agents():
    inputs = _mutated_inputs()
    before = generate.render(inputs)

    templates_after = dict(inputs.templates)
    templates_after["agent"] = "A dependency-exactness marker.\n\n" + templates_after["agent"]
    inputs_after = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=inputs.overlays,
        templates=templates_after,
        contracts=inputs.contracts,
        rules=inputs.rules,
    )
    after = generate.render(inputs_after)

    changed = set(_changed_keys(_digest_map(before), _digest_map(after)))
    expected = {k for k in before if k.startswith(".opencode/agents/")}
    assert changed == expected
    assert changed == set(_changed_keys(_content_map(before), _content_map(after)))


def test_dependency_exact_role_prompt_edit_changes_only_that_agent():
    inputs = _mutated_inputs()
    before = generate.render(inputs)

    overlays_after = json.loads(json.dumps(inputs.overlays))
    overlays_after["roles"]["gate"]["prompt"].append("An extra paragraph for the dependency-exactness test.")
    inputs_after = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=overlays_after,
        templates=inputs.templates,
        contracts=inputs.contracts,
        rules=inputs.rules,
    )
    after = generate.render(inputs_after)

    changed = _changed_keys(_digest_map(before), _digest_map(after))
    assert changed == [".opencode/agents/quoin-gate.md"]
    assert changed == _changed_keys(_content_map(before), _content_map(after))


def test_dependency_exact_entry_notes_edit_changes_only_that_skill():
    inputs = _mutated_inputs()
    before = generate.render(inputs)

    overlays_after = json.loads(json.dumps(inputs.overlays))
    overlays_after["entries"]["plan"]["notes"].append("An extra note for the dependency-exactness test.")
    inputs_after = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=overlays_after,
        templates=inputs.templates,
        contracts=inputs.contracts,
        rules=inputs.rules,
    )
    after = generate.render(inputs_after)

    changed = _changed_keys(_digest_map(before), _digest_map(after))
    assert changed == [".opencode/skills/quoin-plan/SKILL.md"]
    assert changed == _changed_keys(_content_map(before), _content_map(after))


def test_dependency_exact_non_bundle_contract_edit_changes_nothing(tmp_path):
    import shutil

    copy_dir = tmp_path / "quoin"
    shutil.copytree(SOURCE_DIR, copy_dir)
    before = generate.render_source_dir(copy_dir)

    sleep_path = copy_dir / "core" / "skills" / "sleep.md"
    assert sleep_path.exists()
    sleep_path.write_text(sleep_path.read_text() + "\nExtra prose for the dependency-exactness test.\n")

    after = generate.render_source_dir(copy_dir)
    assert _digest_map(before) == _digest_map(after)
    assert _content_map(before) == _content_map(after)


def test_dependency_exact_new_catalog_id_mentioned_in_a_contract_changes_only_that_skill():
    inputs = _mutated_inputs()
    marker = "zzz-synthetic-marker"

    contracts = dict(inputs.contracts)
    contracts["plan"] = contracts["plan"].replace("## Purpose\n", "## Purpose\nSee /%s for detail.\n" % marker, 1)

    before_inputs = generate.GeneratorInputs(
        catalog=inputs.catalog,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=inputs.overlays,
        templates=inputs.templates,
        contracts=contracts,
        rules=inputs.rules,
    )
    before = generate.render(before_inputs)

    catalog_after = list(inputs.catalog) + [{"name": marker}]
    after_inputs = generate.GeneratorInputs(
        catalog=catalog_after,
        manifest=inputs.manifest,
        pinned_version=inputs.pinned_version,
        overlays=inputs.overlays,
        templates=inputs.templates,
        contracts=contracts,
        rules=inputs.rules,
    )
    after = generate.render(after_inputs)

    changed = _changed_keys(_digest_map(before), _digest_map(after))
    assert changed == [".opencode/skills/quoin-plan/SKILL.md"]
    assert changed == _changed_keys(_content_map(before), _content_map(after))


def test_generator_version_pin_forces_a_schema_bump_on_output_change():
    import hashlib

    templates = {
        "command": "Command body. {{SKILL_NAME}} {{TITLE}} {{COMMAND_NOTE}} $ARGUMENTS\n",
        "skill": "Skill body. {{SKILL_NAME}} {{COMMAND_NAME}} {{ROLE_AGENT}} {{CANONICAL_ID}} {{CONTRACT}} {{NOTES}}\n",
        "agent": "Agent body. {{ROLE_PROMPT}} {{DELEGATION}}\n",
        "instructions": (
            "Instructions. {{COMMAND_LIST}} {{ARTIFACT_ROOT}} {{ROLE_TABLE}} "
            "{{LIMITS}} {{UNAVAILABLE_LIST}} {{CORE_RULES}}\n"
        ),
    }
    contract = (
        "## Purpose\nDo the thing.\n\n"
        "## When to use\nWhen needed.\n\n"
        "## Inputs\nNone.\n\n"
        "## Output\nA result.\n\n"
        "## Behavior contract\nBehaves.\n"
    )
    overlays = {
        "entries": {
            "sample": {
                "description": "A sample entry for the version-pin test.",
                "command_note": "",
                "extra_sections": [],
                "notes": ["A note."],
                "rewrites": [],
            }
        },
        "roles": {"gate": {"description": "Sample role.", "prompt": ["Sample prompt."]}},
    }
    manifest_data = {
        "catalog_entries": [{"id": "sample", "status": "supported", "opencode": {"agent_role": "gate"}}],
        "roles": {"gate": {"mode": "primary", "summary": "Sample role summary."}},
    }
    inputs = generate.GeneratorInputs(
        catalog=[{"name": "sample"}],
        manifest=manifest_data,
        pinned_version="0.0.0",
        overlays=overlays,
        templates=templates,
        contracts={"sample": contract},
        rules="## Section\nSome rule text.\n",
    )
    files = generate.render(inputs)
    combined = b"".join(b"%s\0%s\0" % (k.encode("utf-8"), files[k].content) for k in sorted(files))
    digest = hashlib.sha256(combined).hexdigest()
    # Pinned under GENERATOR_SCHEMA_VERSION 1. A renderer change that alters
    # rendered bytes for these same inputs must update this hash and bump
    # GENERATOR_SCHEMA_VERSION, which moves every real digest in turn.
    assert generate.GENERATOR_SCHEMA_VERSION == 1
    assert digest == "64d75414a3cbeeab0d9992fda9fbf6be6ca18bf779e5ff1b703243e02f4fa727"


# --- core-input routing and source sweep ---


def _import_affected_tests():
    import sys

    scripts_dir = SOURCE_DIR / "core" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import affected_tests  # type: ignore

    return affected_tests


def test_bundle_contracts_select_the_generator_test():
    affected_tests = _import_affected_tests()
    manifest_data = _load_real_manifest()
    for row in _supported_rows(manifest_data):
        changed = ["quoin/core/skills/%s.md" % row["id"]]
        selectors, unmatched, ignored = affected_tests.map_changed_to_tests(changed, REPO_ROOT)
        assert not unmatched, (row["id"], unmatched)
        assert not ignored, (row["id"], ignored)
        assert any(Path(s).name == "test_opencode_generate.py" for s in selectors), row["id"]


def test_rules_md_selects_the_generator_test():
    affected_tests = _import_affected_tests()
    selectors, unmatched, ignored = affected_tests.map_changed_to_tests(
        ["quoin/core/workflow/rules.md"], REPO_ROOT
    )
    assert not unmatched
    assert not ignored
    assert any(Path(s).name == "test_opencode_generate.py" for s in selectors)


def test_self_swept_for_forbidden_and_model_id_patterns():
    from test_opencode_docs import _FORBIDDEN_PATTERNS, _model_id_denylist

    text = Path(__file__).read_text(encoding="utf-8")
    for pattern in _FORBIDDEN_PATTERNS:
        match = pattern.search(text)
        assert match is None, "%s matched %r" % (pattern.pattern, match)
    for pattern in _model_id_denylist():
        match = pattern.search(text)
        assert match is None, "%s matched %r" % (pattern.pattern, match)
