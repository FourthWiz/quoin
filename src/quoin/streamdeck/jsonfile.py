"""Refuse-malformed JSON reads and atomic compare-and-swap writes.

A file that does not parse is never rewritten. A write re-reads the target just
before the rename and re-merges when another writer got there first, so an
edit made by another tool between our read and our replace is not lost.
"""
import copy
import datetime
import json
import os
import re
import shutil
import uuid


class MalformedJSON(Exception):
    def __init__(self, path, detail):
        self.path = path
        self.detail = detail
        super().__init__("%s is not valid JSON (%s); it was left unchanged" % (path, detail))


class ConcurrentModification(Exception):
    def __init__(self, path):
        self.path = path
        super().__init__("%s changed repeatedly while it was being updated; "
                         "the other writer's version was left in place" % path)


class _Conflict(Exception):
    pass


def read(path):
    """Return (data, raw). Absent file gives ({}, None)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {}, None
    if not raw.strip():
        raise MalformedJSON(path, "empty file")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedJSON(path, str(exc))
    if not isinstance(data, dict):
        raise MalformedJSON(path, "top level is not an object")
    return data, raw


def _current_bytes(target):
    try:
        with open(target, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path, data, *, expected_raw, default_mode=0o600, create_parents=False, _before_replace=None):
    """Write `data` unless the file no longer equals `expected_raw` (None = absent)."""
    target = os.path.realpath(path)
    directory = os.path.dirname(target)
    if create_parents:
        os.makedirs(directory, exist_ok=True)
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        mode = os.stat(target).st_mode & 0o7777
    except FileNotFoundError:
        mode = default_mode
    temp = os.path.join(directory, ".%s.quoin-%d-%s.tmp" % (os.path.basename(target), os.getpid(), uuid.uuid4().hex[:8]))
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if _before_replace is not None:
            _before_replace()
        if _current_bytes(target) != expected_raw:
            raise _Conflict()
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(directory)
    return True


def update(path, mutate, *, retries=3, **kwargs):
    """Apply `mutate(copy_of_data) -> new_data`. Returns (changed, result)."""
    conflicts = 0
    while True:
        data, raw = read(path)
        new = mutate(copy.deepcopy(data))
        if new == data:
            return False, data
        try:
            write_atomic(path, new, expected_raw=raw, **kwargs)
            return True, new
        except _Conflict:
            conflicts += 1
            if conflicts >= retries:
                raise ConcurrentModification(path)


_BACKUP_NAME = re.compile(r"^(quoin-\d{8}T\d{6}Z)(?:-(\d+))?$")


def _backup_order(entry):
    """Sort key for backup folders: timestamp, then the numeric suffix (a bare folder counts as 1)."""
    match = _BACKUP_NAME.match(entry)
    if not match:
        return (entry, 0)
    return (match.group(1), int(match.group(2)) if match.group(2) else 1)


def backup(path, backups_root, *, now, keep=5):
    """Copy `path` into backups_root/quoin-<UTC ts>[-NN]/ and prune old quoin-* dirs.

    Backups taken in the same second share one folder unless that folder already
    holds a file with this basename; then the first free `-02`, `-03`, ... folder
    is used, so nothing is overwritten and names sort in creation order.
    """
    if not os.path.isfile(path):
        return None
    stamp = now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") if now.tzinfo else now.strftime("%Y%m%dT%H%M%SZ")
    base = "quoin-" + stamp
    name = os.path.basename(path)
    os.makedirs(backups_root, exist_ok=True)
    same_second = sorted((entry for entry in os.listdir(backups_root)
                          if (entry == base or entry.startswith(base + "-"))
                          and os.path.isdir(os.path.join(backups_root, entry))), key=_backup_order)
    if not same_second:
        folder = os.path.join(backups_root, base)
    else:
        last = same_second[-1]
        if os.path.lexists(os.path.join(backups_root, last, name)):
            suffix = int(last.rsplit("-", 1)[1]) if last != base and last.rsplit("-", 1)[1].isdigit() else 1
            folder = os.path.join(backups_root, "%s-%02d" % (base, suffix + 1))
        else:
            folder = os.path.join(backups_root, last)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, name)
    shutil.copy2(path, dest)
    ours = sorted((entry for entry in os.listdir(backups_root)
                   if entry.startswith("quoin-") and os.path.isdir(os.path.join(backups_root, entry))),
                  key=_backup_order)
    for entry in ours[:-keep] if keep > 0 else ours:
        shutil.rmtree(os.path.join(backups_root, entry), ignore_errors=True)
    return dest
