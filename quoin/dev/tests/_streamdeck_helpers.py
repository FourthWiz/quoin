"""Shared fixtures and guards for the Stream Deck component tests.

Payload scripts are plain scripts (run with their own directory on sys.path),
so tests load them by path and purge them from sys.modules around every test.
Every test module in this family applies `deck_home` and
`forbid_dangerous_subprocess` so a test can never reach the real HOME config,
the live Agent Desk socket, a real zellij or a real osascript.
"""
import importlib
import json
import os
import pathlib
import pwd
import shutil
import stat
import subprocess
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PAYLOAD_DIR = REPO_ROOT / "src" / "quoin" / "streamdeck" / "payload"
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "streamdeck_prototype"
PAYLOAD_MODULES = ("monitor", "dashboard", "controller", "deckenv")

# Captured at import, before any fixture changes HOME or PATH.
REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
NODE = shutil.which("node")

_UID = os.getuid()
FORBIDDEN_ROOTS = tuple(
    os.path.join(REAL_HOME, sub)
    for sub in (
        ".claude",
        ".codex",
        ".config",
        "Library/Application Support/QuoinStreamDeck",
        "Library/Application Support/com.elgato.StreamDeck",
    )
) + ("/tmp/zellij-agentdesk-%d" % _UID, "/private/tmp/zellij-agentdesk-%d" % _UID)

FAKE_BINS = {}  # name -> absolute path, filled by deck_home
DANGEROUS = {"kill", "pkill", "killall", "brew", "launchctl"}
FAKE_ONLY = {"osascript", "open", "zellij"}
VIOLATIONS = []


class GuardViolation(AssertionError):
    pass


def _under(path, root):
    path = os.path.abspath(path)
    return path == root or path.startswith(root + os.sep)


def _is_forbidden(value):
    try:
        text = os.fsdecode(value)
    except (TypeError, ValueError):
        return False
    if not text.startswith("/"):
        return False
    return any(_under(text, root) for root in FORBIDDEN_ROOTS)


def check_call(argv, env=None):
    """Raise GuardViolation when a subprocess call could touch the real machine."""
    if isinstance(argv, (str, bytes, os.PathLike)):
        argv = [argv]
    argv = list(argv)
    if not argv:
        return
    first = os.fsdecode(argv[0])
    base = os.path.basename(first)
    if base in DANGEROUS:
        raise GuardViolation("dangerous command: %s" % base)
    if base in FAKE_ONLY and first not in FAKE_BINS.values():
        raise GuardViolation("%s is not a recorded fake: %s" % (base, first))
    for item in argv:
        if _is_forbidden(item):
            raise GuardViolation("argv touches a forbidden root: %s" % os.fsdecode(item))
    effective = os.environ if env is None else env
    if effective.get("HOME") == REAL_HOME:
        raise GuardViolation("child env carries the real HOME")
    sock = effective.get("ZELLIJ_SOCKET_DIR")
    if sock and _is_forbidden(sock):
        raise GuardViolation("child env points at a forbidden socket dir")


def check_env_paths(env, root):
    """Every path field of a deckenv object must live under `root`."""
    root = os.path.abspath(str(root))
    names = [
        "runtime", "state_root", "config_path", "state_dir", "agents_dir",
        "legacy_agents_dir", "surfaces_path", "health_path", "errors_log",
        "monitor_lock", "legacy_monitor_lock", "controller_lock", "manifest",
        "socket_dir", "terminal_entry",
    ]
    values = []
    for name in names:
        value = getattr(env, name, None)
        if isinstance(value, (str, os.PathLike)):
            values.append((name, os.fspath(value)))
    for key, value in (getattr(env, "bins", None) or {}).items():
        values.append(("bins[%s]" % key, value))
    bad = [(n, v) for n, v in values if not _under(v, root)]
    if bad:
        raise GuardViolation("paths outside the test tmp dir: %r" % (bad,))


def purge_payload_modules():
    for name in PAYLOAD_MODULES:
        sys.modules.pop(name, None)


def _write_fake(path, name):
    log = path.with_suffix(".log")
    out = path.with_suffix(".out")
    rc = path.with_suffix(".rc")
    path.write_text(
        "#!/bin/sh\n"
        'printf "%%s\\n" "$*" >> "%s"\n'
        'if [ -f "%s" ]; then cat "%s"; r=0; [ -f "%s" ] && r=$(cat "%s"); exit "$r"; fi\n'
        '%s\n' % (
            log, out, out, rc, rc,
            'echo "No active zellij sessions found" >&2; exit 1' if name == "zellij" else "exit 0",
        )
    )
    path.chmod(0o755)


@pytest.fixture
def deck_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    FAKE_BINS.clear()
    for name in ("zellij", "osascript", "open"):
        path = fakebin / name
        _write_fake(path, name)
        FAKE_BINS[name] = str(path)
    for key in list(os.environ):
        if key.startswith("QUOIN_DECK_") or key.startswith("ZELLIJ") or key in (
            "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "CODEX_HOME"
        ):
            monkeypatch.delenv(key)
    zsock = tmp_path / "zsock"
    zsock.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pinned = "%s:/usr/bin:/bin" % fakebin
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", str(zsock))
    monkeypatch.setenv("PATH", pinned)
    monkeypatch.setenv("QUOIN_DECK_RUNTIME", str(runtime))
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which",
        lambda cmd, mode=os.F_OK | os.X_OK, path=None: real_which(cmd, mode, path or pinned),
    )
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path(str(home))))
    python = fakebin / "python"
    python.symlink_to(sys.executable)
    (runtime / "install-manifest.json").write_text(json.dumps({
        "binaries": dict(FAKE_BINS, python=str(python)),
        "zellij_socket_dir": str(zsock),
    }))
    state = types.SimpleNamespace(
        root=tmp_path, home=home, fakebin=fakebin, runtime=runtime, zsock=zsock, recorded=[]
    )
    yield state
    if "deckenv" in sys.modules and hasattr(sys.modules["deckenv"], "reset"):
        sys.modules["deckenv"].reset()
    for env in state.recorded:
        check_env_paths(env, tmp_path)
    purge_payload_modules()


@pytest.fixture
def forbid_dangerous_subprocess(monkeypatch):
    VIOLATIONS.clear()
    real_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **kw):
        try:
            check_call(args, kw.get("env"))
        except GuardViolation as exc:
            VIOLATIONS.append(str(exc))
            raise
        return real_init(self, args, *a, **kw)

    def guard_os(real):
        def wrapper(*a, **kw):
            argv = a[0] if a else kw.get("command", "")
            if real is os.posix_spawn:
                argv = a[1] if len(a) > 1 else kw.get("argv", [])
            try:
                check_call(argv, kw.get("env"))
            except GuardViolation as exc:
                VIOLATIONS.append(str(exc))
                raise
            return real(*a, **kw)
        return wrapper

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    monkeypatch.setattr(os, "system", guard_os(os.system))
    monkeypatch.setattr(os, "posix_spawn", guard_os(os.posix_spawn))
    yield VIOLATIONS
    assert not VIOLATIONS, "guard fired: %r" % (VIOLATIONS,)


@pytest.fixture
def payload(deck_home, monkeypatch):
    """Return load(*names) importing payload scripts from their own directory."""
    def load(*names):
        purge_payload_modules()
        monkeypatch.syspath_prepend(str(PAYLOAD_DIR))
        importlib.invalidate_caches()
        mods = [importlib.import_module(name) for name in names]
        deckenv = sys.modules.get("deckenv")
        if deckenv is not None and not getattr(deckenv, "_guard_wrapped", False):
            real_load = deckenv.load

            def recording_load(*a, **kw):
                env = real_load(*a, **kw)
                # Loads with an explicit environ dict are self-contained table tests.
                if not a and "environ" not in kw:
                    deck_home.recorded.append(env)
                return env

            monkeypatch.setattr(deckenv, "load", recording_load)
            monkeypatch.setattr(deckenv, "_guard_wrapped", True, raising=False)
        return mods[0] if len(mods) == 1 else mods
    yield load
    purge_payload_modules()


def load_frozen(monkeypatch, deck_home, answers=None):
    """Load the frozen prototype scripts with every external call faked.

    `answers` maps a zellij argv tail (joined) to its stdout; any other call
    raises so the frozen code's own broad excepts cannot hide a real call.
    """
    legacy = deck_home.root / "legacy"
    legacy.mkdir(exist_ok=True)
    purge_payload_modules()
    monkeypatch.setenv("QUOIN_DECK_STATE", str(legacy))
    monkeypatch.syspath_prepend(str(FIXTURE_DIR))
    importlib.invalidate_caches()
    controller = importlib.import_module("controller")
    dashboard = importlib.import_module("dashboard")
    env = dict(controller.ENV)
    env["ZELLIJ_SOCKET_DIR"] = str(deck_home.zsock)
    env["PATH"] = "%s:/usr/bin:/bin" % deck_home.fakebin
    monkeypatch.setattr(controller, "ENV", env)
    calls = []
    table = dict(answers or {})

    def fake_run(args, check=True, timeout=20):
        calls.append(list(args))
        key = " ".join(args[1:])
        if key in table:
            return table[key]
        raise RuntimeError("unexpected call: %r" % (args,))

    monkeypatch.setattr(controller, "run", fake_run)
    return types.SimpleNamespace(controller=controller, dashboard=dashboard, calls=calls, legacy=legacy)


RUNTIME_FILES = ("monitor.py", "dashboard.py", "controller.py", "deckenv.py", "terminal.zsh")


def stage_runtime(tmp):
    """A deployed-looking runtime: versions/1.0.0 plus a `current` symlink and a manifest."""
    base = tmp / "App Support" / "QuoinStreamDeck"
    version = base / "versions" / "1.0.0"
    version.mkdir(parents=True)
    for name in RUNTIME_FILES:
        shutil.copy2(PAYLOAD_DIR / name, version / name)
    (base / "current").symlink_to("versions/1.0.0")
    fakes = tmp / "stage-fakes"
    fakes.mkdir()
    bins = {}
    for name in ("zellij", "osascript", "open"):
        _write_fake(fakes / name, name)
        bins[name] = str(fakes / name)
    state_root = tmp / "stage-state"
    zsock = tmp / "stage-zsock"
    zsock.mkdir()
    bins["python"] = sys.executable
    (base / "install-manifest.json").write_text(json.dumps({
        "paths": {"config": str(state_root / "config.json"), "state": str(state_root / "state")},
        "binaries": bins, "zellij_socket_dir": str(zsock),
    }))
    home = tmp / "stage-home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "QUOIN_DECK_RUNTIME": str(base),
           "QUOIN_DECK_STATE_ROOT": str(state_root), "ZELLIJ_SOCKET_DIR": str(zsock)}
    cwd = tmp / "stage-cwd"
    cwd.mkdir()

    def command(script, *args, python=None):
        return [python or sys.executable, "-E", "-s", str(base / "current" / script)] + list(args)

    def run(cmd, stdin=b"", extra_env=None, timeout=10):
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(cwd), env=dict(env, **(extra_env or {})), start_new_session=True)
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.communicate()
            raise
        return proc.returncode, out, err

    return types.SimpleNamespace(runtime=base, state_root=state_root, state_dir=state_root / "state",
                                 zsock=zsock, env=env, command=command, run=run, fakes=fakes, cwd=cwd, home=home)
