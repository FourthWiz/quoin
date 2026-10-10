"""Per-profile storage layout: directories, stamp, project index, launch session."""
from __future__ import annotations

import ast
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from quoin.opencode_adapter import launch_env, paths, profile_dirs
from quoin.opencode_adapter.profile_dirs import (
    LaunchSession, ProfileDirs, ProfileDirUnsafe, ProfileSwitch, StampMismatch,
)

MODULE = Path(profile_dirs.__file__)


@pytest.fixture()
def adapter(tmp_path):
    root = tmp_path / "adapter"
    root.mkdir(mode=0o700)
    return root


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_layout_and_private_modes(adapter):
    dirs = profile_dirs.prepare(adapter, "work", "work")
    assert dirs.root == adapter / "work"
    for sub in profile_dirs.SUBDIRS:
        assert (dirs.root / sub).is_dir()
        assert _mode(dirs.root / sub) == 0o700
    assert _mode(dirs.root) == 0o700
    for lazy in profile_dirs.LAZY_SUBDIRS:
        assert not (dirs.root / lazy).exists()
    assert dirs.npm_cache == dirs.cache / "npm"
    assert dirs.npm_userconfig == dirs.state / "npmrc"
    assert not dirs.npm_userconfig.exists()


def test_data_dir_matches_launch_env_data_dir(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path / "xs")}
    home = tmp_path / "home"
    home.mkdir()
    base = paths.state_dir(env, home)
    dirs = profile_dirs.prepare(base, "work", "work")
    assert dirs.data == launch_env.data_dir(env, home, "work")


def test_stamp_written_once_and_reread(adapter):
    first = profile_dirs.prepare(adapter, "work", "work")
    body = first.stamp.read_bytes()
    assert json.loads(body) == {"created_format": 1, "profile": "work", "classification": "work"}
    assert _mode(first.stamp) == 0o600
    again = profile_dirs.prepare(adapter, "work", "work")
    assert again == first
    assert first.stamp.read_bytes() == body


def test_stamp_mismatch_per_field(adapter):
    profile_dirs.prepare(adapter, "work", "work")
    with pytest.raises(StampMismatch) as err:
        profile_dirs.prepare(adapter, "work", "personal")
    assert err.value.field == "classification"
    stamp = adapter / "work" / "profile.json"
    stamp.write_text(json.dumps({"created_format": 1, "profile": "other", "classification": "work"}))
    with pytest.raises(StampMismatch) as err:
        profile_dirs.prepare(adapter, "work", "work")
    assert err.value.field == "profile"


def _fresh_with_stamp(adapter, writer):
    profile_dirs.prepare(adapter, "work", "work")
    stamp = adapter / "work" / "profile.json"
    stamp.unlink()
    writer(stamp)
    return stamp


@pytest.mark.parametrize(
    "case,reason",
    [
        ("symlink", "stamp-unreadable"),
        ("directory", "stamp-unreadable"),
        ("oversize", "stamp-unreadable"),
        ("badjson", "stamp-invalid"),
        ("extra", "stamp-invalid"),
        ("format", "stamp-invalid"),
        ("array", "stamp-invalid"),
    ],
)
def test_unusable_stamps_are_unsafe(adapter, tmp_path, case, reason):
    good = {"created_format": 1, "profile": "work", "classification": "work"}

    def write(stamp: Path):
        if case == "symlink":
            target = tmp_path / "real.json"
            target.write_text(json.dumps(good))
            stamp.symlink_to(target)
        elif case == "directory":
            stamp.mkdir()
        elif case == "oversize":
            stamp.write_text(" " * 5000 + json.dumps(good))
        elif case == "badjson":
            stamp.write_text("{nope")
        elif case == "extra":
            stamp.write_text(json.dumps(dict(good, more=1)))
        elif case == "format":
            stamp.write_text(json.dumps(dict(good, created_format=2)))
        else:
            stamp.write_text("[]")

    _fresh_with_stamp(adapter, write)
    with pytest.raises(ProfileDirUnsafe) as err:
        profile_dirs.prepare(adapter, "work", "work")
    assert err.value.reason == reason


def test_concurrent_first_prepare_leaves_one_valid_stamp(adapter):
    errors = []

    def run():
        try:
            profile_dirs.prepare(adapter, "work", "work")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads((adapter / "work" / "profile.json").read_text())["profile"] == "work"


def test_group_writable_root_is_not_private(adapter):
    (adapter / "work").mkdir(mode=0o700)
    os.chmod(adapter / "work", 0o770)
    with pytest.raises(ProfileDirUnsafe) as err:
        profile_dirs.prepare(adapter, "work", "work")
    assert err.value.reason == "not-private"


def test_record_project_dedup_realpath_and_lock_mode(adapter, tmp_path):
    dirs = profile_dirs.prepare(adapter, "work", "work")
    real = tmp_path / "proj"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    other = tmp_path / "a-proj"
    other.mkdir()
    profile_dirs.record_project(dirs, link)
    profile_dirs.record_project(dirs, real)
    profile_dirs.record_project(dirs, other)
    data = json.loads(dirs.index.read_text())
    assert data["index_format"] == 1
    assert data["projects"] == sorted({os.path.realpath(real), os.path.realpath(other)})
    assert _mode(dirs.root / "projects.json.lock") == 0o600


def test_invalid_index_fails_closed(adapter, tmp_path):
    dirs = profile_dirs.prepare(adapter, "work", "work")
    dirs.index.write_text("{bad")
    with pytest.raises(ProfileDirUnsafe) as err:
        profile_dirs.record_project(dirs, tmp_path)
    assert err.value.reason == "index-unwritable"
    assert dirs.index.read_text() == "{bad"


@pytest.mark.parametrize("name", ["a/b", "..", "Work", "", "a b"])
def test_profile_root_rejects_unsafe_names(adapter, name):
    with pytest.raises(ValueError):
        profile_dirs.profile_root(adapter, name)


def test_adapter_root_honours_only_a_stamped_launcher_root(adapter, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dirs = profile_dirs.prepare(adapter, "work", "work")
    plain = {"XDG_STATE_HOME": str(tmp_path / "xs")}
    ordinary = paths.state_dir(plain, home)
    assert profile_dirs.adapter_root(dict(plain, **{paths.ENV_STATE_DIR: str(dirs.root)}), home) == adapter
    unstamped = adapter / "bare"
    unstamped.mkdir()
    assert profile_dirs.adapter_root(dict(plain, **{paths.ENV_STATE_DIR: str(unstamped)}), home) == ordinary
    assert profile_dirs.adapter_root(dict(plain, **{paths.ENV_STATE_DIR: "relative/work"}), home) == ordinary
    mismatched = adapter / "other"
    mismatched.mkdir()
    (mismatched / "profile.json").write_text(
        json.dumps({"created_format": 1, "profile": "work", "classification": "work"})
    )
    assert profile_dirs.adapter_root(dict(plain, **{paths.ENV_STATE_DIR: str(mismatched)}), home) == ordinary
    assert profile_dirs.adapter_root(plain, home) == ordinary
    assert not ordinary.exists()


def test_launch_session_matrix():
    session = LaunchSession()
    session.check_profile("anything")
    session.claim("work", "work")
    session.claim("work", "work")
    session.check_profile("work")
    with pytest.raises(ProfileSwitch) as err:
        session.check_profile("personal")
    assert err.value.field == "profile"
    with pytest.raises(ProfileSwitch) as err:
        session.claim("personal", "work")
    assert err.value.field == "profile"
    with pytest.raises(ProfileSwitch) as err:
        session.claim("work", "personal")
    assert err.value.field == "classification"
    names = [n for n in dir(LaunchSession) if not n.startswith("_")]
    assert not [n for n in names if any(w in n for w in ("reset", "clear", "release"))]


def test_launch_session_seeded_from_a_stamped_launcher_env(adapter):
    dirs = profile_dirs.prepare(adapter, "work", "work")
    session = LaunchSession.from_launcher_env({paths.ENV_STATE_DIR: str(dirs.root)})
    with pytest.raises(ProfileSwitch):
        session.claim("personal", "personal")
    session.claim("work", "work")
    assert LaunchSession.from_launcher_env({}).pair is None
    assert LaunchSession.from_launcher_env({paths.ENV_STATE_DIR: str(adapter / "nope")}).pair is None


def test_state_root_for_with_and_without_the_method(adapter):
    class Plain:
        state_root = adapter

    class Scripted:
        state_root = adapter

        def profile_state_root(self, profile):
            return adapter / "elsewhere" / profile

    class Bare:
        state_root = None

    assert profile_dirs.state_root_for(Plain(), "work") == adapter / "work"
    assert profile_dirs.state_root_for(Scripted(), "work") == adapter / "elsewhere" / "work"
    assert profile_dirs.state_root_for(Bare(), "work") is None


def test_compiled_dir_matches_paths_for_a_plain_env_and_follows_a_launcher_root(adapter, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    env = {"XDG_STATE_HOME": str(tmp_path / "xs")}
    base = paths.state_dir(env, home)
    assert profile_dirs.compiled_dir(base, "work", project) == paths.compiled_output_dir("work", project, env, home)
    dirs = profile_dirs.prepare(adapter, "work", "work")
    nested = {"XDG_STATE_HOME": str(dirs.state), paths.ENV_STATE_DIR: str(dirs.root)}
    got = profile_dirs.compiled_dir(profile_dirs.adapter_root(nested, home), "work", project)
    assert got.parent.parent == adapter
    assert dirs.state not in got.parents


def test_profile_dirs_at_creates_nothing_and_matches_prepare(adapter):
    ghost = adapter / "ghost"
    at = ProfileDirs.at(ghost)
    assert not ghost.exists()
    prepared = profile_dirs.prepare(adapter, "work", "work")
    assert ProfileDirs.at(adapter / "work") == prepared


def test_child_storage_env_values(adapter):
    dirs = ProfileDirs.at(adapter / "work")
    assert profile_dirs.child_storage_env(dirs) == {
        "XDG_STATE_HOME": str(dirs.state),
        "XDG_CACHE_HOME": str(dirs.cache),
        "TMPDIR": str(dirs.tmp),
        "npm_config_cache": str(dirs.cache / "npm"),
    }


def test_module_hygiene():
    source = MODULE.read_text()
    tree = ast.parse(source, feature_version=(3, 10))
    assert isinstance(tree.body[1], ast.ImportFrom) and tree.body[1].module == "__future__"
    assert "os.environ" not in source
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "LaunchRefused" not in [a.name for a in node.names]
    assert "LaunchRefused" not in source


def test_a_leftover_staging_file_from_the_old_scheme_does_not_block(adapter):
    root = adapter / "work"
    root.mkdir(mode=0o700)
    pid, tid = os.getpid(), threading.get_ident()
    (root / ("profile.json.%d.%d.tmp" % (pid, tid))).write_text("junk")
    profile_dirs.prepare(adapter, "work", "work")
    assert json.loads((root / "profile.json").read_text())["profile"] == "work"


def test_an_existing_stamp_creates_no_staging_file(adapter):
    profile_dirs.prepare(adapter, "work", "work")
    before = sorted(os.listdir(adapter / "work"))
    profile_dirs.prepare(adapter, "work", "work")
    assert sorted(os.listdir(adapter / "work")) == before


def test_a_filesystem_without_hard_links_still_gets_a_valid_stamp(adapter, monkeypatch):
    import errno

    def refuse_link(src, dst, *a, **kw):
        raise OSError(errno.EPERM, "not permitted")

    monkeypatch.setattr(os, "link", refuse_link)
    dirs = profile_dirs.prepare(adapter, "work", "work")
    assert json.loads(dirs.stamp.read_text()) == {
        "created_format": 1, "profile": "work", "classification": "work",
    }
    assert [n for n in os.listdir(dirs.root) if n.endswith(".tmp")] == []


def test_a_fresh_truncated_stamp_is_given_a_moment_to_finish(adapter):
    root = adapter / "work"
    root.mkdir(mode=0o700)
    stamp = root / "profile.json"
    full = json.dumps({"created_format": 1, "profile": "work", "classification": "work"})
    stamp.write_text(full[:10])

    def finish():
        import time
        time.sleep(0.03)
        stamp.write_text(full)

    timer = threading.Thread(target=finish)
    timer.start()
    try:
        profile_dirs.prepare(adapter, "work", "work")
    finally:
        timer.join()


def test_an_old_truncated_stamp_is_refused_without_waiting(adapter):
    root = adapter / "work"
    root.mkdir(mode=0o700)
    stamp = root / "profile.json"
    stamp.write_text("{")
    os.utime(stamp, (1, 1))
    with pytest.raises(ProfileDirUnsafe) as err:
        profile_dirs.prepare(adapter, "work", "work")
    assert err.value.reason == "stamp-invalid"


@pytest.mark.parametrize("name", profile_dirs.RESERVED_NAMES)
def test_reserved_names_are_refused_and_create_nothing(adapter, name):
    with pytest.raises(ValueError):
        profile_dirs.profile_root(adapter, name)
    with pytest.raises(ProfileDirUnsafe) as err:
        profile_dirs.prepare(adapter, name, "work")
    assert err.value.reason == "reserved-name"
    assert os.listdir(adapter) == []


def test_recording_a_known_project_again_leaves_the_index_untouched(adapter, tmp_path):
    dirs = profile_dirs.prepare(adapter, "work", "work")
    project = tmp_path / "proj"
    project.mkdir()
    profile_dirs.record_project(dirs, project)
    os.utime(dirs.index, (1, 1))
    profile_dirs.record_project(dirs, project)
    assert os.stat(dirs.index).st_mtime == 1
