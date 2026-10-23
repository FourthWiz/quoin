"""Live check of the list-sessions parser against a real Zellij 0.44.x.

Skipped without Zellij. With it, everything runs in a temp HOME, temp XDG dirs
and short private socket dirs under /tmp; the user's own Zellij state and Agent
Desk socket directory are never touched.
"""
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from quoin import agentdesk_sessions as ads
from test_agentdesk_keybinds_live import run_zellij, zellij_env
from test_agentdesk_layout_live import needs_zellij


def _runner(expected_sock):
    def run(argv, **kw):
        assert kw["env"]["ZELLIJ_SOCKET_DIR"] == expected_sock, "listing used a socket dir that is not the test's own"
        return subprocess.run(argv, **kw)
    return run


def _names(zellij, env, sock):
    return {s.name: s for s in ads.list_sessions(zellij, env, run=_runner(sock))}


@needs_zellij
def test_real_listing_shapes(tmp_path):
    sock = tempfile.mkdtemp(dir="/tmp", prefix="adq")
    sock2 = tempfile.mkdtemp(dir="/tmp", prefix="adq")
    name = "adk" + uuid.uuid4().hex[:8]
    env = zellij_env(tmp_path, sock)
    env2 = dict(env, ZELLIJ_SOCKET_DIR=sock2)
    cfg = Path(env["XDG_CONFIG_HOME"]) / "zellij"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.kdl").write_text("show_release_notes false\nshow_startup_tips false\n")
    zellij = shutil.which("zellij", path=env["PATH"])
    try:
        assert ads.list_sessions(zellij, env, run=_runner(sock)) == []
        started = run_zellij(env, sock, "attach", "-b", "-c", name, timeout=30)
        assert started.returncode == 0, started.stderr
        deadline = time.time() + 15
        while True:
            found = _names(zellij, env, sock)
            if name in found or time.time() >= deadline:
                break
            time.sleep(0.3)
        assert name in found, found
        assert found[name].state == "running" and found[name].live
        assert found[name].age

        assert run_zellij(env, sock, "kill-session", name).returncode == 0
        # A background session that never rendered a client leaves nothing to
        # resurrect, so it disappears from the listing instead of turning EXITED.
        deadline = time.time() + 10
        while True:
            names = _names(zellij, env, sock)
            if name not in names or time.time() >= deadline:
                break
            time.sleep(0.3)
        assert name not in names, names

        # A second, empty socket directory lists nothing either.
        assert ads.list_sessions(zellij, env2, run=_runner(sock2)) == []
    finally:
        run_zellij(env, sock, "delete-session", "--force", name)
        shutil.rmtree(sock, ignore_errors=True)
        shutil.rmtree(sock2, ignore_errors=True)
