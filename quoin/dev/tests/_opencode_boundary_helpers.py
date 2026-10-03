"""Shared, non-collected helpers for the role-boundary and snapshot tests.

`SnapshotProject` is an installed project whose root is a real git repository
with one commit, a staged task and the real OpenCode driver pointed at the fake
executable, so an isolated critic or review run can be exercised end to end.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import _opencode_gate_helpers as gh
import _opencode_helpers as helpers
from _opencode_run_helpers import InstalledProject, FakeClock
from quoin.opencode_adapter import driver, phase_loop, runstore, snapshot

TASK = "demo"
SOURCE_DIR = helpers.SOURCE_DIR


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode()


class SnapshotProject(InstalledProject):
    """`InstalledProject` over a real git repository holding `src/app.py` and a
    `.gitignore` (ignoring `env/`), with task `demo`: an architecture with stage
    rows and a valid `stage-1/current-plan.md`."""

    def __init__(self, tmp_path: Path, monkeypatch: Any, scenario: Any = "record_only") -> None:
        tmp = Path(tmp_path)
        gh.isolate_git(monkeypatch, tmp / "home")
        project = tmp / "project"
        project.mkdir(parents=True, exist_ok=True)
        _git(project, "init", "-q")
        super().__init__(tmp, monkeypatch, scenario)
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("env/\n", encoding="utf-8")
        _git(self.root, "add", "src/app.py", ".gitignore")
        _git(self.root, "commit", "-q", "-m", "init")
        base = gh.task_dir(self.root, TASK)
        gh.write(base / "architecture.md", gh.ARCHITECTURE)
        gh.write(base / "stage-1" / "current-plan.md", gh.PLAN)
        self.removed_state: Optional[Dict[str, Any]] = None

    # -- running ------------------------------------------------------------
    def request(self, kind: str = "review", *, stage: Optional[str] = "1") -> "driver.RunRequest":
        return driver.RunRequest(
            project_root=self.root, task=TASK, stage=stage, phase=kind, profile="work",
        )

    def run_isolated(self, kind: str = "review", *, stage: Optional[str] = "1", **kw: Any) -> "snapshot.IsolatedRun":
        drv = self.driver_factory()(self.root)
        kw.setdefault("cancel", phase_loop.CancelToken())
        kw.setdefault("max_relaunch", 0)
        kw.setdefault("backoff_fn", lambda attempt: 0.0)
        kw.setdefault("clock", FakeClock())
        return snapshot.run_in_snapshot(
            drv, self.request(kind, stage=stage), kind=kind, source_dir=SOURCE_DIR, **kw,
        )

    def run_plain(self, phase: str = "plan") -> Any:
        """A normal (non-isolated) run in the project root."""
        drv = self.driver_factory()(self.root)
        return phase_loop.run_phase(
            drv, self.request(phase), max_relaunch=0, cancel=phase_loop.CancelToken(),
            new_run=True, backoff_fn=lambda attempt: 0.0,
        )

    def spy_on_remove(self, monkeypatch: Any) -> None:
        """Record the effects log and the snapshot's files just before removal."""
        real = snapshot.remove

        def spy(snap: "snapshot.Snapshot") -> bool:
            files = set()
            for base, _dirs, names in os.walk(str(snap.root)):
                if os.path.relpath(base, str(snap.root)).split(os.sep)[0] == ".git":
                    continue
                for name in names:
                    files.add(os.path.relpath(os.path.join(base, name), str(snap.root)).replace(os.sep, "/"))
            self.removed_state = {"effects": self.effects(), "files": files, "root": snap.root}
            return real(snap)

        monkeypatch.setattr(snapshot, "remove", spy)

    # -- observations of the real tree ---------------------------------------
    def tree_state(self) -> Dict[str, Any]:
        """Task file hashes, the repositories' head and source state, and the
        source file contents: what an isolated run must leave unchanged."""
        hashes: Dict[str, str] = {}
        for path in sorted(gh.task_dir(self.root, TASK).rglob("*")):
            if path.is_file():
                hashes[str(path.relative_to(self.root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        repos = [
            {k: r.get(k) for k in ("path", "head", "source_dirty", "source_digest")}
            for r in runstore.repo_revisions(self.root, source=True)
        ]
        src = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in sorted((self.root / "src").rglob("*")) if p.is_file()
        }
        return {"task": hashes, "repos": repos, "src": src}

    def finding_names(self, prefix: str) -> List[str]:
        folder = gh.task_dir(self.root, TASK) / "stage-1"
        return sorted(p.name for p in folder.iterdir() if p.name.startswith(prefix))

    def snapshot_roots(self) -> List[Path]:
        base = self.world.tmp / "state" / "quoin" / "opencode" / "snapshots"
        return sorted(p for p in base.glob("*/*") if p.is_dir()) if base.exists() else []
