"""
repo_prep.py — Per-task repository preparation and patch capture for
SWE-bench Verified tasks.

Mirrors are plain bare clones kept in an operator-run cache. Each task gets a
fresh `git init` repository that fetches exactly one pinned ref from the
mirror over the local file transport, so later upstream history, tags and the
upstream fix are never reachable from the clone the agent works in.

All git calls run under a minimal environment (`hermetic_git_env`) so the
operator's git config, templates, hooks and credential helpers never apply.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

REPO_CACHE_ENV = "QUOIN_BENCH_REPO_CACHE"
DEFAULT_REPO_CACHE = "~/.cache/quoin-bench/repos"
REPO_URL_TEMPLATE_ENV = "QUOIN_BENCH_REPO_URL_TEMPLATE"
DEFAULT_REPO_URL_TEMPLATE = "https://github.com/{repo}.git"
PIN_REF_PREFIX = "refs/quoin-bench/"
CLONE_PREFIX = "qbench_verified_"

BENCH_GIT_IDENTITY = {"name": "quoin-bench", "email": "quoin-bench@localhost.invalid"}

PATCH_EXCLUDES = (".workflow_artifacts", ".claude", "CLAUDE.md", "AGENTS.md")

_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")

_BASE_ENV_KEYS = ("PATH", "TMPDIR", "LANG", "LC_ALL")
_NETWORK_ENV_KEYS = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
)

_HOME_LOCK = threading.Lock()
_EMPTY_HOME: Optional[str] = None


class RepoPrepError(RuntimeError):
    """Repository preparation or patch capture failed; `reason` is a short kebab string."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _empty_home() -> str:
    global _EMPTY_HOME
    with _HOME_LOCK:
        if _EMPTY_HOME is None or not os.path.isdir(_EMPTY_HOME):
            _EMPTY_HOME = tempfile.mkdtemp(prefix="qbench_home_")
            atexit.register(shutil.rmtree, _EMPTY_HOME, True)
        return _EMPTY_HOME


def hermetic_git_env(base: Optional[dict] = None, *, network: bool = False) -> dict:
    """Return a minimal environment for git; operator git variables never pass through."""
    src = os.environ if base is None else base
    env = {k: src[k] for k in _BASE_ENV_KEYS if k in src}
    if network:
        env.update({k: src[k] for k in _NETWORK_ENV_KEYS if k in src})
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ADVICE"] = "0"
    env["HOME"] = _empty_home()
    return env


def _git(
    args: list,
    cwd: Optional[Path] = None,
    *,
    env: Optional[dict] = None,
    check: bool = True,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """Run git with a hermetic env; stdout and stderr are bytes."""
    try:
        proc = subprocess.run(
            ["git", *[str(a) for a in args]],
            cwd=str(cwd) if cwd is not None else None,
            env=env if env is not None else hermetic_git_env(),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoPrepError("git-timeout", f"git {' '.join(map(str, args[:2]))}") from exc
    if check and proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-400:]
        raise RepoPrepError("git-failed", f"git {' '.join(map(str, args[:2]))}: {tail}")
    return proc


def _text(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def validate_repo_name(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO_NAME.match(repo) or ".." in repo:
        raise RepoPrepError("bad-repo-name", repr(repo))
    return repo


def validate_instance_id(iid: str) -> str:
    if not isinstance(iid, str) or not _INSTANCE_ID.match(iid):
        raise RepoPrepError("bad-instance-id", repr(iid))
    return iid


def _validate_commit(sha: str) -> str:
    if not isinstance(sha, str) or not _SHA40.match(sha):
        raise RepoPrepError("bad-base-commit", repr(sha))
    return sha


def _resolve_cache_root(cache_root) -> Path:
    if cache_root is not None:
        return Path(cache_root)
    return Path(os.environ.get(REPO_CACHE_ENV) or DEFAULT_REPO_CACHE).expanduser()


def _resolve_url_template(url_template) -> str:
    return url_template or os.environ.get(REPO_URL_TEMPLATE_ENV) or DEFAULT_REPO_URL_TEMPLATE


def mirror_path(cache_root, repo: str) -> Path:
    return Path(cache_root) / (repo.replace("/", "__") + ".git")


def _is_verified(task: dict) -> bool:
    return task.get("source") == "swebench_verified"


def prepare_mirrors(
    tasks: list,
    cache_root=None,
    url_template: Optional[str] = None,
    log: Callable = print,
) -> list:
    """Clone or update the bare mirrors and pin one ref per Verified task (network step)."""
    root = _resolve_cache_root(cache_root)
    template = _resolve_url_template(url_template)
    net_env = hermetic_git_env(network=True)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for task in tasks:
        if not _is_verified(task):
            log(f"skip {task.get('id')}: not a Verified task")
            continue
        repo = validate_repo_name(task["repo"])
        iid = validate_instance_id(task["source_id"])
        base = _validate_commit(task["base_commit"])
        url = template.format(repo=repo)
        mirror = mirror_path(root, repo)
        row = {
            "task_id": task["id"], "repo": repo, "instance_id": iid,
            "base_commit": base, "pin_ref_ok": False,
        }
        if not mirror.is_dir():
            tmp = mirror.with_name(mirror.name + f".partial-{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            log(f"cloning {url} -> {mirror}")
            _git(["clone", "--bare", "--no-tags", url, tmp], env=net_env, timeout=3600)
            os.replace(tmp, mirror)
        have = _git(["cat-file", "-e", f"{base}^{{commit}}"], mirror, check=False)
        if have.returncode != 0:
            log(f"fetching {base[:12]} for {iid}")
            _git(["fetch", "--no-tags", url, base], mirror, env=net_env, timeout=3600)
            have = _git(["cat-file", "-e", f"{base}^{{commit}}"], mirror, check=False)
        if have.returncode == 0:
            _git(["update-ref", PIN_REF_PREFIX + iid, base], mirror)
            row["pin_ref_ok"] = True
        rows.append(row)
    return rows


def check_pin_refs(tasks: list, cache_root=None) -> list:
    """Offline check that every Verified task has its pin ref at the expected commit."""
    root = _resolve_cache_root(cache_root)
    problems = []
    for task in tasks:
        if not _is_verified(task):
            continue
        try:
            repo = validate_repo_name(task["repo"])
            iid = validate_instance_id(task["source_id"])
            base = _validate_commit(task["base_commit"])
        except RepoPrepError as exc:
            problems.append(f"{task.get('id')}: {exc.reason}")
            continue
        mirror = mirror_path(root, repo)
        if not mirror.is_dir():
            problems.append(f"{task['id']}: mirror missing for {repo}")
            continue
        proc = _git(["rev-parse", "--verify", f"{PIN_REF_PREFIX}{iid}^{{commit}}"], mirror, check=False)
        if proc.returncode != 0:
            problems.append(f"{task['id']}: pin ref missing for {iid}")
        elif _text(proc) != base:
            problems.append(f"{task['id']}: pin ref for {iid} points at {_text(proc)[:12]}, expected {base[:12]}")
    return problems


@dataclass
class PreparedRepo:
    path: Path
    base_commit: str
    tree_sha: str
    refs: list = field(default_factory=list)
    instance_id: str = ""
    git_config_bytes: bytes = b""
    git_info_bytes: dict = field(default_factory=dict)


_INFO_FILES = ("exclude", "attributes")


def _read_optional(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _prepare_into(path: Path, mirror: Path, iid: str, base: str) -> PreparedRepo:
    _git(["init", "--template=", "-q"], path)
    fetch = _git(["fetch", "--no-tags", "-q", mirror, PIN_REF_PREFIX + iid], path, check=False)
    if fetch.returncode != 0:
        raise RepoPrepError("missing-pin-ref", iid)
    _git(["checkout", "-q", "-B", "main", "FETCH_HEAD"], path)
    _git(["config", "user.name", BENCH_GIT_IDENTITY["name"]], path)
    _git(["config", "user.email", BENCH_GIT_IDENTITY["email"]], path)
    try:
        (path / ".git" / "FETCH_HEAD").unlink()
    except FileNotFoundError:
        pass
    _git(["reflog", "expire", "--expire=now", "--all"], path)

    if _text(_git(["rev-parse", "HEAD"], path)) != base:
        raise RepoPrepError("head-mismatch", iid)
    refs = [r for r in _text(_git(["for-each-ref", "--format=%(refname)"], path)).splitlines() if r]
    if refs != ["refs/heads/main"]:
        raise RepoPrepError("extra-refs", ",".join(refs))
    if _text(_git(["tag"], path)):
        raise RepoPrepError("tags-present", iid)
    if _text(_git(["remote"], path)):
        raise RepoPrepError("remote-present", iid)
    if _text(_git(["rev-list", "--all", f"^{base}"], path)):
        raise RepoPrepError("later-history", iid)
    if _text(_git(["status", "--porcelain"], path)):
        raise RepoPrepError("dirty-tree", iid)
    hooks = path / ".git" / "hooks"
    if hooks.is_dir():
        for entry in hooks.rglob("*"):
            if entry.is_file() and entry.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                raise RepoPrepError("hook-present", entry.name)
    tree_sha = _text(_git(["rev-parse", "HEAD^{tree}"], path))
    info = path / ".git" / "info"
    return PreparedRepo(
        path=path,
        base_commit=base,
        tree_sha=tree_sha,
        refs=refs,
        instance_id=iid,
        git_config_bytes=(path / ".git" / "config").read_bytes(),
        git_info_bytes={name: _read_optional(info / name) for name in _INFO_FILES},
    )


def prepare_task_repo(task_spec: dict, cache_root=None, work_root=None) -> PreparedRepo:
    """Create a fresh clone at the task's base commit with no other history reachable."""
    repo = validate_repo_name(task_spec.get("repo"))
    iid = validate_instance_id(task_spec.get("source_id"))
    base = _validate_commit(task_spec.get("base_commit"))
    mirror = mirror_path(_resolve_cache_root(cache_root), repo)
    if not mirror.is_dir():
        raise RepoPrepError("missing-pin-ref", f"mirror absent for {repo}")
    path = Path(tempfile.mkdtemp(prefix=CLONE_PREFIX, dir=str(work_root) if work_root else None))
    try:
        return _prepare_into(path, mirror, iid, base)
    except BaseException:
        cleanup_task_repo(path)
        raise


def cleanup_task_repo(path) -> Optional[str]:
    """Remove a clone created by `prepare_task_repo`; returns None on success, else a reason."""
    try:
        path = Path(path)
        if not path.name.startswith(CLONE_PREFIX):
            return "refused-path"
        if not path.exists():
            return None
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                full = os.path.join(root, name)
                try:
                    if not os.path.islink(full):
                        os.chmod(full, os.stat(full).st_mode | stat.S_IRWXU)
                except OSError:
                    pass
        try:
            os.chmod(path, os.stat(path).st_mode | stat.S_IRWXU)
        except OSError:
            pass
        shutil.rmtree(path)
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"[:200]
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"[:200]


@dataclass
class CapturedPatch:
    patch_bytes: bytes
    text: str
    sha256: str
    byte_count: int
    repo_config_modified: bool
    repo_info_modified: bool
    excluded_paths: list
    head_commit: str
    decode_lossy: bool


_CAPTURE_OVERRIDES = [
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.untrackedCache=false",
    "-c", "core.attributesFile=/dev/null",
]


MAX_PATCH_BYTES = 10 * 1024 * 1024

# Entries inside .git that redirect git to another directory or layer extra
# configuration, objects or attributes on top of the pinned ones.
_GIT_REDIRECT_FILES = ("commondir", "gitdir", "config.worktree", "grafts", "shallow")
_GIT_REDIRECT_DIRS = ("worktrees",)


def _neutralize_git_dir(git_dir: Path) -> bool:
    """Make `git_dir` a self-contained real directory; return True if anything was removed.

    The agent controls the working tree and everything under `.git`, so a
    redirect such as `commondir` could make the harness's own `git add -A`
    read a config that defines a clean filter. A symlinked or missing `.git`
    cannot be made safe and is refused.
    """
    try:
        st = os.lstat(git_dir)
    except OSError as exc:
        raise RepoPrepError("git-dir-invalid", str(exc)[:200]) from exc
    if not stat.S_ISDIR(st.st_mode):
        raise RepoPrepError("git-dir-invalid", ".git is not a real directory")
    changed = False
    for name in _GIT_REDIRECT_FILES:
        target = git_dir / name
        if os.path.lexists(target):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
            changed = True
    for name in _GIT_REDIRECT_DIRS:
        target = git_dir / name
        if os.path.lexists(target):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
            changed = True
    alternates = git_dir / "objects" / "info" / "alternates"
    if os.path.lexists(alternates):
        alternates.unlink()
        changed = True
    return changed


def _capture_env(path: Path, git_dir: Path) -> dict:
    env = hermetic_git_env()
    env["GIT_DIR"] = str(git_dir)
    env["GIT_COMMON_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(path)
    env["GIT_CEILING_DIRECTORIES"] = str(path.parent)
    env["GIT_ATTR_NOSYSTEM"] = "1"
    return env


def capture_patch(repo, base_commit: str) -> CapturedPatch:
    """Capture everything the agent changed relative to `base_commit` as patch bytes."""
    import hashlib

    prepared = repo if isinstance(repo, PreparedRepo) else None
    path = Path(prepared.path if prepared else repo)
    git_dir = path / ".git"
    config_modified = False
    info_modified = False
    try:
        if _neutralize_git_dir(git_dir):
            config_modified = True
        if prepared is not None:
            if _read_optional(git_dir / "config") != prepared.git_config_bytes:
                config_modified = True
                (git_dir / "config").write_bytes(prepared.git_config_bytes)
            info_dir = git_dir / "info"
            for name in _INFO_FILES:
                wanted = prepared.git_info_bytes.get(name)
                if _read_optional(info_dir / name) != wanted:
                    info_modified = True
                    if wanted is None:
                        (info_dir / name).unlink()
                    else:
                        (info_dir / name).write_bytes(wanted)
        env = _capture_env(path, git_dir)
        _git([*_CAPTURE_OVERRIDES, "add", "-A"], path, env=env)
        pathspecs = [f":(top,exclude){p}" for p in PATCH_EXCLUDES]
        diff = _git(
            [*_CAPTURE_OVERRIDES, "diff", "--cached", "--binary", "--no-color",
             "--no-ext-diff", "--no-textconv", base_commit, "--", ".", *pathspecs],
            path, env=env,
        )
        head = _git(["rev-parse", "HEAD"], path, env=env, check=False)
    except (RepoPrepError, OSError) as exc:
        raise RepoPrepError("patch-capture-failed", str(exc)[:300]) from exc
    data = diff.stdout
    if len(data) > MAX_PATCH_BYTES:
        raise RepoPrepError("patch-too-large", f"{len(data)} bytes exceeds {MAX_PATCH_BYTES}")
    try:
        text = data.decode("utf-8")
        lossy = False
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        lossy = True
    return CapturedPatch(
        patch_bytes=data,
        text=text,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        repo_config_modified=config_modified,
        repo_info_modified=info_modified,
        excluded_paths=list(PATCH_EXCLUDES),
        head_commit=_text(head) if head.returncode == 0 else "",
        decode_lossy=lossy,
    )
