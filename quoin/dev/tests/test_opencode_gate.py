"""The workflow gate: path checks, verdict parsing, critic status (part one)."""
from __future__ import annotations

import os
import shutil

import pytest

import _opencode_gate_helpers as h
import _opencode_helpers as helpers
from quoin.opencode_adapter import gate

SOURCE = helpers.SOURCE_DIR
GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# -- verdict parsing -------------------------------------------------------

CRITIC = ("PASS", "REVISE")
REVIEW = ("APPROVED", "CHANGES_REQUESTED", "BLOCKED")


@pytest.mark.parametrize("text,allowed,expected", [
    ("## Verdict\n\n`<verdict>PASS</verdict>`\n", CRITIC, "PASS"),
    ("## Verdict: REVISE\n\ntext\n", CRITIC, "REVISE"),
    ("## Verdict: REVISE\n\n## Summary\n\n`<verdict>PASS</verdict>`\n", CRITIC, "REVISE"),
    ("## Verdict\n\n`<verdict>PASS</verdict>`\n\n## Summary\n\n## Verdict: REVISE\n", CRITIC, "PASS"),
    ("## Verdict\n\n`<verdict>PASS</verdict>` and `<verdict>REVISE</verdict>`\n", CRITIC, None),
    ("## Verdict\n\n```\n`<verdict>PASS</verdict>`\n```\n", CRITIC, None),
    ("## Summary\n\n`<verdict>PASS</verdict>`\n\n## Verdict\n\ntext\n", CRITIC, None),
    ("## Verdict\n\n`<verdict>pass</verdict>`\n", CRITIC, None),
    ("## Verdict: PASS and more\n", CRITIC, None),
    ("## Verdict\n\nAPPROVED\n", REVIEW, "APPROVED"),
    ("## Verdict\n\nAPPROVED\n\nbecause it is fine\n", REVIEW, None),
    ("## Verdict\n\napproved\n", REVIEW, None),
    ("## Verdict\n\n```\nAPPROVED\n```\n", REVIEW, None),
    ("## Verdict\n\nAPPROVED — N/A, no active task\n", REVIEW, None),
    ("## Verdict\n\nPASS\n", REVIEW, None),
    ("## Dimension Verdicts\n\n| a | b |\n|---|---|\n| x | APPROVED |\n", REVIEW, None),
    ("## Summary\n\ntext\n\n## Verdict\n\nAPPROVED\n\n## Dimension Verdicts\n\n| a | b |\n|---|---|\n| x | y |\n",
     REVIEW, "APPROVED"),
    ("", REVIEW, None),
    ("## Verdict\n\nBLOCKED\n", REVIEW, "BLOCKED"),
])
def test_parse_verdict(text, allowed, expected):
    assert gate.parse_verdict(text, allowed) == expected


# -- critic status ---------------------------------------------------------


def sdir_with(tmp_path, *responses):
    sdir = tmp_path / "stage-1"
    sdir.mkdir()
    for number, text in responses:
        (sdir / ("critic-response-%d.md" % number)).write_text(text)
    return sdir


def status(entry, origin, sdir, settings=None, root=None):
    return gate.critic_status(entry, origin, sdir, settings or {}, project_root=root)


def test_coordinator_never_falls_back_to_disk(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    assert status({"critic_responses": []}, "coordinator", sdir) == [
        ("FAIL", "critic-missing", "no critic response was recorded for this plan")]


def test_critic_not_required_warns(tmp_path):
    sdir = sdir_with(tmp_path)
    result = status({}, "coordinator", sdir, {"critic_required": False})
    assert [(s, c) for s, c, _ in result] == [("WARN", "critic-not-run")]


def test_adopted_with_nothing_warns_and_phase_run_fails(tmp_path):
    sdir = sdir_with(tmp_path)
    assert status({}, "adopted", sdir)[0][:2] == ("WARN", "critic-not-run")
    assert status({}, "phase-run", sdir)[0][:2] == ("FAIL", "critic-missing")


def test_disk_fallback_for_phase_run_passes(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_REVISE), (2, h.CRITIC_PASS))
    assert status({}, "phase-run", sdir) == [("PASS", "", "")]


def test_recorded_responses_win_over_disk(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    other = tmp_path / "other.md"
    other.write_text(h.CRITIC_REVISE)
    result = status({"critic_responses": [str(other)]}, "coordinator", sdir)
    assert result[0][:2] == ("FAIL", "critic-not-converged")


def test_revise_last_does_not_converge(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS), (2, h.CRITIC_REVISE))
    assert status({}, "adopted", sdir)[0][:2] == ("FAIL", "critic-not-converged")


def test_six_responses_exceed_the_cap(tmp_path):
    sdir = sdir_with(tmp_path, *[(n, h.CRITIC_REVISE) for n in range(1, 6)], (6, h.CRITIC_PASS))
    result = status({}, "adopted", sdir)
    assert [(s, c) for s, c, _ in result] == [("FAIL", "critic-not-converged")]
    assert status({}, "adopted", sdir, {"max_critic_rounds": 6}) == [("PASS", "", "")]


def test_unparseable_last_response(tmp_path):
    sdir = sdir_with(tmp_path, (1, "## Verdict\n\n**PASS**\n"))
    assert status({}, "adopted", sdir)[0][:2] == ("FAIL", "verdict-unparseable")


def test_numeric_ordering_of_responses(tmp_path):
    sdir = sdir_with(tmp_path, (9, h.CRITIC_REVISE), (10, h.CRITIC_PASS))
    assert status({}, "adopted", sdir) == [("PASS", "", "")]


# -- stage and artifact paths ---------------------------------------------


def test_stage_dir_requires_a_listed_stage(tmp_path):
    h.write(h.task_dir(tmp_path, "t1") / "architecture.md", h.ARCHITECTURE)
    assert gate.stage_dir(tmp_path, "t1", 2, SOURCE) == h.task_dir(tmp_path, "t1") / "stage-2"
    assert gate.stage_dir(tmp_path, "t1", None, SOURCE) == h.task_dir(tmp_path, "t1")
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 3, SOURCE)


def test_stage_dir_without_architecture(tmp_path):
    h.task_dir(tmp_path, "t1").mkdir(parents=True)
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 1, SOURCE)


def test_stage_row_with_em_dash_heading_is_not_a_row(tmp_path):
    text = h.ARCHITECTURE.replace("1. S-1: First stage", "1. S-1 — First stage")
    h.write(h.task_dir(tmp_path, "t1") / "architecture.md", text)
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 1, SOURCE)


def test_symlinked_stage_dir_is_unresolved(tmp_path):
    base = h.task_dir(tmp_path, "t1")
    h.write(base / "architecture.md", h.ARCHITECTURE)
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, base / "stage-1")
    with pytest.raises(gate.PathUnresolved):
        gate.stage_dir(tmp_path, "t1", 1, SOURCE)


def test_review_number_chosen_numerically(tmp_path):
    base = h.build_task(tmp_path, review=None)
    for n in (2, 9, 10):
        h.write(base / "stage-1" / ("review-%d.md" % n), h.REVIEW)
    paths, missing = gate.expected_artifacts(tmp_path, "t1", 1, "review", {}, base / "stage-1")
    assert [p.name for p in paths] == ["review-10.md"] and missing == []


def test_missing_artifacts_are_named(tmp_path):
    base = h.build_task(tmp_path, review=None)
    assert gate.expected_artifacts(tmp_path, "t1", 1, "review", {}, base / "stage-1") == ([], ["review-N.md"])
    paths, missing = gate.expected_artifacts(tmp_path, "t1", None, "discover", {}, base)
    assert paths == [] and len(missing) == 3 and all(m.startswith(".workflow_artifacts/memory/") for m in missing)
    assert gate.expected_artifacts(tmp_path, "t1", 1, "implement", {}, base / "stage-1") == ([], [])


# -- validator subprocess --------------------------------------------------


def test_validator_pass_and_fail(tmp_path):
    base = h.build_task(tmp_path)
    assert gate.run_validator(tmp_path, SOURCE, base / "stage-1" / "current-plan.md") is None
    bad = h.write(base / "stage-1" / "current-plan.md", h.PLAN.replace("## Risks", "## Surprise"))
    detail = gate.run_validator(tmp_path, SOURCE, bad)
    assert detail and "V-02" in detail and "Surprise" in detail


def test_validator_unavailable_fails_closed(tmp_path):
    base = h.build_task(tmp_path)
    plan = base / "stage-1" / "current-plan.md"
    assert gate.run_validator(tmp_path, tmp_path / "nowhere", plan) == "validator-unavailable"
    broken = tmp_path / "src"
    shutil.copytree(SOURCE / "core" / "scripts", broken / "core" / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    assert gate.run_validator(tmp_path, broken, plan) == "validator-unavailable"
    (broken / "memory").mkdir()
    (broken / "memory" / "format-kit.sections.json").write_text("{not json")
    assert gate.run_validator(tmp_path, broken, plan) == "validator-unavailable"


def test_local_redaction_matches_the_launcher():
    from quoin.opencode_adapter import launch_env

    samples = ["token sk-abcdefghijklmnopqrstuvwx123456", "plain text", "ghp_" + "a" * 36, "Bearer abcdefghijklmnopqrstuvwxyz0123"]
    for text in samples:
        assert gate._redact(text) == launch_env.Redactor()(text)


def test_core_loading_is_cached():
    first = gate.load_core(SOURCE, "path_resolve")
    assert gate.load_core(SOURCE, "path_resolve") is first
    assert hasattr(gate.load_core(SOURCE, "handoff_validate"), "validate")
