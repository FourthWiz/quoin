"""Unattended staged runs through Claude Code's `/goal`.

`quoin run --autonomous <task>` runs each unfinished stage of a staged task as
one headless session, `claude -p "/goal <condition>"`. The condition is the
stage's acceptance checks plus evidence: the session must mark the stage done
in `architecture.md`, commit it, and print the output of `affected_tests.py`
so the goal's evaluator can see the tests that cover the change pass. A task
without a stage list is one stage whose condition is its brief.

After each session the stage list is read again: a stage marked done moves the
run on, anything else stops it. Rerunning the command continues at the first
unfinished stage, which is also how a run cut short by a usage limit resumes;
the run reports `paused` and exits with 75 (temporary failure) in that case.

The session gets a fixed tool allow-list rather than
`--dangerously-skip-permissions`, which an auto-mode classifier can refuse.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ALLOWED_TOOLS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent", "Skill", "TaskCreate", "TaskUpdate")
SESSION_TIMEOUT_S = 6 * 3600
# Tells the session it runs headless: it must wait for long test runs instead
# of ending its turn with work pending (autonomous-mode.md).
HEADLESS_ENV = "QUOIN_HEADLESS_CHILD"
EXIT_OK, EXIT_STOPPED, EXIT_INVALID, EXIT_PAUSED = 0, 1, 2, 75

_SECTION = re.compile(r"^##\s+Stage decomposition\s*$", re.M)
_NEXT_H2 = re.compile(r"^##\s+", re.M)
_ROW = re.compile(r"^\s*(\d+)\.\s+(\S+)\s+(S-\d+)\s*:\s*(.+?)\s*$")
_FIELD = re.compile(r"^\s+-\s+(Repo|Acceptance|Scope)\s*:\s*(.+?)\s*$")
_LIMIT = re.compile(r"usage limit|rate limit|limit reached|too many requests|\b429\b|overloaded", re.I)


@dataclass
class Stage:
    id: str
    name: str
    done: bool
    repo: str = ""
    acceptance: str = ""
    scope: str = ""


@dataclass
class Outcome:
    status: str  # complete, paused, stopped, invalid
    stages: list = field(default_factory=list)
    message: str = ""

    def exit_code(self) -> int:
        return {"complete": EXIT_OK, "paused": EXIT_PAUSED, "invalid": EXIT_INVALID}.get(self.status, EXIT_STOPPED)


def parse_stages(text: str) -> list[Stage]:
    """The rows of `## Stage decomposition` with their Repo, Scope and Acceptance lines."""
    match = _SECTION.search(text)
    if not match:
        return []
    body = text[match.end():]
    nxt = _NEXT_H2.search(body)
    body = body[: nxt.start()] if nxt else body
    stages: list[Stage] = []
    for line in body.splitlines():
        row = _ROW.match(line)
        if row:
            mark = row.group(2)
            stages.append(Stage(id=row.group(3), name=row.group(4), done="✅" in mark or "✓" in mark))
            continue
        fld = _FIELD.match(line)
        if fld and stages:
            setattr(stages[-1], fld.group(1).lower(), fld.group(2))
    return stages


def affected_tests_script() -> Path:
    """The staged plugin's copy when installed, else this checkout's."""
    try:
        from quoin import installer

        staged = installer.default_stage_dir() / "scripts" / "affected_tests.py"
        if staged.is_file():
            return staged
    except Exception:  # noqa: BLE001 - fall back to the source tree
        pass
    return Path(__file__).resolve().parents[2] / "quoin" / "scripts" / "affected_tests.py"


def condition(task: str, stage: Optional[Stage], project_root: Path) -> str:
    """The `/goal` condition for one stage, or for a task with no stage list."""
    tests = f'python3 "{affected_tests_script()}" --project-root "{project_root}"'
    evidence = (
        f"As evidence, this session ran `{tests} --repo-root <the repository it changed> --task {task}` after the last "
        "change and printed its output, and that output reports the affected tests passing (exit_reason "
        "affected-green, or docs-only-no-selectors / no-changes when nothing testable changed)."
    )
    if stage is None:
        return (
            f"Task {task} is done as .workflow_artifacts/{task}/brief.md asks, on the task's branch (never main or "
            f"master), committed with a conventional commit message. {evidence} Do not push or open a pull request. If it cannot be done, say why and stop."
        )
    where = f" in {stage.repo}" if stage.repo else ""
    checks = stage.acceptance or "the stage's acceptance criteria in architecture.md hold"
    return (
        f"Stage {stage.id} ({stage.name}) of task {task} is done{where}: {checks}. The work follows "
        f".workflow_artifacts/{task}/architecture.md (and stage-{int(stage.id.split('-')[1])}/brief.md when present), "
        "is committed on the task branch, which has the same name in every repository and is never main or master, "
        f"and the row for {stage.id} in that architecture.md is marked ✅. {evidence} Do not push or open a pull request. If the stage cannot be done, say why and stop."
    )


def argv_for(cond: str, max_budget_usd: Optional[float] = None, model: Optional[str] = None) -> list[str]:
    argv = ["claude", "-p", f"/goal {cond}", "--allowedTools", *ALLOWED_TOOLS, "--output-format", "json"]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", str(max_budget_usd)]
    if model:
        argv += ["--model", model]
    return argv


def _summary(proc: subprocess.CompletedProcess) -> dict:
    """The session's JSON result, or what can be read from its output."""
    try:
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    return {"is_error": proc.returncode != 0, "result": (proc.stdout or "") + (proc.stderr or "")}


def _hit_limit(data: dict) -> bool:
    if data.get("api_error_status") == 429:
        return True
    text = " ".join(str(data.get(k) or "") for k in ("result", "subtype", "terminal_reason", "error"))
    return bool(data.get("is_error")) and bool(_LIMIT.search(text))


def _log(task_dir: Path, line: str) -> None:
    try:
        with (task_dir / "goal-run.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


Runner = Callable[..., subprocess.CompletedProcess]


def run(project_root: str | os.PathLike, task: str, *, runner: Runner = subprocess.run,
        max_budget_usd: Optional[float] = None, dry_run: bool = False, model: Optional[str] = None,
        printer: Callable[[str], None] = print) -> Outcome:
    root = Path(project_root).resolve()
    task_dir = root / ".workflow_artifacts" / task
    if not task_dir.is_dir():
        return Outcome("invalid", message=f"no task folder {task_dir}")
    arch = task_dir / "architecture.md"
    staged = arch.is_file() and bool(parse_stages(arch.read_text(encoding="utf-8")))
    if not staged and not (task_dir / "brief.md").is_file():
        return Outcome("invalid", message=f"{task} has neither a stage list in architecture.md nor a brief.md")

    done: list[str] = []
    while True:
        pending = None
        if staged:
            stages = parse_stages(arch.read_text(encoding="utf-8"))
            pending = next((s for s in stages if not s.done), None)
            if pending is None:
                return Outcome("complete", done, f"all {len(stages)} stages of {task} are done")
        label = pending.id if pending else task
        cond = condition(task, pending, root)
        argv = argv_for(cond, max_budget_usd, model)
        if dry_run:
            printer(json.dumps({"stage": label, "argv": argv}))
            return Outcome("complete", done, "dry run")

        printer(f"quoin run: {label}: starting /goal session")
        _log(task_dir, f"{label} start")
        try:
            proc = runner(argv, cwd=str(root), capture_output=True, text=True, timeout=SESSION_TIMEOUT_S,
                          env={**os.environ, HEADLESS_ENV: "1"})
        except subprocess.TimeoutExpired:
            _log(task_dir, f"{label} timeout")
            return Outcome("stopped", done, f"{label}: the session ran past {SESSION_TIMEOUT_S // 3600} hours")
        except OSError as exc:
            return Outcome("invalid", done, f"could not start claude: {exc}")
        data = _summary(proc)
        cost = data.get("total_cost_usd")
        _log(task_dir, f"{label} end subtype={data.get('subtype')} error={data.get('is_error')} cost={cost} "
                       f"session={data.get('session_id')}")

        if _hit_limit(data):
            return Outcome("paused", done, f"{label}: stopped at a usage limit; rerun `quoin run --autonomous {task}` later "
                                           "to continue from this stage")
        if not staged:
            if data.get("is_error") or data.get("subtype") not in (None, "success"):
                return Outcome("stopped", done, f"{task}: the /goal session ended without success "
                                                f"({data.get('subtype') or 'error'})")
            return Outcome("complete", [task], f"{task} is done")
        refreshed = {s.id: s for s in parse_stages(arch.read_text(encoding="utf-8"))}
        if not refreshed.get(pending.id, pending).done:
            reason = data.get("subtype") or ("error" if data.get("is_error") else "goal not met")
            return Outcome("stopped", done, f"{pending.id}: the /goal session ended ({reason}) without marking the "
                                            "stage ✅; see goal-run.log and the session transcript")
        done.append(pending.id)
        printer(f"quoin run: {pending.id}: done" + (f" (${cost:.2f})" if isinstance(cost, (int, float)) else ""))
