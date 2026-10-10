"""Configuration discovery shared by the doctor, the launch checks and the inventory."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from quoin.opencode_adapter import doctor, inventory, launch_env

INVENTORY = Path(inventory.__file__)
LAUNCH_ENV = Path(launch_env.__file__)

ALIASES = (
    ("display_path", "display_path"),
    ("_ConfigUnreadable", "ConfigUnreadable"),
    ("_strip_jsonc", "strip_jsonc"),
    ("_load_config_file", "load_config_file"),
    ("_xdg_config_dir", "xdg_config_dir"),
    ("_worktree_root", "worktree_root"),
    ("_dir_chain", "dir_chain"),
    ("_build_roots", "build_roots"),
    ("_project_config_dirs", "project_config_dirs"),
)


@pytest.mark.parametrize("old,new", ALIASES)
def test_doctor_names_are_the_inventory_objects(old, new):
    assert getattr(doctor, old) is getattr(inventory, new)


def _imported_adapter_modules(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("quoin.opencode_adapter"):
            found.add(node.module.split(".")[-1])
    return found


def test_inventory_imports_no_adapter_module_beyond_jsonio_and_paths():
    assert _imported_adapter_modules(INVENTORY) <= {"jsonio", "paths"}


def test_launch_env_no_longer_imports_the_doctor():
    assert "doctor" not in _imported_adapter_modules(LAUNCH_ENV)
    assert "inventory" in _imported_adapter_modules(LAUNCH_ENV)


def test_inventory_module_hygiene():
    text = INVENTORY.read_text(encoding="utf-8")
    ast.parse(text, feature_version=(3, 10))
    body = [n for n in ast.parse(text).body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__"
    assert "os.environ" not in text and "os.getenv" not in text


def _world(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    (home / ".config" / "opencode").mkdir(parents=True)
    (project / ".git").mkdir(parents=True)
    return home, project


def test_read_config_files_returns_documents_in_load_order_and_unreadable_paths(tmp_path):
    home, project = _world(tmp_path)
    glob = home / ".config" / "opencode"
    (glob / "opencode.json").write_text(json.dumps({"model": "a/b"}))
    (glob / "config.json").write_text("{not json")
    (project / "opencode.jsonc").write_text('{"share": "disabled", // note\n}')
    docs, unreadable = inventory.read_config_files(project, project, {}, home)
    assert [p.name for p, _ in docs] == ["opencode.json", "opencode.jsonc"]
    assert unreadable == [glob / "config.json"]


def test_the_doctor_wrapper_turns_unreadable_paths_into_findings_in_order(tmp_path):
    home, project = _world(tmp_path)
    glob = home / ".config" / "opencode"
    (glob / "config.json").write_text("[]")
    (project / "opencode.json").write_text("{")
    roots = doctor._build_roots(project, {}, home)
    docs, findings = doctor._read_config_files(project, project, {}, home, roots)
    assert docs == []
    assert [f.id for f in findings] == ["config-unreadable", "config-unreadable"]
    assert [f.path for f in findings] == ["~/.config/opencode/config.json", "./opencode.json"]


def test_project_config_can_be_disabled_by_the_legacy_flag(tmp_path):
    home, project = _world(tmp_path)
    (project / "opencode.json").write_text("{}")
    docs, _ = inventory.read_config_files(project, project, {"OPENCODE_DISABLE_PROJECT_CONFIG": "1"}, home)
    assert docs == []
