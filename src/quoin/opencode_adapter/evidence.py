"""Workflow evidence: what a phase's inputs looked like when it finished.

A snapshot hashes the files that make up a task's evidence and records each
repository's head and source state. Comparing a stored snapshot with a fresh
one tells the gate whether anything it vouched for has changed since. The
scope is one shared policy, kept here:

* every regular file under the task folder counts, at any depth, except the
  cost ledger, gate audit files and temporary files (their growth is
  bookkeeping, not evidence);
* a discover snapshot also covers the three discover memory files;
* symlinks are never followed and never hashed;
* a capped hash walk makes the evidence incomplete.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import runstore

DISCOVER_FILES = (
    ".workflow_artifacts/memory/repos-inventory.md",
    ".workflow_artifacts/memory/architecture-overview.md",
    ".workflow_artifacts/memory/dependencies-map.md",
)
SCOPE_VERSION = 1
_ARTIFACT_ROOT = ".workflow_artifacts/"


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str


def in_scope(rel_path: str, task: str) -> bool:
    """Whether a project-relative path under the task folder is evidence."""
    prefix = _ARTIFACT_ROOT + task + "/"
    if not rel_path.startswith(prefix):
        return False
    name = rel_path.rsplit("/", 1)[-1]
    if name == "cost-ledger.md" or fnmatch.fnmatchcase(name, "gate-*.md") or name.endswith(".tmp"):
        return False
    return True


def take_snapshot(
    project_root, task: str, phase: str, *,
    runner: Optional[runstore.GitRunner] = None,
    bytes_runner: Optional[runstore.GitBytesRunner] = None,
    budget_s: float = runstore.REVISIONS_BUDGET_S,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """The current evidence: task file hashes plus per-repository source state."""
    extra = DISCOVER_FILES if phase == "discover" else ()

    def excluded(rel: str) -> bool:
        return not (rel in DISCOVER_FILES or in_scope(rel, task))

    hashes = runstore.hash_inputs(project_root, task, extra, exclude=excluded)
    truncated = "<truncated>" in hashes or any(isinstance(v, dict) for v in hashes.values())
    repos = []
    for item in runstore.repo_revisions(
        project_root, runner=runner, bytes_runner=bytes_runner, budget_s=budget_s, source=True,
    ):
        repos.append({
            "path": item["path"], "head": item["head"],
            "source_dirty": item.get("source_dirty"), "source_digest": item.get("source_digest"),
            "source_error": item.get("source_error"), "error": item["error"],
        })
    return {
        "scope_version": SCOPE_VERSION,
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())),
        "task_hashes": hashes,
        "coverage": "truncated" if truncated else "full",
        "repos": repos,
    }


def well_formed(snapshot: Any) -> bool:
    return (
        isinstance(snapshot, Mapping)
        and isinstance(snapshot.get("task_hashes"), Mapping)
        and isinstance(snapshot.get("repos"), list)
        and all(isinstance(r, Mapping) and isinstance(r.get("path"), str) for r in snapshot["repos"])
    )


def _compare_tasks(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> List[Finding]:
    if (
        recorded.get("coverage") != "full" or current.get("coverage") != "full"
        or recorded.get("scope_version") != SCOPE_VERSION
    ):
        return [Finding("evidence-incomplete", "task file coverage is not complete")]
    before, after = recorded["task_hashes"], current["task_hashes"]
    found: List[Finding] = []
    for path in sorted(set(before) | set(after)):
        if path not in after:
            found.append(Finding("artifact-removed", path))
        elif path not in before:
            found.append(Finding("artifact-added", path))
        elif before[path] != after[path]:
            found.append(Finding("input-hash-changed", path))
    return found


def _compare_repos(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> List[Finding]:
    found: List[Finding] = []
    now = {r["path"]: r for r in current["repos"]}
    seen = set()
    for old in recorded["repos"]:
        path = old["path"]
        seen.add(path)
        if old.get("head") is None or old.get("error"):
            found.append(Finding("evidence-incomplete", path))
            continue
        cur = now.get(path)
        if cur is None:
            found.append(Finding("repo-missing", path))
            continue
        if cur.get("error") or cur.get("head") is None:
            found.append(Finding("repo-content-unverifiable", "%s: %s" % (path, cur.get("error") or "no head")))
            continue
        if cur["head"] != old["head"]:
            found.append(Finding("repo-head-changed", path))
            continue
        if old.get("source_dirty") is None or cur.get("source_dirty") is None:
            found.append(Finding("repo-content-unverifiable", "%s: %s" % (
                path, cur.get("source_error") or old.get("source_error") or "unknown")))
            continue
        if old["source_dirty"] != cur["source_dirty"]:
            found.append(Finding("repo-dirty-changed", path))
            continue
        if cur["source_dirty"]:
            if old.get("source_digest") is None or cur.get("source_digest") is None:
                found.append(Finding("repo-content-unverifiable", "%s: %s" % (
                    path, cur.get("source_error") or old.get("source_error") or "no digest")))
            elif old["source_digest"] != cur["source_digest"]:
                found.append(Finding("repo-content-changed", path))
    for path in sorted(set(now) - seen):
        found.append(Finding("repo-added", path))
    return found


def compare(recorded: Any, current: Any) -> Tuple[List[Finding], List[Finding]]:
    """`(task_findings, repo_findings)` between a stored and a fresh snapshot.
    Details are project-relative or repository paths, never file contents."""
    if not well_formed(recorded) or not well_formed(current):
        bad = [Finding("evidence-incomplete", "the evidence snapshot is malformed")]
        return list(bad), list(bad)
    return _compare_tasks(recorded, current), _compare_repos(recorded, current)


def record_evidence(
    project_root, task: str, stage: Optional[int], phase: str, origin: str, snapshot: Mapping[str, Any], *,
    runs: Sequence[str] = (), boundary: Optional[str] = None,
    clock: Callable[[], float] = time.time, **fields: Any,
) -> Dict[str, Any]:
    """Append a phase entry carrying `snapshot` to the task's workflow state.
    The caller holds the task lock."""
    directory = runstore.store_dir(project_root, create=True)
    state = runstore.load_workflow_state(directory, task) or runstore.new_workflow_state(task, clock)
    entry = runstore.record_phase_entry(state, {
        "stage": stage, "phase": phase, "origin": origin, "runs": list(runs),
        "boundary": boundary, "evidence": dict(snapshot), **fields,
    }, clock)
    runstore.write_workflow_state(directory, state)
    return entry
