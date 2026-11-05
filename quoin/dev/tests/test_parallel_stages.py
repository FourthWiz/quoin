"""Tests for parallel_stages.py: wave planning, stage merges, per-stage costs."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "core" / "scripts" / "parallel_stages.py"
_SPEC = importlib.util.spec_from_file_location("_test_parallel_stages", _SCRIPT)
ps = importlib.util.module_from_spec(_SPEC)
sys.modules["_test_parallel_stages"] = ps
_SPEC.loader.exec_module(ps)


ARCH = """# t — stages
## Overview
x
## Stage decomposition
1. ✅ S-01: schema
   - Repo: .
   - Depends on: none
2. ⏳ S-02: api
   - Depends on: S-01
3. ⏳ S-03: cli
   - Depends on: S-01
4. ⏳ S-04: docs
   - Depends on: none
5. ⏳ S-05: wire-up
   - Depends on: S-02, S-03
## Interfaces between stages
- 1. ⏳ S-09: not a stage
"""


def test_plan_waves_groups_independent_stages_and_honours_cap():
    stages = ps.parse_stages(ARCH)
    assert [s["id"] for s in stages] == ["S-01", "S-02", "S-03", "S-04", "S-05"]
    assert stages[0]["done"] and stages[4]["depends_on"] == ["S-02", "S-03"]
    assert ps.plan_waves(stages, 2) == [["S-02", "S-03"], ["S-04"], ["S-05"]]
    assert ps.plan_waves(stages, 3) == [["S-02", "S-03", "S-04"], ["S-05"]]
    assert ps.plan_waves(stages, 1) == [["S-02"], ["S-03"], ["S-04"], ["S-05"]]


def test_dependency_lines_need_explicit_none_and_real_stage_refs():
    text = ("## Stage decomposition\n"
            "1. ⏳ S-01: a\n   - Depends on: none (stores files in S3)\n"
            "2. ⏳ S-02: b\n"
            "3. ⏳ S-03: c\n   - Depends on: None\n")
    stages = ps.parse_stages(text)
    assert [s["depends_on"] for s in stages] == [[], ["S-01"], []]
    assert ps.plan_waves(stages, 2) == [["S-01", "S-03"], ["S-02"]]


@pytest.mark.parametrize("text,msg", [
    ("## Stage decomposition\n1. ⏳ S-01: a\n   - Depends on: S-07\n", "unknown"),
    ("## Stage decomposition\n1. ⏳ S-01: a\n   - Depends on: S-02\n"
     "2. ⏳ S-02: b\n   - Depends on: S-01\n", "cycle"),
    ("# no section\n", "no '## Stage decomposition'"),
])
def test_plan_rejects_bad_decompositions(text, msg):
    with pytest.raises(ps.StageError, match=msg):
        ps.plan_waves(ps.parse_stages(text), 2)


def test_cap_env_default_and_budget(monkeypatch):
    monkeypatch.delenv("QUOIN_PARALLEL_MAX", raising=False)
    assert ps.resolve_cap(None, None, None) == (2, "default")
    monkeypatch.setenv("QUOIN_PARALLEL_MAX", "4")
    assert ps.resolve_cap(None, None, None) == (4, "env")
    assert ps.resolve_cap(3, 1.0, 10.0) == (3, "flag")
    cap, reason = ps.resolve_cap(None, 12.0, 10.0)
    assert cap == 1 and "budget" in reason


def test_plan_cli_reads_budget_env(tmp_path, monkeypatch, capsys):
    (tmp_path / "architecture.md").write_text(ARCH, encoding="utf-8")
    monkeypatch.setenv("QUOIN_PARALLEL_BUDGET_USD", "5")
    assert ps.main(["plan", "--task-dir", str(tmp_path), "--spent-usd", "6"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cap"] == 1 and out["parallel"] is False


# ── git ──────────────────────────────────────────────────────────────────────

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "task")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "shared.txt").write_text("base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "base")
    return r


def _stage_commit(repo, tmp_path, sid, fname, text):
    info = ps.make_worktree(str(repo), "task", sid, None)
    wt = Path(info["path"])
    (wt / fname).write_text(text)
    _git(wt, "add", fname)
    _git(wt, "commit", "-qm", sid)
    return info


def test_parallel_stages_merge_cleanly_then_clean_up(repo, tmp_path):
    a = _stage_commit(repo, tmp_path, "S-01", "a.txt", "a\n")
    b = _stage_commit(repo, tmp_path, "S-02", "b.txt", "b\n")
    assert a["branch"] == "task--s-01" and a["base"] == b["base"]
    assert _git(repo, "status", "--porcelain") == ""  # in-repo worktrees excluded
    again = ps.make_worktree(str(repo), "task", "S-01", None)  # resume reuses it
    assert again["status"] == "exists" and again["path"] == a["path"]
    (Path(a["path"]) / "junit.xml").write_text("<x/>")  # untracked test output
    for info in (a, b):
        code, res = ps.merge_stage(str(repo), "task", info["branch"])
        assert code == 0 and res["status"] == "merged"
        code, res = ps.cleanup_stage(str(repo), "task", info["branch"], False)
        assert code == 0 and res["status"] == "cleaned", res
    assert (repo / "a.txt").exists() and (repo / "b.txt").exists()
    assert "task--s-01" not in _git(repo, "branch")


def test_cleanup_refuses_unmerged_branch_unless_discarded(repo, tmp_path):
    a = _stage_commit(repo, tmp_path, "S-01", "a.txt", "a\n")
    code, res = ps.cleanup_stage(str(repo), "task", a["branch"], False)
    assert code == ps.EXIT_REFUSED and res["reason"] == "unmerged"
    code, res = ps.cleanup_stage(str(repo), "task", a["branch"], True)
    assert code == 0 and not Path(a["path"]).exists()


def test_conflicting_merge_aborts_and_restores_task_branch(repo, tmp_path):
    a = _stage_commit(repo, tmp_path, "S-01", "shared.txt", "from a\n")
    b = _stage_commit(repo, tmp_path, "S-02", "shared.txt", "from b\n")
    assert ps.merge_stage(str(repo), "task", a["branch"])[0] == 0
    before = _git(repo, "rev-parse", "HEAD")
    code, res = ps.merge_stage(str(repo), "task", b["branch"])
    assert code == ps.EXIT_CONFLICT and res["status"] == "conflict"
    assert res["conflicts"] == ["shared.txt"] and res["restored"] is True
    assert res["reason"] == "conflict"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert (repo / "shared.txt").read_text() == "from a\n"


def test_merge_blocked_by_untracked_file_reports_merge_failed(repo, tmp_path):
    a = _stage_commit(repo, tmp_path, "S-01", "a.txt", "a\n")
    (repo / "a.txt").write_text("untracked local\n")
    before = _git(repo, "rev-parse", "HEAD")
    code, res = ps.merge_stage(str(repo), "task", a["branch"])
    assert code == ps.EXIT_CONFLICT and res["reason"] == "merge-failed"
    assert res["conflicts"] == [] and res["restored"] is True
    assert _git(repo, "rev-parse", "HEAD") == before


def test_merge_refuses_dirty_tree_and_wrong_branch(repo, tmp_path):
    a = _stage_commit(repo, tmp_path, "S-01", "a.txt", "a\n")
    (repo / "shared.txt").write_text("local edit\n")
    code, res = ps.merge_stage(str(repo), "task", a["branch"])
    assert code == ps.EXIT_REFUSED and res["reason"] == "dirty-tree"
    assert (repo / "shared.txt").read_text() == "local edit\n"
    code, res = ps.merge_stage(str(repo), "other", a["branch"])
    assert code == ps.EXIT_REFUSED and res["reason"] == "wrong-branch"


def test_worktree_cli_accepts_bare_stage_number(repo, capsys):
    assert ps.main(["worktree", "--repo", str(repo), "--task-branch", "task",
                    "--stage", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["branch"] == "task--s-01"


def test_stage_costs_breaks_down_by_stage_key(tmp_path):
    ledger = tmp_path / "cost-ledger.md"
    ledger.write_text(
        "# Cost Ledger — t\n"
        "a1 | 2026-11-05 | implement | opus | task | n | 0 | usd=1.5;src=nested_jsonl;stage=S-01\n"
        "a1 | 2026-11-05 | implement | opus | task | n | 0 | usd=1.5;src=nested_jsonl;stage=S-01\n"
        "a2 | 2026-11-05 | implement | opus | task | n | 0 | usd=0.25;src=nested_jsonl;stage=S-02\n"
        "a3 | 2026-11-05 | review | opus | task | n | 0 | tok=9;src=unresolved;stage=S-02\n"
        "s1 | 2026-11-05 | run-orchestrator | opus | task | n\n",
        encoding="utf-8",
    )
    got = ps.stage_costs(ledger)
    assert list(got) == ["S-01", "S-02", "untagged"]
    assert got["S-01"] == {"usd": 1.5, "rows": 2, "unresolved": 0}
    assert got["S-02"] == {"usd": 0.25, "rows": 2, "unresolved": 1}
    assert got["untagged"]["unresolved"] == 1
