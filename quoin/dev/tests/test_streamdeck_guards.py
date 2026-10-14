"""The Stream Deck test guards must fire for what they claim to forbid."""
import os
import pathlib
import subprocess
import sys

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")


def safe_env(deck_home):
    return {"HOME": str(deck_home.home), "PATH": "/usr/bin:/bin"}


def test_bare_zellij_is_refused(deck_home):
    with pytest.raises(h.GuardViolation):
        h.check_call(["zellij", "list-sessions"], safe_env(deck_home))


def test_fake_zellij_path_is_allowed(deck_home):
    h.check_call([h.FAKE_BINS["zellij"], "list-sessions"], safe_env(deck_home))


def test_real_home_config_argv_is_refused(deck_home):
    with pytest.raises(h.GuardViolation):
        h.check_call(["/bin/cat", os.path.join(h.REAL_HOME, ".claude", "settings.json")], safe_env(deck_home))
    with pytest.raises(h.GuardViolation):
        h.check_call(["/bin/ls", "/tmp/zellij-agentdesk-%d/x" % os.getuid()], safe_env(deck_home))


@pytest.mark.parametrize("name", ["kill", "pkill", "killall", "brew", "launchctl"])
def test_dangerous_commands_are_refused(name, deck_home):
    with pytest.raises(h.GuardViolation):
        h.check_call([name, "1"], safe_env(deck_home))


def test_home_leaking_env_is_refused(deck_home):
    with pytest.raises(h.GuardViolation):
        h.check_call(["/bin/echo"], {"HOME": h.REAL_HOME})
    with pytest.raises(h.GuardViolation):
        h.check_call(["/bin/echo"], {"HOME": str(deck_home.home), "ZELLIJ_SOCKET_DIR": h.FORBIDDEN_ROOTS[-1]})


def test_repo_and_venv_paths_pass(deck_home):
    h.check_call([sys.executable, str(h.REPO_ROOT / "src")], safe_env(deck_home))
    h.check_call(["/usr/bin/python3", "-V"], safe_env(deck_home))


def test_subprocess_hook_is_installed_and_records(deck_home, forbid_dangerous_subprocess):
    with pytest.raises(h.GuardViolation):
        subprocess.run(["zellij", "list-sessions"], env=safe_env(deck_home))
    assert h.VIOLATIONS
    h.VIOLATIONS.clear()


def test_fake_zellij_runs_and_logs(deck_home):
    result = subprocess.run(
        [h.FAKE_BINS["zellij"], "list-sessions"], env=safe_env(deck_home), capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "No active zellij sessions found" in result.stderr
    assert pathlib.Path(h.FAKE_BINS["zellij"]).with_suffix(".log").read_text().strip() == "list-sessions"


def test_post_check_fires_for_path_outside_tmp(deck_home):
    class Env:
        runtime = str(deck_home.root / "runtime")
        socket_dir = "/tmp/elsewhere"
        bins = {"zellij": h.FAKE_BINS["zellij"]}

    with pytest.raises(h.GuardViolation):
        h.check_env_paths(Env, deck_home.root)
    Env.socket_dir = str(deck_home.zsock)
    h.check_env_paths(Env, deck_home.root)
