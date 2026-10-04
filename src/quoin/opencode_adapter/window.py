"""Saved pre-run listings for coordinator phases that may be resumed.

A phase resumed in a new invocation would otherwise take its pre-run listing
after the run began, so the boundary window would be partial and the entry's
boundary unrecorded. The store keeps the listing taken before the first
launch, bound to the run id, together with the project files the coordinator
itself wrote while the run was open, so the resumed run is judged against the
same starting point as an uninterrupted one.

The files live under the adapter state directory, outside the project, so a
project-confined agent cannot forge them; the trust limit is the one test
results and snapshots already have: a file under the user's state directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from . import boundaries, jsonio, paths, runstore

MAX_FILE_BYTES = 64 * 1024 * 1024
PENDING_NAME = "pending.json"
_VERSION = 1


class WindowError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _private_dirs(base: Path, *parts: str) -> Path:
    current = Path(base)
    for part in parts:
        current = current / part
        try:
            os.mkdir(str(current), 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise WindowError("state-dir-unsafe", "the window directory could not be created") from None
        info = os.lstat(str(current))
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise WindowError("state-dir-unsafe", "the window directory is not a plain directory of this user")
        if info.st_mode & 0o077:
            os.chmod(str(current), 0o700)
    return current


def _ensure_root(state_root: Path) -> None:
    missing = []
    probe = Path(state_root)
    while not probe.exists():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    for item in reversed(missing):
        try:
            os.mkdir(str(item), 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise WindowError("state-dir-unsafe", "the state directory could not be created") from None


class WindowStore:
    """The window files of one task under `state_root/workflow/PROJECT_KEY/TASK/`."""

    def __init__(self, state_root, project_root, task: str) -> None:
        self.state_root = Path(state_root)
        self.project_root = Path(project_root)
        self.task = runstore.check_task_name(task)
        self.key = paths.project_key(self.project_root)

    # -- locations ----------------------------------------------------------
    def directory(self, *, create: bool = False) -> Path:
        if create:
            _ensure_root(self.state_root)
            return _private_dirs(self.state_root, "workflow", self.key, self.task)
        return self.state_root / "workflow" / self.key / self.task

    def _path(self, name: str, *, create: bool = False) -> Path:
        return self.directory(create=create) / name

    def _run_name(self, run_id: str) -> str:
        return "%s.json" % runstore.check_run_id(run_id)

    # -- file io ------------------------------------------------------------
    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        got = jsonio.read_regular_bytes(path, max_bytes=MAX_FILE_BYTES)
        if got is None:
            return None
        try:
            data = json.loads(got[0].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("version") == _VERSION else None

    def _write(self, path: Path, data: Mapping[str, Any]) -> None:
        raw = (json.dumps(data, sort_keys=True) + "\n").encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise WindowError("window-too-large", "the saved listing exceeds the size cap")
        try:
            jsonio.write_private_atomic(path, raw)
        except OSError:
            raise WindowError("state-dir-unsafe", "the window file could not be written") from None

    # -- operations ---------------------------------------------------------
    def save_pending(self, stage: Any, phase: str, listing: boundaries.Listing) -> None:
        """Store the listing taken before launch; replaces an earlier pending one."""
        self.directory(create=True)
        self._write(self._path(PENDING_NAME), {
            "version": _VERSION, "stage": None if stage is None else str(stage), "phase": phase,
            "listing": boundaries.listing_to_json(listing), "writes": {},
        })

    def bind(self, run_id: str) -> bool:
        """Name the pending file after `run_id`. False when nothing was pending."""
        target = self._path(self._run_name(run_id))
        source = self._path(PENDING_NAME)
        if self._read(source) is None:
            return False
        try:
            os.replace(str(source), str(target))
        except OSError:
            return False
        return True

    def note_write(self, run_id: str, rel_path: str, sha256: str) -> bool:
        """Record a project file the coordinator wrote while `run_id` was open."""
        if not isinstance(rel_path, str) or not rel_path or rel_path.startswith("/") or ".." in rel_path.split("/"):
            return False
        path = self._path(self._run_name(run_id))
        data = self._read(path)
        if data is None:
            return False
        writes = data.get("writes")
        if not isinstance(writes, dict):
            return False
        writes[rel_path] = sha256
        data["writes"] = writes
        self._write(path, data)
        return True

    def load(self, run_id: str) -> Optional[Tuple[boundaries.Listing, Dict[str, str]]]:
        data = self._read(self._path(self._run_name(run_id)))
        if data is None:
            return None
        listing = boundaries.listing_from_json(data.get("listing"))
        writes = data.get("writes")
        if listing is None or not isinstance(writes, dict):
            return None
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in writes.items()):
            return None
        return listing, dict(writes)

    def drop(self, run_id: str) -> None:
        try:
            os.unlink(str(self._path(self._run_name(run_id))))
        except OSError:
            pass


def _disk_sha(project_root, rel: str) -> Optional[str]:
    got = jsonio.read_regular_bytes(Path(project_root) / rel, max_bytes=MAX_FILE_BYTES)
    return hashlib.sha256(got[0]).hexdigest() if got else None


def reconcile(
    before: boundaries.Listing, after: boundaries.Listing, writes: Mapping[str, str], project_root,
) -> boundaries.Listing:
    """A copy of `before` where each noted path whose current content is the
    last content the coordinator wrote takes its `after` entry, so those
    writes pass while any other change to the same paths still differs."""
    entries = dict(before.entries)
    for rel, noted in writes.items():
        current = after.entries.get(rel)
        if current is None or current[0] != "f":
            continue
        sha = current[3] if current[3] is not None else _disk_sha(project_root, rel)
        if sha == noted:
            entries[rel] = current
    return boundaries.Listing(
        entries=entries, repos=before.repos, live_task_locks=before.live_task_locks,
        taken_at=before.taken_at, truncated=before.truncated, error=before.error,
    )
