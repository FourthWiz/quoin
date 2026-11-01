"""The harness modules under test must come from this tree, not an installed copy."""
from __future__ import annotations

from pathlib import Path

from quoin.benchmarks.harness import aggregate
from quoin.benchmarks.harness.cells import quoin_claude, simple_claude

BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"


def _under_tree(module) -> bool:
    return BENCHMARKS in Path(module.__file__).resolve().parents


def test_aggregate_resolves_under_the_repo_tree():
    assert _under_tree(aggregate), aggregate.__file__


def test_quoin_claude_cell_resolves_under_the_repo_tree():
    assert _under_tree(quoin_claude), quoin_claude.__file__


def test_simple_claude_cell_resolves_under_the_repo_tree():
    assert _under_tree(simple_claude), simple_claude.__file__
