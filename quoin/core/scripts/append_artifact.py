#!/usr/bin/env python3
"""append_artifact.py - append a row or section to a workflow artifact.

Skills call this instead of the editor's Write/Edit tools when they only need
to add text to the end of a file (insights scratchpad, lessons log,
workflow suggestions, soft-forget archive, cost ledger). The editor tools refuse to write a file
that was not read first in the session, or that changed since it was read;
appending through this helper needs neither, and is safe when several agents
append to the same file at once.

Usage
  python3 append_artifact.py [--header TEXT] [--blank-line] [--text TEXT] DST
  printf '%s\\n' "row" | python3 append_artifact.py DST

  The text comes from --text, or else from stdin (refused when stdin is a
  terminal, which would mean the caller forgot the pipe). Quote every path:
  project folders often contain spaces.

Behaviour
  - Missing parent directories and a missing DST are created.
  - --header is written first when DST is missing or empty; it is ignored for
    a file that already has content, so callers can pass it on every call.
  - When DST does not end with a newline, one is added before the new text.
  - --blank-line separates the new text from existing content by exactly one
    empty line (for files whose entries are blank-line separated).
  - The text is written with a trailing newline (added when absent).
  - The append holds an exclusive lock on DST (where the platform supports
    it) and is issued as one write, so concurrent appends never interleave.

Output and exit codes
  Nothing is printed on success. Each failure prints exactly one line to
  stderr starting with `append_artifact: `.
  0 success; 1 operating-system or encoding error; 2 usage error (no text,
  empty text, stdin is a terminal, DST is a directory).
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

EXIT_OK = 0
EXIT_OS = 1
EXIT_USAGE = 2

PROG = "append_artifact"


def _fail(code: int, message: str) -> int:
    text = " ".join(str(message).split())
    sys.stderr.write("{}: {}\n".format(PROG, text))
    return code


def _os_message(exc: OSError) -> str:
    detail = exc.strerror or str(exc)
    if exc.filename:
        return "{}: {}".format(detail, exc.filename)
    return detail


def _encode(value: str | bytes) -> bytes:
    # argv and stdin may carry undecodable bytes; surrogateescape round-trips
    # them unchanged instead of failing.
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", "surrogateescape")


def _with_newline(data: bytes) -> bytes:
    return data if data.endswith(b"\n") else data + b"\n"


def _tail(fd: int, size: int) -> bytes:
    """Return the last (up to) two bytes of the file."""
    count = min(size, 2)
    if count == 0:
        return b""
    os.lseek(fd, size - count, os.SEEK_SET)
    return os.read(fd, count)


def _separator(size: int, tail: bytes, blank_line: bool) -> bytes:
    if size == 0:
        return b""
    if blank_line:
        if tail.endswith(b"\n\n") or (size == 1 and tail == b"\n"):
            return b""
        return b"\n" if tail.endswith(b"\n") else b"\n\n"
    return b"" if tail.endswith(b"\n") else b"\n"


def append(
    dst: str,
    text: str | bytes,
    header: str | bytes | None = None,
    blank_line: bool = False,
) -> int:
    """Append ``text`` to ``dst``; return an exit code."""
    try:
        body = _encode(text)
        head = _encode(header) if header else b""
    except UnicodeError as exc:
        return _fail(EXIT_OS, "cannot encode text: {}".format(exc))
    if body.strip(b"\n") == b"":
        return _fail(EXIT_USAGE, "nothing to append (empty text)")
    if blank_line:
        body = body.lstrip(b"\n")
    if os.path.isdir(dst):
        return _fail(EXIT_USAGE, "destination is a directory: {}".format(dst))
    parent = os.path.dirname(os.path.abspath(dst))
    if os.path.exists(parent) and not os.path.isdir(parent):
        return _fail(EXIT_OS, "parent is not a directory: {}".format(parent))
    try:
        os.makedirs(parent, exist_ok=True)
        fd = os.open(dst, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError as exc:
        return _fail(EXIT_OS, _os_message(exc))
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # Size and tail are read under the lock, so the header and the
            # separator see the file exactly as the write will.
            size = os.fstat(fd).st_size
            if size == 0 and head:
                data = _with_newline(head) + (b"\n" if blank_line else b"")
            else:
                data = _separator(size, _tail(fd, size), blank_line)
            data += _with_newline(body)
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        return _fail(EXIT_OS, _os_message(exc))
    finally:
        os.close(fd)
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    """Argument parser whose usage errors are one stderr line and exit 2."""

    def error(self, message: str):  # type: ignore[override]
        sys.exit(_fail(EXIT_USAGE, message))


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("dst", metavar="DST", help="file to append to")
    parser.add_argument("--text", help="text to append (default: read stdin)")
    parser.add_argument("--header", help="written first when DST is missing or empty")
    parser.add_argument("--blank-line", action="store_true", help="separate the text from existing content by one empty line")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    text = args.text
    if text is None:
        if sys.stdin is None or sys.stdin.isatty():
            return _fail(EXIT_USAGE, "no --text given and stdin is a terminal")
        text = sys.stdin.buffer.read()
    return append(args.dst, text, args.header, args.blank_line)


if __name__ == "__main__":
    sys.exit(main())
