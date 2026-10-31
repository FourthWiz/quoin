"""Retention for OpenCode profile storage: a descriptor-based deletion engine.

Every walk and every removal works relative to an open directory descriptor
and refuses to follow symbolic links, so a path component swapped for a link
after planning cannot redirect a removal outside the planned root. Entries
owned by another user are never entered or removed.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import runstore

CLASSES = (
    "runs", "tests", "workflow", "snapshots", "records", "cache", "tmp", "logs",
    "crash", "exports", "profile-data", "profile-state", "compiled", "other",
)
KINDS = ("file", "tree", "run")
KEPT_REASONS = (
    "credential", "profile-stamp", "project-index", "unattributable", "foreign-owner",
    "evidence-referenced", "task-unfinished", "run-active", "live-lock", "store-unavailable",
    "walk-truncated", "not-aged", "never-swept", "profile-in-use",
)
FAILED_REASONS = ("changed-since-plan", "foreign-owner", "os-error")

WALK_BUDGET = 50000
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
    """Entry budget shared by every walk of one plan."""

    def __init__(self, limit: int = WALK_BUDGET) -> None:
        self.left = limit
        self.truncated = False

    def take(self) -> bool:
        if self.left <= 0:
            self.truncated = True
            return False
        self.left -= 1
        return True


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
    foreign: bool = False


def _measure(dir_fd: int, rel: Tuple[str, ...], uid: int, budget: Budget,
             excl: Set[Tuple[str, ...]], out: _Measure) -> None:
    """Count files, bytes and the newest mtime over files and directories."""
    try:
        entries = sorted(os.scandir(dir_fd), key=lambda e: e.name)
    except OSError:
        out.foreign = True
        return
    for entry in entries:
        if not budget.take():
            return
        try:
            info = os.lstat(entry.name, dir_fd=dir_fd)
        except OSError:
            continue
        if info.st_uid != uid:
            out.foreign = True
            continue
        child = rel + (entry.name,)
        exact, ancestor = _is_excluded_or_ancestor(child, excl)
        if exact:
            continue
        out.newest = max(out.newest, info.st_mtime)
        if stat.S_ISDIR(info.st_mode):
            try:
                sub = _open_child(dir_fd, entry.name, uid)
            except OSError:
                out.foreign = True
                continue
            try:
                _measure(sub, child, uid, budget, excl, out)
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
              exclude: Sequence[str] = ()) -> Tuple[Optional[Item], Optional[Kept]]:
    """Describe one entry under `root/parent_parts` as a removable unit, or say why
    it is kept. Links, FIFOs, sockets and devices are file units and never opened."""
    shown = display or "/".join(parent_parts + (name,))
    try:
        parent = _chain_fd(root_fd, parent_parts, uid)
    except OSError:
        return None, Kept(shown, "foreign-owner")
    try:
        try:
            info = os.lstat(name, dir_fd=parent)
        except OSError:
            return None, None
        if info.st_uid != uid:
            return None, Kept(shown, "foreign-owner")
        if not stat.S_ISDIR(info.st_mode):
            return Item(cls, name, shown, parent_parts + (name,), "file", 1, info.st_size,
                        info.st_mtime, (), root), None
        excl = _excl_parts(exclude)
        measure = _Measure(newest=info.st_mtime)
        try:
            sub = _open_child(parent, name, uid)
        except OSError:
            return None, Kept(shown, "foreign-owner")
        try:
            _measure(sub, (), uid, budget, excl, measure)
        finally:
            os.close(sub)
        if measure.foreign:
            return None, Kept(shown, "foreign-owner")
        if budget.truncated:
            return None, Kept(shown, "walk-truncated")
        return Item(cls, name, shown, parent_parts + (name,), "tree", measure.files, measure.bytes,
                    measure.newest, tuple(exclude), root), None
    finally:
        os.close(parent)


def plan_entries(root: str, parent_parts: Tuple[str, ...], *, cls: str, uid: int, budget: Budget,
                 excludes: Optional[Dict[str, Sequence[str]]] = None,
                 skip: Sequence[str] = ()) -> Tuple[List[Item], List[Kept]]:
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
                                  exclude=(excludes or {}).get(name, ()))
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
                              sum(i.st_size for i in infos), max(i.st_mtime for i in infos), (), root))
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
