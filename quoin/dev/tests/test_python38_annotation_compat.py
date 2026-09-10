"""test_python38_annotation_compat.py — regression guard for the recurring
PEP-604 crash (lessons-learned 2026-09-04).

quoin's own test suite runs through a 3.10+ venv, so a bare `X | None`
annotation missing `from __future__ import annotations` never fails locally —
it only crashes at import time (`type.__or__` is undefined before 3.10) on
the plain system Python 3.8 that /run's bare `python3` call sites actually
execute under. This was rediscovered independently 7 times across
2026-09-04 through 2026-09-07 before being fixed.

Static AST check — needs no 3.8 interpreter, no subprocess.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCAN_DIRS = [
    REPO_ROOT / "quoin" / "core" / "scripts",
    REPO_ROOT / "quoin" / "scripts",
    REPO_ROOT / "quoin" / "hooks",
]


def _target_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        if d.exists():
            files.extend(sorted(d.rglob("*.py")))
    return files


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _annotation_nodes(tree: ast.Module):
    """Yield every annotation expression node: function arg/return
    annotations and `AnnAssign` (variable) annotations."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                yield node.returns
            args = (
                list(node.args.posonlyargs)
                + list(node.args.args)
                + list(node.args.kwonlyargs)
            )
            if node.args.vararg is not None:
                args.append(node.args.vararg)
            if node.args.kwarg is not None:
                args.append(node.args.kwarg)
            for arg in args:
                if arg.annotation is not None:
                    yield arg.annotation
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _has_pep604_union(annotation: ast.AST) -> bool:
    return any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        for node in ast.walk(annotation)
    )


_FILES = _target_files()


@pytest.mark.parametrize(
    "path", _FILES, ids=[str(p.relative_to(REPO_ROOT)) for p in _FILES]
)
def test_no_unguarded_pep604_annotations(path: Path):
    """A `X | None`-style annotation must be guarded by
    `from __future__ import annotations`, or it crashes at def-time on
    Python 3.8 — see lessons-learned 2026-09-04."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    if _has_future_annotations(tree):
        return
    offending = [n for n in _annotation_nodes(tree) if _has_pep604_union(n)]
    assert not offending, (
        f"{path}: PEP-604 `X | Y` annotation(s) without "
        f"`from __future__ import annotations` — crashes at import time on "
        f"Python 3.8 (lessons-learned 2026-09-04)."
    )


@pytest.mark.parametrize(
    "path", _FILES, ids=[str(p.relative_to(REPO_ROOT)) for p in _FILES]
)
def test_parses_under_python38_grammar(path: Path):
    """Best-effort net for other 3.9+/3.10+-only grammar (e.g. `match`
    statements) that would fail to even parse on Python 3.8."""
    src = path.read_text(encoding="utf-8")
    try:
        ast.parse(src, filename=str(path), feature_version=(3, 8))
    except SyntaxError as exc:
        pytest.fail(f"{path}: does not parse under Python 3.8 grammar: {exc}")
