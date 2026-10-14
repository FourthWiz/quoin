import ast
import json
import os
import stat
import subprocess
import time

import pytest

import _streamdeck_helpers as h
from _streamdeck_helpers import deck_home, forbid_dangerous_subprocess, payload  # noqa: F401

pytestmark = pytest.mark.usefixtures("deck_home", "forbid_dangerous_subprocess")

AGENTDESK = h.REPO_ROOT / "quoin" / "tools" / "agentdesk" / "agentdesk.zsh"


@pytest.fixture
def de(payload):
    return payload("deckenv")


def write_manifest(runtime, data):
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "install-manifest.json").write_text(json.dumps(data))


def make_env(de, tmp_path, manifest=None, extra=None, path="/usr/bin:/bin"):
    runtime = tmp_path / "rt"
    if manifest is not None:
        write_manifest(runtime, manifest)
    environ = {"HOME": str(tmp_path / "h"), "QUOIN_DECK_RUNTIME": str(runtime), "PATH": path,
               "ZELLIJ_SOCKET_DIR": str(tmp_path / "zs")}
    environ.update(extra or {})
    return de.load(environ)


def exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


# ------------------------------------------------------------------ paths, manifest and environment

def test_defaults_without_manifest(de, tmp_path):
    env = make_env(de, tmp_path)
    assert env.runtime == str(tmp_path / "rt")
    assert env.state_root == str(tmp_path / "h/.config/agentdesk/streamdeck")
    assert env.state_dir == env.state_root + "/state"
    assert env.legacy_agents_dir == env.state_root + "/agents"
    assert env.terminal_entry == str(tmp_path / "rt/bin/quoin-deck-terminal")
    assert env.agentdesk is None
    assert env.socket_dir == str(tmp_path / "zs")


def test_default_runtime_under_home(de, tmp_path):
    env = de.load({"HOME": str(tmp_path / "x")})
    assert env.runtime == str(tmp_path / "x/Library/Application Support/QuoinStreamDeck")


def test_precedence_env_over_manifest_over_default(de, tmp_path):
    manifest = {"paths": {"config": str(tmp_path / "m/config.json"), "state": str(tmp_path / "m/st")}}
    env = make_env(de, tmp_path, manifest)
    assert (env.config_path, env.state_dir, env.state_root) == (
        str(tmp_path / "m/config.json"), str(tmp_path / "m/st"), str(tmp_path / "m"))
    env = make_env(de, tmp_path, manifest, {"QUOIN_DECK_STATE_ROOT": str(tmp_path / "e")})
    assert (env.config_path, env.state_dir) == (str(tmp_path / "e/config.json"), str(tmp_path / "e/state"))
    env = make_env(de, tmp_path, manifest, {"QUOIN_DECK_STATE": str(tmp_path / "old")})
    assert env.state_root == str(tmp_path / "old")
    env = make_env(de, tmp_path, manifest, {"QUOIN_DECK_STATE_ROOT": str(tmp_path / "e"),
                                            "QUOIN_DECK_STATE": str(tmp_path / "old")})
    assert env.state_root == str(tmp_path / "e")


@pytest.mark.parametrize("content", [None, "{not json", "[1, 2]", '"str"', ""])
def test_bad_manifest_never_raises(de, tmp_path, content):
    runtime = tmp_path / "rt"
    runtime.mkdir()
    if content is not None:
        (runtime / "install-manifest.json").write_text(content)
    env = make_env(de, tmp_path)
    assert env.state_root.endswith("agentdesk/streamdeck")


def test_binaries_string_or_object(de, tmp_path):
    z = exe(tmp_path / "b1/zellij")
    o = exe(tmp_path / "b2/osascript")
    env = make_env(de, tmp_path, {"binaries": {"zellij": z, "osascript": {"path": o}}})
    assert env.bins["zellij"] == z and env.bins["osascript"] == o
    assert env.bins["open"] == "/usr/bin/open"


def test_path_env_order_and_no_duplicates(de, tmp_path):
    z = exe(tmp_path / "core/zellij")
    py = exe(tmp_path / "core/python")
    claude = exe(tmp_path / "tools/claude")
    codex = exe(tmp_path / "tools2/codex")
    env = make_env(de, tmp_path, {"binaries": {"zellij": z, "python": py, "claude": claude, "codex": codex}},
                   path="/usr/bin:%s:/bin" % (tmp_path / "core"))
    assert env.path_env.split(":") == [
        str(tmp_path / "core"), str(tmp_path / "tools"), str(tmp_path / "tools2"), "/usr/bin", "/bin"]
    assert "/opt/homebrew" not in env.path_env


def test_recorded_tool_off_path_is_in_path_env(de, tmp_path):
    claude = exe(tmp_path / "elsewhere/claude")
    env = make_env(de, tmp_path, {"binaries": {"claude": claude}})
    assert str(tmp_path / "elsewhere") in env.path_env.split(":")
    assert env.tools == {"claude": claude}


def test_relative_or_nonstring_tool_paths_ignored(de, tmp_path):
    env = make_env(de, tmp_path, {"binaries": {"claude": "claude", "codex": 5, "ccr": {"path": ["x"]},
                                               "lazygit": {"nope": 1}, "python3": ""}})
    assert env.tools == {}


def test_manifest_without_tools_gives_core_plus_inherited(de, tmp_path):
    z = exe(tmp_path / "core/zellij")
    env = make_env(de, tmp_path, {"binaries": {"zellij": z}})
    assert env.path_env == "%s:/usr/bin:/bin" % (tmp_path / "core")


def test_agentdesk_requires_marker(de, tmp_path):
    good = tmp_path / "ad/good.zsh"
    good.parent.mkdir()
    good.write_text("# x\nAGENTDESK_API_VERSION=1\n")
    bad = tmp_path / "ad/bad.zsh"
    bad.write_text("echo hi\n")
    zero = tmp_path / "ad/zero.zsh"
    zero.write_text("AGENTDESK_API_VERSION=0\n")
    assert make_env(de, tmp_path, {"paths": {"agentdesk": str(good)}}).agentdesk == str(good)
    assert make_env(de, tmp_path, {"binaries": {"agentdesk": {"path": str(good)}}}).agentdesk == str(good)
    assert make_env(de, tmp_path, {"paths": {"agentdesk": str(bad)}}).agentdesk is None
    assert make_env(de, tmp_path, {"paths": {"agentdesk": str(zero)}}).agentdesk is None
    assert make_env(de, tmp_path, {"paths": {"agentdesk": str(tmp_path / "missing")}}).agentdesk is None
    if os.getuid() != 0:
        good.chmod(0)
        try:
            assert make_env(de, tmp_path, {"paths": {"agentdesk": str(good)}}).agentdesk is None
        finally:
            good.chmod(0o644)


def test_default_agentdesk_location(de, tmp_path):
    path = tmp_path / "h/.config/agentdesk/agentdesk.zsh"
    path.parent.mkdir(parents=True)
    path.write_text("AGENTDESK_API_VERSION=1\n")
    assert make_env(de, tmp_path).agentdesk == str(path)


def test_zellij_env_does_not_touch_os_environ(de, tmp_path):
    env = make_env(de, tmp_path)
    before = dict(os.environ)
    out = env.zellij_env("/some/sock")
    assert out["ZELLIJ_SOCKET_DIR"] == "/some/sock" and out["PATH"] == env.path_env
    assert dict(os.environ) == before
    out["X"] = "1"
    assert "X" not in os.environ


def test_runtime_with_space_and_no_resolve(de, tmp_path):
    real = tmp_path / "App Support" / "Real"
    real.mkdir(parents=True)
    link = tmp_path / "App Support" / "link"
    link.symlink_to(real)
    env = de.load({"HOME": str(tmp_path), "QUOIN_DECK_RUNTIME": str(link), "ZELLIJ_SOCKET_DIR": "/z"})
    assert env.runtime == str(link)
    assert env.terminal_entry == str(link / "bin" / "quoin-deck-terminal")


def test_record_socket(de, tmp_path):
    env = make_env(de, tmp_path)
    assert de.record_socket({"socket": "/s"}, env) == "/s"
    assert de.record_socket({"socket": ""}, env) == env.socket_dir
    assert de.record_socket({}, env) == env.socket_dir


def test_ps_env_is_fresh_c_locale(de):
    a, b = de.ps_env(), de.ps_env()
    assert a == {"LC_ALL": "C", "PATH": "/usr/bin:/bin"} and a is not b
    a["X"] = 1
    assert "X" not in de.PS_ENV


def test_is_c_lstart(de):
    assert de.is_c_lstart("Tue Oct 13 12:16:19 2026")
    assert de.is_c_lstart("  Tue Oct  3 12:16:19 2026\n")
    assert not de.is_c_lstart("Tue 13 Oct 12:16:19 2026")
    assert not de.is_c_lstart("вторник, 13 октября 2026 г. 12:16:19")
    assert not de.is_c_lstart("")
    assert not de.is_c_lstart(None) and not de.is_c_lstart(5)


@pytest.mark.parametrize("xdg", [None, "set"])
@pytest.mark.parametrize("zsock", [None, "set"])
def test_socket_dir_matches_agentdesk(de, tmp_path, xdg, zsock):
    import shutil
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh missing")
    home = tmp_path / "zh"
    work = tmp_path / "zw"
    stub = tmp_path / "stub"
    for d in (home, work, stub):
        d.mkdir()
    (stub / "mkdir").write_text("#!/bin/sh\nexit 0\n")
    (stub / "mkdir").chmod(0o755)
    environ = {"HOME": str(home), "PATH": "%s:/usr/bin:/bin" % stub}
    if xdg:
        environ["XDG_RUNTIME_DIR"] = str(tmp_path / "xdg")
    if zsock:
        environ["ZELLIJ_SOCKET_DIR"] = str(tmp_path / "zsock")
    out = subprocess.run([zsh, "-f", "-c", 'source "$1"; print -r -- "$ZELLIJ_SOCKET_DIR"', "_", str(AGENTDESK)],
                         env=environ, cwd=str(work), capture_output=True, text=True, timeout=30)
    assert out.stdout.strip().splitlines()[-1] == de.agentdesk_socket_dir(environ), out.stderr


def test_empty_values_count_as_unset(de):
    uid = os.getuid()
    assert de.agentdesk_socket_dir({"ZELLIJ_SOCKET_DIR": "", "XDG_RUNTIME_DIR": ""}) == "/tmp/zellij-agentdesk-%d" % uid


def test_modules_parse_as_py39():
    src = (h.PAYLOAD_DIR / "deckenv.py").read_text()
    ast.parse(src, feature_version=(3, 9))


def test_no_import_time_side_effects(payload, tmp_path):
    before = sorted(os.listdir(tmp_path))
    environ_before = dict(os.environ)
    payload("deckenv")
    assert sorted(os.listdir(tmp_path)) == before and dict(os.environ) == environ_before


# ------------------------------------------------------------------ directories and file modes

def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_ensure_dir_makes_0700_under_umask_022(de, tmp_path):
    old = os.umask(0o022)
    try:
        de.ensure_dir(str(tmp_path / "fresh/state/agents"))
    finally:
        os.umask(old)
    for sub in ("fresh", "fresh/state", "fresh/state/agents"):
        assert mode(tmp_path / sub) == 0o700


def test_ensure_dir_keeps_existing_parent_mode(de, tmp_path):
    parent = tmp_path / "pre"
    parent.mkdir()
    parent.chmod(0o755)
    de.ensure_dir(str(parent / "a/b"))
    assert mode(parent) == 0o755 and mode(parent / "a") == 0o700
    de.ensure_dir(str(parent / "a/b"))


def test_atomic_write_modes_and_no_temp(de, tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    target = d / "x.json"
    de.write_json_atomic(str(target), {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1} and mode(target) == 0o600
    assert sorted(os.listdir(d)) == ["x.json"]


def test_atomic_write_failure_keeps_old_file(de, tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    target = d / "x.json"
    de.write_json_atomic(str(target), {"a": 1})
    with pytest.raises(TypeError):
        de.write_json_atomic(str(target), {"a": object()})
    assert json.loads(target.read_text()) == {"a": 1}
    assert sorted(os.listdir(d)) == ["x.json"]


def test_read_json_default(de, tmp_path):
    (tmp_path / "bad.json").write_text("{")
    assert de.read_json(str(tmp_path / "bad.json"), 7) == 7
    assert de.read_json(str(tmp_path / "none.json"), None) is None


def records_env(de, tmp_path):
    return make_env(de, tmp_path, None, {"QUOIN_DECK_STATE_ROOT": str(tmp_path / "root")})


def test_dual_layout_precedence_and_legacy_left_in_place(de, tmp_path):
    env = records_env(de, tmp_path)
    de.ensure_dir(env.agents_dir)
    de.ensure_dir(env.legacy_agents_dir)
    de.write_json_atomic(os.path.join(env.agents_dir, "same.json"), {"v": "new"})
    de.write_json_atomic(os.path.join(env.legacy_agents_dir, "same.json"), {"v": "old"})
    de.write_json_atomic(os.path.join(env.legacy_agents_dir, "only.json"), {"v": "legacy"})
    got = {os.path.basename(p): (r, layout) for p, r, layout in de.iter_records(env)}
    assert got == {"same.json": ({"v": "new"}, "new"), "only.json": ({"v": "legacy"}, "legacy")}
    shadowed = [(os.path.basename(p), layout) for p, r, layout in de.iter_records(env, include_shadowed=True)]
    assert ("same.json", "legacy-shadowed") in shadowed
    assert os.path.exists(os.path.join(env.legacy_agents_dir, "only.json"))


def test_unparsable_record_yields_none_and_temp_files_ignored(de, tmp_path):
    env = records_env(de, tmp_path)
    de.ensure_dir(env.agents_dir)
    open(os.path.join(env.agents_dir, "bad.json"), "w").write("{")
    open(os.path.join(env.agents_dir, ".x.json.abc.tmp"), "w").write("{}")
    assert [(os.path.basename(p), r) for p, r, _ in de.iter_records(env)] == [("bad.json", None)]


def test_surfaces_keyed_by_socket_with_legacy_only_on_deck_socket(de, tmp_path):
    env = records_env(de, tmp_path)
    de.ensure_dir(env.state_root)
    de.write_json_atomic(env.config_path, {"surfaces": {"s1": "legacy-surface"}, "keep": 1})
    assert de.get_surface(env, env.socket_dir, "s1") == "legacy-surface"
    assert de.get_surface(env, "/other/socket", "s1") is None
    de.set_surface(env, "/a", "s1", "sa")
    de.set_surface(env, "/b", "s1", "sb")
    assert de.get_surface(env, "/a", "s1") == "sa" and de.get_surface(env, "/b", "s1") == "sb"
    assert "surfaces" in json.loads(open(env.config_path).read())
    assert mode(env.surfaces_path) == 0o600
    assert de.find_surface_owner(env, "sa") == ("/a", "s1")
    assert de.find_surface_owner(env, "legacy-surface") == (env.socket_dir, "s1")
    assert de.find_surface_owner(env, "nope") is None
    de.set_surface(env, "/c", "s2", "sa")
    assert de.find_surface_owner(env, "sa") is None


def test_config_roundtrip_keeps_unknown_keys(de, tmp_path):
    env = records_env(de, tmp_path)
    cfg = de.load_config(env)
    assert cfg["session"] is None and cfg["projects"] == {}
    cfg.update(project="/tmp/тест", session="s", surfaces={"s": "x"}, extra=[1])
    de.save_config(env, cfg)
    assert de.load_config(env) == cfg
    assert mode(env.config_path) == 0o600


def test_error_log_is_single_line_and_bounded(de, tmp_path):
    env = records_env(de, tmp_path)
    de.log_error(env, ValueError("first line\nsecond \x1b[31m line" + "x" * 500))
    de.log_error(env, RuntimeError("a\rb\x00c"))
    de.log_error(env, KeyError())
    de.log_error_type_only(env, OSError("secret prompt text"))
    lines = open(env.errors_log).read().split("\n")
    assert lines[-1] == "" and len(lines) == 5
    assert lines[0].split(" ", 1)[1] == "ValueError: first line"
    assert len(lines[0].split(": ", 1)[1]) <= 200
    assert "\x1b" not in lines[1] and "\x00" not in lines[1] and "\r" not in lines[1]
    assert lines[2].split(" ", 1)[1] == "KeyError"
    assert lines[3].split(" ", 1)[1] == "OSError" and "secret" not in lines[3]
    assert mode(env.errors_log) == 0o600


def test_log_error_swallows_failures(de, tmp_path):
    env = records_env(de, tmp_path)
    open(env.state_root, "w").close()  # state_dir cannot be created under a file
    de.log_error(env, ValueError("x"))
    de.log_error_type_only(env, ValueError("x"))


def test_relative_and_empty_inherited_path_entries_are_dropped(de, tmp_path):
    env = make_env(de, tmp_path, None, path="/usr/bin:.::rel/bin:/bin")
    assert env.path_env.split(":") == ["/usr/bin", "/bin"]


def test_relative_manifest_socket_dir_is_ignored(de, tmp_path):
    env = make_env(de, tmp_path, {"zellij_socket_dir": "rel/sock"}, extra={"ZELLIJ_SOCKET_DIR": str(tmp_path / "zs")})
    assert env.socket_dir == str(tmp_path / "zs")
    env = make_env(de, tmp_path, {"zellij_socket_dir": "/abs/sock"})
    assert env.socket_dir == "/abs/sock"


def test_error_log_rotates_at_the_cap_and_keeps_one_generation(de, tmp_path):
    env = records_env(de, tmp_path)
    de.ensure_dir(env.state_dir)
    with open(env.errors_log, "wb") as handle:
        handle.write(b"x" * (de.LOG_CAP + 1))
    de.log_error(env, ValueError("fresh"))
    assert os.path.getsize(env.errors_log + ".1") == de.LOG_CAP + 1
    assert open(env.errors_log).read().count("\n") == 1
    with open(env.errors_log, "wb") as handle:
        handle.write(b"y" * (de.LOG_CAP + 1))
    de.log_error(env, ValueError("again"))
    assert open(env.errors_log + ".1", "rb").read(1) == b"y"
    assert not os.path.exists(env.errors_log + ".2")


def test_symlinked_error_log_and_lock_are_not_followed(de, tmp_path):
    env = records_env(de, tmp_path)
    de.ensure_dir(env.state_dir)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    os.symlink(str(victim), env.errors_log)
    de.log_error(env, ValueError("x"))
    assert victim.read_text() == "keep"
    os.symlink(str(victim), env.monitor_lock)
    with pytest.raises(OSError):
        de.open_lock(env.monitor_lock)
    assert victim.read_text() == "keep"


def test_open_lock_creates_a_private_file(de, tmp_path):
    path = str(tmp_path / "l.lock")
    fd = de.open_lock(path)
    os.close(fd)
    assert mode(path) == 0o600


@pytest.mark.parametrize("name,ok", [("sess", True), ("a b", True), ("-x", False), ("", False), (None, False),
                                     ("a\nb", False), ("a\x00", False), ("x" * 257, False), ("x" * 256, True), (5, False)])
def test_valid_session(de, name, ok):
    assert de.valid_session(name) is ok


def test_remember_project_caps_and_refreshes(de):
    config = {"projects": {}}
    for i in range(205):
        de.remember_project(config, "s%d" % i, "/p%d" % i)
    assert len(config["projects"]) == 200
    assert "s0" not in config["projects"] and "s204" in config["projects"]
    de.remember_project(config, "s5", "/new")
    assert list(config["projects"])[-1] == "s5" and config["projects"]["s5"] == "/new"
    bare = {}
    de.remember_project(bare, "a", "/a")
    assert bare == {"projects": {"a": "/a"}}
