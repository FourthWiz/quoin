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
import functools
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

    def __init__(self, reason: str, detail: str = "", inner_reason: str = ""):
        self.reason = reason
        self.detail = detail
        self.inner_reason = inner_reason
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


def check_heavy_dirs(tasks: list, cache_root=None) -> list:
    """Offline check that no Verified task's base tree tracks a path under a heavy directory.

    Patch capture skips these directories at any depth, so a tracked file under
    one would silently drop out of a patch that edits it.
    """
    root = _resolve_cache_root(cache_root)
    problems = []
    for task in tasks:
        if not _is_verified(task):
            continue
        try:
            repo = validate_repo_name(task["repo"])
            base = _validate_commit(task["base_commit"])
        except RepoPrepError as exc:
            problems.append(f"{task.get('id')}: {exc.reason}")
            continue
        mirror = mirror_path(root, repo)
        if not mirror.is_dir():
            problems.append(f"{task['id']}: mirror missing for {repo}")
            continue
        proc = _git(["ls-tree", "-r", "--name-only", "-z", base], mirror, check=False)
        if proc.returncode != 0:
            problems.append(f"{task['id']}: base-missing")
            continue
        for raw in proc.stdout.split(b"\0"):
            parts = raw.decode("utf-8", "replace").split("/")
            if any(part in HEAVY_DIR_NAMES for part in parts[:-1]):
                problems.append(f"{task['id']}: heavy-dir-tracked {'/'.join(parts)}")
                break
    return problems


def pin_ref_status_offline(task: dict, cache_root=None) -> Optional[str]:
    """Commit the task's pin ref points at, read straight from the bare mirror's files.

    Looks at the loose ref file first, then packed-refs. Runs no git process.
    Returns None when the mirror or the ref is missing.
    """
    try:
        repo = validate_repo_name(task["repo"])
        iid = validate_instance_id(task["source_id"])
    except (RepoPrepError, KeyError):
        return None
    mirror = mirror_path(_resolve_cache_root(cache_root), repo)
    ref = PIN_REF_PREFIX + iid
    try:
        loose = (mirror / ref).read_text(encoding="utf-8").strip()
        if _SHA40.match(loose):
            return loose
    except OSError:
        pass
    try:
        for line in (mirror / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "^")):
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref and _SHA40.match(sha):
                return sha
    except OSError:
        pass
    return None


@dataclass
class PreparedRepo:
    path: Path
    base_commit: str
    tree_sha: str
    refs: list = field(default_factory=list)
    instance_id: str = ""
    git_config_bytes: bytes = b""
    git_info_bytes: dict = field(default_factory=dict)
    mirror: Optional[Path] = None


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
        mirror=mirror,
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
    symlinks_recorded: int = 0
    special_files_skipped: int = 0
    rejected_names: int = 0
    oversize_files: list = field(default_factory=list)
    skipped_heavy_dirs: int = 0
    attr_source_supported: bool = True


# Patch capture never runs git against anything the agent controls. The agent
# owns the clone's working tree and everything under its `.git` (config,
# index, refs, objects, nested repositories), so capture first copies the
# working tree into a harness-owned snapshot, dropping every `.git` entry at
# any depth, and then diffs that snapshot with a git dir and index the harness
# creates fresh outside the clone. Base objects come from the harness's own
# mirror through an alternates file. Symlinks are recorded as symlinks (their
# target text, mode 120000) and are never followed, so nothing outside the
# clone is read.

MAX_PATCH_BYTES = 10 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024 * 1024
MAX_SNAPSHOT_ENTRIES = 500_000
MAX_SNAPSHOT_DEPTH = 128
MAX_SNAPSHOT_FILE_BYTES = 100 * 1024 * 1024
MAX_LISTED_OVERSIZE = 20
# Directories that hold installed dependencies or tool caches. They are
# skipped at any depth: they are git-ignored in practice, can hold millions of
# entries, and are not part of what the agent changed.
HEAVY_DIR_NAMES = (
    ".venv", "venv", ".tox", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".eggs",
)
_MIN_ATTR_SOURCE_GIT = (2, 42)
_NTFS_SHORT_GIT = re.compile(r"^git~[0-9]+$")
_READ_CHUNK = 1024 * 1024
_SMALL_READ_LIMIT = 1024 * 1024
_CAPTURE_PREFIX = "qbench_capture_"

_CAPTURE_OVERRIDES = [
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.untrackedCache=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.excludesFile=/dev/null",
    "-c", "core.autocrlf=false",
    "-c", "core.safecrlf=false",
]

# Capture reads `.gitattributes` from the base commit (GIT_ATTR_SOURCE), not
# from the snapshot, so `add` undoes exactly the line-ending, ident and
# encoding conversion the clone's checkout applied, and the agent's own
# `.gitattributes` edits cannot change how files are hashed. On top of that,
# `$GIT_DIR/info/attributes` outranks every tree attribute and switches off
# the drivers that would run a command or change the diff output.
_NEUTRAL_ATTRIBUTES = b"* !filter !diff !merge\n"

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_REF_NAME = re.compile(r"^refs/heads/[A-Za-z0-9._/-]+$")


@dataclass
class _SnapshotCounts:
    bytes: int = 0
    entries: int = 0
    symlinks: int = 0
    special: int = 0
    rejected_names: int = 0
    skipped_heavy_dirs: int = 0
    oversize_total: int = 0
    oversize_files: list = field(default_factory=list)

    def note_oversize(self, rel: str) -> None:
        self.oversize_total += 1
        if len(self.oversize_files) < MAX_LISTED_OVERSIZE:
            self.oversize_files.append(rel)


def _git_rejected_name(name: str) -> bool:
    """Names `git add` refuses because they alias the `.git` directory on some filesystems."""
    lowered = name.lower()
    if lowered == ".git":
        return False
    return lowered.rstrip(" .") == ".git" or bool(_NTFS_SHORT_GIT.match(lowered))


@functools.lru_cache(maxsize=1)
def git_supports_attr_source() -> bool:
    """Whether the installed git honours GIT_ATTR_SOURCE (git 2.42 or newer)."""
    try:
        proc = subprocess.run(
            ["git", "version"], capture_output=True, text=True, timeout=30,
            env=hermetic_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and _parse_attr_source_support(proc.stdout)


def _parse_attr_source_support(version_output: str) -> bool:
    match = re.search(r"(\d+)\.(\d+)", version_output or "")
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= _MIN_ATTR_SOURCE_GIT


def _copy_regular(dir_fd: int, name: str, target: Path, counts: _SnapshotCounts, rel: str) -> None:
    try:
        src = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        counts.special += 1
        return
    try:
        st = os.fstat(src)
        if not stat.S_ISREG(st.st_mode):
            counts.special += 1
            return
        if st.st_size > MAX_SNAPSHOT_FILE_BYTES:
            counts.note_oversize(rel)
            return
        mode = 0o755 if st.st_mode & stat.S_IXUSR else 0o644
        dst = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, mode)
        file_bytes = 0
        try:
            os.fchmod(dst, mode)
            while True:
                chunk = os.read(src, _READ_CHUNK)
                if not chunk:
                    break
                file_bytes += len(chunk)
                if file_bytes > MAX_SNAPSHOT_FILE_BYTES:
                    counts.bytes -= file_bytes - len(chunk)
                    counts.note_oversize(rel)
                    os.close(dst)
                    dst = -1
                    os.unlink(target)
                    return
                counts.bytes += len(chunk)
                if counts.bytes > MAX_SNAPSHOT_BYTES:
                    raise RepoPrepError("snapshot-too-large", f"working tree exceeds {MAX_SNAPSHOT_BYTES} bytes")
                os.write(dst, chunk)
        finally:
            if dst >= 0:
                os.close(dst)
    finally:
        os.close(src)


def _snapshot_dir(
    dir_fd: int, out_dir: Path, depth: int, counts: _SnapshotCounts, rel: str = "",
) -> None:
    if depth > MAX_SNAPSHOT_DEPTH:
        raise RepoPrepError("snapshot-too-deep", f"directory depth exceeds {MAX_SNAPSHOT_DEPTH}")
    with os.scandir(dir_fd) as it:
        names = sorted(entry.name for entry in it)
    for name in names:
        if name.lower() == ".git" or (depth == 0 and name in PATCH_EXCLUDES):
            continue
        if _git_rejected_name(name):
            counts.rejected_names += 1
            continue
        child_rel = f"{rel}{name}"
        counts.entries += 1
        if counts.entries > MAX_SNAPSHOT_ENTRIES:
            raise RepoPrepError("snapshot-too-large", f"more than {MAX_SNAPSHOT_ENTRIES} entries")
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        target = out_dir / name
        if stat.S_ISLNK(st.st_mode):
            try:
                link_target = os.readlink(name, dir_fd=dir_fd)
            except OSError:
                counts.special += 1
                continue
            os.symlink(link_target, target)
            counts.symlinks += 1
        elif stat.S_ISDIR(st.st_mode):
            if name in HEAVY_DIR_NAMES:
                counts.skipped_heavy_dirs += 1
                continue
            try:
                child = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=dir_fd)
            except OSError:
                counts.special += 1
                continue
            try:
                target.mkdir()
                _snapshot_dir(child, target, depth + 1, counts, child_rel + "/")
            finally:
                os.close(child)
        elif stat.S_ISREG(st.st_mode):
            _copy_regular(dir_fd, name, target, counts, child_rel)
        else:
            counts.special += 1


def _snapshot_worktree(src: Path, dst: Path) -> _SnapshotCounts:
    """Copy the agent's working tree into `dst` without following any symlink.

    Directories and files are opened relative to their parent's descriptor
    with O_NOFOLLOW, so a path swapped for a symlink mid-walk is not followed.
    Sockets, FIFOs and devices are skipped. The byte cap is enforced while
    reading, not from `st_size`.
    """
    counts = _SnapshotCounts()
    root = os.open(src, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        _snapshot_dir(root, dst, 0, counts)
    finally:
        os.close(root)
    return counts


def _read_small_nofollow(path: Path, limit: int = _SMALL_READ_LIMIT) -> Optional[bytes]:
    """Read a small regular file without following a final symlink; None if unusable."""
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, limit + 1)
        return None if len(data) > limit else data
    except OSError:
        return None
    finally:
        os.close(fd)


def _agent_git_dir(path: Path) -> Optional[Path]:
    git_dir = path / ".git"
    try:
        if stat.S_ISDIR(os.lstat(git_dir).st_mode):
            return git_dir
    except OSError:
        pass
    return None


def _agent_head(path: Path) -> str:
    """Best-effort, informational HEAD of the agent's clone, read as plain files."""
    git_dir = _agent_git_dir(path)
    if git_dir is None:
        return ""
    head = (_read_small_nofollow(git_dir / "HEAD", 4096) or b"").decode("ascii", "replace").strip()
    if _SHA40.match(head):
        return head
    if not head.startswith("ref: "):
        return ""
    ref = head[5:].strip()
    if not _REF_NAME.match(ref) or ".." in ref:
        return ""
    loose = (_read_small_nofollow(git_dir / ref, 4096) or b"").decode("ascii", "replace").strip()
    if _SHA40.match(loose):
        return loose
    packed = (_read_small_nofollow(git_dir / "packed-refs") or b"").decode("ascii", "replace")
    for line in packed.splitlines():
        parts = line.split(" ")
        if len(parts) == 2 and parts[1] == ref and _SHA40.match(parts[0]):
            return parts[0]
    return ""


def _agent_git_state_modified(path: Path, prepared: Optional[PreparedRepo]) -> tuple:
    """Report whether the agent changed its clone's config or info files (never written back)."""
    if prepared is None:
        return False, False
    git_dir = _agent_git_dir(path)
    if git_dir is None:
        return True, True
    config_modified = _read_small_nofollow(git_dir / "config") != prepared.git_config_bytes
    info_modified = any(
        _read_small_nofollow(git_dir / "info" / name) != prepared.git_info_bytes.get(name)
        for name in _INFO_FILES
    )
    return config_modified, info_modified


def _git_capped(args: list, cwd: Path, env: dict, limit: int, timeout: int = 600) -> bytes:
    """Run git and return stdout, killing it as soon as stdout passes `limit` bytes."""
    timed_out = threading.Event()
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            ["git", *[str(a) for a in args]], cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL,
        )

        def _kill():
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.start()
        chunks = []
        total = 0
        over = False
        try:
            while True:
                chunk = proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    over = True
                    proc.kill()
                    break
                chunks.append(chunk)
        finally:
            timer.cancel()
            proc.stdout.close()
            rc = proc.wait()
        if over:
            raise RepoPrepError("patch-too-large", f"diff exceeds {limit} bytes")
        if timed_out.is_set():
            raise RepoPrepError("git-timeout", f"git {' '.join(map(str, args[:2]))}")
        if rc != 0:
            err.seek(0)
            tail = err.read().decode("utf-8", "replace").strip()[-400:]
            raise RepoPrepError("git-failed", f"git {' '.join(map(str, args[:2]))}: {tail}")
    return b"".join(chunks)


def _harness_git_env(scratch: Path, git_dir: Path, worktree: Path) -> dict:
    home = scratch / "home"
    home.mkdir(exist_ok=True)
    env = hermetic_git_env()
    env["HOME"] = str(home)
    env["GIT_DIR"] = str(git_dir)
    env["GIT_COMMON_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(worktree)
    env["GIT_INDEX_FILE"] = str(git_dir / "capture-index")
    env["GIT_CEILING_DIRECTORIES"] = str(scratch)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _init_harness_git_dir(scratch: Path, mirror: Path) -> Path:
    """Create an empty git dir that borrows objects read-only from the harness mirror."""
    repo = scratch / "repo"
    init_env = hermetic_git_env()
    init_env["HOME"] = str(scratch / "home")
    _git(["init", "--template=", "-q", repo], scratch, env=init_env)
    git_dir = repo / ".git"
    (git_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (git_dir / "objects" / "info" / "alternates").write_text(
        str(Path(mirror).resolve() / "objects") + "\n", encoding="utf-8"
    )
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "attributes").write_bytes(_NEUTRAL_ATTRIBUTES)
    return git_dir


_CAPTURE_LIMIT_REASONS = ("patch-too-large", "snapshot-too-large", "snapshot-too-deep")


def capture_patch(repo, base_commit: str) -> CapturedPatch:
    """Capture everything the agent changed relative to `base_commit` as patch bytes.

    `repo` must be the `PreparedRepo` returned by `prepare_task_repo`; its
    mirror supplies the base objects so nothing is read from the clone's
    own git metadata.
    """
    import hashlib

    prepared = repo if isinstance(repo, PreparedRepo) else None
    path = Path(prepared.path if prepared else repo)
    try:
        base = _validate_commit(base_commit)
        if prepared is None or prepared.mirror is None or not Path(prepared.mirror).is_dir():
            raise RepoPrepError("mirror-unavailable", "capture needs the harness mirror for the base objects")
        parent = path.parent if path.parent.is_dir() else None
        scratch = Path(tempfile.mkdtemp(prefix=_CAPTURE_PREFIX, dir=str(parent) if parent else None))
    except RepoPrepError as exc:
        raise RepoPrepError("patch-capture-failed", str(exc)[:300], exc.reason) from exc
    except OSError as exc:
        raise RepoPrepError("patch-capture-failed", str(exc)[:300]) from exc
    try:
        try:
            snapshot = scratch / "tree"
            snapshot.mkdir()
            counts = _snapshot_worktree(path, snapshot)
            git_dir = _init_harness_git_dir(scratch, Path(prepared.mirror))
            env = _harness_git_env(scratch, git_dir, snapshot)
            env["GIT_ATTR_SOURCE"] = base
            have = _git(["cat-file", "-e", f"{base}^{{commit}}"], snapshot, env=env, check=False)
            if have.returncode != 0:
                raise RepoPrepError("base-missing", base[:12])
            _git([*_CAPTURE_OVERRIDES, "read-tree", base], snapshot, env=env)
            _git([*_CAPTURE_OVERRIDES, "add", "-A"], snapshot, env=env)
            pathspecs = [f":(top,exclude){p}" for p in PATCH_EXCLUDES]
            data = _git_capped(
                [*_CAPTURE_OVERRIDES, "diff", "--cached", "--binary", "--no-color",
                 "--no-ext-diff", "--no-textconv", base, "--", ".", *pathspecs],
                snapshot, env, MAX_PATCH_BYTES,
            )
        except RepoPrepError as exc:
            if exc.reason in _CAPTURE_LIMIT_REASONS:
                raise
            raise RepoPrepError("patch-capture-failed", str(exc)[:300], exc.reason) from exc
        except OSError as exc:
            raise RepoPrepError("patch-capture-failed", f"{type(exc).__name__}: {exc}"[:300]) from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    config_modified, info_modified = _agent_git_state_modified(path, prepared)
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
        head_commit=_agent_head(path),
        decode_lossy=lossy,
        symlinks_recorded=counts.symlinks,
        special_files_skipped=counts.special,
        rejected_names=counts.rejected_names,
        oversize_files=list(counts.oversize_files),
        skipped_heavy_dirs=counts.skipped_heavy_dirs,
        attr_source_supported=git_supports_attr_source(),
    )
