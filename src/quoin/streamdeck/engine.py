"""Install, upgrade and uninstall pipeline for the optional Stream Deck component.

The engine is the only writer of the component's files. It is built around three
ideas:

* every outside effect goes through an injected seam (`ctx_factory` for the
  prerequisite probes and installers, `sysops` for `pgrep` and `open`), and the
  real implementations live in `default_sysops` / `_real_probe` / `_real_run`;
* an operation marker (`operation.json`) is an intent log. A run that started
  from a clean tree writes `phase: staging` and is rolled back on failure; any
  run that finds a committed, uninstall or unreadable marker is a *completing
  run*, writes `phase: committed` from its first write and is never rolled back;
* every step is idempotent, so re-running an interrupted operation converges.

Hook entry points are only ever written after their targets exist, and shims
never exec and always exit 0, so no window exists in which an agent hook can
fail.
"""
import contextlib
import copy
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import (COMPONENT_VERSION, PAYLOAD_DIR, hooks_merge, jsonfile, macos, manifest, migrations,
               payload_build, payload_fingerprint, payload_files, prereqs, shims)

STEPS = ("stage_payload", "set_aside", "swap_current", "write_entry_points", "plugin_swap", "commit",
         "state_migration", "claude_settings", "codex_settings", "finalize")
UNINSTALL_STEPS = ("uninstall:claude_settings", "uninstall:codex_settings", "uninstall:tombstones",
                   "uninstall:remove_files", "uninstall:manifest")
RUNTIME_KINDS = ("claude", "codex")
PATH_FLAGS = ("runtime_dir", "plugins_dir", "state_root", "claude_settings", "codex_home", "agentdesk")
UNREADABLE = {"phase": "unreadable"}
TRACKED_ROOT_SCRIPTS = shims.FORWARDER_NAMES
PROFILE_NAME = "Quoin Live Agents.streamDeckProfile"
SAFE_EXIT = 0


class OperationError(Exception):
    """A refused or failed operation. `code` 1 = refused/failed, 2 = unsupported platform."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SysOps:
    probe: Callable
    run: Callable


@dataclass
class Env:
    paths: Any
    flags: dict = field(default_factory=dict)
    runtimes: Optional[tuple] = None
    prereq_mode: str = "ask"
    dry_run: bool = False
    open_profile: bool = False
    purge: bool = False
    allow_downgrade: bool = False
    ctx_factory: Optional[Callable] = None
    sysops: Optional[SysOps] = None
    environ: dict = field(default_factory=dict)
    now: Any = None
    platform_info: Any = None
    package_version: str = ""
    component_version: str = COMPONENT_VERSION
    payload_dir: str = PAYLOAD_DIR
    out: Callable = print
    faults: dict = field(default_factory=dict)
    on_step: Optional[Callable] = None


@dataclass
class Result:
    exit_code: int
    mode: Optional[str]
    changed: bool
    lines: list
    manifest: Optional[dict]


# ------------------------------------------------------------------ real seams

def _real_probe(argv, timeout=5):
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", type(exc).__name__
    return proc.returncode, proc.stdout, proc.stderr


def _real_run(argv):
    try:
        return subprocess.run(list(argv)).returncode
    except OSError:
        return 127


def default_sysops():
    return SysOps(probe=_real_probe, run=_real_run)


def stream_deck_running(sysops):
    """Read-only `pgrep`; the engine never quits or restarts the app."""
    try:
        rc, _, _ = sysops.probe(["/usr/bin/pgrep", "-x", "Stream Deck"], 5)
    except Exception:
        return False
    return rc == 0


# ------------------------------------------------------------------ small helpers

def _iso(now):
    if now is None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
    if isinstance(now, str):
        return now
    return manifest._stamp(now)


def _vkey(text):
    parts = re.findall(r"\d+", text or "")[:4]
    return tuple(int(p) for p in parts) if parts else (0,)


def _lexists(path):
    return os.path.lexists(path)


def _read_bytes(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _token():
    return uuid.uuid4().hex[:10]


def write_file_atomic(path, data, mode):
    """Write bytes via same-directory temp + fsync + rename; True when content or mode changed."""
    try:
        info = os.lstat(path)
    except OSError:
        info = None
    if info is not None and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == mode \
            and _read_bytes(path) == data:
        return False
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.quoin-%d-%s.tmp" % (os.path.basename(path), os.getpid(), _token()))
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)
    return True


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


def _remove_any(path):
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except OSError:
        pass


def _tree_signature(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                out[rel] = ("link", os.readlink(full))
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                out[rel] = ("file", stat.S_IMODE(info.st_mode), _sha256_file(full))
    return out


def _plugin_uuid(plugin_dir):
    """UUID in the plugin's manifest.json, or None when unreadable / not JSON / not an object."""
    raw = _read_bytes(os.path.join(plugin_dir, "manifest.json"))
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data.get("UUID") if isinstance(data, dict) else None


def _socket_dir(environ, uid=None):
    """Agent Desk's rule, identical to the payload's: ZELLIJ_SOCKET_DIR, else XDG_RUNTIME_DIR or /tmp."""
    explicit = environ.get("ZELLIJ_SOCKET_DIR")
    if isinstance(explicit, str) and explicit:
        return explicit
    base = environ.get("XDG_RUNTIME_DIR")
    if not (isinstance(base, str) and base):
        base = "/tmp"
    return "%s/zellij-agentdesk-%d" % (base, os.getuid() if uid is None else uid)


# ------------------------------------------------------------------ marker model

def well_shaped(marker):
    if not isinstance(marker, dict):
        return False
    op, phase = marker.get("op"), marker.get("phase")
    if not isinstance(op, str) or op not in ("install", "upgrade", "uninstall"):
        return False
    if not isinstance(phase, str) or phase not in ("staging", "committed"):
        return False
    if not isinstance(marker.get("steps_done"), list):
        return False
    if "draft" in marker and not isinstance(marker["draft"], dict):
        return False
    runtimes = marker.get("runtimes")
    if runtimes is not None and not (isinstance(runtimes, list) and all(isinstance(r, str) for r in runtimes)):
        return False
    if phase == "staging" and op != "uninstall" and not isinstance(marker.get("previous"), dict):
        return False
    return True


def classify_marker(marker):
    """none | staging | committed | uninstall | unreadable."""
    if marker is None:
        return "none"
    if not well_shaped(marker):
        return "unreadable"
    if marker["op"] == "uninstall":
        return "uninstall"
    return "staging" if marker["phase"] == "staging" else "committed"


def _hook_key(record):
    return (record.get("runtime"), record.get("settings_path"), record.get("event"))


def union_hooks(*lists):
    """Union of hook records by (runtime, settings_path, event); created_event_key is OR-ed."""
    merged = {}
    for records in lists:
        for record in records or []:
            if not isinstance(record, dict):
                continue
            key = _hook_key(record)
            if key in merged:
                merged[key]["created_event_key"] = bool(merged[key].get("created_event_key")) or bool(
                    record.get("created_event_key"))
            else:
                merged[key] = copy.deepcopy(record)
                merged[key]["created_event_key"] = bool(record.get("created_event_key"))
    return list(merged.values())


@dataclass
class Carry:
    """What a completing run inherits from the marker it found."""
    completing: bool = False
    kind: str = "none"
    marker: Optional[dict] = None
    note: Optional[str] = None
    prev_hooks: list = field(default_factory=list)
    stale: dict = field(default_factory=lambda: {"rollback_dirs": [], "plugin_asides": [], "version_dir_asides": []})
    legacy_owned_events: Optional[dict] = None
    binaries: Optional[dict] = None
    mode: Optional[str] = None
    wired: bool = False
    deactivated: bool = False
    adoption: Optional[bool] = None


def carry_from_marker(marker, manifest_hooks, kind=None):
    """Build the inheritance record for a marker found at the start of a run (pure)."""
    kind = kind or classify_marker(marker)
    carry = Carry(kind=kind, marker=marker if kind not in ("none", "unreadable") else None)
    carry.prev_hooks = union_hooks(manifest_hooks)
    if kind in ("none", "staging"):
        return carry
    carry.completing = True
    if kind == "unreadable":
        carry.note = "completing an interrupted operation (created event keys unknown)"
        return carry
    carry.note = "completing an interrupted operation"
    draft = marker.get("draft") or {}
    carry.prev_hooks = union_hooks(manifest_hooks, draft.get("hooks"), marker.get("carried_hooks"))
    stale = marker.get("stale") if isinstance(marker.get("stale"), dict) else {}
    for key in carry.stale:
        carry.stale[key] = list(stale.get(key) or [])
    if marker.get("rollback_dir"):
        carry.stale["rollback_dirs"].append(marker["rollback_dir"])
    previous = marker.get("previous") if isinstance(marker.get("previous"), dict) else {}
    aside = previous.get("plugin_aside")
    if isinstance(aside, dict) and aside.get("path") and not aside.get("kept"):
        carry.stale["plugin_asides"].append(aside["path"])
    if previous.get("version_dir_aside"):
        carry.stale["version_dir_asides"].append(previous["version_dir_aside"])
    carry.legacy_owned_events = copy.deepcopy(draft.get("legacy_owned_events"))
    carry.binaries = copy.deepcopy(draft.get("binaries"))
    carry.mode = draft.get("mode")
    carry.wired = bool(draft.get("wired"))
    carry.deactivated = bool(draft.get("deactivated"))
    carry.adoption = bool(marker.get("adoption"))
    return carry


class MarkerStore:
    def __init__(self, path):
        self.path = path
        self.data = None

    def write(self, data):
        self.data = data
        raw = _read_bytes(self.path)
        jsonfile.write_atomic(self.path, data, expected_raw=raw, default_mode=0o644, create_parents=True)

    def save(self):
        self.write(self.data)

    def clear(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.data = None


def open_marker(store, *, op, phase, completing, carry, prev_hooks, adoption, draft_now, previous=None,
                runtimes=None, started_at=None, rollback_dir=None):
    """The one writer of a new marker. `op` and `phase` are required (no defaults).

    A completing run copies its predecessor's leftovers, hook records and legacy-owned events forward.
    Its draft keeps the predecessor's verified binaries overlaid by anything this run verified, takes this
    run's decided mode, and keeps `wired` / `deactivated` once set.
    """
    if completing and phase == "staging":
        raise ValueError("a completing run is never written as staging")
    draft_now = draft_now or {}
    carry = carry or Carry()
    if completing:
        binaries = copy.deepcopy(carry.binaries or {})
        binaries.update(copy.deepcopy(draft_now.get("binaries") or {}))
        mode = draft_now.get("mode") or carry.mode
        legacy_owned = copy.deepcopy(carry.legacy_owned_events) if carry.legacy_owned_events is not None \
            else copy.deepcopy(draft_now.get("legacy_owned_events"))
        wired = bool(carry.wired or draft_now.get("wired"))
        deactivated = bool(draft_now.get("deactivated") if "deactivated" in draft_now else carry.deactivated)
        stale = copy.deepcopy(carry.stale)
    else:
        binaries = copy.deepcopy(draft_now.get("binaries") or {})
        mode = draft_now.get("mode")
        legacy_owned = copy.deepcopy(draft_now.get("legacy_owned_events"))
        wired = bool(draft_now.get("wired"))
        deactivated = bool(draft_now.get("deactivated"))
        stale = {"rollback_dirs": [], "plugin_asides": [], "version_dir_asides": []}
    data = {
        "op": op, "phase": phase, "adoption": bool(adoption), "steps_done": [],
        "started_at": started_at, "rollback_dir": rollback_dir, "runtimes": runtimes,
        "draft": {"hooks": [], "legacy_owned_events": legacy_owned, "mode": mode, "binaries": binaries,
                  "wired": wired, "deactivated": deactivated, "version_dir": draft_now.get("version_dir")},
        "carried_hooks": copy.deepcopy(prev_hooks or []),
        "stale": stale,
        "previous": copy.deepcopy(previous) if previous is not None else {},
    }
    store.write(data)
    return data


# ------------------------------------------------------------------ route

@dataclass
class Facts:
    manifest_mode: Optional[str]
    detection: str
    own_evidence: bool
    revival: bool = False


def route_adoption(facts, marker):
    """True when the run is an adoption. With a marker on disk the route comes from the marker."""
    if facts.manifest_mode == "adoption-pending":
        return True
    kind = classify_marker(marker)
    if kind == "none":
        return facts.detection == "prototype" and not facts.revival
    if kind in ("committed", "staging") and marker["op"] in ("install", "upgrade"):
        return bool(marker.get("adoption"))
    if kind == "uninstall":
        return bool(marker.get("adoption")) and facts.detection == "prototype"
    return facts.detection == "prototype" and not facts.own_evidence


def own_install_evidence(paths):
    """`current` exists or the hook shim is a live (non-tombstone) shim: this install is ours."""
    if _lexists(paths.current):
        return True
    shim = paths.hook_shim
    return os.path.isfile(shim) and not manifest.is_tombstone(shim)


def is_revival(paths):
    """Only inert stubs remain (listed by `uninstalled.json`): a reinstall revives them, it is not an adoption."""
    if not manifest.read_uninstalled(paths) or os.path.lexists(paths.plugin_dir):
        return False
    return not any(os.path.lexists(s) and not manifest.is_tombstone(s) for s in paths.legacy_root_scripts)


def collect_facts(paths):
    """Read-only facts for `route_adoption` (settings read tolerantly)."""
    def tolerant(path):
        try:
            return jsonfile.read(path)[0]
        except Exception:
            return {}
    det = manifest.detect(paths, tolerant(paths.claude_settings), tolerant(paths.codex_hooks))
    loaded = manifest.load_any(paths.manifest)
    mode = loaded.get("mode") if isinstance(loaded, dict) else None
    return Facts(mode, det.state, own_install_evidence(paths), is_revival(paths))


# ------------------------------------------------------------------ rollback

def _restore_file(src, dst):
    directory = os.path.dirname(dst)
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, ".%s.quoin-restore-%s.tmp" % (os.path.basename(dst), _token()))
    shutil.copy2(src, temp)
    os.replace(temp, dst)


def _set_symlink(link, target):
    """Point `link` at `target` (relative or absolute) via temp symlink + rename."""
    try:
        if os.readlink(link) == target:
            return False
    except OSError:
        pass
    temp = "%s.quoin-%s.tmp" % (link, _token())
    os.symlink(target, temp)
    try:
        os.replace(temp, link)
    except BaseException:
        _remove_any(temp)
        raise
    return True


def rollback(paths, marker):
    """Undo a staging install/upgrade. Never called for an uninstall marker."""
    if marker.get("op") == "uninstall":
        raise RuntimeError("an uninstall marker is roll-forward only")
    previous = marker.get("previous") or {}
    # current
    target = previous.get("current_target")
    if target is None:
        if os.path.islink(paths.current):
            os.unlink(paths.current)
    else:
        _set_symlink(paths.current, target)
    # tracked files
    rollback_dir = marker.get("rollback_dir")
    for rel in previous.get("existing") or []:
        src = os.path.join(rollback_dir or "", rel)
        if rollback_dir and os.path.exists(src):
            _restore_file(src, os.path.join(paths.runtime, rel))
    for rel in previous.get("created") or []:
        _remove_any(os.path.join(paths.runtime, rel))
    # plugin
    aside = previous.get("plugin_aside")
    if isinstance(aside, dict) and aside.get("path"):
        if os.path.lexists(aside["path"]):
            if os.path.lexists(paths.plugin_dir):
                _remove_any(paths.plugin_dir)
            os.rename(aside["path"], paths.plugin_dir)
    elif previous.get("plugin_created") and os.path.isdir(paths.plugin_dir) \
            and _plugin_uuid(paths.plugin_dir) == payload_build.PLUGIN_UUID:
        _remove_any(paths.plugin_dir)
    # version dir
    version_aside = previous.get("version_dir_aside")
    version_dir = (marker.get("draft") or {}).get("version_dir")
    if isinstance(version_aside, dict) and version_aside.get("path") and os.path.lexists(version_aside["path"]):
        if version_dir and os.path.lexists(version_dir):
            _remove_any(version_dir)
        os.rename(version_aside["path"], version_aside["original"])
    elif previous.get("version_dir_created") and version_dir:
        _remove_any(version_dir)
    # adoption: records written by the new monitor stay visible to the restored prototype
    if marker.get("adoption") or previous.get("adoption"):
        migrations.reverse_sweep(paths)
    for created in reversed(previous.get("created_paths") or []):
        try:
            if os.path.isdir(created) and not os.path.islink(created):
                os.rmdir(created)
            elif os.path.lexists(created):
                os.unlink(created)
        except OSError:
            pass
    if rollback_dir:
        _rmtree(rollback_dir)
    try:
        os.rmdir(paths.staging)
    except OSError:
        pass


# ------------------------------------------------------------------ state reading

@dataclass
class State:
    paths: Any
    notes: list
    manifest: Optional[dict]
    manifest_problem: Optional[str]
    detection: Any
    claude: tuple
    codex: tuple
    marker: Optional[dict]
    marker_kind: str
    installed_version: Optional[str]
    prev_mode: Optional[str]
    facts: Facts


class _Fail(Exception):
    pass


def _effective_paths(env, recorded):
    """Resolved paths, except that a valid manifest's recorded paths win unless a flag was passed (D-20)."""
    paths, notes = env.paths, []
    rec = recorded.get("paths") if isinstance(recorded, dict) else None
    if not isinstance(rec, dict) or manifest.validate(recorded):
        return paths, notes
    explicit = set(env.flags or {}) & set(PATH_FLAGS)
    flag_for = {"runtime": "runtime_dir", "plugin_dir": "plugins_dir", "config": "state_root",
                "state": "state_root", "claude_settings": "claude_settings", "codex_hooks": "codex_home",
                "agentdesk": "agentdesk"}
    use = {}
    for key, value in rec.items():
        resolved = paths.manifest_paths().get(key)
        if resolved is not None and resolved != value:
            if flag_for.get(key) in explicit:
                use[key] = resolved
            else:
                notes.append("note: using the recorded %s %s (this shell resolves %s); pass the matching flag to "
                             "change it" % (key, value, resolved))
                use[key] = value
        else:
            use[key] = value
    if all(use.get(k) == v for k, v in paths.manifest_paths().items()):
        return paths, notes
    plugin_dir, config, hooks = use["plugin_dir"], use["config"], use["codex_hooks"]
    codex_home = os.path.dirname(hooks)
    effective = dataclasses.replace(
        paths, runtime=use["runtime"], plugins_dir=os.path.dirname(plugin_dir), plugin_dir=plugin_dir,
        state_root=os.path.dirname(config), config=config, state=use["state"], claude_settings=use["claude_settings"],
        codex_home=codex_home, codex_hooks=hooks, codex_config=os.path.join(codex_home, os.path.basename(paths.codex_config)),
        agentdesk=use["agentdesk"])
    return effective, notes


def _read_settings(path, kind):
    try:
        data, raw = jsonfile.read(path)
    except jsonfile.MalformedJSON as exc:
        raise OperationError("%s is not valid JSON (%s); it was left unchanged" % (path, exc.detail))
    except OSError as exc:
        raise OperationError("%s could not be read (%s); it was left unchanged" % (path, exc.strerror or exc))
    try:
        hooks_merge.merge(data, kind, "probe")
    except hooks_merge.HooksShapeError as exc:
        raise OperationError("%s has an unexpected hooks layout (%s); it was left unchanged" % (path, exc))
    return data, raw


def read_state(env, op, *, read_kinds=RUNTIME_KINDS):
    """Read-only preflight: effective paths, manifest, marker, detection and both settings files."""
    recorded = manifest.load_any(env.paths.manifest)
    paths, notes = _effective_paths(env, recorded)
    problem = None
    data = None
    if os.path.lexists(paths.manifest):
        recorded = manifest.load_any(paths.manifest)
        try:
            if recorded is None:
                raise migrations.MigrationError("manifest is not a JSON object")
            migrated = migrations.migrate_manifest(recorded)
            issues = manifest.validate(migrated)
            if issues:
                problem = "; ".join(issues)
            else:
                data = migrated
        except migrations.MigrationError as exc:
            problem = str(exc)
    claude = _read_settings(paths.claude_settings, "claude") if "claude" in read_kinds else ({}, None)
    codex = _read_settings(paths.codex_hooks, "codex") if "codex" in read_kinds else ({}, None)
    marker = manifest.read_operation_marker(paths)
    kind = classify_marker(marker)
    detection = manifest.detect(paths, claude[0], codex[0])
    facts = Facts(data.get("mode") if data else None, detection.state, own_install_evidence(paths),
                  is_revival(paths))
    return State(paths=paths, notes=notes, manifest=data, manifest_problem=problem, detection=detection,
                 claude=claude, codex=codex, marker=marker, marker_kind=kind,
                 installed_version=data.get("component_version") if data else None,
                 prev_mode=data.get("mode") if data else None, facts=facts)


# ------------------------------------------------------------------ the run

class _Run:
    def __init__(self, env, op):
        self.env = env
        self.op = op
        self.lines = []
        self.changed = False
        self.lock_fd = None
        self.store = None
        self.carry = Carry()
        self.ctx = None
        self.codex_changed = False
        self.claude_changed = False
        self.plugin_changed = False
        self.warnings_done = set()
        self.removed_kinds = set()
        self.deactivated = False
        self.keep_recorded = False

    # -- output
    def say(self, text):
        self.lines.append(text)
        self.env.out(text)

    # -- fault injection
    def _fault(self, name):
        fault = self.env.faults.get(name)
        if fault is not None:
            raise fault() if isinstance(fault, type) else fault

    @contextlib.contextmanager
    def step(self, name):
        self._fault(name)
        yield
        self._fault(name + ":after")
        if self.store is not None and self.store.data is not None:
            self.store.data["steps_done"].append(name)
            self.store.save()
        if self.env.on_step:
            self.env.on_step(name)

    # -- entry
    def execute(self):
        env = self.env
        try:
            macos.require_macos(env.platform_info)
        except macos.UnsupportedPlatform as exc:
            raise OperationError(str(exc), code=2)
        state = read_state(env, self.op)
        for note in state.notes:
            self.say(note)
        self._refuse(state)
        carry = carry_from_marker(state.marker if state.marker_kind not in ("none",) else None,
                                  (state.manifest or {}).get("hooks"), state.marker_kind)
        consent = self._consent(state, carry)
        if env.dry_run:
            return self._dry_run(state, carry, consent)
        os.makedirs(state.paths.runtime, exist_ok=True)
        self.lock_fd = self._lock(state.paths)
        try:
            return self._locked(state, consent)
        finally:
            if self.lock_fd is not None:
                try:
                    os.close(self.lock_fd)
                except OSError:
                    pass

    # -- refusals
    def _refuse(self, state):
        paths, env = state.paths, self.env
        if state.manifest_problem:
            raise OperationError("manifest invalid: %s; fix it or run `quoin streamdeck uninstall`"
                                 % state.manifest_problem)
        if state.installed_version and _vkey(state.installed_version) > _vkey(env.component_version) \
                and not env.allow_downgrade:
            raise OperationError("installed component %s is newer than this package (%s); pass --allow-downgrade "
                                 "to replace it" % (state.installed_version, env.component_version))
        if self.op == "upgrade":
            installed = state.manifest is not None or state.detection.state == "prototype"
            if not installed:
                if state.marker_kind == "uninstall":
                    raise OperationError("an interrupted uninstall is pending; run `quoin streamdeck install` or "
                                         "`quoin streamdeck uninstall` to complete it")
                if state.marker_kind in ("committed", "staging", "unreadable") and state.marker is not None:
                    raise OperationError("an interrupted install is pending; run `quoin streamdeck install` to "
                                         "complete it")
                raise OperationError("not installed; run `quoin streamdeck install`")
        for kind, (data, _) in (("claude", state.claude), ("codex", state.codex)):
            for command in hooks_merge.managed_commands_elsewhere(data, kind, paths.runtime):
                other = os.path.dirname(os.path.dirname(shlex_first(command)))
                if os.path.exists(os.path.join(other, "install-manifest.json")):
                    raise OperationError("the %s settings already carry hooks for another Stream Deck runtime "
                                         "(%s); uninstall it first or pass --runtime-dir %s" % (kind, other, other))

    # -- lock
    def _lock(self, paths):
        fd = os.open(paths.lock, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            raise OperationError("another quoin streamdeck operation is running")
        return fd

    # -- consent
    def _recorded_binaries(self, state, carry):
        recorded = copy.deepcopy((state.manifest or {}).get("binaries") or {})
        if carry.completing and carry.binaries:
            recorded.update(copy.deepcopy(carry.binaries))  # what the interrupted run just verified wins
        return recorded

    def _marker_for_route(self, state):
        return state.marker if state.marker_kind != "none" else None

    def _consent(self, state, carry):
        env = self.env
        legacy = migrations.harvest_legacy(state.paths, state.claude[0], state.codex[0])
        adoption = route_adoption(state.facts, self._marker_for_route(state))
        recorded = self._recorded_binaries(state, carry)
        ctx = env.ctx_factory(state.paths, env.flags, recorded=recorded, legacy=legacy)
        self.ctx = ctx
        results = prereqs.run_checks(ctx)
        outcome = prereqs.resolve(ctx, results, mode="none" if env.dry_run else env.prereq_mode)
        return {"ctx": ctx, "results": results, "outcome": outcome, "legacy": legacy, "adoption": adoption,
                "recorded": recorded, "key": (adoption, state.facts.manifest_mode, state.paths.runtime,
                                              tuple(sorted(outcome.core_missing)))}

    # -- dry run
    def _dry_run(self, state, carry, consent):
        env = self.env
        if state.marker_kind == "staging":
            self.say("dry run: would roll back an interrupted operation")
        elif state.marker_kind != "none":
            self.say("dry run: would complete an interrupted operation")
        outcome = consent["outcome"]
        mode = self._decide_mode(state, carry, consent, dry=True)
        self.say("dry run: would %s the Stream Deck component %s (%s)" % (self.op, env.component_version, mode))
        if outcome.core_missing:
            self.say("dry run: missing core prerequisites: %s" % ", ".join(outcome.core_missing))
        return Result(0, mode, False, self.lines, state.manifest)

    # -- recovery
    def _recover(self, state):
        paths = state.paths
        kind = state.marker_kind
        marker = state.marker
        hooks = (state.manifest or {}).get("hooks")
        if kind == "none":
            self._sweep_staging(paths)
            return carry_from_marker(None, hooks, "none")
        if kind == "staging":
            try:
                rollback(paths, marker)
            except Exception as exc:
                raise OperationError("could not roll back the interrupted operation (%s); the marker was left in "
                                     "place" % (exc,))
            try:
                os.unlink(paths.operation_marker)
            except FileNotFoundError:
                pass
            self.say("rolled back an interrupted operation")
            return carry_from_marker(None, hooks, "none")
        carry = carry_from_marker(marker, hooks, kind)
        self.say(carry.note)
        return carry

    def _sweep_staging(self, paths):
        if os.path.isdir(paths.staging):
            for name in os.listdir(paths.staging):
                _remove_any(os.path.join(paths.staging, name))
        if os.path.isdir(paths.versions_dir):
            for name in os.listdir(paths.versions_dir):
                if ".staging-" in name:
                    _remove_any(os.path.join(paths.versions_dir, name))

    # -- locked section
    def _locked(self, state, consent):
        env = self.env
        carry = self._recover(state)
        fresh = read_state(env, self.op)
        adoption = route_adoption(fresh.facts, self._marker_for_route(fresh))
        key = (adoption, fresh.facts.manifest_mode, fresh.paths.runtime)
        if key != consent["key"][:3] or fresh.paths != state.paths:
            consent = self._consent(fresh, carry)
        else:
            consent = dict(consent)
            consent["adoption"] = adoption
        self.carry = carry
        mode = self._decide_mode(fresh, carry, consent)
        return self._pipeline(fresh, carry, consent, mode)

    # -- mode
    def _decide_mode(self, state, carry, consent, dry=False):
        core_missing = list(consent["outcome"].core_missing)
        ctx = consent["ctx"]
        adoption = consent["adoption"]
        prev_mode = carry.mode if carry.completing and carry.mode else state.prev_mode
        self.deactivated = False
        self.keep_recorded = False
        if carry.completing and carry.deactivated:
            self.deactivated = True
            return "dormant"
        if carry.completing and prev_mode == "active" and carry.wired:
            if not core_missing:
                return "active"
            return self._deactivate_or_keep(ctx, dry)
        if adoption and core_missing:
            return "adoption-pending"
        if prev_mode in (None, "dormant") or adoption:
            return "dormant" if core_missing else "active"
        if not core_missing:
            return "active"
        return self._deactivate_or_keep(ctx, dry)

    def _deactivate_or_keep(self, ctx, dry):
        if not dry and ctx.stdin_isatty() and ctx.stdout_isatty():
            try:
                answer = ctx.prompt("Deactivate the deck (remove the plugin and hooks)? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if str(answer).strip().lower() in ("y", "yes"):
                self.deactivated = True
                return "dormant"
        self.keep_recorded = True
        return "active"

    # -- binaries
    def _binaries(self, consent):
        results, ctx, recorded = consent["results"], consent["ctx"], consent["recorded"]
        found = {}
        py = results["python"]
        if py.ok:
            found["python"] = {"path": py.path, "version": py.version}
        node = results["node"]
        if node.ok:
            if node.detail == "bundled":
                found["node"] = {"mode": "bundled", "path": None}
            else:
                found["node"] = {"mode": "external", "path": node.path, "version": node.version}
        zel = results["zellij"]
        if zel.ok:
            found["zellij"] = {"path": zel.path, "version": zel.version}
        for name in ("osascript", "open"):
            path = ctx.which(name)
            if not (isinstance(path, str) and os.path.isabs(path)):
                path = "/usr/bin/" + name if os.path.exists("/usr/bin/" + name) else None
            if path:
                found[name] = {"path": path}
        gh = results["ghostty"]
        if gh.ok:
            found["ghostty_app"] = {"path": gh.path}
        ad = results["agentdesk"]
        if ad.ok:
            api = int(ad.version) if (ad.version or "").isdigit() else ad.version
            found["agentdesk"] = {"path": ad.path, "api_version": api}
        found.update(manifest.record_optional_tools(ctx.which))
        if self.keep_recorded:
            for key in ("python", "node", "zellij", "ghostty_app", "agentdesk"):
                if key not in found and key in recorded:
                    found[key] = copy.deepcopy(recorded[key])
        return found

    # -- marker helpers
    def _save(self):
        self.store.save()

    def _draft(self):
        return self.store.data["draft"]

    def _previous(self):
        return self.store.data["previous"]

    def _set_draft_hooks(self, kind, records):
        draft = self._draft()
        kept = [r for r in draft.get("hooks", []) if r.get("runtime") != kind]
        new = kept + copy.deepcopy(records)
        if new != draft.get("hooks"):
            draft["hooks"] = new
            self._save()

    # -- pipeline
    def _pipeline(self, state, carry, consent, mode):
        env, paths = self.env, state.paths
        self.state, self.paths, self.mode = state, paths, mode
        self.consent = consent
        self.adoption = consent["adoption"]
        self.binaries = self._binaries(consent)
        fingerprint = payload_fingerprint(env.payload_dir)
        self.fingerprint = fingerprint
        self.version_name = "%s-%s" % (env.component_version, fingerprint[:12])
        self.version_dir = os.path.join(paths.versions_dir, self.version_name)
        previous_installed = state.installed_version
        if previous_installed and previous_installed != env.component_version:
            self.say("upgrading from %s to %s" % (previous_installed, env.component_version))
        self.selected = self._selected_runtimes(state, consent)
        self.legacy_agents = [a for a in migrations.collect_agents(paths) if a.get("via") == "legacy"] \
            if self.adoption else []
        if self.adoption:
            owned = (carry.legacy_owned_events if carry.legacy_owned_events is not None
                     else migrations.legacy_owned(state.claude[0], state.codex[0], manifest.runtime_names(paths)))
        else:
            owned = carry.legacy_owned_events if carry.legacy_owned_events is not None \
                else (state.manifest or {}).get("legacy_owned_events") or {}
        self.legacy_owned = owned
        self.forwarders = self._forwarder_names(state)
        self.rollback_dir = os.path.join(paths.staging, "rollback-" + _token())
        self.store = MarkerStore(paths.operation_marker)
        completing = carry.completing
        previous = None if completing else {
            "current_target": self._readlink(paths.current), "first_install": self._first_install(state),
            "adoption": self.adoption, "existing": [], "created": [], "created_paths": [], "plugin_aside": None,
            "plugin_created": False, "version_dir_aside": None, "version_dir_created": False}
        open_marker(self.store, op=self.op, phase="committed" if completing else "staging", completing=completing,
                    carry=carry, prev_hooks=carry.prev_hooks, adoption=self.adoption, previous=previous,
                    draft_now={"binaries": self.binaries, "mode": mode, "legacy_owned_events": owned,
                               "version_dir": self.version_dir, "deactivated": self.deactivated},
                    started_at=_iso(env.now), rollback_dir=None if completing else self.rollback_dir)
        try:
            if mode == "adoption-pending":
                with self.step("stage_payload"):
                    self._stage_payload()
                with self.step("commit"):
                    self._commit()
                with self.step("finalize"):
                    manifest_data = self._finalize()
            else:
                with self.step("stage_payload"):
                    self._stage_payload()
                with self.step("set_aside"):
                    self._set_aside()
                with self.step("swap_current"):
                    self._swap_current()
                with self.step("write_entry_points"):
                    self._write_entry_points()
                with self.step("plugin_swap"):
                    self._plugin_swap()
                with self.step("commit"):
                    self._commit()
                with self.step("state_migration"):
                    self._state_migration()
                with self.step("claude_settings"):
                    self._settings("claude")
                with self.step("codex_settings"):
                    self._settings("codex")
                with self.step("finalize"):
                    manifest_data = self._finalize()
            self.store.clear()
        except Exception as exc:
            self._failed(exc)
        self._report(manifest_data)
        return Result(0, mode, self.changed, self.lines, manifest_data)

    def _failed(self, exc):
        data = self.store.data
        detail = "%s: %s" % (type(exc).__name__, exc) if str(exc) else type(exc).__name__
        if data and data.get("phase") == "staging":
            try:
                rollback(self.paths, data)
            except Exception as rb:
                raise OperationError("%s failed (%s) and the rollback was incomplete (%s); re-run the command to "
                                     "retry the rollback" % (self.op, detail, rb))
            self.store.clear()
            raise OperationError("%s failed (%s); everything was rolled back" % (self.op, detail))
        raise OperationError("%s was interrupted after its commit point (%s); re-run `quoin streamdeck install` "
                             "to complete it" % (self.op, detail))

    @staticmethod
    def _readlink(path):
        try:
            return os.readlink(path)
        except OSError:
            return None

    def _first_install(self, state):
        return (state.manifest is None and state.detection.state == "not-installed"
                and not _lexists(self.paths.current))

    def _selected_runtimes(self, state, consent):
        env = self.env
        if env.runtimes is not None:
            return [k for k in RUNTIME_KINDS if k in env.runtimes]
        detected, _ = migrations.codex_detected(state.paths, env.environ, consent["ctx"].which, state.codex[0],
                                                state.manifest)
        if not detected:
            self.say("Codex not detected; skipping Codex hooks")
        return ["claude"] + (["codex"] if detected else [])

    def _forwarder_names(self, state):
        if self.adoption:
            return list(shims.FORWARDER_NAMES)
        names = []
        paths = state.paths
        revived = [os.path.basename(p) for p in manifest.read_uninstalled(paths)
                   if os.path.dirname(p) == paths.runtime]
        if "monitor.py" in revived:
            names.append("monitor.py")
        for entry in (state.manifest or {}).get("files") or []:
            if entry.get("kind") == "forwarder" and entry.get("rel") in shims.FORWARDER_NAMES:
                names.append(entry["rel"])
        return sorted(set(names), key=shims.FORWARDER_NAMES.index)

    # -- steps
    def _tracked(self):
        rels = [os.path.join("bin", "quoin-deck-hook"), os.path.join("bin", "quoin-deck-terminal")]
        if self.mode == "active":
            rels.append(os.path.join("profile", PROFILE_NAME))
        rels.extend(self.forwarders)
        return rels

    def _note_created_dir(self, path):
        if not os.path.isdir(path):
            self._previous().setdefault("created_paths", []).append(path)
            self._save()

    def _stage_payload(self):
        env, paths = self.env, self.paths
        files = payload_files(env.payload_dir)
        expected = {rel: _sha256_file(os.path.join(env.payload_dir, rel)) for rel in files}
        if not self.carry.completing:
            self._note_created_dir(paths.versions_dir)
        os.makedirs(paths.versions_dir, exist_ok=True)
        if _lexists(self.version_dir):
            if self._version_dir_matches(expected):
                return
            if self.carry.completing:
                _remove_any(self.version_dir)
            else:
                aside = os.path.join(self.rollback_dir, "aside", self.version_name)
                os.makedirs(os.path.dirname(aside), exist_ok=True)
                self._previous()["version_dir_aside"] = {"path": aside, "original": self.version_dir}
                self._save()
                os.rename(self.version_dir, aside)
        elif not self.carry.completing:
            self._previous()["version_dir_created"] = True
            self._save()
        staging = "%s.staging-%s" % (self.version_dir, _token())
        for rel in files:
            dest = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(env.payload_dir, rel), dest)
            os.chmod(dest, 0o644)
            if _sha256_file(dest) != expected[rel]:
                _remove_any(staging)
                raise RuntimeError("payload file %s failed verification after copy" % rel)
        os.rename(staging, self.version_dir)
        self.changed = True

    def _version_dir_matches(self, expected):
        for rel, digest in expected.items():
            target = os.path.join(self.version_dir, rel)
            try:
                if not stat.S_ISREG(os.lstat(target).st_mode) or _sha256_file(target) != digest:
                    return False
            except OSError:
                return False
        return True

    def _set_aside(self):
        if self.carry.completing:
            return
        paths = self.paths
        previous = self._previous()
        os.makedirs(self.rollback_dir, exist_ok=True)
        for rel in self._tracked():
            src = os.path.join(paths.runtime, rel)
            if _lexists(src):
                dst = os.path.join(self.rollback_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst, follow_symlinks=False)
                previous["existing"].append(rel)
            else:
                previous["created"].append(rel)
        for directory in (paths.bin_dir, paths.profile_dir):
            if not os.path.isdir(directory):
                previous["created_paths"].append(directory)
        self._save()

    def _swap_current(self):
        if _set_symlink(self.paths.current, "versions/" + self.version_name):
            self.changed = True

    def _python_for_shim(self):
        entry = self.binaries.get("python")
        return entry["path"] if isinstance(entry, dict) else None

    def _write_entry_points(self):
        paths = self.paths
        python = self._python_for_shim()
        if not os.path.isdir(paths.bin_dir):
            os.makedirs(paths.bin_dir, exist_ok=True)
        for path, text in ((paths.hook_shim, shims.render_hook_shim(paths.runtime, python)),
                           (paths.terminal_entry, shims.render_terminal_entry(paths.runtime))):
            if write_file_atomic(path, text.encode("utf-8"), 0o755):
                self.changed = True
        for name in self.forwarders:
            text = shims.render_forwarder(name, paths.runtime, python)
            if write_file_atomic(os.path.join(paths.runtime, name), text.encode("utf-8"), 0o755):
                self.changed = True
        if self.mode == "active":
            self._write_profile()
        self._draft()["wired"] = True
        self._save()

    def _write_profile(self):
        paths = self.paths
        os.makedirs(paths.staging, exist_ok=True)
        temp = os.path.join(paths.staging, "profile-%s.streamDeckProfile" % _token())
        payload_build.build_profile(temp)
        try:
            data = _read_bytes(temp)
        finally:
            _remove_any(temp)
        os.makedirs(paths.profile_dir, exist_ok=True)
        if write_file_atomic(paths.profile, data, 0o644):
            self.changed = True

    def _plugin_swap(self):
        paths = self.paths
        if self.mode != "active":
            if self.deactivated and os.path.isdir(paths.plugin_dir) \
                    and _plugin_uuid(paths.plugin_dir) == payload_build.PLUGIN_UUID:
                aside = os.path.join(paths.plugins_dir, ".quoin-plugin-old-" + _token())
                self._previous()["plugin_aside"] = {"path": aside, "kept": False}
                self._save()
                os.rename(paths.plugin_dir, aside)
                self.changed = self.plugin_changed = True
            return
        node = self.binaries.get("node") or {}
        python = self.binaries.get("python") or {}
        if not self.carry.completing and not os.path.isdir(paths.plugins_dir):
            self._previous().setdefault("created_paths", []).append(paths.plugins_dir)
            self._save()
        os.makedirs(paths.plugins_dir, exist_ok=True)
        os.makedirs(paths.staging, exist_ok=True)
        new = os.path.join(paths.staging, "plugin-" + _token())
        payload_build.build_plugin_dir(
            new, node_mode=node.get("mode", "external"), node_path=node.get("path"), python=python.get("path"),
            runtime=paths.runtime, state_root=os.path.dirname(paths.config))
        if os.path.isdir(paths.plugin_dir) and _tree_signature(new) == _tree_signature(paths.plugin_dir):
            _remove_any(new)
            return
        if _lexists(paths.plugin_dir):
            ours = _plugin_uuid(paths.plugin_dir) == payload_build.PLUGIN_UUID
            kind = "old" if ours else "kept"
            aside = os.path.join(paths.plugins_dir, ".quoin-plugin-%s-%s" % (kind, _token()))
            if not self.carry.completing:
                self._previous()["plugin_aside"] = {"path": aside, "kept": not ours}
                self._save()
            os.rename(paths.plugin_dir, aside)
            if not ours:
                self.say("warning: the existing plugin directory is not ours; it was kept as %s" % aside)
            elif self.carry.completing:
                self.store.data["stale"]["plugin_asides"].append(aside)
                self._save()
        elif not self.carry.completing:
            self._previous()["plugin_created"] = True
            self._save()
        try:
            os.rename(new, paths.plugin_dir)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            copy_to = os.path.join(paths.plugins_dir, ".quoin-plugin-new-" + _token())
            shutil.copytree(new, copy_to)
            os.rename(copy_to, paths.plugin_dir)
            _remove_any(new)
        self.changed = self.plugin_changed = True

    def _commit(self):
        data = self.store.data
        if data["phase"] != "committed":
            data["phase"] = "committed"
            self._save()

    def _state_migration(self):
        paths = self.paths
        for note in migrations.ensure_state_dirs(paths):
            self.say(note)
        report = migrations.merge_agents(paths, self.env.now)
        if report["moved"]:
            self.changed = True
        if report["skipped"]:
            self.say("some legacy agent records were busy and will move on the next run")
        migrations.split_surfaces(paths, self._manifest_socket() or _socket_dir(self.env.environ))
        migrations.move_errors_log(paths)
        socket = self._manifest_socket()
        if socket and os.path.exists(socket):
            info = os.stat(socket)
            if info.st_uid != os.getuid() or (info.st_mode & 0o077):
                self.say("warning: the zellij socket directory %s is not private to you" % socket)

    def _manifest_socket(self):
        value = _socket_dir(self.env.environ)
        return value if os.path.isabs(value) else None

    # -- settings
    def _runtime_command(self, kind):
        return hooks_merge.managed_command(self.paths.runtime, kind)

    def _prev_records(self, kind):
        return [r for r in self.carry.prev_hooks if r.get("runtime") == kind]

    def _settings(self, kind):
        env, paths = self.env, self.paths
        settings_path = paths.claude_settings if kind == "claude" else paths.codex_hooks
        names = manifest.runtime_names(paths)
        recorded = tuple(r["command"] for r in self.carry.prev_hooks if isinstance(r.get("command"), str))
        prev_by_key = {_hook_key(r): r for r in self.carry.prev_hooks}
        if self.mode == "active" and kind in self.selected:
            command = self._runtime_command(kind)
            self._merge_settings(kind, settings_path, names, recorded, prev_by_key, command)
        elif self.mode != "active" and self.deactivated and self._prev_records(kind):
            self._remove_settings(kind, settings_path, names, recorded, prev_by_key)
        elif env.runtimes is not None and kind not in self.selected and self._owned_present(kind, settings_path, names):
            self._remove_settings(kind, settings_path, names, recorded, prev_by_key, report_note=True)

    def _owned_present(self, kind, settings_path, names):
        data = jsonfile.read(settings_path)[0]
        recorded = tuple(r["command"] for r in self.carry.prev_hooks if isinstance(r.get("command"), str))
        for event, gi, hi in hooks_merge.owned_commands(data, kind, runtime_names=names, recorded=recorded):
            hook = data["hooks"][event][gi]["hooks"][hi]
            if hooks_merge.classify(hook.get("command"), runtime_names=names, recorded=recorded) != "legacy":
                return True
        return False

    def _merge_settings(self, kind, settings_path, names, recorded, prev_by_key, command):
        paths, env = self.paths, self.env
        current = jsonfile.read(settings_path)[0]
        _, dry = hooks_merge.merge(current, kind, command, runtime_names=names, recorded=recorded)
        for other in hooks_merge.managed_commands_elsewhere(current, kind, paths.runtime):
            self.say("warning: replacing the hook command %s, which pointed at another runtime" % other)
        if dry.changed and os.path.isfile(settings_path):
            jsonfile.backup(settings_path, paths.backups, now=env.now)
        reports = {}

        def mutate(data):
            new, report = hooks_merge.merge(data, kind, command, runtime_names=names, recorded=recorded)
            records = []
            for event in hooks_merge.EVENTS[kind]:
                key = (kind, settings_path, event)
                created = event in report.created_event_keys or bool(prev_by_key.get(key, {}).get("created_event_key"))
                records.append({"runtime": kind, "settings_path": settings_path, "event": event, "command": command,
                                "timeout": hooks_merge.HOOK_TIMEOUT, "created_event_key": created})
            self._set_draft_hooks(kind, records)
            reports["report"] = report
            return new

        changed, _ = jsonfile.update(settings_path, mutate, create_parents=True)
        if changed:
            self.changed = True
            if kind == "claude":
                self.claude_changed = True
            else:
                self.codex_changed = True
            report = reports["report"]
            if kind == "codex" and report.shifted_events:
                self.say("Codex will ask you to re-review these events in /hooks: %s" % ", ".join(report.shifted_events))

    def _remove_settings(self, kind, settings_path, names, recorded, prev_by_key, report_note=False):
        paths, env = self.paths, self.env
        removable = [key[2] for key, rec in prev_by_key.items() if key[0] == kind and rec.get("created_event_key")]
        only = paths.runtime
        current = jsonfile.read(settings_path)[0]
        _, dry = hooks_merge.remove(current, kind, runtime_names=names, recorded=recorded,
                                    removable_event_keys=removable, only_runtime=only)
        if dry.changed and os.path.isfile(settings_path):
            jsonfile.backup(settings_path, paths.backups, now=env.now)
        reports = {}

        def mutate(data):
            new, report = hooks_merge.remove(data, kind, runtime_names=names, recorded=recorded,
                                             removable_event_keys=removable, only_runtime=only)
            self._set_draft_hooks(kind, [dict(r) for r in self._prev_records(kind)])
            reports["report"] = report
            return new

        changed, _ = jsonfile.update(settings_path, mutate, create_parents=False) if os.path.exists(settings_path) \
            else (False, {})
        self.removed_kinds = self.removed_kinds | {kind}
        if changed:
            self.changed = True
            if kind == "claude":
                self.claude_changed = True
            else:
                self.codex_changed = True
            if report_note:
                self.say("removed the %s hook entries (not selected by --runtimes)" % kind)
            report = reports["report"]
            if kind == "codex" and report.shifted_events:
                self.say("Codex will ask you to re-review these events in /hooks: %s" % ", ".join(report.shifted_events))

    # -- finalize
    def _file_entry(self, base, full, kind, root):
        info = os.stat(full)
        return {"rel": os.path.relpath(full, base).replace(os.sep, "/"), "root": root, "sha256": _sha256_file(full),
                "mode": stat.S_IMODE(info.st_mode), "kind": kind}

    def _build_files(self):
        paths = self.paths
        entries = []
        for rel in payload_files(self.env.payload_dir):
            entries.append(self._file_entry(paths.runtime, os.path.join(self.version_dir, rel), "code", "runtime"))
        if self.mode != "adoption-pending":
            for path in (paths.hook_shim, paths.terminal_entry):
                entries.append(self._file_entry(paths.runtime, path, "shim", "runtime"))
            for name in self.forwarders:
                entries.append(self._file_entry(paths.runtime, os.path.join(paths.runtime, name), "forwarder",
                                                "runtime"))
            if self.mode == "active" and os.path.isfile(paths.profile):
                entries.append(self._file_entry(paths.runtime, paths.profile, "profile", "runtime"))
            if self.mode == "active" and os.path.isdir(paths.plugin_dir):
                for base, dirs, files in os.walk(paths.plugin_dir):
                    dirs.sort()
                    for name in sorted(files):
                        entries.append(self._file_entry(paths.plugin_dir, os.path.join(base, name), "plugin",
                                                        "plugin"))
        return sorted(entries, key=lambda e: (e["root"], e["rel"]))

    def _final_hooks(self):
        if self.mode != "active":
            return []
        written = {r["runtime"] for r in self._draft()["hooks"]} - self.removed_kinds
        kept = []
        for kind in RUNTIME_KINDS:
            if kind in self.removed_kinds:
                continue
            if kind in written:
                kept.extend(r for r in self._draft()["hooks"] if r["runtime"] == kind)
            else:
                kept.extend(self._prev_records(kind))
        return kept

    def _assemble_manifest(self):
        env, paths = self.env, self.paths
        previous = self.state.manifest
        outcome = self.consent["outcome"]
        stamp = _iso(env.now)
        files = self._build_files()
        hooks = self._final_hooks()
        plugin = {"deployed": self.mode == "active" and os.path.isdir(paths.plugin_dir),
                  "uuid": payload_build.PLUGIN_UUID, "action_uuid": payload_build.ACTION_UUID}
        node = self.binaries.get("node")
        if plugin["deployed"] and isinstance(node, dict):
            plugin["node_mode"] = node.get("mode")
        prev_adopted = (previous or {}).get("adopted_from")
        adopted_from = "prototype" if self.adoption else prev_adopted
        legacy_owned = self.legacy_owned if self.adoption or self.carry.legacy_owned_events is not None \
            else ((previous or {}).get("legacy_owned_events") or {})
        platform = env.platform_info
        data = manifest.new_manifest(
            paths=paths, component_version=env.component_version, payload_fingerprint=self.fingerprint,
            quoin_version=env.package_version,
            platform={"system": platform.system, "macos": platform.macos, "machine": platform.machine},
            mode=self.mode, binaries=self.binaries, zellij_socket_dir=self._manifest_socket(), files=files,
            version_dir=self.version_dir, previous_version_dir=None, hooks=hooks, plugin=plugin,
            prereqs=copy.deepcopy(outcome.prereqs), features_unavailable=list(outcome.features_unavailable),
            adopted_from=adopted_from, legacy_owned_events=legacy_owned, now=stamp)
        reasons = list(outcome.core_missing)
        if reasons and self.mode != "active":
            data["mode_reasons"] = reasons
        elif reasons and self.keep_recorded:
            data["mode_reasons"] = reasons
        if previous:
            data["installed_at"] = previous.get("installed_at", stamp)
            changed_version = previous.get("version_dir") != self.version_dir
            data["previous_version_dir"] = previous.get("version_dir") if changed_version \
                else previous.get("previous_version_dir")
            for name, entry in data["prereqs"].items():
                old = (previous.get("prereqs") or {}).get(name)
                if isinstance(old, dict) and all(old.get(k) == entry.get(k) for k in ("state", "path", "version")):
                    entry["checked_at"] = old.get("checked_at", entry.get("checked_at"))
            candidate = copy.deepcopy(data)
            candidate["updated_at"] = previous.get("updated_at", stamp)
            if manifest.same_except_verified(candidate, previous):
                candidate["last_verified_at"] = stamp
                return candidate, False
        return data, True

    def _finalize(self):
        paths = self.paths
        data, differs = self._assemble_manifest()
        if differs or not os.path.exists(paths.manifest):
            self.changed = True
        manifest.write(paths.manifest, data)
        self._cleanup(data)
        return data

    def _cleanup(self, data):
        paths = self.paths
        previous = self.state.manifest
        if self.mode != "adoption-pending":
            _remove_any(paths.uninstalled)
        _rmtree(self.rollback_dir)
        keep_files = {e["rel"] for e in data["files"] if e["root"] == "runtime"}
        protected = {"bin/quoin-deck-hook", "monitor.py"}
        for entry in (previous or {}).get("files") or []:
            rel = entry.get("rel")
            if entry.get("root") != "runtime" or not isinstance(rel, str) or rel in keep_files or rel in protected \
                    or rel.startswith("versions/"):
                continue
            target = os.path.join(paths.runtime, rel)
            if os.path.isfile(target) and os.path.realpath(target).startswith(os.path.realpath(paths.runtime) + os.sep):
                os.unlink(target)
                self.changed = True
        keep = {os.path.basename(data["version_dir"])}
        if data.get("previous_version_dir"):
            keep.add(os.path.basename(data["previous_version_dir"]))
        if os.path.isdir(paths.versions_dir):
            for name in os.listdir(paths.versions_dir):
                if name not in keep:
                    _remove_any(os.path.join(paths.versions_dir, name))
        stale = self.store.data["stale"]
        for key in ("rollback_dirs", "plugin_asides", "version_dir_asides"):
            for path in stale.get(key) or []:
                _remove_any(path)
        previous_marker = self.store.data.get("previous") or {}
        aside = previous_marker.get("plugin_aside")
        if isinstance(aside, dict) and aside.get("path") and not aside.get("kept"):
            _remove_any(aside["path"])
        try:
            os.rmdir(paths.staging)  # only when empty
        except OSError:
            pass
        kept_dirs = []
        if os.path.isdir(paths.plugins_dir):
            kept_dirs = sorted(n for n in os.listdir(paths.plugins_dir) if n.startswith(".quoin-plugin-kept-"))
        for name in kept_dirs:
            if name not in self.warnings_done:
                self.warnings_done.add(name)
                self.say("note: a plugin directory that is not ours was kept as %s"
                         % os.path.join(paths.plugins_dir, name))

    # -- report
    def _report(self, data):
        env = self.env
        mode = data["mode"]
        self.say("Stream Deck component %s %s (%s)" % (data["component_version"],
                                                       "installed" if self.op == "install" else "upgraded", mode))
        if mode == "dormant":
            self.say("installed DORMANT: no plugin and no hooks until the missing items are installed; re-run "
                     "`quoin streamdeck install`")
        elif mode == "adoption-pending":
            self.say("the prototype install was left untouched (adoption pending): install the missing items, then "
                     "re-run `quoin streamdeck install`")
        if mode == "active":
            profile = self.paths.profile
            self.say("profile: %s - import it in Stream Deck (double-click the file)" % profile)
            if env.open_profile:
                opener = (self.binaries.get("open") or {}).get("path")
                if opener:
                    env.sysops.run([opener, profile])
            if self.claude_changed:
                self.say("Claude Code: start new sessions (review in /hooks if asked)")
            if self.codex_changed:
                self.say("Codex: review the changed hooks in Codex /hooks (trust is never written by quoin)")
            if self.adoption and self.codex_changed:
                self.say("Codex hook definitions changed once (prototype -> managed); review them in Codex /hooks")
            if self.adoption and "codex" not in self.selected \
                    and (self.legacy_owned or {}).get("codex"):
                self.say("Codex was left on the legacy hooks (not selected by --runtimes)")
            if self.legacy_agents:
                self.say("restart these agents to pick up managed hooks:")
                for agent in self.legacy_agents:
                    self.say("  %s session %s pane %s" % (agent.get("kind"), agent.get("session"), agent.get("pane")))
            if self.plugin_changed and stream_deck_running(env.sysops):
                self.say("Stream Deck is running: the new plugin takes effect after you restart Stream Deck")


def shlex_first(command):
    import shlex
    try:
        return shlex.split(command)[0]
    except (ValueError, IndexError):
        return command


def install(env, op="install"):
    return _Run(env, op).execute()


def upgrade(env):
    return install(env, op="upgrade")


def check_partial_uninstall(marker, runtimes):
    """Refusal text when `uninstall --runtimes R` may not complete the marker on disk, else None."""
    kind = classify_marker(marker)
    if kind in ("none", "staging"):
        return None
    wanted = set(runtimes)
    if kind == "uninstall":
        marked = marker.get("runtimes")
        if isinstance(marked, list) and marked and set(marked) <= wanted:
            return None
        command = "quoin streamdeck uninstall" + (" --runtimes %s" % ",".join(marked) if marked else "")
        return "an interrupted uninstall is pending; run `%s` to complete it" % command
    if kind == "committed":
        return "an interrupted install is pending; run `quoin streamdeck install` to complete it"
    return "an interrupted operation is pending; run `quoin streamdeck install` or `quoin streamdeck uninstall` " \
           "to complete it"


class _Uninstall(_Run):
    """Roll-forward removal of the component. Every step is idempotent, so re-running converges."""

    def __init__(self, env):
        super().__init__(env, "uninstall")
        self.selected = list(env.runtimes) if env.runtimes is not None else None
        self.tombstoned = []

    # -- helpers
    def _kinds(self):
        return self.selected if self.selected is not None else list(RUNTIME_KINDS)

    def _read(self):
        return read_state(self.env, "uninstall", read_kinds=tuple(self._kinds()))

    def _recorded(self, hooks):
        return tuple(r["command"] for r in hooks if isinstance(r.get("command"), str))

    def _legacy_owned(self, state, carry):
        recorded = (state.manifest or {}).get("legacy_owned_events")
        if isinstance(recorded, dict) and recorded:
            return recorded
        if carry.legacy_owned_events:
            return carry.legacy_owned_events
        if state.detection.state == "prototype":
            return migrations.legacy_owned(state.claude[0], state.codex[0], manifest.runtime_names(state.paths))
        return {}

    def _removal(self, state, kind, hooks, legacy_owned, data):
        paths = state.paths
        names = manifest.runtime_names(paths)
        removable = [r["event"] for r in hooks if r.get("runtime") == kind and r.get("created_event_key")]
        removable += list((legacy_owned or {}).get(kind) or [])
        return hooks_merge.remove(data, kind, runtime_names=names, recorded=self._recorded(hooks),
                                  removable_event_keys=removable, only_runtime=paths.runtime)

    def _purge_targets(self, paths):
        found = [p for p in (paths.config, paths.state, os.path.join(paths.state_root, "agents"))
                 if os.path.lexists(p)]
        return found

    def _nothing(self, state, hooks, legacy_owned):
        if state.manifest is not None or state.marker_kind != "none" or state.manifest_problem:
            return False
        if state.detection.state == "prototype" and not self._kept_plugin_only(state):
            return False
        for kind in self._kinds():
            data = (state.claude if kind == "claude" else state.codex)[0]
            if self._removal(state, kind, hooks, legacy_owned, data)[1].changed:
                return False
        if self.env.purge and (state.detection.tombstones or self._purge_targets(state.paths)
                               or os.path.lexists(state.paths.uninstalled) or os.path.lexists(state.paths.lock)):
            return False
        return True

    def _kept_plugin_only(self, state):
        """Only a plugin directory that is not ours is left (it is kept, so there is nothing to remove)."""
        return state.detection.state == "prototype" and state.detection.reasons == ["plugin directory present"] \
            and _plugin_uuid(state.paths.plugin_dir) != payload_build.PLUGIN_UUID

    def _say_nothing(self, state):
        stubs = [p for p in state.detection.tombstones if os.path.lexists(p)]
        if not stubs:
            stubs = [p for p in manifest.read_uninstalled(state.paths) if os.path.lexists(p)]
        suffix = ""
        if stubs and not self.env.purge:
            suffix = " (%d inert hook stub%s remain; `quoin streamdeck uninstall --purge` removes them)" % (
                len(stubs), "" if len(stubs) == 1 else "s")
        self.say("nothing to remove" + suffix)
        if self._kept_plugin_only(state):
            self.say("warning: the plugin directory %s was kept (it is not ours or has no readable manifest.json)"
                     % state.paths.plugin_dir)
        return Result(0, None, False, self.lines, None)

    # -- entry
    def execute(self):
        env = self.env
        state = self._read()
        for note in state.notes:
            self.say(note)
        if self.selected is not None:
            refusal = check_partial_uninstall(state.marker, self.selected)
            if refusal:
                raise OperationError(refusal)
        hooks = (state.manifest or {}).get("hooks") or []
        if self._nothing(state, hooks, self._legacy_owned(state, Carry())):
            return self._say_nothing(state)
        if env.dry_run:
            return self._dry_run_uninstall(state, hooks)
        os.makedirs(state.paths.runtime, exist_ok=True)
        self.lock_fd = self._lock(state.paths)
        try:
            return self._locked_uninstall()
        finally:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass

    def _dry_run_uninstall(self, state, hooks):
        paths = state.paths
        if state.marker_kind == "staging":
            self.say("dry run: would roll back an interrupted operation")
        elif state.marker_kind != "none":
            self.say("dry run: would complete an interrupted operation")
        legacy_owned = self._legacy_owned(state, Carry())
        for kind in self._kinds():
            data = (state.claude if kind == "claude" else state.codex)[0]
            report = self._removal(state, kind, hooks, legacy_owned, data)[1]
            if report.changed:
                self.say("dry run: would remove the %s hook entries (%s)" % (kind, ", ".join(report.removed)))
        if self.selected is None:
            for path in (paths.plugin_dir, paths.versions_dir, paths.current, paths.profile_dir, paths.hook_shim,
                         paths.terminal_entry, paths.manifest):
                if os.path.lexists(path):
                    self.say("dry run: would remove %s" % path)
            for script in paths.legacy_root_scripts:
                if os.path.lexists(script):
                    self.say("dry run: would %s %s" % ("replace with an inert stub" if script.endswith("monitor.py")
                                                       else "remove", script))
            if self.env.purge:
                for path in self._purge_targets(paths):
                    self.say("dry run: would remove %s" % path)
        return Result(0, None, False, self.lines, state.manifest)

    # -- the locked part
    def _locked_uninstall(self):
        env = self.env
        marker_now = manifest.read_operation_marker(env.paths)
        if self.selected is not None:
            refusal = check_partial_uninstall(marker_now, self.selected)
            if refusal:
                raise OperationError(refusal)
        state = self._read()
        carry = self._recover(state)
        state = self._read()
        hooks = carry.prev_hooks if carry.completing else ((state.manifest or {}).get("hooks") or [])
        legacy_owned = self._legacy_owned(state, carry)
        if not carry.completing and self._nothing(state, hooks, legacy_owned):
            return self._say_nothing(state)
        self.state, self.paths, self.carry = state, state.paths, carry
        paths = state.paths
        adoption = bool(carry.adoption) or state.detection.state == "prototype" \
            or (state.manifest or {}).get("mode") == "adoption-pending"
        self.store = MarkerStore(paths.operation_marker)
        open_marker(self.store, op="uninstall", phase="committed", completing=carry.completing, carry=carry,
                    prev_hooks=hooks, adoption=adoption, previous={}, runtimes=self.selected,
                    draft_now={"legacy_owned_events": legacy_owned},
                    started_at=_iso(env.now))
        self.store.data["draft"]["hooks"] = [copy.deepcopy(r) for r in hooks if r.get("runtime") in self._kinds()]
        self.store.save()
        self.hooks, self.legacy_owned = hooks, legacy_owned
        self.removed_any = False
        try:
            for kind in RUNTIME_KINDS:
                with self.step("uninstall:%s_settings" % kind):
                    if kind in self._kinds():
                        self._uninstall_settings(kind)
            if self.selected is not None:
                self._finish_partial(state)
            else:
                with self.step("uninstall:tombstones"):
                    self._tombstones()
                with self.step("uninstall:remove_files"):
                    self._remove_files(state)
                with self.step("uninstall:manifest"):
                    _remove_any(paths.manifest)
                self.store.clear()
        except OperationError:
            raise
        except Exception as exc:
            raise OperationError("uninstall was interrupted (%s: %s); re-run `quoin streamdeck uninstall` to "
                                 "complete it" % (type(exc).__name__, exc))
        if self.selected is None and env.purge:
            self._purge()
        self._report_uninstall(state)
        return Result(0, None, self.changed, self.lines, None)

    def _uninstall_settings(self, kind):
        paths, env = self.paths, self.env
        path = paths.claude_settings if kind == "claude" else paths.codex_hooks
        if not os.path.exists(path):
            return
        current = jsonfile.read(path)[0]
        _, dry = self._removal(self.state, kind, self.hooks, self.legacy_owned, current)
        if not dry.changed:
            return
        jsonfile.backup(path, paths.backups, now=env.now)
        reports = {}

        def mutate(data):
            new, report = self._removal(self.state, kind, self.hooks, self.legacy_owned, data)
            reports["report"] = report
            return new

        changed, _ = jsonfile.update(path, mutate)
        if changed:
            self.changed = True
            self.removed_any = True
            report = reports["report"]
            if kind == "codex" and report.shifted_events:
                self.say("Codex will ask you to re-review these events in /hooks: %s" % ", ".join(report.shifted_events))

    def _finish_partial(self, state):
        paths = self.paths
        data = state.manifest
        if data is not None:
            new = copy.deepcopy(data)
            new["hooks"] = [r for r in data.get("hooks") or [] if r.get("runtime") not in self.selected]
            if data.get("mode") == "adoption-pending":
                owned = dict(data.get("legacy_owned_events") or {})
                for kind in self.selected:
                    owned.pop(kind, None)
                new["legacy_owned_events"] = owned
            if new != data:
                new["updated_at"] = _iso(self.env.now)
                manifest.write(paths.manifest, new)
                self.changed = True
        self.store.clear()

    def _tombstones(self):
        paths = self.paths
        listed = []
        for target, kind in ((paths.hook_shim, "sh"), (os.path.join(paths.runtime, "monitor.py"), "python")):
            if os.path.lexists(target):
                if write_file_atomic(target, shims.render_tombstone(kind).encode("utf-8"), 0o755):
                    self.changed = True
            if os.path.lexists(target):
                listed.append(target)
        for old in manifest.read_uninstalled(paths):
            if old not in listed and os.path.lexists(old) and manifest.is_tombstone(old):
                listed.append(old)
        self.tombstoned = listed
        if listed:
            payload = {"tombstones": listed, "uninstalled_at": _iso(self.env.now)}
            raw = _read_bytes(paths.uninstalled)
            jsonfile.write_atomic(paths.uninstalled, payload, expected_raw=raw, default_mode=0o644,
                                  create_parents=True)

    def _under(self, path, root):
        real_root = os.path.realpath(root)
        real = os.path.realpath(path)
        return real == real_root or real.startswith(real_root + os.sep)

    def _remove_files(self, state):
        paths = self.paths
        runtime = paths.runtime
        marker = self.store.data
        plugin_ours = os.path.isdir(paths.plugin_dir) and _plugin_uuid(paths.plugin_dir) == payload_build.PLUGIN_UUID
        if os.path.lexists(paths.plugin_dir) and not plugin_ours:
            self.say("warning: the plugin directory %s was kept (it is not ours or has no readable manifest.json)"
                     % paths.plugin_dir)
        keep = {os.path.normpath(p) for p in self.tombstoned}
        for entry in (state.manifest or {}).get("files") or []:
            rel = entry.get("rel")
            if not isinstance(rel, str) or entry.get("root") != "runtime" or rel.startswith("versions/"):
                continue
            target = os.path.join(runtime, rel)
            if os.path.normpath(target) in keep or not os.path.lexists(target):
                continue
            if self._under(target, runtime) and not os.path.islink(target):
                _remove_any(target)
                self.changed = True
        if plugin_ours:
            _remove_any(paths.plugin_dir)
            self.changed = True
        for name in shims.FORWARDER_NAMES:
            target = os.path.join(runtime, name)
            if name != "monitor.py" and os.path.lexists(target):
                _remove_any(target)
                self.changed = True
        for path in (paths.current, paths.versions_dir, paths.staging, paths.profile_dir, paths.terminal_entry):
            if os.path.lexists(path):
                _remove_any(path)
                self.changed = True
        stale = marker.get("stale") or {}
        for key in ("rollback_dirs", "plugin_asides", "version_dir_asides"):
            for path in stale.get(key) or []:
                if ".quoin-plugin-kept-" not in path:
                    _remove_any(path)
        aside = (marker.get("previous") or {}).get("plugin_aside")
        if isinstance(aside, dict) and aside.get("path") and not aside.get("kept"):
            _remove_any(aside["path"])
        try:
            os.rmdir(paths.bin_dir)
        except OSError:
            pass

    def _purge(self):
        paths = self.paths
        agents = migrations.collect_agents(paths)
        if agents:
            self.say("restart these agents now: their hooks fail once the stubs are gone:")
            for agent in agents:
                self.say("  %s session %s pane %s" % (agent.get("kind"), agent.get("session"), agent.get("pane")))
        for path in [paths.hook_shim, os.path.join(paths.runtime, "monitor.py")]:
            if os.path.lexists(path) and manifest.is_tombstone(path):
                os.unlink(path)
        for path in (paths.uninstalled, paths.lock, paths.config, paths.state,
                     os.path.join(paths.state_root, "agents"), os.path.join(paths.state_root, "errors.log"),
                     os.path.join(paths.state_root, "monitor.lock"), os.path.join(paths.state_root, "health.json")):
            _remove_any(path)
        for directory in (paths.bin_dir, paths.state_root, paths.runtime):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        self.changed = True

    def _report_uninstall(self, state):
        if self.selected is not None:
            self.say("removed the %s hook entries; the Stream Deck component stays installed"
                     % ", ".join(self.selected))
            if state.manifest is None and state.detection.state == "prototype":
                self.say("the prototype stays in place for the other runtime")
            return
        self.say("Stream Deck component removed")
        stubs = [p for p in self.tombstoned if os.path.lexists(p)]
        if stubs and not self.env.purge:
            self.say("%d inert hook stub%s remain so running agents keep working; `quoin streamdeck uninstall "
                     "--purge` removes them (restart your agents first)" % (len(stubs), "" if len(stubs) == 1 else "s"))
        self.say("Stream Deck: delete the Quoin Live Agents profile in the app if you imported it")


def uninstall(env):
    macos_info = env.platform_info
    if not macos_info.supported:
        env.out("quoin streamdeck: macOS only (found %s); nothing to remove" % macos_info.system)
        return Result(0, None, False, [], None)
    return _Uninstall(env).execute()
