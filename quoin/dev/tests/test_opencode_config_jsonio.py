"""Tests for strict JSON loading, canonical dumping and private atomic writes."""
from __future__ import annotations

import os
import stat

import pytest

from quoin.opencode_adapter import jsonio
from quoin.opencode_adapter.errors import ConfigErrors


def load(tmp_path, data, **kw):
    path = tmp_path / "f.json"
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return jsonio.load_strict(path, file_label="f.json", **kw)


def err_of(tmp_path, data, **kw):
    with pytest.raises(ConfigErrors) as info:
        load(tmp_path, data, **kw)
    return info.value


def test_valid_document_round_trips_to_plain_types(tmp_path):
    tree = load(tmp_path, '{"a": {"b": [1, 2, {"c": null}]}}')
    assert tree == {"a": {"b": [1, 2, {"c": None}]}}
    assert type(tree) is dict and type(tree["a"]) is dict


@pytest.mark.parametrize(
    "text,path",
    [
        ('{"a": 1, "a": 2}', "$.a"),
        ('{"providers": {"corp-gw": {"x": 1, "x": 2}}}', "$.providers.corp-gw.x"),
        ('{"list": [{"k": 1, "k": 2}]}', "$.list[0].k"),
    ],
)
def test_duplicate_keys(tmp_path, text, path):
    exc = err_of(tmp_path, text)
    assert [e.rejection_class for e in exc.errors] == ["duplicate-key"]
    assert exc.errors[0].json_path == path


def test_all_duplicates_reported(tmp_path):
    exc = err_of(tmp_path, '{"a": 1, "a": 2, "b": {"c": 1, "c": 2}}')
    assert {e.json_path for e in exc.errors} == {"$.a", "$.b.c"}


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers(tmp_path, literal):
    exc = err_of(tmp_path, '{"a": %s}' % literal)
    assert exc.errors[0].rejection_class == "invalid-json"
    assert exc.errors[0].message_id == "non-finite-number"


@pytest.mark.parametrize("literal", ["1e400", "-1e400", "1E999", "[1e400]"])
def test_overflowing_exponents_rejected_at_load(tmp_path, literal):
    text = literal if literal.startswith("[") else '{"a": %s}' % literal
    exc = err_of(tmp_path, text)
    assert exc.errors[0].rejection_class == "invalid-json"
    assert exc.errors[0].message_id == "non-finite-number"


def test_huge_integers_are_classified_as_too_large(tmp_path):
    for digits in (33, 5000):
        exc = err_of(tmp_path, '{"a": %s}' % ("9" * digits))
        assert exc.errors[0].rejection_class == "invalid-json"
        assert exc.errors[0].message_id == "number-too-large"
        assert "9999" not in str(exc)
    exc = err_of(tmp_path, '{"a": -%s}' % ("9" * 33))
    assert exc.errors[0].message_id == "number-too-large"


def test_integer_at_the_digit_limit_loads_and_sign_is_not_counted(tmp_path):
    assert load(tmp_path, '{"a": %s}' % ("9" * 32))["a"] == int("9" * 32)
    assert load(tmp_path, '{"a": -%s}' % ("9" * 32))["a"] == -int("9" * 32)


def test_load_strict_with_stat_returns_the_stat_of_the_read_descriptor(tmp_path):
    path = tmp_path / "f.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    tree, info = jsonio.load_strict_with_stat(path, file_label="f.json")
    assert tree == {"a": 1}
    assert info.st_size == path.stat().st_size
    assert info.st_uid == os.getuid()
    assert stat.S_ISREG(info.st_mode)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
def test_load_strict_with_stat_refuses_a_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ConfigErrors) as info:
        jsonio.load_strict_with_stat(link, file_label="link.json")
    assert info.value.errors[0].message_id == "unreadable-file"


def test_ordinary_floats_still_load(tmp_path):
    assert load(tmp_path, '{"a": 1.5, "b": 1e3}') == {"a": 1.5, "b": 1000.0}


def test_fifo_is_not_opened_blocking(tmp_path):
    fifo = tmp_path / "f.json"
    os.mkfifo(fifo)
    with pytest.raises(ConfigErrors) as info:
        jsonio.load_strict(fifo, file_label="f.json")
    assert info.value.errors[0].message_id == "unreadable-file"


def test_bom_rejected(tmp_path):
    exc = err_of(tmp_path, b"\xef\xbb\xbf{}")
    assert exc.errors[0].message_id == "has-bom"


def test_invalid_utf8_rejected(tmp_path):
    exc = err_of(tmp_path, b'{"a": "\xff\xfe"}')
    assert exc.errors[0].message_id == "not-utf8"


def test_truncated_reports_position_without_content(tmp_path):
    exc = err_of(tmp_path, '{"secret_looking_word": "value-here"\n')
    err = exc.errors[0]
    assert err.message_id == "malformed-json"
    assert dict(err.params)["line"] >= 1
    assert "secret_looking_word" not in str(exc)
    assert "value-here" not in str(exc)


def test_oversize_file(tmp_path):
    exc = err_of(tmp_path, '{"a": "%s"}' % ("x" * 200), max_bytes=100)
    assert exc.errors[0].message_id == "file-too-large"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigErrors) as info:
        jsonio.load_strict(tmp_path / "absent.json", file_label="absent.json")
    assert info.value.errors[0].message_id == "unreadable-file"


def test_very_deep_nesting_is_classified_not_a_crash(tmp_path):
    exc = err_of(tmp_path, "[" * 100000)
    assert exc.errors[0].rejection_class == "invalid-json"


def _nested(levels):
    return '{"a":' * (levels - 1) + "{}" + "}" * (levels - 1)


def test_depth_limit(tmp_path):
    assert load(tmp_path, _nested(64))
    exc = err_of(tmp_path, _nested(65))
    assert exc.errors[0].message_id == "nesting-too-deep"


def test_canonical_dump():
    assert jsonio.dump_canonical({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == b'{"a":[1,{"c":3,"d":2}],"b":1}'
    assert jsonio.dump_canonical({"k": "café"}) == '{"k":"café"}'.encode("utf-8")
    with pytest.raises(ValueError):
        jsonio.dump_canonical({"a": float("nan")})


@pytest.mark.parametrize("mask", [0, 0o077])
def test_atomic_write_modes_are_umask_proof(tmp_path, mask):
    old = os.umask(mask)
    try:
        target = tmp_path / "a" / "b" / "file.json"
        jsonio.write_private_atomic(target, b"{}")
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700
    assert target.read_bytes() == b"{}"
    assert [p.name for p in target.parent.iterdir()] == ["file.json"]


def test_refuses_world_writable_non_sticky_parent(tmp_path):
    parent = tmp_path / "open"
    parent.mkdir()
    os.chmod(parent, 0o777)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio.write_private_atomic(parent / "sub" / "f.json", b"{}")
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio.write_private_atomic(parent / "f.json", b"{}")


def test_accepts_group_writable_and_sticky_parents(tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    os.chmod(group, 0o775)
    jsonio.write_private_atomic(group / "f.json", b"1")
    sticky = tmp_path / "sticky"
    sticky.mkdir()
    os.chmod(sticky, 0o1777)
    jsonio.write_private_atomic(sticky / "f.json", b"1")
    assert (sticky / "f.json").read_bytes() == b"1"


def test_temp_removed_when_replace_fails(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(jsonio.os, "replace", boom)
    with pytest.raises(OSError):
        jsonio.write_private_atomic(tmp_path / "f.json", b"{}")
    assert list(tmp_path.iterdir()) == []


def test_existing_target_is_replaced(tmp_path):
    target = tmp_path / "f.json"
    target.write_bytes(b"old")
    jsonio.write_private_atomic(target, b"new")
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["f.json"]


# ------------------------------------------------ private parent and reads


@pytest.fixture
def umask0():
    old = os.umask(0)
    yield
    os.umask(old)


def _dir(path, mode):
    path.mkdir()
    os.chmod(path, mode)
    return path


def test_private_parent_accepts_a_closed_directory(tmp_path):
    parent = _dir(tmp_path / "out", 0o700)
    jsonio.write_private_atomic(parent / "f.json", b"x", private_parent=True)
    assert stat.S_IMODE((parent / "f.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o770, 0o777, 0o1777, 0o720])
def test_private_parent_refuses_a_loose_existing_directory(tmp_path, mode):
    parent = _dir(tmp_path / "out", mode)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio.write_private_atomic(parent / "f.json", b"x", private_parent=True)
    assert list(parent.iterdir()) == []
    if mode & 0o002 and not mode & stat.S_ISVTX:
        return  # the default rule already refuses a non-sticky world-writable directory
    jsonio.write_private_atomic(parent / "g.json", b"x")
    assert (parent / "g.json").read_bytes() == b"x"


def test_created_directories_stay_private_under_a_zero_umask(tmp_path, umask0):
    target = tmp_path / "a" / "b" / "f.json"
    jsonio.write_private_atomic(target, b"x", private_parent=True)
    for directory in (tmp_path / "a", tmp_path / "a" / "b"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "mode,ok", [(0o770, False), (0o1777, True), (0o777, False), (0o700, True)]
)
def test_private_parent_checks_the_nearest_existing_ancestor(tmp_path, mode, ok):
    ancestor = _dir(tmp_path / "base", mode)
    target = ancestor / "new" / "f.json"
    if ok:
        jsonio.write_private_atomic(target, b"x", private_parent=True)
        assert stat.S_IMODE((ancestor / "new").stat().st_mode) == 0o700
    else:
        with pytest.raises(jsonio.UnsafeDirectoryError):
            jsonio.write_private_atomic(target, b"x", private_parent=True)
        assert not (ancestor / "new").exists()


def test_write_forces_a_private_file_mode_over_an_existing_file(tmp_path):
    target = tmp_path / "f.json"
    target.write_bytes(b"old")
    os.chmod(target, 0o644)
    jsonio.write_private_atomic(target, b"new", private_parent=True)
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_read_regular_bytes(tmp_path):
    good = tmp_path / "f"
    good.write_bytes(b"abc")
    raw, info = jsonio.read_regular_bytes(good, max_bytes=10)
    assert raw == b"abc" and stat.S_ISREG(info.st_mode)
    assert jsonio.read_regular_bytes(tmp_path / "missing", max_bytes=10) is None
    (tmp_path / "link").symlink_to(good)
    assert jsonio.read_regular_bytes(tmp_path / "link", max_bytes=10) is None
    assert jsonio.read_regular_bytes(tmp_path, max_bytes=10) is None
    os.mkfifo(tmp_path / "fifo")
    assert jsonio.read_regular_bytes(tmp_path / "fifo", max_bytes=10) is None
    assert jsonio.read_regular_bytes(good, max_bytes=2) is None


# ----------------------------------------------- exact parent and ancestors


def _stat_result(uid, mode):
    return os.stat_result((mode, 1, 1, 1, uid, 0, 0, 0, 0, 0))


@pytest.mark.parametrize("mode,ok", [(0o700, True), (0o755, False), (0o750, False), (0o705, False)])
def test_private_parent_requires_an_exactly_private_existing_directory(tmp_path, mode, ok):
    parent = _dir(tmp_path / "out", mode)
    if ok:
        jsonio.write_private_atomic(parent / "f.json", b"x", private_parent=True)
        assert (parent / "f.json").read_bytes() == b"x"
    else:
        with pytest.raises(jsonio.UnsafeDirectoryError, match="private"):
            jsonio.write_private_atomic(parent / "f.json", b"x", private_parent=True)
        assert list(parent.iterdir()) == []


@pytest.mark.parametrize("mode,ok", [(0o777, False), (0o1777, True), (0o770, False), (0o755, True)])
def test_private_parent_checks_every_ancestor(tmp_path, mode, ok):
    top = _dir(tmp_path / "top", mode)
    target = top / "mid" / "leaf" / "f.json"
    if ok:
        jsonio.write_private_atomic(target, b"x", private_parent=True)
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    else:
        with pytest.raises(jsonio.UnsafeDirectoryError):
            jsonio.write_private_atomic(target, b"x", private_parent=True)
        assert not (top / "mid").exists()


def test_private_parent_refuses_a_loose_ancestor_above_an_existing_directory(tmp_path):
    top = _dir(tmp_path / "top", 0o777)
    inner = top / "inner"
    inner.mkdir(mode=0o700)
    os.chmod(inner, 0o700)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio.write_private_atomic(inner / "f.json", b"x", private_parent=True)
    os.chmod(top, 0o755)
    jsonio.write_private_atomic(inner / "f.json", b"x", private_parent=True)


def test_check_ancestor_accepts_a_root_owned_sticky_directory_only_when_asked(tmp_path, monkeypatch):
    def fake(uid, mode):
        monkeypatch.setattr(jsonio.os, "stat", lambda path, *a, **k: _stat_result(uid, mode))

    fake(0, 0o41777)
    jsonio._check_ancestor(tmp_path, allow_root_sticky=True)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio._check_ancestor(tmp_path)
    fake(0, 0o40777)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio._check_ancestor(tmp_path, allow_root_sticky=True)
    fake(os.getuid() + 1, 0o41777)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio._check_ancestor(tmp_path, allow_root_sticky=True)


def _shared_tmp_is_root_sticky():
    try:
        info = os.stat("/tmp")
    except OSError:
        return False
    return info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)


@pytest.mark.skipif(not _shared_tmp_is_root_sticky(), reason="/tmp is not a root-owned sticky directory")
def test_output_below_the_shared_temporary_directory_is_accepted():
    import shutil
    import secrets

    base = "/tmp/quoin-t-" + secrets.token_hex(6)
    try:
        jsonio.write_private_atomic(base + "/x", b"1", private_parent=True)
        assert stat.S_IMODE(os.stat(base).st_mode) == 0o700
        assert open(base + "/x", "rb").read() == b"1"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_ensure_private_directory_creates_a_private_tree(tmp_path, umask0):
    target = tmp_path / "a" / "b"
    jsonio.ensure_private_directory(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    jsonio.ensure_private_directory(target)  # an existing private directory is fine


def test_ensure_private_directory_refuses_a_loose_existing_directory(tmp_path):
    loose = _dir(tmp_path / "loose", 0o777)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio.ensure_private_directory(loose)
    group = _dir(tmp_path / "group", 0o770)
    with pytest.raises(jsonio.UnsafeDirectoryError):
        jsonio.ensure_private_directory(group)
    readable = _dir(tmp_path / "readable", 0o755)
    jsonio.ensure_private_directory(readable)  # only writes matter here
    assert stat.S_IMODE(readable.stat().st_mode) == 0o755


def test_default_writes_keep_their_previous_rules(tmp_path):
    group = _dir(tmp_path / "g", 0o775)
    jsonio.write_private_atomic(group / "f.json", b"1")
    assert (group / "f.json").read_bytes() == b"1"
