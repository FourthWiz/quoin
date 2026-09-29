"""A small JSON-Schema validator over a closed keyword subset.

The validator is generic: it knows nothing about runtime configuration. Any
keyword outside `SUPPORTED_KEYWORDS` raises `SchemaError` at validation time
instead of being ignored, so a schema can never silently loosen.
"""
from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .jsonio import dump_canonical

SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "enum",
        "const",
        "pattern",
        "minLength",
        "minimum",
        "items",
        "minItems",
        "uniqueItems",
        "$ref",
        "$defs",
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
    }
)

# Keywords whose value is a schema, a map of schemas or a list of names.
_SCHEMA_VALUED = ("additionalProperties", "propertyNames", "items")
_SCHEMA_MAPS = ("properties", "$defs")


class SchemaError(Exception):
    """The schema itself is unusable (unsupported keyword, bad reference)."""


@dataclass(frozen=True)
class Violation:
    path: Tuple[Any, ...]
    keyword: str
    key: Optional[str] = None
    expected: Optional[Tuple[Any, ...]] = None


def iter_keywords(schema: Any, path: Tuple[Any, ...] = ()) -> Iterator[Tuple[Tuple[Any, ...], str]]:
    """Yield (schema path, keyword) for every keyword in schema position.

    Property-name maps, `$defs` name maps and `enum`/`const` values are not
    schema positions and are skipped.
    """
    if not isinstance(schema, dict):
        return
    for keyword, value in schema.items():
        yield path, keyword
        if keyword in _SCHEMA_MAPS and isinstance(value, dict):
            for name, sub in value.items():
                yield from iter_keywords(sub, path + (keyword, name))
        elif keyword in _SCHEMA_VALUED:
            yield from iter_keywords(value, path + (keyword,))


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "null":
        return value is None
    raise SchemaError("unknown type name")


def _json_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def _normalise(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


@functools.lru_cache(maxsize=None)
def _compile(pattern: str) -> "re.Pattern[str]":
    """Compile a schema pattern with ECMA-262 end-of-input semantics: a final
    unescaped `$` matches only at the very end of the string, never before a
    trailing newline (which is what Python's `$` does)."""
    if pattern.endswith("$"):
        backslashes = len(pattern) - 1 - len(pattern[:-1].rstrip("\\"))
        if backslashes % 2 == 0:
            pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


class _Validator:
    def __init__(self, root: Dict[str, Any]):
        self.root = root
        self.defs = root.get("$defs", {})

    def resolve(self, ref: Any) -> Dict[str, Any]:
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            raise SchemaError("only local $defs references are supported")
        name = ref[len("#/$defs/"):]
        if name not in self.defs:
            raise SchemaError("reference target is missing")
        return self.defs[name]

    def check(self, instance: Any, schema: Any, path: Tuple[Any, ...], out: List[Violation]) -> None:
        if not isinstance(schema, dict):
            raise SchemaError("schema must be an object")
        for keyword in schema:
            if keyword not in SUPPORTED_KEYWORDS:
                raise SchemaError("unsupported schema keyword")
        if "$ref" in schema:
            self.check(instance, self.resolve(schema["$ref"]), path, out)
        if "type" in schema:
            names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_is_type(instance, n) for n in names):
                out.append(Violation(path, "type", expected=tuple(names)))
                return
        if "enum" in schema and not any(_json_equal(instance, v) for v in schema["enum"]):
            out.append(Violation(path, "enum", expected=tuple(schema["enum"])))
        if "const" in schema and not _json_equal(instance, schema["const"]):
            out.append(Violation(path, "const", expected=(schema["const"],)))
        if isinstance(instance, str):
            if "pattern" in schema and not _compile(schema["pattern"]).search(instance):
                out.append(Violation(path, "pattern"))
            if "minLength" in schema and len(instance) < schema["minLength"]:
                out.append(Violation(path, "minLength"))
        if (
            "minimum" in schema
            and isinstance(instance, (int, float))
            and not isinstance(instance, bool)
            and instance < schema["minimum"]
        ):
            out.append(Violation(path, "minimum"))
        if isinstance(instance, dict):
            self._object(instance, schema, path, out)
        if isinstance(instance, list):
            self._array(instance, schema, path, out)

    def _object(self, instance: dict, schema: dict, path: Tuple[Any, ...], out: List[Violation]) -> None:
        props = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in instance:
                out.append(Violation(path, "required", key=key, expected=(key,)))
        if "propertyNames" in schema:
            for key in sorted(instance):
                sub: List[Violation] = []
                self.check(key, schema["propertyNames"], path, sub)
                if sub:
                    out.append(Violation(path, "propertyNames", key=key))
        extra = schema.get("additionalProperties", True)
        for key in sorted(instance):
            if key in props:
                self.check(instance[key], props[key], path + (key,), out)
            elif extra is False:
                out.append(Violation(path, "additionalProperties", key=key))
            elif isinstance(extra, dict):
                self.check(instance[key], extra, path + (key,), out)

    def _array(self, instance: list, schema: dict, path: Tuple[Any, ...], out: List[Violation]) -> None:
        if "minItems" in schema and len(instance) < schema["minItems"]:
            out.append(Violation(path, "minItems"))
        if "items" in schema:
            items = schema["items"]
            if not isinstance(items, dict):
                raise SchemaError("tuple-form items is not supported")
            for index, value in enumerate(instance):
                self.check(value, items, path + (index,), out)
        if schema.get("uniqueItems"):
            seen = set()
            for value in instance:
                form = dump_canonical(_normalise(value))
                if form in seen:
                    out.append(Violation(path, "uniqueItems"))
                    break
                seen.add(form)


def validate(instance: Any, root_schema: Dict[str, Any], entry: str) -> List[Violation]:
    validator = _Validator(root_schema)
    out: List[Violation] = []
    validator.check(instance, validator.resolve("#/$defs/" + entry), (), out)
    return out
