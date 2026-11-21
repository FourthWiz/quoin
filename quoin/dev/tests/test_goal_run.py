"""quoin run --autonomous: one headless /goal session per unfinished stage,
stopping when a stage is not marked done, pausing at a usage limit, and
continuing from the first unfinished stage on a rerun."""

import json
import subprocess
from pathlib import Path

import pytest

from quoin import cli, goal_run

ARCH = """# demo — stages
## Overview
Three stages.
## Stage decomposition
1. {m1} S-01: shared types
   - Repo: shared-schema
   - Scope: add the currency field
   - Class: r0
   - Acceptance: Order has a currency field defaulting to USD
2. {m2} S-02: service
   - Repo: order-service
   - Acceptance: POST /orders accepts currency
3. {m3} S-03: client
   - Repo: web-client
   - Acceptance: createOrder sends currency
## Interfaces between stages
- Order.currency
"""


@pytest.fixture
def project(tmp_path):
    task = tmp_path / ".workflow_artifacts" / "demo"
    task.mkdir(parents=True)
    (task / "architecture.md").write_text(ARCH.format(m1="⏳", m2="⏳", m3="⏳"))
    return tmp_path


def _arch(project):
    return project / ".workflow_artifacts" / "demo" / "architecture.md"


def _mark_done(project, stage_id):
    path = _arch(project)
    path.write_text(path.read_text().replace(f"⏳ {stage_id}:", f"✅ {stage_id}:"))


class FakeClaude:
    """Stands in for `claude -p "/goal ..."`: marks the stage it is asked about done, or not."""

    def __init__(self, project, *, skip=(), limit_at=None):
        self.project, self.skip, self.limit_at, self.calls = project, set(skip), limit_at, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        prompt = argv[2]
        stage = prompt.split("Stage ", 1)[1].split(" ", 1)[0] if "Stage " in prompt else None
        if self.limit_at is not None and stage == self.limit_at:
            out = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "Claude AI usage limit reached|1795300000"}
            return subprocess.CompletedProcess(argv, 1, json.dumps(out), "")
        if stage and stage not in self.skip:
            _mark_done(self.project, stage)
        out = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.5, "result": "done"}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")


def test_parse_stages_reads_marks_repos_and_acceptance(project):
    stages = goal_run.parse_stages(_arch(project).read_text())
    assert [(s.id, s.done, s.repo) for s in stages] == [
        ("S-01", False, "shared-schema"), ("S-02", False, "order-service"), ("S-03", False, "web-client")]
    assert stages[0].acceptance == "Order has a currency field defaulting to USD"
    assert goal_run.parse_stages("# no stages here\n") == []


def test_three_stages_complete_in_order(project):
    fake = FakeClaude(project)
    outcome = goal_run.run(project, "demo", runner=fake, printer=lambda _l: None)
    assert outcome.status == "complete" and outcome.stages == ["S-01", "S-02", "S-03"]
    assert outcome.exit_code() == 0
    prompts = [argv[2] for argv, _ in fake.calls]
    assert [p.split()[2] for p in prompts] == ["S-01", "S-02", "S-03"]
    assert all(kw["cwd"] == str(project.resolve()) for _, kw in fake.calls)
    assert all(kw["env"]["QUOIN_HEADLESS_CHILD"] == "1" for _, kw in fake.calls)


def test_goal_condition_carries_acceptance_and_test_evidence(project):
    fake = FakeClaude(project)
    goal_run.run(project, "demo", runner=fake, printer=lambda _l: None)
    argv = fake.calls[1][0]
    assert argv[:2] == ["claude", "-p"] and argv[2].startswith("/goal Stage S-02 (service) of task demo is done in order-service: POST /orders accepts currency.")
    assert "affected_tests.py" in argv[2] and "marked ✅" in argv[2] and "Do not push" in argv[2]
    assert argv[argv.index("--allowedTools") + 1:argv.index("--output-format")] == list(goal_run.ALLOWED_TOOLS)
    assert "--dangerously-skip-permissions" not in argv


def test_a_stage_left_unmarked_stops_the_run(project):
    fake = FakeClaude(project, skip={"S-02"})
    outcome = goal_run.run(project, "demo", runner=fake, printer=lambda _l: None)
    assert outcome.status == "stopped" and outcome.stages == ["S-01"] and outcome.exit_code() == 1
    assert "S-02" in outcome.message and len(fake.calls) == 2


def test_a_usage_limit_pauses_and_a_rerun_continues(project):
    first = goal_run.run(project, "demo", runner=FakeClaude(project, limit_at="S-02"), printer=lambda _l: None)
    assert first.status == "paused" and first.exit_code() == 75 and first.stages == ["S-01"]
    again = FakeClaude(project)
    second = goal_run.run(project, "demo", runner=again, printer=lambda _l: None)
    assert second.status == "complete" and second.stages == ["S-02", "S-03"]
    log = (project / ".workflow_artifacts" / "demo" / "goal-run.log").read_text()
    assert "S-02 start" in log and "S-03 end" in log


def test_finished_task_starts_nothing(project):
    _arch(project).write_text(ARCH.format(m1="✅", m2="✅", m3="✅"))
    fake = FakeClaude(project)
    assert goal_run.run(project, "demo", runner=fake, printer=lambda _l: None).status == "complete"
    assert fake.calls == []


def test_task_without_stages_runs_its_brief_as_one_goal(tmp_path):
    task = tmp_path / ".workflow_artifacts" / "solo"
    task.mkdir(parents=True)
    (task / "brief.md").write_text("# solo\n## Request\nfix the thing\n")
    fake = FakeClaude(tmp_path)
    outcome = goal_run.run(tmp_path, "solo", runner=fake, printer=lambda _l: None)
    assert outcome.status == "complete" and len(fake.calls) == 1
    assert fake.calls[0][0][2].startswith("/goal Task solo is done as .workflow_artifacts/solo/brief.md asks")


def test_missing_task_is_invalid(tmp_path):
    assert goal_run.run(tmp_path, "nope", runner=None).status == "invalid"
    (tmp_path / ".workflow_artifacts" / "empty").mkdir(parents=True)
    assert goal_run.run(tmp_path, "empty", runner=None).exit_code() == 2


def test_budget_is_passed_to_each_session(project):
    fake = FakeClaude(project)
    goal_run.run(project, "demo", runner=fake, max_budget_usd=4.0, printer=lambda _l: None)
    argv = fake.calls[0][0]
    assert argv[argv.index("--max-budget-usd") + 1] == "4.0"


def test_cli_dry_run_prints_the_next_command(project, capsys):
    _mark_done(project, "S-01")
    code = cli.main(["run", "--autonomous", "demo", "--project-root", str(project), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0 and '"stage": "S-02"' in out and "/goal Stage S-02" in out
