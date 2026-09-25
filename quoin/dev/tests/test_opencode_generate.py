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

from quoin.opencode_adapter import frontmatter, scripts

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
