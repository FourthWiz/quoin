"""settings.json backups written by deploy_hooks(): when they appear and how
many are kept.

The installer backs up a valid settings.json only when the merge changes it,
saves an unparseable one as ``settings.json.broken-<UTC timestamp>`` before
starting fresh (never pruned, since it may be the only copy of the user's
settings), and keeps only the newest N backups that match its own
``settings.json.bak-<UTC timestamp>`` naming (default 5, overridable with
QUOIN_SETTINGS_BACKUP_KEEP).
"""
import json
from pathlib import Path

import pytest

from quoin.installer import deploy_hooks

REPO_ROOT = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO_ROOT / "quoin"


def _backups(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.glob("settings.json.bak-*"))


def _seed_old_backups(dest: Path, count: int) -> list[str]:
    names = [f"settings.json.bak-202605{day:02d}T120000Z" for day in range(1, count + 1)]
    for name in names:
        (dest / name).write_text("{}\n")
    return names


@pytest.fixture
def dest(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOIN_SETTINGS_BACKUP_KEEP", raising=False)
    d = tmp_path / ".claude"
    d.mkdir()
    return d


def test_fresh_install_writes_no_backup(dest):
    deploy_hooks(QUOIN_SRC, dest)
    assert _backups(dest) == []


def test_changed_settings_are_backed_up_once(dest):
    original = json.dumps({"permissions": {"allow": ["Bash(git:*)"]}})
    (dest / "settings.json").write_text(original)
    deploy_hooks(QUOIN_SRC, dest)
    backups = _backups(dest)
    assert len(backups) == 1
    assert (dest / backups[0]).read_text() == original


def test_idempotent_reinstall_adds_no_backup(dest):
    deploy_hooks(QUOIN_SRC, dest)
    deploy_hooks(QUOIN_SRC, dest)
    assert _backups(dest) == []


def _broken(dest: Path) -> list[str]:
    return sorted(p.name for p in dest.glob("settings.json.broken-*"))


def test_corrupt_settings_saved_as_broken_copy(dest):
    (dest / "settings.json").write_text("{not json")
    deploy_hooks(QUOIN_SRC, dest)
    assert _backups(dest) == []
    broken = _broken(dest)
    assert len(broken) == 1
    assert (dest / broken[0]).read_text() == "{not json"


def test_broken_copy_survives_many_changing_installs(dest):
    (dest / "settings.json").write_text("{not json")
    deploy_hooks(QUOIN_SRC, dest)
    broken = _broken(dest)
    assert len(broken) == 1
    # Each install below changes settings.json, so each writes a .bak- copy
    # and prunes; seeding older .bak- files forces pruning past the default 5.
    _seed_old_backups(dest, 5)
    for i in range(7):
        settings = json.loads((dest / "settings.json").read_text())
        settings["marker"] = i
        (dest / "settings.json").write_text(json.dumps(settings))
        deploy_hooks(QUOIN_SRC, dest)
    assert len(_backups(dest)) == 5
    assert _broken(dest) == broken
    assert (dest / broken[0]).read_text() == "{not json"


def test_prunes_to_default_five_keeping_newest(dest):
    old = _seed_old_backups(dest, 8)
    (dest / "settings.json").write_text("{}")
    deploy_hooks(QUOIN_SRC, dest)
    backups = _backups(dest)
    assert len(backups) == 5
    # The backup from this run (current year, newest) survives with the 4
    # newest seeded ones; the 4 oldest seeded ones are gone.
    assert set(old[-4:]) <= set(backups)
    assert not set(old[:4]) & set(backups)


def test_prunes_even_when_nothing_changes(dest):
    deploy_hooks(QUOIN_SRC, dest)
    _seed_old_backups(dest, 7)
    deploy_hooks(QUOIN_SRC, dest)
    assert len(_backups(dest)) == 5


def test_env_override(dest, monkeypatch):
    monkeypatch.setenv("QUOIN_SETTINGS_BACKUP_KEEP", "2")
    old = _seed_old_backups(dest, 4)
    deploy_hooks(QUOIN_SRC, dest)
    assert _backups(dest) == old[-2:]


@pytest.mark.parametrize("value", ["0", "-3"])
def test_env_override_never_below_one(dest, monkeypatch, value):
    monkeypatch.setenv("QUOIN_SETTINGS_BACKUP_KEEP", value)
    old = _seed_old_backups(dest, 3)
    deploy_hooks(QUOIN_SRC, dest)
    assert _backups(dest) == old[-1:]


def test_invalid_env_falls_back_to_default(dest, monkeypatch):
    monkeypatch.setenv("QUOIN_SETTINGS_BACKUP_KEEP", "lots")
    _seed_old_backups(dest, 9)
    deploy_hooks(QUOIN_SRC, dest)
    assert len(_backups(dest)) == 5


def test_foreign_backup_names_are_never_pruned(dest, monkeypatch):
    monkeypatch.setenv("QUOIN_SETTINGS_BACKUP_KEEP", "1")
    foreign = [
        "settings.json.bak-smoke",
        "settings.json.bak-20260501T120000Z.keep",
        "settings.json.pre-deny-rmrf",
        "other.json.bak-20260501T120000Z",
    ]
    for name in foreign:
        (dest / name).write_text("{}\n")
    _seed_old_backups(dest, 3)
    deploy_hooks(QUOIN_SRC, dest)
    for name in foreign:
        assert (dest / name).exists(), name
    installer_backups = [
        n for n in _backups(dest)
        if n.startswith("settings.json.bak-2") and len(n) == len("settings.json.bak-20260501T120000Z")
    ]
    assert len(installer_backups) == 1
