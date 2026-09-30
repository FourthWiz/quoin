#!/usr/bin/env python3
"""fsops.py - file operations for skill instructions, without shell rm/rmdir/mv.

Skills run these through `python3 <quoin-home>/scripts/fsops.py <subcommand> ...`
so the commands stay available when the shell deny rules block `rm`, `rmdir`
and `mv`. Quote every path argument: project folders often contain spaces.

Subcommands
  mv [--parents] [--overwrite] SRC DST
      Rename SRC to DST. DST is the final path, never a directory to move SRC
      into. An existing file at DST is replaced. An existing empty directory at
      DST is replaced by a directory SRC. A non-empty directory at DST is
      refused (exit 3) unless --overwrite is given, the destination is inside a
      the workflow-artifacts directory tree outside `memory/`, and SRC is a directory. With
      --overwrite the old directory is renamed to `DST.fsops-old-<pid>`, the
      move is made, and the old directory is deleted; if the move fails the old
      directory is put back. If the helper dies between the two renames, the
      previous contents remain in the leftover `DST.fsops-old-<pid>` sibling and
      can be renamed back by hand. --parents creates the missing parent
      directories of DST. Symbolic links are moved as links. A move across
      filesystems falls back to copy-and-delete.

  rm [-r|--recursive] PATH...
      Delete files and symbolic links (a link is removed, never its target).
      A missing path is not an error. A directory needs -r, and -r only deletes
      directories inside a workflow-artifacts tree: the workflow-artifacts directory
      directory itself and everything under its `memory/` directory are
      refused (exit 3). Confinement is decided on the fully resolved path and
      compares names without regard to case, so `MEMORY` or a symlink alias
      cannot slip through. On a case-sensitive filesystem this also treats a
      directory spelled `.Workflow_Artifacts` as a workflow-artifacts root.
      Every path is attempted; the exit code is the worst per-path outcome.

  write-atomic [--parents] DST
      Read all of stdin, write it to `DST.tmp`, flush it to disk, then rename it
      over DST. Refuses (exit 2) when stdin is a terminal, which would mean the
      caller forgot the pipe.

  trash --base DIR PATH...
      Move each path to `DIR/trash/<UTC date>/<name>`, appending `-1`, `-2`, ...
      when that name is taken. Directories and links are accepted. A missing
      path is reported (exit 1) and the remaining paths are still moved.

  finalize SRC DST --cleanup PATH...
      Rename SRC over DST, then always remove the --cleanup paths (missing ones
      are ignored), even when the rename failed. Exits non-zero if the rename
      failed, so cleanup never hides a failed write.

Output and exit codes
  Nothing is printed on success. Each failure prints exactly one line to stderr
  starting with `fsops <subcommand>: `.
  0 success; 1 operating-system error or missing source; 2 usage error;
  3 refused by a safety rule.
"""
from __future__ import annotations

import argparse
import datetime
import errno
import os
import shutil
import stat
import sys
from pathlib import PurePath

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

_ROOT_NAME = ".workflow_artifacts"
_GUARDED_CHILD = "memory"


def _fail(sub: str, message: str, code: int) -> int:
    text = " ".join(str(message).split())
    sys.stderr.write("fsops {}: {}\n".format(sub, text))
    return code


def _os_message(path: str, exc: OSError) -> str:
    return "{}: {}".format(path, exc.strerror or exc)


def _strip_seps(path: str) -> str:
    """Drop trailing separators so a link spelled `link/` is treated as the link."""
    stripped = path.rstrip("/" + (os.sep if os.sep != "/" else ""))
    return stripped if stripped else path[:1]


def _samefile_or_false(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def is_confined(resolved: str) -> bool:
    """True when `resolved` (a realpath) may be deleted recursively.

    It must lie strictly inside a the workflow-artifacts directory directory and not under
    that directory's `memory/` child. Names are compared case-insensitively and
    any prefix that is the same file as the root or its `memory/` directory
    (for example through a Unicode-normalization alias) also refuses.
    """
    parts = PurePath(resolved).parts
    roots = [i for i, name in enumerate(parts) if name.casefold() == _ROOT_NAME]
    if not roots or parts[-1].casefold() == _ROOT_NAME:
        return False
    for i in roots:
        if i + 1 < len(parts) and parts[i + 1].casefold() == _GUARDED_CHILD:
            return False
    for i in roots:
        root = os.path.join(*parts[: i + 1])
        memory = os.path.join(root, _GUARDED_CHILD)
        for j in range(i + 1, len(parts)):
            prefix = os.path.join(*parts[: j + 1])
            if _samefile_or_false(prefix, root) or _samefile_or_false(prefix, memory):
                return False
    return True


def _is_real_dir(path: str) -> bool:
    return os.path.isdir(path) and not os.path.islink(path)


def _move(src: str, dst: str) -> None:
    """Rename src to dst, falling back to copy-and-delete across filesystems."""
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        if _is_real_dir(dst):
            os.rmdir(dst)
        shutil.move(src, dst)


def mv(src: str, dst: str, parents: bool = False, overwrite: bool = False) -> int:
    src = _strip_seps(src)
    dst = _strip_seps(dst)
    try:
        src_mode = os.lstat(src).st_mode
    except OSError as exc:
        return _fail("mv", _os_message(src, exc), EXIT_ERROR)
    aside = None
    try:
        if parents:
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
        if _is_real_dir(dst) and os.listdir(dst):
            if not overwrite:
                return _fail("mv", "{}: destination is a non-empty directory".format(dst), EXIT_REFUSED)
            if not stat.S_ISDIR(src_mode):
                return _fail("mv", "{}: refusing to replace a directory with a non-directory".format(dst), EXIT_REFUSED)
            if not is_confined(os.path.realpath(dst)):
                return _fail("mv", "{}: refusing to overwrite a directory outside {}".format(dst, _ROOT_NAME), EXIT_REFUSED)
            candidate = "{}.fsops-old-{}".format(dst, os.getpid())
            os.replace(dst, candidate)
            aside = candidate
        _move(src, dst)
    except OSError as exc:
        if aside is None:
            return _fail("mv", _os_message(src, exc), EXIT_ERROR)
        return _restore_aside("mv", aside, dst, exc)
    if aside is not None:
        try:
            shutil.rmtree(aside)
        except OSError as exc:
            sys.stderr.write("fsops mv: moved, but could not delete {}: {}\n".format(aside, exc.strerror or exc))
    return EXIT_OK


def _restore_aside(sub: str, aside: str, dst: str, cause: OSError) -> int:
    """Put the previous destination back after a failed overwrite."""
    try:
        if os.path.lexists(dst):
            if _is_real_dir(dst):
                shutil.rmtree(dst)
            else:
                os.unlink(dst)
        os.replace(aside, dst)
    except OSError as exc:
        return _fail(
            sub,
            "{}; previous destination is kept at {} ({})".format(
                _os_message(dst, cause), aside, exc.strerror or exc
            ),
            EXIT_ERROR,
        )
    return _fail(sub, _os_message(dst, cause), EXIT_ERROR)


def _rm_one(path: str, recursive: bool) -> int:
    p = _strip_seps(path)
    try:
        mode = os.lstat(p).st_mode
    except FileNotFoundError:
        return EXIT_OK
    except OSError as exc:
        return _fail("rm", _os_message(p, exc), EXIT_ERROR)
    try:
        if stat.S_ISLNK(mode):
            os.unlink(p)
        elif stat.S_ISDIR(mode):
            if not recursive:
                return _fail("rm", "{}: is a directory; pass -r".format(p), EXIT_REFUSED)
            if not is_confined(os.path.realpath(p)):
                return _fail("rm", "{}: refusing recursive delete outside {} (or inside its {}/)".format(p, _ROOT_NAME, _GUARDED_CHILD), EXIT_REFUSED)
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except FileNotFoundError:
        return EXIT_OK
    except OSError as exc:
        return _fail("rm", _os_message(p, exc), EXIT_ERROR)
    return EXIT_OK


def rm(paths: list[str], recursive: bool = False) -> int:
    code = EXIT_OK
    for path in paths:
        code = max(code, _rm_one(path, recursive))
    return code


def write_atomic(dst: str, parents: bool = False) -> int:
    if sys.stdin.isatty():
        return _fail("write-atomic", "stdin is a terminal; pipe the content in", EXIT_USAGE)
    dst = _strip_seps(dst)
    tmp = dst + ".tmp"
    try:
        data = sys.stdin.buffer.read()
        if parents:
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dst)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return _fail("write-atomic", _os_message(dst, exc), EXIT_ERROR)
    return EXIT_OK


def _utc_day() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def trash(base: str, paths: list[str]) -> int:
    code = EXIT_OK
    for path in paths:
        p = _strip_seps(path)
        if not os.path.lexists(p):
            code = max(code, _fail("trash", "{}: no such file or directory".format(p), EXIT_ERROR))
            continue
        try:
            day_dir = os.path.join(base, "trash", _utc_day())
            os.makedirs(day_dir, exist_ok=True)
            name = os.path.basename(p)
            dest = os.path.join(day_dir, name)
            n = 1
            while os.path.lexists(dest):
                dest = os.path.join(day_dir, "{}-{}".format(name, n))
                n += 1
            _move(p, dest)
        except OSError as exc:
            code = max(code, _fail("trash", _os_message(p, exc), EXIT_ERROR))
    return code


def finalize(src: str, dst: str, cleanup: list[str]) -> int:
    src = _strip_seps(src)
    dst = _strip_seps(dst)
    code = EXIT_OK
    try:
        if not os.path.lexists(src):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), src)
        _move(src, dst)
    except OSError as exc:
        code = _fail("finalize", _os_message(src, exc), EXIT_ERROR)
    for path in cleanup:
        p = _strip_seps(path)
        try:
            if os.path.lexists(p):
                os.unlink(p)
        except OSError as exc:
            code = max(code, _fail("finalize", "cleanup " + _os_message(p, exc), EXIT_ERROR))
    return code


class _Parser(argparse.ArgumentParser):
    """Argument parser whose usage errors are one stderr line and exit 2."""

    def error(self, message: str):  # type: ignore[override]
        text = " ".join(str(message).split())
        sys.stderr.write("{}: {}\n".format(self.prog, text))
        sys.exit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="fsops",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subs = parser.add_subparsers(dest="command", metavar="SUBCOMMAND")
    subs.required = True

    p_mv = subs.add_parser("mv", help="rename SRC to DST")
    p_mv.add_argument("--parents", action="store_true", help="create missing parents of DST")
    p_mv.add_argument("--overwrite", action="store_true", help="replace a non-empty directory inside .workflow_artifacts")
    p_mv.add_argument("src", metavar="SRC")
    p_mv.add_argument("dst", metavar="DST")

    p_rm = subs.add_parser("rm", help="delete files, links, or confined directories")
    p_rm.add_argument("-r", "--recursive", action="store_true", help="allow deleting directories inside .workflow_artifacts")
    p_rm.add_argument("paths", nargs="+", metavar="PATH")

    p_wa = subs.add_parser("write-atomic", help="write stdin to DST atomically")
    p_wa.add_argument("--parents", action="store_true", help="create missing parents of DST")
    p_wa.add_argument("dst", metavar="DST")

    p_tr = subs.add_parser("trash", help="move paths to BASE/trash/<date>/")
    p_tr.add_argument("--base", required=True, metavar="DIR")
    p_tr.add_argument("paths", nargs="+", metavar="PATH")

    p_fi = subs.add_parser("finalize", help="rename SRC over DST, then remove cleanup paths")
    p_fi.add_argument("src", metavar="SRC")
    p_fi.add_argument("dst", metavar="DST")
    p_fi.add_argument("--cleanup", nargs="+", required=True, metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "mv":
        return mv(args.src, args.dst, args.parents, args.overwrite)
    if args.command == "rm":
        return rm(args.paths, args.recursive)
    if args.command == "write-atomic":
        return write_atomic(args.dst, args.parents)
    if args.command == "trash":
        return trash(args.base, args.paths)
    return finalize(args.src, args.dst, args.cleanup)


if __name__ == "__main__":
    sys.exit(main())
