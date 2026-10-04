"""Shared, non-collected helpers for the coordinator end-to-end suites.

`WorkflowProject` is an installed git project with the real driver pointed at
the fake executable. `workflow_scenario` scripts every command the coordinator
launches so each writes the artifacts an agent would write.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import _opencode_driver_helpers as dh
import _opencode_gate_helpers as gh
import _opencode_handoff_helpers as hh
from _opencode_run_helpers import InstalledProject
from quoin import cli
from quoin.opencode_adapter import runstore

TASK = "demo"
fake = dh.fake
CRITIC_REVISE_TEXT = fake.CRITIC_PASS_TEXT.replace("PASS", "REVISE")
STAGE_DIR = ".workflow_artifacts/demo/stage-%d"


def _step(*items: Dict[str, Any]) -> Dict[str, Any]:
    return {"steps": [fake._step_start("prt_s1"), *items, fake._step_finish("prt_f1", "stop"),
                      {"do": "exit", "code": 0}]}


def _write(path: str, text: str) -> Dict[str, Any]:
    return {"do": "write_file", "path": path, "content": text}


def _try(path: str, text: str) -> Dict[str, Any]:
    return fake._try_write(path, text)


def discover_attempt() -> Dict[str, Any]:
    return _step(*[_write(rel, text) for rel, text in (
        (".workflow_artifacts/memory/repos-inventory.md", gh.DISCOVER_TEXT["repos-inventory.md"]),
        (".workflow_artifacts/memory/architecture-overview.md", gh.DISCOVER_TEXT["architecture-overview.md"]),
        (".workflow_artifacts/memory/dependencies-map.md", gh.DISCOVER_TEXT["dependencies-map.md"]),
    )])


def workflow_scenario(
    *, stages: int = 2, critic_rounds: Optional[Dict[int, List[str]]] = None,
    extra: Optional[Dict[str, List[Dict[str, Any]]]] = None, review_text: Any = gh.REVIEW,
    implement_steps: Optional[Callable[[int], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """A scenario table for the whole sequence. `critic_rounds[stage]` lists the
    verdict of each critic run (default: REVISE then PASS for stage 1, PASS after);
    `extra[key]` replaces the attempts of one command key."""
    critic_rounds = critic_rounds or {1: ["REVISE", "PASS"]}
    commands: Dict[str, Dict[str, Any]] = {
        "quoin-discover": {"attempts": [discover_attempt()]},
        "quoin-architect": {"attempts": [_step(_write(".workflow_artifacts/demo/architecture.md", gh.ARCHITECTURE))]},
    }
    for stage in range(1, stages + 1):
        sdir = STAGE_DIR % stage
        commands["quoin-plan@%d" % stage] = {"attempts": [
            _step(_write(sdir + "/current-plan.md", gh.PLAN)),
            _step(_write(sdir + "/current-plan.md", gh.PLAN + "\nrevised after the critic\n")),
        ]}
        verdicts = critic_rounds.get(stage, ["PASS"])
        commands["quoin-critic@%d" % stage] = {"attempts": [
            _step(_try("%s/critic-response-%d.md" % (sdir, number),
                       CRITIC_REVISE_TEXT if verdict == "REVISE" else fake.CRITIC_PASS_TEXT))
            for number, verdict in enumerate(verdicts, 1)
        ]}
        steps = implement_steps(stage) if implement_steps else [
            _write("src/stage%d.py" % stage, "VALUE = %d\n" % stage)]
        commands["quoin-implement@%d" % stage] = {"attempts": [_step(*steps)]}
        review_texts = review_text if isinstance(review_text, list) else [review_text]
        commands["quoin-review@%d" % stage] = {"attempts": [
            _step(_try("%s/review-%d.md" % (sdir, number), text))
            for number, text in enumerate(review_texts, 1)]}
    for key, attempts in (extra or {}).items():
        commands[key] = {"attempts": attempts}
    return {"attempts": [_step()], "commands": commands}


class WorkflowProject(InstalledProject):
    def __init__(self, tmp_path: Path, monkeypatch: Any, scenario: Optional[Dict[str, Any]] = None) -> None:
        tmp = Path(tmp_path)
        gh.isolate_git(monkeypatch, tmp / "home")
        project = tmp / "project"
        project.mkdir(parents=True, exist_ok=True)
        gh.git(project, "init", "-q")
        super().__init__(tmp, monkeypatch, scenario if scenario is not None else workflow_scenario())
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("env/\n", encoding="utf-8")
        gh.git(self.root, "add", "src/app.py", ".gitignore")
        gh.git(self.root, "commit", "-q", "-m", "init")
        gh.git(self.root, "switch", "-q", "-c", "feat/demo")
        (self.root / ".workflow_artifacts" / TASK).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(cli, "_make_opencode_driver", self.make_driver)
        monkeypatch.setattr(cli, "_opencode_backoff", lambda n: 0)
        monkeypatch.setattr(cli, "_handoff_evaluator", lambda root: (lambda profile: hh.fixed_scope()))
        self.last_driver: Any = None
        self.last_err = ""

    def make_driver(self, root: Any) -> Any:
        self.last_driver = self.driver_factory()(root)
        return self.last_driver

    def set_scenario(self, scenario: Dict[str, Any]) -> None:
        dh.fake.write_scenario(self.tmp / "s.json", scenario)

    def run_workflow(self, capsys: Any, *extra: str, profile: str = "work") -> Any:
        argv = ["run", TASK, "--runtime", "opencode", "--project-root", str(self.root),
                "--profile", profile, "--workflow"] + list(extra)
        code = cli.main(argv)
        captured = capsys.readouterr()
        self.last_err = captured.err
        lines = captured.out.strip().splitlines()
        return code, json.loads(lines[-1])

    def state(self) -> Dict[str, Any]:
        return runstore.load_workflow_state(runstore.store_dir(self.root), TASK) or {}

    def runs(self) -> List[Dict[str, Any]]:
        return [i for i in self.invocations() if i.get("attempt") is not None]

    def arguments(self) -> List[str]:
        return [" ".join(i["parsed"]["message"]) for i in self.runs()]

    def commands(self) -> List[str]:
        return [i["parsed"].get("command") for i in self.runs()]
