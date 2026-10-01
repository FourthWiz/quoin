"""Fixtures shared by the evidence and gate tests (not collected)."""
from __future__ import annotations

import functools
import os
import subprocess
import time
from pathlib import Path

from quoin.opencode_adapter import driver, runstore

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def isolate_git(monkeypatch, home) -> None:
    monkeypatch.setenv("HOME", str(home))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def git(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode()


def make_repo(path, files=None) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for name, text in (files or {"src/x.py": "x = 1\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def clock_at(value=None):
    base = time.time() if value is None else value
    return lambda: base


def patch_runstore(monkeypatch, **kwargs) -> None:
    """Make `repo_revisions` and `hash_inputs` default to the given limits."""
    for name in ("repo_revisions", "hash_inputs"):
        wanted = {k: v for k, v in kwargs.items() if k in _PARAMS[name]}
        if wanted:
            monkeypatch.setattr(runstore, name, functools.partial(getattr(runstore, name), **wanted))


_PARAMS = {
    "repo_revisions": {"max_source_bytes", "max_untracked_files", "budget_s"},
    "hash_inputs": {"max_files", "max_file_bytes", "max_total_bytes"},
}
