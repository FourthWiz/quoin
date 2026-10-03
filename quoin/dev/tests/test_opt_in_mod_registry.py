"""Opt-in mod registry: the context-tracker messages are pinned as literals.

The installer's opt-in mod helpers are generic over a mod name. These tests
spell every context-tracker message out in full (not built from the generic
template), so a template that only agrees with itself cannot pass.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import quoin.installer as inst

REPO = Path(__file__).resolve().parents[3]
QUOIN_SRC = REPO / "quoin"
PLUGIN_SRC = QUOIN_SRC / "plugins" / "context-tracker"


def _src_copy(tmp_path: Path, *, drop: str | None = None) -> Path:
    src = tmp_path / "srctree"
    shutil.copytree(PLUGIN_SRC, src / "plugins" / "context-tracker")
    if drop:
        (src / "plugins" / "context-tracker" / drop).unlink()
    return src


def _preflight(source, dest, *, mode, project=False, home=None, cwd=None):
    return inst.context_tracker_preflight(
        source,
        dest,
        mode=mode,
        is_project_mode=project,
        home_dest_root=home if home is not None else dest,
        cwd_dest_root=cwd if cwd is not None else dest,
    )


def _foreign(dest: Path) -> Path:
    folder = dest / "skills" / "context-tracker"
    folder.mkdir(parents=True)
    (folder / "note.txt").write_text("mine")
    return folder


class TestContextTrackerMessages:
    def test_missing_sources_error(self, tmp_path):
        src = _src_copy(tmp_path, drop="hooks/register.tsx")
        errors, warnings = _preflight(src, tmp_path / "dest", mode="with")
        assert errors == ["quoin: context-tracker source files missing: hooks/register.tsx"]
        assert warnings == []

    def test_foreign_folder_error(self, tmp_path):
        dest = tmp_path / "dest"
        folder = _foreign(dest)
        errors, _ = _preflight(QUOIN_SRC, dest, mode="with")
        assert errors == [
            f"quoin: {folder} exists and is not the quoin context-tracker mod "
            "(or is a symlink); move it aside and re-run"
        ]

    def test_project_double_load_error(self, tmp_path):
        home, project = tmp_path / "home", tmp_path / "project"
        inst.deploy_context_tracker(QUOIN_SRC, home, strict=True)
        errors, _ = _preflight(QUOIN_SRC, project, mode="with", project=True, home=home)
        assert errors == [
            f"quoin: user-scope copy installed at {home / 'skills' / 'context-tracker'}; "
            "remove it first (--remove-context-tracker --scope user) to avoid a double load"
        ]

    def test_user_scope_reverse_warning(self, tmp_path):
        home, project = tmp_path / "home", tmp_path / "project"
        inst.deploy_context_tracker(QUOIN_SRC, project, strict=True)
        errors, warnings = _preflight(QUOIN_SRC, home, mode="with", home=home, cwd=project)
        assert errors == []
        assert warnings == [
            f"quoin: project copy at {project / 'skills' / 'context-tracker'} "
            "will load alongside this user copy"
        ]

    def test_remove_foreign_warning(self, tmp_path):
        dest = tmp_path / "dest"
        folder = _foreign(dest)
        errors, warnings = _preflight(QUOIN_SRC, dest, mode="remove")
        assert errors == []
        assert warnings == [f"quoin: {folder} is not the quoin context-tracker mod; leaving it untouched"]

    def test_strict_deploy_with_a_missing_file_exits(self, tmp_path, capsys):
        src = _src_copy(tmp_path, drop="types/index.d.ts")
        with pytest.raises(SystemExit) as exc:
            inst.deploy_context_tracker(src, tmp_path / "dest", strict=True)
        assert exc.value.code == 1
        missing = src / "plugins" / "context-tracker" / "types/index.d.ts"
        assert capsys.readouterr().err == (
            f"quoin: Expected context-tracker file types/index.d.ts at {missing} but not found\n"
        )

    def test_non_strict_deploy_skips_with_a_warning(self, tmp_path, capsys):
        src = _src_copy(tmp_path, drop="types/index.d.ts")
        copied = inst.deploy_context_tracker(src, tmp_path / "dest", strict=False)
        assert copied == 3
        missing = src / "plugins" / "context-tracker" / "types/index.d.ts"
        assert capsys.readouterr().err == (
            f"quoin: Expected context-tracker file types/index.d.ts at {missing} but not found; skipping\n"
        )

    def test_apply_deployed_then_refreshed_then_removed(self, tmp_path, capsys):
        dest = tmp_path / "dest"
        folder = dest / "skills" / "context-tracker"
        assert inst.apply_context_tracker(QUOIN_SRC, dest, mode="with") == "deployed"
        assert capsys.readouterr().out == f"Deployed context-tracker mod to {folder}\n"
        assert inst.apply_context_tracker(QUOIN_SRC, dest, mode="with") == "refreshed"
        assert capsys.readouterr().out == f"Refreshed context-tracker mod at {folder}\n"
        assert inst.apply_context_tracker(QUOIN_SRC, dest, mode=None) == "refreshed"
        assert capsys.readouterr().out == f"Refreshed context-tracker mod at {folder}\n"
        assert inst.apply_context_tracker(QUOIN_SRC, dest, mode="remove") == "removed"
        assert capsys.readouterr().out == f"Removed context-tracker mod from {folder}\n"

    def test_apply_nothing_to_remove(self, tmp_path, capsys):
        dest = tmp_path / "dest"
        folder = dest / "skills" / "context-tracker"
        assert inst.apply_context_tracker(QUOIN_SRC, dest, mode="remove") == "absent"
        assert capsys.readouterr().out == f"context-tracker mod not installed at {folder}; nothing to remove\n"

    def test_apply_remove_foreign_is_silent(self, tmp_path, capsys):
        dest = tmp_path / "dest"
        _foreign(dest)
        assert inst.apply_context_tracker(QUOIN_SRC, dest, mode="remove") == "foreign"
        assert capsys.readouterr().out == ""

    def test_no_mode_and_absent_is_silent(self, tmp_path, capsys):
        assert inst.apply_context_tracker(QUOIN_SRC, tmp_path / "dest", mode=None) == "noop"
        assert capsys.readouterr().out == ""
