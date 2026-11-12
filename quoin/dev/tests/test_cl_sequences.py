"""SWE-Bench-CL sequence suite: suite generation, memory carry, resume, report, profile text."""
from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
from pathlib import Path

import pytest

from aggregate_fixtures import OPUS, suite_sha, write_manifest, write_row
from quoin.benchmarks.harness import runner, sequence_memory
from quoin.benchmarks.harness.config import HarnessConfig, quoin_route
from quoin.benchmarks.harness.repo_prep import prepare_mirrors
from quoin.benchmarks.scripts import pick_subset, sequence_report
from verified_standin import build_upstream, configure_env

QUOIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = QUOIN_ROOT.parent
BENCH = QUOIN_ROOT / "benchmarks"
SUITE = BENCH / "suite-cl-sequences.json"
PROVENANCE = BENCH / "suite-cl-sequences-provenance.md"
CL_SNAPSHOT = BENCH / "data" / "swebench-cl-snapshot.json"
PROBLEMS = BENCH / "data" / "swebench-verified-problems-cl-sequences.json"
VERIFIED = BENCH / "data" / "swebench-verified-snapshot.json"
RUN_SKILL = QUOIN_ROOT / "adapters" / "claude" / "skills" / "run" / "SKILL.md"
CORE_RUN = QUOIN_ROOT / "core" / "skills" / "run.md"
CL_REVISION = "74a38a90baace25635f3827ee2f98caff24b3768"
CL_SOURCE_SHA = "91bc39a769b6218419bd44308650e5d2c846ecd3e6f7a6c086f74a37b6db90f7"
PLAIN, QUOIN = "simple-claude", "quoin-claude"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _suite():
    return json.loads(SUITE.read_text(encoding="utf-8"))


# --- suite ------------------------------------------------------------------------

def test_suite_regenerates_byte_for_byte():
    built = pick_subset.build_cl_suite(CL_SNAPSHOT, VERIFIED, PROBLEMS, 1729)
    assert pick_subset.render_suite(built) == SUITE.read_text(encoding="utf-8")


def test_suite_runs_whole_sequences_in_chronological_order():
    doc = _suite()
    tiers = list(doc["tiered"]["tiers"])
    assert tiers == list(pick_subset.CL_SEQUENCE_SPEC) and doc["tiered"]["pilot_tier"] == tiers[0]
    assert doc["tiered"]["sequence_mode"] is True and doc["tiered"]["paired_run"] is True
    # Tasks are grouped by sequence, in tier order, so suite order is sequence order.
    assert [t["tier"] for t in doc["tasks"]] == sorted(
        (t["tier"] for t in doc["tasks"]), key=tiers.index)
    for tier in tiers:
        seq = [t for t in doc["tasks"] if t["tier"] == tier]
        assert len(seq) == doc["tiered"]["tiers"][tier]["n"] == 12
        assert [t["sequence_position"] for t in seq] == list(range(12))
        assert all(t["sequence_id"] == tier and t["sequence_length"] == 12 for t in seq)
        cl = [t["cl_position"] for t in seq]
        assert cl == list(range(cl[0], cl[0] + 12)), "consecutive upstream tasks"
        assert [t["created_at"] for t in seq] == sorted(t["created_at"] for t in seq)
        assert all(t["source"] == "swebench_verified" for t in seq)


def test_selection_is_seeded_and_deterministic():
    snap = pick_subset.load_cl_snapshot(CL_SNAPSHOT)
    a = pick_subset.select_cl_windows(snap, 1729)
    assert a == pick_subset.select_cl_windows(snap, 1729)
    other = pick_subset.select_cl_windows(snap, 7)
    assert any(a[t] != other[t] for t in a)


def test_every_task_is_a_verified_instance_with_the_verified_prompt():
    verified = {r["instance_id"]: r for r in json.loads(VERIFIED.read_text())["rows"]}
    problems = json.loads(PROBLEMS.read_text())["problems"]
    for t in _suite()["tasks"]:
        row = verified[t["source_id"]]
        assert (row["repo"], row["base_commit"], row["difficulty"]) == (
            t["repo"], t["base_commit"], t["difficulty"])
        assert t["description"] == problems[t["source_id"]]


def test_window_outside_the_chronological_head_is_refused():
    snap = pick_subset.load_cl_snapshot(CL_SNAPSHOT)
    spec = {"sympy": {"sequence_id": "sympy_sympy_sequence", "n": 30, "sub_seed_offset": 1}}
    with pytest.raises(pick_subset.TieredSuiteError, match="chronological"):
        pick_subset.select_cl_windows(snap, 1729, spec)


def test_cl_snapshot_and_provenance_pins():
    snap = json.loads(CL_SNAPSHOT.read_text())
    assert snap["revision"] == CL_REVISION and snap["source_file_sha256"] == CL_SOURCE_SHA
    assert snap["task_count"] == 273 and snap["license"] == "MIT"
    source = _suite()["tiered"]["sequence_source"]
    assert source["revision"] == CL_REVISION and source["source_file_sha256"] == CL_SOURCE_SHA
    assert source["snapshot_sha256"] == _sha(CL_SNAPSHOT)
    tiered = _suite()["tiered"]
    assert tiered["snapshot_sha256"] == _sha(VERIFIED)
    assert tiered["problems_sha256"] == _sha(PROBLEMS)
    prov = PROVENANCE.read_text(encoding="utf-8")
    for value in (CL_REVISION, CL_SOURCE_SHA, _sha(CL_SNAPSHOT), _sha(PROBLEMS), _sha(VERIFIED)):
        assert value in prov
    for t in _suite()["tasks"]:
        assert f"| {t['id']} | {t['tier']} | {t['sequence_position']} | {t['source_id']} |" in prov
    money = lambda pat: int(re.search(pat, prov).group(1).replace(",", ""))
    cost = pick_subset.CL_COST
    assert money(r"Per-session cap \| \$([\d,]+)") == cost["documented_cap_usd"] == tiered["documented_cap_usd"]
    assert money(r"Enforced ceiling \| \$([\d,]+)") == cost["worst_case_ceiling_usd"]
    assert money(r"Documented target \| \$([\d,]+)") == cost["worst_case_target_usd"] == 35 * 36 * 2
    assert money(r"\*\*([\d,]+) s\*\*") == pick_subset.CL_WALL_CLOCK_SECONDS


def test_suite_file_is_not_normalised_by_git():
    out = subprocess.run(
        ["git", "check-attr", "text", "--", str(SUITE.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert out.strip().endswith("text: unset")


# --- memory carry -------------------------------------------------------------------

def _memory_tree(root: Path, lessons: int):
    mem = root / "memory"
    (mem / "sessions").mkdir(parents=True)
    (mem / "checkpoints").mkdir()
    (mem / "lessons-learned.md").write_text(
        "".join(f"## 2026-11-0{i} — t{i}\n**Lesson:** x\n\n" for i in range(lessons)))
    (mem / "repos-inventory.md").write_text("inventory")
    (mem / "sessions" / "s.md").write_text("session")
    (mem / "checkpoints" / "c.md").write_text("checkpoint")
    (mem / "run-state-t.json").write_text("{}")
    (mem / "autonomous-run-t.marker").write_text("")
    for name in ("workspaces", "telemetry"):
        (mem / name).mkdir()
        (mem / name / "x.json").write_text("{}")
    (mem / "1m-tier-sonnet.txt").write_text("")
    (mem / "needs-decision-t.md").write_text("")
    (mem / "worktree-probe.txt").write_text("")
    return mem


def test_only_memory_carries_and_session_state_does_not(tmp_path):
    src = _memory_tree(tmp_path / "src", lessons=2)
    work = tmp_path / "work"
    (work / ".workflow_artifacts" / "cache").mkdir(parents=True)
    copied = sequence_memory.seed_workdir(work, src)
    assert sorted(copied) == ["lessons-learned.md", "repos-inventory.md"]
    wa = work / ".workflow_artifacts"
    assert sorted(p.name for p in (wa / "memory").iterdir()) == copied
    assert sorted(p.name for p in wa.iterdir()) == ["cache", "memory"]
    assert not any((wa / "cache").iterdir())


def test_seeded_files_get_a_fresh_mtime_so_staleness_counts_from_the_seed(tmp_path):
    import os
    src = _memory_tree(tmp_path / "src", lessons=1)
    old = src / "repos-inventory.md"
    os.utime(old, (1_000_000, 1_000_000))
    work = tmp_path / "work"
    sequence_memory.seed_workdir(work, src)
    assert (work / ".workflow_artifacts" / "memory" / "repos-inventory.md").stat().st_mtime > 1_000_000


def test_real_quoin_cell_seeds_after_resetting_workflow_artifacts(tmp_path):
    from quoin.benchmarks.harness.cells import quoin_claude, simple_claude
    from quoin.benchmarks.harness.config import BudgetSpec
    import inspect
    assert "memory_seed_dir" in inspect.signature(quoin_claude.invoke).parameters
    assert "memory_seed_dir" not in inspect.signature(simple_claude.invoke).parameters
    seed = _memory_tree(tmp_path / "seed", lessons=2)
    work = tmp_path / "work"
    (work / ".workflow_artifacts" / "memory").mkdir(parents=True)
    (work / ".workflow_artifacts" / "memory" / "stale.md").write_text("from an earlier task")
    arm = tmp_path / "arm"   # not a git checkout: the cell refuses before any spawn
    arm.mkdir()
    out = quoin_claude.invoke(
        task_spec={"id": "cl_x_01", "description": "d", "repo": "o/r", "base_commit": "0" * 40},
        workdir=work, budget=BudgetSpec(wall_clock_seconds=10), run_id="r",
        quoin_install_script=arm / "quoin" / "install.sh", quoin_install_mode="skip",
        arm_root=arm, memory_seed_dir=seed,
    )
    assert out["verdict"] == "error" and out["extra"]["memory_seed_files"] == 2
    mem = work / ".workflow_artifacts" / "memory"
    assert sorted(p.name for p in mem.iterdir()) == ["lessons-learned.md", "repos-inventory.md"]


def test_finish_task_snapshots_final_memory_and_counts_lessons(tmp_path):
    seed = _memory_tree(tmp_path / "seed", lessons=1)
    task_dir = tmp_path / "row"
    _memory_tree(task_dir / "workflow_artifacts_evidence", lessons=3)
    spec = {"id": "cl_x_01", "sequence_id": "x", "sequence_position": 1}
    rec = sequence_memory.finish_task(task_dir, spec, seed, "cl_x_00")
    assert rec["lessons_inherited"] == 1 and rec["lessons_after"] == 3 and rec["lessons_written"] == 2
    assert rec["inherited_from"] == "cl_x_00"
    assert [f["path"] for f in rec["inherited_files"]] == ["lessons-learned.md", "repos-inventory.md"]
    assert sorted(p.name for p in (task_dir / "memory-snapshot").iterdir()) == [
        "lessons-learned.md", "repos-inventory.md"]
    assert json.loads((task_dir / "memory-carry.json").read_text()) == rec


def _row(cell_dir: Path, task_id, seq, pos, snapshot=True):
    d = cell_dir / task_id
    d.mkdir(parents=True)
    (d / "memory-carry.json").write_text(json.dumps({"sequence_id": seq, "sequence_position": pos}))
    if snapshot:
        (d / "memory-snapshot").mkdir()
    return d


def test_seed_is_the_nearest_earlier_snapshot_of_the_same_sequence(tmp_path):
    cell = tmp_path / QUOIN
    _row(cell, "a0", "a", 0)
    _row(cell, "a1", "a", 1, snapshot=False)   # session left no memory evidence
    _row(cell, "a3", "a", 3)                   # later position: never a seed
    _row(cell, "b1", "b", 1)                   # other sequence
    spec = {"id": "a2", "sequence_id": "a", "sequence_position": 2}
    seed, frm = sequence_memory.find_seed(cell, spec)
    assert frm == "a0" and seed == cell / "a0" / "memory-snapshot"
    assert sequence_memory.find_seed(cell, {"id": "a0", "sequence_id": "a",
                                            "sequence_position": 0}) == (None, None)


# --- runner: carry through a real checkout, and resume ---------------------------------

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


class MemoryAdapter:
    """Stands in for the quoin cell: wipes .workflow_artifacts, seeds, appends a lesson."""

    def __init__(self):
        self.seen = {}

    def invoke(self, task_spec, workdir, budget, run_id, run_output_dir=None, memory_seed_dir=None):
        wa = Path(workdir) / ".workflow_artifacts"
        if wa.exists():
            import shutil
            shutil.rmtree(wa)
        sequence_memory.seed_workdir(Path(workdir), memory_seed_dir)
        mem = wa / "memory"
        mem.mkdir(parents=True, exist_ok=True)
        lessons = mem / "lessons-learned.md"
        before = lessons.read_text() if lessons.exists() else ""
        self.seen[task_spec["id"]] = before
        lessons.write_text(before + f"## 2026-11-12 — {task_spec['id']}\n**Lesson:** l\n\n")
        (mem / f"run-state-{task_spec['id']}.json").write_text("{}")
        import shutil
        shutil.copytree(wa, Path(run_output_dir) / "workflow_artifacts_evidence", dirs_exist_ok=True)
        return {"transcript_events": [], "prompt": "p", "cost_runtime_usd": 0.1,
                "cost_available": True, "extra": {}}


class PlainAdapter:
    def __init__(self):
        self.kwargs = []

    def invoke(self, task_spec, workdir, budget, run_id, run_output_dir=None):
        self.kwargs.append(task_spec["id"])
        return {"transcript_events": [], "prompt": "p", "cost_runtime_usd": 0.1,
                "cost_available": True, "extra": {}}


@pytest.fixture
def seq_env(tmp_path, monkeypatch):
    standin = build_upstream(tmp_path)
    cache = configure_env(monkeypatch, tmp_path, standin)
    work = tmp_path / "work"
    work.mkdir()
    prepare_mirrors([standin["task"]], log=lambda *_: None)
    cfg = HarnessConfig(run_dir=tmp_path / "runs", suite_path=SUITE,
                        repo_cache_root=cache, repo_work_root=work)
    monkeypatch.setattr(runner, "judge_task", lambda *a, **k: None)
    specs = [dict(standin["task"], id=f"cl_s_{i:02d}", sequence_id="s", sequence_position=i,
                  sequence_length=3) for i in range(3)]
    return cfg, specs


def _invoke(cfg, specs, adapter, cell, monkeypatch, route="memory"):
    monkeypatch.setenv("QUOIN_BENCH_QUOIN_ROUTE", route)
    monkeypatch.setattr(runner, "_load_cell_adapter", lambda c: adapter)
    cfg.defer_judge = True
    for spec in specs:
        runner.run_one_task(cell, spec, "r1", cfg)


def test_runner_carries_memory_in_order_and_resume_restores_the_previous_snapshot(seq_env, monkeypatch):
    cfg, specs = seq_env
    adapter = MemoryAdapter()
    _invoke(cfg, specs, adapter, QUOIN, monkeypatch)
    assert adapter.seen["cl_s_00"] == ""
    assert "— cl_s_00" in adapter.seen["cl_s_01"] and "— cl_s_01" not in adapter.seen["cl_s_01"]
    assert "— cl_s_01" in adapter.seen["cl_s_02"]
    cell = cfg.run_dir / "r1" / QUOIN
    rec = json.loads((cell / "cl_s_02" / "memory-carry.json").read_text())
    assert rec["inherited_from"] == "cl_s_01" and rec["lessons_written"] == 1
    snap = cell / "cl_s_01" / "memory-snapshot"
    assert not any(p.name.startswith("run-state-") for p in snap.iterdir())

    # Resume after the last row was lost: it is seeded again from cl_s_01's snapshot,
    # never from its own earlier snapshot.
    import shutil
    first_view = adapter.seen["cl_s_02"]
    shutil.rmtree(cell / "cl_s_02")
    adapter.seen.clear()
    _invoke(cfg, specs[2:], adapter, QUOIN, monkeypatch)
    assert adapter.seen["cl_s_02"] == first_view


def test_lean_route_never_carries(seq_env, monkeypatch):
    cfg, specs = seq_env
    adapter = MemoryAdapter()
    _invoke(cfg, specs[:2], adapter, QUOIN, monkeypatch, route="lean")
    assert adapter.seen == {"cl_s_00": "", "cl_s_01": ""}
    assert not (cfg.run_dir / "r1" / QUOIN / "cl_s_01" / "memory-carry.json").exists()


def test_plain_arm_gets_no_memory_and_no_carry_record(seq_env, monkeypatch):
    cfg, specs = seq_env
    adapter = PlainAdapter()
    _invoke(cfg, specs[:2], adapter, PLAIN, monkeypatch)
    assert adapter.kwargs == ["cl_s_00", "cl_s_01"]
    for spec in specs[:2]:
        assert not (cfg.run_dir / "r1" / PLAIN / spec["id"] / "memory-carry.json").exists()


# --- memory route and profile text ----------------------------------------------------

def test_memory_route_is_accepted():
    assert quoin_route({"QUOIN_BENCH_QUOIN_ROUTE": "memory"}) == "memory"


def test_run_skill_defines_the_memory_profile():
    text = RUN_SKILL.read_text(encoding="utf-8")
    item = text[text.index("6. **`QUOIN_RUN_PROFILE`**"):text.index("7. **`direct:`**")]
    assert "`bench-lean` sets `LEAN`" in item and "`bench-memory` sets `MEMORY`" in item
    flat = " ".join(item.split())
    assert "`MEMORY` skips only isolation (Step 1 item 2, Step 8 item 6)" in flat
    assert "`LEAN`/`MEMORY`: no claim" in text
    # MEMORY never gates the lessons read/append, discovery or the dirty marker.
    assert text.count("Unless `LEAN`,") == 3 and "Unless `MEMORY`" not in text
    assert "bench-memory" in CORE_RUN.read_text(encoding="utf-8")


def test_tiered_entry_takes_the_pilot_tier_from_the_suite(tmp_path):
    from quoin.benchmarks.scripts import tiered_entry
    args = tiered_entry.build_parser().parse_args(["--suite", str(SUITE), "--repo-root", str(REPO_ROOT)])
    res = tiered_entry.resolve(args)
    assert not isinstance(res, int) and res.pilot_tier == "django"


# --- sequence report -------------------------------------------------------------------

def _seq_suite(path):
    tasks = [{"id": f"t{i}", "tier": "s", "source": "swebench_verified", "source_id": f"o__r-{i}",
              "sequence_id": "s", "sequence_position": i, "sequence_length": 6} for i in range(6)]
    doc = {"schema_version": 2, "tiered": {"sequence_mode": True, "tiers": {"s": {"n": 6}}},
           "tasks": tasks}
    path.write_text(json.dumps(doc))
    return path


def _seq_run(tmp_path, run_id, route, plain, quoin, lessons=None):
    suite = _seq_suite(tmp_path / "suite.json")
    run = tmp_path / run_id
    run.mkdir()
    for i in range(6):
        write_row(run, PLAIN, f"t{i}", verdict=plain[i], wall=10, cost=1.0)
        d = write_row(run, QUOIN, f"t{i}", verdict=quoin[i], wall=10 + 10 * i, cost=2.0 + i)
        if lessons is not None:
            (d / "memory-carry.json").write_text(json.dumps({
                "sequence_id": "s", "sequence_position": i,
                "inherited_from": f"t{i - 1}" if i else None,
                "inherited_files": [{"path": "lessons-learned.md"}] if i else [],
                "lessons_inherited": sum(lessons[:i]), "lessons_written": lessons[i]}))
    write_manifest(run, run_id=run_id, suite_path=suite, suite_sha=suite_sha(suite),
                   simple_claude_model=OPUS, quoin_claude_model=OPUS, quoin_route=route)
    return run


def test_sequence_report_on_fixture_runs(tmp_path, capsys):
    mem = _seq_run(tmp_path, "mem", "memory", ["fail"] * 6,
                   ["fail", "fail", "pass", "pass", "pass", "pass"], lessons=[1, 0, 1, 0, 0, 1])
    lean = _seq_run(tmp_path, "lean", "lean", ["fail"] * 6,
                    ["fail", "fail", "fail", "fail", "pass", "pass"])
    out = tmp_path / "r.json"
    assert sequence_report.main([str(mem), "--lean-run", str(lean), "--json", str(out)]) == 0
    report = json.loads(out.read_text())
    pos = report["by_position"]
    assert pos["memory"]["first"]["pass_rate"] == 0.0 and pos["memory"]["last"]["pass_rate"] == 1.0
    assert pos["memory"]["first"]["n"] == pos["memory"]["middle"]["n"] == 2
    assert pos["memory"]["first"]["mean_usd"] == pytest.approx(2.5)
    # Cost rises $1 per position over relative positions 0..1 in 5 steps: slope 5.
    assert pos["memory"]["slope"]["usd"] == pytest.approx(5.0)
    assert pos["plain"]["slope"]["pass"] == pytest.approx(0.0)
    assert report["paired"]["memory_vs_lean"]["mean"]["pass"] == pytest.approx(2 / 6)
    assert report["paired"]["memory_vs_lean"]["sign_test"]["pass"]["positive"] == 2
    assert report["paired"]["memory_vs_plain"]["n"] == 6
    m = {r["task_id"]: r for r in report["memory"]}
    assert m["t0"]["inherited_from"] is None and m["t3"]["inherited_from"] == "t2"
    assert m["t3"]["lessons_inherited"] == 2 and m["t5"]["lessons_written"] == 1
    assert "## By position in the sequence" in capsys.readouterr().out


def test_slope_is_within_sequence_and_unknown_cost_is_left_out(tmp_path):
    # Two sequences at different levels, each flat: no slope, whatever rows are missing.
    pts = [("a", 0.0, 1.0), ("a", 1.0, 1.0), ("b", 0.5, 0.0), ("b", 1.0, 0.0)]
    assert sequence_report.slope(pts) == pytest.approx(0.0)
    assert sequence_report.slope([("a", 0.0, 1.0)]) is None
    cell = {"verdict": "pass", "usd": 0.0, "usd_source": "unavailable", "wall_clock_s": 5}
    assert sequence_report._counted(cell) and not sequence_report._counted(cell, "usd")


def test_sequence_report_refuses_wrong_routes(tmp_path):
    mem = _seq_run(tmp_path, "mem", "default", ["fail"] * 6, ["fail"] * 6)
    assert sequence_report.main([str(mem)]) == 2
    assert sequence_report.main([str(mem), "--allow-mixed"]) == 0
