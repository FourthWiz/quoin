"""The continuation core script and doc stay runtime-neutral and 3.8-compatible."""

import ast
import re
import subprocess
import sys
from pathlib import Path

QUOIN_DIR = Path(__file__).resolve().parents[2]
SCRIPT = QUOIN_DIR / "core" / "scripts" / "continuation_handoff.py"
DOC = QUOIN_DIR / "core" / "workflow" / "continuation-handoff.md"

FORBIDDEN = ("opencode", "claude", "codex", "ccusage", "jsonl")
TYPE_NAMES = {"str", "int", "float", "bool", "bytes", "list", "dict", "tuple", "set", "Any", "List", "Dict", "Optional"}
STDLIB_ALLOWED = {
    "argparse", "copy", "errno", "json", "os", "re", "stat", "sys", "typing", "__future__",
}


def test_no_runtime_names():
    for path in (SCRIPT, DOC):
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN:
            assert token not in text, (path.name, token)


def test_imports_stdlib_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            modules.add((node.module or "").split(".")[0])
    assert modules <= STDLIB_ALLOWED, modules - STDLIB_ALLOWED


def _annotation_nodes(tree):
    ids = set()
    for node in ast.walk(tree):
        annos = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annos.append(node.returns)
            args = node.args
            for a in args.args + args.kwonlyargs + args.posonlyargs + [args.vararg, args.kwarg]:
                if a is not None:
                    annos.append(a.annotation)
        elif isinstance(node, ast.AnnAssign):
            annos.append(node.annotation)
        for anno in annos:
            if anno is not None:
                for sub in ast.walk(anno):
                    ids.add(id(sub))
    return ids


def test_python38_syntax_and_runtime_expressions():
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    assert "from __future__ import annotations" in source
    skip = _annotation_nodes(tree)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            for side in (node.left, node.right):
                base = side.value if isinstance(side, ast.Subscript) else side
                is_type = isinstance(base, ast.Name) and base.id in TYPE_NAMES
                is_none = isinstance(side, ast.Constant) and side.value is None
                assert not (is_type or is_none), (
                    "PEP 604 union outside an annotation at line %d" % node.lineno
                )
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            assert node.value.id not in {"list", "dict", "tuple", "set"}, (
                "builtin generic at line %d" % node.lineno
            )


def test_self_test_runs_isolated():
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--self-test"], capture_output=True, text=True, cwd="/"
    )
    assert result.returncode == 0, result.stdout + result.stderr
