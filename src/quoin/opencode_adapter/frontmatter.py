"""Stdlib-only frontmatter emitter and restricted parser.

The OpenCode generator writes Markdown files with a YAML-looking
frontmatter block ahead of the body text. Rather than depend on a YAML
library at runtime, this module emits a deliberately narrow subset of
YAML (bare or JSON-quoted keys, JSON double-quoted string scalars,
2-space nested maps) and parses only that same subset back. Anything
outside the subset — flow collections, unquoted scalars, anchors,
block scalars, tabs, CRLF — is rejected rather than guessed at.

`emit` and `parse` are inverses of each other over ordered `dict`
trees whose leaves are strings: `parse(emit(fields)) == (fields, "")`.
"""
from __future__ import annotations

import json
import re
from typing import Dict, Tuple

_BARE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f]")
_FENCE = "---"
_INDENT_UNIT = "  "

_JSON_DECODER = json.JSONDecoder()


class FrontmatterError(ValueError):
    pass


def _emit_key(key) -> str:
    if not isinstance(key, str) or key == "":
        raise FrontmatterError("frontmatter keys must be non-empty strings, got %r" % (key,))
    if _BARE_KEY_RE.match(key):
        return key
    return json.dumps(key, ensure_ascii=True)


def _emit_map(fields: Dict, lines, level: int) -> None:
    if not isinstance(fields, dict):
        raise FrontmatterError("frontmatter map must be a dict, got %r" % (type(fields).__name__,))
    if not fields:
        raise FrontmatterError("frontmatter map must not be empty")
    indent = _INDENT_UNIT * level
    for key, value in fields.items():
        key_repr = _emit_key(key)
        if isinstance(value, str):
            if _CONTROL_CHAR_RE.search(value):
                raise FrontmatterError("value for key %r contains a control character" % (key,))
            lines.append("%s%s: %s" % (indent, key_repr, json.dumps(value, ensure_ascii=True)))
        elif isinstance(value, dict):
            lines.append("%s%s:" % (indent, key_repr))
            _emit_map(value, lines, level + 1)
        else:
            raise FrontmatterError(
                "value for key %r must be a str or dict, got %r" % (key, type(value).__name__)
            )


def emit(fields: Dict) -> str:
    """Render `fields` (an ordered dict of str/dict) as a frontmatter block.

    Insertion order is emission order, never sorted. Raises
    `FrontmatterError` for a non-str/non-dict value, an empty key, an
    empty map (top-level or nested), or a string value containing a
    control character (U+0000 to U+001F).
    """
    lines = [_FENCE]
    _emit_map(fields, lines, 0)
    lines.append(_FENCE)
    return "\n".join(lines) + "\n"


def _split_key(rest_of_line: str, line_no: int) -> Tuple[str, str]:
    if rest_of_line.startswith('"'):
        try:
            key, end = _JSON_DECODER.raw_decode(rest_of_line)
        except json.JSONDecodeError as exc:
            raise FrontmatterError("line %d: invalid quoted key: %s" % (line_no, exc)) from exc
        if not isinstance(key, str):
            raise FrontmatterError("line %d: quoted key must decode to a string" % line_no)
        remainder = rest_of_line[end:]
        if not remainder.startswith(":"):
            raise FrontmatterError("line %d: expected ':' after quoted key" % line_no)
        return key, remainder[1:]

    colon_idx = rest_of_line.find(":")
    if colon_idx == -1:
        raise FrontmatterError("line %d: expected ':' after key" % line_no)
    candidate = rest_of_line[:colon_idx]
    if not _BARE_KEY_RE.match(candidate):
        raise FrontmatterError("line %d: invalid bare key %r" % (line_no, candidate))
    return candidate, rest_of_line[colon_idx + 1 :]


def _parse_scalar(value_text: str, line_no: int) -> str:
    try:
        value, end = _JSON_DECODER.raw_decode(value_text)
    except json.JSONDecodeError as exc:
        raise FrontmatterError("line %d: invalid JSON scalar value: %s" % (line_no, exc)) from exc
    if end != len(value_text):
        raise FrontmatterError("line %d: unexpected trailing content after value" % line_no)
    if not isinstance(value, str):
        raise FrontmatterError("line %d: frontmatter values must be JSON strings" % line_no)
    return value


def _parse_map(lines, i: int, end: int, level: int) -> Tuple[Dict, int]:
    result: Dict = {}
    indent = _INDENT_UNIT * level
    while i < end:
        raw = lines[i]
        line_no = i + 1
        if "\t" in raw:
            raise FrontmatterError("line %d: tab characters are not allowed" % line_no)
        if not raw.startswith(indent):
            break
        rest_of_line = raw[len(indent) :]
        if rest_of_line == "" or rest_of_line.startswith(" "):
            raise FrontmatterError("line %d: unexpected indentation or blank line" % line_no)

        key, sep_rest = _split_key(rest_of_line, line_no)
        if sep_rest == "":
            i += 1
            child, i = _parse_map(lines, i, end, level + 1)
            value = child
        elif sep_rest.startswith(" "):
            value = _parse_scalar(sep_rest[1:], line_no)
            i += 1
        else:
            raise FrontmatterError("line %d: expected ':' followed by a space or newline" % line_no)

        if key in result:
            raise FrontmatterError("line %d: duplicate key %r" % (line_no, key))
        result[key] = value

    if not result:
        raise FrontmatterError("line %d: expected at least one field" % (i + 1 if i < len(lines) else i))
    return result, i


def parse(text: str) -> Tuple[Dict, str]:
    """Parse a frontmatter block written by `emit`.

    Returns `(fields, body)` where `body` is the text after the
    closing fence. Raises `FrontmatterError` naming the offending line
    number for anything outside the restricted subset `emit` produces:
    flow maps, unquoted scalars, tabs, anchors, block scalars, CRLF,
    or duplicate keys.
    """
    if not isinstance(text, str):
        raise FrontmatterError("frontmatter text must be a str, got %r" % (type(text).__name__,))
    if "\r" in text:
        raise FrontmatterError("line 1: CRLF line endings are not supported")

    lines = text.split("\n")
    if not lines or lines[0] != _FENCE:
        raise FrontmatterError("line 1: document must start with '---'")

    close_i = None
    for i in range(1, len(lines)):
        if lines[i] == _FENCE:
            close_i = i
            break
    if close_i is None:
        raise FrontmatterError("line %d: missing closing '---' fence" % len(lines))

    fields, next_i = _parse_map(lines, 1, close_i, 0)
    if next_i != close_i:
        raise FrontmatterError("line %d: unexpected content before closing fence" % (next_i + 1))

    body = "\n".join(lines[close_i + 1 :])
    return fields, body
