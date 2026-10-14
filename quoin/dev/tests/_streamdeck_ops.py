"""Stage-4 fixtures and guards for the Stream Deck operation engine.

Everything here writes only below the test's tmp dir. Three guards apply to every
operation test module (declare them in `pytestmark`):

* `forbid_dangerous_subprocess` - no kill/pgrep/brew/launchctl, no real
  osascript/zellij/open, no profile import from a real path; records every argv
  in `subprocess_calls`.
* `forbid_real_home_io` - any open/mutation of a path under a real-HOME root the
  component or the user's config lives in raises before the call, realpath-aware.
* `forbid_real_seams` - the real prerequisite and system seams raise, so a test
  that forgets to inject a fake fails loudly instead of probing the machine.
"""
import builtins
import datetime
import hashlib
import importlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys

import pytest

import _streamdeck_helpers as h
import _streamdeck_lifecycle as lc
from _streamdeck_helpers import GuardViolation, forbid_dangerous_subprocess, subprocess_calls  # noqa: F401
from _streamdeck_lifecycle import (FakeCtx, NOW, deck_home, fixture_python, lifecycle_home,  # noqa: F401
                                   make_installed, make_prototype, make_settings, tree_snapshot, write_json)
from quoin.streamdeck import hooks_merge, macos, manifest, paths as paths_mod, prereqs
import quoin.streamdeck as sd

LATER = NOW + datetime.timedelta(hours=1)
DARWIN = macos.PlatformInfo("darwin", "15.0", "arm64", True)
SAMPLE_EVENT = json.dumps({"session_id": "sid-ops", "hook_event_name": "Stop", "cwd": "/work"}).encode()

IO_VIOLATIONS = []
_ONE_PATH = ("builtins.open", "io.open", "os.open", "os.mkdir", "os.makedirs", "os.rmdir", "os.remove",
             "os.unlink", "os.chmod", "os.mkfifo", "shutil.rmtree")
_TWO_PATH = ("os.replace", "os.rename", "os.symlink", "os.link", "shutil.copy", "shutil.copy2",
             "shutil.copyfile", "shutil.copytree", "shutil.move")
_DIR_FD_KEYS = ("dir_fd", "src_dir_fd", "dst_dir_fd")


def _lookup(dotted):
    module_name, _, attr = dotted.rpartition(".")
    module = {"builtins": builtins, "io": io, "os": os, "shutil": shutil}[module_name]
    return module, attr


def _forbidden(value):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None
    try:
        text = os.fsdecode(value)
    except (TypeError, ValueError):
        return None
    if text == "":
        return None
    for candidate in (os.path.abspath(text), os.path.realpath(text)):
        for root in h.FORBIDDEN_ROOTS:
            if h._under(candidate, root):
                return candidate
    return None


@pytest.fixture
def forbid_real_home_io(monkeypatch):
    IO_VIOLATIONS.clear()

    def wrap(dotted, count):
        module, attr = _lookup(dotted)
        real = getattr(module, attr)

        def guarded(*args, **kwargs):
            relative_fd = any(kwargs.get(key) is not None for key in _DIR_FD_KEYS)
            if not relative_fd:
                for value in args[:count]:
                    hit = _forbidden(value)
                    if hit:
                        IO_VIOLATIONS.append("%s -> %s" % (dotted, hit))
                        raise GuardViolation("%s touches a protected real-HOME path: %s" % (dotted, hit))
            return real(*args, **kwargs)

        guarded.__wrapped__ = real
        monkeypatch.setattr(module, attr, guarded)

    for dotted in _ONE_PATH:
        wrap(dotted, 1)
    for dotted in _TWO_PATH:
        wrap(dotted, 2)
    yield IO_VIOLATIONS
    assert not IO_VIOLATIONS, "a real-HOME access was attempted: %r" % (IO_VIOLATIONS,)


def _real_seam_targets():
    targets = [(prereqs, "default_ctx"), (prereqs, "_real_run_probe"), (prereqs, "_real_runner")]
    if importlib.util.find_spec("quoin.streamdeck.engine") is not None:
        engine = importlib.import_module("quoin.streamdeck.engine")
        targets += [(engine, "default_sysops"), (engine, "_real_probe"), (engine, "_real_run")]
    return targets


@pytest.fixture
def forbid_real_seams(monkeypatch):
    def blocked(name):
        def tripwire(*args, **kwargs):
            raise GuardViolation("the real seam %s was reached; inject a fake" % name)
        return tripwire

    for module, name in _real_seam_targets():
        monkeypatch.setattr(module, name, blocked(name))
    yield


def assert_seam_tripwire():
    """Prove `forbid_real_seams` is active: the real seams raise instead of working."""
    paths = paths_mod.resolve({}, {"HOME": "/nonexistent-home-for-tripwire"})
    with pytest.raises(GuardViolation):
        prereqs.default_ctx(paths, {})
    if importlib.util.find_spec("quoin.streamdeck.engine") is not None:
        engine = importlib.import_module("quoin.streamdeck.engine")
        with pytest.raises(GuardViolation):
            engine.default_sysops()


# ------------------------------------------------------------------ fake system

class FakeSys:
    """Recording stand-in for the engine's system operations (pgrep probe, `open`)."""

    def __init__(self, running=False, run_rc=0):
        self.probe_calls = []
        self.run_calls = []
        self.probes = {}
        self.running = running
        self.run_rc = run_rc

    def probe(self, argv, timeout=5):
        key = tuple(argv)
        self.probe_calls.append(key)
        if key in self.probes:
            value = self.probes[key]
            return value(key) if callable(value) else value
        if key[:1] and os.path.basename(key[0]) == "pgrep":
            return (0, "4242\n", "") if self.running else (1, "", "")
        return 127, "", "unscripted"

    def run(self, argv):
        self.run_calls.append(tuple(argv))
        return self.run_rc(tuple(argv)) if callable(self.run_rc) else self.run_rc


# ------------------------------------------------------------------ engine env

CORE_NAMES = ("python", "node", "streamdeck-app", "agentdesk", "zellij", "ghostty", "homebrew")


def _write_exec(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return str(path)


def _present_world(lh, missing, app_version):
    """Which/probe tables with every prerequisite present except those in `missing`."""
    which, probes = {}, {}
    python = fixture_python(lh)
    if "python" not in missing:
        which["python3"] = python
        probes[(python, "-E", "-s", "-c", prereqs.PY_PROBE)] = (0, "3.14.0\n", "")
    if "node" not in missing:
        node = _write_exec(lh.root / "toolbin" / "node")
        which["node"] = node
        probes[(node, "-e", prereqs.WEBSOCKET_PROBE)] = (0, "", "")
        probes[(node, "--version")] = (0, "v24.1.0\n", "")
    if "streamdeck-app" not in missing:
        info = lh.apps / "Elgato Stream Deck.app" / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True, exist_ok=True)
        import plistlib
        info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": app_version}))
    if "agentdesk" not in missing:
        agentdesk = lh.home / ".config" / "agentdesk" / "agentdesk.zsh"
        agentdesk.parent.mkdir(parents=True, exist_ok=True)
        if not agentdesk.exists():
            agentdesk.write_text("AGENTDESK_API_VERSION=1\n")
    if "zellij" not in missing:
        zellij = _write_exec(lh.fakebin / "zellij")
        which["zellij"] = zellij
        probes[(zellij, "action", "--help")] = (0, "list-panes focus-pane-id", "")
        probes[(zellij, "--version")] = (0, "zellij 0.43.1\n", "")
    if "ghostty" not in missing:
        sdef = lh.apps / "Ghostty.app" / "Contents" / "Resources" / "Ghostty.sdef"
        sdef.parent.mkdir(parents=True, exist_ok=True)
        sdef.write_text("<command name='new surface configuration'/>")
    if "homebrew" not in missing:
        which["brew"] = _write_exec(lh.root / "toolbin" / "brew")
    for name in ("osascript", "open"):
        which[name] = str(lh.fakebin / name)
    if "codex" not in missing:
        which["codex"] = _write_exec(lh.root / "toolbin" / "codex")
    return which, probes


def make_env(lh, *, missing=(), tty=False, answers=(), prereqs="ask", runtimes=None, flags=None, faults=None,
             on_step=None, dry_run=False, purge=False, allow_downgrade=False, open_profile=False, now=NOW,
             environ=None, app_version="7.6", payload_dir=None, platform=DARWIN, running=False):
    """An `engine.Env` whose every outside effect is a recording fake."""
    engine = importlib.import_module("quoin.streamdeck.engine")
    if environ is None:
        zsock = lh.root / "zsock"
        zsock.mkdir(exist_ok=True)
        zsock.chmod(0o700)
        environ = {"ZELLIJ_SOCKET_DIR": str(zsock)}
    missing = set(missing)
    which, probes = _present_world(lh, missing, app_version)
    fake = FakeCtx(lh.root, which=which, probes=probes, stdin_tty=tty, stdout_tty=tty, answers=answers,
                   flags=dict(flags or {}), now=now, environ=dict(environ or {}))
    fake.ctx.apps_dir = str(lh.apps)
    fake.ctx.home = str(lh.home)
    sysops = FakeSys(running=running)
    lines = []

    def ctx_factory(paths, ctx_flags, recorded=None, legacy=None):
        fake.ctx.flags = dict(ctx_flags or {})
        fake.ctx.recorded = dict(recorded or {})
        fake.ctx.legacy = dict(legacy or {})
        return fake.ctx

    env = engine.Env(
        paths=lh.resolve(), flags=dict(flags or {}), runtimes=runtimes, prereq_mode=prereqs, dry_run=dry_run,
        open_profile=open_profile, purge=purge, allow_downgrade=allow_downgrade, ctx_factory=ctx_factory,
        sysops=sysops, environ=dict(environ or {}), now=now, platform_info=platform, package_version="9.9.9",
        component_version=sd.COMPONENT_VERSION, payload_dir=payload_dir or sd.PAYLOAD_DIR, out=lines.append,
        faults=dict(faults or {}), on_step=on_step)
    env.fake_ctx = fake
    env.fake_sys = sysops
    env.lines = lines
    return env


# ------------------------------------------------------------------ hook execution

def _hook_commands(data):
    hooks = data.get("hooks") if isinstance(data, dict) else None
    found = []
    if isinstance(hooks, dict):
        for lst in hooks.values():
            if not isinstance(lst, list):
                continue
            for group in lst:
                if not (isinstance(group, dict) and isinstance(group.get("hooks"), list)):
                    continue
                for hook in group["hooks"]:
                    command = hook.get("command") if isinstance(hook, dict) else None
                    if isinstance(command, str) and (hooks_merge.classify(command, runtime_names=("QuoinStreamDeck",))
                                                      or "monitor.py" in command or "quoin-deck" in command):
                        found.append(command)
    return found


def component_commands(lh):
    """Every component-looking hook command currently in the tmp Claude and Codex files."""
    commands = []
    for path in (lh.claude_settings, lh.codex_home / "hooks.json"):
        try:
            with open(str(path)) as handle:
                commands += _hook_commands(json.load(handle))
        except (OSError, ValueError):
            pass
    return commands


def run_hook_commands(lh, *, extra=()):
    """Run each distinct hook command once the way an agent would; returns [(cmd, rc, out, err)]."""
    commands = component_commands(lh)
    python = fixture_python(lh)
    # Agents that started before an operation keep the command they read at start, so the managed and
    # legacy forms are run too, whenever the file they name exists (a tombstone, shim or forwarder).
    if os.path.lexists(str(lh.runtime / "bin" / "quoin-deck-hook")):
        commands.append(hooks_merge.managed_command(str(lh.runtime), "claude"))
    if os.path.lexists(str(lh.runtime / "monitor.py")):
        commands.append("%s %s claude" % (python, shlex.quote(str(lh.runtime / "monitor.py"))))
    commands.extend(extra)
    ordered = list(dict.fromkeys(commands))
    for command in ordered:
        try:
            argv0 = shlex.split(command)[0]
        except (ValueError, IndexError):
            argv0 = ""
        if argv0 == "/usr/bin/python3":
            raise AssertionError("refusing to execute a hook command that runs /usr/bin/python3: %s" % command)
    env = {"HOME": str(lh.home), "PATH": "%s:/usr/bin:/bin" % lh.fakebin}
    results = []
    for command in ordered:
        proc = subprocess.run(["/bin/sh", "-c", command], input=SAMPLE_EVENT, capture_output=True,
                              cwd=str(lh.root), env=env, timeout=10)
        results.append((command, proc.returncode, proc.stdout, proc.stderr))
    return results


def assert_hooks_inert(results):
    bad = [(cmd, rc, out) for cmd, rc, out, _ in results if rc != 0 or out != b""]
    assert not bad, "hook commands were not inert: %r" % (bad,)


# ------------------------------------------------------------------ snapshots

def _file_digest(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _normalised_manifest_digest(path):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _file_digest(path)
    data.pop("last_verified_at", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def snapshot_owned(lh):
    """Tree snapshot of every owned location, minus the verification clock and the lock file."""
    out = {}
    for label, root in (("runtime", lh.runtime), ("plugin", lh.plugin_dir), ("state", lh.state_root)):
        if os.path.isdir(str(root)):
            for rel, value in tree_snapshot(root).items():
                if label == "runtime" and rel == ".install.lock":
                    continue
                if "__pycache__" in rel.split(os.sep):
                    continue  # running a hook with the interpreter writes bytecode next to the payload
                out["%s/%s" % (label, rel)] = value
    manifest_path = str(lh.runtime / "install-manifest.json")
    if os.path.exists(manifest_path):
        out["runtime/install-manifest.json"] = ("manifest", _normalised_manifest_digest(manifest_path))
    for label, path in (("claude", lh.claude_settings), ("codex", lh.codex_home / "hooks.json")):
        out["settings/" + label] = ("file", _file_digest(str(path)))
    return out
