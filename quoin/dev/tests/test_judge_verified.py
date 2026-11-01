"""Offline tests for the SWE-bench Verified judge: no Docker, no network, no swebench."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from quoin.benchmarks.harness import judge
from quoin.benchmarks.harness.judge import (
    SWEBENCH_PINNED_VERSION, VERIFIED_DATASET, JudgeResult, _hf_cached_revisions,
    build_verified_command, judge_swebench_verified, judge_task, parse_verified_report,
)

SID = "django__django-11119"
SPEC = {"id": "verified_easy_000", "source": "swebench_verified", "source_id": SID,
        "repo": "django/django", "base_commit": "a" * 40, "description": "d"}
PATCH = b"diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n ctx  \r\n-old\n+new  \n"


def _report(resolved=(), unresolved=(), empty=(), error=(), incomplete=()):
    return {
        "resolved_ids": list(resolved), "unresolved_ids": list(unresolved),
        "empty_patch_ids": list(empty), "error_ids": list(error),
        "incomplete_ids": list(incomplete), "completed_ids": [], "submitted_ids": [SID],
        "schema_version": 2,
    }


class Runner:
    """Stub for subprocess.run: answers the version probe, then writes a report into cwd."""

    def __init__(self, report=None, version=SWEBENCH_PINNED_VERSION, probe_rc=0, raises=None):
        self.report, self.version, self.probe_rc, self.raises = report, version, probe_rc, raises
        self.calls = []
        self.sent = None

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[1] == "-c":
            return subprocess.CompletedProcess(cmd, self.probe_rc, f"{self.version}\n", "")
        if self.raises:
            raise self.raises
        cwd = Path(kw["cwd"])
        self.sent = json.loads(Path(cmd[cmd.index("--predictions_path") + 1]).read_text())[0]["model_patch"]
        if self.report is not None:
            run_id = cmd[cmd.index("--run_id") + 1]
            (cwd / f"quoin-bench.{run_id}.json").write_text(json.dumps(self.report))
        return subprocess.CompletedProcess(cmd, 0, "resolved\n", "")


def _patch(tmp_path, data=PATCH):
    p = tmp_path / "diff.patch"
    p.write_bytes(data)
    return p


def _judge(tmp_path, runner, **kw):
    return judge_swebench_verified(SPEC, _patch(tmp_path, kw.pop("data", PATCH)), "run1",
                                   tmp_path, cell=kw.pop("cell", "simple-claude"), run=runner, **kw)


def test_command_names_verified_dataset():
    cmd = build_verified_command("py", SID, "/p.json", "rid")
    assert cmd[cmd.index("--dataset_name") + 1] == "princeton-nlp/SWE-bench_Verified"
    assert cmd[cmd.index("--split") + 1] == "test"
    assert cmd[cmd.index("--instance_ids") + 1] == SID
    assert "--namespace" not in cmd
    assert build_verified_command("py", SID, "/p", "r", "")[-2:] == ["--namespace", ""]


def test_lite_spec_still_uses_lite_dataset(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    task_dir = tmp_path / "t"
    task_dir.mkdir()
    (task_dir / "diff.patch").write_text("diff --git a/x b/x\n")
    lite = {"id": "swebench_lite_001", "source": "swebench_lite", "source_id": "a__b-1"}
    judge_task("swebench_lite_001", task_dir, "r", evidence_dir=tmp_path, task_spec=lite)
    assert seen and "princeton-nlp/SWE-bench_Lite" in seen[0]


def test_dispatch_routing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(judge, "judge_swebench_verified",
                        lambda *a, **k: calls.append((a, k)) or JudgeResult("x", "swebench_verified", "pass", None, 0.0))
    judge_task("verified_easy_000", tmp_path, "r", task_spec=SPEC,
               judge_context={"cell": "quoin-claude", "snapshot_revision": "rev"})
    assert calls and calls[0][1] == {"cell": "quoin-claude", "snapshot_revision": "rev",
                                     "emulate_x86": False}
    out = judge_task("verified_easy_000", tmp_path, "r")
    assert out.verdict == "error" and out.source_benchmark == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize("report,expected", [
    (_report(resolved=[SID]), ("pass", True)),
    (_report(unresolved=[SID]), ("fail", True)),
    (_report(empty=[SID]), ("fail", True)),
    (_report(error=[SID]), ("error", False)),
    (_report(incomplete=[SID]), ("error", False)),
    (_report(), ("error", False)),
])
def test_report_parser(tmp_path, report, expected):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(report))
    verdict, _reason, final = parse_verified_report(path, SID)
    assert (verdict, final) == expected


def test_report_parser_missing_and_malformed(tmp_path):
    assert parse_verified_report(tmp_path / "none.json", SID) == ("error", "report-missing", False)
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    assert parse_verified_report(bad, SID) == ("error", "report-missing", False)


def test_pass_sends_patch_bytes_unchanged(tmp_path):
    r = Runner(report=_report(resolved=[SID]))
    out = _judge(tmp_path, r)
    assert out.verdict == "pass" and out.extra["judge_final"] is True
    assert r.sent.encode("utf-8") == PATCH
    assert b"ctx  \r\n" in r.sent.encode("utf-8")


def test_unresolved_is_fail_even_if_stdout_says_resolved(tmp_path):
    out = _judge(tmp_path, Runner(report=_report(unresolved=[SID])))
    assert out.verdict == "fail" and out.extra["judge_final"] is True


def test_non_utf8_patch_is_not_judged(tmp_path):
    r = Runner(report=_report(resolved=[SID]))
    out = _judge(tmp_path, r, data=b"diff\n+caf\xe9\n")
    assert (out.verdict, out.extra["failure_reason"]) == ("error", "patch-not-utf8")
    assert r.calls == []


def test_distinct_run_ids_per_cell(tmp_path):
    a = _judge(tmp_path, Runner(report=_report(resolved=[SID])), cell="simple-claude")
    b = _judge(tmp_path, Runner(report=_report(resolved=[SID])), cell="quoin-claude")
    assert a.extra["judge_run_id"] != b.extra["judge_run_id"]


def test_empty_patch_fails_without_running(tmp_path):
    r = Runner()
    out = _judge(tmp_path, r, data=b"  \n")
    assert out.verdict == "fail" and out.extra["failure_reason"] == "empty-patch" and r.calls == []


def test_version_and_install_problems_are_not_final(tmp_path):
    mismatch = _judge(tmp_path, Runner(version="3.0.0"))
    assert mismatch.extra["failure_reason"] == "swebench-version-mismatch"
    assert mismatch.extra["judge_final"] is False
    missing = _judge(tmp_path, Runner(probe_rc=1))
    assert missing.extra["failure_reason"] == "swebench-not-installed"
    assert missing.verdict == "error" and missing.extra["judge_final"] is False


def test_timeout_oserror_and_exception_are_contained(tmp_path):
    cases = [
        (subprocess.TimeoutExpired("x", 1), "judge-timeout"),
        (OSError("disk"), "judge-oserror"),
        (RuntimeError("boom"), "judge-exception"),
    ]
    for exc, reason in cases:
        out = _judge(tmp_path, Runner(raises=exc))
        assert (out.verdict, out.extra["failure_reason"], out.extra["judge_final"]) == ("error", reason, False)


def test_missing_interpreter_does_not_raise(tmp_path):
    out = judge_swebench_verified(SPEC, _patch(tmp_path), "r", tmp_path, cell="c",
                                  python=str(tmp_path / "no-such-python"))
    assert out.extra["failure_reason"] == "swebench-python-missing"
    assert out.extra["judge_final"] is False


def test_missing_report_is_non_final_error(tmp_path):
    out = _judge(tmp_path, Runner(report=None))
    assert out.extra["failure_reason"] == "report-missing" and out.extra["judge_final"] is False


def test_rejudge_clears_old_evaluation_dir(tmp_path):
    _judge(tmp_path, Runner(report=_report(resolved=[SID])))
    stale = tmp_path / "swebench_eval" / "stale.txt"
    stale.write_text("x")
    _judge(tmp_path, Runner(report=_report(resolved=[SID])))
    assert not stale.exists()


def _cache(root):
    base = root / "princeton-nlp___SWE-bench_Verified" / "default" / "0.0.0"
    (base / "abc123").mkdir(parents=True)
    (root / "princeton-nlp___SWE-bench_Verified_default_0.0.0_abc123.lock").write_text("")
    (base / "abc123.lock").write_text("")
    return root


def test_hf_revisions_via_datasets_cache_and_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    root = _cache(tmp_path / "ds")
    monkeypatch.setenv("HF_DATASETS_CACHE", str(root))
    assert _hf_cached_revisions() == ["abc123"]
    monkeypatch.delenv("HF_DATASETS_CACHE")
    home = tmp_path / "home"
    _cache(home / "datasets")
    monkeypatch.setenv("HF_HOME", str(home))
    assert _hf_cached_revisions() == ["abc123"]


def test_revision_mismatch_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_DATASETS_CACHE", str(_cache(tmp_path / "ds")))
    out = _judge(tmp_path, Runner(report=_report(resolved=[SID])), snapshot_revision="other")
    assert out.extra["dataset_revision_mismatch"] is True
    ok = _judge(tmp_path, Runner(report=_report(resolved=[SID])), snapshot_revision="abc123")
    assert "dataset_revision_mismatch" not in ok.extra


def test_undeletable_old_evaluation_dir_is_non_final_error(tmp_path, monkeypatch):
    _judge(tmp_path, Runner(report=_report(resolved=[SID])))
    # A silent partial delete leaves the old resolved report in place; the
    # re-judge must not parse it as a fresh, final result.
    monkeypatch.setattr(judge.shutil, "rmtree", lambda *a, **k: None)
    runner = Runner(report=None)
    out = _judge(tmp_path, runner)
    assert out.extra["failure_reason"] == "judge-oserror" and out.extra["judge_final"] is False
    assert runner.sent is None


def test_raising_delete_of_old_evaluation_dir_is_non_final_error(tmp_path, monkeypatch):
    _judge(tmp_path, Runner(report=_report(resolved=[SID])))
    monkeypatch.setattr(judge.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("busy")))
    out = _judge(tmp_path, Runner(report=_report(resolved=[SID])))
    assert out.extra["failure_reason"] == "judge-oserror" and out.extra["judge_final"] is False


# --- x86_64 emulation ----------------------------------------------------------------

import textwrap  # noqa: E402

FLAGS = ["--dataset_name", VERIFIED_DATASET, "--split", "test", "--instance_ids", SID,
         "--predictions_path", "/p.json", "--max_workers", "1", "--timeout", "1800",
         "--run_id", "rid"]


def test_command_without_emulation_is_exactly_today_s():
    assert build_verified_command("py", SID, "/p.json", "rid") == ["py", "-m", "swebench.harness.run_evaluation", *FLAGS]
    assert build_verified_command("py", SID, "/p.json", "rid", emulate_x86=False) == \
        ["py", "-m", "swebench.harness.run_evaluation", *FLAGS]


def test_command_with_emulation_runs_the_shim_with_the_same_flags():
    cmd = build_verified_command("py", SID, "/p.json", "rid", "ns", emulate_x86=True)
    assert cmd[:2] == ["py", str(judge.SHIM_PATH)]
    assert judge.SHIM_PATH.name == "swebench_x86_shim.py" and judge.SHIM_PATH.is_file()
    assert cmd[2:] == [*FLAGS, "--namespace", "ns"]


def test_judge_json_extra_records_the_emulation(tmp_path):
    runner = Runner(report=_report(resolved=[SID]))
    result = _judge(tmp_path, runner, emulate_x86=True)
    assert result.verdict == "pass"
    assert result.extra["emulated"] is True
    assert result.extra["forced_arch"] == "x86_64" and result.extra["pull_platform"] == "linux/amd64"
    assert runner.calls[1][0][1] == str(judge.SHIM_PATH)
    plain = _judge(tmp_path, Runner(report=_report(resolved=[SID])))
    assert "emulated" not in plain.extra


@pytest.mark.parametrize("flag,machine,expected", [
    (True, "arm64", True), (True, "aarch64", True), (True, "ARM64", True),
    (False, "arm64", False), (True, "x86_64", False), (True, "AMD64", False),
])
def test_emulation_is_decided_in_one_function(flag, machine, expected):
    assert judge.emulation_active(flag, machine) is expected


def test_emulation_reads_the_host_when_no_machine_is_given(monkeypatch):
    monkeypatch.setattr(judge.platform, "machine", lambda: "arm64")
    assert judge.emulation_active(True) is True
    monkeypatch.setattr(judge.platform, "machine", lambda: "x86_64")
    assert judge.emulation_active(True) is False


def _fake_packages(root):
    files = {
        "docker/__init__.py": "",
        "docker/models/__init__.py": "",
        "docker/models/images.py": """
            class ImageCollection:
                def pull(self, repository, tag=None, all_tags=False, **kwargs):
                    return {"repository": repository, "kwargs": kwargs}
        """,
        "swebench/__init__.py": "",
        "swebench/harness/__init__.py": "",
        "swebench/harness/run_evaluation.py": """
            import json, platform, sys
            from docker.models.images import ImageCollection
            images = ImageCollection()
            out = {
                "machine": platform.machine(),
                "argv": sys.argv[1:],
                "sys_path": list(sys.path),
                "default_pull": images.pull("repo:tag"),
                "explicit_pull": images.pull("repo:tag", platform="linux/arm64"),
            }
            json.dump(out, open("fake-result.json", "w"))
        """,
    }
    for name, body in files.items():
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))


def test_shim_forces_the_architecture_and_defaults_the_pull_platform(tmp_path):
    fakes, work = tmp_path / "fakes", tmp_path / "work"
    work.mkdir()
    _fake_packages(fakes)
    proc = subprocess.run(
        [sys.executable, str(judge.SHIM_PATH), "--run_id", "rid", "--split", "test"],
        cwd=work, env={"PYTHONPATH": str(fakes), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads((work / "fake-result.json").read_text())
    assert result["machine"] == "x86_64"
    assert result["argv"] == ["--run_id", "rid", "--split", "test"]
    assert result["default_pull"]["kwargs"] == {"platform": "linux/amd64"}
    assert result["explicit_pull"]["kwargs"] == {"platform": "linux/arm64"}
    assert str(judge.SHIM_PATH.parent) not in result["sys_path"]
    record = json.loads((work / "shim-record.json").read_text())
    assert record == {"forced_arch": "x86_64", "pull_platform": "linux/amd64"}


def _pending_run(tmp_path):
    from quoin.benchmarks.harness.session_state import write_session_json

    t_dir = tmp_path / "runs" / "r1" / "simple-claude" / SPEC["id"]
    write_session_json(t_dir, {"judge_required": True, "source": "swebench_verified"})
    (t_dir / "diff.patch").write_bytes(PATCH)
    return tmp_path / "runs"


@pytest.mark.parametrize("flag,machine,expect_shim", [
    (True, "arm64", True), (False, "arm64", False), (True, "x86_64", False),
])
def test_emulation_branches_reach_the_command_through_judge_pending_rows(
        tmp_path, monkeypatch, flag, machine, expect_shim):
    from quoin.benchmarks.harness import runner as runner_mod

    stub = Runner(report=_report(resolved=[SID]))
    original = judge.judge_swebench_verified
    monkeypatch.setattr(
        judge, "judge_swebench_verified", lambda *a, **k: original(*a, run=stub, **k)
    )
    runs = _pending_run(tmp_path)
    rows = runner_mod.judge_pending_rows(
        runs, "r1", [SPEC], emulate_x86=judge.emulation_active(flag, machine),
    )
    assert rows and rows[0]["verdict"] == "pass"
    eval_cmd = stub.calls[1][0]
    assert (eval_cmd[1] == str(judge.SHIM_PATH)) is expect_shim
    if not expect_shim:
        assert eval_cmd[1:3] == ["-m", "swebench.harness.run_evaluation"]
