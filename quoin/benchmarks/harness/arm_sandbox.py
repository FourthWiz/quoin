"""Sandbox homes, base settings, quoin install and session environments for paired runs.

Each arm gets its own throwaway HOME with its own config directory, so the
plain arm cannot see the operator's skills, rules or hooks and the quoin arm
sees exactly what the installer put there. The session environment handed to
the CLI is built from an allowlist; nothing else is inherited.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .repo_prep import BENCH_GIT_IDENTITY

SANDBOX_PREFIX = "qbench_sandbox_"

SANDBOX_BASE_PERMISSIONS = {
    "allow": ["Read", "Edit", "Write", "Glob", "Grep", "Bash"],
    "deny": [
        "WebFetch",
        "WebSearch",
        "Bash(curl:*)",
        "Bash(wget:*)",
        "Bash(pip:*)",
        "Bash(pip3:*)",
        "Bash(claude:*)",
    ],
}

AUTH_ENV_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

PASSTHROUGH_KEYS = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
)

_IDENTITY_DIRS = ("skills", "commands", "agents", "hooks")
_IDENTITY_SETTINGS_KEYS = ("hooks", "enabledPlugins", "permissions")


@dataclass
class Sandbox:
    kind: str
    home: Path
    claude_dir: Path


def assert_private_dir(path: Path) -> None:
    """Raise unless `path` is a plain directory owned by this user with no group or other access."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"sandbox path is not a plain directory: {path}")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"sandbox path is not owned by this user: {path}")
    if st.st_mode & 0o077:
        raise RuntimeError(f"sandbox path is accessible to others: {path}")


def write_base_settings(claude_dir: Path) -> None:
    (claude_dir / "settings.json").write_text(
        json.dumps({"permissions": SANDBOX_BASE_PERMISSIONS}, indent=2) + "\n"
    )


def create_sandbox(kind: str, parent: Optional[Path] = None) -> Sandbox:
    if parent is not None and os.path.islink(parent):
        raise RuntimeError(f"sandbox parent is a symlink: {parent}")
    path = Path(tempfile.mkdtemp(prefix=SANDBOX_PREFIX + kind + "_", dir=parent))
    try:
        assert_private_dir(path)
        home = Path(os.path.realpath(path))
        claude_dir = home / ".claude"
        claude_dir.mkdir(mode=0o700)
        write_base_settings(claude_dir)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return Sandbox(kind=kind, home=home, claude_dir=claude_dir)


def check_arm_tree(arm_root: Path, expected_commit: str) -> List[str]:
    """Problems with the arm checkout: wrong HEAD or uncommitted changes under quoin/ and src/."""
    from ..scripts import run_three_arm_gate as gate

    problems: List[str] = []
    head = gate.worktree_head(arm_root)
    if head != expected_commit:
        problems.append(f"head-mismatch: {head} != {expected_commit}")
    status = subprocess.run(
        ["git", "-C", str(arm_root), "status", "--porcelain", "--", "quoin", "src"],
        capture_output=True, text=True, timeout=60,
    )
    paths = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    if status.returncode != 0:
        problems.append("dirty-tree: git status failed")
    elif paths:
        problems.append("dirty-tree: " + ", ".join(paths[:5]))
    return problems


def _tail(text: str, limit: int = 2000) -> str:
    return (text or "")[-limit:]


def install_quoin(sandbox: Sandbox, arm_root: Path, expected_commit: str, *,
                  python: str = sys.executable, run=subprocess.run) -> dict:
    """Install the arm's quoin into the sandbox home; refuse unless the tree is exactly `expected_commit`."""
    problems = check_arm_tree(arm_root, expected_commit)
    if problems:
        return {"ok": False, "reason": "arm-tree: " + "; ".join(problems), "returncode": None,
                "stdout_tail": "", "stderr_tail": "", "installed_commit": None}
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env["HOME"] = str(sandbox.home)
    env["PYTHONPATH"] = str(Path(arm_root) / "src")
    argv = [python, "-m", "quoin", "install", "--source-dir", str(Path(arm_root) / "quoin"),
            "--scope", "user"]
    try:
        proc = run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "install-timeout", "returncode": None,
                "stdout_tail": "", "stderr_tail": "", "installed_commit": None}
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "reason": None if ok else f"install-exit-{proc.returncode}",
        "returncode": proc.returncode,
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
        "installed_commit": expected_commit if ok else None,
    }


def select_auth(environ) -> Tuple[str, str]:
    """The single auth variable set in `environ`, as (name, value)."""
    present = [key for key in AUTH_ENV_KEYS if environ.get(key)]
    if not present:
        raise ValueError("no auth variable: set " + " or ".join(AUTH_ENV_KEYS))
    if len(present) > 1:
        raise ValueError("both auth variables set; unset one")
    return present[0], environ[present[0]]


def build_session_env(sandbox_or_none: Optional[Sandbox], cell: str, run_id: str,
                      auth: Tuple[str, str], environ=None) -> dict:
    """The complete environment for one CLI session: allowlisted names only."""
    environ = os.environ if environ is None else environ
    env = {key: environ[key] for key in PASSTHROUGH_KEYS if key in environ}
    env[auth[0]] = auth[1]
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = BENCH_GIT_IDENTITY["name"]
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = BENCH_GIT_IDENTITY["email"]
    if sandbox_or_none is not None:
        env["HOME"] = str(sandbox_or_none.home)
        env["CLAUDE_CONFIG_DIR"] = str(sandbox_or_none.claude_dir)
    else:
        env["HOME"] = str(Path.home())
    if cell == "quoin-claude":
        env["QUOIN_GATE_AUTO_APPROVE"] = "1"
        env["QUOIN_BENCHMARK_RUN"] = run_id
    return env


def session_env_keys(env: dict) -> List[str]:
    return sorted(env)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_fingerprint(claude_dir: Path) -> str:
    """Hash of the identity-bearing parts of a config directory.

    CLI-maintained state (plugin caches, projects, history, `.claude.json`) is
    deliberately not read: a session writes it, so it would change without the
    arm's identity changing.
    """
    claude_dir = Path(claude_dir)
    digest = hashlib.sha256()
    entries = []
    for name in _IDENTITY_DIRS:
        root = claude_dir / name
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    entries.append((str(path.relative_to(claude_dir)), path.stat().st_size,
                                    _file_digest(path)))
    rules = claude_dir / "CLAUDE.md"
    if rules.is_file():
        entries.append(("CLAUDE.md", rules.stat().st_size, _file_digest(rules)))
    for rel, size, sha in entries:
        digest.update(f"{rel}\0{size}\0{sha}\n".encode())
    settings = {}
    try:
        loaded = json.loads((claude_dir / "settings.json").read_text())
        settings = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    picked = {key: settings[key] for key in _IDENTITY_SETTINGS_KEYS if key in settings}
    digest.update(json.dumps(picked, sort_keys=True).encode())
    return digest.hexdigest()


def installed_plugin_names(claude_dir: Path) -> List[str]:
    path = Path(claude_dir) / "plugins" / "installed_plugins.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        plugins = data.get("plugins") if isinstance(data, dict) else None
        if plugins is None:
            return []
        return sorted(plugins)
    except (OSError, ValueError, TypeError):
        return ["<unparsable>"]


def remove_sandbox(sandbox: Sandbox) -> Optional[str]:
    """Delete a sandbox this module created; returns a message on failure, never raises."""
    try:
        home = Path(sandbox.home)
        if not home.name.startswith(SANDBOX_PREFIX):
            return f"refused to remove a path without the sandbox prefix: {home}"
        for root, dirs, files in os.walk(home):
            for name in dirs + files:
                try:
                    os.chmod(os.path.join(root, name), 0o700, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
        os.chmod(home, 0o700)
        shutil.rmtree(home)
        return None
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the run's own outcome
        return f"sandbox removal failed: {exc}"
