"""Tests for the bare suite alias accepted by run_benchmark.py --suite."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.scripts import run_benchmark

BENCH = Path(run_benchmark.__file__).resolve().parent.parent
SCRIPT = BENCH / "scripts" / "run_benchmark.py"


def test_alias_resolves_to_benchmarks_dir_regardless_of_cwd(tmp_path, monkeypatch):
    (tmp_path / "tiered-small").write_text("decoy")
    monkeypatch.chdir(tmp_path)
    assert run_benchmark._resolve_suite_arg("tiered-small") == BENCH / "suite-tiered-small.json"


def test_balanced_alias_resolves_to_the_committed_suite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = run_benchmark._resolve_suite_arg("tiered-balanced")
    assert path == BENCH / "suite-tiered-balanced.json"
    assert path.is_file()


@pytest.mark.parametrize(
    "value",
    ["quoin/benchmarks/suite-v1.json", "./x", "suite-v1.json", "/abs/path/suite.json"],
)
def test_paths_pass_through_unchanged(value):
    assert run_benchmark._resolve_suite_arg(value) == Path(value)


def test_unknown_alias_lists_known_ones(capsys):
    with pytest.raises(SystemExit) as exc:
        run_benchmark._resolve_suite_arg("nope")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "tiered-small" in err and "v1" in err


def test_unknown_alias_ignores_sync_copies(tmp_path, capsys):
    (tmp_path / "suite-a.json").write_text("{}")
    (tmp_path / "suite-a 2.json").write_text("{}")
    with pytest.raises(SystemExit):
        run_benchmark._resolve_suite_arg("nope", tmp_path)
    err = capsys.readouterr().err
    assert err.strip().endswith("known aliases: a")


def test_dry_run_subprocess_resolves_alias(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--suite", "tiered-small",
         "--cells", "simple-claude,quoin-claude", "--run-id", "alias-smoke", "--dry-run"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    out = proc.stdout + proc.stderr
    assert str(BENCH / "suite-tiered-small.json") in out
    assert "(9 tasks)" in out
