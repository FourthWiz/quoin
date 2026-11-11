"""Tests of the multi-run report over fixture tiered runs."""
from __future__ import annotations

import json
import shutil

import pytest

from aggregate_fixtures import OPUS, SONNET, build_tiered_run, write_row
from quoin.benchmarks.scripts import multi_run_report as mrr

PLAIN, QUOIN = "simple-claude", "quoin-claude"


def _route(name):
    return {"workflow_route": {"route": name}}


def _runs(tmp_path, n=3, **manifest):
    """Three repeats of the five-task fixture with every row present.

    Plain solves e1 in every run and e2 only in r1; quoin solves e1 and e2 in
    every run. m1/m2 pass for neither arm, l1 passes for quoin in r1 only.
    The quoin arm takes the light route on e*, direct on m*, and on l1 light
    in r1 but direct afterwards.
    """
    paths = []
    for i in range(1, n + 1):
        run_dir, run_id, _suite = build_tiered_run(tmp_path, run_id=f"R-r{i}", quoin_route="lean",
                                                   **manifest)
        run_path = run_dir / run_id
        plain = {"e1": "pass", "e2": "pass" if i == 1 else "fail", "m1": "fail", "m2": "fail",
                 "l1": "fail"}
        quoin = {"e1": "pass", "e2": "pass", "m1": "fail", "m2": "fail",
                 "l1": "pass" if i == 1 else "fail"}
        routes = {"e1": "light", "e2": "light", "m1": "direct", "m2": "direct",
                  "l1": "light" if i == 1 else "direct"}
        for task_id in plain:
            write_row(run_path, PLAIN, task_id, verdict=plain[task_id], wall=1, cost=0.1)
            write_row(run_path, QUOIN, task_id, verdict=quoin[task_id], wall=1, cost=0.1,
                      extra=_route(routes[task_id]))
        paths.append(run_path)
    return paths


def test_sign_test_and_wilcoxon_known_values():
    assert mrr.sign_test([0.5, 0.5, 0.5])["p_value"] == pytest.approx(0.25)
    assert mrr.sign_test([0, 0])["p_value"] is None
    assert mrr.sign_test([1, -1])["p_value"] == pytest.approx(1.0)
    # Five positive differences of distinct size: W+ = 15, p = 2/32.
    w = mrr.wilcoxon_signed_rank([0.1, 0.2, 0.3, 0.4, 0.5])
    assert w["w_plus"] == 15 and w["p_value"] == pytest.approx(0.0625)
    # Ties share mid-ranks: |d| = 1,1,2 -> ranks 1.5,1.5,3; W+ = 4.5.
    t = mrr.wilcoxon_signed_rank([1, -1, 2])
    assert t["w_plus"] == 4.5 and t["w_minus"] == 1.5
    assert t["p_value"] == pytest.approx(0.75)


def test_equal_differences_from_different_pass_counts_tie():
    def rec(task_id, plain, quoin, runs=3):
        return {"task_id": task_id, "runs": runs, "passes": {PLAIN: plain, QUOIN: quoin},
                "rate": {PLAIN: plain / runs, QUOIN: quoin / runs}}

    # 3/3 - 2/3 and 1/3 - 0/3 are the same difference; as floats they are not equal.
    s = mrr.summarize([rec("a", 2, 3), rec("b", 0, 1), rec("c", 1, 0)])
    assert s["wilcoxon"]["w_plus"] == 4.0 and s["wilcoxon"]["p_value"] == pytest.approx(1.0)


def test_report_counts_passes_per_task_tier_route_and_stability(tmp_path, capsys):
    paths = _runs(tmp_path)
    out_json = tmp_path / "report.json"
    assert mrr.main([str(p) for p in paths] + ["--json", str(out_json)]) == 0
    md = capsys.readouterr().out
    report = json.loads(out_json.read_text())

    tasks = {t["task_id"]: t for t in report["tasks"]}
    assert tasks["e2"]["passes"] == {PLAIN: 1, QUOIN: 3} and tasks["e2"]["runs"] == 3
    assert tasks["e2"]["diff"] == pytest.approx(2 / 3)
    assert tasks["l1"]["route"] == "mixed" and tasks["m1"]["route"] == "direct"

    easy = report["tiers"]["easy"]
    assert easy["mean_pass_rate"][PLAIN] == pytest.approx(2 / 3)
    assert easy["mean_pass_rate"][QUOIN] == pytest.approx(1.0)
    overall = report["overall"]
    assert overall["n_tasks"] == 5 and overall["runs"] == 15
    assert overall["mean_diff"] == pytest.approx((2 / 3 + 1 / 3) / 5)
    assert overall["sign_test"]["positive"] == 2 and overall["sign_test"]["negative"] == 0
    assert overall["sign_test"]["p_value"] == pytest.approx(0.5)

    stab = overall["stability"]
    assert stab[PLAIN] == {"every": ["e1"], "none": ["m1", "m2", "l1"], "some": ["e2"]}
    assert stab[QUOIN] == {"every": ["e1", "e2"], "none": ["m1", "m2"], "some": ["l1"]}

    assert set(report["by_route"]) == {"light", "direct", "mixed"}
    assert report["by_route"]["light"]["n_tasks"] == 2
    assert "| e2 | easy | 1/3 | 3/3 | +66.7 pp | light |" in md
    assert "## Stability" in md and "## By quoin route" in md


def test_rows_not_run_by_either_arm_are_not_counted(tmp_path):
    paths = _runs(tmp_path, n=2)
    shutil.rmtree(paths[1] / QUOIN / "m2")
    runs = [mrr.load_run(p) for p in paths]
    tasks = {t["task_id"]: t for t in mrr.build_report(runs)["tasks"]}
    assert tasks["m2"]["runs"] == 1 and tasks["m2"]["not_counted"] == 1


@pytest.mark.parametrize("key,value", [
    ("suite_sha", "0000000000000000"),
    ("quoin_claude_model", SONNET),
    ("quoin_route", "default"),
])
def test_mismatched_runs_are_refused_unless_allowed(tmp_path, capsys, key, value):
    paths = _runs(tmp_path, n=2)
    build_tiered_run(tmp_path, run_id="R-other", **{"quoin_route": "lean", key: value})
    other = tmp_path / "R-other"
    argv = [str(p) for p in paths] + [str(other)]
    assert mrr.main(argv) == 2
    captured = capsys.readouterr()
    assert f"{key} differs:" in captured.err and "REFUSED: mixed-runs" in captured.err
    assert f"{other}: {value}" in captured.err
    assert captured.out == ""

    assert mrr.main(argv + ["--allow-mixed"]) == 0
    assert f"{key} differs" in capsys.readouterr().out


def test_unreadable_or_repeated_runs_are_refused(tmp_path, capsys):
    paths = _runs(tmp_path, n=1)
    assert mrr.main([str(tmp_path / "missing")]) == 2
    assert "no readable run-manifest.yaml" in capsys.readouterr().err
    assert mrr.main([str(paths[0]), str(paths[0])]) == 2
    assert "more than once" in capsys.readouterr().err


def test_runs_without_routes_say_so(tmp_path, capsys):
    run_dir, run_id, _ = build_tiered_run(tmp_path, run_id="plain-only", simple_claude_model=OPUS)
    assert mrr.main([str(run_dir / run_id)]) == 0
    assert "The runs record no quoin route per task." in capsys.readouterr().out
