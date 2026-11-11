#!/usr/bin/env python3
"""run_isolation.py — keep concurrent /run sessions out of each other's checkout.

Each checkout has a small registry of the /run sessions working in it. A run
claims the checkout at setup. The first live run works in the checkout itself,
exactly as before; a run that finds another live run already there (or was
asked to isolate) gets its own git worktree on its task branch, created
through ``workspace.py`` outside the project folder. The registry is guarded
by an exclusive lock, so two runs that start at the same moment can never
both take the checkout.

Subcommands (each prints one JSON object on stdout):

  claim    Register this run and say where it works. Prints ``mode``
           (``main`` = the checkout itself, ``worktree`` = an isolated
           worktree), ``worktree`` (the absolute path to work in), ``branch``,
           ``reason`` (``free``, ``isolate``, ``conflict`` or ``resume``) and
           ``blocking`` (the task holding the checkout, on a conflict).
           Claiming a task that already has a worktree reuses it, so
           ``/run --resume`` lands back in the same tree.
  release  Remove this task's registry entry. The worktree and branch stay;
           ``workspace.py teardown`` removes them with its safety checks.
  status   List the checkout's registry entries with their liveness.

Liveness: an entry records the pid of the Claude Code process that owns the
run. A pid that no longer exists (or now belongs to a different process)
marks the entry dead at once. Without a usable pid the entry falls back to
``workspace._owner_is_live`` (session transcript mtime, then ``last_seen``).
Dead entries and entries left by the same session (one session runs one
/run at a time) never block a claim; they stay until their task releases or
resumes, so a resume still knows where the task started.

Env:
  QUOIN_RUN_REGISTRY   registry folder (default ``~/.quoin/runs``).
  QUOIN_WORKTREE_ROOT  worktree parent (default ``~/.quoin/worktrees``); the
                       worktree lands at ``<root>/<repo>/<task>/<repo>``.
  CLAUDE_CODE_SESSION_ID  default session id.

Exit codes: 0 ok, 2 invalid input (argparse, not a git repo, lock timeout),
3 not claimed: the worktree could not be created, or a task that started in
the checkout is resumed while another live run holds it
(``reason: resume-conflict``). Nothing is registered on exit 3.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import workspace  # noqa: E402  (core/scripts sibling)

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

EXIT_OK, EXIT_INVALID, EXIT_NO_ISOLATION = 0, 2, 3
LOCK_TIMEOUT_S = 120.0


# ── locations ───────────────────────────────────────────────────────────────

def registry_root() -> Path:
    raw = os.environ.get("QUOIN_RUN_REGISTRY", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".quoin" / "runs"


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, timeout=workspace._subprocess_timeout())
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def repo_toplevel(path: Path) -> Path | None:
    """The repo's main working tree, also when ``path`` is a linked worktree.

    The registry is keyed on the main checkout, so a release or resume that
    passes a task worktree still finds the run's entry.
    """
    rc, out, _ = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc == 0 and out:
        common = Path(out).resolve()
        if common.name == ".git":
            return common.parent
    rc, out, _ = _git(path, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if rc == 0 and out else None


def checkout_key(repo: Path) -> str:
    digest = hashlib.sha1(str(repo).encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name) or "repo"
    return f"{name}-{digest}"


def registry_dir(repo: Path) -> Path:
    return registry_root() / checkout_key(repo)


def worktree_parent(repo: Path) -> Path:
    return workspace.worktree_root() / repo.name


# ── lock ────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Hold the registry's exclusive lock (flock; O_EXCL lockfile without fcntl)."""
    directory.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"registry lock busy: {directory / '.lock'}")
                    time.sleep(0.05)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return
    lock = directory / ".lock.excl"  # pragma: no cover - Windows path
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_TIMEOUT_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"registry lock busy: {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


# ── entries ─────────────────────────────────────────────────────────────────

def _entry_path(directory: Path, slug: str) -> Path:
    return directory / f"{slug}.json"


def load_entries(directory: Path) -> dict[str, dict]:
    """Registry entries by slug. Corrupt or non-object files are skipped."""
    entries: dict[str, dict] = {}
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return entries
    for path in files:
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                data["_mtime"] = path.stat().st_mtime
                entries[path.stem] = data
        except (OSError, ValueError):
            continue
    return entries


def _write_entry(directory: Path, slug: str, entry: dict) -> None:
    clean = {k: v for k, v in entry.items() if not k.startswith("_")}
    tmp = directory / f".{slug}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(clean, indent=2) + "\n")
    os.replace(tmp, _entry_path(directory, slug))


def _ps(pid: int, field: str) -> str | None:
    try:
        proc = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def owner_pid() -> int | None:
    """Pid of the Claude Code process this script runs under, if found.

    ``QUOIN_RUN_OWNER_PID`` overrides the lookup (tests). Otherwise walk up
    the parent chain to the nearest process whose command name starts with
    ``claude``. None when there is no such ancestor.
    """
    raw = os.environ.get("QUOIN_RUN_OWNER_PID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return None
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        comm = _ps(pid, "comm")
        if comm and os.path.basename(comm).lower().startswith("claude"):
            return pid
        ppid = _ps(pid, "ppid")
        try:
            pid = int(ppid) if ppid else 0
        except ValueError:
            return None
    return None


def is_live(entry: dict) -> bool:
    """True while the run that wrote ``entry`` may still be working."""
    try:
        pid = entry.get("owner_pid")
        if isinstance(pid, int) and pid > 1 and entry.get("hostname") == socket.gethostname():
            if not _pid_exists(pid):
                return False
            started = entry.get("owner_pid_start")
            if started and _ps(pid, "lstart") not in (None, started):
                return False  # pid reused by another process
            return True
        return workspace._owner_is_live({
            "owner_session_uuid": entry.get("session_id"),
            "last_seen": entry.get("last_seen"),
            "_record_mtime": entry.get("_mtime"),
        })
    except Exception:  # noqa: BLE001 - an unreadable entry never blocks
        return False


def _session_id(arg: str | None) -> str:
    return arg or os.environ.get("CLAUDE_CODE_SESSION_ID") or f"unknown-{uuid.uuid4()}"


def _identity(session_id: str) -> dict[str, Any]:
    pid = owner_pid()
    return {
        "session_id": session_id,
        "hostname": socket.gethostname(),
        "owner_pid": pid,
        "owner_pid_start": _ps(pid, "lstart") if pid else None,
        "last_seen": workspace._utc_now_iso(),
    }


# ── claim ───────────────────────────────────────────────────────────────────

def _default_base(repo: Path) -> str:
    """Up-to-date default branch when a remote exists, else the checkout's HEAD."""
    rc, remote, _ = _git(repo, "remote")
    if rc == 0 and "origin" in remote.split():
        default = workspace.default_branch(repo)
        if _git(repo, "fetch", "-q", "origin", default)[0] == 0:
            return f"origin/{default}"
        return default
    rc, head, _ = _git(repo, "rev-parse", "HEAD")
    return head if rc == 0 and head else "HEAD"


def _existing_worktree(slug: str, entry: dict | None, record: dict | None,
                       repo: Path, project_root: Path) -> Path | None:
    """A worktree this task already has: its registry entry first, then its record.

    Both sources are untrusted, so a candidate must sit at the exact place
    this task's worktree would be created and be a linked worktree of ``repo``.
    """
    expected = (worktree_parent(repo) / slug / repo.name)
    candidates = []
    if entry and entry.get("mode") == "worktree":
        candidates.append(Path(str(entry.get("worktree") or "")))
    if isinstance(record, dict):
        ws_root = workspace.resolve_workspace_root(project_root, slug, record=record)
        candidates.append(ws_root / repo.name)
    for cand in candidates:
        try:
            if (cand.resolve() == expected.resolve() and (cand / ".git").is_file()
                    and repo_toplevel(cand) == repo):
                return cand
        except OSError:
            continue
    return None


def _worktree_branch(path: Path) -> str | None:
    rc, out, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 and out and out != "HEAD" else None


def claim(task: str, repo: Path, project_root: Path, branch: str | None = None,
          isolate: bool = False, session_id: str | None = None,
          base: str | None = None) -> tuple[int, dict]:
    slug = workspace.slugify(task)
    sid = _session_id(session_id)
    directory = registry_dir(repo)
    with _locked(directory):
        entries = load_entries(directory)
        mine = entries.get(slug)
        record = workspace.read_ownership_record(project_root, slug)
        out: dict[str, Any] = {"task": task, "repo": str(repo),
                               "registry": str(_entry_path(directory, slug)),
                               "blocking": None, "created": False}

        existing = _existing_worktree(slug, mine, record, repo, project_root)
        if existing is not None:
            entry = {**(mine or {}), "task": task, "repo": str(repo), "mode": "worktree",
                     "worktree": str(existing), "workspace_path": str(existing.parent),
                     "branch": _worktree_branch(existing) or (mine or {}).get("branch") or branch,
                     **_identity(sid)}
            entry.setdefault("created", workspace._utc_now_iso())
            _write_entry(directory, slug, entry)
            out.update(mode="worktree", reason="resume", worktree=str(existing),
                       branch=entry["branch"], workspace_path=str(existing.parent))
            return EXIT_OK, out

        holders = []
        for other_slug, other in entries.items():
            if other_slug == slug or other.get("mode") != "main":
                continue
            if other.get("session_id") == sid or not is_live(other):
                # Dead or superseded: it no longer blocks, but its entry stays
                # so a resume of that task still knows where it started.
                continue
            holders.append(other)

        if not isolate and not holders:
            entry = {"task": task, "repo": str(repo), "mode": "main", "worktree": str(repo),
                     "branch": branch or (mine or {}).get("branch"),
                     "created": (mine or {}).get("created") or workspace._utc_now_iso(),
                     **_identity(sid)}
            _write_entry(directory, slug, entry)
            out.update(mode="main", reason="resume" if mine else "free",
                       worktree=str(repo), branch=entry["branch"], workspace_path=None)
            return EXIT_OK, out

        if mine and mine.get("mode") == "main" and holders:
            # This task started in the checkout; isolating now would leave its
            # uncommitted work behind there. Let the caller decide.
            out.update(mode=None, reason="resume-conflict", branch=mine.get("branch"),
                       blocking=holders[0].get("task"),
                       error=f"task started in this checkout, now held by "
                             f"{holders[0].get('task')}; resume after that run ends")
            return EXIT_NO_ISOLATION, out

        task_branch = branch or (mine or {}).get("branch") or slug
        result = workspace.create_workspace(
            feature=task, project_root=project_root, repo_paths=[repo],
            base=base or _default_base(repo), session_uuid=sid,
            workspaces_root=worktree_parent(repo), branch=task_branch,
        )
        per_repo = result.per_repo[0] if result.per_repo else None
        if result.refused or per_repo is None or not (per_repo.created or per_repo.skipped):
            detail = result.message or (per_repo.error if per_repo else "") or "worktree not created"
            out.update(mode=None, reason="error", error=detail, branch=task_branch,
                       blocking=holders[0].get("task") if holders else None)
            return EXIT_NO_ISOLATION, out

        entry = {"task": task, "repo": str(repo), "mode": "worktree",
                 "worktree": per_repo.dest, "workspace_path": result.workspace_path,
                 "branch": task_branch, "created": workspace._utc_now_iso(),
                 **_identity(sid)}
        _write_entry(directory, slug, entry)
        out.update(mode="worktree", reason="isolate" if isolate and not holders else "conflict",
                   worktree=per_repo.dest, branch=task_branch,
                   workspace_path=result.workspace_path, created=per_repo.created,
                   blocking=holders[0].get("task") if holders else None)
        return EXIT_OK, out


def release(task: str, repo: Path) -> dict:
    slug = workspace.slugify(task)
    directory = registry_dir(repo)
    with _locked(directory):
        entry = load_entries(directory).get(slug)
        removed = False
        if entry is not None:
            with contextlib.suppress(OSError):
                _entry_path(directory, slug).unlink()
                removed = True
    entry = entry or {}
    return {"task": task, "released": removed, "mode": entry.get("mode"),
            "worktree": entry.get("worktree"), "branch": entry.get("branch")}


def status(repo: Path) -> dict:
    directory = registry_dir(repo)
    runs = []
    for slug, entry in load_entries(directory).items():
        view = {k: v for k, v in entry.items() if not k.startswith("_")}
        view["slug"] = slug
        view["live"] = is_live(entry)
        runs.append(view)
    return {"repo": str(repo), "registry": str(directory), "runs": runs}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _emit(code: int, payload: dict) -> int:
    print(json.dumps(payload, indent=2))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("claim", "release", "status"):
        p = sub.add_parser(name)
        if name != "status":
            p.add_argument("--task", required=True)
        p.add_argument("--repo", default=".", help="the checkout (default: cwd)")
        if name == "claim":
            p.add_argument("--project-root", default=None,
                           help="folder holding .workflow_artifacts (default: cwd)")
            p.add_argument("--branch", default=None)
            p.add_argument("--base", default=None)
            p.add_argument("--isolate", action="store_true")
            p.add_argument("--session-id", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_INVALID

    repo = repo_toplevel(Path(args.repo).expanduser())
    if repo is None:
        return _emit(EXIT_INVALID, {"error": f"not a git repository: {args.repo}"})
    try:
        if args.command == "claim":
            root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()
            code, payload = claim(args.task, repo, root.resolve(), branch=args.branch,
                                  isolate=args.isolate, session_id=args.session_id,
                                  base=args.base)
            return _emit(code, payload)
        if args.command == "release":
            return _emit(EXIT_OK, release(args.task, repo))
        return _emit(EXIT_OK, status(repo))
    except (OSError, TimeoutError, ValueError) as exc:
        return _emit(EXIT_INVALID, {"error": str(exc)})


if __name__ == "__main__":
    sys.exit(main())
