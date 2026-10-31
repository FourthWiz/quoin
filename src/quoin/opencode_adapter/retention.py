"""Retention for OpenCode profile storage: a descriptor-based deletion engine.

Every walk and every removal works relative to an open directory descriptor
and refuses to follow symbolic links, so a path component swapped for a link
after planning cannot redirect a removal outside the planned root. Entries
owned by another user are never entered or removed.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from . import boundaries, handoff, install, jsonio, paths, profile_dirs, profile_policy, runstore

CLASSES = (
    "runs", "tests", "workflow", "snapshots", "records", "cache", "tmp", "logs",
    "crash", "exports", "profile-data", "profile-state", "compiled", "other",
)
KINDS = ("file", "tree", "run")
KEPT_REASONS = (
    "credential", "profile-stamp", "project-index", "unattributable", "foreign-owner",
    "evidence-referenced", "task-unfinished", "run-active", "live-lock", "store-unavailable",
    "walk-truncated", "not-aged", "never-swept", "profile-in-use", "unreadable",
)
# Reasons that mean a purge left behind something it should have deleted.
PURGE_INCOMPLETE_REASONS = ("foreign-owner", "unreadable", "walk-truncated")
FAILED_REASONS = ("changed-since-plan", "foreign-owner", "os-error")

WALK_BUDGET = 50000
MAX_WALK_DEPTH = 64
SIDECAR_RE = re.compile(r"^(oc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})\.(jsonl|run\.json|checkpoint\.json)$")
SIDECAR_SUFFIXES = (".jsonl", ".run.json", ".checkpoint.json")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class RootUnsafe(Exception):
    """The root is a link, not a directory, or owned by another user."""


@dataclass(frozen=True)
class Item:
    cls: str
    unit: str
    display: str
    rel_parts: Tuple[str, ...]
    kind: str
    files: int = 0
    bytes: int = 0
    newest_mtime: float = 0.0
    exclude: Tuple[str, ...] = ()
    root: str = ""
    root_id: Tuple[int, int] = ()
    skipped: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Kept:
    display: str
    reason: str


@dataclass(frozen=True)
class Plan:
    profile: str
    mode: str
    remove: Tuple[Item, ...] = ()
    keep: Tuple[Kept, ...] = ()
    referenced_runs: Tuple[Dict[str, str], ...] = ()
    stores_not_visited: Tuple[Dict[str, str], ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class Result:
    removed: Tuple[Item, ...] = ()
    failed: Tuple[Tuple[str, str], ...] = ()
    skipped: Tuple[str, ...] = ()


class Budget:
    """Entry budget for a group of walks; `None` means unbounded."""

    def __init__(self, limit: Optional[int] = WALK_BUDGET) -> None:
        self.left = limit
        self.truncated = False

    def take(self) -> bool:
        if self.left is None:
            return True
        if self.left <= 0:
            self.truncated = True
            return False
        self.left -= 1
        return True


def _root_id(fd: int) -> Tuple[int, int]:
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino)


def _is_foreign(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) and str(exc) == "foreign-owner"


def _open_root(path, uid: int) -> int:
    """Open `path` as a directory without following a final link, owned by `uid`."""
    try:
        fd = os.open(str(path), _DIR_FLAGS)
    except OSError as exc:
        raise RootUnsafe(str(exc)) from exc
    try:
        if os.fstat(fd).st_uid != uid:
            raise RootUnsafe("foreign-owner")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_child(parent_fd: int, name: str, uid: int) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        if os.fstat(fd).st_uid != uid:
            raise PermissionError("foreign-owner")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _excl_parts(exclude: Sequence[str]) -> Set[Tuple[str, ...]]:
    return {tuple(p for p in item.split("/") if p) for item in exclude}


def _is_excluded_or_ancestor(rel: Tuple[str, ...], excl: Set[Tuple[str, ...]]) -> Tuple[bool, bool]:
    """(is an excluded path, is a strict ancestor of one)."""
    exact = rel in excl
    ancestor = any(len(e) > len(rel) and e[: len(rel)] == rel for e in excl)
    return exact, ancestor


@dataclass
class _Measure:
    files: int = 0
    bytes: int = 0
    newest: float = 0.0
    foreign: List[Tuple[str, ...]] = field(default_factory=list)
    unreadable: List[Tuple[str, ...]] = field(default_factory=list)
    young: bool = False


def _measure(dir_fd: int, rel: Tuple[str, ...], uid: int, budget: Budget,
             excl: Set[Tuple[str, ...]], out: _Measure, young_after: Optional[float] = None) -> None:
    """Count files, bytes and the newest mtime over files and directories. With
    `young_after`, stop as soon as one entry is at least that new: the unit is
    then known not to be aged and its totals are partial."""
    if len(rel) >= MAX_WALK_DEPTH:
        budget.truncated = True
        return
    try:
        entries = sorted(os.scandir(dir_fd), key=lambda e: e.name)
    except OSError:
        out.unreadable.append(rel)
        return
    for entry in entries:
        if out.young:
            return
        if not budget.take():
            return
        try:
            info = os.lstat(entry.name, dir_fd=dir_fd)
        except OSError:
            continue
        child = rel + (entry.name,)
        if info.st_uid != uid:
            out.foreign.append(child)
            continue
        exact, ancestor = _is_excluded_or_ancestor(child, excl)
        if exact:
            continue
        out.newest = max(out.newest, info.st_mtime)
        if young_after is not None and out.newest >= young_after:
            out.young = True
            return
        if stat.S_ISDIR(info.st_mode):
            try:
                sub = _open_child(dir_fd, entry.name, uid)
            except OSError as exc:
                (out.foreign if _is_foreign(exc) else out.unreadable).append(child)
                continue
            try:
                _measure(sub, child, uid, budget, excl, out, young_after)
            finally:
                os.close(sub)
        else:
            out.files += 1
            out.bytes += info.st_size


def _chain_fd(root_fd: int, parts: Sequence[str], uid: int) -> int:
    """A new descriptor for `root_fd/parts...`, opened one component at a time."""
    fd = os.dup(root_fd)
    try:
        for part in parts:
            nxt = _open_child(fd, part, uid)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def unit_item(root_fd: int, root: str, parent_parts: Tuple[str, ...], name: str, *, cls: str,
              uid: int, budget: Budget, display: Optional[str] = None,
              exclude: Sequence[str] = (), young_after: Optional[float] = None,
              skip_foreign: bool = False,
              measure_only: bool = False) -> Tuple[Optional[Item], Optional[Kept]]:
    """Describe one entry under `root/parent_parts` as a removable unit, or say why
    it is kept. Links, FIFOs, sockets and devices are file units and never opened.

    A foreign-owned or unreadable entry inside a tree keeps the whole unit, unless
    `skip_foreign` is set: then it is excluded from removal and listed on the item.
    With `measure_only`, running out of budget only makes the totals a lower bound."""
    shown = display or "/".join(parent_parts + (name,))
    try:
        parent = _chain_fd(root_fd, parent_parts, uid)
    except OSError:
        return None, Kept(shown, "foreign-owner")
    rid = _root_id(root_fd)
    try:
        try:
            info = os.lstat(name, dir_fd=parent)
        except OSError:
            return None, None
        if info.st_uid != uid:
            return None, Kept(shown, "foreign-owner")
        if not stat.S_ISDIR(info.st_mode):
            return Item(cls, name, shown, parent_parts + (name,), "file", 1, info.st_size,
                        info.st_mtime, (), root, rid), None
        excl = _excl_parts(exclude)
        measure = _Measure(newest=info.st_mtime)
        if young_after is not None and info.st_mtime >= young_after:
            measure.young = True
        try:
            sub = _open_child(parent, name, uid)
        except OSError as exc:
            return None, Kept(shown, "foreign-owner" if _is_foreign(exc) else "unreadable")
        try:
            if not measure.young:
                _measure(sub, (), uid, budget, excl, measure, young_after)
        finally:
            os.close(sub)
        skipped: List[Tuple[str, str]] = []
        extra: List[str] = []
        for rels, reason in ((measure.foreign, "foreign-owner"), (measure.unreadable, "unreadable")):
            for rel in rels:
                if not skip_foreign or not rel:
                    return None, Kept(shown, reason)
                extra.append("/".join(rel))
                skipped.append(("/".join(parent_parts + (name,) + rel), reason))
        if budget.truncated and not measure_only:
            return None, Kept(shown, "walk-truncated")
        return Item(cls, name, shown, parent_parts + (name,), "tree", measure.files, measure.bytes,
                    measure.newest, tuple(exclude) + tuple(extra), root, rid, tuple(skipped)), None
    finally:
        os.close(parent)


def plan_entries(root: str, parent_parts: Tuple[str, ...], *, cls: str, uid: int, budget: Budget,
                 excludes: Optional[Dict[str, Sequence[str]]] = None,
                 skip: Sequence[str] = (), young_after: Optional[float] = None) -> Tuple[List[Item], List[Kept]]:
    """Units for every immediate child of `root/parent_parts`, sorted by name.
    Children after the budget runs out are kept as `walk-truncated`."""
    root_fd = _open_root(root, uid)
    items: List[Item] = []
    kept: List[Kept] = []
    try:
        try:
            directory = _chain_fd(root_fd, parent_parts, uid)
        except OSError:
            return items, kept
        try:
            names = sorted(e.name for e in os.scandir(directory))
        finally:
            os.close(directory)
        for name in names:
            if name in skip:
                continue
            shown = "/".join(parent_parts + (name,))
            if not budget.take():
                kept.append(Kept(shown, "walk-truncated"))
                continue
            item, why = unit_item(root_fd, root, parent_parts, name, cls=cls, uid=uid, budget=budget,
                                  exclude=(excludes or {}).get(name, ()), young_after=young_after)
            if item is not None:
                items.append(item)
            if why is not None:
                kept.append(why)
    finally:
        os.close(root_fd)
    return items, kept


def plan_store_runs(project_root, *, uid: int, budget: Budget) -> Tuple[List[Item], List[Kept]]:
    """One `run` unit per run id in the project's run store, built from the strict
    sidecar names only; every other file in the store is ignored."""
    directory = runstore.inspect_store(project_root)
    if directory is None:
        return [], []
    root = str(directory)
    root_fd = _open_root(root, uid)
    try:
        found: Dict[str, List[os.stat_result]] = {}
        for entry in sorted(os.scandir(root_fd), key=lambda e: e.name):
            match = SIDECAR_RE.match(entry.name)
            if not match:
                continue
            if not budget.take():
                break
            try:
                info = os.lstat(entry.name, dir_fd=root_fd)
            except OSError:
                continue
            found.setdefault(match.group(1), []).append(info)
        items: List[Item] = []
        kept: List[Kept] = []
        for run_id in sorted(found):
            infos = found[run_id]
            if any(i.st_uid != uid for i in infos):
                kept.append(Kept(run_id, "foreign-owner"))
                continue
            items.append(Item("runs", run_id, run_id, (), "run", len(infos),
                              sum(i.st_size for i in infos), max(i.st_mtime for i in infos), (), root, _root_id(root_fd)))
        if budget.truncated:
            kept.append(Kept("run-store", "walk-truncated"))
        return items, kept
    finally:
        os.close(root_fd)


def _remove_entry(dir_fd: int, name: str, rel: Tuple[str, ...], uid: int,
                  excl: Set[Tuple[str, ...]]) -> bool:
    """Remove `name` below `dir_fd`; True when it is gone, False when something
    excluded had to stay inside it."""
    exact, ancestor = _is_excluded_or_ancestor(rel, excl)
    if exact:
        return False
    info = os.lstat(name, dir_fd=dir_fd)
    if info.st_uid != uid:
        raise PermissionError("foreign-owner")
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return True
    sub = _open_child(dir_fd, name, uid)
    try:
        mode = os.fstat(sub).st_mode
        if mode & 0o700 != 0o700:
            os.fchmod(sub, mode | 0o700)
        clean = True
        for child in sorted(e.name for e in os.scandir(sub)):
            if not _remove_entry(sub, child, rel + (child,), uid, excl):
                clean = False
    finally:
        os.close(sub)
    if not clean:
        return False
    os.rmdir(name, dir_fd=dir_fd)
    return True


def _execute_item(item: Item, uid: int) -> Optional[str]:
    """None on success, else a closed failure reason."""
    try:
        root_fd = _open_root(item.root, uid)
    except RootUnsafe:
        return "changed-since-plan"
    try:
        if item.root_id and _root_id(root_fd) != item.root_id:
            return "changed-since-plan"
        if item.kind == "run":
            for suffix in SIDECAR_SUFFIXES:
                name = item.unit + suffix
                try:
                    info = os.lstat(name, dir_fd=root_fd)
                except FileNotFoundError:
                    continue
                if info.st_uid != uid:
                    return "foreign-owner"
                os.unlink(name, dir_fd=root_fd)
            return None
        parts = item.rel_parts
        try:
            parent = _chain_fd(root_fd, parts[:-1], uid)
        except PermissionError:
            return "foreign-owner"
        except OSError:
            return "changed-since-plan"
        try:
            try:
                _remove_entry(parent, parts[-1], (), uid, _excl_parts(item.exclude))
            except FileNotFoundError:
                return "changed-since-plan"
            except PermissionError as exc:
                return "foreign-owner" if "foreign-owner" in str(exc) else "os-error"
            except OSError:
                return "changed-since-plan" if _is_swap_error(parts[-1], parent) else "os-error"
        finally:
            os.close(parent)
    finally:
        os.close(root_fd)
    return None


def _is_swap_error(name: str, parent: int) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=parent).st_mode)
    except OSError:
        return False


def execute(plan: Plan, *, uid: int) -> Result:
    """Remove every planned item. Never raises for a filesystem error: the item is
    reported as failed with a closed reason."""
    removed: List[Item] = []
    failed: List[Tuple[str, str]] = []
    for item in plan.remove:
        try:
            reason = _execute_item(item, uid)
        except Exception:  # noqa: BLE001 - an item must never abort the rest
            reason = "os-error"
        if reason is None:
            removed.append(item)
        else:
            failed.append((item.display, reason))
    return Result(tuple(removed), tuple(failed), ())


# ---------------------------------------------------------------------------
# evidence index
# ---------------------------------------------------------------------------

LIVENESS_GRACE_S = 3600.0
PREPARE_WINDOW_S = 900.0
FINALIZED_DEPTH = 3
ACTIVITY_REASONS = ("live-driver", "live-child", "liveness-unknown", "prepare-in-progress")
EVIDENCE_REASONS = ("missing", "unreadable", "unsafe")
REF_SOURCES = ("workflow-state", "continuation", "pointer")

_RUN_TOKEN_RE = re.compile(runstore.RUN_ID_RE.pattern.strip("^$"))
_WORKFLOW_FILE_RE = re.compile(r"^workflow-(.+)\.json$")
_POINTER_FILE_RE = re.compile(r"^task-(.+)\.json$")


@dataclass(frozen=True)
class RunFacts:
    profile: Optional[str]
    active: bool
    empty_orphan: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class ProjectEvidence:
    readable: bool
    reason: Optional[str] = None
    referenced: Mapping[str, Tuple[str, str]] = field(default_factory=dict)
    unfinished_tasks: FrozenSet[str] = frozenset()
    live_lock_tasks: FrozenSet[str] = frozenset()
    runs: Mapping[str, RunFacts] = field(default_factory=dict)

    @classmethod
    def load(cls, project_root, *, uid: int, attribute: bool, table, budget: Budget,
             now: Optional[float] = None) -> "ProjectEvidence":
        return _load_evidence(project_root, uid, attribute, table, budget,
                              time.time() if now is None else now)


def _age(now: float, mtime: float) -> float:
    return max(0.0, now - mtime)


def _positive(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def stale_window(record: Mapping) -> float:
    """How long a run whose liveness cannot be decided stays protected: its own
    deadline (at least the default) plus a grace hour."""
    from . import driver  # noqa: PLC0415 - driver imports this module

    request = record.get("request")
    values = [driver.DEFAULT_TIMEOUT_S]
    if isinstance(request, Mapping):
        values.append(_positive(request.get("timeout_s")))
    return max(v for v in values if v is not None) + LIVENESS_GRACE_S


def run_activity(record: Mapping, table, now: float, newest_mtime: float) -> Optional[str]:
    """`None` when the run is terminal, else the closed reason it must be kept.
    Uses the process table taken once per invocation and never signals."""
    state = record.get("state")
    age = _age(now, newest_mtime)
    if state == "prepared":
        if record.get("refusal") is not None:
            return None
        return "prepare-in-progress" if age <= PREPARE_WINDOW_S else None
    if state != "running":
        return None
    if runstore.live_identities(record, table):
        return "live-child"
    attempts = record.get("attempts") or []
    last = attempts[-1] if attempts else None
    if (isinstance(last, Mapping) and last.get("driver_pid") is not None
            and last.get("driver_start") is not None and table is not None):
        return None if runstore.orphan_state(record, table) == "driver-lost" else "live-driver"
    return "liveness-unknown" if age <= stale_window(record) else None


def run_active(record: Mapping, table, now: float, newest_mtime: float) -> bool:
    return run_activity(record, table, now, newest_mtime) is not None


def _moved_time(info: os.stat_result) -> float:
    """When a directory arrived where it is: a rename keeps its mtime but updates
    its ctime."""
    return max(info.st_mtime, info.st_ctime)


def _finalized_dirs(artifacts: str, budget: Budget) -> Dict[str, float]:
    """Task name -> mtime of a directory of that name directly inside a
    `finalized` directory, searching a few levels, never following links."""
    found: Dict[str, float] = {}
    stack = [(artifacts, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for entry in entries:
            if not budget.take():
                return found
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name == "finalized":
                try:
                    for child in os.scandir(entry.path):
                        if child.is_dir(follow_symlinks=False) and not child.is_symlink():
                            moved = _moved_time(child.stat(follow_symlinks=False))
                            found[child.name] = max(found.get(child.name, 0.0), moved)
                except OSError:
                    pass
            elif depth + 1 < FINALIZED_DEPTH and entry.name not in ("memory", "cache"):
                stack.append((entry.path, depth + 1))
    return found


def _read_text(path, budget: Budget) -> Optional[str]:
    if not budget.take():
        return None
    got = jsonio.read_regular_bytes(path, max_bytes=runstore.MAX_RECORD_BYTES)
    if got is None:
        return None
    return got[0].decode("utf-8", "replace")


def _load_evidence(project_root, uid, attribute, table, budget, now) -> ProjectEvidence:
    try:
        directory = runstore.inspect_store(project_root)
    except runstore.RunStoreError:
        return ProjectEvidence(False, "unsafe")
    except OSError:
        return ProjectEvidence(False, "unreadable")
    if directory is None:
        return ProjectEvidence(False, "missing")
    try:
        names_ = sorted(e.name for e in os.scandir(str(directory)))
    except OSError:
        return ProjectEvidence(False, "unreadable")

    state_files: List[Tuple[str, str, str]] = []  # task, source, path
    for name in names_:
        for regex, source in ((_WORKFLOW_FILE_RE, "workflow-state"), (_POINTER_FILE_RE, "pointer")):
            match = regex.match(name)
            if match and runstore.TASK_RE.match(match.group(1)):
                state_files.append((match.group(1), source, os.path.join(str(directory), name)))
    try:
        cdir = handoff.continuation_dir(project_root)
    except handoff.HandoffRefused:
        return ProjectEvidence(False, "unsafe")
    try:
        cnames = sorted(n for n in os.listdir(str(cdir)) if n.endswith(".json"))
    except FileNotFoundError:
        cnames = []
    except OSError:
        return ProjectEvidence(False, "unreadable")
    for name in cnames:
        task = name[:-len(".json")]
        if runstore.TASK_RE.match(task):
            state_files.append((task, "continuation", os.path.join(str(cdir), name)))

    refs: List[Tuple[str, str, str]] = []  # run id, task, source
    task_mtime: Dict[str, float] = {}
    for task, source, path in state_files:
        text = _read_text(path, budget)
        if text is None:
            return ProjectEvidence(False, "unreadable")
        try:
            task_mtime[task] = max(task_mtime.get(task, 0.0), os.lstat(path).st_mtime)
        except OSError:
            return ProjectEvidence(False, "unreadable")
        for run_id in sorted(set(_RUN_TOKEN_RE.findall(text))):
            refs.append((run_id, task, source))

    artifacts = os.path.join(str(project_root), ".workflow_artifacts")
    finalized = _finalized_dirs(artifacts, budget)
    unfinished = set()
    for task, mtime in task_mtime.items():
        done = (not os.path.lexists(os.path.join(artifacts, task))
                and task in finalized and mtime <= finalized[task])
        if not done:
            unfinished.add(task)

    referenced: Dict[str, Tuple[str, str]] = {}
    for run_id, task, source in refs:
        if run_id not in referenced or (task in unfinished and referenced[run_id][0] not in unfinished):
            referenced[run_id] = (task, source)

    try:
        locks = boundaries.live_task_locks(project_root, strict=True)
    except OSError:
        return ProjectEvidence(False, "unreadable")

    runs: Dict[str, RunFacts] = {}
    if attribute:
        runs = _attribute_runs(str(directory), uid, table, budget, now)
    return ProjectEvidence(True, None, referenced, frozenset(unfinished), frozenset(locks), runs)


def _attribute_runs(directory: str, uid: int, table, budget: Budget, now: float) -> Dict[str, RunFacts]:
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return {}
    by_run: Dict[str, Dict[str, os.stat_result]] = {}
    for entry in entries:
        match = SIDECAR_RE.match(entry.name)
        if not match:
            continue
        try:
            by_run.setdefault(match.group(1), {})[match.group(2)] = os.lstat(entry.path)
        except OSError:
            continue
    out: Dict[str, RunFacts] = {}
    for run_id in sorted(by_run):
        parts = by_run[run_id]
        empty = ("run.json" not in parts and "checkpoint.json" not in parts
                 and "jsonl" in parts and parts["jsonl"].st_size == 0)
        profile: Optional[str] = None
        active = False
        reason: Optional[str] = None
        if "run.json" in parts:
            if not budget.take():
                break
            record = _read_record(os.path.join(directory, run_id + ".run.json"))
            if record is not None:
                request = record.get("request")
                value = request.get("profile") if isinstance(request, Mapping) else None
                if isinstance(value, str) and install.PROFILE_RE.fullmatch(value):
                    profile = value
                    newest = max(i.st_mtime for i in parts.values())
                    reason = run_activity(record, table, now, newest)
                    active = reason is not None
        out[run_id] = RunFacts(profile, active, empty, reason)
    return out


def _read_record(path: str) -> Optional[Mapping]:
    got = jsonio.read_regular_bytes(path, max_bytes=runstore.MAX_RECORD_BYTES)
    if got is None:
        return None
    try:
        data = json.loads(got[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def project_keys(dirs) -> Dict[str, str]:
    """Project key -> realpath for every project the profile's index lists."""
    return {paths.project_key(Path(p)): p for p in profile_dirs.read_index(dirs)}


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

DAY_S = 86400.0
_START_RECORD_RE = re.compile(r"^start-[0-9a-f]{12}\.json$")
_MANIFEST_NAMES = ("package.json", "package-lock.json", "bun.lock", "bun.lockb", "yarn.lock", "node_modules")


@dataclass(frozen=True)
class RetentionPolicy:
    wide: Mapping[str, Optional[int]]
    project: Mapping[str, Optional[int]]

    @classmethod
    def from_effective(cls, effective) -> "RetentionPolicy":
        view = profile_policy.view(effective)
        return cls(dict(view.retention), dict(view.project_retention))

    @property
    def empty(self) -> bool:
        return all(v is None for v in list(self.wide.values()) + list(self.project.values()))


def _days(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _aged(now: float, newest: float, days: Optional[int]) -> bool:
    return days is not None and _age(now, newest) > days * DAY_S


def _names(root: str, parts: Tuple[str, ...], uid: int) -> List[str]:
    """Sorted entry names of `root/parts`, empty when it is absent or unsafe."""
    try:
        root_fd = _open_root(root, uid)
    except RootUnsafe:
        return []
    try:
        try:
            fd = _chain_fd(root_fd, parts, uid)
        except OSError:
            return []
        try:
            return sorted(e.name for e in os.scandir(fd))
        except OSError:
            return []
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def _group_manifest(items: List[Item]) -> List[List[Item]]:
    """Manifest siblings form one unit; every other item stands alone."""
    group = [i for i in items if i.rel_parts[-1] in _MANIFEST_NAMES]
    rest = [[i] for i in items if i.rel_parts[-1] not in _MANIFEST_NAMES]
    return ([group] if group else []) + rest


def _decide(units: List[List[Item]], days: Optional[int], now: float,
            remove: List[Item], keep: List[Kept]) -> None:
    for unit in units:
        newest = max(i.newest_mtime for i in unit)
        if _aged(now, newest, days):
            remove.extend(unit)
        else:
            keep.extend(Kept(i.display, "not-aged") for i in unit)


def _tmp_files(root: str, uid: int, budget: Budget) -> Tuple[List[Item], List[Kept]]:
    """Every file under `tmp/` as a unit aged by the newer of itself and its directory."""
    items: List[Item] = []
    kept: List[Kept] = []
    try:
        root_fd = _open_root(root, uid)
    except RootUnsafe:
        return items, kept
    rid = _root_id(root_fd)

    def walk(fd: int, parts: Tuple[str, ...], dir_mtime: float) -> None:
        if len(parts) > MAX_WALK_DEPTH:
            kept.append(Kept("/".join(parts), "walk-truncated"))
            return
        try:
            entries = sorted(os.scandir(fd), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            shown = "/".join(parts + (entry.name,))
            if not budget.take():
                kept.append(Kept(shown, "walk-truncated"))
                return
            try:
                info = os.lstat(entry.name, dir_fd=fd)
            except OSError:
                continue
            if info.st_uid != uid:
                kept.append(Kept(shown, "foreign-owner"))
                continue
            if stat.S_ISDIR(info.st_mode):
                try:
                    sub = _open_child(fd, entry.name, uid)
                except OSError:
                    kept.append(Kept(shown, "foreign-owner"))
                    continue
                try:
                    walk(sub, parts + (entry.name,), info.st_mtime)
                finally:
                    os.close(sub)
            else:
                items.append(Item("tmp", entry.name, shown, parts + (entry.name,), "file", 1,
                                  info.st_size, max(info.st_mtime, dir_mtime), (), root, rid))

    try:
        top = os.fstat(root_fd).st_mtime
        try:
            fd = _chain_fd(root_fd, ("tmp",), uid)
        except OSError:
            return items, kept
        try:
            walk(fd, ("tmp",), top)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    return items, kept


def _prune_empty_dirs(root: str, uid: int, cutoff_days: int, now: float,
                      budget: Optional[Budget] = None) -> None:
    """Remove emptied, aged directories below `tmp/`, deepest first."""
    budget = budget or Budget()
    try:
        root_fd = _open_root(root, uid)
    except RootUnsafe:
        return

    def prune(fd: int, depth: int = 0) -> None:
        if depth >= MAX_WALK_DEPTH:
            return
        try:
            names = sorted(e.name for e in os.scandir(fd))
        except OSError:
            return
        for name in names:
            if not budget.take():
                return
            try:
                info = os.lstat(name, dir_fd=fd)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
                    continue
                sub = _open_child(fd, name, uid)
            except OSError:
                continue
            try:
                prune(sub, depth + 1)
            finally:
                os.close(sub)
            try:
                if _aged(now, os.lstat(name, dir_fd=fd).st_mtime, cutoff_days):
                    os.rmdir(name, dir_fd=fd)
            except OSError:
                pass

    try:
        fd = _chain_fd(root_fd, ("tmp",), uid)
    except OSError:
        os.close(root_fd)
        return
    try:
        prune(fd)
    finally:
        os.close(fd)
        os.close(root_fd)


def _stores(dirs, project_root) -> Tuple[str, Dict[str, str]]:
    """(current key, key -> project realpath) for the current project and the index."""
    current = os.path.realpath(str(project_root))
    mapping = project_keys(dirs)
    key = paths.project_key(Path(current))
    mapping.setdefault(key, current)
    return key, mapping


def _profile_in_use(mapping: Mapping[str, str], current_key: str, profile: str, uid: int, table,
                    now: float, current_run: Optional[str],
                    current_evidence: Optional[ProjectEvidence]) -> bool:
    """Whether another run of `profile` may be active in any indexed project. The
    current project's evidence is reused; the rest are read with their own budget."""
    budget = Budget()
    for key in sorted(mapping):
        if key == current_key and current_evidence is not None and current_evidence.readable:
            ev = current_evidence
        else:
            ev = ProjectEvidence.load(mapping[key], uid=uid, attribute=True, table=table,
                                      budget=budget, now=now)
        if not ev.readable:
            if ev.reason == "missing":
                continue
            return True
        for run_id, facts in ev.runs.items():
            if facts.profile == profile and facts.active and not (key == current_key and run_id == current_run):
                return True
        if budget.truncated:
            return True
    return budget.truncated


def plan_sweep(*, dirs, project_root, policy: RetentionPolicy, now: float, uid: int,
               profile: Optional[str] = None, table=None,
               current_run: Optional[str] = None) -> Plan:
    """What the at-launch sweep would remove under `policy`, and what it keeps."""
    profile = profile or Path(str(dirs.root)).name
    root = str(dirs.root)
    remove: List[Item] = []
    keep: List[Kept] = []
    if policy.empty:
        return Plan(profile, "sweep")
    # Each class of storage walks under its own budget so one large tree cannot
    # keep the others from ever being reached.
    budgets: List[Budget] = []

    def fresh() -> Budget:
        budgets.append(Budget())
        return budgets[-1]

    def young(days: Optional[int]) -> Optional[float]:
        return None if days is None else now - days * DAY_S

    evidence_budget = fresh()
    current_key, mapping = _stores(dirs, project_root)

    evidence: Dict[str, ProjectEvidence] = {}

    def ev_for(key: str) -> Optional[ProjectEvidence]:
        if key not in mapping:
            return None
        if key not in evidence:
            evidence[key] = ProjectEvidence.load(
                mapping[key], uid=uid, attribute=(key == current_key), table=table,
                budget=evidence_budget, now=now)
        return evidence[key]

    def days_for(kind: str, key: Optional[str]) -> Optional[int]:
        scope = policy.project if key == current_key else policy.wide
        return _days(scope.get(kind))

    # runs of the current project
    cur = ev_for(current_key)
    runs_days = days_for("runs_days", current_key)
    if runs_days is not None and cur is not None and cur.readable:
        items, kept = plan_store_runs(mapping[current_key], uid=uid, budget=fresh())
        keep.extend(kept)
        for item in items:
            facts = cur.runs.get(item.unit)
            if facts is None or (facts.profile is None and not facts.empty_orphan):
                keep.append(Kept(item.display, "unattributable"))
                continue
            if facts.profile is not None and facts.profile != profile:
                continue
            if item.unit == current_run or facts.active:
                keep.append(Kept(item.display, "run-active"))
                continue
            ref = cur.referenced.get(item.unit)
            if ref is not None and ref[0] in cur.unfinished_tasks:
                keep.append(Kept(item.display, "evidence-referenced"))
                continue
            _decide([[item]], runs_days, now, remove, keep)
    elif runs_days is not None and cur is not None and not cur.readable and cur.reason != "missing":
        keep.append(Kept("run-store", "store-unavailable"))

    # tests, workflow, snapshots, keyed by project
    for cls in ("tests", "workflow", "snapshots"):
        for key in _names(root, (cls,), uid):
            shown = "%s/%s" % (cls, key)
            ev = ev_for(key)
            if ev is None:
                keep.append(Kept(shown, "unattributable"))
                continue
            if not ev.readable:
                keep.append(Kept(shown, "store-unavailable"))
                continue
            days = days_for("runs_days", key)
            items, kept = plan_entries(root, (cls, key), cls=cls, uid=uid, budget=fresh(),
                                       young_after=young(days))
            keep.extend(kept)
            for item in items:
                if cls == "snapshots":
                    if ev.live_lock_tasks:
                        keep.append(Kept(item.display, "live-lock"))
                        continue
                elif item.rel_parts[-1] in ev.unfinished_tasks:
                    keep.append(Kept(item.display, "task-unfinished"))
                    continue
                _decide([[item]], days, now, remove, keep)

    # records
    records_days = days_for("runs_days", None)
    items, kept = plan_entries(root, ("records",), cls="records", uid=uid, budget=fresh(),
                               young_after=young(records_days))
    keep.extend(kept)
    _decide([[i] for i in items if _START_RECORD_RE.match(i.rel_parts[-1])], records_days, now, remove, keep)

    # cache and tmp
    cache_days = days_for("cache_days", None)
    if cache_days is not None:
        if _profile_in_use(mapping, current_key, profile, uid, table, now, current_run,
                           ev_for(current_key)):
            keep.append(Kept("cache", "profile-in-use"))
            keep.append(Kept("tmp", "profile-in-use"))
        else:
            top, kept = plan_entries(root, ("cache",), cls="cache", uid=uid, budget=fresh(),
                                     skip=("opencode", "npm"), young_after=young(cache_days))
            keep.extend(kept)
            units = _group_manifest(top)
            for name in ("opencode", "npm"):
                sub, kept = plan_entries(root, ("cache", name), cls="cache", uid=uid, budget=fresh(),
                                         young_after=young(cache_days))
                keep.extend(kept)
                units.extend(_group_manifest(sub))
            _decide(units, cache_days, now, remove, keep)
            tfiles, kept = _tmp_files(root, uid, fresh())
            keep.extend(kept)
            _decide([[i] for i in tfiles], cache_days, now, remove, keep)

    # logs and crash files
    log_parts = ("data", "opencode", "log")
    logs, kept = plan_entries(root, log_parts, cls="logs", uid=uid, budget=fresh())
    keep.extend(kept)
    crash_units, log_units = [], []
    for item in logs:
        if item.kind != "file":
            continue
        if fnmatch.fnmatchcase(item.rel_parts[-1], "heap-*.heapsnapshot"):
            crash_units.append([Item("crash", item.unit, item.display, item.rel_parts, item.kind,
                                     item.files, item.bytes, item.newest_mtime, item.exclude, item.root, item.root_id)])
        else:
            log_units.append([item])
    _decide(crash_units, days_for("crash_days", None), now, remove, keep)
    _decide(log_units, days_for("logs_days", None), now, remove, keep)

    # exports
    exports_days = days_for("exports_days", None)
    items, kept = plan_entries(root, ("exports",), cls="exports", uid=uid, budget=fresh(),
                               young_after=young(exports_days))
    keep.extend(kept)
    _decide([[i] for i in items], exports_days, now, remove, keep)

    return Plan(profile, "sweep", tuple(remove), tuple(keep), (), (), any(b.truncated for b in budgets))


def sweep(*, dirs, project_root, policy: RetentionPolicy, now: float, uid: int,
          profile: Optional[str] = None, table=None, current_run: Optional[str] = None) -> Dict:
    """Plan and execute the at-launch sweep. Never raises."""
    try:
        if policy is None or policy.empty:
            return {"status": "skipped", "removed": {}, "kept_evidence": 0, "kept_other": 0,
                    "failed": 0, "truncated": False}
        plan = plan_sweep(dirs=dirs, project_root=project_root, policy=policy, now=now, uid=uid,
                          profile=profile, table=table, current_run=current_run)
        result = execute(plan, uid=uid)
        tmp_days = _days(policy.wide.get("cache_days"))
        if tmp_days is not None and any(i.cls == "tmp" for i in result.removed):
            _prune_empty_dirs(str(dirs.root), uid, tmp_days, now, Budget())
        removed: Dict[str, int] = {}
        for item in result.removed:
            removed[item.cls] = removed.get(item.cls, 0) + 1
        evidence = sum(1 for k in plan.keep if k.reason in
                       ("evidence-referenced", "task-unfinished", "run-active", "live-lock", "store-unavailable"))
        return {"status": "done", "removed": removed, "kept_evidence": evidence,
                "kept_other": len(plan.keep) - evidence, "failed": len(result.failed),
                "truncated": plan.truncated}
    except Exception:  # noqa: BLE001 - a sweep never refuses a launch
        return {"status": "error", "removed": {}, "kept_evidence": 0, "kept_other": 0,
                "failed": 0, "truncated": False}


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------

PURGE_MESSAGES = {
    "purge-inside-launch": "purge cannot run inside a launched OpenCode session",
    "purge-profile-invalid": "the profile name is not valid",
    "purge-profile-unsafe": "the profile storage directory is not a private directory owned by this user",
    "purge-run-active": "a run of this profile is active; stop it before purging",
    "purge-confirm-required": "purge deletes data; re-run with --yes to delete, or --dry-run to preview",
}
KEPT_TOP_LEVEL = {
    profile_dirs.STAMP_NAME: "profile-stamp",
    profile_dirs.INDEX_NAME: "project-index",
    profile_dirs.INDEX_NAME + ".lock": "project-index",
}
CREDENTIAL_EXCLUDES = {
    "data": ("opencode/auth.json", "opencode/mcp-auth.json"),
    "state": ("npmrc",),
}
_TOP_CLASS = {
    "data": "profile-data", "state": "profile-state", "cache": "cache", "tmp": "tmp",
    "tests": "tests", "workflow": "workflow", "snapshots": "snapshots", "records": "records",
    "exports": "exports",
}


@dataclass(frozen=True)
class Blocker:
    run_id_or_task: str
    project: str
    reason: str
    pid_file: Optional[str] = None


class PurgeRefused(Exception):
    def __init__(self, code: str, blocking: Sequence[Blocker] = ()) -> None:
        super().__init__(PURGE_MESSAGES.get(code, code))
        self.code = code
        self.message = PURGE_MESSAGES.get(code, code)
        self.blocking: Tuple[Blocker, ...] = tuple(blocking)


def plan_purge(*, adapter_root, profile: str, extra_project=None, env: Mapping[str, str],
               uid: int, table, now: float) -> Plan:
    """Everything a purge of `profile` would remove, or a refusal."""
    if paths.ENV_STATE_DIR in env:
        raise PurgeRefused("purge-inside-launch")
    if (not isinstance(profile, str) or not install.PROFILE_RE.fullmatch(profile)
            or profile in profile_dirs.RESERVED_NAMES):
        raise PurgeRefused("purge-profile-invalid")
    root = str(profile_dirs.profile_root(adapter_root, profile))
    if not os.path.lexists(root):
        return Plan(profile, "purge")
    try:
        os.close(_open_root(root, uid))
    except RootUnsafe:
        raise PurgeRefused("purge-profile-unsafe") from None
    dirs = profile_dirs.ProfileDirs.at(root)
    try:
        indexed = profile_dirs.read_index(dirs)
    except profile_dirs.ProfileDirUnsafe:
        raise PurgeRefused("purge-profile-unsafe") from None

    stores: List[str] = []
    for item in list(indexed) + ([os.path.realpath(str(extra_project))] if extra_project else []):
        real = os.path.realpath(item)
        if real not in stores:
            stores.append(real)

    budgets: List[Budget] = []
    not_visited: List[Dict[str, str]] = []
    visited: List[Tuple[str, ProjectEvidence]] = []
    blockers: List[Blocker] = []
    for project in stores:
        budget = Budget()
        ev = ProjectEvidence.load(project, uid=uid, attribute=True, table=table, budget=budget, now=now)
        if budget.truncated:
            budgets.append(budget)
            not_visited.append({"project": project, "reason": "walk-truncated"})
            continue
        if not ev.readable:
            if ev.reason != "missing":
                not_visited.append({"project": project, "reason": ev.reason or "unreadable"})
            continue
        visited.append((project, ev))
        mine = {rid for rid, f in ev.runs.items() if f.profile == profile}
        for run_id in sorted(mine):
            reason = ev.runs[run_id].reason
            if reason is not None:
                blockers.append(Blocker(run_id, project, reason))
        for task in sorted(ev.live_lock_tasks):
            if any(ev.referenced.get(rid, (None,))[0] == task for rid in mine):
                blockers.append(Blocker(task, project, "live-lock",
                                        ".workflow_artifacts/memory/run-supervisor-%s.pid" % task))
    if blockers:
        raise PurgeRefused("purge-run-active", blockers)

    remove: List[Item] = []
    keep: List[Kept] = []
    referenced: List[Dict[str, str]] = []
    root_fd = _open_root(root, uid)
    try:
        names = sorted(e.name for e in os.scandir(root_fd))
    finally:
        os.close(root_fd)
    for name in names:
        if name in KEPT_TOP_LEVEL:
            keep.append(Kept(name, KEPT_TOP_LEVEL[name]))
            continue
        excludes = CREDENTIAL_EXCLUDES.get(name, ())
        # Deleting must not depend on how much there is to measure: the totals are
        # a lower bound when the walk budget runs out, and the unit is removed anyway.
        unit_budget = Budget()
        budgets.append(unit_budget)
        root_fd = _open_root(root, uid)
        try:
            cls = _TOP_CLASS.get(name, "compiled" if re.fullmatch(r"[0-9a-f]{16}", name) else "other")
            item, why = unit_item(root_fd, root, (), name, cls=cls, uid=uid, budget=unit_budget,
                                  exclude=excludes, skip_foreign=True, measure_only=True)
        finally:
            os.close(root_fd)
        if item is not None:
            remove.append(item)
            keep.extend(Kept(shown, reason) for shown, reason in item.skipped)
        if why is not None:
            keep.append(why)
        for rel in excludes:
            if os.path.lexists(os.path.join(root, name, *rel.split("/"))):
                keep.append(Kept("%s/%s" % (name, rel), "credential"))

    for project, ev in visited:
        try:
            items, kept = plan_store_runs(project, uid=uid, budget=Budget(None))
        except (RootUnsafe, runstore.RunStoreError, OSError) as exc:
            not_visited.append({"project": project,
                                "reason": "unsafe" if isinstance(exc, (RootUnsafe, runstore.RunStoreError))
                                else "unreadable"})
            continue
        keep.extend(kept)
        for item in items:
            facts = ev.runs.get(item.unit)
            if facts is None or (facts.profile is None):
                keep.append(Kept(item.display, "unattributable"))
                continue
            if facts.profile != profile:
                continue
            remove.append(item)
            ref = ev.referenced.get(item.unit)
            if ref is not None:
                referenced.append({"run_id": item.unit, "task": ref[0], "project": project,
                                   "referenced_by": ref[1]})
    return Plan(profile, "purge", tuple(remove), tuple(keep), tuple(referenced),
                tuple(not_visited), any(b.truncated for b in budgets))


def purge_incomplete(plan: Plan) -> bool:
    """Whether the plan leaves behind anything a purge is meant to delete."""
    return any(k.reason in PURGE_INCOMPLETE_REASONS for k in plan.keep)
