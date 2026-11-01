"""The legacy summary must not move when the tiered section is added.

The goldens were produced by the aggregation code as it stood before the tiered
section existed, with the interval and McNemar helpers pinned so the bytes do
not depend on whether statsmodels is installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aggregate_fixtures import build_tiered_run, build_v1_run
from quoin.benchmarks.harness import aggregate

GOLDEN = Path(__file__).parent / "fixtures" / "aggregate_golden"


@pytest.fixture(autouse=True)
def _pin_statistics(monkeypatch):
    monkeypatch.setattr(aggregate, "_wilson_ci", lambda k, n, alpha=0.05: (0.1, 0.9))
    monkeypatch.setattr(aggregate, "_mcnemar_pvalue", lambda a, b: 0.5)


def _read(path):
    return path.read_bytes()


@pytest.mark.parametrize("mode", ["explicit", "from_manifest", "no_manifest"])
def test_non_tiered_suite_summary_is_byte_identical(tmp_path, mode):
    run_dir, run_id, suite_path = build_v1_run(tmp_path)
    run_path = run_dir / run_id
    if mode == "no_manifest":
        (run_path / "run-manifest.yaml").unlink()
    kwargs = {"suite_path": suite_path} if mode == "explicit" else {}
    aggregate.aggregate_run(run_dir, run_id, **kwargs)
    assert _read(run_path / "summary.json") == _read(GOLDEN / "v1_summary.json")
    assert _read(run_path / "summary.md") == _read(GOLDEN / "v1_summary.md")
    assert "tiered" not in json.loads((run_path / "summary.json").read_text())


def test_tiered_suite_keeps_the_legacy_summary_and_appends_its_section(tmp_path):
    run_dir, run_id, _ = build_tiered_run(tmp_path)
    run_path = run_dir / run_id
    aggregate.aggregate_run(run_dir, run_id)

    summary = json.loads((run_path / "summary.json").read_text())
    legacy = json.loads((GOLDEN / "tiered_legacy_summary.json").read_text())
    assert list(summary)[-1] == "tiered"
    without_tiered = {k: v for k, v in summary.items() if k != "tiered"}
    assert without_tiered == legacy
    assert list(without_tiered) == list(legacy)

    markdown = _read(run_path / "summary.md")
    legacy_md = _read(GOLDEN / "tiered_legacy_summary.md")
    assert markdown.startswith(legacy_md)
    assert b"## Tiered comparison" in markdown[len(legacy_md):]
