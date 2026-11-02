"""A short-lived result cache bound to one launcher profile.

Entries live under the stamped profile root's `cache/workctl/` directory
(mode 700, files 600) and expire after the configured TTL. With no stamped
profile root there is no cache, so results never land in a location shared
between profiles. Keys hash the profile, service, tenant, operation and
normalized parameters; entry names reveal nothing about the query.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from ..opencode_adapter import jsonio

MAX_ENTRY_BYTES = 2 * 1_048_576
CACHE_FORMAT = 1


def key(profile: str, service: str, tenant: str, op_name: str, params: Mapping[str, Any]) -> str:
    material = json.dumps(
        {"p": profile, "s": service, "t": tenant, "o": op_name, "a": dict(params)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Cache:
    def __init__(self, directory: Path, ttl: int, *, clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self.ttl = ttl
        self._clock = clock

    def _path(self, entry_key: str) -> Path:
        return self.directory / ("%s.json" % entry_key)

    def get(self, entry_key: str) -> Optional[Dict[str, Any]]:
        if self.ttl <= 0:
            return None
        got = jsonio.read_regular_bytes(self._path(entry_key), max_bytes=MAX_ENTRY_BYTES)
        if got is None:
            return None
        raw, info = got
        if info.st_uid != os.getuid():
            return None
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("format") != CACHE_FORMAT:
            return None
        stored = entry.get("stored_at")
        if not isinstance(stored, (int, float)) or not (0 <= self._clock() - stored < self.ttl):
            return None
        value = entry.get("value")
        return value if isinstance(value, dict) else None

    def put(self, entry_key: str, value: Dict[str, Any]) -> None:
        if self.ttl <= 0:
            return
        body = json.dumps(
            {"format": CACHE_FORMAT, "stored_at": self._clock(), "value": value}, separators=(",", ":")
        ).encode("utf-8")
        if len(body) > MAX_ENTRY_BYTES:
            return
        try:
            jsonio.ensure_private_directory(self.directory)
            jsonio.write_private_atomic(self._path(entry_key), body)
        except (jsonio.UnsafeDirectoryError, OSError):
            return
        self.purge()

    def purge(self) -> None:
        """Remove expired entries; failures are ignored."""
        now = self._clock()
        try:
            names = os.listdir(str(self.directory))
        except OSError:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            path = self.directory / name
            try:
                if now - path.lstat().st_mtime >= self.ttl:
                    path.unlink()
            except OSError:
                continue
