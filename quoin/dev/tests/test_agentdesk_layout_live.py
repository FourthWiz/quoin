"""Live checks of the generated Agent Desk layout in a real Zellij session.

Each test generates a layout with the real builder, swaps every pane command
for `sleep` (so no agent CLI ever starts), and runs it in its own Zellij
session on a pty with an isolated HOME and an isolated ZELLIJ_SOCKET_DIR.
Cleanup only ever names the test's own session. Skipped unless Zellij 0.44.x
is installed.
"""
import contextlib
import fcntl
import json
import os
import pty
import re
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

from test_agentdesk_keybinds_live import run_zellij, zellij_env

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTDESK_ZSH = REPO_ROOT / "quoin" / "tools" / "agentdesk" / "agentdesk.zsh"
ZELLIJ = shutil.which("zellij")
ROWS, COLS = 50, 200


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
    reason="live layout tests need Zellij 0.44.x on PATH",
)


def _write_config(env):
    cfg = Path(env["XDG_CONFIG_HOME"]) / "zellij"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.kdl").write_text(
        "show_release_notes false\nshow_startup_tips false\n", encoding="utf-8"
    )


def _sleep_commands(text: str) -> str:
    """Replace every pane command with `sleep 600` and check nothing was missed."""
    count = text.count('command "zsh"')
    assert count > 0
    swapped, n_cmd = re.subn(r'command "zsh"', 'command "sleep"', text)
    swapped, n_args = re.subn(r'args "-lc" ".*"', 'args "600"', swapped)
    assert n_cmd == count and n_args == count, "pane command rewrite was incomplete"
    assert "claude" not in swapped.replace("Claude", "") and "codex" not in swapped.replace("Codex", "")
    return swapped


class LiveLayout:
    def __init__(self, tmp_path, layout_text, auto_layout="false", rewrite=None):
        self.tmp_path = Path(tmp_path)
        self.sock = tempfile.mkdtemp(dir="/tmp", prefix="adz")
        self.name = "adl" + uuid.uuid4().hex[:8]
        self.env = zellij_env(tmp_path, self.sock)
        self.env["TMPDIR"] = str(self.tmp_path)
        _write_config(self.env)
        self.layout = self.tmp_path / "live-layout.kdl"
        self.layout.write_text((rewrite or _sleep_commands)(layout_text), encoding="utf-8")
        self.auto_layout = auto_layout
        self.pid = 0
        self.fd = -1

    def z(self, *args, timeout=30):
        return run_zellij(self.env, self.sock, *args, timeout=timeout)

    def act(self, *args):
        done = self.z("-s", self.name, "action", *args)
        assert done.returncode == 0, f"{args}: {done.stderr}"
        return done.stdout

    def start(self):
        assert str(Path(self.env["HOME"]).resolve()).startswith(str(self.tmp_path.resolve()))
        argv = ["zellij", "--new-session-with-layout", str(self.layout), "--session", self.name]
        if self.auto_layout is not None:
            argv += ["options", "--auto-layout", self.auto_layout]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe("zellij", argv, self.env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        self.drain(2.0)

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

    def wait_for(self, predicate, timeout=20):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                last = predicate()
            except (json.JSONDecodeError, AssertionError, KeyError, IndexError):
                last = None
            if last:
                return last
            self.drain(0.4)
        return last

    def panes(self):
        done = self.z("-s", self.name, "action", "list-panes", "--json")
        return [p for p in json.loads(done.stdout) if not p["is_plugin"]]

    def tiled(self, tab=None):
        return [p for p in self.panes() if not p["is_floating"]
                and (tab is None or p["tab_name"] == tab)]

    def ready(self, count):
        got = self.wait_for(lambda: len(self.panes()) == count and self.panes(), 30)
        assert got, f"expected {count} panes, saw {[p['title'] for p in self.panes()]}"
        self.drain(1.0)

    def dump(self):
        return self.act("dump-layout")

    def settle(self, seconds=1.2):
        self.drain(seconds)

    def close(self):
        with contextlib.suppress(Exception):
            self.z("kill-session", self.name)
        with contextlib.suppress(Exception):
            self.z("delete-session", self.name, "--force")
        if self.pid:
            with contextlib.suppress(Exception):
                end = time.time() + 5
                while time.time() < end:
                    done, _ = os.waitpid(self.pid, os.WNOHANG)
                    if done:
                        self.pid = 0
                        break
                    self.drain(0.2)
                if self.pid:
                    os.kill(self.pid, signal.SIGKILL)
                    os.waitpid(self.pid, 0)
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.fd)
        shutil.rmtree(self.sock, ignore_errors=True)


def generate(tmp_path, tokens, bars=None):
    flag = f"--bars {bars} " if bars else ""
    env = {"HOME": str(Path(tmp_path) / "home"), "PATH": "/usr/bin:/bin",
           "TMPDIR": str(tmp_path)}
    (Path(tmp_path) / "home").mkdir(exist_ok=True)
    done = subprocess.run(
        ["zsh", "-c", f'source "{AGENTDESK_ZSH}"; _agentdesk_gen_layout {flag}{tokens}'],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return Path(done.stdout.strip()).read_text()


@contextlib.contextmanager
def live(tmp_path, layout_text, count, auto_layout="false", rewrite=None, prepare=None):
    session = LiveLayout(tmp_path, layout_text, auto_layout, rewrite)
    try:
        if prepare:
            prepare(session)
        session.start()
        session.ready(count)
        yield session
    finally:
        session.close()


def by_title(panes):
    return {p["title"]: p for p in panes}


def shape(session):
    """Rows of (title, columns) for the tiled agent and shell panes."""
    return sorted((p["title"], p["pane_columns"]) for p in session.tiled())


def full_shape(session):
    """Like shape(), with rows too, so a return to base checks both axes."""
    return sorted((p["title"], p["pane_columns"], p["pane_rows"]) for p in session.tiled())


# ── harness unit tests (no Zellij needed) ─────────────────────────────────────

def test_live_env_never_leaks_zellij_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "real")
    sock = tempfile.mkdtemp(dir="/tmp", prefix="adz")
    try:
        env = zellij_env(tmp_path, sock)
        assert not [k for k in env if k.startswith("ZELLIJ") and k != "ZELLIJ_SOCKET_DIR"]
        assert env["ZELLIJ_SOCKET_DIR"] == sock
    finally:
        shutil.rmtree(sock, ignore_errors=True)


def test_run_zellij_refuses_a_socket_dir_outside_the_test(tmp_path):
    own = tempfile.mkdtemp(dir="/tmp", prefix="adz")
    try:
        env = zellij_env(tmp_path, own)
        with pytest.raises(AssertionError, match="not the test's own"):
            run_zellij(env, "/tmp/zellij-agentdesk-501", "ls")
    finally:
        shutil.rmtree(own, ignore_errors=True)


def test_command_rewrite_is_complete_and_not_vacuous(tmp_path):
    text = generate(tmp_path, "claude claude shell")
    swapped = _sleep_commands(text)
    assert 'command "zsh"' not in swapped
    assert swapped.count('command "sleep"') == text.count('command "zsh"') == 4
    with pytest.raises(AssertionError):
        _sleep_commands("layout {\n}\n")


# ── live scenarios ─────────────────────────────────────────────────────────────

@needs_zellij
def test_stream_deck_shape_titles(tmp_path):
    with live(tmp_path, generate(tmp_path, "codex codex shell"), 4) as s:
        panes = s.panes()
        assert sorted(p["title"] for p in panes) == [
            "Codex · A", "Codex · B", "Shell", "Token Spend"]
        floating = [p for p in panes if p["is_floating"]]
        assert [p["title"] for p in floating] == ["Token Spend"]
        # The Stream Deck dashboard selects agent panes by title substring.
        agents = [p for p in panes if "codex" in p["title"].lower()]
        assert sorted(p["title"] for p in agents) == ["Codex · A", "Codex · B"]


@needs_zellij
def test_two_agents_and_strip_proportions(tmp_path):
    with live(tmp_path, generate(tmp_path, "claude claude shell"), 4) as s:
        assert "hide_floating_panes=true" in s.dump()
        panes = by_title(s.panes())
        agents = panes["Claude · A"]["pane_rows"]
        shell = panes["Shell"]
        tiled_rows = agents + shell["pane_rows"]
        assert 0.70 <= agents / tiled_rows <= 0.80
        assert shell["pane_columns"] == COLS
        assert panes["Claude · A"]["pane_columns"] + panes["Claude · B"]["pane_columns"] == COLS


@needs_zellij
def test_swap_layouts_cycle_and_return(tmp_path):
    with live(tmp_path, generate(tmp_path, "claude claude shell"), 4) as s:
        base = shape(s)
        base_full = full_shape(s)
        assert dict(base)["Shell"] == COLS

        s.act("next-swap-layout"); s.settle()
        even = [p["pane_columns"] for p in s.tiled()]
        assert len(even) == 3 and max(even) - min(even) <= 1

        s.act("next-swap-layout"); s.settle()
        cols = sorted(p["pane_columns"] for p in s.tiled())
        assert cols[-1] >= COLS * 0.65, cols       # the 70% pane
        assert cols[0] <= COLS * 0.35, cols        # next to the stacked group

        s.act("next-swap-layout"); s.settle()
        stacked = {p["pane_columns"] for p in s.tiled()}
        assert stacked == {COLS}, stacked           # one stacked group

        s.act("next-swap-layout"); s.settle()
        assert shape(s) == base
        assert full_shape(s) == base_full

        s.act("previous-swap-layout"); s.settle()
        assert {p["pane_columns"] for p in s.tiled()} == {COLS}


@needs_zellij
def test_floating_toggle_shows_and_hides_spend(tmp_path):
    with live(tmp_path, generate(tmp_path, "claude shell"), 3) as s:
        assert "hide_floating_panes=true" in s.dump()
        s.act("toggle-floating-panes"); s.settle()
        assert "hide_floating_panes=true" not in s.dump()
        s.act("toggle-floating-panes"); s.settle()
        assert "hide_floating_panes=true" in s.dump()


@needs_zellij
def test_new_pane_keeps_the_strip_when_auto_layout_is_off(tmp_path):
    with live(tmp_path, generate(tmp_path, "claude claude shell"), 4) as s:
        s.act("new-pane"); s.settle(2.0)
        assert len(s.tiled()) == 4
        assert by_title(s.panes())["Shell"]["pane_columns"] == COLS
        s.act("close-pane"); s.settle()
        assert len(s.tiled()) == 3
        panes = by_title(s.panes())
        assert panes["Shell"]["pane_columns"] == COLS
        assert panes["Claude · A"]["pane_columns"] == panes["Claude · B"]["pane_columns"] == COLS // 2


@needs_zellij
@pytest.mark.xfail(strict=False, reason="depends on Zellij's default auto-layout rule")
def test_default_auto_layout_rearranges_on_new_pane(tmp_path):
    with live(tmp_path, generate(tmp_path, "claude claude shell"), 4, auto_layout=None) as s:
        s.act("new-pane"); s.settle(2.0)
        assert by_title(s.panes())["Shell"]["pane_columns"] != COLS


@needs_zellij
def test_compact_bar_gives_one_bar_row(tmp_path):
    with live(tmp_path, generate(tmp_path, "claude", "compact"), 2) as s:
        agent = by_title(s.panes())["Claude Code"]
        assert agent["pane_rows"] == ROWS - 1
        for _ in range(3):
            s.act("next-swap-layout"); s.settle()
        assert by_title(s.panes())["Claude Code"]["pane_rows"] == ROWS - 1


@needs_zellij
def test_dashboard_tab_has_bars_and_survives_a_swap(tmp_path):
    base = tmp_path / "base.kdl"
    base.write_text(generate(tmp_path, "claude shell"))
    env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)}
    done = subprocess.run(
        ["zsh", "-c",
         f'source "{AGENTDESK_ZSH}"; sed \'$d\' "{base}"; '
         f'_agentdesk_kdl_dashboard_tab "{base}" "{tmp_path}/url.txt"'],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    with live(tmp_path, done.stdout, 4) as s:
        dump = s.dump()
        dash = dump.split('tab name="Dashboard"', 1)[1].split("new_tab_template", 1)[0]
        assert 'location="zellij:tab-bar"' in dash and 'location="zellij:status-bar"' in dash
        s.act("go-to-tab-name", "Dashboard"); s.settle()
        before = sorted(p["title"] for p in s.panes())
        s.act("next-swap-layout"); s.settle()
        assert sorted(p["title"] for p in s.panes()) == before


def generate_standard(tmp_path):
    env = {"HOME": str(Path(tmp_path) / "home"), "PATH": "/usr/bin:/bin",
           "TMPDIR": str(tmp_path)}
    (Path(tmp_path) / "home").mkdir(exist_ok=True)
    done = subprocess.run(
        ["zsh", "-c", f'source "{AGENTDESK_ZSH}"; _agentdesk_gen_standard_layout --stdout'],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _sleep_main_tab_only(text: str) -> str:
    """Swap the main tab's agent, shell and spend commands; keep git and status real."""
    head, sep, rest = text.partition('    tab name="git"')
    assert sep, "standard layout has no git tab"
    swapped = _sleep_commands(head)
    assert swapped.count('command "sleep"') == 4
    return swapped + sep + rest


@needs_zellij
def test_standard_layout_git_and_status_tabs_stay_alive(tmp_path):
    project = Path(tmp_path) / "project"
    for name in ("repo-a", "repo-b"):
        (project / name / ".git").mkdir(parents=True)

    def prepare(session):
        helper_dir = Path(session.env["HOME"]) / ".config" / "agentdesk"
        helper_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(AGENTDESK_ZSH, helper_dir / "agentdesk.zsh")
        session.env["PROJECT_ROOT"] = str(project)

    with live(tmp_path, generate_standard(tmp_path), 6, rewrite=_sleep_main_tab_only,
              prepare=prepare) as s:
        s.settle(3.0)
        panes = s.panes()
        assert sorted({p["tab_name"] for p in panes}) == ["git", "main", "status"]
        by_name = {p["title"]: p for p in panes}
        for title in ("Git", "Status"):
            assert title in by_name, [p["title"] for p in panes]
            assert not by_name[title]["exited"], title
            assert not by_name[title]["is_held"], title
        assert re.search(r'tab name="main"[^\n]*focus=true', s.dump())
