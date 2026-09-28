#!/usr/bin/env python3
"""quoin/core/scripts/task_bookkeeping.py — task-folder bookkeeping classifier.

Sorts the workflow's task folders into done / abandoned / nearly-done /
in-progress / not-a-task, backed by disk facts (EOT preflight evidence, session
files, workflow phase) plus an optional fail-open GitHub PR probe. Read-only
(`classify`); folder moves are performed only by `apply`, one task at a time.

This is a PORTABLE CORE module (quoin/quoin/core/scripts/). It loads its
siblings `status_graph.py` and `path_resolve.py` via spec_from_file_location
(the `_load_core` pattern used by `dashboard_model.py`) rather than package
imports, so it works whether or not `quoin/quoin/` is on `sys.path`.

Public API:
  scan_candidates(wa: Path) -> list[Path]
  activity(task_dir: Path) -> tuple[float, int]
  load_eot(task_dir: Path, session_index=None) -> EotState
  parse_stage_ids(arch_text: str) -> tuple[set[int], set[int]]
  classify(root, *, now=None, stale_days=14, idle_days=30, pr_lookup=None,
           gh_enabled=True, repo_dirs=None) -> dict
  main(argv=None) -> int
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Sibling-core module loaders (spec_from_file_location, per dashboard_model.py)
# ---------------------------------------------------------------------------

def _load_core(name: str):
    path = Path(__file__).resolve().parent / f"{name}.py"
    if not path.exists():
        raise ImportError(f"Cannot load {name}: {path} not found")
    module_key = f"_task_bookkeeping_{name}"
    spec = importlib.util.spec_from_file_location(module_key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {name} at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = mod
    spec.loader.exec_module(mod)
    return mod


_status_graph = _load_core("status_graph")
_path_resolve = _load_core("path_resolve")

detect_phase = _status_graph.detect_phase
_is_drive_conflict = _status_graph._is_drive_conflict
SECTION_RE = _path_resolve.SECTION_RE

# Local union: correct even before the one-token status_graph change
# (adding "trash" to _EXCLUDED_NAMES) lands.
_EXCLUDED_NAMES = frozenset(_status_graph._EXCLUDED_NAMES | {"trash"})


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _subprocess_timeout() -> int:
    """Self-contained local copy — do NOT cross-import (repo convention)."""
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def _is_dotfile(name: str) -> bool:
    return name.startswith(".")


def _iso(epoch: float) -> str:
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _list_regular_entries(task_dir: Path):
    """Direct children of task_dir, excluding dotfiles and Drive-conflict names."""
    out = []
    try:
        for entry in task_dir.iterdir():
            if _is_dotfile(entry.name) or _is_drive_conflict(entry.name):
                continue
            out.append(entry)
    except (OSError, PermissionError):
        pass
    return out


def scan_candidates(wa: Path) -> list:
    """Direct child dirs of the task-artifacts root, skipping files, symlinks
    and excluded names (memory/cache/finalized/security-review/trash)."""
    out = []
    try:
        entries = sorted(wa.iterdir(), key=lambda p: p.name)
    except (OSError, PermissionError):
        return out
    for entry in entries:
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        if entry.name in _EXCLUDED_NAMES:
            continue
        out.append(entry)
    return out


def activity(task_dir: Path):
    """Return (last_activity_epoch, file_count): recursive walk excluding the
    nested <task>/finalized/ subtree, Drive-conflict names and dotfiles.

    Empty -> (directory's own mtime, 0).
    """
    mtimes: list = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(str(task_dir)):
        dp = Path(dirpath)
        if dp == task_dir:
            dirnames[:] = [d for d in dirnames if d != "finalized"]
        dirnames[:] = [d for d in dirnames if not _is_dotfile(d) and not _is_drive_conflict(d)]
        for fname in filenames:
            if _is_dotfile(fname) or _is_drive_conflict(fname):
                continue
            try:
                st = (dp / fname).stat()
            except OSError:
                continue
            mtimes.append(st.st_mtime)
            count += 1
    if mtimes:
        return max(mtimes), count
    try:
        return task_dir.stat().st_mtime, 0
    except OSError:
        return 0.0, 0


def _fingerprint(last_activity_epoch: float, file_count: int) -> str:
    return f"{int(last_activity_epoch)}:{file_count}"


def _dir_is_effectively_empty(d: Path) -> bool:
    try:
        for entry in d.iterdir():
            if _is_dotfile(entry.name):
                continue
            return False
    except (OSError, PermissionError):
        return True
    return True


def is_stub(task_dir: Path) -> bool:
    """No regular files other than root cost-ledger.md / task-source.md and
    dotfiles; empty subdirectories do not count."""
    for entry in _list_regular_entries(task_dir):
        if entry.is_dir():
            if not _dir_is_effectively_empty(entry):
                return False
            continue
        if entry.name in ("cost-ledger.md", "task-source.md"):
            continue
        return False
    return True


_RECOGNISED_ARTIFACT_FILES = frozenset({
    "cost-ledger.md", "architecture.md", "current-plan.md", "spec.md",
    "enriched-prompt.md", "task-source.md", "user-decisions.md",
    "eot-preflights.json",
})
_RECOGNISED_PREFIXES = ("critic-response-", "review-", "gate-")
_STAGE_DIR_RE = re.compile(r"^stage-(\d+)$")
_STAGE_DIR_ANY_RE = re.compile(r"^stage-(.+)$")


def _has_stage_dirs(task_dir: Path) -> bool:
    for entry in _list_regular_entries(task_dir):
        if entry.is_dir() and _STAGE_DIR_ANY_RE.match(entry.name):
            return True
    return False


def _has_recognised_artifact(task_dir: Path) -> bool:
    names = {e.name for e in _list_regular_entries(task_dir) if e.is_file()}
    if names & _RECOGNISED_ARTIFACT_FILES:
        return True
    if any(n.startswith(p) for n in names for p in _RECOGNISED_PREFIXES):
        return True
    if _has_stage_dirs(task_dir):
        return True
    if (task_dir / "finalized").is_dir():
        return True
    return False


def not_a_task_shape(task_dir: Path) -> bool:
    names = {e.name for e in _list_regular_entries(task_dir) if e.is_file()}
    if "program.md" in names:
        has_core = (
            "cost-ledger.md" in names
            or "architecture.md" in names
            or "current-plan.md" in names
            or _has_stage_dirs(task_dir)
        )
        if not has_core:
            return True
    if not _has_recognised_artifact(task_dir):
        return True
    return False


def multi_stage_shape(task_dir: Path) -> bool:
    """Rule 4: any stage-* dir, OR a nested <task>/finalized/ dir. A bare
    '## Stage decomposition' heading with no stage dirs does NOT qualify."""
    if _has_stage_dirs(task_dir):
        return True
    if (task_dir / "finalized").is_dir():
        return True
    return False


# ---------------------------------------------------------------------------
# EOT evidence
# ---------------------------------------------------------------------------

@dataclass
class EotState:
    status: str  # "complete" | "more-work" | "incomplete" | "absent"
    archive_type: Optional[str] = None
    commit_hash: Optional[str] = None
    stage: Optional[str] = None
    branch: Optional[str] = None
    marker: bool = False  # finalized_by_end_of_task: session marker (corroboration only)


_SESSION_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)\.md$")
_BRANCH_LINE_RE = re.compile(r"(?i)\bbranch\b[^:\n]{0,20}:\s*`?([A-Za-z0-9._/-]+)")


class SessionIndex:
    """Reads memory/sessions/*.md once; key = filename minus date prefix,
    minus '.md', minus a trailing '-orchestrator'; exact-equality lookup."""

    def __init__(self, root: Path, task_names: Optional[set] = None):
        self._markers = set()
        self._branches: dict = {}
        sessions_dir = root / ".workflow_artifacts" / "memory" / "sessions"
        try:
            entries = sorted(sessions_dir.iterdir(), key=lambda p: p.name)
        except (OSError, PermissionError):
            entries = []
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".md"):
                continue
            m = _SESSION_NAME_RE.match(entry.name)
            if not m:
                continue
            slug = m.group(1)
            if slug.endswith("-orchestrator"):
                slug = slug[: -len("-orchestrator")]
            # A session file can only ever be looked up under a name in
            # task_names (the folders classify is actually scoring), so skip
            # the read entirely for everything else — avoids reading every
            # session file on every classify call.
            if task_names is not None and slug not in task_names:
                continue
            try:
                text = entry.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "finalized_by_end_of_task:" in text:
                self._markers.add(slug)
            if slug not in self._branches:
                bm = _BRANCH_LINE_RE.search(text)
                if bm:
                    self._branches[slug] = bm.group(1)

    def has_marker(self, task_name: str) -> bool:
        return task_name in self._markers

    def branch(self, task_name: str) -> Optional[str]:
        return self._branches.get(task_name)


def load_eot(task_dir: Path, session_index: Optional[SessionIndex] = None) -> EotState:
    marker = bool(session_index and session_index.has_marker(task_dir.name))
    preflight_path = task_dir / "eot-preflights.json"
    if not preflight_path.is_file():
        return EotState(status="absent", marker=marker)
    try:
        data = json.loads(preflight_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return EotState(status="absent", marker=marker)
    if not isinstance(data, dict):
        return EotState(status="absent", marker=marker)

    commit_or_abort = data.get("commit_or_abort")
    archive_type = data.get("archive_type")
    # Fields below feed a shell command (commit_hash) or an equality/lookup
    # (branch, stage); a malformed non-string value is treated as absent
    # rather than raised, so one bad preflight file never takes down the
    # whole classify pass.
    commit_hash = data.get("commit_hash")
    if not isinstance(commit_hash, str):
        commit_hash = None
    stage = data.get("stage")
    if not isinstance(stage, str):
        stage = None
    branch = data.get("branch")
    if not isinstance(branch, str):
        branch = None

    if commit_or_abort == "abort":
        status = "incomplete"
    elif archive_type == "none":
        status = "more-work"
    elif archive_type and (commit_hash or marker):
        status = "complete"
    else:
        status = "incomplete"

    return EotState(
        status=status, archive_type=archive_type, commit_hash=commit_hash,
        stage=stage, branch=branch, marker=marker,
    )


# ---------------------------------------------------------------------------
# Single-stage classification
# ---------------------------------------------------------------------------

def single_stage_rules(task_dir: Path, eot: EotState, phase: str, idle_days: int,
                        idle_threshold: int) -> dict:
    name = task_dir.name
    evidence: list = []

    if eot.status == "complete" and phase == "review-gated":
        evidence.append(
            "EOT complete (commit_hash)" if eot.commit_hash else "EOT complete (session marker)"
        )
        evidence.append("phase=review-gated")
        return {"bucket": "done", "evidence": evidence, "next_commands": [], "prompt": False}

    if eot.status == "more-work":
        evidence.append("more work planned at end_of_task")
        return {"bucket": "in-progress", "evidence": evidence, "next_commands": [], "prompt": False}

    if eot.status == "incomplete":
        evidence.append("eot-preflights.json incomplete")
        return {
            "bucket": "nearly-done", "evidence": evidence,
            "next_commands": [f"/end_of_task {name}"], "prompt": False,
        }

    if phase in ("review", "review-gated") and eot.status == "absent":
        evidence.append(f"phase={phase}")
        evidence.append("no eot-preflights.json")
        return {
            "bucket": "nearly-done", "evidence": evidence,
            "next_commands": [f"/end_of_task {name}"], "prompt": False,
        }

    if eot.status == "complete":
        evidence.append("EOT complete")
        evidence.append(f"phase={phase} (below review-gated)")
        return {
            "bucket": "nearly-done", "evidence": evidence,
            "next_commands": [f"/end_of_task {name}"], "prompt": False,
        }

    evidence.append(f"phase={phase}")
    prompt = idle_days > idle_threshold
    return {"bucket": "in-progress", "evidence": evidence, "next_commands": [], "prompt": prompt}


# ---------------------------------------------------------------------------
# Multi-stage classification
# ---------------------------------------------------------------------------

_STAGE_ROW_RE = re.compile(r"^\d+\.\s+(.*?)\bS-(\d+)\b")


def parse_stage_ids(arch_text: str):
    """Lenient stage-decomposition parser. Returns (all stage IDs, completed IDs)."""
    all_ids: set = set()
    completed_ids: set = set()
    m = SECTION_RE.search(arch_text)
    if not m:
        return all_ids, completed_ids
    start = m.end()
    next_h2 = re.search(r"^## ", arch_text[start:], re.MULTILINE)
    end = start + next_h2.start() if next_h2 else len(arch_text)
    section = arch_text[start:end]
    for line in re.finditer(r"^\d+\..*$", section, re.MULTILINE):
        row_m = _STAGE_ROW_RE.match(line.group(0))
        if not row_m:
            continue
        stage_id = int(row_m.group(2))
        prefix = row_m.group(1)
        all_ids.add(stage_id)
        if "✅" in prefix or "✓" in prefix:
            completed_ids.add(stage_id)
    return all_ids, completed_ids


def _live_stage_dirs(task_dir: Path):
    numeric: set = set()
    non_numeric: list = []
    for entry in _list_regular_entries(task_dir):
        if not entry.is_dir():
            continue
        m = _STAGE_DIR_RE.match(entry.name)
        if m:
            numeric.add(int(m.group(1)))
            continue
        if _STAGE_DIR_ANY_RE.match(entry.name):
            non_numeric.append(entry.name)
    return numeric, non_numeric


def _finalized_stage_ids(task_dir: Path):
    numeric: set = set()
    finalized_dir = task_dir / "finalized"
    if not finalized_dir.is_dir():
        return numeric
    try:
        for entry in finalized_dir.iterdir():
            if _is_dotfile(entry.name) or not entry.is_dir():
                continue
            m = _STAGE_DIR_RE.match(entry.name)
            if m:
                numeric.add(int(m.group(1)))
    except (OSError, PermissionError):
        pass
    return numeric


def _normalize_stage_field(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    m = re.match(r"^stage-(.+)$", s)
    if m:
        s = m.group(1)
    if s.isdigit():
        return int(s)
    return s


def multi_stage_rules(task_dir: Path, eot: EotState, idle_days: int, idle_threshold: int,
                       arch_text: str) -> dict:
    name = task_dir.name
    parsed_ids, completed_from_parse = parse_stage_ids(arch_text)
    live_numeric, live_non_numeric = _live_stage_dirs(task_dir)
    finalized_numeric = _finalized_stage_ids(task_dir)

    S = parsed_ids | live_numeric | finalized_numeric
    F = finalized_numeric
    C = completed_from_parse
    L = live_numeric
    X = live_non_numeric

    evidence: list = []
    unstarted = sorted(S - (F | C | L))

    if S and not L and not X and S <= (F | C):
        evidence.append(f"finalized/completed stages: {sorted(F | C)}")
        return {"bucket": "done", "evidence": evidence, "next_commands": [], "prompt": False}

    if S and S <= (F | C | L) and (L or X):
        # Stage-scoped "more work planned" preflight
        if eot.stage and eot.archive_type == "none":
            norm_stage = _normalize_stage_field(eot.stage)
            if norm_stage in L or norm_stage in X:
                evidence.append(f"stage {eot.stage} marked more work planned at end_of_task")
                return {"bucket": "in-progress", "evidence": evidence, "next_commands": [], "prompt": False}

        all_review_gated = True
        for n in sorted(L):
            ph = detect_phase(task_dir / f"stage-{n}").phase
            if ph not in ("review", "review-gated"):
                all_review_gated = False
        for xname in X:
            ph = detect_phase(task_dir / xname).phase
            if ph not in ("review", "review-gated"):
                all_review_gated = False

        if all_review_gated:
            next_commands = [f"/end_of_task stage {n} of {name}" for n in sorted(L)]
            for xname in sorted(X):
                next_commands.append(f"/end_of_task {name}")
                evidence.append(f"live non-numeric stage dir: {xname}")
            evidence.append(f"live stages review-gated: F={sorted(F)} L={sorted(L)} X={sorted(X)}")
            return {"bucket": "nearly-done", "evidence": evidence, "next_commands": next_commands, "prompt": False}

    if unstarted:
        evidence.append(f"unstarted stages: {unstarted}")
    evidence.append(f"F={sorted(F)} C={sorted(C)} L={sorted(L)} X={sorted(X)}")
    prompt = idle_days > idle_threshold
    return {"bucket": "in-progress", "evidence": evidence, "next_commands": [], "prompt": prompt}


# ---------------------------------------------------------------------------
# PR probe — fail-open, injectable, never promotes to done
# ---------------------------------------------------------------------------

@dataclass
class PrInfo:
    state: str = "unknown"  # merged|open|closed|none|unknown
    number: Optional[int] = None
    repo: Optional[str] = None


def _pr_state_from_row(row: dict) -> str:
    if row.get("mergedAt"):
        return "merged"
    state = (row.get("state") or "").upper()
    if state == "OPEN":
        return "open"
    if state == "CLOSED":
        return "closed"
    if state == "MERGED":
        return "merged"
    return "unknown"


class GhProbe:
    """Default pr_lookup implementation: shells out to `gh`, fail-open."""

    def __init__(self, repo_dirs=None):
        self._repo_dirs = list(repo_dirs) if repo_dirs else None
        self._bulk_cache: dict = {}
        self._bulk_ok_cache: dict = {}
        self._repos_cache: Optional[list] = None
        self._status = None
        self._detail = ""

    def preflight(self, root: Path):
        if self._status is not None:
            return self._status, self._detail
        if shutil.which("gh") is None:
            self._status, self._detail = "unavailable", "gh not found on PATH"
            return self._status, self._detail
        try:
            proc = subprocess.run(
                ["gh", "auth", "status", "--hostname", "github.com"],
                capture_output=True, text=True, timeout=_subprocess_timeout(),
            )
        except (subprocess.TimeoutExpired, OSError):
            self._status, self._detail = "unavailable", "gh auth status timed out or failed to run"
            return self._status, self._detail
        if proc.returncode != 0:
            self._status, self._detail = "unavailable", "gh auth status failed"
            return self._status, self._detail
        self._status, self._detail = "ok", ""
        return self._status, self._detail

    def _repos(self, root: Path):
        if self._repo_dirs is not None:
            return [Path(d) for d in self._repo_dirs]
        if self._repos_cache is not None:
            return self._repos_cache
        repos = []
        candidates = [root]
        try:
            candidates.extend(c for c in root.iterdir() if c.is_dir())
        except (OSError, PermissionError):
            pass
        for c in candidates:
            if not (c / ".git").exists():
                continue
            try:
                remote = subprocess.run(
                    ["git", "-C", str(c), "remote", "-v"],
                    capture_output=True, text=True, timeout=_subprocess_timeout(),
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if "github.com" in (remote.stdout or ""):
                repos.append(c)
        self._repos_cache = repos
        return repos

    def _bulk_list(self, repo: Path):
        key = str(repo)
        if key in self._bulk_cache:
            return self._bulk_cache[key]
        data, ok = [], False
        try:
            proc = subprocess.run(
                ["gh", "pr", "list", "--state", "all", "--limit", "300",
                 "--json", "number,state,headRefName,mergedAt"],
                capture_output=True, text=True, timeout=_subprocess_timeout(), cwd=str(repo),
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                ok = True
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
            data, ok = [], False
        self._bulk_cache[key] = data
        self._bulk_ok_cache[key] = ok
        return data

    def __call__(self, task: str, branch_keys, commit_hash, root: Path) -> PrInfo:
        status, _ = self.preflight(root)
        if status != "ok":
            return PrInfo(state="unknown")
        any_unsaturated_ok_no_match = False
        for repo in self._repos(root):
            rows = self._bulk_list(repo)
            ok = self._bulk_ok_cache.get(str(repo), True)
            saturated = len(rows) >= 300
            for bk in branch_keys:
                for r in rows:
                    if r.get("headRefName") == bk:
                        return PrInfo(state=_pr_state_from_row(r), number=r.get("number"), repo=repo.name)
            # Not matched on a branch key in this repo (or there were none):
            # fall back to a commit-SHA search whenever we have a hash to
            # search with, not only when branch_keys is empty.
            if commit_hash:
                try:
                    proc = subprocess.run(
                        ["gh", "pr", "list", "--state", "all", "--search", commit_hash,
                         "--json", "number,state"],
                        capture_output=True, text=True, timeout=_subprocess_timeout(), cwd=str(repo),
                    )
                    sha_rows = json.loads(proc.stdout) if proc.returncode == 0 else []
                except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
                    sha_rows = []
                if sha_rows:
                    r0 = sha_rows[0]
                    return PrInfo(state=_pr_state_from_row(r0), number=r0.get("number"), repo=repo.name)
            if saturated:
                return PrInfo(state="unknown")
            if ok:
                any_unsaturated_ok_no_match = True
        if any_unsaturated_ok_no_match:
            return PrInfo(state="none")
        return PrInfo(state="unknown")


def _pr_lookup_accepts_root(pr_lookup) -> bool:
    """True unless pr_lookup's positional signature clearly has fewer than 4
    params — decided once via introspection, never via a TypeError retry
    (a TypeError raised inside pr_lookup for an unrelated reason, e.g. bad
    input data, must not be mistaken for an arity mismatch)."""
    try:
        params = inspect.signature(pr_lookup).parameters.values()
    except (TypeError, ValueError):
        return True
    positional = [
        p for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 4


def _apply_pr_effects(rows: list, root: Path, pr_lookup, repo_dirs, session_index: SessionIndex):
    if pr_lookup is None:
        pr_lookup = GhProbe(repo_dirs=repo_dirs)

    gh_status = "ok"
    gh_detail = ""
    if isinstance(pr_lookup, GhProbe):
        gh_status, gh_detail = pr_lookup.preflight(root)

    accepts_root = _pr_lookup_accepts_root(pr_lookup)

    for row in rows:
        if row.get("_is_multi_stage"):
            continue
        eot: Optional[EotState] = row.pop("_eot", None)
        name = row["task"]
        branch_keys = []
        if eot and eot.branch:
            branch_keys.append(eot.branch)
        session_branch = session_index.branch(name)
        if session_branch and session_branch not in branch_keys:
            branch_keys.append(session_branch)

        has_commit_key = bool(eot and eot.status != "absent" and eot.commit_hash)
        has_key = bool(branch_keys) or has_commit_key
        if not has_key:
            row["pr"] = {"state": "unknown", "number": None, "repo": None}
            continue

        commit_hash = eot.commit_hash if eot else None
        if accepts_root:
            info = pr_lookup(name, branch_keys, commit_hash, root)
        else:
            info = pr_lookup(name, branch_keys, commit_hash)
        row["pr"] = {"state": info.state, "number": info.number, "repo": info.repo}

        if info.state == "unknown":
            continue
        if row["bucket"] == "done" and info.state in ("open", "closed", "none"):
            row["bucket"] = "nearly-done"
            row["evidence"].append(f"pr {info.state}")
            row["next_commands"] = ["/pr"]
        elif row["bucket"] in ("in-progress", "nearly-done") and info.state == "merged" and (
            eot is None or eot.status == "absent"
        ):
            if row["bucket"] != "nearly-done":
                row["bucket"] = "nearly-done"
            row["evidence"].append("pr merged, no eot-preflights.json")
            if f"/end_of_task {name}" not in row["next_commands"]:
                row["next_commands"] = row["next_commands"] + [f"/end_of_task {name}"]

    return gh_status, gh_detail


# ---------------------------------------------------------------------------
# Options matrix, actions
# ---------------------------------------------------------------------------

_BUCKET_ORDER = ["done", "abandoned", "nearly-done", "in-progress", "not-a-task"]


def _finish_row(name, result, last_activity_epoch, idle, fingerprint, *, is_multi_stage, eot=None):
    bucket = result["bucket"]
    evidence = list(result["evidence"])
    next_commands = list(result["next_commands"])
    prompt = bool(result["prompt"])

    row = {
        "task": name,
        "bucket": bucket,
        "evidence": evidence,
        "last_activity": _iso(last_activity_epoch),
        "idle_days": idle,
        "next_commands": next_commands,
        "prompt": prompt,
        "fingerprint": fingerprint,
        "pr": {"state": "unknown", "number": None, "repo": None},
        "archive_blocked": False,
        "_is_multi_stage": is_multi_stage,
        "_eot": eot,
    }
    return row


def _options_for(row: dict, wa: Path) -> None:
    """Compute recommended_action, options (<=4), archive_blocked in place."""
    bucket = row["bucket"]
    name = row["task"]

    if bucket in ("done",):
        recommended_action = "archive"
        options = ["Archive", "Leave"]
    elif bucket == "abandoned":
        recommended_action = row.get("_recommended_action_override") or "archive"
        options = row.get("_options_override") or ["Archive", "Leave"]
    elif bucket == "nearly-done":
        recommended_action = "choose-closeout"
        options = []
        for cmd in row["next_commands"]:
            head = cmd.split()[0]
            label = f"Print {head}"
            if label not in options:
                options.append(label)
        options.append("Leave")
        options.append("Archive anyway")
        options = options[:4]
    elif bucket == "in-progress":
        if row["prompt"]:
            recommended_action = "prompt-idle"
            options = ["Leave", "Archive", "Trash"]
        else:
            recommended_action = "report"
            options = []
    else:  # not-a-task
        recommended_action = "skip"
        options = []

    archive_blocked = False
    twin_exists = (wa / "finalized" / name).exists()
    eligible_for_block = bucket in ("done", "nearly-done") or (bucket == "in-progress" and row["prompt"])
    if twin_exists and eligible_for_block:
        if any(opt.lower().startswith("archive") for opt in options):
            archive_blocked = True
            options = [o for o in options if not o.lower().startswith("archive")]
            recommended_action = "resolve-manually"
            row["evidence"].append("archive target exists")

    row["recommended_action"] = recommended_action
    row["options"] = options[:4]
    row["archive_blocked"] = archive_blocked
    row.pop("_recommended_action_override", None)
    row.pop("_options_override", None)


def _row_for_marker_silenced(name):
    return name


def classify(root: Path, *, now: Optional[float] = None, stale_days: int = 14,
             idle_days: int = 30, pr_lookup: Optional[Callable] = None,
             gh_enabled: bool = True, repo_dirs: Optional[list] = None) -> dict:
    if now is None:
        now = time.time()
    wa = root / ".workflow_artifacts"
    candidates = scan_candidates(wa)
    session_index = SessionIndex(root, task_names={c.name for c in candidates})

    rows: list = []
    silenced: list = []

    for task_dir in candidates:
        name = task_dir.name

        if not _TASK_NAME_RE.match(name):
            last_activity_epoch, file_count = activity(task_dir)
            idle = int((now - last_activity_epoch) // 86400)
            fingerprint = _fingerprint(last_activity_epoch, file_count)
            row = _finish_row(
                name, {"bucket": "not-a-task", "evidence": ["unsafe folder name"],
                       "next_commands": [], "prompt": False},
                last_activity_epoch, idle, fingerprint, is_multi_stage=False,
            )
            rows.append(row)
            continue

        if (task_dir / ".quoin-not-a-task").exists():
            silenced.append(name)
            continue

        last_activity_epoch, file_count = activity(task_dir)
        idle = int((now - last_activity_epoch) // 86400)
        fingerprint = _fingerprint(last_activity_epoch, file_count)
        finalized_twin = (wa / "finalized" / name).exists()

        if finalized_twin and is_stub(task_dir):
            row = _finish_row(
                name,
                {
                    "bucket": "abandoned",
                    "evidence": [f"finalized/{name} exists; stub re-created after finalization"],
                    "next_commands": [],
                    "prompt": False,
                },
                last_activity_epoch, idle, fingerprint, is_multi_stage=False,
            )
            row["_recommended_action_override"] = "trash-stub"
            row["_options_override"] = ["Trash", "Leave"]
            rows.append(row)
            continue

        if _is_drive_conflict(name):
            row = _finish_row(
                name, {"bucket": "not-a-task", "evidence": ["Drive sync conflict-copy name"],
                       "next_commands": [], "prompt": False},
                last_activity_epoch, idle, fingerprint, is_multi_stage=False,
            )
            rows.append(row)
            continue

        if is_stub(task_dir):
            if idle > stale_days:
                bucket, ev = "abandoned", [f"stub, idle {idle}d > stale threshold {stale_days}d"]
            else:
                bucket, ev = "in-progress", [f"stub, idle {idle}d (fresh start)"]
            row = _finish_row(
                name, {"bucket": bucket, "evidence": ev, "next_commands": [], "prompt": False},
                last_activity_epoch, idle, fingerprint, is_multi_stage=False,
            )
            rows.append(row)
            continue

        if not_a_task_shape(task_dir):
            row = _finish_row(
                name, {"bucket": "not-a-task", "evidence": ["no recognised task artifact"],
                       "next_commands": [], "prompt": False},
                last_activity_epoch, idle, fingerprint, is_multi_stage=False,
            )
            rows.append(row)
            continue

        if multi_stage_shape(task_dir):
            arch_path = task_dir / "architecture.md"
            arch_text = ""
            if arch_path.is_file():
                try:
                    arch_text = arch_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    arch_text = ""
            eot = load_eot(task_dir, session_index)
            result = multi_stage_rules(task_dir, eot, idle, idle_days, arch_text)
            result["evidence"].append("PR check skipped for multi-stage tasks")
            row = _finish_row(name, result, last_activity_epoch, idle, fingerprint, is_multi_stage=True)
            rows.append(row)
            continue

        eot = load_eot(task_dir, session_index)
        if eot.stage:
            # A root preflight scoped to a stage belongs to a stage close-out,
            # not to this single-stage task: ignore it for single-stage rules.
            eot = EotState(status="absent", marker=eot.marker)
        phase = detect_phase(task_dir).phase
        result = single_stage_rules(task_dir, eot, phase, idle, idle_days)
        row = _finish_row(name, result, last_activity_epoch, idle, fingerprint, is_multi_stage=False, eot=eot)
        rows.append(row)

    if gh_enabled:
        gh_status, gh_detail = _apply_pr_effects(rows, root, pr_lookup, repo_dirs, session_index)
    else:
        gh_status, gh_detail = "disabled", ""
        for row in rows:
            row.pop("_eot", None)

    for row in rows:
        _options_for(row, wa)
        row.pop("_is_multi_stage", None)
        row.pop("_eot", None)

    rows.sort(key=lambda r: (_BUCKET_ORDER.index(r["bucket"]), r["task"]))

    return {
        "schema": 1,
        "root": str(root),
        "now": _iso(now),
        "thresholds": {"stale_days": stale_days, "idle_days": idle_days},
        "gh": {"status": gh_status, "detail": gh_detail},
        "silenced": sorted(silenced),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

_TASK_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_EXCLUDED_NAMES_LOWER = frozenset(n.lower() for n in _EXCLUDED_NAMES)


def _validate_task_name(root: Path, task: str):
    """Return (task_dir, error) — error is None on success."""
    if task in (".", ".."):
        return None, "invalid task name"
    if "/" in task or "\\" in task:
        return None, "invalid task name"
    if not _TASK_NAME_RE.match(task):
        return None, "invalid task name"
    # Case-insensitive: on a case-insensitive filesystem (default macOS),
    # "Security-Review" resolves to the same directory as "security-review",
    # so the exclusion has to key on lowercase too, not just exact match.
    if task.lower() in _EXCLUDED_NAMES_LOWER:
        return None, "invalid task name"
    task_dir = root / ".workflow_artifacts" / task
    if task_dir.is_symlink():
        return None, "invalid task name"
    if not task_dir.is_dir():
        return None, "task not found"
    return task_dir, None


def _is_not_a_task_by_disk(root: Path, task_dir: Path) -> bool:
    """Disk rules 0-3, in the same order classify uses: marker, then the stub
    check (an empty or ledger-only folder is never refused, whether or not a
    finalized/ twin exists — this is what lets apply trash/archive an empty
    stub), then drive-conflict, then the general shape check."""
    name = task_dir.name
    if (task_dir / ".quoin-not-a-task").exists():
        return True
    if is_stub(task_dir):
        return False
    if _is_drive_conflict(name):
        return True
    return not_a_task_shape(task_dir)


def apply_action(root: Path, action: str, task: str, *, dry_run: bool = False,
                  expect: Optional[str] = None) -> dict:
    task_dir, err = _validate_task_name(root, task)
    if err:
        return {"ok": False, "error": err, "exit": 3}

    if _is_not_a_task_by_disk(root, task_dir):
        return {"ok": False, "error": "not a task (disk rules 0-3)", "task": task, "exit": 3}

    if expect is not None:
        last_activity_epoch, file_count = activity(task_dir)
        current_fp = _fingerprint(last_activity_epoch, file_count)
        if current_fp != expect:
            return {"ok": False, "error": "fingerprint mismatch", "task": task,
                     "expected": expect, "actual": current_fp, "exit": 3}

    wa = root / ".workflow_artifacts"
    if action == "archive":
        target = wa / "finalized" / task
        if target.exists():
            return {"ok": False, "error": "archive target exists", "task": task,
                     "to": str(target), "exit": 4}
        if dry_run:
            return {"action": action, "task": task, "from": str(task_dir), "to": str(target),
                     "ok": True, "dry_run": True}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(task_dir), str(target))
        except OSError as exc:
            return {"ok": False, "error": f"rename failed: {exc}", "task": task, "exit": 5}
        return {"action": action, "task": task, "from": str(task_dir), "to": str(target), "ok": True}

    if action == "trash":
        date_str = datetime.date.today().isoformat()
        base = wa / "trash" / date_str / task
        target = base
        suffix = 2
        while target.exists():
            target = wa / "trash" / date_str / f"{task}-{suffix}"
            suffix += 1
        if dry_run:
            return {"action": action, "task": task, "from": str(task_dir), "to": str(target),
                     "ok": True, "dry_run": True}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(task_dir), str(target))
        except OSError as exc:
            return {"ok": False, "error": f"rename failed: {exc}", "task": task, "exit": 5}
        return {"action": action, "task": task, "from": str(task_dir), "to": str(target), "ok": True}

    return {"ok": False, "error": f"unknown action {action!r}", "task": task, "exit": 2}


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _render_json(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _render_table(data: dict) -> str:
    lines = []
    counts: dict = {}
    for row in data["rows"]:
        counts[row["bucket"]] = counts.get(row["bucket"], 0) + 1
    summary_parts = ", ".join(f"{b} {counts.get(b, 0)}" for b in _BUCKET_ORDER[:-1])
    lines.append(f"[cleanup] tasks: {summary_parts}")

    header = f"{'task':<40} {'bucket':<12} {'evidence':<50} {'last activity':<12} {'recommended action'}"
    lines.append(header)
    for row in data["rows"]:
        evidence = "; ".join(row["evidence"][:2])
        last_activity_date = row["last_activity"][:10]
        lines.append(
            f"{row['task']:<40} {row['bucket']:<12} {evidence:<50} {last_activity_date:<12} {row['recommended_action']}"
        )
    lines.append(f"gh: {data['gh']['status']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

def _resolve_threshold(cli_raw, env_name: str, default: int) -> int:
    def _try(raw):
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None

    if cli_raw is not None:
        v = _try(cli_raw)
        if v is not None:
            return v
        print(f"[task_bookkeeping] invalid threshold value {cli_raw!r}; using default {default}", file=sys.stderr)
        return default
    env_raw = os.environ.get(env_name)
    if env_raw is not None:
        v = _try(env_raw)
        if v is not None:
            return v
        print(f"[task_bookkeeping] invalid {env_name} value {env_raw!r}; using default {default}", file=sys.stderr)
        return default
    return default


def _resolve_root(root_arg: Optional[str]) -> Optional[Path]:
    if root_arg:
        p = Path(root_arg).resolve()
        return p if (p / ".workflow_artifacts").is_dir() else None
    cwd = Path.cwd()
    if (cwd / ".workflow_artifacts").is_dir():
        return cwd
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task_bookkeeping",
        description="Sort .workflow_artifacts/ task folders by bookkeeping bucket.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    classify_p = sub.add_parser("classify", help="classify task folders (read-only)")
    classify_p.add_argument("--root", default=None)
    classify_p.add_argument("--format", choices=["json", "table"], default="json")
    classify_p.add_argument("--stale-days", default=None)
    classify_p.add_argument("--idle-days", default=None)
    classify_p.add_argument("--now", default=None)
    classify_p.add_argument("--no-gh", action="store_true")
    classify_p.add_argument("--repo-dir", action="append", default=None)

    apply_p = sub.add_parser("apply", help="archive or trash one task folder")
    apply_p.add_argument("action", choices=["archive", "trash"])
    apply_p.add_argument("task")
    apply_p.add_argument("--root", default=None)
    apply_p.add_argument("--dry-run", action="store_true")
    apply_p.add_argument("--expect", default=None)

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    root = _resolve_root(args.root)
    if root is None:
        print("No .workflow_artifacts/ directory found under the given/current root.", file=sys.stderr)
        return 2

    if args.command == "classify":
        stale_days = _resolve_threshold(args.stale_days, "QUOIN_CLEANUP_TASK_STALE_DAYS", 14)
        idle_days = _resolve_threshold(args.idle_days, "QUOIN_CLEANUP_TASK_IDLE_DAYS", 30)
        now = None
        if args.now is not None:
            try:
                now = float(args.now)
            except ValueError:
                now = None
        no_gh = args.no_gh or os.environ.get("QUOIN_CLEANUP_GH") == "0"
        try:
            data = classify(
                root, now=now, stale_days=stale_days, idle_days=idle_days,
                gh_enabled=not no_gh, repo_dirs=args.repo_dir,
            )
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[task_bookkeeping] unexpected error: {exc}", file=sys.stderr)
            return 1
        if args.format == "table":
            print(_render_table(data))
        else:
            print(_render_json(data))
        return 0

    if args.command == "apply":
        result = apply_action(root, args.action, args.task, dry_run=args.dry_run, expect=args.expect)
        print(json.dumps(result, sort_keys=True))
        if result.get("ok"):
            return 0
        return int(result.get("exit", 1))

    return 2


if __name__ == "__main__":
    sys.exit(main())
