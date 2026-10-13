"""Path resolution, the platform gate, and the lifecycle test guards."""
import os
import subprocess

import pytest

import _streamdeck_helpers as h
import _streamdeck_lifecycle as lc
from _streamdeck_lifecycle import deck_home, forbid_dangerous_subprocess, lifecycle_home  # noqa: F401
from quoin.streamdeck import macos, paths as paths_mod

pytestmark = pytest.mark.usefixtures("forbid_dangerous_subprocess")


def env(home, **extra):
    return dict({"HOME": str(home)}, **extra)


def test_defaults_live_under_home(lifecycle_home):
    p = lifecycle_home.resolve()
    assert p.runtime == str(lifecycle_home.runtime) and " " in p.runtime
    assert p.plugin_dir == str(lifecycle_home.plugin_dir)
    assert p.state_root == str(lifecycle_home.state_root)
    assert p.config == os.path.join(p.state_root, "config.json")
    assert p.claude_settings == str(lifecycle_home.claude_settings)
    assert p.codex_home == str(lifecycle_home.codex_home)
    assert p.codex_hooks == os.path.join(p.codex_home, "hooks.json")
    assert p.agentdesk.endswith(".config/agentdesk/agentdesk.zsh")
    assert p.manifest == os.path.join(p.runtime, "install-manifest.json")
    assert p.current == os.path.join(p.runtime, "current")
    assert p.hook_shim.endswith("bin/quoin-deck-hook")


@pytest.mark.parametrize("flag,envname,field", [
    ("runtime_dir", "QUOIN_STREAMDECK_RUNTIME_DIR", "runtime"),
    ("plugins_dir", "QUOIN_STREAMDECK_PLUGINS_DIR", "plugins_dir"),
    ("state_root", "QUOIN_STREAMDECK_STATE_ROOT", "state_root"),
    ("codex_home", "CODEX_HOME", "codex_home"),
])
def test_flag_beats_env_beats_default(lifecycle_home, flag, envname, field):
    home = lifecycle_home.home
    by_env = lifecycle_home.raw_resolve({}, env(home, **{envname: "/x/env"}))
    assert getattr(by_env, field) == "/x/env"
    by_flag = lifecycle_home.raw_resolve({flag: "/x/flag"}, env(home, **{envname: "/x/env"}))
    assert getattr(by_flag, field) == "/x/flag"
    assert getattr(lifecycle_home.raw_resolve({}, env(home)), field) != "/x/env"


def test_claude_config_dir_and_flag(lifecycle_home):
    home = lifecycle_home.home
    assert lifecycle_home.raw_resolve({}, env(home, CLAUDE_CONFIG_DIR="/alt")).claude_settings == "/alt/settings.json"
    assert lifecycle_home.raw_resolve({"claude_settings": "/f/s.json"}, env(home, CLAUDE_CONFIG_DIR="/alt")).claude_settings == "/f/s.json"


def test_agentdesk_flag(lifecycle_home):
    assert lifecycle_home.raw_resolve({"agentdesk": "/a/agentdesk.zsh"}, env(lifecycle_home.home)).agentdesk == "/a/agentdesk.zsh"


def test_relative_flag_is_made_absolute_and_tilde_uses_env_home(lifecycle_home):
    p = lifecycle_home.raw_resolve({"runtime_dir": "rel/dir", "state_root": "~/sr"}, env(lifecycle_home.home))
    assert os.path.isabs(p.runtime) and p.runtime.endswith("rel/dir")
    assert p.state_root == str(lifecycle_home.home / "sr")


def test_symlinked_runtime_is_not_resolved(lifecycle_home):
    real = lifecycle_home.root / "real"
    real.mkdir()
    link = lifecycle_home.root / "link"
    link.symlink_to(real)
    p = lifecycle_home.raw_resolve({"runtime_dir": str(link)}, env(lifecycle_home.home))
    assert p.runtime == str(link)


def test_state_is_always_under_state_root(lifecycle_home):
    home = lifecycle_home.home
    for flags in ({}, {"state_root": "/a/b"}, {"runtime_dir": "/r", "state_root": "/c"}):
        for extra in ({}, {"QUOIN_STREAMDECK_STATE_ROOT": "/e/f"}):
            p = lifecycle_home.raw_resolve(flags, env(home, **extra))
            assert p.state == os.path.join(p.state_root, "state")
            assert p.config == os.path.join(p.state_root, "config.json")


def test_manifest_paths_keys(lifecycle_home):
    assert set(lifecycle_home.resolve().manifest_paths()) == {
        "runtime", "plugin_dir", "config", "state", "claude_settings", "codex_hooks", "agentdesk"}


def test_platform_gate():
    for system in ("linux", "win32"):
        info = macos.detect(system, lambda: ("", ("", "", ""), ""), lambda: "x86_64")
        assert not info.supported
        with pytest.raises(macos.UnsupportedPlatform, match="macOS only"):
            macos.require_macos(info)
    info = macos.detect("darwin", lambda: ("15.1", ("", "", ""), ""), lambda: "arm64")
    assert info.supported and info.macos == "15.1" and info.machine == "arm64"
    assert macos.require_macos(info) is info


def test_guard_rejects_real_settings_path(lifecycle_home):
    bad = lifecycle_home.raw_resolve({"claude_settings": os.path.join(h.REAL_HOME, ".claude", "settings.json")},
                            env(lifecycle_home.home))
    with pytest.raises(h.GuardViolation):
        lc.assert_paths_under(bad, lifecycle_home.root)
    lc.assert_paths_under(lifecycle_home.resolve(), lifecycle_home.root)


def test_guard_blocks_brew_argv():
    with pytest.raises(h.GuardViolation):
        subprocess.Popen(["brew", "--version"])
    h.VIOLATIONS.clear()
