"""launch_unattended.py starts `quoin run --autonomous` detached and returns at once."""

import os
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PKG / "core" / "scripts"))
import launch_unattended as lu  # noqa: E402


class FakePopen:
    calls = []

    def __init__(self, argv, **kwargs):
        FakePopen.calls.append((argv, kwargs))
        self.pid = 4242


def _task(tmp_path):
    (tmp_path / ".workflow_artifacts" / "demo").mkdir(parents=True)
    return tmp_path


def test_launch_starts_a_detached_run_and_records_it(tmp_path):
    FakePopen.calls = []
    root = _task(tmp_path)
    out = lu.launch(str(root), "demo", "haiku", 2.0, which=lambda _n: "/bin/quoin", popen=FakePopen)
    assert out["started"] and out["pid"] == 4242
    argv, kwargs = FakePopen.calls[0]
    assert argv == ["/bin/quoin", "run", "--autonomous", "demo", "--project-root", str(root.resolve()),
                    "--model", "haiku", "--max-budget-usd", "2.0"]
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL
    assert (root / ".workflow_artifacts" / "demo" / "unattended.pid").read_text() == "4242\n"
    assert out["log"].endswith("demo/unattended.log") and out["goal_log"].endswith("demo/goal-run.log")


def test_a_second_launch_while_one_runs_is_refused(tmp_path):
    root = _task(tmp_path)
    (root / ".workflow_artifacts" / "demo" / "unattended.pid").write_text(f"{os.getpid()}\n")
    out = lu.launch(str(root), "demo", which=lambda _n: "/bin/quoin", popen=FakePopen)
    assert out["started"] is False and "already running" in out["error"]


def test_a_finished_previous_run_does_not_block(tmp_path):
    FakePopen.calls = []
    root = _task(tmp_path)
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    (root / ".workflow_artifacts" / "demo" / "unattended.pid").write_text(f"{done.pid}\n")
    assert lu.launch(str(root), "demo", which=lambda _n: "/bin/quoin", popen=FakePopen)["started"]


def test_missing_quoin_or_task_is_reported(tmp_path):
    root = _task(tmp_path)
    assert "not on PATH" in lu.launch(str(root), "demo", which=lambda _n: None, popen=FakePopen)["error"]
    assert "no task folder" in lu.launch(str(root), "nope", which=lambda _n: "/bin/quoin", popen=FakePopen)["error"]


def test_skill_is_registered():
    skill = (PKG / "adapters" / "claude" / "skills" / "run-unattended" / "SKILL.md").read_text()
    head = skill.split("\n---", 1)[0]
    assert "name: run-unattended" in head and "model: haiku" in head
    assert (PKG / "core" / "skills" / "run-unattended.md").is_file()
