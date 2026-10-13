"""Install manifest (schema 1): build, validate, load, write, and install-state detection.

The manifest is the contract between the installer and the deployed payload
(`payload/deckenv.py` reads `paths`, `binaries` and `zellij_socket_dir`) and
the source of truth for `status` once it loads.
"""
import copy
import dataclasses
import datetime
import json
import os
import re

from . import hooks_merge, jsonfile

SCHEMA = 1
COMPONENT = "streamdeck"
MANIFEST_NAME = "install-manifest.json"
TOMBSTONE_MARKER = "quoin-streamdeck-tombstone v1"
PLUGIN_UUID = "local.quoin.agentdesk"
ACTION_UUID = "local.quoin.agentdesk.dashboard"
CORE_BINARY_KEYS = ("python", "node", "zellij", "osascript", "open", "ghostty_app", "agentdesk")
OPTIONAL_TOOL_KEYS = ("claude", "codex", "ccr", "lazygit", "python3")
MODES = ("active", "dormant", "adoption-pending")
PATH_KEYS = ("runtime", "plugin_dir", "config", "state", "claude_settings", "codex_hooks", "agentdesk")
FILE_KINDS = ("code", "shim", "forwarder", "plugin", "profile")
FILE_ROOTS = ("runtime", "plugin")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _stamp(now):
    if isinstance(now, str):
        return now
    return now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if now.tzinfo \
        else now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return copy.deepcopy(value)


def new_manifest(*, paths, component_version, payload_fingerprint, quoin_version, platform, mode,
                 binaries, zellij_socket_dir, files, version_dir, previous_version_dir, hooks, plugin,
                 prereqs, features_unavailable, adopted_from, legacy_owned_events, now):
    stamp = _stamp(now)
    path_map = paths.manifest_paths() if hasattr(paths, "manifest_paths") else dict(paths)
    return {
        "schema": SCHEMA,
        "component": COMPONENT,
        "component_version": component_version,
        "payload_fingerprint": payload_fingerprint,
        "quoin_version": quoin_version,
        "platform": _plain(platform),
        "mode": mode,
        "installed_at": stamp,
        "updated_at": stamp,
        "last_verified_at": stamp,
        "paths": path_map,
        "binaries": _plain(binaries),
        "zellij_socket_dir": zellij_socket_dir,
        "version_dir": version_dir,
        "previous_version_dir": previous_version_dir,
        "files": _plain(files),
        "hooks": _plain(hooks),
        "plugin": _plain(plugin),
        "prereqs": _plain(prereqs),
        "features_unavailable": list(features_unavailable),
        "adopted_from": _plain(adopted_from),
        "legacy_owned_events": list(legacy_owned_events),
    }


def _is_abs(value):
    return isinstance(value, str) and os.path.isabs(value)


def _bin_path(entry):
    return entry.get("path") if isinstance(entry, dict) else entry


def validate(data):
    problems = []
    if not isinstance(data, dict):
        return ["manifest is not an object"]
    if data.get("schema") != SCHEMA:
        problems.append("schema must be %d" % SCHEMA)
    if data.get("component") != COMPONENT:
        problems.append("component must be %r" % COMPONENT)
    if data.get("mode") not in MODES:
        problems.append("mode must be one of %s" % ", ".join(MODES))
    paths = data.get("paths")
    if not isinstance(paths, dict):
        problems.append("paths missing")
        paths = {}
    for key in PATH_KEYS:
        if not _is_abs(paths.get(key)):
            problems.append("paths.%s must be an absolute path" % key)
    config, state = paths.get("config"), paths.get("state")
    if _is_abs(config):
        if os.path.basename(config) != "config.json":
            problems.append("paths.config must be named config.json (state-dir invariant)")
        if _is_abs(state) and state != os.path.join(os.path.dirname(config), "state"):
            problems.append("paths.state must be <dirname(paths.config)>/state (state-dir invariant)")
    for key, value in paths.items():
        if isinstance(value, str) and "versions/" in value:
            problems.append("paths.%s must not point into versions/ (stable-path rule)" % key)
    binaries = data.get("binaries")
    if not isinstance(binaries, dict):
        problems.append("binaries missing")
        binaries = {}
    for key, entry in binaries.items():
        if key not in CORE_BINARY_KEYS and key not in OPTIONAL_TOOL_KEYS:
            problems.append("binaries.%s is not a known key" % key)
            continue
        if key == "node" and isinstance(entry, dict):
            mode = entry.get("mode")
            if mode not in ("external", "bundled"):
                problems.append("binaries.node.mode must be external or bundled")
            elif mode == "bundled":
                if entry.get("path") is not None:
                    problems.append("binaries.node.path must be null for bundled mode")
            elif not _is_abs(entry.get("path")):
                problems.append("binaries.node.path must be an absolute path for external mode")
            continue
        if key == "node":
            problems.append("binaries.node must be an object with a mode")
            continue
        if not _is_abs(_bin_path(entry)):
            problems.append("binaries.%s path must be absolute" % key)
    files = data.get("files")
    if not isinstance(files, list):
        problems.append("files must be a list")
        files = []
    for i, entry in enumerate(files):
        if not isinstance(entry, dict):
            problems.append("files[%d] is not an object" % i)
            continue
        rel = entry.get("rel")
        if not isinstance(rel, str) or os.path.isabs(rel) or ".." in rel.split("/") or rel == "":
            problems.append("files[%d].rel must be a relative path without '..'" % i)
        if entry.get("root") not in FILE_ROOTS:
            problems.append("files[%d].root must be runtime or plugin" % i)
        if not (isinstance(entry.get("sha256"), str) and _HEX64.match(entry["sha256"])):
            problems.append("files[%d].sha256 must be 64 hex characters" % i)
        if not isinstance(entry.get("mode"), int) or isinstance(entry.get("mode"), bool):
            problems.append("files[%d].mode must be an integer" % i)
        if entry.get("kind") not in FILE_KINDS:
            problems.append("files[%d].kind invalid" % i)
    hooks = data.get("hooks")
    if not isinstance(hooks, list):
        problems.append("hooks must be a list")
        hooks = []
    for i, entry in enumerate(hooks):
        if not isinstance(entry, dict):
            problems.append("hooks[%d] is not an object" % i)
            continue
        runtime = entry.get("runtime")
        if runtime not in hooks_merge.EVENTS:
            problems.append("hooks[%d].runtime must be claude or codex" % i)
        elif entry.get("event") not in hooks_merge.EVENTS[runtime]:
            problems.append("hooks[%d].event is not in the %s registry" % (i, runtime))
        if not _is_abs(entry.get("settings_path")):
            problems.append("hooks[%d].settings_path must be absolute" % i)
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            problems.append("hooks[%d].command missing" % i)
        elif "versions/" in command:
            problems.append("hooks[%d].command must not point into versions/ (stable-path rule)" % i)
        if not isinstance(entry.get("timeout"), int) or isinstance(entry.get("timeout"), bool):
            problems.append("hooks[%d].timeout must be an integer" % i)
        if not isinstance(entry.get("created_event_key"), bool):
            problems.append("hooks[%d].created_event_key must be a boolean" % i)
    return problems


def load(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError(["manifest is not valid JSON: %s" % exc])
    problems = validate(data)
    if problems:
        raise ManifestError(problems)
    return data


def write(path, data):
    problems = validate(data)
    if problems:
        raise ManifestError(problems)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raw = None
    jsonfile.write_atomic(path, data, expected_raw=raw, default_mode=0o644, create_parents=True)


def same_except_verified(a, b):
    a, b = dict(a), dict(b)
    a.pop("last_verified_at", None)
    b.pop("last_verified_at", None)
    return a == b


def record_optional_tools(which):
    out = {}
    for name in OPTIONAL_TOOL_KEYS:
        found = which(name)
        if isinstance(found, str) and os.path.isabs(found):
            out[name] = {"path": found}
    return out


# ------------------------------------------------------------------ detection

def is_tombstone(path):
    try:
        with open(path, "rb") as handle:
            return TOMBSTONE_MARKER.encode() in handle.read(512)
    except OSError:
        return False


def read_uninstalled(paths):
    try:
        with open(paths.uninstalled, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("tombstones", [])
    return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []


def read_operation_marker(paths):
    try:
        with open(paths.operation_marker, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    except OSError:
        return {"phase": "unreadable"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"phase": "unreadable"}
    if not isinstance(data, dict):
        return {"phase": "unreadable"}
    return data


@dataclasses.dataclass
class Detection:
    state: str
    reasons: list
    tombstones: list
    legacy_hook_count: int


def runtime_names(paths):
    return tuple(dict.fromkeys((os.path.basename(paths.runtime), "QuoinStreamDeck")))


def detect(paths, claude_data, codex_data):
    reasons = []
    tombstones = []
    live_scripts = []
    for script in paths.legacy_root_scripts:
        if os.path.lexists(script):
            (tombstones if is_tombstone(script) else live_scripts).append(script)
    names = runtime_names(paths)
    legacy = 0
    for data, kind in ((claude_data, "claude"), (codex_data, "codex")):
        if isinstance(data, dict):
            hooks = data.get("hooks")
            if isinstance(hooks, dict):
                for lst in hooks.values():
                    if not isinstance(lst, list):
                        continue
                    for group in lst:
                        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                            for hook in group["hooks"]:
                                if isinstance(hook, dict) and hooks_merge.classify(
                                        hook.get("command"), runtime_names=names) == "legacy":
                                    legacy += 1
    if os.path.lexists(paths.manifest):
        return Detection("managed", ["manifest present"], tombstones, legacy)
    if live_scripts:
        reasons.append("prototype scripts: " + ", ".join(os.path.basename(s) for s in live_scripts))
    if os.path.lexists(paths.plugin_dir):
        reasons.append("plugin directory present")
    if legacy:
        reasons.append("%d legacy hook entries" % legacy)
    if reasons:
        return Detection("prototype", reasons, tombstones, legacy)
    if not tombstones:
        listed = [p for p in read_uninstalled(paths)]
        tombstones = listed
    if tombstones:
        return Detection("tombstones-only", ["inert hook stubs remain"], tombstones, legacy)
    return Detection("not-installed", [], [], legacy)
