#!/usr/bin/env python3
"""parallel_stages.py — helpers for running independent stages concurrently.

A staged task lists its stages under ``## Stage decomposition`` in
``architecture.md``; each stage row may carry a ``- Depends on: none | S-0N``
line. This script turns that list into dependency waves and gives the
orchestrating skill deterministic git primitives for the parallel path:

  plan       Group the pending (non-✅) stages into waves. Stages inside one
             wave never depend on each other; a wave holds at most ``cap``
             stages (``--max``, else ``QUOIN_PARALLEL_MAX``, default 2). When a
             spend budget is given and already reached, the cap drops to 1 so
             the run continues sequentially.
  worktree   Create a stage branch from the task branch tip and check it out
             in its own linked worktree (default path inside the repo under
             ``.claude/worktrees/``, kept out of ``git status``). An existing
             stage branch or worktree is reused, so ``--resume`` is safe.
  merge      Merge a stage branch into the task branch. Refuses a dirty tree
             or the wrong checked-out branch; on a conflict it aborts the merge
             and verifies the task branch is back at its pre-merge commit, so
             a failed integration never leaves a half-merged tree.
  cleanup    Remove a stage worktree and delete its branch. Refuses an
             unmerged branch unless ``--discard`` is given.
  costs      Per-stage cost breakdown of a cost ledger, keyed on the
             ``stage=`` attribution key.

Every subcommand prints one JSON object on stdout. Exit codes: 0 ok,
2 invalid input (bad architecture, unknown dependency, cycle, git failure),
3 merge refused (dirty tree / wrong branch), 4 merge conflict (aborted).

Stdlib only; portable core (no runtime-specific imports).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SECTION_RE = re.compile(r"^## Stage decomposition\s*$", re.MULTILINE)
NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
# Same row shape as path_resolve.ROW_RE, plus the status marker captured.
ROW_RE = re.compile(
    r"^[0-9]+\.\s+((?:[✅✓✗⏳⛔⚠️\s])*)S-([0-9]+):\s*(.+?)\s*$",
    re.MULTILINE,
)
DEPENDS_RE = re.compile(r"^\s*[-*]\s*Depends on:\s*(.*?)\s*$", re.IGNORECASE)
DEP_REF_RE = re.compile(r"\bS-0*([0-9]+)\b")
STAGE_ARG_RE = re.compile(r"(?:S-?)?0*([0-9]+)", re.IGNORECASE)
# Merges can run hooks and touch many files; give them more room than the
# generic subprocess timeout.
MERGE_TIMEOUT_DEFAULT = 600

DEFAULT_MAX = 2
EXIT_OK, EXIT_INVALID, EXIT_REFUSED, EXIT_CONFLICT = 0, 2, 3, 4


class StageError(Exception):
    """Invalid stage decomposition (unknown dependency, cycle, no section)."""


def stage_id(num: int) -> str:
    return f"S-{num:02d}"


def parse_stages(arch_text: str) -> list[dict]:
    """Return the stage rows of ``## Stage decomposition`` in file order.

    Each entry: ``{"id": "S-01", "num": 1, "name": str, "done": bool,
    "depends_on": ["S-00", ...]}``. ``done`` is true when the row is marked ✅.
    Only an explicit ``Depends on: none`` makes a stage independent; a stage
    with no ``Depends on:`` line depends on the stage before it, which keeps
    older decompositions sequential.
    """
    m = SECTION_RE.search(arch_text)
    if not m:
        raise StageError("architecture.md has no '## Stage decomposition' section")
    body = arch_text[m.end():]
    nxt = NEXT_H2_RE.search(body)
    if nxt:
        body = body[: nxt.start()]

    stages: list[dict] = []
    for line in body.splitlines():
        row = ROW_RE.match(line)
        if row:
            num = int(row.group(2))
            stages.append({
                "id": stage_id(num),
                "num": num,
                "name": row.group(3),
                "done": "✅" in row.group(1) or "✓" in row.group(1),
                "depends_on": None,
            })
            continue
        dep = DEPENDS_RE.match(line)
        if dep and stages:
            value = dep.group(1)
            if value.strip().lower().startswith("none"):
                stages[-1]["depends_on"] = []
            else:
                refs = DEP_REF_RE.findall(value)
                stages[-1]["depends_on"] = [stage_id(int(r)) for r in refs]
    if not stages:
        raise StageError("'## Stage decomposition' lists no stage rows")
    for prev, cur in zip([None] + stages[:-1], stages):
        if cur["depends_on"] is None:
            cur["depends_on"] = [prev["id"]] if prev else []
    return stages


def resolve_cap(max_arg: int | None, spent_usd: float | None,
                budget_usd: float | None) -> tuple[int, str]:
    """Return (cap, reason). Budget reached forces a sequential cap of 1."""
    if max_arg is not None:
        cap, reason = max_arg, "flag"
    else:
        raw = os.environ.get("QUOIN_PARALLEL_MAX", "").strip()
        try:
            cap, reason = (int(raw), "env") if raw else (DEFAULT_MAX, "default")
        except ValueError:
            cap, reason = DEFAULT_MAX, "default (QUOIN_PARALLEL_MAX not an integer)"
    cap = max(cap, 1)
    if budget_usd is not None and spent_usd is not None and spent_usd >= budget_usd:
        return 1, f"budget reached (${spent_usd:.2f} of ${budget_usd:.2f}): sequential"
    return cap, reason


def plan_waves(stages: list[dict], cap: int) -> list[list[str]]:
    """Group pending stages into waves of mutually independent stages.

    A stage's level is one more than the deepest pending stage it depends on;
    completed (✅) stages count as satisfied. Each level is split into chunks
    of at most ``cap`` stages, in file order.
    """
    by_id = {s["id"]: s for s in stages}
    for s in stages:
        for dep in s["depends_on"]:
            if dep not in by_id:
                raise StageError(f"{s['id']} depends on unknown stage {dep}")
            if dep == s["id"]:
                raise StageError(f"{s['id']} depends on itself")

    pending = [s for s in stages if not s["done"]]
    level: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(sid: str) -> int:
        if sid in level:
            return level[sid]
        if sid in visiting:
            raise StageError(f"dependency cycle through {sid}")
        visiting.add(sid)
        deps = [d for d in by_id[sid]["depends_on"] if not by_id[d]["done"]]
        lvl = 1 + max((depth(d) for d in deps), default=-1)
        visiting.discard(sid)
        level[sid] = lvl
        return lvl

    for s in pending:
        depth(s["id"])

    waves: list[list[str]] = []
    for lvl in sorted(set(level.values())):
        ids = [s["id"] for s in pending if level[s["id"]] == lvl]
        for i in range(0, len(ids), cap):
            waves.append(ids[i:i + cap])
    return waves


# ── git helpers ──────────────────────────────────────────────────────────────

def _timeout() -> int:
    try:
        return max(int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30")), 1)
    except ValueError:
        return 30


def _merge_timeout() -> int:
    try:
        return max(int(os.environ.get("QUOIN_PARALLEL_MERGE_TIMEOUT",
                                      str(MERGE_TIMEOUT_DEFAULT))), 1)
    except ValueError:
        return MERGE_TIMEOUT_DEFAULT


def _git(repo: str, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=timeout or _timeout(), check=False,
    )


def _git_ok(repo: str, *args: str) -> str:
    res = _git(repo, *args)
    if res.returncode != 0:
        raise StageError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def stage_branch_name(task_branch: str, sid: str) -> str:
    return f"{task_branch}--{sid.lower()}"


WORKTREE_SUBDIR = ".claude/worktrees"


def default_worktree_path(repo: str, task_branch: str, sid: str) -> Path:
    top = Path(_git_ok(repo, "rev-parse", "--show-toplevel"))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stage_branch_name(task_branch, sid))
    return top / WORKTREE_SUBDIR / slug


def _exclude_worktree_dir(repo: str) -> None:
    """Keep in-repo stage worktrees out of the main checkout's git status."""
    probe = f"{WORKTREE_SUBDIR}/probe"
    if _git(repo, "check-ignore", "-q", probe).returncode == 0:
        return
    common = Path(_git_ok(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as fh:
        fh.write(f"\n/{WORKTREE_SUBDIR}/\n")


def _worktree_for_branch(repo: str, branch: str) -> str | None:
    current = None
    for line in _git_ok(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return current
    return None


def make_worktree(repo: str, task_branch: str, sid: str, path: str | None) -> dict:
    branch = stage_branch_name(task_branch, sid)
    tip = _git_ok(repo, "rev-parse", "--verify", f"{task_branch}^{{commit}}")
    exists = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0
    if exists:
        base = _git_ok(repo, "merge-base", task_branch, branch)
        found = _worktree_for_branch(repo, branch)
        if found:
            return {"status": "exists", "stage": sid, "branch": branch,
                    "path": found, "base": base}
    else:
        base = tip
    target = Path(path) if path else default_worktree_path(repo, task_branch, sid)
    if not path:
        _exclude_worktree_dir(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        _git_ok(repo, "worktree", "add", str(target), branch)
    else:
        _git_ok(repo, "worktree", "add", "-b", branch, str(target), base)
    return {"status": "exists" if exists else "created", "stage": sid,
            "branch": branch, "path": str(target.resolve()), "base": base}


def _tracked_dirty(repo: str) -> list[str]:
    # Workflow artifacts (ledger rows, ✅ marks) change between merges by
    # design; they never collide with stage branches, so they do not block.
    out = _git_ok(repo, "status", "--porcelain", "--untracked-files=no",
                  "--", ".", ":(exclude).workflow_artifacts")
    return [ln[3:] for ln in out.splitlines() if ln.strip()]


def merge_stage(repo: str, into: str, branch: str) -> tuple[int, dict]:
    current = _git_ok(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if current != into:
        return EXIT_REFUSED, {"status": "refused", "reason": "wrong-branch",
                              "expected": into, "current": current}
    dirty = _tracked_dirty(repo)
    if dirty:
        return EXIT_REFUSED, {"status": "refused", "reason": "dirty-tree",
                              "files": dirty}
    _git_ok(repo, "rev-parse", "--verify", f"{branch}^{{commit}}")
    pre = _git_ok(repo, "rev-parse", "HEAD")

    try:
        res = _git(repo, "merge", "--no-ff", "--no-edit", branch,
                   timeout=_merge_timeout())
        ok, detail = res.returncode == 0, (res.stderr.strip() or res.stdout.strip())
    except subprocess.TimeoutExpired:
        ok, detail = False, f"git merge timed out after {_merge_timeout()}s"
    if ok:
        return EXIT_OK, {"status": "merged", "branch": branch, "into": into,
                         "pre_merge": pre, "head": _git_ok(repo, "rev-parse", "HEAD")}

    conflicts = [f for f in _git(repo, "diff", "--name-only", "--diff-filter=U")
                 .stdout.splitlines() if f]
    if _git(repo, "merge", "--abort").returncode != 0:
        _git(repo, "reset", "--merge")
    head = _git_ok(repo, "rev-parse", "HEAD")
    restored = head == pre and not _tracked_dirty(repo)
    return EXIT_CONFLICT, {
        "status": "conflict", "branch": branch, "into": into,
        # merge-failed: git refused before producing conflicts (for example an
        # untracked file in the way, or a timeout); detail says why.
        "reason": "conflict" if conflicts else "merge-failed",
        "conflicts": conflicts, "pre_merge": pre, "head": head,
        "restored": restored, "detail": detail[-500:],
    }


def cleanup_stage(repo: str, into: str, branch: str, discard: bool) -> tuple[int, dict]:
    """Remove the stage worktree and branch.

    Without ``discard`` the branch must already be merged into ``into``. The
    worktree is removed with --force because test output and other untracked
    files are expected there once the work is merged (or deliberately dropped).
    """
    merged = _git(repo, "merge-base", "--is-ancestor", branch, into).returncode == 0
    if not merged and not discard:
        return EXIT_REFUSED, {"status": "refused", "reason": "unmerged",
                              "branch": branch, "into": into}
    path = _worktree_for_branch(repo, branch)
    removed = path is None or _git(repo, "worktree", "remove", "--force", path).returncode == 0
    _git(repo, "worktree", "prune")
    deleted = _git(repo, "branch", "-D" if discard else "-d", branch).returncode == 0
    ok = removed and deleted
    return (EXIT_OK if ok else EXIT_INVALID), {
        "status": "cleaned" if ok else "partial", "path": path,
        "worktree_removed": removed, "branch_deleted": deleted}


# ── per-stage cost breakdown ─────────────────────────────────────────────────

def _load_cost_event():
    path = Path(__file__).resolve().parent / "cost_event.py"
    key = "_quoin_parallel_stages_cost_event"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def stage_costs(ledger: Path) -> dict:
    """Sum resolved ``usd`` per ``stage=`` key; rows without one go to "untagged".

    A UUID repeated within one stage is counted once. Unresolvable and legacy
    (no col 8) rows are counted, never folded into $0.
    """
    ce = _load_cost_event()
    out: dict[str, dict] = {}
    seen: set[tuple[str, str]] = set()
    for ev in ce.iter_events(ledger):
        key = ce.parse_attribution(ev.attribution).get("stage") or "untagged"
        bucket = out.setdefault(key, {"usd": 0.0, "rows": 0, "unresolved": 0})
        bucket["rows"] += 1
        kind, usd = ce.classify_attribution(ev.attribution)
        if kind == "resolved":
            if (key, ev.uuid) not in seen:
                bucket["usd"] = round(bucket["usd"] + usd, 6)
                seen.add((key, ev.uuid))
        else:
            bucket["unresolved"] += 1
    return dict(sorted(out.items(), key=lambda kv: (kv[0] == "untagged", kv[0])))


# ── CLI ──────────────────────────────────────────────────────────────────────

def _emit(code: int, payload: dict) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="group pending stages into waves")
    p.add_argument("--task-dir", required=True, help="dir holding architecture.md")
    p.add_argument("--max", type=int, default=None)
    p.add_argument("--spent-usd", type=float, default=None)
    p.add_argument("--budget-usd", type=float, default=None,
                   help="default: QUOIN_PARALLEL_BUDGET_USD when set")

    w = sub.add_parser("worktree", help="create a stage branch + worktree")
    w.add_argument("--repo", required=True)
    w.add_argument("--task-branch", required=True)
    w.add_argument("--stage", required=True, help="S-01 or 1")
    w.add_argument("--path", default=None,
                   help="default: <repo>/.claude/worktrees/<stage branch>")

    m = sub.add_parser("merge", help="merge a stage branch into the task branch")
    m.add_argument("--repo", required=True)
    m.add_argument("--into", required=True)
    m.add_argument("--branch", required=True)

    c = sub.add_parser("cleanup", help="remove a stage worktree and branch")
    c.add_argument("--repo", required=True)
    c.add_argument("--into", required=True, help="task branch")
    c.add_argument("--branch", required=True)
    c.add_argument("--discard", action="store_true",
                   help="drop an unmerged stage branch (re-run sequentially)")

    k = sub.add_parser("costs", help="per-stage cost breakdown of a ledger")
    k.add_argument("--ledger", required=True)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            budget = args.budget_usd
            if budget is None and os.environ.get("QUOIN_PARALLEL_BUDGET_USD", "").strip():
                try:
                    budget = float(os.environ["QUOIN_PARALLEL_BUDGET_USD"])
                except ValueError:
                    budget = None
            arch = Path(args.task_dir) / "architecture.md"
            if not arch.is_file():
                raise StageError(f"{arch} not found")
            stages = parse_stages(arch.read_text(encoding="utf-8"))
            cap, reason = resolve_cap(args.max, args.spent_usd, budget)
            waves = plan_waves(stages, cap)
            return _emit(EXIT_OK, {
                "cap": cap, "cap_reason": reason, "waves": waves,
                "parallel": any(len(w) > 1 for w in waves),
                "stages": [{k: s[k] for k in ("id", "name", "done", "depends_on")}
                           for s in stages],
            })
        if args.cmd == "worktree":
            ref = STAGE_ARG_RE.fullmatch(args.stage.strip())
            if not ref:
                raise StageError(f"bad stage reference {args.stage!r}")
            return _emit(EXIT_OK, make_worktree(args.repo, args.task_branch,
                                                stage_id(int(ref.group(1))), args.path))
        if args.cmd == "merge":
            return _emit(*merge_stage(args.repo, args.into, args.branch))
        if args.cmd == "cleanup":
            return _emit(*cleanup_stage(args.repo, args.into, args.branch, args.discard))
        if args.cmd == "costs":
            return _emit(EXIT_OK, {"stages": stage_costs(Path(args.ledger))})
    except (StageError, OSError, subprocess.SubprocessError) as exc:
        return _emit(EXIT_INVALID, {"status": "error", "error": str(exc)})
    return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
