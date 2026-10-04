"""Parity between the workflow-tasks mod's stage logic and status_graph.detect_phase.

The mod's pure TypeScript (hooks/tasks.ts) re-implements the base phase
detection of core/scripts/status_graph.py. Both sides read one shared fixture
table (hooks/stage-fixtures.ts). This file runs the Python half, always: each
fixture tree is materialized on disk and handed to detect_phase. The
TypeScript half (detectPhaseCompat, stage derivation, next command) runs in
test_workflow_tasks_plugin_ts.py and skips where the `claude` CLI is absent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HOOKS = REPO / "quoin" / "plugins" / "workflow-tasks" / "hooks"
FIXTURES_TS = HOOKS / "stage-fixtures.ts"
TASKS_TEST_TS = HOOKS / "tasks.test.ts"

_CORE = REPO / "quoin" / "core" / "scripts" / "status_graph.py"
_SPEC = importlib.util.spec_from_file_location("_quoin_core_status_graph_parity", _CORE)
_SG = importlib.util.module_from_spec(_SPEC)
sys.modules["_quoin_core_status_graph_parity"] = _SG
_SPEC.loader.exec_module(_SG)

BASE_SECONDS_RE = re.compile(r"FIXTURE_BASE_SECONDS\s*=\s*(\d+)")
REQUIRED_KEYS = {"id", "tree", "task", "expect"}
EXPECT_KEYS = {"detectPhase", "stage", "command"}


def _load_fixtures():
    text = FIXTURES_TS.read_text(encoding="utf-8")
    start = text.index("// BEGIN STAGE FIXTURES") + len("// BEGIN STAGE FIXTURES")
    end = text.index("// END STAGE FIXTURES")
    base = int(BASE_SECONDS_RE.search(text).group(1))
    return json.loads(text[start:end]), base


FIXTURES, BASE_SECONDS = _load_fixtures()
WITH_PHASE = [f for f in FIXTURES if f["expect"]["detectPhase"] is not None]


def _materialize(root: Path, tree: dict) -> Path:
    artifacts = root / ".workflow_artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    for rel, value in tree.items():
        target = artifacts / rel
        if rel.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        text = value["text"] if isinstance(value, dict) else value
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if isinstance(value, dict):
            stamp = BASE_SECONDS + value["mtime"]
            os.utime(target, (stamp, stamp))
    return artifacts


def test_fixture_table_is_well_formed():
    ids = [f["id"] for f in FIXTURES]
    assert len(ids) == len(set(ids)), "fixture ids must be unique"
    for fixture in FIXTURES:
        assert REQUIRED_KEYS <= set(fixture), fixture["id"]
        assert EXPECT_KEYS <= set(fixture["expect"]), fixture["id"]
        for value in fixture["tree"].values():
            assert isinstance(value, str) or {"text", "mtime"} <= set(value), fixture["id"]


@pytest.mark.parametrize("fixture", WITH_PHASE, ids=[f["id"] for f in WITH_PHASE])
def test_detect_phase_matches_fixture(fixture, tmp_path):
    artifacts = _materialize(tmp_path, fixture["tree"])
    phase_dir = artifacts / fixture.get("phaseDir", fixture["task"])
    result = _SG.detect_phase(phase_dir, probe_git=False)
    assert result.phase == fixture["expect"]["detectPhase"]


def test_phase_vocabulary_is_covered():
    # `implement` is only reachable by probing git, which the mod never does.
    needed = set(_SG._PHASE_TO_NODE) - {"implement"}
    covered = {f["expect"]["detectPhase"] for f in FIXTURES}
    assert needed <= covered, "no fixture for phases: %s" % sorted(needed - covered)


def test_finalized_path_parts_are_limited_to_known_cases():
    allowed = {
        "finalized-folder-is-done",
        "multi-stage-1-archived-stage-2-not-started",
        "multi-stage-all-stages-archived",
        "multi-stage-final-stage-review-approved",
        "multi-stage-live-stage-implement-gate-fail",
    }
    for fixture in FIXTURES:
        phase_dir = fixture.get("phaseDir", fixture["task"])
        assert "finalized" not in Path(phase_dir).parts or fixture["id"] == "finalized-folder-is-done", fixture["id"]
        has_finalized = any("finalized" in Path(k).parts for k in fixture["tree"])
        assert not has_finalized or fixture["id"] in allowed, fixture["id"]


def test_typescript_test_consumes_the_shared_table():
    # tasks.test.ts must iterate the same table, or the two sides can drift
    # while both stay green.
    text = TASKS_TEST_TS.read_text(encoding="utf-8")
    assert re.search(r"import\s*\{[^}]*\bSTAGE_FIXTURES\b[^}]*\}\s*from\s*'\./stage-fixtures(\.ts)?'", text)
    assert re.search(r"for\s*\(\s*const\s+\w+\s+of\s+STAGE_FIXTURES\s*\)", text)
