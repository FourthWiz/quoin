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

import io

from quoin.opencode_adapter import generate, install

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "quoin"


def _rendered(relpath, content, kind="skill", source_id="plan", source_digest="sha256:abc"):
    return generate.RenderedFile(
        relpath=relpath,
        content=content if isinstance(content, bytes) else content.encode("utf-8"),
        kind=kind,
        source_id=source_id,
        source_digest=source_digest,
    )


def _owned_meta(owned=None, created_dirs=None, profile=None, quoin_version="0.1.0", opencode_version="1.18.32"):
    return install.Metadata(
        quoin_version=quoin_version,
        opencode_version=opencode_version,
        profile=profile,
        owned=owned or {},
        created_dirs=created_dirs or [],
    )


def _materialize(root: Path, plan: "install.Plan", rendered) -> None:
    """Test-only helper: apply a plan's predicted post-state directly to disk."""
    for d in sorted(plan.dirs_to_create, key=lambda p: p.count("/")):
        (root / d).mkdir(parents=True, exist_ok=True)
    for action in plan.actions:
        if action.action in ("create", "update"):
            path = root / action.relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rendered[action.relpath].content)
        elif action.action in ("delete", "forget"):
            path = root / action.relpath
            if path.exists():
                path.unlink()
    for d in plan.dirs_to_prune:
        try:
            (root / d).rmdir()
        except OSError:
            pass
    meta_path = root / install.METADATA_RELPATH
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_bytes(plan.desired_metadata)


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


# --- plan_install: clean root ---


def test_plan_install_on_clean_root_creates_every_file(tmp_path: Path):
    rendered = {
        ".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "cmd", kind="command"),
        ".opencode/agents/quoin-plan.md": _rendered(".opencode/agents/quoin-plan.md", "agent", kind="agent"),
        ".opencode/skills/quoin-plan/SKILL.md": _rendered(".opencode/skills/quoin-plan/SKILL.md", "skill"),
        ".opencode/quoin/instructions.md": _rendered(
            ".opencode/quoin/instructions.md", "inst", kind="instructions", source_id=None
        ),
        ".opencode/opencode.jsonc": _rendered(".opencode/opencode.jsonc", "{}", kind="config", source_id=None),
    }
    plan = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    assert {a.action for a in plan.actions} == {"create"}
    assert len(plan.actions) == 5
    assert plan.metadata_action == "update"
    assert set(plan.dirs_to_create) == {
        ".opencode",
        ".opencode/agents",
        ".opencode/commands",
        ".opencode/quoin",
        ".opencode/skills",
        ".opencode/skills/quoin-plan",
        ".quoin",
    }


def test_plan_install_identical_unowned_bytes_are_adopted(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"same")
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "same")}
    plan = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    action = plan.actions[0]
    assert action.action == "adopt"
    assert action.reason == install.REASON_ADOPTED


def test_plan_install_different_unowned_bytes_is_a_conflict(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"different")
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "wanted")}
    plan = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    action = plan.actions[0]
    assert action.action == "conflict"
    assert action.reason == install.REASON_UNOWNED_EXISTS
    assert plan.conflicts == [action]


def test_plan_install_owned_modified_since_install_is_a_conflict(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"edited-by-user")
    owned = {".opencode/commands/quoin-plan.md": _owned_record(sha256=install._sha256_hex(b"original"))}
    meta = _owned_meta(owned=owned)
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "wanted")}
    plan = install.plan_install(tmp_path, rendered, meta, "0.1.0", "1.18.32", None)
    action = plan.actions[0]
    assert action.action == "conflict"
    assert action.reason == install.REASON_OWNED_MODIFIED


def test_plan_install_owned_unmodified_desired_bytes_differ_is_update(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"original")
    owned = {".opencode/commands/quoin-plan.md": _owned_record(sha256=install._sha256_hex(b"original"))}
    meta = _owned_meta(owned=owned)
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "new-content")}
    plan = install.plan_install(tmp_path, rendered, meta, "0.1.0", "1.18.32", None)
    assert plan.actions[0].action == "update"


def test_plan_install_owned_and_missing_is_recreated(tmp_path: Path):
    owned = {".opencode/commands/quoin-plan.md": _owned_record(sha256=install._sha256_hex(b"original"))}
    meta = _owned_meta(owned=owned)
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "original")}
    plan = install.plan_install(tmp_path, rendered, meta, "0.1.0", "1.18.32", None)
    action = plan.actions[0]
    assert action.action == "create"
    assert action.reason == install.REASON_OWNED_MISSING_RECREATED


def test_plan_install_stale_owned_file_handling(tmp_path: Path):
    # unmodified stale file -> delete
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-old.md").write_bytes(b"stale")
    owned = {".opencode/commands/quoin-old.md": _owned_record(sha256=install._sha256_hex(b"stale"))}
    meta = _owned_meta(owned=owned)
    plan = install.plan_install(tmp_path, {}, meta, "0.1.0", "1.18.32", None)
    assert plan.actions[0].action == "delete"

    # modified stale file -> conflict
    (tmp_path / ".opencode" / "commands" / "quoin-old.md").write_bytes(b"user-edited")
    plan2 = install.plan_install(tmp_path, {}, meta, "0.1.0", "1.18.32", None)
    assert plan2.actions[0].action == "conflict"
    assert plan2.actions[0].reason == install.REASON_STALE_MODIFIED

    # missing stale file -> forget
    (tmp_path / ".opencode" / "commands" / "quoin-old.md").unlink()
    plan3 = install.plan_install(tmp_path, {}, meta, "0.1.0", "1.18.32", None)
    assert plan3.actions[0].action == "forget"


def test_plan_install_symlink_and_bad_parent_are_conflicts(tmp_path: Path):
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "wanted")}

    # symlink at the target
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    real = tmp_path / "elsewhere.md"
    real.write_bytes(b"x")
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").symlink_to(real)
    plan = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    assert plan.actions[0].action == "conflict"
    assert plan.actions[0].reason == install.REASON_TARGET_NOT_REGULAR

    # directory at the target
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").unlink()
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").mkdir()
    plan2 = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    assert plan2.actions[0].action == "conflict"
    assert plan2.actions[0].reason == install.REASON_TARGET_NOT_REGULAR

    # symlinked parent
    tmp_path2 = tmp_path / "case2"
    tmp_path2.mkdir()
    real_dir = tmp_path2 / "realcommands"
    real_dir.mkdir()
    (tmp_path2 / ".opencode").mkdir()
    (tmp_path2 / ".opencode" / "commands").symlink_to(real_dir, target_is_directory=True)
    plan3 = install.plan_install(tmp_path2, rendered, None, "0.1.0", "1.18.32", None)
    assert plan3.actions[0].action == "conflict"
    assert plan3.actions[0].reason == install.REASON_PARENT_NOT_REAL_DIR

    # parent that is a file
    tmp_path3 = tmp_path / "case3"
    tmp_path3.mkdir()
    (tmp_path3 / ".opencode").mkdir()
    (tmp_path3 / ".opencode" / "commands").write_bytes(b"not a dir")
    plan4 = install.plan_install(tmp_path3, rendered, None, "0.1.0", "1.18.32", None)
    assert plan4.actions[0].action == "conflict"
    assert plan4.actions[0].reason == install.REASON_PARENT_NOT_REAL_DIR


def test_plan_install_metadata_only_change_leaves_files_unchanged(tmp_path: Path):
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "content")}
    plan1 = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    _materialize(tmp_path, plan1, rendered)

    plan2 = install.plan_install(tmp_path, rendered, install.load_metadata(tmp_path), "0.2.0", "1.18.32", None)
    assert all(a.action == "unchanged" for a in plan2.actions)
    assert plan2.metadata_action == "update"


def test_plan_install_repeat_plan_is_fully_unchanged(tmp_path: Path):
    rendered = {
        ".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "content"),
        ".opencode/skills/quoin-plan/SKILL.md": _rendered(".opencode/skills/quoin-plan/SKILL.md", "skill body"),
    }
    plan1 = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    _materialize(tmp_path, plan1, rendered)

    meta = install.load_metadata(tmp_path)
    plan2 = install.plan_install(tmp_path, rendered, meta, "0.1.0", "1.18.32", None)
    assert all(a.action == "unchanged" for a in plan2.actions)
    assert plan2.metadata_action == "unchanged"
    assert plan2.dirs_to_prune == []


def test_plan_install_prunes_stale_skill_dir_when_only_file_is_stale(tmp_path: Path):
    rendered_v1 = {".opencode/skills/quoin-old/SKILL.md": _rendered(".opencode/skills/quoin-old/SKILL.md", "body")}
    plan1 = install.plan_install(tmp_path, rendered_v1, None, "0.1.0", "1.18.32", None)
    _materialize(tmp_path, plan1, rendered_v1)
    meta = install.load_metadata(tmp_path)

    plan2 = install.plan_install(tmp_path, {}, meta, "0.1.0", "1.18.32", None)
    file_action = [a for a in plan2.actions if a.relpath == ".opencode/skills/quoin-old/SKILL.md"][0]
    assert file_action.action == "delete"
    assert ".opencode/skills/quoin-old" in plan2.dirs_to_prune
    obj = json.loads(plan2.desired_metadata.decode("utf-8"))
    assert ".opencode/skills/quoin-old" not in obj["created_dirs"]


def test_plan_install_does_not_prune_dir_with_a_user_file_added(tmp_path: Path):
    rendered_v1 = {".opencode/skills/quoin-old/SKILL.md": _rendered(".opencode/skills/quoin-old/SKILL.md", "body")}
    plan1 = install.plan_install(tmp_path, rendered_v1, None, "0.1.0", "1.18.32", None)
    _materialize(tmp_path, plan1, rendered_v1)
    meta = install.load_metadata(tmp_path)
    (tmp_path / ".opencode" / "skills" / "quoin-old" / "notes.txt").write_bytes(b"user file")

    plan2 = install.plan_install(tmp_path, {}, meta, "0.1.0", "1.18.32", None)
    assert ".opencode/skills/quoin-old" not in plan2.dirs_to_prune
    obj = json.loads(plan2.desired_metadata.decode("utf-8"))
    assert ".opencode/skills/quoin-old" in obj["created_dirs"]


def test_plan_install_records_unrecorded_dirs_after_interrupted_install(tmp_path: Path):
    # Simulate a first install interrupted after files were written but
    # before metadata was: the directories exist, hold only rendered
    # paths, and are unrecorded (no metadata at all).
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    (tmp_path / ".opencode" / "agents" / "quoin-plan.md").write_bytes(b"agent body")
    rendered = {".opencode/agents/quoin-plan.md": _rendered(".opencode/agents/quoin-plan.md", "agent body")}

    plan = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    obj = json.loads(plan.desired_metadata.decode("utf-8"))
    assert ".opencode" in obj["created_dirs"]
    assert ".opencode/agents" in obj["created_dirs"]


def test_plan_install_does_not_record_unrecorded_dir_holding_a_user_file(tmp_path: Path):
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    (tmp_path / ".opencode" / "agents" / "quoin-plan.md").write_bytes(b"agent body")
    (tmp_path / ".opencode" / "user-thing.json").write_bytes(b"{}")
    rendered = {".opencode/agents/quoin-plan.md": _rendered(".opencode/agents/quoin-plan.md", "agent body")}

    plan = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    obj = json.loads(plan.desired_metadata.decode("utf-8"))
    assert ".opencode" not in obj["created_dirs"]
    assert ".opencode/agents" in obj["created_dirs"]


def test_plan_install_never_writes_anything(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"content")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    rendered = {".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "different")}
    install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after
    assert (tmp_path / ".opencode" / "commands" / "quoin-plan.md").read_bytes() == b"content"


def test_plan_install_against_real_generator_output_is_fully_creates(tmp_path: Path):
    rendered = generate.render_source_dir(SOURCE_DIR)
    plan = install.plan_install(tmp_path, rendered, None, "0.1.0", "1.18.32", None)
    assert len(plan.actions) == len(rendered)
    assert {a.action for a in plan.actions} == {"create"}
    assert plan.metadata_action == "update"


# --- apply_install / run_install ---


def _small_rendered():
    return {
        ".opencode/commands/quoin-plan.md": _rendered(".opencode/commands/quoin-plan.md", "command body", kind="command"),
        ".opencode/agents/quoin-plan.md": _rendered(".opencode/agents/quoin-plan.md", "agent body", kind="agent"),
        ".opencode/skills/quoin-plan/SKILL.md": _rendered(".opencode/skills/quoin-plan/SKILL.md", "skill body"),
    }


def _run(project_root, rendered=None, profile=None, check=False, quoin_version="0.1.0", opencode_version="1.18.32"):
    out, err = io.StringIO(), io.StringIO()
    code = install.run_install(
        project_root, SOURCE_DIR, profile, check, out, err,
        rendered=rendered, quoin_version=quoin_version, opencode_version=opencode_version,
    )
    return code, out.getvalue(), err.getvalue()


def _snapshot(root: Path):
    entries = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_dir():
            entries[rel] = None
        elif p.is_file() and not p.is_symlink():
            entries[rel] = p.read_bytes()
    return entries


def test_install_twice_is_byte_identical_and_second_plan_is_all_unchanged(tmp_path: Path):
    rendered = _small_rendered()
    code1, out1, _ = _run(tmp_path, rendered=rendered)
    assert code1 == 0
    snapshot_after_first = _snapshot(tmp_path)

    code2, out2, _ = _run(tmp_path, rendered=rendered)
    assert code2 == 0
    assert _snapshot(tmp_path) == snapshot_after_first
    body_lines = [l for l in out2.splitlines() if not l.startswith("summary")]
    assert all(l.split()[0] in ("unchanged", "metadata") for l in body_lines)
    assert "metadata  unchanged" in out2


def test_install_write_ordering_metadata_last(tmp_path: Path, monkeypatch):
    rendered = _small_rendered()
    calls = []
    original = install.atomic_write

    def _spy(path, data):
        calls.append(str(path))
        original(path, data)

    monkeypatch.setattr(install, "atomic_write", _spy)
    code, _, _ = _run(tmp_path, rendered=rendered)
    assert code == 0
    assert calls[-1].endswith(install.METADATA_RELPATH)
    assert (tmp_path / ".quoin").is_dir()


def test_check_returns_1_on_clean_root_and_0_on_installed_root(tmp_path: Path):
    rendered = _small_rendered()
    before = _snapshot(tmp_path)
    code, _, _ = _run(tmp_path, rendered=rendered, check=True)
    assert code == 1
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / ".opencode").exists()
    assert not (tmp_path / ".quoin").exists()

    install_code, _, _ = _run(tmp_path, rendered=rendered)
    assert install_code == 0
    installed_snapshot = _snapshot(tmp_path)
    check_code, _, _ = _run(tmp_path, rendered=rendered, check=True)
    assert check_code == 0
    assert _snapshot(tmp_path) == installed_snapshot


def test_conflicts_return_3_and_list_path_and_reason_leave_tree_unchanged(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"user file")
    rendered = _small_rendered()
    before = _snapshot(tmp_path)
    code, _, err = _run(tmp_path, rendered=rendered)
    assert code == 3
    assert ".opencode/commands/quoin-plan.md" in err
    assert install.REASON_UNOWNED_EXISTS in err
    assert _snapshot(tmp_path) == before


def test_two_conflicts_at_once_are_both_listed(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"user file")
    (tmp_path / ".opencode" / "agents").mkdir(parents=True)
    (tmp_path / ".opencode" / "agents" / "quoin-plan.md").write_bytes(b"another user file")
    rendered = _small_rendered()
    code, _, err = _run(tmp_path, rendered=rendered)
    assert code == 3
    assert ".opencode/commands/quoin-plan.md" in err
    assert ".opencode/agents/quoin-plan.md" in err


def test_conflict_under_check_returns_3_not_1(tmp_path: Path):
    (tmp_path / ".opencode" / "commands").mkdir(parents=True)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").write_bytes(b"user file")
    rendered = _small_rendered()
    code, _, _ = _run(tmp_path, rendered=rendered, check=True)
    assert code == 3


def test_metadata_only_change_reinstall_prints_metadata_update(tmp_path: Path, monkeypatch):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered, quoin_version="0.1.0")

    check_code, check_out, _ = _run(tmp_path, rendered=rendered, quoin_version="0.0.0-test", check=True)
    assert check_code == 1
    assert "metadata  update" in check_out

    calls = []
    original = install.atomic_write

    def _spy(path, data):
        calls.append(str(path))
        original(path, data)

    monkeypatch.setattr(install, "atomic_write", _spy)
    code, out, _ = _run(tmp_path, rendered=rendered, quoin_version="0.0.0-test")
    assert code == 0
    assert "metadata  update" in out
    assert len(calls) == 1
    assert calls[0].endswith(install.METADATA_RELPATH)


def test_crash_recovery_converges_to_a_clean_install(tmp_path: Path, monkeypatch, tmp_path_factory):
    rendered = _small_rendered()
    calls = {"n": 0}
    original = install.atomic_write

    def _flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(5, "injected crash")
        original(path, data)

    monkeypatch.setattr(install, "atomic_write", _flaky)
    code, _, _ = _run(tmp_path, rendered=rendered)
    assert code == 2
    monkeypatch.setattr(install, "atomic_write", original)

    rerun_code, _, _ = _run(tmp_path, rendered=rendered)
    assert rerun_code == 0

    clean_root = tmp_path_factory.mktemp("clean")
    clean_code, _, _ = _run(clean_root, rendered=rendered)
    assert clean_code == 0
    assert _snapshot(tmp_path) == _snapshot(clean_root)

    check_code, _, _ = _run(tmp_path, rendered=rendered, check=True)
    assert check_code == 0


def test_stale_owned_file_delete_and_prune_then_idempotent(tmp_path: Path, monkeypatch):
    rendered_v1 = {".opencode/skills/quoin-old/SKILL.md": _rendered(".opencode/skills/quoin-old/SKILL.md", "body")}
    _run(tmp_path, rendered=rendered_v1)

    calls = []
    original = install.atomic_write

    def _spy(path, data):
        calls.append(str(path))
        original(path, data)

    monkeypatch.setattr(install, "atomic_write", _spy)
    code, _, _ = _run(tmp_path, rendered={})
    assert code == 0
    assert not (tmp_path / ".opencode" / "skills" / "quoin-old").exists()

    check_code, check_out, _ = _run(tmp_path, rendered={}, check=True)
    assert check_code == 0
    assert all("unchanged" in l or l.startswith("summary") for l in check_out.splitlines() if l.strip())

    calls.clear()
    reinstall_code, _, _ = _run(tmp_path, rendered={})
    assert reinstall_code == 0
    assert calls == []


def test_owned_file_deleted_by_user_is_recreated_with_reason(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    (tmp_path / ".opencode" / "commands" / "quoin-plan.md").unlink()

    code, out, _ = _run(tmp_path, rendered=rendered)
    assert code == 0
    assert install.REASON_OWNED_MISSING_RECREATED in out


def test_files_outside_the_five_families_are_untouched(tmp_path: Path):
    (tmp_path / generate.ARTIFACT_ROOT).mkdir()
    (tmp_path / generate.ARTIFACT_ROOT / "memory.md").write_bytes(b"artifacts")
    (tmp_path / "AGENTS.md").write_bytes(b"agents doc")
    (tmp_path / ".env").write_bytes(b"SECRET=1")

    rendered = _small_rendered()
    before = {
        generate.ARTIFACT_ROOT + "/memory.md": (tmp_path / generate.ARTIFACT_ROOT / "memory.md").read_bytes(),
        "AGENTS.md": (tmp_path / "AGENTS.md").read_bytes(),
        ".env": (tmp_path / ".env").read_bytes(),
    }
    code, _, _ = _run(tmp_path, rendered=rendered)
    assert code == 0
    for relpath, data in before.items():
        assert (tmp_path / relpath).read_bytes() == data


# --- plan_uninstall / run_uninstall ---


def _uninstall(project_root, dry_run=False):
    out, err = io.StringIO(), io.StringIO()
    code = install.run_uninstall(project_root, dry_run, out, err)
    return code, out.getvalue(), err.getvalue()


def test_uninstall_round_trip_keeps_edited_file_and_removes_the_rest(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    edited = tmp_path / ".opencode" / "commands" / "quoin-plan.md"
    edited_bytes = edited.read_bytes() + b"\nextra line\n"
    edited.write_bytes(edited_bytes)

    (tmp_path / generate.ARTIFACT_ROOT).mkdir()
    (tmp_path / generate.ARTIFACT_ROOT / "memory.md").write_bytes(b"artifacts")
    (tmp_path / "AGENTS.md").write_bytes(b"agents doc")
    (tmp_path / ".env").write_bytes(b"SECRET=1")
    (tmp_path / ".opencode" / "opencode.json").write_bytes(b'{"theme": "dark"}')
    untouched = {
        generate.ARTIFACT_ROOT + "/memory.md": b"artifacts",
        "AGENTS.md": b"agents doc",
        ".env": b"SECRET=1",
        ".opencode/opencode.json": b'{"theme": "dark"}',
    }

    code, out, _ = _uninstall(tmp_path)
    assert code == 4
    assert "keep" in out
    assert edited.read_bytes() == edited_bytes
    assert not (tmp_path / ".opencode" / "agents" / "quoin-plan.md").exists()
    assert not (tmp_path / ".opencode" / "skills" / "quoin-plan").exists()
    assert (tmp_path / ".opencode" / "commands").is_dir()
    assert (tmp_path / ".opencode").is_dir()
    for relpath, data in untouched.items():
        assert (tmp_path / relpath).read_bytes() == data
    meta = install.load_metadata(tmp_path)
    assert list(meta.owned) == [".opencode/commands/quoin-plan.md"]


def test_uninstall_reinstall_after_kept_edit_conflicts(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    edited = tmp_path / ".opencode" / "commands" / "quoin-plan.md"
    edited.write_bytes(edited.read_bytes() + b"\nextra\n")
    _uninstall(tmp_path)

    code, _, err = _run(tmp_path, rendered=rendered)
    assert code == 3
    assert install.REASON_OWNED_MODIFIED in err
    assert ".opencode/commands/quoin-plan.md" in err


def test_uninstall_clean_removes_metadata_and_quoin_dir(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    code, _, _ = _uninstall(tmp_path)
    assert code == 0
    assert not (tmp_path / ".quoin").exists()
    assert not (tmp_path / ".opencode").exists()


def test_uninstall_clean_leaves_dir_holding_a_file_opencode_itself_wrote(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    (tmp_path / ".opencode" / ".gitignore").write_bytes(b"node_modules\n")

    code, _, _ = _uninstall(tmp_path)
    assert code == 0
    assert (tmp_path / ".opencode").is_dir()
    assert (tmp_path / ".opencode" / ".gitignore").read_bytes() == b"node_modules\n"
    assert not (tmp_path / ".opencode" / "commands").exists()
    assert not (tmp_path / ".quoin").exists()


def test_uninstall_dry_run_writes_nothing_and_returns_the_kept_code(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    edited = tmp_path / ".opencode" / "commands" / "quoin-plan.md"
    edited.write_bytes(edited.read_bytes() + b"\nextra\n")
    before = _snapshot(tmp_path)

    code, out, _ = _uninstall(tmp_path, dry_run=True)
    assert code == 4
    assert _snapshot(tmp_path) == before
    assert "keep" in out


def test_uninstall_dry_run_clean_case_writes_nothing_and_returns_0(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    before = _snapshot(tmp_path)

    code, _, _ = _uninstall(tmp_path, dry_run=True)
    assert code == 0
    assert _snapshot(tmp_path) == before


def test_uninstall_tampered_metadata_owned_path_outside_families_returns_2(tmp_path: Path):
    for bad_path in ("src/app.py", "../x.md"):
        obj = _base_metadata_obj(owned={bad_path: _owned_record()})
        _write_metadata_json(tmp_path, obj)
        code, _, _ = _uninstall(tmp_path)
        assert code == 2
        assert (tmp_path / install.METADATA_RELPATH).exists()


def test_uninstall_keeps_a_symlinked_owned_path_without_following_it(tmp_path: Path):
    rendered = _small_rendered()
    _run(tmp_path, rendered=rendered)
    target = tmp_path.parent / "outside-target.txt"
    target.write_bytes(b"do not touch")
    owned_path = tmp_path / ".opencode" / "commands" / "quoin-plan.md"
    owned_path.unlink()
    owned_path.symlink_to(target)

    code, out, _ = _uninstall(tmp_path)
    assert code == 4
    assert owned_path.is_symlink()
    assert target.read_bytes() == b"do not touch"
    assert install.REASON_LEFT_IN_PLACE in out


def test_uninstall_with_no_metadata_returns_0_and_says_nothing_installed(tmp_path: Path):
    code, out, _ = _uninstall(tmp_path)
    assert code == 0
    assert "nothing installed" in out


# --- CLI wiring (src/quoin/cli.py) ---

import quoin.cli as cli_mod


def test_cli_install_runtime_opencode_dispatches_run_install_with_parsed_values(monkeypatch):
    calls = []

    def _fake_run_install(project_root, source_dir, profile, check, out, err, **kwargs):
        calls.append((project_root, profile, check))
        return 0

    monkeypatch.setattr(install, "run_install", _fake_run_install)
    code = cli_mod.main([
        "install", "--runtime", "opencode",
        "--project-root", "/tmp/some-project",
        "--profile", "demo",
        "--check",
        "--source-dir", str(SOURCE_DIR),
    ])
    assert code == 0
    assert calls == [("/tmp/some-project", "demo", True)]


def test_cli_install_profile_flag_rejected_for_non_opencode_runtimes():
    for runtime in ("claude", "codex"):
        with pytest.raises(SystemExit) as exc_info:
            cli_mod.main(["install", "--runtime", runtime, "--profile", "x"])
        assert exc_info.value.code == 2


def test_cli_install_scope_project_mutex_with_opencode():
    from quoin.cli import _cmd_install

    class _Args:
        scope = "project"
        runtime = "opencode"
        profile = None
        check = False

    with pytest.raises(SystemExit) as exc_info:
        _cmd_install(_Args())
    assert exc_info.value.code == 2


def test_cli_opencode_uninstall_dispatches_run_uninstall_with_parsed_values(monkeypatch):
    calls = []

    def _fake_run_uninstall(project_root, dry_run, out, err):
        calls.append((project_root, dry_run))
        return 0

    monkeypatch.setattr(install, "run_uninstall", _fake_run_uninstall)
    code = cli_mod.main([
        "opencode", "uninstall", "--project-root", "/tmp/some-project", "--dry-run",
    ])
    assert code == 0
    assert calls == [("/tmp/some-project", True)]


def test_cli_opencode_script_dispatches_scripts_run_with_parsed_values(monkeypatch):
    from quoin.opencode_adapter import scripts as scripts_mod

    calls = []

    def _fake_run(name, argv, source_dir, **kwargs):
        calls.append((name, argv))
        return 0

    monkeypatch.setattr(scripts_mod, "run", _fake_run)
    code = cli_mod.main([
        "opencode", "script", "--source-dir", str(SOURCE_DIR),
        "path_resolve", "--task", "t",
    ])
    assert code == 0
    assert calls == [("path_resolve", ["--task", "t"])]


def test_cli_help_texts_mention_opencode():
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[3]
    src = repo / "src"
    env = dict(os.environ, PYTHONPATH=str(src))
    for argv in (["opencode", "--help"], ["install", "--help"]):
        result = subprocess.run(
            [sys.executable, "-m", "quoin", *argv],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 0
        assert "opencode" in result.stdout.lower()
