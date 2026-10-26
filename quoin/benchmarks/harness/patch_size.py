"""
patch_size.py - Pure patch-size measure over the text of a `git diff` patch.

A patch is split into one section per `diff --git` header. Every section counts
as one touched file, even when it has no hunks (an empty new file, a mode-only
change, a pure rename, a binary change), because `git diff --cached --binary`
emits no `---`/`+++` lines for those. Lines are counted from the hunk headers'
line budgets, so content lines that start with `---`, `+++` or `-- ` are
counted as changes rather than mistaken for file headers.
"""

from __future__ import annotations

import re

TEST_FILE_RULE = (
    "A file counts as a test file when a directory in its path is named tests, test "
    "or testing, or its name starts with test_, ends with _test.py, equals or ends "
    "with tests.py, or is conftest.py."
)

_TEST_DIRS = frozenset({"tests", "test", "testing"})
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_OCTAL = re.compile(rb"\\([0-7]{3})")
_SIMPLE_ESCAPES = {
    b"a": b"\a", b"b": b"\b", b"f": b"\f", b"n": b"\n", b"r": b"\r",
    b"t": b"\t", b"v": b"\v", b'"': b'"', b"\\": b"\\",
}


class _Unreadable(Exception):
    """A section whose path cannot be resolved; the whole patch is unreadable."""


def is_test_path(path: str) -> bool:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    if any(d in _TEST_DIRS for d in parts[:-1]):
        return True
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "tests.py"
        or name.endswith("tests.py")
        or name == "conftest.py"
    )


def _zero() -> dict:
    return {"added": 0, "removed": 0, "files": 0}


def _empty(status: str) -> dict:
    return {
        "status": status, "non_test": _zero(), "test": _zero(),
        "files": 0, "binary_files": 0, "lines_changed": 0,
    }


def _unquote(token: str) -> str:
    """Undo git's C-style quoting of a path; plain tokens pass through."""
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        return token
    raw = token[1:-1].encode("utf-8", errors="surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i:i + 1]
        if ch == b"\\" and i + 1 < len(raw):
            octal = _OCTAL.match(raw, i)
            if octal:
                out.append(int(octal.group(1), 8))
                i += 4
                continue
            esc = _SIMPLE_ESCAPES.get(raw[i + 1:i + 2])
            if esc is not None:
                out += esc
                i += 2
                continue
        out += ch
        i += 1
    return bytes(out).decode("utf-8", errors="replace")


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _split_header(rest: str, deleted: bool):
    """Return the path named by a `diff --git` header, or None when ambiguous."""
    if rest.startswith('"'):
        end = 1
        while end < len(rest):
            if rest[end] == "\\":
                end += 2
                continue
            if rest[end] == '"':
                break
            end += 1
        first, second = rest[: end + 1], rest[end + 1:].strip()
    elif " " in rest:
        mid = (len(rest) - 1) // 2
        if len(rest) % 2 == 1 and rest[mid] == " ":
            first, second = rest[:mid], rest[mid + 1:]
        elif rest.count(" b/") == 1:
            first, second = rest.split(" b/", 1)
            second = "b/" + second
        else:
            return None
    else:
        return None
    a_path, b_path = _unquote(first), _unquote(second)
    return _strip_prefix(a_path if deleted else b_path) or None


def _section_path(header: str, lines: list) -> str:
    deleted = any(l.startswith("deleted file mode") for l in lines)
    path = _split_header(header, deleted)
    for line in lines:
        if line.startswith("rename to ") or line.startswith("copy to "):
            path = _unquote(line.split(" to ", 1)[1].strip())
    minus = plus = None
    for line in lines:
        if line.startswith("--- "):
            minus = line[4:].rstrip("\t").strip()
        elif line.startswith("+++ "):
            plus = line[4:].rstrip("\t").strip()
        elif line.startswith("@@"):
            break
    if plus is not None and plus != "/dev/null":
        path = _strip_prefix(_unquote(plus))
    elif plus == "/dev/null" and minus is not None and minus != "/dev/null":
        path = _strip_prefix(_unquote(minus))
    if not path:
        raise _Unreadable()
    return path


def _count_section(lines: list):
    """Return (added, removed, binary) for one section's lines."""
    added = removed = 0
    binary = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _HUNK.match(line)
        if m:
            old_left = int(m.group(1)) if m.group(1) is not None else 1
            new_left = int(m.group(2)) if m.group(2) is not None else 1
            i += 1
            while i < n and (old_left > 0 or new_left > 0):
                body = lines[i]
                if body.startswith("\\"):
                    i += 1
                    continue
                if body.startswith("+"):
                    added += 1
                    new_left -= 1
                elif body.startswith("-"):
                    removed += 1
                    old_left -= 1
                else:
                    old_left -= 1
                    new_left -= 1
                i += 1
            continue
        if line.startswith("GIT binary patch") or (
            line.startswith("Binary files ") and line.rstrip().endswith("differ")
        ):
            binary = True
        i += 1
    return added, removed, binary


def measure_patch(data) -> dict:
    """Measure a patch given as bytes; None means the file was missing."""
    if data is None:
        return _empty("missing")
    try:
        if not data:
            return _empty("empty")
        text = data.decode("utf-8", errors="replace")
        sections = []
        current = None
        for line in text.replace("\r\n", "\n").split("\n"):
            if line.startswith("diff --git "):
                current = (line[len("diff --git "):], [])
                sections.append(current)
            elif current is not None:
                current[1].append(line)
        if not sections:
            return _empty("empty")
        result = _empty("ok")
        for header, lines in sections:
            path = _section_path(header, lines)
            added, removed, binary = _count_section(lines)
            bucket = result["test" if is_test_path(path) else "non_test"]
            bucket["added"] += added
            bucket["removed"] += removed
            bucket["files"] += 1
            result["files"] += 1
            result["binary_files"] += 1 if binary else 0
            result["lines_changed"] += added + removed
        return result
    except Exception:
        return _empty("unreadable")
