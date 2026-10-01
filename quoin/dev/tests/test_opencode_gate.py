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
    ("## Verdict\n\n`<verdict>PASS</verdict>`\n\n## Summary\n\n## Verdict: REVISE\n", CRITIC, None),
    ("## Verdict\n\n`<verdict>PASS</verdict>`\n\n## Summary\n\n## Verdict: PASS\n", CRITIC, "PASS"),
    ("## Verdict\n\n<verdict>APPROVED</verdict>\n", REVIEW, "APPROVED"),
    ("## Verdict\n\n<verdict>PASS</verdict>\n", CRITIC, "PASS"),
    ("## Verdict\n\n<verdict>CHANGES_REQUESTED</verdict>\n\nThe plan needs work.\n", REVIEW, "CHANGES_REQUESTED"),
    ("## Verdict\n\n<verdict>APPROVED</verdict> with one note on naming.\n", REVIEW, "APPROVED"),
    ("## Verdict\n\n<verdict> PASS </verdict>\n", CRITIC, "PASS"),
    ("## Verdict\n\n<verdict>PASS</verdict> and <verdict>REVISE</verdict>\n", CRITIC, None),
    ("## Verdict\n\n<verdict>PASS</verdict>\n<verdict>PASS</verdict>\n", CRITIC, "PASS"),
    ("## Verdict\n\n<verdict></verdict>\n", CRITIC, None),
    ("## Verdict\n\nBLOCKED\n\n## Notes\n\n## Verdict: APPROVED\n", REVIEW, None),
    ("## Verdict\n\nBLOCKED\n\n## Verdict: BLOCKED\n", REVIEW, "BLOCKED"),
    ("## Verdict\n\n<verdict>BLOCKED</verdict>\n\n## Verdict: APPROVED\n", REVIEW, None),
    ("## Verdict\n\n~~~\n<verdict>PASS</verdict>\n~~~\n", CRITIC, None),
    ("## Verdict\n\n<!-- <verdict>REVISE</verdict> -->\n<verdict>PASS</verdict>\n", CRITIC, "PASS"),
    ("## Verdict\n\n<!--\n<verdict>REVISE</verdict>\n-->\n<verdict>PASS</verdict>\n", CRITIC, "PASS"),
    ("## Verdict: PASS\n\n<!--\n## Verdict: REVISE\n-->\n", CRITIC, "PASS"),
    ("## Verdict\n\n```\n## Verdict: REVISE\n```\n<verdict>PASS</verdict>\n", CRITIC, "PASS"),
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
    ("## Verdict\n\nCHANGES_REQUESTED\n\nOnce fixed this becomes\n<verdict>APPROVED</verdict>\n", REVIEW, None),
    ("## Verdict\n\n**CHANGES_REQUESTED**\n\n<verdict>APPROVED</verdict>\n", REVIEW, None),
    ("## Verdict\n\n    <verdict>APPROVED</verdict>\n\nCHANGES_REQUESTED\n", REVIEW, None),
    ("## Verdict\n\nREVISE\n\nlater\n<verdict>PASS</verdict>\n", CRITIC, None),
    ("## Verdict\n\n> REVISE\n<verdict>PASS</verdict>\n", CRITIC, None),
    ("## Verdict\n\n<verdict>APPROVED</verdict>\n\nAPPROVED\n", REVIEW, "APPROVED"),
])
def test_parse_verdict(text, allowed, expected):
    assert gate.parse_verdict(text, allowed) == expected


REAL_REVIEW = """---
task: demo
stage: 1
round: 1
verdict: APPROVED
---
## For human

Everything lines up.

## Summary

Stage one is done.

## Verdict

<verdict>APPROVED</verdict>

## Plan Compliance

- all tasks done
"""

REAL_CRITIC = """## Summary

The plan holds up.

## Verdict

<verdict>PASS</verdict>

## Issues Found

None.
"""


def test_parse_verdict_real_skill_shapes():
    assert gate.parse_verdict(REAL_REVIEW, REVIEW) == "APPROVED"
    assert gate.parse_verdict(REAL_CRITIC, CRITIC) == "PASS"
    assert gate.parse_verdict(REAL_REVIEW.replace("APPROVED</verdict>", "BLOCKED</verdict>"), REVIEW) == "BLOCKED"


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
    other = sdir / "critic-response-9.md"
    other.write_text(h.CRITIC_REVISE)
    result = status({"critic_responses": [str(other)]}, "coordinator", sdir)
    assert result[0][:2] == ("FAIL", "critic-not-converged")


@pytest.mark.parametrize("where", ["outside", "dotdot", "relative-outside"])
def test_recorded_response_outside_the_task_folder_is_refused(tmp_path, where):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    outside = tmp_path.parent / (tmp_path.name + "-outside.md")
    outside.write_text(h.CRITIC_PASS)
    value = {
        "outside": str(outside),
        "dotdot": str(sdir / ".." / ".." / outside.name),
        "relative-outside": "../" + outside.name,
    }[where]
    result = status({"critic_responses": [value]}, "coordinator", sdir, root=tmp_path)
    assert [(s, c) for s, c, _ in result] == [("FAIL", "path-unresolved")]


def test_recorded_response_through_a_symlink_is_refused(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    real = tmp_path / "real"
    real.mkdir()
    (real / "r.md").write_text(h.CRITIC_PASS)
    (sdir / "link").symlink_to(real)
    result = status({"critic_responses": [str(sdir / "link" / "r.md")]}, "coordinator", sdir, root=tmp_path)
    assert [(s, c) for s, c, _ in result] == [("FAIL", "path-unresolved")]


def test_read_text_refuses_a_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(str(fifo))
    assert gate._read_text(fifo, 1024) is None


def test_adopt_command_quotes_its_arguments():
    text = gate.adopt_command("t1", 1, "plan", "/my path/with space")
    assert "'/my path/with space'" in text


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


def test_recorded_critic_outside_stage_or_misnamed_is_refused(tmp_path):
    sdir = sdir_with(tmp_path, (1, h.CRITIC_PASS))
    sibling = sdir.parent / "stage-9"
    sibling.mkdir(exist_ok=True)
    (sibling / "critic-response-1.md").write_text(h.CRITIC_PASS)
    result = status({"critic_responses": [str(sibling / "critic-response-1.md")]}, "coordinator", sdir)
    assert result[0][:2] == ("FAIL", "path-unresolved")
    (sdir / "notes.md").write_text(h.CRITIC_PASS)
    result = status({"critic_responses": [str(sdir / "notes.md")]}, "coordinator", sdir)
    assert result[0][:2] == ("FAIL", "path-unresolved")
