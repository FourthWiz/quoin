"""Prerequisite registry, read-only checks and the consent engine.

Every outside effect (which, probes, prompts, installs, TTY detection) goes
through an injectable `Ctx`. The only place real subprocess, input and isatty
calls are bound is `default_ctx` with `_real_run_probe` and `_real_runner`.
"""
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple

CLT_STUB = "/usr/bin/python3"
PY_PROBE = "import sys,fcntl;print('%d.%d.%d'%sys.version_info[:3])"
WEBSOCKET_PROBE = "typeof WebSocket==='function'||process.exit(1)"
APP_NAME = "Elgato Stream Deck.app"
APP_BUNDLE_ID = "com.elgato.StreamDeck"
GHOSTTY_APP = "Ghostty.app"
SDEF_PHRASE = "new surface configuration"
_API_RE = re.compile(r"^AGENTDESK_API_VERSION=(\d+)", re.M)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    path: Optional[str] = None
    version: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class InstallPlan:
    kind: str
    argv: Optional[Tuple[str, ...]]
    display: str
    needs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Prereq:
    name: str
    what: str
    why: str
    features: Tuple[str, ...]
    check: Callable
    install: Optional[InstallPlan]
    severity: str


@dataclass
class Ctx:
    which: Callable
    realpath: Callable
    run_probe: Callable
    stdin_isatty: Callable
    stdout_isatty: Callable
    prompt: Callable
    runner: Callable
    deploy_agentdesk: Callable
    environ: dict
    apps_dir: str
    home: str
    sys_prefix: str
    virtual_env: Optional[str]
    flags: dict = field(default_factory=dict)
    recorded: dict = field(default_factory=dict)
    legacy: dict = field(default_factory=dict)
    out: Callable = print
    now: Callable = None


@dataclass
class Outcome:
    prereqs: dict
    features_unavailable: list
    core_missing: list
    lines: list


# --------------------------------------------------------------------- binding

def _real_run_probe(argv, timeout=5):
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", type(exc).__name__
    return proc.returncode, proc.stdout, proc.stderr


def _real_runner(argv):
    try:
        return subprocess.run(list(argv)).returncode
    except OSError:
        return 127


def deploy_agentdesk_files(agentdesk_path):
    """Copy the packaged Agent Desk files next to `agentdesk_path`; 0 on success, 1 when no source tree resolves."""
    # Lazy: the installer is only needed when the user accepts this item.
    import pathlib
    from quoin import cli, installer
    try:
        source_dir = cli._resolve_source_dir(None)
    except SystemExit:
        # Source resolution exits the process when no source tree is found; the consent
        # engine only catches Exception, so report it as a failed deploy instead.
        return 1
    installer.deploy_agentdesk(source_dir, pathlib.Path(os.path.dirname(agentdesk_path)))
    return 0


def default_ctx(paths, flags, recorded=None, legacy=None):
    import datetime
    flags = dict(flags or {})
    flags.setdefault("agentdesk", paths.agentdesk)

    def deploy():
        return deploy_agentdesk_files(paths.agentdesk)

    return Ctx(
        which=shutil.which,
        realpath=os.path.realpath,
        run_probe=_real_run_probe,
        stdin_isatty=lambda: sys.stdin.isatty(),
        stdout_isatty=lambda: sys.stdout.isatty(),
        prompt=input,
        runner=_real_runner,
        deploy_agentdesk=deploy,
        environ=dict(os.environ),
        apps_dir="/Applications",
        home=paths.home,
        sys_prefix=sys.prefix,
        virtual_env=os.environ.get("VIRTUAL_ENV"),
        flags=flags,
        recorded=dict(recorded or {}),
        legacy=dict(legacy or {}),
        out=lambda text: print(text),
        now=lambda: datetime.datetime.now(datetime.timezone.utc),
    )


# --------------------------------------------------------------------- helpers

def _bin_path(entry):
    if isinstance(entry, dict):
        entry = entry.get("path")
    return entry if isinstance(entry, str) and entry else None


def _vtuple(text):
    parts = re.findall(r"\d+", text or "")[:3]
    return tuple(int(p) for p in parts) if parts else ()


def parse_legacy_python(command):
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    return argv[0] if argv else None


def parse_launch_sh(text):
    for line in text.splitlines():
        match = re.match(r"^\s*exec\s+(.*)$", line)
        if match:
            try:
                argv = shlex.split(match.group(1))
            except ValueError:
                return None
            return argv[0] if argv else None
    return None


def _clt_ok(ctx, memo):
    if "clt" not in memo:
        try:
            memo["clt"] = ctx.run_probe(["xcode-select", "-p"], 5)[0] == 0
        except Exception:
            memo["clt"] = False
    return memo["clt"]


def _login_which(ctx, name):
    found = ctx.which(name)
    if found:
        return found
    try:
        rc, out, _ = ctx.run_probe(["/bin/zsh", "-lc", "command -v %s" % name], 5)
    except Exception:
        return None
    line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
    return line if rc == 0 and os.path.isabs(line) else None


# ---------------------------------------------------------------------- checks

def python_candidates(ctx):
    """[(source, path)] in priority order, de-duplicated by unresolved path."""
    out, seen = [], set()

    def add(source, path):
        if path and path not in seen:
            seen.add(path)
            out.append((source, path))

    flag = ctx.flags.get("python")
    if flag:
        add("flag", flag)
        return out
    add("recorded", _bin_path(ctx.recorded.get("python")))
    add("legacy", ctx.legacy.get("python"))
    add("fallback", CLT_STUB)
    found = ctx.which("python3")
    if found and not (
        (ctx.sys_prefix and found.startswith(ctx.sys_prefix.rstrip("/") + "/"))
        or (ctx.virtual_env and found.startswith(ctx.virtual_env.rstrip("/") + "/"))
        or "site-packages" in found
    ):
        add("path", found)
    return out


def check_python(ctx, results, memo):
    notes = []
    for source, path in python_candidates(ctx):
        label = "--python flag" if source == "flag" else source
        if not os.path.isabs(path):
            # A bare name would be resolved through PATH at probe time, which can land on the
            # CLT stub; resolve it first so the gate sees the real target.
            resolved = ctx.which(path)
            if not resolved or not os.path.isabs(resolved):
                notes.append("%s: %s not found on PATH" % (label, path))
                continue
            path = resolved
        if path == CLT_STUB or ctx.realpath(path) == CLT_STUB:
            if not _clt_ok(ctx, memo):
                notes.append("%s: CLT not installed (xcode-select -p failed); %s not probed" % (label, CLT_STUB))
                continue
        rc, out, _ = ctx.run_probe([path, "-E", "-s", "-c", PY_PROBE], 5)
        version = (out or "").strip()
        if rc == 0 and _vtuple(version) >= (3, 9):
            return CheckResult(True, path, version, "")
        notes.append("%s: %s unusable (rc %s)" % (label, path, rc))
    return CheckResult(False, None, None, "; ".join(notes) or "no python >= 3.9 found")


def _app_version(plist_path):
    try:
        with open(plist_path, "rb") as handle:
            info = plistlib.load(handle)
    except Exception:
        return None
    value = info.get("CFBundleShortVersionString")
    return value if isinstance(value, str) else None


def check_streamdeck_app(ctx, results, memo):
    app = os.path.join(ctx.apps_dir, APP_NAME)
    version = _app_version(os.path.join(app, "Contents", "Info.plist"))
    if version is None:
        rc, out, _ = ctx.run_probe(["mdfind", "kMDItemCFBundleIdentifier == '%s'" % APP_BUNDLE_ID], 5)
        hits = [line for line in (out or "").splitlines() if line.strip()] if rc == 0 else []
        if hits:
            app = hits[0].strip()
            version = _app_version(os.path.join(app, "Contents", "Info.plist"))
    if version is None:
        return CheckResult(False, None, None, "Elgato Stream Deck app not found")
    if _vtuple(version) < (6, 6):
        return CheckResult(False, app, version, "Stream Deck %s is older than 6.6" % version)
    return CheckResult(True, app, version, "")


def _node_verified(ctx, path):
    rc, _, _ = ctx.run_probe([path, "-e", WEBSOCKET_PROBE], 5)
    if rc != 0:
        return None
    rc, out, _ = ctx.run_probe([path, "--version"], 5)
    return (out or "").strip() or "unknown" if rc == 0 else None


def check_node(ctx, results, memo):
    flag = (ctx.flags.get("node") or "auto")
    candidates = []
    if flag not in ("bundled", "auto", "PATH"):
        candidates.append(flag)
    elif flag == "PATH":
        found = ctx.which("node")
        if found:
            candidates.append(found)
    elif flag == "auto":
        rec = ctx.recorded.get("node")
        if isinstance(rec, dict) and rec.get("mode") == "external":
            candidates.append(_bin_path(rec))
        candidates.append(ctx.legacy.get("node"))
        candidates.append(ctx.which("node"))
    if flag != "bundled":
        seen = set()
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            version = _node_verified(ctx, path)
            if version:
                return CheckResult(True, path, version, "external")
    app = results.get("streamdeck-app")
    if flag != "PATH" and app is not None and app.ok and _vtuple(app.version) >= (7, 1):
        return CheckResult(True, None, "bundled", "bundled")
    return CheckResult(False, None, None, "no node with WebSocket support and Stream Deck < 7.1")


def check_agentdesk(ctx, results, memo):
    path = ctx.flags.get("agentdesk") or os.path.join(ctx.home, ".config", "agentdesk", "agentdesk.zsh")
    try:
        with open(path, "r") as handle:
            text = handle.read(1 << 20)
    except OSError:
        return CheckResult(False, None, None, "%s not found" % path)
    match = _API_RE.search(text)
    if not match or int(match.group(1)) < 1:
        return CheckResult(False, path, None, "AGENTDESK_API_VERSION >= 1 not found")
    return CheckResult(True, path, match.group(1), "")


def check_zellij(ctx, results, memo):
    path = _login_which(ctx, "zellij")
    if not path:
        return CheckResult(False, None, None, "zellij not found")
    rc, out, err = ctx.run_probe([path, "action", "--help"], 5)
    text = (out or "") + (err or "")
    if "list-panes" not in text or "focus-pane-id" not in text:
        return CheckResult(False, path, None, "zellij lacks list-panes / focus-pane-id")
    rc, out, _ = ctx.run_probe([path, "--version"], 5)
    return CheckResult(True, path, (out or "").strip() or None, "")


def check_ghostty(ctx, results, memo):
    app = os.path.join(ctx.apps_dir, GHOSTTY_APP)
    sdef = os.path.join(app, "Contents", "Resources", "Ghostty.sdef")
    try:
        with open(sdef, "r", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return CheckResult(False, None, None, "Ghostty.app not found")
    if SDEF_PHRASE not in text:
        return CheckResult(False, app, None, "Ghostty scripting dictionary lacks surface configuration")
    return CheckResult(True, app, None, "")


def check_homebrew(ctx, results, memo):
    path = _login_which(ctx, "brew")
    return CheckResult(True, path, None, "") if path else CheckResult(False, None, None, "brew not found")


def _brew(kind, *argv, needs=("homebrew",)):
    return InstallPlan(kind, ("brew",) + argv, "brew " + " ".join(argv), needs)


REGISTRY = (
    Prereq("homebrew", "Homebrew package manager", "needed to propose the installs below", (),
           check_homebrew, InstallPlan("manual", None, "see https://brew.sh", ()), "support"),
    Prereq("python", "Python 3.9 or newer", "runs the hooks and the plugin backend",
           ("hooks", "plugin backend"), check_python,
           InstallPlan("brew-formula", ("brew", "install", "python"),
                       "brew install python (or xcode-select --install)", ("homebrew",)), "core"),
    Prereq("streamdeck-app", "Elgato Stream Deck app 6.6 or newer", "the deck itself",
           ("the deck",), check_streamdeck_app, _brew("brew-cask", "install", "--cask", "elgato-stream-deck"), "core"),
    Prereq("node", "Node.js with WebSocket support", "runs the plugin (or the app's bundled Node 7.1+)",
           ("plugin process",), check_node, _brew("brew-formula", "install", "node"), "core"),
    Prereq("agentdesk", "Quoin Agent Desk", "the Desk key and Claude / Codex launch keys",
           ("Desk key", "Claude launch", "Codex launch"), check_agentdesk,
           InstallPlan("quoin-agentdesk", None, "deploy Quoin Agent Desk (same files `quoin install` deploys)", ()),
           "feature"),
    Prereq("zellij", "zellij terminal multiplexer", "agent observation, focus and the Desk",
           ("agent observation", "focus", "Desk"), check_zellij, _brew("brew-formula", "install", "zellij"), "feature"),
    Prereq("ghostty", "Ghostty terminal", "tab navigation, surface focus and Desk launch",
           ("tab navigation", "surface focus", "Desk launch"), check_ghostty,
           _brew("brew-cask", "install", "--cask", "ghostty"), "feature"),
)
BY_NAME = {p.name: p for p in REGISTRY}


def run_checks(ctx):
    results, memo = {}, {}
    for prereq in REGISTRY:
        try:
            results[prereq.name] = prereq.check(ctx, results, memo)
        except Exception as exc:
            results[prereq.name] = CheckResult(False, None, None, type(exc).__name__)
    return results


# --------------------------------------------------------------------- consent

def _stamp(ctx):
    value = ctx.now() if ctx.now else None
    if value is None or isinstance(value, str):
        return value
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _recheck(ctx, results, name):
    memo = {}
    try:
        results[name] = BY_NAME[name].check(ctx, results, memo)
    except Exception as exc:
        results[name] = CheckResult(False, None, None, type(exc).__name__)


def resolve(ctx, results, *, mode="ask"):
    results = dict(results)
    lines = []

    def say(text):
        lines.append(text)
        ctx.out(text)

    interactive = bool(ctx.stdin_isatty()) and bool(ctx.stdout_isatty())
    initially_missing = [p for p in REGISTRY if p.name != "homebrew" and not results[p.name].ok]
    for prereq in initially_missing:
        say("%s is missing: %s. Why it matters: %s." % (prereq.name, prereq.what, prereq.why))
        say("  how: %s" % (prereq.install.display if prereq.install else "install manually"))
        if not interactive:
            say("  not installed: non-interactive run")
    states = {}
    declined_rest = False
    if interactive:
        for prereq in REGISTRY:
            name = prereq.name
            if name == "homebrew":
                continue
            if results[name].ok:
                states[name] = "installed" if states.get(name) == "installed" else "ok"
                continue
            plan = prereq.install
            if plan is None or plan.kind == "manual":
                states[name] = "missing"
                continue
            if "homebrew" in plan.needs and not results["homebrew"].ok:
                say("%s: install Homebrew first (https://brew.sh)" % name)
                states[name] = "missing"
                continue
            if mode == "none" or declined_rest:
                states[name] = "declined"
                continue
            accepted = mode == "all"
            if mode == "ask":
                try:
                    answer = ctx.prompt("Install %s? [y/N] " % name)
                except (EOFError, KeyboardInterrupt):
                    declined_rest = True
                    states[name] = "declined"
                    continue
                accepted = str(answer).strip().lower() in ("y", "yes")
            if not accepted:
                states[name] = "declined"
                continue
            try:
                rc = ctx.deploy_agentdesk() if plan.kind == "quoin-agentdesk" else ctx.runner(list(plan.argv))
            except Exception as exc:
                say("%s: install failed (%s)" % (name, type(exc).__name__))
                states[name] = "failed"
                continue
            _recheck(ctx, results, name)
            if name == "streamdeck-app":
                _recheck(ctx, results, "node")
            if (rc or 0) != 0 or not results[name].ok:
                say("%s: install did not succeed" % name)
                states[name] = "failed"
            else:
                states[name] = "installed"
    record = {}
    for prereq in REGISTRY:
        name = prereq.name
        res = results[name]
        if res.ok:
            state = states.get(name, "ok")
        else:
            state = states.get(name, "missing")
            if state in ("ok", "installed"):
                state = "missing"
        record[name] = {"state": state, "path": res.path, "version": res.version, "checked_at": _stamp(ctx)}
    features = sorted({f for p in REGISTRY if record[p.name]["state"] not in ("ok", "installed") for f in p.features})
    core_missing = [p.name for p in REGISTRY if p.severity == "core" and record[p.name]["state"] not in ("ok", "installed")]
    return Outcome(record, features, core_missing, lines)
