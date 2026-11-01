"""Read-only installation report.

Uses only reads (`open` in read mode, stat, readlink) and the library readers;
it never takes a lock, creates a directory or runs a subprocess. Once a valid
manifest loads, every location it checks comes from the manifest, so a shell
whose environment differs from the install shell still reports correctly.
"""
import datetime
import hashlib
import json
import os
import re
import stat

from . import hooks_merge, jsonfile, manifest, payload_build

try:
    import tomllib as _tomllib
except ImportError:  # Python 3.10
    _tomllib = None

SCHEMA = 1
STALE_HEALTH_SECONDS = 300
CORE_PREREQS = ("python", "streamdeck-app", "node")
LEFTOVER_NAMES = ("backups", "Buttons", "prototype-backup.sdProfile", "health.json", ".DS_Store")
_TRUST_LINE = re.compile(r'^\[hooks\.state\."(.+)"\]\s*$')


def _snake(event):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def _now_ms(now):
    if isinstance(now, datetime.datetime):
        return int(now.timestamp() * 1000)
    return int(float(now) * 1000)


PROBLEM_CODES = (
    "OPERATION_INCOMPLETE", "MANIFEST_INVALID", "PROTOTYPE_UNADOPTED", "SETTINGS_UNREADABLE",
    "SETTINGS_MALFORMED", "FILE_MISSING", "FILE_MODIFIED", "MODE_DORMANT", "MODE_ADOPTION_PENDING",
    "CURRENT_MISSING", "CURRENT_MISMATCH", "HOOKS_NONE_RECORDED", "HOOK_MISSING", "HOOK_DUPLICATE",
    "HOOK_STALE", "HOOK_SHADOWED", "PREREQ_CORE_MISSING", "BINARY_VANISHED", "HEALTH_STALE",
)
INFO_CODES = (
    "PATHS_DIFFER", "UPGRADE_AVAILABLE", "UNMANAGED_LEFTOVER", "CODEX_TRUST", "PREREQ_FEATURE_UNAVAILABLE",
    "OPTIONAL_TOOL_MISSING", "PLUGIN_NEVER_RAN", "HEALTH_UNREADABLE", "PLUGIN_SNAPSHOT_ERROR",
    "SETTINGS_UNREADABLE", "SETTINGS_MALFORMED", "PROFILE_NOT_IMPORTED",
)


class _SettingsError:
    def __init__(self, code, path, detail):
        self.code, self.path, self.detail = code, path, detail


def _read_settings(path):
    try:
        data, _ = jsonfile.read(path)
        return data, None
    except jsonfile.MalformedJSON as exc:
        return {}, _SettingsError("SETTINGS_MALFORMED", exc.path, exc.detail)
    except OSError as exc:
        return {}, _SettingsError("SETTINGS_UNREADABLE", path, "%s: %s" % (type(exc).__name__, exc.strerror or exc))


class NotRegular(OSError):
    pass


def _sha256(path):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise NotRegular("not a regular file")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bin_path(entry):
    return entry.get("path") if isinstance(entry, dict) else entry


def _trust_keys(text):
    if _tomllib is not None:
        try:
            parsed = _tomllib.loads(text)
            state = parsed.get("hooks", {}).get("state", {})
            if isinstance(state, dict):
                return set(state)
        except Exception:
            pass
    keys = set()
    for line in text.splitlines():
        match = _TRUST_LINE.match(line.strip())
        if match:
            keys.add(match.group(1))
    return keys


class _Report:
    def __init__(self):
        self.d = {
            "schema": SCHEMA, "state": None, "platform": None, "installed_version": None,
            "package_version": None, "mode": None, "paths": {}, "files": {"checked": 0, "missing": [], "modified": []},
            "hooks": {}, "codex_trust": [], "prereqs": {}, "problems": [], "info": [], "exit_code": 0,
        }

    def problem(self, code, detail):
        self.d["problems"].append({"code": code, "detail": detail})

    def info(self, code, detail):
        self.d["info"].append({"code": code, "detail": detail})


def _leftovers(rep, runtime):
    for name in LEFTOVER_NAMES:
        if os.path.lexists(os.path.join(runtime, name)):
            rep.info("UNMANAGED_LEFTOVER", os.path.join(runtime, name))


def _trust_hint(rep, hooks_path, data, names, recorded, read_text):
    config = os.path.join(os.path.dirname(hooks_path), "config.toml")
    owned = hooks_merge.owned_commands(data, "codex", runtime_names=names, recorded=recorded)
    if not owned:
        return
    try:
        text = read_text(config)
    except OSError:
        rep.info("CODEX_TRUST", "no Codex config at %s" % config)
        return
    keys = _trust_keys(text)
    for event, gi, hi in owned:
        key = "%s:%s:%d:%d" % (hooks_path, _snake(event), gi, hi)
        present = key in keys
        rep.d["codex_trust"].append({"event": event, "key": key, "present": present})
        rep.info("CODEX_TRUST", "%s: %s" % (event, "trust record present at this position (definition match "
                                           "not verifiable)" if present else "no trust record - review in Codex /hooks"))


def _check_valid(rep, paths, data, package_version, package_fingerprint, now, read_text):
    rec = data["paths"]
    rep.d["paths"] = dict(rec)
    resolved = paths.manifest_paths()
    differ = {k: v for k, v in resolved.items() if rec.get(k) != v}
    for key, value in differ.items():
        rep.info("PATHS_DIFFER", "%s: recorded %s, resolved %s (status uses the recorded path)" % (key, rec.get(key), value))
    if differ:
        rep.d["paths_resolved"] = resolved
    rep.d["installed_version"] = data.get("component_version")
    rep.d["mode"] = data.get("mode")
    if data.get("component_version") != package_version or data.get("payload_fingerprint") != package_fingerprint:
        rep.info("UPGRADE_AVAILABLE", "installed %s, package %s: run `quoin streamdeck upgrade`"
                 % (data.get("component_version"), package_version))
    roots = {"runtime": rec["runtime"], "plugin": rec["plugin_dir"]}
    for entry in data["files"]:
        target = os.path.join(roots[entry["root"]], entry["rel"])
        rep.d["files"]["checked"] += 1
        try:
            actual = _sha256(target)
        except OSError as exc:
            rep.d["files"]["missing"].append(entry["rel"])
            rep.problem("FILE_MISSING", "%s (not a regular file)" % target if isinstance(exc, NotRegular) else target)
            continue
        if actual != entry["sha256"]:
            rep.d["files"]["modified"].append(entry["rel"])
            rep.problem("FILE_MODIFIED", target)
    mode = data["mode"]
    reasons = data.get(manifest.MODE_REASON_KEY)
    if not reasons:
        reasons = [name for name, entry in sorted((data.get("prereqs") or {}).items())
                   if name in CORE_PREREQS and not (isinstance(entry, dict) and entry.get("state") in ("ok", "installed"))]
    reasons = ", ".join(reasons)
    if mode == "dormant":
        rep.problem("MODE_DORMANT", "installed without plugin and hooks; missing core prerequisites %s" % reasons)
    elif mode == "adoption-pending":
        rep.problem("MODE_ADOPTION_PENDING", "prototype not yet adopted; missing core prerequisites %s" % reasons)
    if mode in ("active", "dormant"):
        link = os.path.join(rec["runtime"], "current")
        try:
            target = os.readlink(link)
        except OSError:
            rep.problem("CURRENT_MISSING", "%s is missing or not a symlink" % link)
        else:
            if not os.path.isabs(target):
                target = os.path.join(rec["runtime"], target)
            target = os.path.normpath(target)
            want = os.path.normpath(data.get("version_dir") or "")
            if target != want or not os.path.isdir(target):
                rep.problem("CURRENT_MISMATCH", "current points at %s, manifest records %s" % (target, want))
    names = tuple(dict.fromkeys((os.path.basename(rec["runtime"]), "QuoinStreamDeck")))
    recorded_cmds = tuple(h["command"] for h in data["hooks"])
    if mode == "active" and not data["hooks"]:
        rep.problem("HOOKS_NONE_RECORDED", "active install with no recorded hook entries")
    for runtime in sorted({h["runtime"] for h in data["hooks"]}):
        entries = [h for h in data["hooks"] if h["runtime"] == runtime]
        settings_path = entries[0]["settings_path"]
        settings, error = _read_settings(settings_path)
        if error is not None:
            rep.problem(error.code, "%s: %s" % (settings_path, error.detail))
            continue
        states = hooks_merge.inspect(settings, runtime, entries[0]["command"], runtime_names=names, recorded=recorded_cmds)
        rep.d["hooks"][runtime] = states
        for event, state in sorted(states.items()):
            if state != "ok":
                rep.problem("HOOK_" + state.upper(), "%s:%s in %s" % (runtime, event, settings_path))
        if runtime == "codex":
            _trust_hint(rep, settings_path, settings, names, recorded_cmds, read_text)
    prereqs = data.get("prereqs") or {}
    rep.d["prereqs"] = prereqs
    for name, entry in sorted(prereqs.items()):
        state = entry.get("state") if isinstance(entry, dict) else None
        if state in ("ok", "installed"):
            continue
        if name in CORE_PREREQS:
            rep.problem("PREREQ_CORE_MISSING", "%s: %s" % (name, state))
        elif name != "homebrew":
            rep.info("PREREQ_FEATURE_UNAVAILABLE", "%s: %s (not installed by choice)" % (name, state))
    for key in manifest.CORE_BINARY_KEYS:
        entry = (data.get("binaries") or {}).get(key)
        if entry is None or (key == "node" and isinstance(entry, dict) and entry.get("mode") == "bundled"):
            continue
        path = _bin_path(entry)
        good = os.path.isdir(path) if key == "ghostty_app" else (os.path.isfile(path) and os.access(path, os.X_OK))
        if key == "agentdesk":
            good = os.path.isfile(path)
        if not good:
            rep.problem("BINARY_VANISHED", "%s: %s" % (key, path))
    for key in manifest.OPTIONAL_TOOL_KEYS:
        entry = (data.get("binaries") or {}).get(key)
        if entry is None:
            rep.info("OPTIONAL_TOOL_MISSING", "%s: not found when installed" % key)
        elif not os.access(_bin_path(entry), os.X_OK):
            rep.info("OPTIONAL_TOOL_MISSING", "%s: %s no longer exists" % (key, _bin_path(entry)))
    plugin = data.get("plugin") or {}
    if mode == "active" and isinstance(plugin, dict) and plugin.get("deployed"):
        health = os.path.join(rec["state"], "health.json")
        try:
            with open(health, "rb") as handle:
                parsed = json.loads(handle.read().decode("utf-8"))
        except FileNotFoundError:
            rep.info("PLUGIN_NEVER_RAN", "no health file yet: start Stream Deck")
        except (OSError, ValueError):
            rep.info("HEALTH_UNREADABLE", health)
        else:
            updated = parsed.get("updated") if isinstance(parsed, dict) else None
            if isinstance(updated, (int, float)) and not isinstance(updated, bool):
                if (_now_ms(now) - updated) > STALE_HEALTH_SECONDS * 1000:
                    rep.problem("HEALTH_STALE", "plugin health older than %d s: is Stream Deck running?" % STALE_HEALTH_SECONDS)
            if isinstance(parsed, dict) and parsed.get("error_type"):
                rep.info("PLUGIN_SNAPSHOT_ERROR", str(parsed["error_type"]))
        if not payload_build.profile_imported(os.path.dirname(rec["plugin_dir"])):
            rep.info("PROFILE_NOT_IMPORTED", "Stream Deck has no Quoin Live Agents profile: open %s"
                     % os.path.join(rec["runtime"], "profile", "Quoin Live Agents.streamDeckProfile"))
    _leftovers(rep, rec["runtime"])


def collect(paths, *, package_version, package_fingerprint, platform_info, now, read_text=None):
    def default_read(path):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    read_text = read_text or default_read
    rep = _Report()
    d = rep.d
    d["package_version"] = package_version
    d["platform"] = {"system": platform_info.system, "macos": platform_info.macos, "machine": platform_info.machine}
    if not platform_info.supported:
        d["state"] = "unsupported-platform"
        return d
    d["paths"] = paths.manifest_paths()
    marker = manifest.read_operation_marker(paths)
    if marker is not None:
        d["state"] = "operation-incomplete"
        if marker.get("op") == "uninstall":
            runtimes = marker.get("runtimes")
            usable = isinstance(runtimes, list) and runtimes and all(isinstance(r, str) for r in runtimes)
            command = "uninstall" + (" --runtimes %s" % ",".join(runtimes) if usable else "")
        else:
            command = "install"
        rep.problem("OPERATION_INCOMPLETE", "op %s, phase %s, steps done %s; run `quoin streamdeck %s` to "
                    "complete it" % (marker.get("op"), marker.get("phase"), marker.get("steps_done"), command))
        return d
    try:
        data = manifest.load(paths.manifest)
    except manifest.ManifestError as exc:
        d["state"] = "manifest-invalid"
        rep.problem("MANIFEST_INVALID", "; ".join(exc.problems))
        return d
    if data is None:
        claude, claude_err = _read_settings(paths.claude_settings)
        codex, codex_err = _read_settings(paths.codex_hooks)
        det = manifest.detect(paths, claude, codex)
        d["state"] = det.state
        if det.state == "prototype":
            rep.problem("PROTOTYPE_UNADOPTED", "; ".join(det.reasons))
            _leftovers(rep, paths.runtime)
        elif det.state == "tombstones-only":
            d["tombstones"] = len(det.tombstones)
        for err in (claude_err, codex_err):
            if err is not None:
                rep.info(err.code, "%s: %s" % (err.path, err.detail))
        return d
    d["state"] = "installed"
    _check_valid(rep, paths, data, package_version, package_fingerprint, now, read_text)
    if d["mode"] != "active":
        d["state"] = d["mode"]
    return d


def exit_code(report):
    codes = {p["code"] for p in report["problems"]}
    if "OPERATION_INCOMPLETE" in codes:
        code = 12
    elif codes:
        code = 11
    elif report["state"] in ("not-installed", "tombstones-only"):
        code = 10
    else:
        code = 0
    return code


def finalize(report):
    report["exit_code"] = exit_code(report)
    return report


def render_json(report):
    return json.dumps(finalize(report), indent=2, sort_keys=True)


def render_text(report):
    finalize(report)
    state = report["state"]
    out = []
    if state == "unsupported-platform":
        return "Stream Deck component: unsupported platform (macOS only)\n"
    if state == "not-installed":
        out.append("Stream Deck component: not installed")
    elif state == "tombstones-only":
        out.append("Stream Deck component: not installed (%d inert hook stubs remain; remove with uninstall --purge)"
                   % report.get("tombstones", 0))
    else:
        out.append("Stream Deck component: %s (installed %s, package %s)" % (
            state, report.get("installed_version"), report.get("package_version")))
    if report["problems"]:
        out.append("Problems:")
        out.extend("  %s: %s" % (p["code"], p["detail"]) for p in report["problems"])
    declined = [i for i in report["info"] if i["code"] == "PREREQ_FEATURE_UNAVAILABLE"]
    if declined:
        out.append("Not installed by choice:")
        out.extend("  %s" % i["detail"] for i in declined)
    rest = [i for i in report["info"] if i["code"] != "PREREQ_FEATURE_UNAVAILABLE"]
    if rest:
        out.append("Notes:")
        out.extend("  %s: %s" % (i["code"], i["detail"]) for i in rest)
    out.append("exit code %d" % report["exit_code"])
    return "\n".join(out) + "\n"
