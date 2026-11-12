"""SWE-bench Pro source: snapshot pins, suite, repo prep, prompt, judge mapping, live smoke."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from quoin.benchmarks.harness import judge, pro_prompt, repo_prep
from quoin.benchmarks.harness.cells.simple_claude import _build_prompt
from quoin.benchmarks.harness.repo_prep import prepare_mirrors, prepare_task_repo
from quoin.benchmarks.scripts import pick_subset
from verified_standin import build_upstream, configure_env, git

QUOIN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = QUOIN_ROOT.parent
BENCH = QUOIN_ROOT / "benchmarks"
SUITE = BENCH / "suite-pro-balanced.json"
PROVENANCE = BENCH / "suite-pro-balanced-provenance.md"
SNAPSHOT = BENCH / "data" / "swebench-pro-snapshot.json"
PROBLEMS = BENCH / "data" / "swebench-pro-problems.json"
VERIFIERS = BENCH / "data" / "swebench-pro-verifiers.json"
FIXTURE = Path(__file__).parent / "fixtures" / "swebench_pro"
PRO_REVISION = "2d52cb3df914a3fcf80c7f66738b3a88ae37fc50"
PRO_PARQUET_SHA = "1067f72c1f43bbc578276888625db3b836cebb535546c9eaf824c5d5dd104a7f"
OS_COMMIT = "66f92766bba642462d4bbe5479e83f91f9211862"
HIDDEN_KEYS = ("fail_to_pass", "pass_to_pass", "test_patch", "selected_test_files_to_run",
               "before_repo_set_cmd", "patch")
PRO_ID = "instance_org__repo-" + "a" * 40 + "-v" + "b" * 40


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _suite():
    return json.loads(SUITE.read_text(encoding="utf-8"))


# --- snapshot pins and suite -------------------------------------------------------------

def test_snapshot_pins_and_provenance():
    snap = json.loads(SNAPSHOT.read_text())
    assert snap["revision"] == PRO_REVISION and snap["source_file_sha256"] == PRO_PARQUET_SHA
    assert snap["row_count"] == 237 and {r["repo_language"] for r in snap["rows"]} == {"python"}
    assert json.loads(VERIFIERS.read_text())["commit"] == OS_COMMIT
    tiered = _suite()["tiered"]
    assert tiered["dataset_revision"] == PRO_REVISION and tiered["verifier_commit"] == OS_COMMIT
    assert tiered["snapshot_sha256"] == _sha(SNAPSHOT)
    assert tiered["problems_sha256"] == _sha(PROBLEMS)
    assert tiered["verifiers_sha256"] == _sha(VERIFIERS)
    prov = PROVENANCE.read_text(encoding="utf-8")
    for value in (PRO_REVISION, PRO_PARQUET_SHA, OS_COMMIT, _sha(SNAPSHOT), _sha(PROBLEMS), _sha(VERIFIERS)):
        assert value in prov
    for t in _suite()["tasks"]:
        assert f"| {t['id']} | {t['tier']} | {t['source_id']} | {t['repo']} | {t['patch_lines']} |" in prov
    money = lambda pat: int(re.search(pat, prov).group(1).replace(",", ""))
    assert money(r"Per-session cap \| \$([\d,]+)") == tiered["documented_cap_usd"] == 35
    assert money(r"Enforced ceiling \| \$([\d,]+)") == tiered["worst_case_ceiling_usd"]
    assert money(r"\*\*([\d,]+) s\*\*") == pick_subset.PRO_WALL_CLOCK_SECONDS


def test_suite_regenerates_byte_for_byte_and_is_balanced():
    built = pick_subset.build_pro_suite(SNAPSHOT, PROBLEMS, VERIFIERS, 1729)
    assert pick_subset.render_suite(built) == SUITE.read_text(encoding="utf-8")
    doc = _suite()
    counts = {t: sum(1 for x in doc["tasks"] if x["tier"] == t) for t in ("small", "medium", "large")}
    assert counts == {"small": 10, "medium": 15, "large": 15}
    assert doc["tiered"]["pilot_tier"] == "small" and doc["tiered"]["paired_run"] is True
    assert all(t["source"] == "swebench_pro" and t["repo_language"] == "python" for t in doc["tasks"])
    bands = pick_subset.pro_tier_bands(json.loads(SNAPSHOT.read_text())["rows"])
    for t in doc["tasks"]:
        assert t["source_id"] in bands[t["tier"]]
        assert t["docker_image"] == judge.PRO_IMAGE_PREFIX + t["source_id"]
        assert sorted(t["verifier"]["files"]) == sorted(judge.PRO_VERIFIER_FILES)
    small = max(t["patch_lines"] for t in doc["tasks"] if t["tier"] == "small")
    large = min(t["patch_lines"] for t in doc["tasks"] if t["tier"] == "large")
    assert small < large


def test_selection_is_seeded_and_deterministic():
    rows = json.loads(SNAPSHOT.read_text())["rows"]
    assert pick_subset.select_pro(rows, 1729) == pick_subset.select_pro(rows, 1729)
    assert pick_subset.select_pro(rows, 1729) != pick_subset.select_pro(rows, 7)


def test_patch_size_counts_changed_lines_and_files():
    diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
            "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -0,0 +1 @@\n+c\n")
    assert pick_subset.patch_size(diff) == (3, 2)


def test_suite_file_is_not_normalised_by_git():
    out = subprocess.run(["git", "check-attr", "text", "--", str(SUITE.relative_to(REPO_ROOT))],
                         cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    assert out.strip().endswith("text: unset")


# --- no hidden-test leakage --------------------------------------------------------------

def test_no_hidden_test_data_is_vendored_or_prompted():
    for path in (SUITE, SNAPSHOT, PROBLEMS, VERIFIERS):
        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert k not in HIDDEN_KEYS, (path.name, k)
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(json.loads(path.read_text()))
    # The prompt is exactly Scale's instruction built from the three agent-visible
    # fields (their own text may name test files, as in Scale's setup), nothing more.
    problems = json.loads(PROBLEMS.read_text())["problems"]
    for t in _suite()["tasks"]:
        prompt = _build_prompt(t)
        f = problems[t["source_id"]]
        assert prompt == pro_prompt.render_instruction(
            f["problem_statement"], f["requirements"], f["interface"])
        assert t["source_id"] not in prompt
        for word in ("fail_to_pass", "pass_to_pass", "test_patch", "hidden test"):
            assert word not in prompt.lower()


def test_prompt_is_scales_instruction_with_only_the_location_changed():
    expected = (FIXTURE / "instruction.md").read_text(encoding="utf-8")
    fields = json.loads((FIXTURE / "fields.json").read_text(encoding="utf-8"))
    upstream = pro_prompt.render_instruction(
        fields["problem_statement"], fields["requirements"], fields["interface"],
        preamble=pro_prompt.UPSTREAM_PREAMBLE)
    assert upstream == expected
    ours = pro_prompt.build_pro_prompt({"description": fields["problem_statement"],
                                        "requirements": fields["requirements"],
                                        "interface": fields["interface"]})
    assert ours == expected.replace(pro_prompt.UPSTREAM_PREAMBLE, pro_prompt.PREAMBLE)
    assert pro_prompt.PRO_PROMPT_TEMPLATE_VERSION == _suite()["tiered"]["prompt_template_version"]
    with pytest.raises(ValueError):
        pro_prompt.build_pro_prompt({"description": "x", "requirements": "", "interface": "y"})


# --- repo prep -----------------------------------------------------------------------------

@pytest.fixture
def pro_world(tmp_path, monkeypatch):
    for target in (socket.socket, ):
        monkeypatch.setattr(target, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("net")))
    standin = build_upstream(tmp_path)
    cache = configure_env(monkeypatch, tmp_path, standin)
    task = dict(standin["task"], id="pro_small_000", source="swebench_pro", source_id=PRO_ID)
    work = tmp_path / "work"
    work.mkdir()
    rows = prepare_mirrors([task], log=lambda *_: None)
    assert rows and rows[0]["pin_ref_ok"]
    return {**standin, "task": task, "cache": cache, "work": work}


def test_pro_task_prepares_at_base_with_no_later_history(pro_world):
    p = prepare_task_repo(pro_world["task"], pro_world["cache"], pro_world["work"])
    assert git(["rev-parse", "HEAD"], p.path) == pro_world["base"]
    assert git(["rev-list", "--all", f"^{pro_world['base']}"], p.path) == ""
    assert git(["tag"], p.path) == "" and git(["remote"], p.path) == ""
    assert repo_prep.check_pin_refs([pro_world["task"]], pro_world["cache"]) == []


def test_pro_and_verified_ids_validate_and_others_do_not():
    assert repo_prep.validate_instance_id(PRO_ID) == PRO_ID
    assert repo_prep.validate_instance_id("instance_o__r-" + "c" * 40 + "-vnan")
    assert repo_prep.validate_instance_id("django__django-11099")
    for bad in ("instance_o__r-xyz", "instance_o__r-" + "c" * 40 + "/../x", "../etc"):
        with pytest.raises(repo_prep.RepoPrepError):
            repo_prep.validate_instance_id(bad)


# --- judge mapping on recorded fixtures ----------------------------------------------------

VERIFIER_BYTES = {name: f"# {name}\n".encode() for name in judge.PRO_VERIFIER_FILES}


def _spec(**over):
    spec = {
        "id": "pro_small_000", "source": "swebench_pro", "source_id": PRO_ID,
        "docker_image": judge.PRO_IMAGE_PREFIX + PRO_ID,
        "verifier": {"commit": OS_COMMIT,
                     "files": {k: hashlib.sha256(v).hexdigest() for k, v in VERIFIER_BYTES.items()}},
    }
    spec.update(over)
    return spec


def _fetch(url):
    return VERIFIER_BYTES[url.rsplit("/", 1)[1]]


class FakeDocker:
    """Plays back a recorded verifier outcome into the mounted /logs directory."""

    def __init__(self, reward=None, rc=0, apply_rc=0, output=None, raises=None):
        self.reward, self.rc, self.apply_rc, self.output, self.raises = reward, rc, apply_rc, output, raises
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[1] == "rm":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if self.raises:
            raise self.raises
        logs = Path(next(a.split(":")[0] for a in cmd if a.endswith(":/logs")))
        (logs / "verifier").mkdir(parents=True, exist_ok=True)
        (logs / "apply_rc.txt").write_text(f"{self.apply_rc}\n")
        if self.reward is not None:
            (logs / "verifier" / "reward.txt").write_text(self.reward + "\n")
        if self.output is not None:
            (logs / "verifier" / "output.json").write_text(json.dumps(self.output))
        return subprocess.CompletedProcess(cmd, self.rc, "RESULT: ...", "")


def _judge(tmp_path, run, patch=b"diff --git a/x b/x\n", **kw):
    (tmp_path / "diff.patch").write_bytes(patch)
    return judge.judge_swebench_pro(kw.pop("spec", _spec()), tmp_path / "diff.patch", "r1", tmp_path,
                                    cell="quoin-claude", run=run, fetch=_fetch,
                                    verifier_cache=tmp_path / "cache", **kw)


def test_recorded_pass_and_fail_map_to_final_verdicts(tmp_path):
    out = json.loads((FIXTURE / "output-pass.json").read_text())
    r = _judge(tmp_path, FakeDocker(reward="1", output=out))
    assert (r.verdict, r.extra["judge_final"], r.extra["failure_reason"]) == ("pass", True, None)
    assert r.source_benchmark == "swebench_pro" and r.evidence_path.endswith("output.json")
    r = _judge(tmp_path, FakeDocker(reward="0", apply_rc=1, output={"tests": []}))
    assert (r.verdict, r.extra["judge_final"], r.extra["apply_rc"]) == ("fail", True, 1)


def test_empty_patch_fails_without_a_container(tmp_path):
    docker = FakeDocker(reward="1")
    r = _judge(tmp_path, docker, patch=b"  \n")
    assert (r.verdict, r.evidence_path, r.extra["judge_final"]) == ("fail", "empty-patch", True)
    assert docker.calls == []


@pytest.mark.parametrize("docker,reason", [
    (FakeDocker(reward=None, rc=1), "verifier-no-reward"),
    (FakeDocker(reward=None, rc=125), "docker-run-failed"),
    (FakeDocker(raises=FileNotFoundError()), "docker-unavailable"),
])
def test_infrastructure_failures_are_non_final_errors(tmp_path, docker, reason):
    r = _judge(tmp_path, docker)
    assert (r.verdict, r.extra["failure_reason"], r.extra["judge_final"]) == ("error", reason, False)


def test_stale_container_is_removed_before_each_run(tmp_path):
    docker = FakeDocker(reward="1")
    _judge(tmp_path, docker)
    assert [c[1] for c in docker.calls] == ["rm", "run"]
    assert docker.calls[0][3] == docker.calls[1][docker.calls[1].index("--name") + 1]


def test_timeout_removes_the_container(tmp_path):
    docker = FakeDocker(raises=subprocess.TimeoutExpired("docker", 1))
    r = _judge(tmp_path, docker)
    assert r.extra["failure_reason"] == "judge-timeout"
    assert docker.calls[-1][:3] == ["docker", "rm", "-f"]


def test_bad_pins_and_image_are_refused(tmp_path):
    bad = _spec()
    bad["verifier"]["files"]["test.sh"] = "0" * 64
    assert _judge(tmp_path, FakeDocker(reward="1"), spec=bad).extra["failure_reason"] == "verifier-hash-mismatch"
    wrong = _spec(docker_image="evil.example/image:latest")
    assert _judge(tmp_path, FakeDocker(reward="1"), spec=wrong).extra["failure_reason"] == "bad-docker-image"
    utf = _judge(tmp_path, FakeDocker(reward="1"), patch=b"\xff\xfe")
    assert (utf.verdict, utf.extra["judge_final"]) == ("error", True)


def test_emulation_adds_the_platform_and_patch_is_normalised(tmp_path):
    docker = FakeDocker(reward="1")
    _judge(tmp_path, docker, patch=b"diff --git a/x b/x", emulate_x86=True)
    cmd = docker.calls[-1]
    assert cmd[cmd.index("--platform") + 1] == "linux/amd64"
    assert cmd[-3:-1] == ["bash", "-c"] and "bash /tests/test.sh" in cmd[-1]
    assert (tmp_path / "swebench_pro_eval" / "model.patch").read_text().endswith("\n")
    docker = FakeDocker(reward="1")
    _judge(tmp_path, docker)
    assert "--platform" not in docker.calls[-1]


def test_runner_records_and_defers_pro_rows_like_verified(pro_world, tmp_path, monkeypatch):
    from quoin.benchmarks.harness import runner
    from quoin.benchmarks.harness.config import HarnessConfig
    from quoin.benchmarks.harness.judge import JudgeResult
    from quoin.benchmarks.harness.session_state import pipeline_rule_applies

    stream = Path(__file__).parent / "fixtures" / "benchmark_streams" / "clean_success.jsonl"
    events = [json.loads(l) for l in stream.read_text().splitlines() if l.strip()]

    class Adapter:
        def invoke(self, task_spec, workdir, budget, run_id, run_output_dir=None):
            return {"transcript_events": events, "prompt": "p", "cost_runtime_usd": 0.1,
                    "cost_available": True, "extra": {}}

    cfg = HarnessConfig(run_dir=tmp_path / "runs", repo_cache_root=pro_world["cache"],
                        repo_work_root=pro_world["work"], defer_judge=True)
    monkeypatch.setattr(runner, "_load_cell_adapter", lambda c: Adapter())
    runner.run_one_task("quoin-claude", pro_world["task"], "r1", cfg)
    t_dir = cfg.run_dir / "r1" / "quoin-claude" / pro_world["task"]["id"]
    session = json.loads((t_dir / "session.json").read_text())
    assert session["source"] == "swebench_pro" and session["prompt_template_version"] == "pro-v1"
    assert pipeline_rule_applies("quoin-claude", t_dir)
    # A quoin row with no plan is invalid and never judged; the plain row defers.
    runner.run_one_task("simple-claude", pro_world["task"], "r1", cfg)
    plain = cfg.run_dir / "r1" / "simple-claude" / pro_world["task"]["id"]
    seen = []
    stub = lambda tid, td, rid, **kw: seen.append(kw["task_spec"]["source"]) or JudgeResult(
        tid, "swebench_pro", "pass", "ev", 0.1, extra={"judge_final": True})
    rows = runner.judge_pending_rows(cfg.run_dir, "r1", [pro_world["task"]], judge=stub)
    assert seen == ["swebench_pro"] and rows[0]["verdict"] == "pass"
    assert json.loads((plain / "judge.json").read_text())["source_benchmark"] == "swebench_pro"


def test_judge_task_dispatches_pro_tasks(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(judge, "judge_swebench_pro",
                        lambda spec, patch, run_id, ev, **kw: seen.update(kw, spec=spec) or "ok")
    assert judge.judge_task("t", tmp_path, "r", task_spec=_spec(),
                            judge_context={"cell": "c", "emulate_x86": True}) == "ok"
    assert seen["cell"] == "c" and seen["emulate_x86"] is True


# --- live smoke: one real instance, reference patch and empty patch -------------------------

@pytest.mark.skipif(os.environ.get("QUOIN_BENCH_PRO_LIVE") != "1",
                    reason="live Docker and network smoke; set QUOIN_BENCH_PRO_LIVE=1")
def test_live_smoke_reference_patch_passes_and_empty_patch_fails(tmp_path):
    spec = min((t for t in _suite()["tasks"] if t["tier"] == "small"), key=lambda t: t["patch_lines"])
    cache = tmp_path / "cache"
    solution = judge.ensure_pro_verifier(spec, cache, path="solution")
    gold = tmp_path / "gold"
    gold.mkdir()
    shutil.copy(solution / "gold_patch.diff", gold / "diff.patch")
    r = judge.judge_swebench_pro(spec, gold / "diff.patch", "smoke", gold, emulate_x86=True,
                                 verifier_cache=cache)
    assert r.verdict == "pass", r.extra
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "diff.patch").write_bytes(b"")
    assert judge.judge_swebench_pro(spec, empty / "diff.patch", "smoke", empty,
                                    verifier_cache=cache).verdict == "fail"
