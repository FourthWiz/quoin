"""Shared helpers for the tests that build the package: a tracked-tree copy and an isolated wheel/sdist build.

The build child inherits the real environment minus PYTHONPYCACHEPREFIX (the real HOME lets pip's cache satisfy
the isolated build requirement offline); its cwd and every output are temp directories.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
NETWORK_ERRORS = re.compile(
    r"Could not find a version|No matching distribution|Connection|Temporary failure|Name or service|"
    r"ProxyError|Failed to establish|Network is unreachable|Max retries|timed out|SSLError|No such host", re.I)


def copy_tracked_tree(dst, *, cached_only=False):
    """Copy the repo's tracked files (and, unless `cached_only`, untracked non-ignored ones) into `dst`.

    `quoin/dev/` and `quoin/benchmarks/` are skipped. Returns `dst`.
    """
    args = ["git", "-C", str(REPO), "ls-files", "-z", "--cached"]
    if not cached_only:
        args += ["--others", "--exclude-standard"]
    listing = subprocess.run(args, capture_output=True, check=True).stdout.decode().split("\0")
    for rel in listing:
        if not rel or rel.startswith(("quoin/dev/", "quoin/benchmarks/")):
            continue
        src = REPO / rel
        if not src.is_file():
            continue
        target = pathlib.Path(dst) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return dst


def build(tree, out, *, kinds=("--wheel",), timeout=600):
    """Run `python -m build` on `tree`; returns (completed process, list of artifacts under `out`)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPYCACHEPREFIX"}
    out = pathlib.Path(out)
    try:
        proc = subprocess.run([sys.executable, "-m", "build", *kinds, "--outdir", str(out), str(tree)],
                              capture_output=True, text=True, cwd=str(out.parent), env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tail = exc.stderr or b""
        if isinstance(tail, bytes):
            tail = tail.decode("utf-8", "replace")
        pytest.fail("build timed out after %d s: %s" % (timeout, tail[-600:]))
    artifacts = sorted(out.glob("*")) if out.is_dir() else []
    return proc, artifacts


def skip_or_fail(proc):
    """Skip when the isolated build could not resolve its build requirement offline, fail on any other error."""
    tail = (proc.stderr or proc.stdout)[-600:]
    if NETWORK_ERRORS.search(proc.stderr or ""):
        pytest.skip("isolated build could not resolve its build requirement: %s" % tail)
    pytest.fail("build failed: %s" % tail)
