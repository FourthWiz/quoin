"""Shared offline stand-in for an upstream repository used by the Verified-path tests."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

INSTANCE_ID = "org__repo-1"
REPO = "org/repo"

_CLEAN = {
    "PATH": os.environ.get("PATH", ""),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(args, cwd, env_extra=None) -> str:
    env = dict(_CLEAN)
    env.update(env_extra or {})
    out = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, check=True)
    return out.stdout.decode("utf-8", "replace").strip()


def build_upstream(tmp_path: Path, extra_base_files: Optional[dict] = None) -> dict:
    """Upstream with commit A (the base), a later commit B on main, and tag v1 on B.

    `extra_base_files` maps relative paths to bytes committed in A as well.
    """
    root = tmp_path / "upstream"
    repo = root / "org" / "repo"
    repo.mkdir(parents=True)
    git(["init", "-q", "-b", "main"], repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    (repo / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    for rel, data in (extra_base_files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "A"], repo)
    base = git(["rev-parse", "HEAD"], repo)
    (repo / "a.txt").write_text("base\nlater upstream fix\n", encoding="utf-8")
    git(["commit", "-q", "-am", "B"], repo)
    later = git(["rev-parse", "HEAD"], repo)
    git(["tag", "v1"], repo)
    task = {
        "id": "verified_easy_000", "tier": "easy", "source": "swebench_verified",
        "source_id": INSTANCE_ID, "repo": REPO, "base_commit": base,
        "description": "Fix the thing.\nIt is broken.\n",
    }
    return {"root": root, "url_template": str(root) + "/{repo}", "base": base,
            "later": later, "task": task}


def configure_env(monkeypatch, tmp_path: Path, standin: dict) -> Path:
    cache = tmp_path / "mirrors"
    monkeypatch.setenv("QUOIN_BENCH_REPO_CACHE", str(cache))
    monkeypatch.setenv("QUOIN_BENCH_REPO_URL_TEMPLATE", standin["url_template"])
    return cache
