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
