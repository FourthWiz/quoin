"""Tests for the agentdesk home-isolation machinery in conftest.py.

Everything here runs against fake homes under tmp_path; the real home is only
ever compared by path, never read or written.
"""
import os
import subprocess
from pathlib import Path

import pytest

import conftest as cf
import test_agentdesk_help as help_mod
import test_agentdesk_public_launch as pl
import test_agentdesk_zsh_args as za


def _state_file(env, cwd):
    script = f'source "{za.AGENTDESK_ZSH}"\n_agentdesk_state_file'
    result = subprocess.run(
        ["zsh", "-c", script], env=env, capture_output=True, text=True,
        cwd=str(cwd), timeout=za.SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip())


def test_home_is_temp_and_exists():
    home = Path(os.environ["HOME"])
    assert home.is_dir()
    assert home.resolve() != Path(cf._REAL_HOME).resolve()
    assert not os.environ.get("AGENTDESK_DASHBOARD")
    assert not os.environ.get("ZELLIJ")


@pytest.mark.parametrize("builder", ["mock", "sessions", "help", "public"])
def test_env_helpers_resolve_state_under_temp_root(builder, tmp_path):
    if builder == "mock":
        env = za._make_mock_env(tmp_path)
    elif builder == "sessions":
        env = za._make_mock_env_with_sessions(tmp_path, ["a"])
    elif builder == "help":
        env = help_mod._mock_env(tmp_path)
    else:
        env = pl._env(tmp_path, str(tmp_path / "rt"))
    state = _state_file(env, tmp_path)
    assert str(state).startswith(str(tmp_path))
    assert str(state.resolve()).startswith(str(tmp_path.resolve()))


def _record_dir(env, cwd):
    script = f'source "{za.AGENTDESK_ZSH}"\n_agentdesk_record_dir'
    result = subprocess.run(
        ["zsh", "-c", script], env=env, capture_output=True, text=True,
        cwd=str(cwd), timeout=za.SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip())


@pytest.mark.parametrize("builder", ["mock", "sessions", "help", "public"])
def test_env_helpers_resolve_record_dir_under_temp_root(builder, tmp_path):
    if builder == "mock":
        env = za._make_mock_env(tmp_path)
    elif builder == "sessions":
        env = za._make_mock_env_with_sessions(tmp_path, ["a"])
    elif builder == "help":
        env = help_mod._mock_env(tmp_path)
    else:
        env = pl._env(tmp_path, str(tmp_path / "rt"))
    rec = _record_dir(env, tmp_path)
    assert str(rec).startswith(str(tmp_path))
    assert str(rec.resolve()).startswith(str(tmp_path.resolve()))


def test_python_record_dir_is_under_temp_home():
    from quoin import agentdesk_sessions
    rec = Path(agentdesk_sessions.records_dir(os.environ))
    assert rec.parts[-3:] == (".config", "agentdesk", "sessions")
    assert Path(os.environ["HOME"]).resolve() != Path(cf._REAL_HOME).resolve()
    assert str(rec).startswith(os.environ["HOME"])


# ── child-env resolution guard ──────────────────────────────────────────────

@pytest.fixture
def fake_real(tmp_path):
    real = tmp_path / "realhome"
    real.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    return real, base


def test_guard_accepts_temp_home(fake_real, tmp_path):
    real, base = fake_real
    other = tmp_path / "elsewhere"
    other.mkdir()
    cf._check_child_env(
        {"HOME": str(other), "XDG_CONFIG_HOME": str(other / ".config")}, real, base
    )


def test_guard_rejects_real_home(fake_real):
    real, base = fake_real
    with pytest.raises(AssertionError, match="HOME"):
        cf._check_child_env({"HOME": str(real)}, real, base)


def test_guard_rejects_xdg_under_real_home(fake_real, tmp_path):
    real, base = fake_real
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(AssertionError, match="XDG_CONFIG_HOME"):
        cf._check_child_env(
            {"HOME": str(other), "XDG_CONFIG_HOME": str(real / ".config")}, real, base
        )


def test_guard_rejects_trailing_slash_and_symlink_alias(fake_real, tmp_path):
    real, base = fake_real
    with pytest.raises(AssertionError):
        cf._check_child_env({"HOME": str(real) + "/"}, real, base)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(AssertionError):
        cf._check_child_env({"HOME": str(alias)}, real, base)


def test_guard_rejects_explicit_env_without_or_with_empty_home(fake_real):
    real, base = fake_real
    with pytest.raises(AssertionError, match="without HOME"):
        cf._check_child_env({"PATH": "/usr/bin"}, real, base)
    with pytest.raises(AssertionError, match="without HOME"):
        cf._check_child_env({"HOME": "", "PATH": "/usr/bin"}, real, base)


def test_guard_rejects_relative_home_and_xdg(fake_real, tmp_path):
    real, base = fake_real
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(AssertionError, match="relative"):
        cf._check_child_env({"HOME": "home"}, real, base)
    with pytest.raises(AssertionError, match="relative"):
        cf._check_child_env(
            {"HOME": str(other), "XDG_CONFIG_HOME": ".config"}, real, base
        )


def test_guard_rejects_zdotdir_into_real_home_or_relative(fake_real, tmp_path):
    real, base = fake_real
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(AssertionError, match="ZDOTDIR"):
        cf._check_child_env({"HOME": str(other), "ZDOTDIR": str(real)}, real, base)
    with pytest.raises(AssertionError, match="ZDOTDIR"):
        cf._check_child_env({"HOME": str(other), "ZDOTDIR": "rel"}, real, base)
    cf._check_child_env({"HOME": str(other), "ZDOTDIR": str(other)}, real, base)


def test_zdotdir_points_at_temp_home():
    assert os.environ["ZDOTDIR"] == os.environ["HOME"]


def test_spawn_guard_rejects_relative_home():
    with pytest.raises(AssertionError):
        subprocess.run(["true"], env={"HOME": "rel", "PATH": "/usr/bin:/bin"})


def test_guard_accepts_inherited_env():
    cf._check_child_env(None, "/nonexistent-real-home", "/nonexistent-base")


def test_guard_accepts_allowed_root_under_real_home(tmp_path):
    real = tmp_path / "realhome"
    allowed = real / "tmp"
    (allowed / "h").mkdir(parents=True)
    cf._check_child_env({"HOME": str(allowed / "h")}, real, allowed)


def test_spawn_guard_rejects_real_home_keyword():
    with pytest.raises(AssertionError):
        subprocess.run(
            ["true"], env={"HOME": cf._REAL_HOME, "PATH": "/usr/bin:/bin"}
        )


def test_spawn_guard_rejects_real_home_positional_env():
    # env is the 11th Popen parameter (args, bufsize, executable, stdin,
    # stdout, stderr, preexec_fn, close_fds, shell, cwd, env).
    with pytest.raises(AssertionError):
        subprocess.Popen(
            ["true"], -1, None, None, None, None, None, True, False, None,
            {"HOME": cf._REAL_HOME, "PATH": "/usr/bin:/bin"},
        )


def test_spawn_guard_allows_inherited_env():
    assert subprocess.run(["true"]).returncode == 0


# ── snapshot helpers ────────────────────────────────────────────────────────

def _fake_home(tmp_path):
    home = tmp_path / "snaphome"
    (home / ".config" / "agentdesk").mkdir(parents=True)
    (home / ".config" / "agentdesk" / "last-layout").write_text("a=b\n")
    return home


def test_snapshot_detects_content_change(tmp_path):
    home = _fake_home(tmp_path)
    before = cf._snapshot_real_targets(home)
    (home / ".config" / "agentdesk" / "last-layout").write_text("a=c\n")
    changed = cf._diff_snapshots(before, cf._snapshot_real_targets(home))
    assert changed == [str(home / ".config" / "agentdesk" / "last-layout")]


def test_snapshot_detects_creation_and_deletion(tmp_path):
    home = _fake_home(tmp_path)
    before = cf._snapshot_real_targets(home)
    zshrc = home / ".zshrc"
    zshrc.write_text("x\n")
    created = cf._snapshot_real_targets(home)
    assert cf._diff_snapshots(before, created) == [str(zshrc)]
    zshrc.unlink()
    (home / ".config" / "agentdesk" / "last-layout").unlink()
    deleted = cf._snapshot_real_targets(home)
    assert str(home / ".config" / "agentdesk" / "last-layout") in cf._diff_snapshots(
        created, deleted
    )


def test_snapshot_detects_stray_backup_via_listing(tmp_path):
    home = _fake_home(tmp_path)
    before = cf._snapshot_real_targets(home)
    (home / ".config" / "agentdesk" / "last-layout.bak.X").write_text("")
    assert cf._diff_snapshots(before, cf._snapshot_real_targets(home)) == [
        cf._LISTING_KEY
    ]


def test_snapshot_ignores_mtime_only_change(tmp_path):
    home = _fake_home(tmp_path)
    path = home / ".config" / "agentdesk" / "last-layout"
    before = cf._snapshot_real_targets(home)
    os.utime(path, (1, 1))
    assert cf._diff_snapshots(before, cf._snapshot_real_targets(home)) == []
