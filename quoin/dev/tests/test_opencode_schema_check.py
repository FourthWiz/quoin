"""Tests for the closed-keyword JSON-Schema subset validator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quoin.opencode_adapter import schema_check
from quoin.opencode_adapter.schema_check import SUPPORTED_KEYWORDS, SchemaError, validate

SCHEMAS = Path(__file__).resolve().parent.parent.parent / "adapters" / "opencode" / "schemas"


def check(instance, schema, defs=None):
    root = {"$defs": dict(defs or {}, e=schema)}
    return validate(instance, root, "e")


def kinds(violations):
    return [v.keyword for v in violations]


@pytest.mark.parametrize(
    "schema,good,bad",
    [
        ({"type": "string"}, "a", 1),
        ({"type": "integer"}, 3, 3.5),
        ({"type": "number"}, 3.5, "3"),
        ({"type": "boolean"}, True, 1),
        ({"type": "object"}, {}, []),
        ({"type": "array"}, [], {}),
        ({"type": ["string", "null"]}, None, 1),
        ({"enum": ["a", "b"]}, "a", "c"),
        ({"const": 1}, 1, 2),
        ({"pattern": "^a+$"}, "aaa", "b"),
        ({"minLength": 2}, "ab", "a"),
        ({"minimum": 1}, 1, 0),
        ({"minItems": 1}, [1], []),
        ({"uniqueItems": True}, [1, 2], [1, 1]),
        ({"items": {"type": "integer"}}, [1, 2], [1, "x"]),
    ],
)
def test_keyword_positive_negative(schema, good, bad):
    assert check(good, schema) == []
    assert check(bad, schema) != []


def test_bool_int_traps():
    assert check(True, {"type": "integer"})
    assert check(True, {"type": "number"})
    assert check(1, {"type": "boolean"})
    assert check(True, {"enum": [1]})
    assert check(1, {"enum": [True]})
    assert check(True, {"const": 1})
    assert check(1.0, {"const": 1}) == []
    assert check(True, {"minimum": 1}) == []  # minimum applies to numbers only
    assert check([1, True], {"uniqueItems": True}) == []
    assert check([1, 1.0], {"uniqueItems": True})


def test_object_keywords_and_order():
    schema = {
        "type": "object",
        "required": ["b", "a"],
        "properties": {"x": {"type": "integer"}},
        "additionalProperties": False,
    }
    got = check({"z": 1, "y": 2, "x": "no"}, schema)
    assert [(v.keyword, v.key, v.path) for v in got] == [
        ("required", "b", ()),
        ("required", "a", ()),
        ("type", None, ("x",)),
        ("additionalProperties", "y", ()),
        ("additionalProperties", "z", ()),
    ]
    assert got[0].expected == ("b",)


def test_additional_properties_schema_and_property_names():
    schema = {
        "type": "object",
        "propertyNames": {"pattern": "^[a-z]+$"},
        "additionalProperties": {"type": "integer"},
    }
    assert check({"a": 1}, schema) == []
    assert kinds(check({"A": 1}, schema)) == ["propertyNames"]
    assert kinds(check({"a": "x"}, schema)) == ["type"]


def test_min_length_counts_code_points():
    assert check("\U0001f600\U0001f600", {"minLength": 2}) == []


def test_ref_resolution():
    defs = {"n": {"type": "integer"}}
    assert check(1, {"$ref": "#/$defs/n"}, defs) == []
    assert check("x", {"$ref": "#/$defs/n"}, defs)


@pytest.mark.parametrize(
    "schema",
    [
        {"oneOf": []},
        {"format": "uri"},
        {"minProperties": 1},
        {"type": "array", "items": [{"type": "string"}]},
        {"$ref": "http://elsewhere/schema"},
        {"$ref": "#/$defs/missing"},
    ],
)
def test_unsupported_constructs_fail_closed(schema):
    with pytest.raises(SchemaError):
        check([1] if "items" in schema else {}, schema)


def test_unknown_keyword_in_nested_schema_fails_closed():
    schema = {"type": "object", "properties": {"a": {"oneOf": []}}}
    with pytest.raises(SchemaError):
        check({"a": 1}, schema)


def _schema_files():
    return sorted(SCHEMAS.glob("*.schema.json"))


def test_schema_files_found():
    assert _schema_files(), "no schema files found; the closure test would pass vacuously"


def test_keyword_closure_over_shipped_schemas():
    for path in _schema_files():
        root = json.loads(path.read_text(encoding="utf-8"))
        seen = list(schema_check.iter_keywords(root))
        assert seen
        for where, keyword in seen:
            assert keyword in SUPPORTED_KEYWORDS, "%s: %s at %r" % (path.name, keyword, where)


def test_walker_skips_name_maps_and_values():
    schema = {
        "properties": {"oneOf": {"type": "string"}},
        "$defs": {"format": {"enum": ["const", {"oneOf": 1}]}},
    }
    keywords = {k for _, k in schema_check.iter_keywords(schema)}
    assert "oneOf" not in keywords and "format" not in keywords


def test_validator_does_not_depend_on_config_module():
    text = Path(schema_check.__file__).read_text(encoding="utf-8")
    assert "config" not in [
        line.split()[-1] for line in text.splitlines() if line.startswith("from .") or line.startswith("import ")
    ]
    assert "from .config" not in text and "import config" not in text
