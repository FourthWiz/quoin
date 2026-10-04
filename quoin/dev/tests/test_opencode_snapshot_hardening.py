"""Snapshot copier hardening: a listed directory swapped for a symlink before
its file is copied, and a silent git that never answers."""
from __future__ import annotations

import os
import stat
import time

import pytest

from quoin.opencode_adapter import snapshot

pytestmark = pytest.mark.skipif(os.name != "posix", reason="symlinks and process groups are POSIX-only")


def builder(root, dest):
    dest.mkdir(parents=True, exist_ok=True)
    return snapshot._Builder(str(root), str(dest), 1 << 20, 100)  # noqa: SLF001


def test_a_directory_swapped_for_a_symlink_after_listing_is_skipped(tmp_path):
    root = tmp_path / "project"
    outside = tmp_path / "outside"
    (root / "src").mkdir(parents=True)
    outside.mkdir()
    (root / "src" / "a.py").write_text("inside\n")
    (outside / "a.py").write_text("OUTSIDE\n")
    b = builder(root, tmp_path / "dest")
    source = str(root / "src" / "a.py")
    # the directory was listed as a plain directory, then replaced by a link
    (root / "src" / "a.py").unlink()
    (root / "src").rmdir()
    os.symlink(str(outside), str(root / "src"))
    b.copy("src/a.py", source)
    assert b.skipped == [{"path": "src/a.py", "reason": "symlink-parent"}]
    assert b.manifest == {}
    assert not (tmp_path / "dest" / "src").exists()


def test_a_plain_nested_file_is_still_copied(tmp_path):
    root = tmp_path / "project"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "c.txt").write_text("x\n")
    b = builder(root, tmp_path / "dest")
    b.copy("a/b/c.txt", str(root / "a" / "b" / "c.txt"))
    assert "a/b/c.txt" in b.manifest and b.skipped == []


def test_a_silent_git_is_killed_at_the_deadline(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "git"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(snapshot, "GIT_TIMEOUT_S", 0.5)
    started = time.monotonic()
    code, data, truncated = snapshot._git(str(tmp_path), "status", cap=1024)  # noqa: SLF001
    assert time.monotonic() - started < 10
    assert truncated is True and data == b""
