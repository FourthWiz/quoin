"""Isolated snapshots for critic and review runs.

A critic or reviewer judges work it must not be able to change, so these runs
start in a read-only copy of the project that lives outside it, under the
adapter state directory. The copy is its own git repository, so OpenCode
discovery and project identity stop at its root, and a relative path inside it
cannot name a project file. Only a finding the run writes into the copy's
outbox leaves it: `harvest` validates the one expected file and moves it into
the real task folder under the next free number.

The snapshot holds the tracked and untracked-not-ignored source of every
repository, the installed OpenCode files, the task folder and the discovery
files. It never holds a `.git` entry, a symlink or a `.env*` file. Reviewers
also get `review-context/`, a diff of the stage's changes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import selectors
import secrets as _secrets
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from . import boundaries, gate, install, jsonio, paths, phase_loop, runstore

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_FILES = 20000
MAX_DIFF_BYTES = 8 * 1024 * 1024
MAX_FINDING_BYTES = 2 * 1024 * 1024
GIT_TIMEOUT_S = 120.0

SNAPSHOT_ROLE = {"critic": "critic-snapshot", "review": "reviewer-snapshot"}
_CREATE_KIND = {"critic": "critic", "reviewer": "review"}
_FINDING_RE = {
    "critic": re.compile(r"critic-response-(\d+)\.md\Z"),
    "review": re.compile(r"review-(\d+)\.md\Z"),
}
_FINDING_PREFIX = {"critic": "critic-response-", "review": "review-"}
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_ARTIFACTS = ".workflow_artifacts"
_AGENT_DIRS = (_ARTIFACTS + "/memory/sessions", _ARTIFACTS + "/memory/daily", _ARTIFACTS + "/cache")
_REVIEW_CONTEXT = "review-context"
_ENV_NAME_RE = re.compile(r"\.env(\..*)?\Z")
_CHUNK = 1 << 20


class SnapshotRefused(Exception):
    """A snapshot could not be made; `code` is a short stable identifier."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    """`root` is the snapshot directory, `outbox_rel` the snapshot-relative
    directory the run writes its finding into, `manifest` maps every copied
    path to its sha256 and `skipped` lists what was left out and why."""

    root: Path
    outbox_rel: str
    manifest: Mapping[str, str] = field(default_factory=dict)
    skipped: Tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True)
class Harvest:
    """What `harvest` did. On success `path_rel` (project-relative, forward
    slashes), `sha256` and `number` describe the new file in the real task
    folder; otherwise `error` is `harvest-none`, `harvest-ambiguous`,
    `harvest-invalid`, `harvest-unreadable`, `harvest-target-unresolved` or
    `harvest-write-failed`."""

    path_rel: Optional[str] = None
    sha256: Optional[str] = None
    number: Optional[int] = None
    ignored_tmp: Tuple[str, ...] = ()
    unexpected: Tuple[str, ...] = ()
    error: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class IsolatedRun:
    """The outcome of `run_in_snapshot`. `result` is None when no run started
    (`refusal` then holds the snapshot refusal code). `harvest` is None unless
    a finding was harvested or attempted; `harvest_skipped` says why it was not."""

    result: Optional[phase_loop.PhaseResult]
    snapshot_root: Optional[Path]
    harvest: Optional[Harvest]
    boundary: Optional[boundaries.BoundaryResult]
    removed: bool
    refusal: Optional[str] = None
    harvest_skipped: Optional[str] = None


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def _git_env(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
    env.update(
        LC_ALL="C", GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
    )
    if extra:
        env.update(extra)
    return env


def _git(
    repo: str, *args: str, env: Optional[Mapping[str, str]] = None, cap: Optional[int] = None,
    configs: Sequence[str] = (),
) -> Tuple[int, bytes, bool]:
    """`(exit code, stdout, truncated)` of a git command in `repo`. Output past
    `cap` bytes stops the command and sets `truncated`. Never raises: a git
    that cannot run reports exit code 127."""
    argv = ["git", "-C", repo, *runstore._SOURCE_GIT_CONFIG]  # noqa: SLF001
    for item in configs:
        argv += ["-c", item]
    argv += ["-c", "core.quotepath=off", *args]
    try:
        proc = subprocess.Popen(
            argv, env=_git_env(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        return 127, b"", False
    chunks: List[bytes] = []
    size = 0
    truncated = False
    deadline = time.monotonic() + GIT_TIMEOUT_S
    selector = selectors.DefaultSelector()
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        selector.register(fd, selectors.EVENT_READ)
        while True:
            # wait no longer than the deadline allows, so a silent git is cut off too
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                truncated = True
                break
            want = _CHUNK if cap is None else min(_CHUNK, cap + 1 - size)
            chunk = os.read(fd, want)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if cap is not None and size > cap:
                truncated = True
                break
    finally:
        selector.close()
        if truncated or proc.poll() is None:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait()
    data = b"".join(chunks)
    if truncated and cap is not None:
        data = data[:cap]
    return (127 if truncated and cap is None else proc.returncode), data, truncated


def _repos(project_root: str) -> List[Tuple[str, str]]:
    """`(relative path, absolute path)` of every repository the project holds."""
    out: List[Tuple[str, str]] = []
    for entry in runstore.repo_revisions(project_root):
        rel = str(entry.get("path") or ".")
        out.append((rel, os.path.normpath(os.path.join(project_root, rel))))
    return out


def _write_tree(repo: str, specs: Sequence[str]) -> Optional[str]:
    """A tree object for the repository's current source (the tracked and
    untracked-not-ignored files inside `specs`), built from a throwaway index
    so the real index is untouched. None when git fails."""
    with tempfile.TemporaryDirectory(prefix="quoin-index-") as scratch:
        env = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        code, _, _ = _git(repo, "rev-parse", "--verify", "-q", "HEAD", env=env)
        step = ("read-tree", "HEAD") if code == 0 else ("read-tree", "--empty")
        if _git(repo, *step, env=env)[0] != 0:
            return None
        if _git(repo, "add", "-A", "--", *specs, env=env)[0] != 0:
            return None
        code, out, _ = _git(repo, "write-tree", env=env)
    text = out.decode("ascii", "replace").strip()
    return text if code == 0 and re.fullmatch(r"[0-9a-f]{40,64}", text) else None


_SECRET_EXCLUDES = (":(exclude,glob)**/.env", ":(exclude,glob)**/.env.*")


def _tree_specs(repo: str, root: str, abs_paths: Sequence[str]) -> List[str]:
    """Pathspecs for a tree or diff of the repository's source: the shared
    source scope without any `.env*` file, so a secret never enters a diff."""
    return runstore.source_pathspecs(repo, root, abs_paths) + list(_SECRET_EXCLUDES)


def _stage_key(stage: Optional[int]) -> str:
    return str(stage) if stage is not None else "0"


def record_base_tree(
    project_root, task: str, stage: Optional[Any], *, clock: Callable[[], float] = time.time,
) -> Dict[str, Optional[str]]:
    """Remember what the stage's source looked like before its implementation
    began, per repository, so a review can diff against it. The first call for
    a stage wins; later calls change nothing. The caller holds the task lock.
    Returns the stage's stored map (a repository whose tree could not be
    written maps to None)."""
    root = os.path.realpath(str(project_root))
    number = runstore.normalize_stage(stage)
    key = _stage_key(number)
    directory = runstore.store_dir(root, create=True)
    state = runstore.load_workflow_state(directory, task) or runstore.new_workflow_state(task, clock)
    existing = state["settings"].get("base_trees")
    if isinstance(existing, dict) and isinstance(existing.get(key), dict):
        return dict(existing[key])
    repos = _repos(root)
    abs_paths = [absolute for _, absolute in repos]
    trees: Dict[str, Optional[str]] = {}
    for rel, absolute in repos:
        trees[rel] = _write_tree(absolute, _tree_specs(absolute, root, abs_paths))
    base = state["settings"].setdefault("base_trees", {})
    if not isinstance(base, dict):
        base = state["settings"]["base_trees"] = {}
    base[key] = trees
    state["updated_at"] = datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runstore.write_workflow_state(directory, state)
    return dict(trees)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def _secret_name(name: str) -> bool:
    return bool(_ENV_NAME_RE.match(name))


class _Builder:
    def __init__(self, root: str, dest: str, max_bytes: int, max_files: int) -> None:
        self.root = root
        self.dest = dest
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.bytes = 0
        self.manifest: Dict[str, str] = {}
        self.skipped: List[Dict[str, str]] = []

    def skip(self, rel: str, reason: str) -> None:
        self.skipped.append({"path": rel, "reason": reason})

    def _has_link_parent(self, source: str) -> bool:
        """True when a directory between the project root and `source` is a
        symlink: a directory swapped for a link after it was listed would
        otherwise lead the copy outside the project."""
        root = os.path.normpath(self.root)
        parent = os.path.dirname(os.path.normpath(source))
        parts = []
        while parent != root and parent.startswith(root + os.sep):
            parts.append(parent)
            parent = os.path.dirname(parent)
        for directory in parts:
            try:
                if stat.S_ISLNK(os.lstat(directory).st_mode):
                    return True
            except OSError:
                return True
        return False

    def copy(self, rel: str, source: str) -> None:
        """Copy one regular file as bytes; anything else is skipped and listed."""
        if rel in self.manifest:
            return
        if _secret_name(os.path.basename(rel)):
            self.skip(rel, "secret-name")
            return
        try:
            info = os.lstat(source)
        except OSError:
            self.skip(rel, "missing")
            return
        if stat.S_ISLNK(info.st_mode):
            self.skip(rel, "symlink")
            return
        if stat.S_ISDIR(info.st_mode):
            self.skip(rel, "directory")
            return
        if not stat.S_ISREG(info.st_mode):
            self.skip(rel, "not-regular")
            return
        if self._has_link_parent(source):
            self.skip(rel, "symlink-parent")
            return
        if len(self.manifest) >= self.max_files:
            raise SnapshotRefused("snapshot-too-large")
        target = os.path.join(self.dest, rel)
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            src_fd = os.open(source, flags)
        except OSError:
            self.skip(rel, "unreadable")
            return
        digest = hashlib.sha256()
        try:
            if not stat.S_ISREG(os.fstat(src_fd).st_mode):
                self.skip(rel, "not-regular")
                return
            os.makedirs(os.path.dirname(target), exist_ok=True)
            out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                while True:
                    chunk = os.read(src_fd, _CHUNK)
                    if not chunk:
                        break
                    self.bytes += len(chunk)
                    if self.bytes > self.max_bytes:
                        raise SnapshotRefused("snapshot-too-large")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(out_fd, view):]
            finally:
                os.close(out_fd)
        except OSError:
            try:
                os.unlink(target)
            except OSError:
                pass
            self.skip(rel, "unreadable")
            return
        finally:
            os.close(src_fd)
        os.chmod(target, 0o444)
        self.manifest[rel] = digest.hexdigest()

    def copy_tree(self, rel: str, *, skip_suffix: Tuple[str, ...] = ()) -> None:
        """Copy a directory tree below `rel` (symlinks are listed and skipped)."""
        top = os.path.join(self.root, rel)
        try:
            info = os.lstat(top)
        except OSError:
            return
        if not stat.S_ISDIR(info.st_mode):
            self.skip(rel, "symlink" if stat.S_ISLNK(info.st_mode) else "not-directory")
            return
        stack = [rel]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(os.path.join(self.root, current)) as it:
                    children = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for child in children:
                child_rel = current + "/" + child.name
                if child.name.endswith(skip_suffix) if skip_suffix else False:
                    continue
                try:
                    if child.is_symlink():
                        self.skip(child_rel, "symlink")
                    elif child.is_dir(follow_symlinks=False):
                        stack.append(child_rel)
                    else:
                        self.copy(child_rel, child.path)
                except OSError:
                    self.skip(child_rel, "unreadable")


def _slug(rel: str, taken: set) -> str:
    base = "root" if rel in (".", "") else re.sub(r"[^A-Za-z0-9._-]+", "-", rel).strip(".-") or "repo"
    name, n = base, 1
    while name in taken:
        n += 1
        name = "%s-%d" % (base, n)
    taken.add(name)
    return name


def _write_review_context(builder: _Builder, project_root: str, task: str, stage: Optional[int]) -> None:
    directory = runstore.store_dir(project_root)
    try:
        state = runstore.load_workflow_state(directory, task)
    except runstore.RunStoreError:
        state = None
    recorded = {}
    if state is not None:
        base = (state.get("settings") or {}).get("base_trees")
        if isinstance(base, dict) and isinstance(base.get(_stage_key(stage)), dict):
            recorded = base[_stage_key(stage)]
    repos = _repos(project_root)
    abs_paths = [absolute for _, absolute in repos]
    context = os.path.join(builder.dest, _REVIEW_CONTEXT)
    os.makedirs(context, exist_ok=True)
    taken: set = set()
    notes: List[str] = []
    for rel, absolute in repos:
        slug = _slug(rel, taken)
        specs = _tree_specs(absolute, project_root, abs_paths)
        base_tree = recorded.get(rel)
        base_label = "the tree recorded before the stage began"
        if not (isinstance(base_tree, str) and base_tree):
            code, out, _ = _git(absolute, "rev-parse", "--verify", "-q", "HEAD^{tree}")
            base_tree = out.decode("ascii", "replace").strip() if code == 0 and out.strip() else _EMPTY_TREE
            base_label = "HEAD (no tree was recorded for this stage)"
        current = _write_tree(absolute, specs)
        if current is None:
            notes.append("%s: the diff is unavailable because the current tree could not be written" % rel)
            continue
        code, diff, cut = _git(
            absolute, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
            base_tree, current, "--", *specs, cap=MAX_DIFF_BYTES,
        )
        if code != 0 and not cut:
            notes.append("%s: the diff is unavailable because git failed" % rel)
            continue
        target = os.path.join(context, slug + ".diff")
        with open(target, "wb") as handle:
            handle.write(diff)
        os.chmod(target, 0o444)
        line = "%s: %s.diff compares %s with the current source" % (rel, slug, base_label)
        if cut:
            line += "; the diff was cut at %d bytes" % MAX_DIFF_BYTES
        notes.append(line)
    readme = os.path.join(context, "README.txt")
    with open(readme, "w", encoding="utf-8") as handle:
        handle.write("Changes under review, one diff per repository.\n\n" + "\n".join(notes) + "\n")
    os.chmod(readme, 0o444)


def _lock_down(root: str, writable: FrozenSet[str]) -> None:
    """Files become 0444 and directories 0555, except the `writable` paths
    (absolute) and the snapshot's own `.git`."""

    def visit(path: str) -> None:
        with os.scandir(path) as it:
            children = list(it)
        for child in children:
            if child.name == ".git" and path == root:
                continue
            if child.is_symlink():
                continue
            if child.is_dir(follow_symlinks=False):
                visit(child.path)
            else:
                os.chmod(child.path, 0o444)
        os.chmod(path, 0o755 if path in writable else 0o555)

    visit(root)


def create(
    project_root, task: str, stage: Optional[Any], *, role: str, source_dir, state_root,
    clock: Callable[[], float] = time.time, max_bytes: int = DEFAULT_MAX_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> Snapshot:
    """Build a read-only snapshot for a `critic` or `reviewer` run, or raise
    `SnapshotRefused` (nothing is left behind)."""
    if role not in _CREATE_KIND:
        raise SnapshotRefused("snapshot-invalid-role")
    try:
        runstore.check_task_name(task)
        number = runstore.normalize_stage(stage)
    except (runstore.RunStoreError, ValueError):
        raise SnapshotRefused("snapshot-invalid-request") from None
    root = os.path.realpath(str(project_root))
    parent = Path(state_root) / "snapshots" / paths.project_key(Path(root))
    try:
        jsonio.ensure_private_directory(parent)
    except Exception:  # noqa: BLE001 - any unusable state directory refuses
        raise SnapshotRefused("snapshot-state-unusable") from None
    stamp = datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = str(parent / ("%s-%s" % (stamp, _secrets.token_hex(4))))
    try:
        os.mkdir(dest, 0o700)
    except OSError:
        raise SnapshotRefused("snapshot-state-unusable") from None
    dest = os.path.realpath(dest)
    try:
        return _build(root, task, number, role, source_dir, dest, max_bytes, max_files)
    except BaseException:
        remove(Snapshot(Path(dest), ""))
        raise


def _build(
    root: str, task: str, stage: Optional[int], role: str, source_dir, dest: str,
    max_bytes: int, max_files: int,
) -> Snapshot:
    builder = _Builder(root, dest, max_bytes, max_files)
    try:
        meta = install.load_metadata(root)
    except (install.InstallError, OSError):
        raise SnapshotRefused("snapshot-install-unreadable") from None
    if meta is None:
        raise SnapshotRefused("snapshot-not-installed")

    # An isolated git root with one empty commit: discovery and project
    # identity stop here.
    identity = ("-c", "user.name=quoin", "-c", "user.email=quoin@localhost", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=/dev/null")
    for argv in (
        ("git", "-C", dest, "init", "-q"),
        ("git", "-C", dest, *identity, "commit", "-q", "--allow-empty", "-m", "snapshot"),
    ):
        try:
            done = subprocess.run(
                argv, env=_git_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise SnapshotRefused("snapshot-git-failed") from None
        if done.returncode != 0:
            raise SnapshotRefused("snapshot-git-failed")

    repos = _repos(root)
    abs_paths = [absolute for _, absolute in repos]
    for _, absolute in repos:
        specs = runstore.source_pathspecs(absolute, root, abs_paths)
        code, out, cut = _git(
            absolute, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *specs,
        )
        if code != 0 or cut:
            raise SnapshotRefused("snapshot-git-failed")
        for raw in out.split(b"\0"):
            if not raw:
                continue
            name = os.fsdecode(raw)
            source = os.path.join(absolute, name)
            rel = os.path.relpath(source, root).replace(os.sep, "/")
            if rel == ".." or rel.startswith("../"):
                continue
            builder.copy(rel, source)

    for rel in sorted(meta.owned):
        builder.copy(rel, os.path.join(root, rel))
    for rel in (".quoin/opencode-install.json", ".quoin/runtime.json"):
        if os.path.lexists(os.path.join(root, rel)):
            builder.copy(rel, os.path.join(root, rel))
    builder.copy_tree(_ARTIFACTS + "/" + task, skip_suffix=(".tmp",))
    for rel in boundaries._DISCOVER_FILES + (boundaries._LESSONS_REL,):  # noqa: SLF001
        if os.path.lexists(os.path.join(root, rel)):
            builder.copy(rel, os.path.join(root, rel))

    try:
        outbox = gate.stage_dir(dest, task, stage, source_dir)
    except (gate.PathUnresolved, gate.GateRefused, OSError, ImportError, AttributeError):
        raise SnapshotRefused("snapshot-outbox-unresolved") from None
    outbox_abs = os.path.realpath(str(outbox))
    if not (outbox_abs == dest or outbox_abs.startswith(dest + os.sep)):
        raise SnapshotRefused("snapshot-outbox-unresolved")
    os.makedirs(outbox_abs, exist_ok=True)
    outbox_rel = os.path.relpath(outbox_abs, dest).replace(os.sep, "/")
    for rel in _AGENT_DIRS:
        os.makedirs(os.path.join(dest, rel), exist_ok=True)

    if role == "reviewer":
        _write_review_context(builder, root, task, stage)

    writable = {outbox_abs}
    writable.update(os.path.join(dest, rel) for rel in _AGENT_DIRS)
    if os.path.isdir(os.path.join(dest, ".opencode")):
        writable.add(os.path.join(dest, ".opencode"))
    _lock_down(dest, frozenset(writable))
    return Snapshot(Path(dest), outbox_rel, dict(builder.manifest), tuple(builder.skipped))


# ---------------------------------------------------------------------------
# harvest and remove
# ---------------------------------------------------------------------------

def outbox_names(snapshot: Snapshot) -> FrozenSet[str]:
    """Every name currently in the snapshot's outbox directory."""
    try:
        return frozenset(os.listdir(os.path.join(str(snapshot.root), snapshot.outbox_rel)))
    except OSError:
        return frozenset()


def _numbers(directory: str, kind: str) -> List[int]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        match = _FINDING_RE[kind].match(name)
        if match:
            out.append(int(match.group(1)))
    return out


def _read_finding(path: str) -> Optional[bytes]:
    got = jsonio.read_regular_bytes(path, max_bytes=MAX_FINDING_BYTES)
    return got[0] if got is not None else None


def harvest(
    snapshot: Snapshot, project_root, task: str, stage: Optional[Any], *, kind: str, source_dir,
    before_names: Sequence[str] = (),
) -> Harvest:
    """Move the run's one new finding from the snapshot outbox into the real
    task folder as the next free numbered file. Exactly one new file matching
    the kind must exist and validate; the run's own number is ignored."""
    if kind not in _FINDING_RE:
        return Harvest(error="harvest-none", detail="invalid-kind")
    before = set(before_names)
    outbox = os.path.join(str(snapshot.root), snapshot.outbox_rel)
    matches: List[str] = []
    tmp: List[str] = []
    unexpected: List[str] = []
    try:
        names = sorted(os.listdir(outbox))
    except OSError:
        names = []
    for name in names:
        if name in before:
            continue
        if name.endswith(".tmp"):
            tmp.append(name)
        elif _FINDING_RE[kind].match(name):
            matches.append(name)
        else:
            unexpected.append(name)
    base = dict(ignored_tmp=tuple(tmp), unexpected=tuple(unexpected))
    if not matches:
        return Harvest(error="harvest-none", **base)
    if len(matches) > 1:
        return Harvest(error="harvest-ambiguous", **base)
    name = matches[0]
    data = _read_finding(os.path.join(outbox, name))
    if data is None:
        return Harvest(error="harvest-unreadable", **base)
    sha = hashlib.sha256(data).hexdigest()

    # Validate a private copy under the real filename, so what is checked is
    # exactly what is written.
    candidate_dir = str(Path(str(snapshot.root) + ".candidate"))
    try:
        os.makedirs(candidate_dir, mode=0o700, exist_ok=True)
        candidate = os.path.join(candidate_dir, name)
        with open(candidate, "wb") as handle:
            handle.write(data)
        problem = gate.run_validator(str(project_root), source_dir, candidate)
    except OSError:
        return Harvest(error="harvest-unreadable", **base)
    finally:
        shutil.rmtree(candidate_dir, ignore_errors=True)
    if problem is not None:
        return Harvest(error="harvest-invalid", detail=problem, **base)

    try:
        number_stage = runstore.normalize_stage(stage)
        target_dir = gate.stage_dir(project_root, task, number_stage, source_dir)
    except (gate.PathUnresolved, gate.GateRefused, ValueError, OSError):
        return Harvest(error="harvest-target-unresolved", **base)
    target_dir_s = str(target_dir)
    if not os.path.isdir(target_dir_s):
        return Harvest(error="harvest-target-unresolved", **base)
    prefix = _FINDING_PREFIX[kind]
    number = max(_numbers(target_dir_s, kind), default=0) + 1
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(1000):
        target = os.path.join(target_dir_s, "%s%d.md" % (prefix, number))
        try:
            fd = os.open(target, flags, 0o644)
        except FileExistsError:
            existing = _read_finding(target)
            if existing is not None and hashlib.sha256(existing).hexdigest() == sha:
                break  # an earlier attempt already stored this finding
            number += 1
            continue
        except OSError:
            return Harvest(error="harvest-write-failed", **base)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except OSError:
            os.close(fd)
            try:
                os.unlink(target)
            except OSError:
                pass
            return Harvest(error="harvest-write-failed", **base)
        os.close(fd)
        break
    else:
        return Harvest(error="harvest-write-failed", **base)
    rel = os.path.relpath(target, os.path.realpath(str(project_root))).replace(os.sep, "/")
    return Harvest(path_rel=rel, sha256=sha, number=number, **base)


def remove(snapshot: Snapshot) -> bool:
    """Delete a snapshot, restoring write bits first. Never raises; False means
    something is left behind at `snapshot.root`."""
    root = str(snapshot.root)
    try:
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                info = os.lstat(current)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            try:
                os.chmod(current, 0o700)
                with os.scandir(current) as it:
                    stack.extend(e.path for e in it if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
        shutil.rmtree(root, ignore_errors=True)
    except Exception:  # noqa: BLE001 - removal is best effort
        pass
    return not os.path.lexists(root)


# ---------------------------------------------------------------------------
# the isolated run
# ---------------------------------------------------------------------------

def run_in_snapshot(
    drv: Any, request: Any, *, kind: str, source_dir, cancel: Any, max_relaunch: int,
    backoff_fn: Optional[Callable[[int], float]] = None, clock: Callable[[], float] = time.time,
) -> IsolatedRun:
    """Run a critic (`kind="critic"`) or review (`kind="review"`) phase in a
    fresh snapshot, check that the real tree did not change, and harvest the
    finding. The caller holds the task lock. Nothing is recorded here: the
    caller records the harvested path and hash."""
    if kind not in SNAPSHOT_ROLE:
        raise ValueError("kind must be 'critic' or 'review'")
    root = os.path.realpath(str(request.project_root))
    task = request.task
    directory = runstore.store_dir(root, create=True)
    try:
        pointer = runstore.load_pointer(directory, task)
    except runstore.RunStoreError:
        pointer = None
    prior = pointer.get("run_id") if isinstance(pointer, dict) else None
    before = boundaries.take_listing(root, clock=clock)
    snap: Optional[Snapshot] = None
    removed = True
    try:
        try:
            snap = create(
                root, task, request.stage, role="critic" if kind == "critic" else "reviewer",
                source_dir=source_dir, state_root=drv.state_root, clock=clock,
            )
        except SnapshotRefused as exc:
            return IsolatedRun(None, None, None, None, True, refusal=exc.code)
        before_names = outbox_names(snap)
        result = phase_loop.run_phase(
            drv, dataclasses.replace(request, workspace=snap.root), max_relaunch=max_relaunch,
            cancel=cancel, new_run=True, backoff_fn=backoff_fn,
        )
        after = boundaries.take_listing(root, clock=clock)
        boundary = boundaries.verify(
            SNAPSHOT_ROLE[kind], before, after, task=task, run_id=result.run_id or "", prior_run_id=prior,
        )
        found: Optional[Harvest] = None
        skipped: Optional[str] = None
        if not boundary.ok:
            skipped = "boundary-" + boundary.status
        elif result.outcome != "COMPLETED":
            skipped = "outcome-" + result.outcome.lower()
        else:
            try:
                found = harvest(
                    snap, root, task, request.stage, kind=kind, source_dir=source_dir,
                    before_names=before_names,
                )
            except Exception:  # noqa: BLE001 - a harvest failure never hides the run
                found = Harvest(error="harvest-failed")
        root_path = snap.root
    finally:
        if snap is not None:
            removed = remove(snap)
    return IsolatedRun(result, root_path, found, boundary, removed, harvest_skipped=skipped)
