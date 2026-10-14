"""Refuse-malformed reads, atomic compare-and-swap writes, reference backups."""
import datetime
import json
import os

import pytest

from quoin.streamdeck import jsonfile


def temps(tmp_path):
    return [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("content", ["", "   \n", "{not json", "[1, 2]", '"s"'])
def test_malformed_is_refused_and_untouched(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with pytest.raises(jsonfile.MalformedJSON) as exc:
        jsonfile.update(path, lambda d: dict(d, a=1))
    assert str(path) in str(exc.value) and "unchanged" in str(exc.value)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not temps(tmp_path)


def test_absent_file_is_created_0600(tmp_path):
    path = tmp_path / "new.json"
    changed, result = jsonfile.update(path, lambda d: dict(d, a=1))
    assert changed and result == {"a": 1}
    assert json.loads(path.read_text()) == {"a": 1}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text().endswith("\n")


def test_existing_mode_is_kept(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{}")
    path.chmod(0o644)
    jsonfile.update(path, lambda d: dict(d, a=1))
    assert oct(path.stat().st_mode & 0o777) == "0o644"


def test_symlinked_file_stays_a_link(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"x": 1}')
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    jsonfile.update(link, lambda d: dict(d, y=2))
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"x": 1, "y": 2}


def test_no_change_writes_nothing(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"a":1}')
    before = os.stat(path)
    changed, result = jsonfile.update(path, lambda d: d)
    assert changed is False and result == {"a": 1}
    after = os.stat(path)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not temps(tmp_path)


def test_concurrent_edit_is_remerged(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"a": 1}')
    seen = {"calls": 0, "writes": 0}

    def foreign_write():
        seen["calls"] += 1
        if seen["writes"] == 0:
            seen["writes"] = 1
            path.write_text('{"a": 1, "foreign": true}')

    def mutate(data):
        return dict(data, ours=True)

    data, raw = jsonfile.read(path)
    with pytest.raises(jsonfile._Conflict):
        jsonfile.write_atomic(path, mutate(data), expected_raw=raw, _before_replace=foreign_write)
    assert not temps(tmp_path)
    path.write_text('{"a": 1}')
    seen.update(calls=0, writes=0)
    changed, result = jsonfile.update(path, mutate, _before_replace=foreign_write)
    assert changed and result == {"a": 1, "foreign": True, "ours": True}
    assert json.loads(path.read_text()) == result
    assert seen["calls"] == 2


def test_always_conflicting_gives_up_and_keeps_the_other_writers_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{}")
    n = {"i": 0}

    def foreign_write():
        n["i"] += 1
        path.write_text(json.dumps({"foreign": n["i"]}))

    with pytest.raises(jsonfile.ConcurrentModification):
        jsonfile.update(path, lambda d: dict(d, ours=1), _before_replace=foreign_write)
    assert n["i"] == 3
    assert json.loads(path.read_text()) == {"foreign": 3}
    assert not temps(tmp_path)


def test_exception_in_mutate_leaves_file_alone(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"a": 1}')
    before = path.read_bytes()

    def boom(data):
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        jsonfile.update(path, boom)
    assert path.read_bytes() == before and not temps(tmp_path)


def test_backup_prunes_only_our_directories(tmp_path):
    src = tmp_path / "s.json"
    src.write_text("{}")
    root = tmp_path / "backups"
    root.mkdir()
    (root / "20261013-prototype").mkdir()
    for i in range(7):
        jsonfile.backup(src, root, now=datetime.datetime(2026, 10, 13, 12, 0, i, tzinfo=datetime.timezone.utc))
    ours = sorted(p.name for p in root.iterdir() if p.name.startswith("quoin-"))
    assert len(ours) == 5 and ours[0].endswith("120002Z") and ours[-1].endswith("120006Z")
    assert (root / "20261013-prototype").is_dir()
    assert (root / ours[0] / "s.json").read_text() == "{}"
    assert jsonfile.backup(tmp_path / "missing.json", root, now=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)) is None


def test_backups_of_one_run_share_a_folder_and_a_clash_gets_a_numbered_one(tmp_path):
    a, b = tmp_path / "a" / "settings.json", tmp_path / "b" / "hooks.json"
    for path, text in ((a, '{"a": 1}'), (b, '{"b": 1}')):
        path.parent.mkdir()
        path.write_text(text)
    root = tmp_path / "backups"
    root.mkdir()
    now = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
    first = jsonfile.backup(a, root, now=now)
    second = jsonfile.backup(b, root, now=now)
    assert os.path.dirname(first) == os.path.dirname(second)
    a.write_text('{"a": 2}')
    third = jsonfile.backup(a, root, now=now)
    assert os.path.basename(os.path.dirname(third)).endswith("120000Z-02")
    assert json.loads(open(first).read()) == {"a": 1} and json.loads(open(third).read()) == {"a": 2}


def test_same_second_backups_prune_by_creation_order(tmp_path):
    src = tmp_path / "s.json"
    root = tmp_path / "backups"
    root.mkdir()
    now = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
    for i in range(12):
        src.write_text('{"n": %d}' % i)
        jsonfile.backup(src, root, now=now)
    kept = sorted(p.name for p in root.iterdir())
    assert len(kept) == 5
    contents = [json.loads((root / name / "s.json").read_text())["n"] for name in kept]
    assert contents == [7, 8, 9, 10, 11]
