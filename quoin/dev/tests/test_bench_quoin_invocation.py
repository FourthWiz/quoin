"""The quoin arm starts the pipeline with a slash command, and its on-disk evidence is recorded."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fakes"))
from fake_claude import make_fake_claude, read_log  # noqa: E402
from test_cell_paired_params import INIT, RESULT, _invoke, _session_env  # noqa: E402

from quoin.benchmarks.harness.cells import quoin_claude, simple_claude  # noqa: E402
from quoin.benchmarks.harness.cells.simple_claude import _build_prompt  # noqa: E402

TASK = {"id": "t1", "source": "swebench_verified", "source_id": "org__repo-1",
        "description": "Fix it.", "repo": "org/repo", "base_commit": "0" * 40}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("QUOIN_BENCHMARK_GATE", raising=False)
    monkeypatch.setattr(
        quoin_claude, "_initialize_workflow_artifacts",
        lambda *a, **k: {"ok": True, "reason": None, "returncode": 0},
    )


def _argv_of(cell, tmp_path, cap):
    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"emit": RESULT}]})
    _invoke(cell, tmp_path, bin_dir, cap=cap, session_env=_session_env(bin_dir))
    return read_log(bin_dir)[0]["argv"]


def test_quoin_prompt_is_the_run_slash_command_plus_the_base_prompt(tmp_path):
    argv = _argv_of(quoin_claude, tmp_path, 5.5)
    base = _build_prompt(TASK)
    assert argv[-1] == quoin_claude.QUOIN_RUN_PREFIX + base
    assert argv[-1].startswith("/run --autonomous This session")
    assert "the /run pipeline is required" in argv[-1]
    assert "do not fix the issue directly" in argv[-1]
    assert argv[-1].endswith(base)
    assert "Use /run --autonomous end-to-end" not in argv[-1]


def test_quoin_prefix_carries_no_run_control_tokens():
    # /run strips these from anywhere in its input; the prefix must not add any.
    body = quoin_claude.QUOIN_RUN_PREFIX[len("/run --autonomous "):]
    for token in ("hard:", "small:", "fast:", "strict:", "medium:", "large:", "max_rounds:", "--resume", "["):
        assert token not in body


def test_route_env_selects_heavy_pipeline_prefix(monkeypatch):
    monkeypatch.delenv(quoin_claude.ENV_BENCH_ROUTE, raising=False)
    assert quoin_claude._run_prefix() == quoin_claude.QUOIN_RUN_PREFIX
    monkeypatch.setenv(quoin_claude.ENV_BENCH_ROUTE, "hard")
    assert quoin_claude._run_prefix() == quoin_claude.QUOIN_HARD_RUN_PREFIX
    assert quoin_claude._run_prefix().startswith("/run --autonomous hard: small: ")


def test_light_route_detected_from_brief(tmp_path):
    task = tmp_path / "fix-thing"
    task.mkdir()
    (task / "brief.md").write_text("# fix-thing\n", encoding="utf-8")
    got = quoin_claude._pipeline_diagnostics(tmp_path)
    assert got["pipeline_started"] is True
    assert got["workflow_route"]["route"] == "light"


def test_plain_prompt_is_unchanged(tmp_path):
    argv = _argv_of(simple_claude, tmp_path, 5.5)
    assert argv[-1] == _build_prompt(TASK)


def _cap_forms(cap):
    forms = {str(cap), "%g" % cap, "%.2f" % cap}
    if float(cap).is_integer():
        forms.add(str(int(cap)))
    return forms | {"$" + f for f in forms}


@pytest.mark.parametrize("cap", [5.5, 70.0])
@pytest.mark.parametrize("cell", [simple_claude, quoin_claude], ids=["plain", "quoin"])
def test_prompts_never_state_the_cap_but_the_flag_is_armed(cell, cap, tmp_path):
    argv = _argv_of(cell, tmp_path, cap)
    prompt = argv[-1]
    for form in _cap_forms(cap):
        pattern = r"(?<![\w.])" + re.escape(form) + r"(?![\w.])"
        assert not re.search(pattern, prompt), form
    assert "--max-budget-usd" in argv


# -- on-disk pipeline evidence ----------------------------------------------------


def _tree(tmp_path, files):
    work = tmp_path / "work"
    for name, text in files.items():
        path = work / ".workflow_artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return work


def _capture(tmp_path, files):
    work = _tree(tmp_path, files) if files is not None else tmp_path / "work"
    work.mkdir(exist_ok=True)
    return quoin_claude._capture_workflow_artifacts(work, tmp_path / "out", "t1")


def test_empty_tree_has_no_pipeline_evidence(tmp_path):
    got = _capture(tmp_path, None)
    assert got["pipeline_started"] is False
    assert got["workflow_route"] == {"route": "unknown", "profile": None}
    assert got["phase_files"] == []


def test_orchestrator_ledger_row_alone_marks_the_pipeline_started(tmp_path):
    got = _capture(tmp_path, {"task/cost-ledger.md": "u | d | run-orchestrator | opus | x | n | 1\n"})
    assert got["pipeline_started"] is True
    assert got["has_current_plan_md"] is False
    assert got["phase_files"] == ["task/cost-ledger.md"]
    assert got["workflow_route"]["route"] == "unknown"


def test_fast_stub_plan_is_the_fast_route(tmp_path):
    got = _capture(tmp_path, {"task/current-plan.md": "---\nprovenance: fast-path-triage\n---\nplan\n"})
    assert got["workflow_route"]["route"] == "fast"
    assert got["has_current_plan_md"] is True


def test_triage_decision_names_the_route_and_profile(tmp_path):
    got = _capture(tmp_path, {
        "task/triage-decision.md": "route: full\nTask profile: Medium\n",
        "task/current-plan.md": "plan\n",
    })
    assert got["workflow_route"] == {"route": "full", "profile": "Medium"}


def test_a_plan_without_a_fast_marker_is_the_full_route(tmp_path):
    got = _capture(tmp_path, {"task/current-plan.md": "plan\n"})
    assert got["workflow_route"]["route"] == "full"


def test_run_state_file_marks_started_and_gives_profile(tmp_path):
    got = _capture(tmp_path, {"memory/run-state-task.json": json.dumps({"profile": "Small"})})
    assert got["pipeline_started"] is True
    assert got["workflow_route"]["profile"] == "Small"


def test_diagnostics_land_in_the_cell_result(tmp_path):
    bin_dir = make_fake_claude(tmp_path, {"steps": [{"emit": INIT}, {"emit": RESULT}]})
    result = _invoke(quoin_claude, tmp_path, bin_dir, session_env=_session_env(bin_dir),
                     run_output_dir=tmp_path / "evidence")
    assert result["extra"]["workflow_pipeline_started"] is False
    assert result["extra"]["workflow_route"]["route"] == "unknown"
    assert result["extra"]["workflow_artifacts_phase_files"] == []


def test_failed_evidence_copy_is_not_reported_as_a_missing_plan(tmp_path, monkeypatch):
    work = _tree(tmp_path, {"task/current-plan.md": "plan\n"})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(quoin_claude.shutil, "copytree", boom)
    got = quoin_claude._capture_workflow_artifacts(work, tmp_path / "out", "t1")
    assert got["captured"] is False
    assert got["copy_failed"] is True
