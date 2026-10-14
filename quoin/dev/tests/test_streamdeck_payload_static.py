"""Static guards over the shipped Stream Deck sources."""
import ast
import re
import sys

import pytest

import _streamdeck_helpers as h

SHIPPED = h.REPO_ROOT / "src" / "quoin" / "streamdeck"
PAYLOAD = h.PAYLOAD_DIR
TEXT_SUFFIXES = {".py", ".js", ".zsh", ".json", ".md", ".sh"}
SIBLINGS = {"deckenv", "monitor", "controller", "dashboard"}

# The only deck-path-looking literal allowed: Claude's own install directory,
# used to recognise the agent process, not a path the deck writes or runs.
VERSIONS_ALLOWED = {("monitor.py", "/claude/versions/")}


def shipped_files(suffixes=None):
    out = []
    for path in sorted(SHIPPED.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in (suffixes or TEXT_SUFFIXES):
            out.append(path)
    return out


def python_files():
    return shipped_files({".py"})


def test_shipped_tree_is_not_empty():
    assert len(python_files()) >= 6
    assert any(p.suffix == ".js" for p in shipped_files())
    assert any(p.suffix == ".zsh" for p in shipped_files())


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(SHIPPED)))
def test_py39_syntax_and_annotations(path):
    src = path.read_text()
    tree = ast.parse(src, filename=str(path), feature_version=(3, 9))
    future = any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    annotations = []
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Match), "match statement needs 3.10"
        if isinstance(node, ast.Call):
            names = [k.arg for k in node.keywords]
            func = node.func
            if isinstance(func, ast.Name) and func.id == "zip":
                assert "strict" not in names, "zip(strict=) needs 3.10"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                annotations.append(node.returns)
            a = node.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    annotations.append(arg.annotation)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
    if not future:
        for ann in annotations:
            for sub in ast.walk(ann):
                assert not (isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)), \
                    "X | Y annotation without `from __future__ import annotations`"


@pytest.mark.parametrize("path", shipped_files(), ids=lambda p: str(p.relative_to(SHIPPED)))
def test_no_machine_specific_paths(path):
    text = path.read_text()
    for needle in ("/opt/homebrew", "/Users/", "parents[", "Path(__file__).resolve()"):
        assert needle not in text, "%s contains %r" % (path.name, needle)


def test_no_private_agent_desk_access():
    pattern = re.compile(r"^\s*(function\s+)?_agentdesk_\w+\s*\(\)", re.M)
    for path in shipped_files():
        text = path.read_text()
        assert "_agentdesk_save_layout" not in text, path.name
        assert "_agentdesk_layout_key" not in text, path.name
        assert not pattern.search(text), "%s defines an _agentdesk_ function" % path.name


def test_payload_string_literals_avoid_versions_dir():
    seen = []
    for path in sorted(PAYLOAD.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "versions/" in node.value:
                seen.append((path.name, node.value))
    assert seen == sorted(VERSIONS_ALLOWED) or seen == list(VERSIONS_ALLOWED), seen
    for path in sorted(PAYLOAD.rglob("*")):
        if path.suffix in (".js", ".zsh"):
            assert "versions/" not in path.read_text(), path.name


def test_stdlib_only_imports():
    allowed = set(sys.stdlib_module_names) | {"quoin"} | SIBLINGS
    for path in python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                assert name in allowed, "%s imports %s" % (path.name, name)


def test_no_collectable_tests_in_shipped_trees():
    roots = [SHIPPED, h.REPO_ROOT / "quoin" / "tools" / "agentdesk"]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            name = path.name
            assert not (name.startswith("test_") or name.endswith("_test.py")), path
            assert name != "conftest.py", path
