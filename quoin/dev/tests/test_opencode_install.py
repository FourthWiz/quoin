"""The OpenCode installer's tests.

Grown task by task alongside `install.py` (metadata and path-safety
primitives, the planner, apply/check/crash-recovery, uninstall, CLI
wiring and the end-to-end fixture cases). Test function names describe
the property under test, never a plan task, decision or acceptance-
criterion label (see the plan's test-naming rule); those labels stay in
planning artifacts only.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from quoin.opencode_adapter import install

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"


def _owned_record(sha256="a" * 64, source_digest="sha256:deadbeef", kind="skill", id_="plan"):
    return {"sha256": sha256, "source_digest": source_digest, "kind": kind, "id": id_}


def _write_metadata_json(root: Path, obj) -> None:
    quoin_dir = root / ".quoin"
    quoin_dir.mkdir(parents=True, exist_ok=True)
    (quoin_dir / "opencode-install.json").write_text(json.dumps(obj), encoding="utf-8")


def _base_metadata_obj(**overrides):
    obj = {
        "schema_version": 1,
        "quoin_version": "0.1.0",
        "opencode_version": "1.18.32",
        "profile": None,
        "owned": {
            ".opencode/skills/quoin-plan/SKILL.md": _owned_record(),
        },
        "created_dirs": [".opencode/skills/quoin-plan", ".opencode/skills", ".opencode", ".quoin"],
    }
    obj.update(overrides)
    return obj


# --- serialize / load round trip ---


def test_serialize_load_round_trip_preserves_fields():
    meta = install.Metadata(
        quoin_version="0.1.0",
        opencode_version="1.18.32",
        profile="default",
        owned={".opencode/quoin/instructions.md": _owned_record(kind="instructions", id_=None)},
        created_dirs=[".opencode", ".opencode/quoin", ".quoin"],
    )
    data = install.serialize_metadata(meta)
    assert data.endswith(b"\n")
    obj = json.loads(data.decode("utf-8"))
    assert obj["quoin_version"] == "0.1.0"
    assert obj["profile"] == "default"


def test_serialize_metadata_is_byte_identical_across_calls_and_orders_dirs_deepest_first():
    meta = install.Metadata(
        quoin_version="0.1.0",
        opencode_version="1.18.32",
        profile=None,
        owned={},
        created_dirs=[".quoin", ".opencode/skills/quoin-plan", ".opencode/skills", ".opencode"],
    )
    first = install.serialize_metadata(meta)
    second = install.serialize_metadata(meta)
    assert first == second
    obj = json.loads(first.decode("utf-8"))
    assert obj["created_dirs"] == [
        ".opencode/skills/quoin-plan",
        ".opencode/skills",
        ".opencode",
        ".quoin",
    ]


def test_load_metadata_round_trips_a_serialized_file(tmp_path: Path):
    meta = install.Metadata(
        quoin_version="0.1.0",
        opencode_version="1.18.32",
        profile="default",
        owned={".opencode/quoin/instructions.md": _owned_record(kind="instructions", id_=None)},
        created_dirs=[".opencode", ".opencode/quoin", ".quoin"],
    )
    quoin_dir = tmp_path / ".quoin"
    quoin_dir.mkdir()
    (quoin_dir / "opencode-install.json").write_bytes(install.serialize_metadata(meta))
    loaded = install.load_metadata(tmp_path)
    assert loaded.quoin_version == "0.1.0"
    assert loaded.profile == "default"
    assert loaded.owned[".opencode/quoin/instructions.md"]["kind"] == "instructions"
    assert set(loaded.created_dirs) == {".opencode", ".opencode/quoin", ".quoin"}


def test_load_metadata_returns_none_when_quoin_dir_absent(tmp_path: Path):
    assert install.load_metadata(tmp_path) is None


def test_load_metadata_returns_none_when_quoin_dir_exists_but_file_absent(tmp_path: Path):
    (tmp_path / ".quoin").mkdir()
    assert install.load_metadata(tmp_path) is None


# --- load_metadata: invalid metadata raises InstallError(exit_code=2) ---


@pytest.mark.parametrize(
    "mutate",
    [
        lambda obj: obj.pop("profile"),
        lambda obj: obj.__setitem__("schema_version", 2),
        lambda obj: obj.__setitem__("owned", {"../escape.md": _owned_record()}),
        lambda obj: obj.__setitem__("owned", {"src/app.py": _owned_record()}),
        lambda obj: obj.__setitem__("owned", {"/abs/path.md": _owned_record()}),
        lambda obj: obj.__setitem__("created_dirs", ["src"]),
        lambda obj: obj.__setitem__(
            "owned", {".opencode/skills/quoin-plan/SKILL.md": _owned_record(sha256="not-hex")}
        ),
        lambda obj: obj.__setitem__(
            "owned", {".opencode/skills/quoin-plan/SKILL.md": _owned_record(kind="bogus")}
        ),
        lambda obj: obj.__setitem__(
            "owned", {".opencode/skills/quoin-plan/SKILL.md": {"sha256": "a" * 64}}
        ),
        lambda obj: obj.__setitem__("profile", "Not Valid!"),
    ],
)
def test_load_metadata_rejects_invalid_shapes(tmp_path: Path, mutate):
    obj = _base_metadata_obj()
    mutate(obj)
    _write_metadata_json(tmp_path, obj)
    with pytest.raises(install.InstallError) as excinfo:
        install.load_metadata(tmp_path)
    assert excinfo.value.exit_code == 2


def test_load_metadata_rejects_malformed_json(tmp_path: Path):
    quoin_dir = tmp_path / ".quoin"
    quoin_dir.mkdir()
    (quoin_dir / "opencode-install.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(install.InstallError) as excinfo:
        install.load_metadata(tmp_path)
    assert excinfo.value.exit_code == 2


def test_load_metadata_rejects_symlinked_quoin_dir(tmp_path: Path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (tmp_path / ".quoin").symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(install.InstallError):
        install.load_metadata(tmp_path)


def test_load_metadata_rejects_regular_file_named_quoin(tmp_path: Path):
    (tmp_path / ".quoin").write_text("not a directory", encoding="utf-8")
    with pytest.raises(install.InstallError):
        install.load_metadata(tmp_path)


def test_load_metadata_rejects_symlinked_metadata_file(tmp_path: Path):
    quoin_dir = tmp_path / ".quoin"
    quoin_dir.mkdir()
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(_base_metadata_obj()), encoding="utf-8")
    (quoin_dir / "opencode-install.json").symlink_to(real)
    with pytest.raises(install.InstallError):
        install.load_metadata(tmp_path)


def test_load_metadata_rejects_non_regular_metadata_path(tmp_path: Path):
    quoin_dir = tmp_path / ".quoin"
    quoin_dir.mkdir()
    (quoin_dir / "opencode-install.json").mkdir()
    with pytest.raises(install.InstallError):
        install.load_metadata(tmp_path)


# --- inspect_path ---


def test_inspect_path_missing_target(tmp_path: Path):
    assert install.inspect_path(tmp_path, "a/b.md").state == "missing"


def test_inspect_path_regular_target_reads_bytes(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.md").write_bytes(b"hello")
    result = install.inspect_path(tmp_path, "a/b.md")
    assert result.state == "regular"
    assert result.data == b"hello"


def test_inspect_path_directory_target_is_not_regular(tmp_path: Path):
    (tmp_path / "a").mkdir()
    assert install.inspect_path(tmp_path, "a").state == "not-regular"


def test_inspect_path_symlink_target(tmp_path: Path):
    real = tmp_path / "real.md"
    real.write_bytes(b"x")
    (tmp_path / "link.md").symlink_to(real)
    assert install.inspect_path(tmp_path, "link.md").state == "symlink"


def test_inspect_path_symlinked_parent(tmp_path: Path):
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    (tmp_path / "linkdir").symlink_to(real_dir, target_is_directory=True)
    assert install.inspect_path(tmp_path, "linkdir/child.md").state == "symlinked-parent"


def test_inspect_path_parent_is_a_file(tmp_path: Path):
    (tmp_path / "notadir").write_bytes(b"x")
    assert install.inspect_path(tmp_path, "notadir/child.md").state == "parent-not-dir"


# --- atomic_write ---


def test_atomic_write_leaves_no_tmp_file_after_success(tmp_path: Path):
    target = tmp_path / "out.json"
    install.atomic_write(target, b"data")
    assert target.read_bytes() == b"data"
    assert list(tmp_path.glob(".quoin-tmp-*")) == []


def test_atomic_write_leaves_no_tmp_file_after_injected_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "out.json"

    def _boom(*args, **kwargs):
        raise OSError("injected failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        install.atomic_write(target, b"data")
    assert not target.exists()
    assert list(tmp_path.glob(".quoin-tmp-*")) == []


def test_atomic_write_respects_process_umask(tmp_path: Path):
    old_umask = os.umask(0o022)
    try:
        target = tmp_path / "out.json"
        install.atomic_write(target, b"data")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == (0o666 & ~0o022)
    finally:
        os.umask(old_umask)
