"""Manifest schema chain, legacy-prototype harvest, and state-store migration.

Everything here is roll-forward only and idempotent: a step that is repeated
after an interruption converges on the same state. Files are written 0600 and
directories 0700, each write is atomic (temp file in the same directory, then
rename), and a record is copied before its source is deleted so a crash can
leave a duplicate but never lose one.
"""
import copy
import fcntl
import json
import os
import stat
import tempfile
import time

from . import hooks_merge, jsonfile, manifest, prereqs

ADOPTION_FROM = 0
LOG_CAP = 256 * 1024
LOCK_WAIT_S = 2.0
SCHEMA_MIGRATIONS = []  # ordered (from_schema, to_schema, fn(data) -> data)


class MigrationError(Exception):
    pass


# ------------------------------------------------------------------ manifest schema chain

def migrate_manifest(data):
    """Apply the registered chain until `data` reaches the current schema."""
    schema = data.get("schema") if isinstance(data, dict) else None
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise MigrationError("manifest schema is missing or not an integer")
    if schema > manifest.SCHEMA:
        raise MigrationError("manifest schema %d is newer than this package understands (%d); upgrade quoin"
                             % (schema, manifest.SCHEMA))
    data = copy.deepcopy(data)
    while schema < manifest.SCHEMA:
        step = next((s for s in SCHEMA_MIGRATIONS if s[0] == schema), None)
        if step is None:
            raise MigrationError("no manifest migration from schema %d" % schema)
        _, target, fn = step
        data = fn(data)
        data["schema"] = target
        schema = target
    return data


# ------------------------------------------------------------------ legacy harvest

def _command_iter(data):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return
    for lst in hooks.values():
        if not isinstance(lst, list):
            continue
        for group in lst:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                for hook in group["hooks"]:
                    if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                        yield hook["command"]


def harvest_legacy(paths, claude_data, codex_data):
    """Interpreter and node the prototype was using, read from its hook commands and launch script."""
    names = manifest.runtime_names(paths)
    python = None
    for data in (claude_data, codex_data):
        for command in _command_iter(data):
            if hooks_merge.classify(command, runtime_names=names) == "legacy":
                python = prereqs.parse_legacy_python(command)
                break
        if python:
            break
    node = None
    try:
        with open(os.path.join(paths.plugin_dir, "launch.sh"), "r") as handle:
            node = prereqs.parse_launch_sh(handle.read(1 << 16))
    except OSError:
        pass
    return {"python": python, "node": node}


def legacy_owned(claude_data, codex_data, names):
    """Events whose only entries are the prototype's, per runtime (they may be removed on uninstall)."""
    return {
        "claude": hooks_merge.legacy_only_events(claude_data, "claude", runtime_names=names),
        "codex": hooks_merge.legacy_only_events(codex_data, "codex", runtime_names=names),
    }


def codex_detected(paths, environ, which, codex_data, manifest_data):
    found = which("codex")
    if isinstance(found, str) and os.path.isabs(found):
        return True, "codex found at %s" % found
    if environ.get("CODEX_HOME"):
        return True, "CODEX_HOME is set"
    if os.path.isdir(paths.codex_home):
        return True, "%s exists" % paths.codex_home
    names = manifest.runtime_names(paths)
    for command in _command_iter(codex_data):
        if hooks_merge.classify(command, runtime_names=names) in ("legacy", "managed"):
            return True, "Codex hooks already contain Quoin Stream Deck entries"
    if isinstance(manifest_data, dict) and any(
            isinstance(h, dict) and h.get("runtime") == "codex" for h in manifest_data.get("hooks") or []):
        return True, "the install manifest records Codex hooks"
    return False, "Codex not detected"


# ------------------------------------------------------------------ state migration helpers

def _atomic_write(path, obj):
    """Write JSON atomically with mode 0600 (temp file in the same directory)."""
    directory = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(obj) if not isinstance(obj, str) else obj)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _read_record(path):
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _json_names(directory):
    try:
        return sorted(n for n in os.listdir(directory) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return []


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class _Locks:
    """Hold the given lock files (non-blocking, retried) for the duration of a `with` block."""

    def __init__(self, paths, create, deadline=None):
        self.paths = [(p, c) for p, c in zip(paths, create)]
        self.deadline = deadline
        self.fds = []
        self.ok = False

    def __enter__(self):
        deadline = self.deadline if self.deadline is not None else time.monotonic() + LOCK_WAIT_S
        for path, create in self.paths:
            if not create and not os.path.lexists(path):
                continue
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600) if create \
                    else os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
            except OSError:
                self._release()
                return self
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        self._release()
                        return self
                    time.sleep(0.02)
            self.fds.append(fd)
        self.ok = True
        return self

    def _release(self):
        while self.fds:
            try:
                os.close(self.fds.pop())
            except OSError:
                pass

    def __exit__(self, *exc):
        self._release()
        return False


def _legacy_lock(paths):
    return os.path.join(paths.state_root, "monitor.lock")


def _new_lock(paths):
    return os.path.join(paths.state, "monitor.lock")


# ------------------------------------------------------------------ state directories

def ensure_state_dirs(paths):
    """Create the state root, `state/` and `state/agents/` (0700); refuse directories owned by someone else."""
    notes = []
    for directory in (paths.state_root, paths.state, paths.agents_dir):
        if os.path.lexists(directory):
            # The state root may be a symlink the user set up (dotfile managers do); the two directories this
            # component creates inside it must be real directories.
            own = directory != paths.state_root
            info = os.lstat(directory) if own else os.stat(directory)
            if own and (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)):
                raise MigrationError("%s is a symlink (or not a directory); refusing to use it" % directory)
            if not stat.S_ISDIR(info.st_mode):
                raise MigrationError("%s is not a directory; refusing to use it" % directory)
            if info.st_uid != os.getuid():
                raise MigrationError("%s is owned by another user; refusing to use it" % directory)
            if own and (info.st_mode & 0o077):
                os.chmod(directory, 0o700)
                notes.append("tightened permissions on %s to 0700" % directory)
            continue
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    return notes


# ------------------------------------------------------------------ agent records

def merge_agents(paths, now=None):
    """Move legacy `state_root/agents/*.json` records into `state/agents/`; newest `updated` wins."""
    report = {"moved": [], "dropped": [], "left": [], "skipped": []}
    legacy_dir = os.path.join(paths.state_root, "agents")
    deadline = time.monotonic() + LOCK_WAIT_S  # one wait for the whole pass, not one per record
    for name in _json_names(legacy_dir):
        source = os.path.join(legacy_dir, name)
        target = os.path.join(paths.agents_dir, name)
        with _Locks([_legacy_lock(paths), _new_lock(paths)], [False, True], deadline) as locks:
            if not locks.ok:
                report["skipped"].append(name)
                continue
            record = _read_record(source)
            if record is None:
                report["left"].append(name)
                continue
            existing = _read_record(target) if os.path.lexists(target) else None
            existing_updated = existing.get("updated") if existing else None
            incoming_updated = record.get("updated")
            if existing is None or not _number(existing_updated) or (
                    _number(incoming_updated) and incoming_updated > existing_updated):
                copy_record = dict(record)
                copy_record.setdefault("socket_inferred", True)
                _atomic_write(target, copy_record)
                report["moved"].append(name)
            else:
                report["dropped"].append(name)
            try:
                os.unlink(source)
            except FileNotFoundError:
                pass
    return report


def reverse_sweep(paths):
    """Adoption rollback: copy newer or unmatched new-layout records back to the prototype's directory."""
    report = {"copied": [], "skipped": []}
    legacy_dir = os.path.join(paths.state_root, "agents")
    names = _json_names(paths.agents_dir)
    if not names:
        return report
    with _Locks([_legacy_lock(paths)], [False]) as locks:
        if not locks.ok:
            report["skipped"] = list(names)
            return report
        os.makedirs(legacy_dir, mode=0o700, exist_ok=True)
        for name in names:
            record = _read_record(os.path.join(paths.agents_dir, name))
            if record is None:
                continue
            counterpart = os.path.join(legacy_dir, name)
            old = _read_record(counterpart) if os.path.lexists(counterpart) else None
            old_updated = old.get("updated") if old else None
            if old is not None and _number(old_updated) and not (
                    _number(record.get("updated")) and record["updated"] > old_updated):
                continue
            _atomic_write(counterpart, record)
            report["copied"].append(name)
    return report


def collect_agents(paths):
    """Metadata of every live-looking record in both layouts (for restart and purge messages)."""
    found, seen = [], set()
    for directory, legacy in ((paths.agents_dir, False), (os.path.join(paths.state_root, "agents"), True)):
        for name in _json_names(directory):
            if name in seen:
                continue
            seen.add(name)
            record = _read_record(os.path.join(directory, name))
            if record is None:
                continue
            via = record.get("via")
            found.append({"kind": record.get("kind"), "session": record.get("session"),
                          "pane": record.get("pane"), "via": via if via else ("legacy" if legacy else "managed")})
    return found


# ------------------------------------------------------------------ config split and logs

def split_surfaces(paths, socket_dir):
    """Move `config.json` `surfaces` into `state/surfaces.json` keyed by socket; write the new file first."""
    report = {"moved": 0, "skipped": None}
    try:
        data, _ = jsonfile.read(paths.config)
    except jsonfile.MalformedJSON as exc:
        report["skipped"] = str(exc)
        return report
    surfaces = data.get("surfaces")
    if not isinstance(surfaces, dict):
        return report
    target = os.path.join(paths.state, "surfaces.json")
    current = _read_record(target) or {}
    merged = dict(current)
    for session, surface in surfaces.items():
        merged.setdefault(socket_dir + "\t" + session, surface)
    if merged != current or not os.path.lexists(target):
        _atomic_write(target, merged)
    report["moved"] = len(surfaces)

    def drop(config):
        config.pop("surfaces", None)
        return config

    jsonfile.update(paths.config, drop)
    return report


def move_errors_log(paths):
    """Append the prototype's error log to the new one (bounded) and delete the old file."""
    source = os.path.join(paths.state_root, "errors.log")
    if not os.path.lexists(source):
        return False
    target = os.path.join(paths.state, "errors.log")
    try:
        with open(source, "rb") as handle:
            legacy = handle.read()
    except OSError:
        return False
    try:
        with open(target, "rb") as handle:
            existing = handle.read()
    except OSError:
        existing = b""
    combined = (existing + legacy)[-LOG_CAP:]
    directory = os.path.dirname(target)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".errors.log.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(combined)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    os.unlink(source)
    return True
