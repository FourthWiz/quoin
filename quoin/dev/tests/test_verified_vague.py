"""Offline tests for the vague-prompt suites, their vendored rewrites and the join report."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from aggregate_fixtures import OPUS, suite_sha, write_manifest, write_row
from quoin.benchmarks.scripts import pick_subset, vague_report
from quoin.benchmarks.scripts.run_benchmark import _resolve_suite_arg

BENCH = Path(pick_subset.__file__).resolve().parent.parent
SNAPSHOT = BENCH / "data" / "swebench-verified-snapshot.json"
PROBLEMS_BAL = BENCH / "data" / "swebench-verified-problems-tiered-balanced.json"
REWRITES = BENCH / "data" / "swebench-verified-vague-rewrites.json"
SUITE_VAGUE = BENCH / "suite-verified-vague.json"
SUITE_CEILING = BENCH / "suite-verified-vague-ceiling.json"
REPO_ROOT = BENCH.parent.parent


def _build(suite, rewrites=REWRITES, problems=PROBLEMS_BAL):
    return pick_subset.build_vague_suite(SNAPSHOT, problems, rewrites, 1729, suite)


def _problems():
    return json.loads(PROBLEMS_BAL.read_text(encoding="utf-8"))["problems"]


# --- generation ------------------------------------------------------------------

@pytest.mark.parametrize("name,path", [("verified-vague", SUITE_VAGUE),
                                       ("verified-vague-ceiling", SUITE_CEILING)])
def test_committed_suites_regenerate_byte_for_byte(name, path):
    assert pick_subset.render_suite(_build(name)) == path.read_text(encoding="utf-8")
    assert _resolve_suite_arg(name) == path.resolve()


def test_selection_is_a_tiered_subset_of_the_balanced_pool():
    vague, ceiling = _build("verified-vague"), _build("verified-vague-ceiling")
    pool = pick_subset.build_tiered_suite(SNAPSHOT, PROBLEMS_BAL, 1729, "tiered-balanced")
    pool_tier = {t["source_id"]: t["tier"] for t in pool["tasks"]}
    counts = {}
    for task in vague["tasks"]:
        assert pool_tier[task["source_id"]] == task["tier"]
        counts[task["tier"]] = counts.get(task["tier"], 0) + 1
    assert counts == {"easy": 6, "medium": 7, "large": 7}
    assert vague["tiered"]["paired_run"] is True
    assert {t: s["n"] for t, s in vague["tiered"]["tiers"].items()} == counts
    assert [t["source_id"] for t in vague["tasks"]] == [t["source_id"] for t in ceiling["tasks"]]
    assert [t["id"] for t in vague["tasks"]] == [t["id"] for t in ceiling["tasks"]]


def test_prompts_are_the_rewrites_and_the_ceiling_keeps_the_originals():
    problems = _problems()
    rewrites = json.loads(REWRITES.read_text(encoding="utf-8"))["rewrites"]
    for vague_task, ceiling_task in zip(_build("verified-vague")["tasks"],
                                        _build("verified-vague-ceiling")["tasks"]):
        sid = vague_task["source_id"]
        assert vague_task["description"] == rewrites[sid]["prompt"]
        assert ceiling_task["description"] == problems[sid]
        assert len(vague_task["description"]) < len(problems[sid])
        assert (vague_task["prompt_variant"], ceiling_task["prompt_variant"]) == ("vague", "original")


def test_vendoring_the_committed_prompts_reproduces_the_rewrites_file():
    doc = json.loads(REWRITES.read_text(encoding="utf-8"))
    draft = {iid: entry["prompt"] for iid, entry in doc["rewrites"].items()}
    selected = pick_subset.vague_selected_ids(SNAPSHOT, PROBLEMS_BAL, 1729)
    rebuilt = pick_subset.vague_rewrites_from_draft(
        draft, _problems(), selected, revision=doc["revision"],
        rewriter_model=doc["rewriter_model"], generated_on=doc["generated_on"],
    )
    assert pick_subset.render_suite(rebuilt) == REWRITES.read_text(encoding="utf-8")


def test_new_suite_files_are_kept_byte_exact_by_git():
    attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    for path in (SUITE_VAGUE, SUITE_CEILING):
        assert f"{path.relative_to(REPO_ROOT).as_posix()} -text" in attrs


# --- refusals --------------------------------------------------------------------

def _copy_rewrites(tmp_path, mutate):
    doc = json.loads(REWRITES.read_text(encoding="utf-8"))
    mutate(doc)
    path = tmp_path / "rewrites.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_rewrite_of_a_changed_statement_is_refused(tmp_path):
    problems = json.loads(PROBLEMS_BAL.read_text(encoding="utf-8"))
    sid = next(iter(json.loads(REWRITES.read_text(encoding="utf-8"))["rewrites"]))
    problems["problems"][sid] += " edited"
    changed = tmp_path / "problems.json"
    changed.write_text(json.dumps(problems), encoding="utf-8")
    with pytest.raises(pick_subset.TieredSuiteError, match="different problem statement"):
        _build("verified-vague", problems=changed)


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d["rewrites"].pop(sorted(d["rewrites"])[0]), "does not match the selection"),
    (lambda d: d["rewrites"].update({"extra__extra-1": {"original_sha256": "0", "prompt": "x"}}),
     "does not match the selection"),
    (lambda d: d.update(revision="0" * 40), "revision"),
    (lambda d: d.update(instructions=d["instructions"] + " More."), "instructions_sha256"),
    (lambda d: d.update(instructions="Other.", instructions_sha256=hashlib.sha256(b"Other.").hexdigest()),
     "different instructions"),
    (lambda d: d["rewrites"][sorted(d["rewrites"])[0]].update(prompt="x" * 301), "over 300"),
    (lambda d: d["rewrites"][sorted(d["rewrites"])[0]].update(prompt="line\n`code`"), "one paragraph"),
])
def test_incomplete_or_malformed_rewrites_are_refused(tmp_path, mutate, message):
    with pytest.raises(pick_subset.TieredSuiteError, match=message):
        _build("verified-vague", rewrites=_copy_rewrites(tmp_path, mutate))


# --- join report -----------------------------------------------------------------

def _run(tmp_path, name, suite_path, verdicts, quoin_route=None, briefs=None):
    """A paired run of `suite_path`; verdicts maps task id to (plain, quoin)."""
    run_path = tmp_path / name
    for task_id, (plain, quoin) in verdicts.items():
        write_row(run_path, "simple-claude", task_id, verdict=plain, wall=10, cost=1.0)
        write_row(run_path, "quoin-claude", task_id, verdict=quoin, wall=20, cost=2.0)
    for task_id, cls in (briefs or {}).items():
        brief = run_path / "quoin-claude" / task_id / "workflow_artifacts_evidence" / task_id / "brief.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(f"# {task_id}\n## Triage\nclass: {cls}\n## Request\nx\n", encoding="utf-8")
    write_manifest(run_path, run_id=name, suite_path=suite_path, suite_sha=suite_sha(suite_path),
                   simple_claude_model=OPUS, quoin_claude_model=OPUS, max_retries=0,
                   quoin_route=quoin_route)
    return run_path


def _verdicts(pattern):
    ids = [t["id"] for t in json.loads(SUITE_VAGUE.read_text(encoding="utf-8"))["tasks"]]
    return {tid: pattern(i) for i, tid in enumerate(ids)}


def test_report_joins_the_four_cells(tmp_path):
    # Plain passes every fourth vague task; quoin passes every second; both pass all originals.
    vague = _run(tmp_path, "vague", SUITE_VAGUE,
                 _verdicts(lambda i: ("pass" if i % 4 == 0 else "fail", "pass" if i % 2 == 0 else "fail")),
                 briefs={"vague_easy_000": "C3", "vague_easy_001": "C1"})
    ceiling = _run(tmp_path, "ceiling", SUITE_CEILING, _verdicts(lambda i: ("pass", "pass")))
    report = vague_report.build_report([vague_report.load_run(vague)], [vague_report.load_run(ceiling)])

    cells = report["cells"]
    assert cells["plain_vague"]["overall"]["passes"] == 5
    assert cells["quoin_vague"]["overall"]["passes"] == 10
    assert cells["plain_original"]["overall"]["pass_rate"] == 1.0
    assert cells["quoin_vague"]["tiers"]["easy"]["n"] == 6
    head = report["headline"]
    assert head["n"] == 20 and head["pass_rate_diff"] == pytest.approx(0.25)
    assert (head["sign_test"]["positive"], head["sign_test"]["negative"]) == (5, 0)
    assert head["mean_usd_diff"] == pytest.approx(1.0)
    assert report["recovery"]["vagueness_loss"] == pytest.approx(0.75)
    assert report["recovery"]["recovered_share"] == pytest.approx(1 / 3)
    assert report["recovery"]["difference_in_differences"] == pytest.approx(0.25)
    classes = report["triage"]["classes"]
    assert (classes["C3"]["tasks"], classes["C1"]["tasks"], classes["unknown"]["tasks"]) == (1, 1, 18)
    assert report["triage"]["sessions"] == 20 and report["triage"]["not_c1_share"] == pytest.approx(0.05)
    assert "## Headline: quoin-vague minus plain-vague" in vague_report.render_markdown(report)


def test_report_runs_without_a_ceiling_and_refuses_mismatched_runs(tmp_path, capsys):
    vague = _run(tmp_path, "vague", SUITE_VAGUE, _verdicts(lambda i: ("fail", "pass")))
    assert vague_report.main([str(vague)]) == 0
    assert "Ceiling runs: not given" in capsys.readouterr().out

    lean = _run(tmp_path, "ceiling", SUITE_CEILING, _verdicts(lambda i: ("pass", "pass")), quoin_route="lean")
    assert vague_report.main([str(vague), "--ceiling-run", str(lean)]) == 2
    assert "quoin_route differs" in capsys.readouterr().err
    # The vague run passed where the ceiling run is expected.
    assert vague_report.main([str(vague), "--ceiling-run", str(vague)]) == 2
    assert vague_report.main([str(vague), "--ceiling-run", str(lean), "--allow-mixed"]) == 0


def test_report_pools_repeats_and_pairs_within_each_run(tmp_path):
    r1 = _run(tmp_path, "vague-r1", SUITE_VAGUE, _verdicts(lambda i: ("pass", "fail")))
    r2 = _run(tmp_path, "vague-r2", SUITE_VAGUE, _verdicts(lambda i: ("fail", "pass")))
    report = vague_report.build_report([vague_report.load_run(r1), vague_report.load_run(r2)], [])
    assert report["cells"]["plain_vague"]["overall"]["n"] == 40
    head = report["headline"]
    assert head["n"] == 40 and head["pass_rate_diff"] == pytest.approx(0.0)
    assert (head["sign_test"]["positive"], head["sign_test"]["negative"]) == (20, 20)
    assert report["recovery"] is None


def test_report_refuses_a_run_whose_suite_file_changed(tmp_path, capsys):
    suite = tmp_path / "suite-verified-vague.json"
    suite.write_bytes(SUITE_VAGUE.read_bytes())
    vague = _run(tmp_path, "vague", suite, _verdicts(lambda i: ("fail", "pass")))
    suite.write_text(suite.read_text(encoding="utf-8").replace("Parsing some", "Parsing a few"),
                     encoding="utf-8")
    assert vague_report.main([str(vague)]) == 2
    assert "suite file changed since the run" in capsys.readouterr().err
