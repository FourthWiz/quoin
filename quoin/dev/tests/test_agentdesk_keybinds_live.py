"""Live checks of the merged key map in a real Zellij session.

Each test starts its own Zellij session on a pty with an isolated HOME and an
isolated ZELLIJ_SOCKET_DIR, feeds it key bytes and observes the result through
`zellij ls` and `zellij action query-tab-names`. Nothing touches the real home
or the default socket directory, and cleanup only ever names the test's own
session. Skipped unless Zellij 0.44.x is installed.
"""
import contextlib
import fcntl
import os
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import uuid
from pathlib import Path

import pytest

from _agentdesk_kdl_helpers import fixture_text, make_bin, make_env, merge

ZELLIJ = shutil.which("zellij")


def _zellij_version():
    if not ZELLIJ:
        return ""
    try:
        done = subprocess.run([ZELLIJ, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


needs_zellij = pytest.mark.skipif(
    not _zellij_version().startswith("zellij 0.44."),
    reason="live key tests need Zellij 0.44.x on PATH",
)

STOCK = "zellij-0.44.3-stock.kdl"
PRE_V1 = "user-pre-v1.kdl"
MINIMAL = "minimal-no-keybinds.kdl"

# Key encodings: Ctrl keys as kitty CSI-u, Alt keys as ESC plus the character.
CTRL_T = b"\x1b[116;5u"
CTRL_Q = b"\x1b[113;5u"
CTRL_Q_LEGACY = b"\x11"
CTRL_G = b"\x1b[103;5u"


def alt(char):
    return b"\x1b" + char.encode("utf-8")


def zellij_env(tmp_path, sock_dir):
    """The one environment used for every Zellij call in a live test."""
    base = make_env(tmp_path, make_bin(tmp_path))
    env = {
        "PATH": base["PATH"],
        "HOME": base["HOME"],
        "XDG_CONFIG_HOME": base["XDG_CONFIG_HOME"],
        "ZELLIJ_SOCKET_DIR": str(sock_dir),
        "TERM": "xterm-256color",
        "SHELL": "/bin/sh",
    }
    return env


def run_zellij(env, expected_sock, *args, timeout=30):
    """Run zellij with ``env`` after checking it cannot reach a real socket dir."""
    sock = env.get("ZELLIJ_SOCKET_DIR", "")
    if not sock or os.path.realpath(sock) != os.path.realpath(expected_sock):
        raise AssertionError(f"refusing to run zellij: socket dir {sock!r} is not the test's own")
    for name in env:
        if name.startswith("ZELLIJ") and name != "ZELLIJ_SOCKET_DIR":
            raise AssertionError(f"refusing to run zellij: {name} is set")
    return subprocess.run(
        ["zellij", *args],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


def spawn_on_pty(argv, env, rows, cols):
    """Start ``argv`` on a fresh pty of ``rows`` x ``cols``; return ``(proc, master_fd)``.

    Uses ``subprocess`` rather than forking the test process itself: forking a
    process that already runs threads (pytest-timeout's watchdog) is unsafe and
    Python 3.12+ warns about it. The child gets its own session so the pty becomes its terminal.
    """
    exe = shutil.which(argv[0], path=env["PATH"])
    if not exe:
        raise AssertionError(f"{argv[0]} not found on the test PATH")
    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        proc = subprocess.Popen(
            [exe, *argv[1:]],
            stdin=slave, stdout=slave, stderr=slave,
            env=env, start_new_session=True, close_fds=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return proc, master


def server_pids(sock):
    """Pids of Zellij servers whose socket lives under the test's own ``sock`` dir.

    The server runs in its own session and process group, so it is found by the
    socket path on its command line. ``sock`` is a unique per-test /tmp dir, so
    this can never match a real Agent Desk server.
    """
    needles = {f"--server {sock}/", f"--server {os.path.realpath(sock)}/"}
    done = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10)
    pids = []
    for line in done.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if any(n in command for n in needles) and pid.isdigit():
            pids.append(int(pid))
    return pids


def _drain_fd(fd, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            ready, _, _ = select.select([fd], [], [], min(0.2, max(0.0, end - time.time())))
        except (OSError, ValueError):
            time.sleep(0.2)
            continue
        if ready:
            try:
                if not os.read(fd, 65536):
                    time.sleep(0.2)
            except OSError:
                time.sleep(0.2)


def _wait_servers_gone(sock, fd, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if not server_pids(sock):
            return []
        # Keep reading the pty: a client with unread output can block in exit.
        _drain_fd(fd, 0.2)
    return server_pids(sock)


def stop_session(env, sock, name, proc, fd):
    """Stop the test's session, its server and its client; return surviving server pids."""
    with contextlib.suppress(Exception):
        run_zellij(env, sock, "kill-session", name, timeout=10)
    survivors = _wait_servers_gone(sock, fd, 5)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not survivors:
            break
        for pid in survivors:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        survivors = _wait_servers_gone(sock, fd, 2)
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    with contextlib.suppress(Exception):
        run_zellij(env, sock, "delete-session", name, "--force", timeout=10)
    if fd >= 0:
        with contextlib.suppress(OSError):
            os.close(fd)
    return survivors


class LiveSession:
    def __init__(self, tmp_path, config):
        self.tmp_path = Path(tmp_path)
        self.config = str(config)
        self.sock = tempfile.mkdtemp(dir="/tmp", prefix="adz")
        self.name = "adk" + uuid.uuid4().hex[:8]
        self.env = zellij_env(tmp_path, self.sock)
        self.proc = None
        self.fd = -1

    def z(self, *args, timeout=30):
        return run_zellij(self.env, self.sock, *args, timeout=timeout)

    def start(self):
        assert str(Path(self.env["HOME"]).resolve()).startswith(str(self.tmp_path.resolve()))
        self.proc, self.fd = spawn_on_pty(
            ["zellij", "--config", self.config, "--session", self.name, "options",
             "--show-release-notes", "false", "--show-startup-tips", "false"],
            self.env, 40, 140,
        )
        self.wait_for(self.alive, 15)
        assert self.alive(), "session did not start"
        self.drain(1.5)

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    if not os.read(self.fd, 65536):
                        return
                except OSError:
                    return

    def send(self, data, pause=0.8):
        os.write(self.fd, data)
        self.drain(pause)

    def wait_for(self, predicate, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            self.drain(0.3)
        return predicate()

    def alive(self):
        done = self.z("ls", "-n")
        for line in done.stdout.splitlines():
            if line.split(" ")[0] == self.name and "EXITED" not in line:
                return True
        return False

    def tab_count(self):
        done = self.z("-s", self.name, "action", "query-tab-names")
        return len([ln for ln in done.stdout.splitlines() if ln.strip()])

    def close(self):
        stop_session(self.env, self.sock, self.name, self.proc, self.fd)
        self.proc, self.fd = None, -1
        shutil.rmtree(self.sock, ignore_errors=True)

@contextlib.contextmanager
def live(tmp_path, fixture, tier):
    """A running session on the fixture merged at ``tier`` (None = unmerged)."""
    if tier is None:
        text = fixture_text(fixture)
        cfg = Path(tmp_path) / "unmerged.kdl"
        cfg.write_text(text, encoding="utf-8")
    else:
        env = make_env(tmp_path, make_bin(tmp_path))
        merge(tmp_path, fixture_text(fixture), tier, env=env)
        cfg = Path(tmp_path) / "home" / ".config" / "zellij" / "config.kdl"
    session = LiveSession(tmp_path, cfg)
    try:
        session.start()
        yield session
    finally:
        session.close()


# ── harness unit tests (no Zellij needed) ─────────────────────────────────────

def test_zellij_env_is_isolated_even_when_the_process_env_is_not(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "real")
    sock = tempfile.mkdtemp(dir="/tmp", prefix="adz")
    try:
        env = zellij_env(tmp_path, sock)
        assert not [k for k in env if k.startswith("ZELLIJ") and k != "ZELLIJ_SOCKET_DIR"]
        assert env["ZELLIJ_SOCKET_DIR"] == sock
        assert env["HOME"].startswith(str(tmp_path))
        assert env["XDG_CONFIG_HOME"].startswith(str(tmp_path))
    finally:
        shutil.rmtree(sock, ignore_errors=True)


def test_run_zellij_refuses_a_foreign_socket_dir(tmp_path: Path) -> None:
    sock = tempfile.mkdtemp(dir="/tmp", prefix="adz")
    try:
        env = zellij_env(tmp_path, sock)
        env["ZELLIJ_SOCKET_DIR"] = "/tmp/zellij-agentdesk-501"
        with pytest.raises(AssertionError, match="not the test's own"):
            run_zellij(env, sock, "ls")
        env = zellij_env(tmp_path, sock)
        env.pop("ZELLIJ_SOCKET_DIR")
        with pytest.raises(AssertionError):
            run_zellij(env, sock, "ls")
        env = zellij_env(tmp_path, sock)
        env["ZELLIJ_SESSION_NAME"] = "real"
        with pytest.raises(AssertionError, match="ZELLIJ_SESSION_NAME"):
            run_zellij(env, sock, "ls")
    finally:
        shutil.rmtree(sock, ignore_errors=True)


def test_run_zellij_accepts_the_symlinked_tmp_path(tmp_path: Path, monkeypatch) -> None:
    sock = tempfile.mkdtemp(dir="/tmp", prefix="adz")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or None)
    try:
        env = zellij_env(tmp_path, sock)
        # macOS resolves /tmp to /private/tmp; both spellings name the same directory.
        alias = os.path.realpath(sock)
        env["ZELLIJ_SOCKET_DIR"] = alias
        run_zellij(env, sock, "ls")
        env["ZELLIJ_SOCKET_DIR"] = sock
        run_zellij(env, alias, "ls")
        assert len(calls) == 2
    finally:
        shutil.rmtree(sock, ignore_errors=True)


# ── live scenarios, tier 2 ────────────────────────────────────────────────────

SHAPES = [STOCK, PRE_V1, MINIMAL]


@needs_zellij
@pytest.mark.parametrize("fixture", SHAPES)
def test_ctrl_q_does_not_quit(tmp_path: Path, fixture: str) -> None:
    with live(tmp_path, fixture, 2) as s:
        s.send(CTRL_Q, 1.5)
        s.send(CTRL_Q_LEGACY, 1.5)
        assert s.alive()


@needs_zellij
@pytest.mark.parametrize("fixture", SHAPES)
def test_ctrl_t_no_longer_opens_tab_mode(tmp_path: Path, fixture: str) -> None:
    with live(tmp_path, fixture, 2) as s:
        before = s.tab_count()
        s.send(CTRL_T)
        s.send(b"n", 1.5)
        assert s.tab_count() == before


@needs_zellij
@pytest.mark.parametrize("fixture", SHAPES)
def test_alt_x_then_n_adds_a_tab(tmp_path: Path, fixture: str) -> None:
    with live(tmp_path, fixture, 2) as s:
        before = s.tab_count()
        s.send(alt("x"))
        s.send(b"n")
        assert s.wait_for(lambda: s.tab_count() == before + 1, 5)


@needs_zellij
@pytest.mark.parametrize("fixture", SHAPES)
def test_alt_x_inside_tab_mode_returns_to_normal(tmp_path: Path, fixture: str) -> None:
    with live(tmp_path, fixture, 2) as s:
        before = s.tab_count()
        s.send(alt("x"))
        s.send(alt("x"))
        s.send(b"n", 1.5)
        assert s.tab_count() == before


@needs_zellij
@pytest.mark.parametrize("fixture", SHAPES)
def test_lock_and_quit_sequence(tmp_path: Path, fixture: str) -> None:
    with live(tmp_path, fixture, 2) as s:
        s.send(alt("g"))
        s.send(alt("s"))
        s.send(b"q", 1.5)
        assert s.alive(), "q must be ignored while locked"
        s.send(CTRL_G)
        s.send(alt("ы"))
        s.send("й".encode("utf-8"))
        assert s.wait_for(lambda: not s.alive(), 8), "Alt ы then й should quit"


@needs_zellij
def test_alt_s_then_q_quits(tmp_path: Path) -> None:
    with live(tmp_path, STOCK, 2) as s:
        s.send(alt("s"))
        s.send(b"q")
        assert s.wait_for(lambda: not s.alive(), 8)


# ── live scenarios, tier 1 and control ────────────────────────────────────────

@needs_zellij
@pytest.mark.parametrize("fixture", [STOCK, MINIMAL])
def test_tier1_ctrl_t_still_opens_tab_mode(tmp_path: Path, fixture: str) -> None:
    with live(tmp_path, fixture, 1) as s:
        before = s.tab_count()
        s.send(CTRL_T)
        s.send(b"n")
        assert s.wait_for(lambda: s.tab_count() == before + 1, 5)


@needs_zellij
def test_tier1_ctrl_q_does_not_quit(tmp_path: Path) -> None:
    with live(tmp_path, STOCK, 1) as s:
        s.send(CTRL_Q, 1.5)
        s.send(CTRL_Q_LEGACY, 1.5)
        assert s.alive()


@needs_zellij
def test_control_unmerged_stock_ctrl_q_quits(tmp_path: Path) -> None:
    with live(tmp_path, STOCK, None) as s:
        s.send(CTRL_Q_LEGACY)
        assert s.wait_for(lambda: not s.alive(), 8), "the harness must be able to see a quit"
