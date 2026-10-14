"""Static guards for the lifecycle modules: stdlib only, effects only where allowed."""
import ast
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
PKG = REPO / "src" / "quoin" / "streamdeck"
TESTS = pathlib.Path(__file__).resolve().parent
LIFECYCLE = ("paths", "macos", "jsonfile", "hooks_merge", "manifest", "prereqs", "status", "shims", "migrations",
             "engine", "cli", "notice")
STAGE4_TESTS = ("shims", "migrations", "engine_core", "install", "faults", "adoption", "upgrade", "uninstall", "cli",
                "core_install")
GUARDS = ("forbid_dangerous_subprocess", "forbid_real_home_io", "forbid_real_seams")
PAYLOAD_SCRIPTS = ("deckenv", "monitor", "dashboard", "controller")


def tree(name, base=PKG):
    path = base / (name + ".py")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def source(name):
    return (PKG / (name + ".py")).read_text(encoding="utf-8")


def parents_map(root):
    out = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def enclosing_functions(node, parents):
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return names


def imported_roots(root, top_level_only):
    nodes = root.body if top_level_only else list(ast.walk(root))
    roots = []
    for node in nodes:
        if isinstance(node, ast.Import):
            roots.extend((a.name.split(".")[0], a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            base = node.module or ""
            roots.append((base.split(".")[0], base))
            roots.extend((base.split(".")[0], base + "." + a.name) for a in node.names)
    return roots


@pytest.mark.parametrize("name", LIFECYCLE)
def test_imports_are_stdlib_or_quoin(name):
    for root, full in imported_roots(tree(name), top_level_only=False):
        assert root in sys.stdlib_module_names or root == "quoin", (name, full)


@pytest.mark.parametrize("name", LIFECYCLE)
def test_installer_and_cli_only_imported_lazily(name):
    for root, full in imported_roots(tree(name), top_level_only=True):
        assert full not in ("quoin.installer", "quoin.cli") and not full.startswith(("quoin.installer.", "quoin.cli.")), (name, full)
    root = tree(name)
    parents = parents_map(root)
    for node in ast.walk(root):
        if isinstance(node, ast.ImportFrom) and node.module == "quoin" and {a.name for a in node.names} & {"installer", "cli"}:
            assert name == "prereqs" and "deploy_agentdesk_files" in enclosing_functions(node, parents)


def test_no_runtime_dependencies_added():
    text = (REPO / "pyproject.toml").read_text()
    project = text[text.index("[project]"):text.index("[project.urls]")]
    assert not re.search(r"^dependencies\s*=", project, re.M)


@pytest.mark.parametrize("name", LIFECYCLE)
def test_no_machine_specific_literals(name):
    text = source(name)
    for literal in ("/opt/homebrew", "/Users/", "parents["):
        assert literal not in text, (name, literal)


def test_config_toml_only_in_paths_and_status():
    for name in LIFECYCLE:
        assert ("config.toml" in source(name)) == (name in ("paths", "status")), name


@pytest.mark.parametrize("name", LIFECYCLE)
def test_no_lifecycle_module_imports_payload_scripts(name):
    for root, full in imported_roots(tree(name), top_level_only=False):
        assert root not in PAYLOAD_SCRIPTS and not any(full.endswith("." + s) for s in PAYLOAD_SCRIPTS), (name, full)


def test_only_prereqs_and_engine_may_use_subprocess_input_or_isatty():
    for name in LIFECYCLE:
        if name in ("prereqs", "engine"):
            continue
        for node in ast.walk(tree(name)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert "subprocess" not in ast.dump(node), name
            if isinstance(node, ast.Call):
                func = node.func
                assert not (isinstance(func, ast.Name) and func.id == "input"), name
                assert not (isinstance(func, ast.Attribute) and func.attr == "isatty"), name
    for node in ast.walk(tree("engine")):
        if isinstance(node, ast.Call):
            func = node.func
            assert not (isinstance(func, ast.Name) and func.id == "input")
            assert not (isinstance(func, ast.Attribute) and func.attr == "isatty")


def test_prereqs_effects_live_in_the_named_functions():
    root = tree("prereqs")
    parents = parents_map(root)
    allowed = {"_real_run_probe", "_real_runner", "default_ctx"}
    for node in ast.walk(root):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        effect = (
            (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "subprocess")
            or (isinstance(func, ast.Name) and func.id == "input")
            or (isinstance(func, ast.Attribute) and func.attr == "isatty")
        )
        if effect:
            assert allowed & set(enclosing_functions(node, parents)), ast.dump(func)


@pytest.mark.parametrize("module", ["test_streamdeck_prereqs", "test_streamdeck_status"])
def test_prereq_and_status_tests_never_bind_the_real_context(module):
    forbidden = {"default_ctx", "_real_runner", "_real_run_probe"}
    for node in ast.walk(tree(module, TESTS)):
        if isinstance(node, ast.Name):
            assert node.id not in forbidden, (module, node.id)
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden, (module, node.attr)
        if isinstance(node, ast.alias):
            assert node.name not in forbidden, (module, node.name)


def test_status_is_read_only_by_construction():
    root = tree("status")
    for _, full in imported_roots(root, top_level_only=False):
        assert full.split(".")[0] not in ("subprocess", "fcntl", "shutil", "tempfile"), full
    banned = {"makedirs", "mkdir", "replace", "rename", "remove", "unlink", "rmdir", "symlink", "chmod", "write_atomic"}
    for node in ast.walk(root):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                assert func.attr not in banned, func.attr
                assert not (isinstance(func.value, ast.Name) and func.value.id == "shutil")
                assert not (isinstance(func.value, ast.Name) and func.value.id == "jsonfile" and func.attr != "read"), func.attr
            if isinstance(func, ast.Name) and func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                if mode is not None:
                    assert isinstance(mode, ast.Constant) and mode.value in ("r", "rb"), ast.dump(node)


def test_engine_effects_live_in_the_named_functions():
    root = tree("engine")
    parents = parents_map(root)
    seen = 0
    for node in ast.walk(root):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                seen += 1
                assert {"_real_probe", "_real_run"} & set(enclosing_functions(node, parents)), ast.dump(func)
    assert seen >= 2


def test_only_the_cli_and_the_notice_resolve_paths():
    for name in LIFECYCLE:
        found = False
        for node in ast.walk(tree(name)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id in ("paths", "paths_mod"):
                    found = True
        assert found == (name in ("cli", "notice")), name


def test_nothing_writes_codex_trust():
    for path in PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "trusted_hash" not in text.replace("no trusted_hash", "") or path.name in ("status.py",), path.name
        if path.name != "status.py":
            assert "hooks.state" not in text, path.name


@pytest.mark.parametrize("name", STAGE4_TESTS)
def test_stage_four_tests_use_the_guards_and_stay_off_the_real_seams(name):
    root = tree("test_streamdeck_" + name, TESTS)
    marks = [n for n in ast.walk(root) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets)]
    assert marks, name
    declared = {c.value for c in ast.walk(marks[0].value) if isinstance(c, ast.Constant)}
    assert set(GUARDS) <= declared, name
    forbidden = {"default_ctx", "_real_probe", "_real_run", "_real_runner", "default_sysops"}
    for node in ast.walk(root):
        if isinstance(node, ast.Name):
            assert node.id not in forbidden, (name, node.id)
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden, (name, node.attr)
        if isinstance(node, ast.alias):
            assert node.name not in forbidden, (name, node.name)


PLANNING_ID = re.compile(r"\b[TDRQ]-\d{2}\b|\bAC-\d|\bFR-\d|\b(MAJ|MIN|CRIT)-\d|\bround \d")


@pytest.mark.parametrize("name", STAGE4_TESTS)
def test_stage_four_tests_carry_no_planning_ids(name):
    text = (TESTS / ("test_streamdeck_%s.py" % name)).read_text(encoding="utf-8")
    hits = [m.group(0) for m in PLANNING_ID.finditer(text)]
    assert not hits, (name, hits)


def test_default_ctx_takes_the_apps_directory_from_the_override_helper():
    func = next(n for n in ast.walk(tree("prereqs")) if isinstance(n, ast.FunctionDef) and n.name == "default_ctx")
    values = [kw.value for node in ast.walk(func) if isinstance(node, ast.Call) for kw in node.keywords
              if kw.arg == "apps_dir"]
    assert len(values) == 1
    call = values[0]
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "apps_dir_from"
