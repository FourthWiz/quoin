"""Workflow memory carried between the tasks of a sequence suite.

In a sequence suite (`tiered.sequence_mode`), the tasks of one sequence run in
order, each on a fresh checkout at its own base commit. On the memory route
(QUOIN_BENCH_QUOIN_ROUTE=memory), for a cell whose adapter accepts
`memory_seed_dir`, the `.workflow_artifacts/memory/` the
previous task of the same sequence ended with is copied into the next task's
checkout before its session starts. Nothing else carries: no code, branches,
session state or the arm's auto memory.

After each task the memory it ended with is snapshotted into that task's own
result directory (`memory-snapshot/`), with a `memory-carry.json` record of
what it inherited and what it wrote. A resumed run finds the previous task's
snapshot there, so the next task sees what it would have seen in one pass. A
respawned row is moved aside with its snapshot, and the rerun writes a new one.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
from pathlib import Path
from typing import Optional

SNAPSHOT_DIR = "memory-snapshot"
RECORD_FILE = "memory-carry.json"
EVIDENCE_DIR = "workflow_artifacts_evidence"
LESSONS_FILE = "lessons-learned.md"

# Per-session pipeline state that lives under memory/ but is not memory: run
# state, checkpoints, resume and supervisor records. Carrying it would let one
# task's session leak into the next.
SESSION_STATE_DIRS = ("sessions", "checkpoints", "workspaces", "telemetry")
SESSION_STATE_PATTERNS = (
    "run-state-*",
    "autonomous-*",
    "auto-resume-*",
    "child-watch-*",
    "run-supervisor-*",
    "run-continue-*",
    "run-notes-*",
    "1m-tier-*",
    "needs-decision-*",
    "worktree-probe.txt",
)


def is_sequence_task(task_spec: dict) -> bool:
    return bool(task_spec.get("sequence_id")) and task_spec.get("sequence_position") is not None


def _carried(rel: Path) -> bool:
    if rel.parts and rel.parts[0] in SESSION_STATE_DIRS:
        return False
    return not any(fnmatch.fnmatch(rel.name, pat) for pat in SESSION_STATE_PATTERNS)


def _copy_memory(src: Path, dest: Path, keep_times: bool = True) -> list:
    """Copy the carried part of a memory/ tree; regular files only, no symlinks.

    Seeding a checkout passes keep_times=False, so staleness checks (discovery
    refreshes when repo-heads.md is a week old) count from the seed, and a
    resume days later sees what a single pass would have.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copied = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_symlink() or not path.is_file() or not _carried(rel):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copy2 if keep_times else shutil.copyfile)(path, target)
        copied.append(rel.as_posix())
    return copied


def file_manifest(root: Path) -> list:
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            out.append({
                "path": path.relative_to(root).as_posix(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    return out


def count_lessons(memory_dir: Optional[Path]) -> int:
    """Entries in lessons-learned.md: lines that open a `## ` heading."""
    if memory_dir is None:
        return 0
    path = memory_dir / LESSONS_FILE
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(1 for line in text.splitlines() if line.startswith("## "))


def _read_record(task_dir: Path) -> Optional[dict]:
    try:
        return json.loads((task_dir / RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def find_seed(cell_dir: Path, task_spec: dict) -> tuple:
    """(snapshot dir, task id) of the latest earlier task in the sequence.

    Looks only at rows of this cell in this run. The nearest earlier position
    with a snapshot wins, so a task whose session left no memory evidence is
    skipped over rather than resetting the sequence. (None, None) for the
    first task, or when no earlier task left a snapshot.
    """
    seq = task_spec.get("sequence_id")
    pos = int(task_spec.get("sequence_position"))
    best = None
    if not cell_dir.is_dir():
        return None, None
    for task_dir in cell_dir.iterdir():
        if not task_dir.is_dir() or task_dir.name == task_spec.get("id"):
            continue
        rec = _read_record(task_dir)
        if not rec or rec.get("sequence_id") != seq:
            continue
        other = rec.get("sequence_position")
        if not isinstance(other, int) or other >= pos:
            continue
        snap = task_dir / SNAPSHOT_DIR
        if not snap.is_dir():
            continue
        if best is None or other > best[0]:
            best = (other, snap, task_dir.name)
    if best is None:
        return None, None
    return best[1], best[2]


def seed_workdir(workdir: Path, seed_dir: Optional[Path]) -> list:
    """Copy a snapshot into `<workdir>/.workflow_artifacts/memory/`."""
    if seed_dir is None or not seed_dir.is_dir():
        return []
    return _copy_memory(seed_dir, workdir / ".workflow_artifacts" / "memory", keep_times=False)


def finish_task(task_dir: Path, task_spec: dict, seed_dir: Optional[Path],
                inherited_from: Optional[str]) -> dict:
    """Snapshot the task's final memory and write its carry record.

    The final memory is read from the evidence copy the cell made of the
    checkout's `.workflow_artifacts/` before the checkout was removed.
    """
    task_dir.mkdir(parents=True, exist_ok=True)
    final = task_dir / EVIDENCE_DIR / "memory"
    snap = task_dir / SNAPSHOT_DIR
    if final.is_dir():
        _copy_memory(final, snap)
        snapshot = SNAPSHOT_DIR
    else:
        if snap.exists():
            shutil.rmtree(snap)
        snapshot = None
    before = count_lessons(seed_dir)
    after = count_lessons(snap if snapshot else None)
    record = {
        "sequence_id": task_spec.get("sequence_id"),
        "sequence_position": int(task_spec.get("sequence_position")),
        "inherited_from": inherited_from,
        "inherited_files": [
            f for f in (file_manifest(seed_dir) if seed_dir else []) if _carried(Path(f["path"]))
        ],
        "lessons_inherited": before,
        "lessons_after": after,
        "lessons_written": max(after - before, 0),
        "snapshot": snapshot,
    }
    (task_dir / RECORD_FILE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record
