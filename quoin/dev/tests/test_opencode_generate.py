"""OpenCode M1a generator tests.

Grown task by task as the pure generator (`generate.py`), its helper
modules (`frontmatter.py`, `scripts.py`) and their supporting data
land. Expected id and path sets are built locally inside each test
function, or read from the manifest at test time, never kept as a
module-level ALL-CAPS collection (mirrors `test_opencode_manifest.py`,
which the registration roster census would otherwise pick up).
"""
from __future__ import annotations

import json

import pytest

from quoin.opencode_adapter import frontmatter

# Mirrors generate.ARTIFACT_ROOT, added when the generator module lands;
# a later task replaces this literal with that import directly.
_ARTIFACT_ROOT = ".workflow_artifacts"


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
