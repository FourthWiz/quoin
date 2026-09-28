"""The allowlist of Quoin helper scripts an OpenCode role may run by name.

`run` is the command-line runner: it refuses to run anything when its own
standard output or standard error is a regular file (so a script's output
never lands in a redirect target), confines the one write-capable script's
output to the current project's artifact root before running it, and runs
the allowlisted script in-process otherwise. It never accepts an arbitrary
path — only a name from `ALLOWED_SCRIPTS`, resolved under a given source
directory.
"""
from __future__ import annotations

import argparse
import os
import re
import runpy
import stat
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from quoin.opencode_adapter import generate, install

ALLOWED_SCRIPTS = (
    "checkpoint_picker",
    "classify_critic_issues",
    "generate_discovery_map",
    "handoff_validate",
    "path_resolve",
    "validate_artifact",
)

# The only allowlisted script that writes a file (its --output/-o flag, or
# the default path under its positional project root). The other five only
# read and print.
WRITE_CAPABLE_SCRIPTS = ("generate_discovery_map",)

REFERENCE_RE = re.compile(r"quoin opencode script ([a-z_]+)(?![\w-])")


def script_path(source_dir, name: str) -> Path:
    if name not in ALLOWED_SCRIPTS:
        raise ValueError(
            "unknown script %r, expected one of %s" % (name, ALLOWED_SCRIPTS)
        )
    return Path(source_dir) / "core" / "scripts" / ("%s.py" % name)


def referenced_scripts(text: str) -> List[str]:
    return sorted(set(REFERENCE_RE.findall(text)))


# --- runner policy ---


class RunnerRefusal(Exception):
    """Raised when the runner cannot confirm a write-capable script's output
    would stay inside the current project's artifact root."""


def unsafe_output_fds(fstat=os.fstat) -> List[int]:
    """Which of fd 1, fd 2 are a regular file, a directory or a block device.

    A tty, a pipe, a socket or `/dev/null` (character device, FIFO, socket)
    is safe; a closed descriptor is safe too, because nothing can be written
    through it. `fstat` is a parameter so an in-process test is not fooled
    by pytest's own fd capture, which points fd 1 and 2 at temporary regular
    files.
    """
    unsafe: List[int] = []
    for fd in (1, 2):
        try:
            st = fstat(fd)
        except OSError:
            continue
        mode = st.st_mode
        if not (stat.S_ISCHR(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            unsafe.append(fd)
    return unsafe


def find_project_root(start) -> Optional[Path]:
    """Nearest ancestor of `start` (inclusive) whose `.quoin` is a real
    directory (not a symlink) holding a regular metadata file at
    METADATA_RELPATH (not a symlink), matching `install.load_metadata`'s
    own validation. A symlinked `.quoin` is skipped, not resolved through:
    the walk continues to the next ancestor rather than crediting a
    project it merely points at.
    """
    current = Path(start).resolve()
    candidates = [current] + list(current.parents)
    for candidate in candidates:
        quoin_dir = candidate / ".quoin"
        try:
            dir_st = os.lstat(str(quoin_dir))
        except OSError:
            continue
        if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
            continue
        meta_path = candidate / install.METADATA_RELPATH
        try:
            file_st = os.lstat(str(meta_path))
        except OSError:
            continue
        if stat.S_ISLNK(file_st.st_mode) or not stat.S_ISREG(file_st.st_mode):
            continue
        return candidate
    return None


_DISCOVERY_MAP_REFUSAL = (
    "cannot confirm where generate_discovery_map would write; pass --output "
    "inside the artifact root, --stdout, or no output flag"
)


def _build_discovery_map_mirror_parser() -> argparse.ArgumentParser:
    """The runner's mirror of `generate_discovery_map.py`'s own parser.

    Kept as its own function (rather than inlined in `discovery_map_output`)
    so a drift test can capture the real script's parser and compare their
    option sets directly, instead of duplicating this list a second time.
    """
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("project_root", nargs="?", default=None)
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--output", "-o")
    output_group.add_argument("--stdout", action="store_true")
    parser.add_argument("--generated-at")
    parser.add_argument("--project-name")
    parser.add_argument("--project-description")
    parser.add_argument("--runtime-adapter", action="append", dest="runtime_adapters")
    validate_group = parser.add_mutually_exclusive_group()
    validate_group.add_argument("--validate", action="store_true", default=True)
    validate_group.add_argument("--no-validate", action="store_false", dest="validate")
    parser.add_argument("--quiet", action="store_true")
    return parser


def discovery_map_output(argv: Sequence[str], cwd) -> Optional[Path]:
    """Mirror `generate_discovery_map.py`'s parser closely enough to predict its output path.

    Returns `None` for `--stdout` or `--help` (nothing is written to a
    file). Raises `RunnerRefusal` when the argv cannot be parsed the same
    unambiguous way the script's own parser would: a literal `--`, an
    unrecognized or abbreviated (`allow_abbrev=False`) token, or any other
    parse error.

    The returned path is lexically normalized (`os.path.normpath`) but
    deliberately NOT resolved against the filesystem: `os.path.realpath`
    would silently follow a symlink planted at any component, including the
    final one, which is exactly the input `_confined_write_path` and the
    caller's own `--output` injection need to see untouched in order to
    catch it instead.
    """
    argv = list(argv)
    if "--" in argv:
        raise RunnerRefusal(_DISCOVERY_MAP_REFUSAL)

    parser = _build_discovery_map_mirror_parser()
    try:
        namespace, extras = parser.parse_known_args(argv)
    except SystemExit:
        raise RunnerRefusal(_DISCOVERY_MAP_REFUSAL)
    if extras:
        raise RunnerRefusal(_DISCOVERY_MAP_REFUSAL)

    if namespace.help or namespace.stdout:
        return None

    cwd = str(cwd)
    if namespace.output is not None:
        effective = os.path.join(cwd, namespace.output)
    else:
        root = namespace.project_root if namespace.project_root is not None else cwd
        root = os.path.join(cwd, root)
        effective = os.path.join(root, generate.ARTIFACT_ROOT, "discovery-map.json")
    return Path(os.path.normpath(effective))


def _confined_write_path(project_root: str, dest: str) -> bool:
    """True iff `dest` sits strictly inside `project_root`'s artifact root,
    with no symlinked path component between the two.

    Never calls `realpath`: instead it walks each path component from
    `project_root` down to `dest` with `os.lstat` (mirroring
    `install.inspect_path`'s technique), so a symlink anywhere in the chain
    — including the artifact-root directory itself — is refused rather than
    followed. `dest == artifact_root` is refused too, since its `.tmp`
    sibling would land beside, not inside, the artifact root.
    """
    project_root = os.path.normpath(project_root)
    artifact_root = os.path.normpath(os.path.join(project_root, generate.ARTIFACT_ROOT))
    dest = os.path.normpath(dest)
    prefix = artifact_root + os.sep
    if dest == artifact_root or not dest.startswith(prefix):
        return False

    current = project_root
    for part in os.path.relpath(dest, project_root).split(os.sep):
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            return False
    return True


def run(name, argv, source_dir, out=sys.stdout, err=sys.stderr, fstat=os.fstat) -> int:
    """Run an allowlisted script in-process. Refuses before running anything
    it cannot confirm is safe; see the module docstring."""
    cwd = os.getcwd()

    unsafe = unsafe_output_fds(fstat)
    if unsafe:
        message = "quoin opencode script: refusing to run: standard output or standard error is redirected to a file"
        for fd, stream in ((1, out), (2, err)):
            if fd not in unsafe:
                print(message, file=stream)
                break
        return 2

    if name not in ALLOWED_SCRIPTS:
        print(
            "quoin opencode script: unknown script %r, expected one of %s" % (name, ALLOWED_SCRIPTS),
            file=err,
        )
        return 2

    script = script_path(source_dir, name)
    if not script.is_file():
        print("quoin opencode script: %s is not a regular file" % script, file=err)
        return 2

    argv = list(argv)
    if name in WRITE_CAPABLE_SCRIPTS:
        root = find_project_root(cwd)
        if root is None:
            print(
                "quoin opencode script: run from inside a project where the OpenCode assets are installed",
                file=err,
            )
            return 2
        try:
            dest = discovery_map_output(argv, cwd)
        except RunnerRefusal as exc:
            print("quoin opencode script: %s" % exc, file=err)
            return 2
        if dest is not None:
            if not _confined_write_path(str(root), str(dest)):
                print(
                    "quoin opencode script: refusing to run: %s is outside the artifact root, "
                    "or reaches it through a symlinked path component" % dest,
                    file=err,
                )
                return 2
            if os.path.basename(str(dest)) != "discovery-map.json":
                print(
                    "quoin opencode script: refusing to run: generate_discovery_map may only "
                    "write discovery-map.json inside the artifact root",
                    file=err,
                )
                return 2
            dest_tmp = str(dest) + ".tmp"
            if os.path.lexists(dest_tmp):
                print(
                    "quoin opencode script: refusing to run: %s already exists" % dest_tmp,
                    file=err,
                )
                return 2
            # Pass the exact, already-checked destination through explicitly
            # rather than letting the script re-derive its own default: that
            # default is built from an unresolved project_root/output join,
            # which can disagree with the path just confined above (the
            # runner and the script must open the same file).
            argv = argv + ["--output", str(dest)]

    if name == "validate_artifact":
        sidecar = Path(source_dir) / "memory" / "format-kit.sections.json"
        if sidecar.is_file():
            argv = ["--sections-json", str(sidecar)] + argv

    old_argv = sys.argv[:]
    try:
        sys.argv = [str(script), *argv]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            if code is None:
                return 0
            if isinstance(code, int):
                return code
            print(code, file=err)
            return 1
    finally:
        sys.argv = old_argv
    return 0
