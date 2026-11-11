#!/usr/bin/env python3
"""Portable core implementation of feature-workspace create/teardown/takeover/heartbeat/status (IVG-158).

Gives each in-flight feature its own isolated per-repo git worktree so
concurrent Claude Code sessions never share a mutable branch on the same
repo. This module owns ``create`` (S-01), ``teardown`` (S-03), the read-only
``status``/``list`` subcommand (S-05), and — from S-04 — real owner-liveness
(owner-keyed JSONL transcript-mtime PRIMARY signal + last_seen/record-mtime
fallback), a NON-DESTRUCTIVE ``takeover`` (record-only flip), and an owner-only
``heartbeat``. See architecture.md D-01..D-10.

Public API:
  discover_workspace_repos(project_root, named=None) -> list[Path]
  default_branch(repo) -> str
  add_worktree(repo, feature, dest, base=None) -> WorktreeResult
  read_ownership_record(project_root, slug) -> dict | None
  write_ownership_record(project_root, slug, record) -> bool
  build_record(feature, slug, session_uuid, repos, branches, workspace_path) -> dict
  write_marker(workspace_root, artifact_root, feature, repos) -> bool
  create_workspace(feature, project_root, named_repos=None, base=None,
                    session_uuid=None) -> CreateResult
  _owner_jsonl_mtime(record, home=None) -> float | None
  _owner_is_live(record, home=None) -> bool
  discover_workspace_worktrees(ws_root) -> list[Path]
  teardown_workspace(feature, project_root, force=False) -> TeardownResult
  enumerate_workspaces(project_root) -> list[str]
  collect_status(project_root) -> list[WorkspaceStatusView]
  TakeoverResult
  takeover_workspace(feature, project_root, session_uuid=None, force=False,
                     home=None) -> TakeoverResult
  _find_workspace_marker(cwd) -> Path | None
  heartbeat_workspace(cwd, session_uuid=None, home=None) -> bool
  main(argv: list[str] | None = None) -> int

Exit codes (CLI ``create`` subcommand):
  0 — at least one worktree created-or-skipped and record written
  2 — argparse error
  3 — refused (live non-self owner, authoritative or ambiguous) OR
      zero worktrees succeeded (fail-OPEN, non-destructive)

Exit codes (CLI ``takeover`` subcommand):
  0 — took over (or self re-claim)
  2 — argparse error
  3 — refused (live non-self owner, no --force)
  4 — no record to take over (missing / corrupt / non-dict)

Exit codes (CLI ``heartbeat`` subcommand):
  0 — always (fail-OPEN); prints {"refreshed": <bool>}

Exit codes (CLI ``teardown`` subcommand):
  0 — torn down, OR graceful nothing-to-tear-down
  2 — argparse error
  3 — refused-unsafe (uncommitted/unpushed guard tripped, ``--force`` not passed)
  4 — partial teardown failure (>=1 ``git worktree remove`` failed; resumable)

Exit codes (CLI ``status``/``list`` subcommand):
  0 — always (read-only informational; never fails on workspace state)
  2 — argparse error

Env:
  QUOIN_SUBPROCESS_TIMEOUT — seconds, default 30; bounds every git subprocess.
  QUOIN_WORKSPACE_STALE_HOURS — hours, default 6; owner-liveness staleness
      threshold. S-04 now implements full liveness: the owner-keyed JSONL
      transcript-mtime is the PRIMARY signal, with last_seen/record-mtime as
      the fallback (freshest-of-all-signals; architecture.md D-04).
  QUOIN_WORKSPACE_HEARTBEAT — sessionstart.sh opt-in gate (=1 enables the
      owner-only last_seen heartbeat shell-out; default off, Q-06).
  QUOIN_WORKTREE_ROOT — parent folder for workspaces created outside the
      project (``--workspaces-root``); default ``~/.quoin/worktrees``. Teardown
      and status follow a record's ``workspace_path`` only when it lies under
      this root, so an edited record can never point a removal elsewhere.
  QUOIN_WORKSPACE_DISABLE_GH — when truthy, ``_gh_pr_merged`` always returns
      None (gh-free path only) — an escape hatch (risk R-08) and the
      deterministic switch for the gh-free tests.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Sibling-import bootstrap (robust under direct-run AND wrapper importlib load)
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import branch_hygiene  # noqa: E402  (core/scripts sibling — allowed)
import get_session_uuid  # noqa: E402  (core/scripts sibling — allowed)


# ---------------------------------------------------------------------------
# Copy-not-import local helpers (own copy — do NOT cross-import branch_hygiene's)
# ---------------------------------------------------------------------------

def _subprocess_timeout() -> int:
    """Read QUOIN_SUBPROCESS_TIMEOUT (seconds); default 30; bad values fall back to 30.

    Self-contained local copy — do NOT cross-import; each touched core script
    owns its own copy per the repo's copy-not-import convention.
    """
    try:
        return int(os.environ.get("QUOIN_SUBPROCESS_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30


def _run(args: list[str], cwd: str | Path | None = None) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode).

    `cwd` is OPTIONAL and defaults to None (current behavior, byte-identical —
    every pre-S-05 caller passes no cwd and is unaffected). S-05 uses it to run
    `gh` inside a worktree/orig-repo dir (D-06).
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_subprocess_timeout(),
            cwd=cwd,
        )
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except FileNotFoundError:
        return "", "git not found", 1
    except Exception as exc:  # noqa: BLE001
        return "", str(exc), 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_epoch(s: str | None) -> float | None:
    """Parse an _utc_now_iso() string back to epoch seconds (aware UTC).

    Bad/empty input -> None. Single-basis helper so _owner_is_live never
    mixes datetime objects with epoch floats (T-01 acceptance).
    """
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def slugify(feature: str) -> str:
    """Lowercase, non-[a-z0-9-] -> '-', collapse repeats, strip leading/trailing '-'.

    Raises ValueError if the result is empty.
    """
    lowered = feature.lower()
    replaced = re.sub(r"[^a-z0-9-]", "-", lowered)
    collapsed = re.sub(r"-+", "-", replaced).strip("-")
    if not collapsed:
        raise ValueError(f"slugify({feature!r}) produced an empty slug")
    return collapsed


def worktree_root() -> Path:
    """Parent folder for out-of-project workspaces (``QUOIN_WORKTREE_ROOT``)."""
    raw = os.environ.get("QUOIN_WORKTREE_ROOT", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".quoin" / "worktrees"


def resolve_workspace_root(
    project_root: Path,
    slug: str,
    workspaces_root: Path | None = None,
    record: dict | None = None,
) -> Path:
    """Folder that holds a workspace's per-repo worktrees.

    An explicit ``workspaces_root`` wins. Otherwise the in-project default
    ``<project>/.workspaces/<slug>`` is used, unless it is absent and the
    ownership record names exactly ``<worktree_root()>/<repo>/<slug>`` (a
    workspace that ``/run`` created outside the project). The record is
    untrusted, so any other recorded path, including a shallower folder that
    holds other tasks' workspaces, is ignored.
    """
    if workspaces_root is not None:
        return Path(workspaces_root).expanduser() / slug
    default = project_root / ".workspaces" / slug
    if default.exists() or not isinstance(record, dict):
        return default
    recorded = record.get("workspace_path")
    if isinstance(recorded, str) and recorded:
        cand = Path(recorded)
        try:
            exact = cand.resolve().parent.parent == worktree_root().resolve()
        except OSError:
            exact = False
        if exact and cand.name == slug and cand.is_dir() and not cand.is_symlink():
            return cand
    return default


def _atomic_write_json(path: Path, obj: Any) -> bool:
    """Write obj as JSON to path atomically (mkdir parents, .tmp sidecar, os.replace).

    Fail-OPEN: on OSError, log to stderr and return False; never raise.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(obj, indent=2) + "\n")
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        print(f"[workspace] atomic write failed for {path}: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# T-02: Repo discovery + selection (D-07)
# ---------------------------------------------------------------------------

def discover_workspace_repos(project_root: Path, named: list[str] | None = None) -> list[Path]:
    """Discover candidate repos via branch_hygiene.discover_repos, filtering .workspaces.

    Does NOT re-implement discovery and does NOT mutate branch_hygiene's shared
    _EXCLUDE_NAMES frozenset (D-07). If `named` is given, keep only repos whose
    .name is in `named`; unknown names raise ValueError naming them (never
    silently dropped). Zero `named` -> all discovered.
    """
    candidates = branch_hygiene.discover_repos(project_root)
    workspaces_root = str((project_root / ".workspaces").resolve())
    filtered = [
        p for p in candidates
        if not (str(p) == workspaces_root or str(p).startswith(workspaces_root + os.sep))
    ]

    if not named:
        return filtered

    by_name = {p.name: p for p in filtered}
    unknown = [n for n in named if n not in by_name]
    if unknown:
        raise ValueError(f"unknown repo name(s) requested: {', '.join(unknown)}")
    return [by_name[n] for n in named]


# ---------------------------------------------------------------------------
# T-03: Default-branch resolution + worktree add (D-08, D-09, arch R-05)
# ---------------------------------------------------------------------------

def default_branch(repo: Path) -> str:
    """Resolve the repo's default branch: origin/HEAD -> current branch -> 'main'."""
    stdout, _, rc = _run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    )
    if rc == 0 and stdout:
        return stdout.split("/", 1)[-1] if stdout.startswith("origin/") else stdout

    stdout, _, rc = _run(["git", "-C", str(repo), "branch", "--show-current"])
    if rc == 0 and stdout:
        return stdout

    return "main"


def _worktree_list_porcelain(repo: Path) -> str:
    stdout, _, rc = _run(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    return stdout if rc == 0 else ""


def _worktree_registered(repo: Path, dest: Path) -> bool:
    """True if dest (resolved) is a listed worktree path for repo (idempotent skip-add)."""
    dest_resolved = str(dest.resolve()) if dest.exists() else str(dest)
    porcelain = _worktree_list_porcelain(repo)
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            wt_path = line[len("worktree "):].strip()
            try:
                wt_resolved = str(Path(wt_path).resolve())
            except OSError:
                wt_resolved = wt_path
            if wt_resolved == dest_resolved or wt_path == str(dest):
                return True
    return False


def _branch_checked_out_elsewhere(repo: Path, branch: str) -> str | None:
    """Return the worktree path where `branch` is already checked out, else None."""
    porcelain = _worktree_list_porcelain(repo)
    current_path: str | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and current_path is not None:
            # Format: "branch refs/heads/<name>"
            ref = line[len("branch "):].strip()
            branch_name = ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ref
            if branch_name == branch:
                return current_path
    return None


@dataclasses.dataclass
class WorktreeResult:
    repo: str
    dest: str
    branch: str
    created: bool
    skipped: bool
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def add_worktree(repo: Path, feature: str, dest: Path, base: str | None = None) -> WorktreeResult:
    """Create (or reuse) a linked worktree at dest on the feature branch."""
    repo_str = str(repo)
    dest_str = str(dest)

    if _worktree_registered(repo, dest):
        return WorktreeResult(repo=repo_str, dest=dest_str, branch=feature, created=False, skipped=True, error=None)

    # dest is confirmed NOT already registered (checked above), so any non-None
    # result here is necessarily a DIFFERENT path (arch R-05).
    elsewhere = _branch_checked_out_elsewhere(repo, feature)
    if elsewhere is not None:
        return WorktreeResult(
            repo=repo_str, dest=dest_str, branch=feature, created=False, skipped=False,
            error=f"branch '{feature}' already checked out at {elsewhere}",
        )

    dest.parent.mkdir(parents=True, exist_ok=True)

    base_ref = base or default_branch(repo)
    args = ["git", "-C", repo_str, "worktree", "add", "-b", feature, dest_str, base_ref]
    _, stderr, rc = _run(args)
    if rc != 0:
        # branch may already exist locally — fall back to bare form
        args_fallback = ["git", "-C", repo_str, "worktree", "add", dest_str, feature]
        _, stderr2, rc2 = _run(args_fallback)
        if rc2 != 0:
            return WorktreeResult(
                repo=repo_str, dest=dest_str, branch=feature, created=False, skipped=False,
                error=stderr2 or stderr or f"git worktree add failed (rc={rc2})",
            )

    return WorktreeResult(repo=repo_str, dest=dest_str, branch=feature, created=True, skipped=False, error=None)


# ---------------------------------------------------------------------------
# T-04: Ownership record + marker (D-02, D-03) with minimal liveness stub
# ---------------------------------------------------------------------------

def _record_path(project_root: Path, slug: str) -> Path:
    return project_root / ".workflow_artifacts" / "memory" / "workspaces" / f"{slug}.json"


def read_ownership_record(project_root: Path, slug: str) -> dict | None:
    """Read the ownership record for slug. Missing/corrupt -> None (fail-OPEN).

    The record file is UNTRUSTED input (another session, a partial/atomic-rename
    write, a manual edit). A valid-JSON-but-non-object body (``[]``, ``42``,
    ``"x"``, ``true``, ``null``) is rejected via ``isinstance(record, dict)``
    (2026-07-31 untrusted-JSON lesson) so downstream ``record.get(...)`` consumers
    never see a non-dict (C1 in the T-09 untrusted-record census).
    """
    path = _record_path(project_root, slug)
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def build_record(
    feature: str,
    slug: str,
    session_uuid: str,
    repos: list[str],
    branches: dict[str, str],
    workspace_path: Path,
) -> dict:
    """Build the D-03 ownership record dict."""
    now = _utc_now_iso()
    pid_start_time: str | None = None
    try:
        # Diagnostic-only best-effort (D-04 no-PID-veto) — never gates anything.
        out, _, rc = _run(["ps", "-o", "lstart=", "-p", str(os.getpid())])
        if rc == 0 and out:
            pid_start_time = out
    except Exception:  # noqa: BLE001
        pid_start_time = None

    return {
        "feature": feature,
        "owner_session_uuid": session_uuid,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "pid_start_time": pid_start_time,
        "created": now,
        "last_seen": now,
        "repos": list(repos),
        "branches": dict(branches),
        "workspace_path": str(workspace_path),
    }


def write_ownership_record(project_root: Path, slug: str, record: dict) -> bool:
    """Write the ownership record via _atomic_write_json (whole-file, no read-modify-write)."""
    return _atomic_write_json(_record_path(project_root, slug), record)


def write_marker(workspace_root: Path, artifact_root: Path, feature: str, repos: list[str]) -> bool:
    """Write the .quoin-workspace.json marker. Returns the atomic-write bool (MIN-3)."""
    marker = {
        "artifact_root": str(artifact_root.resolve()),
        "feature": feature,
        "repos": list(repos),
        "created": _utc_now_iso(),
    }
    marker_path = workspace_root / ".quoin-workspace.json"
    return _atomic_write_json(marker_path, marker)


def _owner_jsonl_mtime(record: dict, home: str | None = None) -> float | None:
    """PRIMARY liveness signal (D-04): mtime of the owner's Claude Code transcript.

    Locates the owning session's JSONL by an OWNER-KEYED, launch-cwd-INDEPENDENT
    scan of ``<home>/.claude/projects/*/<owner>.jsonl`` (round-2 MAJ-2). The
    transcript is touched every turn even when no heartbeat write happens, so its
    mtime is the most reliable freshness signal. Because the scan keys on the
    unique ``owner_session_uuid`` rather than a ``project_hash`` of any launch dir,
    it finds the transcript no matter which cwd the owner was launched from —
    closing the flagship R-02 launch-cwd false-stale hole.

    The ENTIRE body is wrapped in ``try/except Exception: return None`` (fail-OPEN,
    round-2 MAJ-1 / C3 in the T-09 census): the record is untrusted and a
    best-effort liveness probe must NEVER propagate into its unwrapped callers.

    Returns the freshest matching transcript's epoch mtime, or None (no owner,
    synthetic ``unknown-*`` owner, no transcript, or any error).
    """
    try:
        owner = record.get("owner_session_uuid")
        if not isinstance(owner, str) or not owner:
            return None
        # Synthetic/subagent fallback UUID has NO JSONL — do not scan for a file
        # that provably cannot exist (get_session_uuid._fallback_uuid form).
        if owner.startswith("unknown-"):
            return None
        home_path = Path(home) if home else Path.home()
        projects = home_path / ".claude" / "projects"
        mtimes: list[float] = []
        for projdir in projects.iterdir():  # SINGLE one-level readdir (MAJ-2)
            if not projdir.is_dir():
                continue
            cand = projdir / f"{owner}.jsonl"  # EXACT stem — Drive conflict-copy safe
            try:
                if cand.is_file():
                    mtimes.append(cand.stat().st_mtime)
            except OSError:
                continue
        return max(mtimes) if mtimes else None
    except Exception:  # noqa: BLE001  (whole-body fail-OPEN, MAJ-1)
        return None


def _owner_is_live(record: dict, home: str | None = None) -> bool:
    """Owner-liveness check (D-04): live IFF the FRESHEST available signal is fresh.

    Folds the PRIMARY JSONL-transcript-mtime signal (``_owner_jsonl_mtime``,
    owner-keyed, launch-cwd-independent) in front of the existing ``last_seen`` and
    record-mtime FALLBACK signals, then takes ``max`` over all available candidates:
    if ANY signal is within ``QUOIN_WORKSPACE_STALE_HOURS`` (default 6) the workspace
    is LIVE. Taking the freshest is the conservative choice that MINIMIZES false-stale
    (R-02) — wrongly concluding STALE is the data-safety hazard, so we bias toward LIVE.
    Exactly architecture D-04: "STALE when the freshest available signal is older than
    the threshold."

    ROUND-3 CLASS HARDENING (C4 in the T-09 census): the ENTIRE body is wrapped in
    ``try/except Exception: return False``. This function receives an UNTRUSTED record
    and is called from UNWRAPPED sites (``create_workspace`` step b,
    ``takeover_workspace`` step 5); a dict-but-corrupt record (e.g. a non-numeric
    ``_record_mtime``) could otherwise poison the candidate ``max``/arithmetic and raise
    ``TypeError`` uncaught. Wrapping makes false-stale (return False, reclaimable) the
    worst case — never a traceback into a CLI caller.

    All timestamp operands are normalized to epoch-seconds floats — never mixed with
    datetime objects.
    """
    try:
        try:
            threshold_hours = float(os.environ.get("QUOIN_WORKSPACE_STALE_HOURS", "6"))
        except (TypeError, ValueError):
            threshold_hours = 6.0

        jsonl_epoch = _owner_jsonl_mtime(record, home=home)  # PRIMARY (D-04)
        last_seen_epoch = _iso_to_epoch(record.get("last_seen"))  # fallback
        mtime_epoch: float | None = record.get("_record_mtime")  # fallback

        candidates = [x for x in (jsonl_epoch, last_seen_epoch, mtime_epoch) if x is not None]
        if not candidates:
            # No usable timestamp at all -- fail-OPEN as stale (safe: treat as
            # reclaimable rather than permanently un-reclaimable).
            return False

        newest = max(candidates)
        now_epoch = datetime.now(timezone.utc).timestamp()
        return (now_epoch - newest) < (threshold_hours * 3600)
    except Exception:  # noqa: BLE001  (whole-body fail-OPEN, C4 round-3)
        return False


def _read_ownership_record_with_mtime(project_root: Path, slug: str) -> dict | None:
    """Like read_ownership_record but injects _record_mtime for _owner_is_live's fallback.

    UNTRUSTED-input hardening (2026-07-31 lesson, C2 in the T-09 census): a
    valid-JSON list/int/str/bool/null body would make ``record["_record_mtime"] = ...``
    raise an uncaught ``TypeError`` and crash every liveness/takeover caller. The
    ``isinstance(record, dict)`` gate BEFORE the write-subscript rejects those bodies;
    ``TypeError`` is added to the caught tuple as belt-and-suspenders.
    """
    path = _record_path(project_root, slug)
    try:
        record = json.loads(path.read_text())
        if not isinstance(record, dict):
            return None
        record["_record_mtime"] = path.stat().st_mtime
        return record
    except (OSError, json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# T-05: create_workspace orchestrator (D-03 ordering, FR1 idempotency, D-07
# gitignore, collision refusal)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class CreateResult:
    slug: str = ""
    workspace_path: str = ""
    per_repo: list[WorktreeResult] = dataclasses.field(default_factory=list)
    record_written: bool = False
    marker_written: bool = False
    refused: bool = False
    ambiguous: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "workspace_path": self.workspace_path,
            "per_repo": [r.to_dict() for r in self.per_repo],
            "record_written": self.record_written,
            "marker_written": self.marker_written,
            "refused": self.refused,
            "ambiguous": self.ambiguous,
            "message": self.message,
        }


def _ensure_gitignore_wholeline(repo_root: Path, line: str) -> None:
    """Append `line` to repo_root/.gitignore if no stripped-whole-line match exists (MIN-5)."""
    gitignore_path = repo_root / ".gitignore"
    existing_lines: list[str] = []
    if gitignore_path.exists():
        try:
            existing_lines = gitignore_path.read_text().splitlines()
        except OSError:
            existing_lines = []

    if any(existing_line.strip() == line for existing_line in existing_lines):
        return

    try:
        with gitignore_path.open("a") as fh:
            if existing_lines and existing_lines[-1] != "":
                fh.write("\n")
            fh.write(line + "\n")
    except OSError as exc:
        print(f"[workspace] failed to update .gitignore at {gitignore_path}: {exc}", file=sys.stderr)


def create_workspace(
    feature: str,
    project_root: Path,
    named_repos: list[str] | None = None,
    base: str | None = None,
    session_uuid: str | None = None,
    workspaces_root: Path | None = None,
    branch: str | None = None,
    repo_paths: list[Path] | None = None,
) -> CreateResult:
    """Create (or idempotently refresh) a feature workspace across selected repos.

    Sequence is load-bearing (D-03): collision check -> discover -> gitignore
    -> worktrees FIRST -> finalize record only after >=1 worktree succeeds.

    Optional overrides (defaults keep the ``/workspace`` behaviour): put the
    workspace under ``workspaces_root`` instead of ``<project>/.workspaces``
    (no ``.gitignore`` edit then, since nothing lands in the project), check out
    ``branch`` instead of a branch named after the feature, and use
    ``repo_paths`` instead of discovering repos under the project root.
    """
    slug = slugify(feature)
    project_root = project_root.resolve()
    ws_root = resolve_workspace_root(project_root, slug, workspaces_root)
    branch_name = branch or feature

    # Step a: identity resolution — caller-authoritative, derive only if None (MAJ-2).
    identity_authoritative = session_uuid is not None
    resolved_uuid = session_uuid if identity_authoritative else get_session_uuid.get_session_uuid(
        project_path=str(project_root), phase="ad-hoc"
    )

    # Step b: collision / self-vs-other check — UUID EQUALITY ONLY (round-2 MAJ fix).
    record = _read_ownership_record_with_mtime(project_root, slug)
    if record is not None:
        is_self = record.get("owner_session_uuid") == resolved_uuid
        if _owner_is_live(record) and not is_self:
            if identity_authoritative:
                return CreateResult(
                    slug=slug,
                    workspace_path=str(ws_root),
                    refused=True,
                    ambiguous=False,
                    message=f"owned by a live session; run: workspace takeover {feature}",
                )
            else:
                return CreateResult(
                    slug=slug,
                    workspace_path=str(ws_root),
                    refused=True,
                    ambiguous=True,
                    message=(
                        "a live workspace record exists under a different derived "
                        "session id; re-run with --session-uuid to confirm ownership "
                        "(identity derivation is ambiguous under parallel sessions; "
                        "full disambiguation deferred to S-04 liveness)"
                    ),
                )
        # else: no-live-conflict (stale record OR self) -> proceed / adopt-refresh

    # Step c: repo discovery (raises ValueError on unknown named repo).
    if repo_paths:
        repos = [Path(p).resolve() for p in repo_paths]
    else:
        repos = discover_workspace_repos(project_root, named_repos)

    # Step d: cwd-as-repo .gitignore edge (D-07 / AC-8) — BEFORE adding worktrees.
    if workspaces_root is None and (project_root / ".git").exists():
        _ensure_gitignore_wholeline(project_root, ".workspaces/")

    # Step e: WORKTREES FIRST.
    results: list[WorktreeResult] = []
    branches: dict[str, str] = {}
    for repo in repos:
        dest = ws_root / repo.name
        result = add_worktree(repo, branch_name, dest, base)
        results.append(result)
        if result.created or result.skipped:
            branches[repo.name] = branch_name

    # Step f: finalize record ONLY IF at least one worktree succeeded.
    if any(r.created or r.skipped for r in results):
        rec_obj = build_record(
            feature=feature,
            slug=slug,
            session_uuid=resolved_uuid,
            repos=list(branches),
            branches=branches,
            workspace_path=ws_root,
        )
        record_written = write_ownership_record(project_root, slug, rec_obj)
        marker_written = write_marker(ws_root, artifact_root=project_root, feature=feature, repos=list(branches))
        return CreateResult(
            slug=slug,
            workspace_path=str(ws_root),
            per_repo=results,
            record_written=record_written,
            marker_written=marker_written,
        )

    return CreateResult(
        slug=slug,
        workspace_path=str(ws_root),
        per_repo=results,
        record_written=False,
        marker_written=False,
        message="no worktree created; resumable",
    )


# ---------------------------------------------------------------------------
# T-04: takeover_workspace — non-destructive ownership-record flip (D-05, FR4/AC-4)
# ---------------------------------------------------------------------------

def _current_identity_fields(session_uuid: str) -> dict[str, Any]:
    """Current-session identity fields for a record flip (owner/hostname/pid/…/last_seen).

    Reuses build_record's diagnostic best-effort pid_start_time derivation
    (D-04 no-PID-veto: diagnostic-only). last_seen is set to now.
    """
    pid_start_time: str | None = None
    try:
        out, _, rc = _run(["ps", "-o", "lstart=", "-p", str(os.getpid())])
        if rc == 0 and out:
            pid_start_time = out
    except Exception:  # noqa: BLE001
        pid_start_time = None
    return {
        "owner_session_uuid": session_uuid,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "pid_start_time": pid_start_time,
        "last_seen": _utc_now_iso(),
    }


@dataclasses.dataclass
class TakeoverResult:
    slug: str = ""
    workspace_path: str = ""
    record_path: str = ""
    took_over: bool = False
    refused: bool = False
    no_record: bool = False
    live: bool = False
    prior_owner: str = ""
    new_owner: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# S-03 teardown (IVG-158): guarded removal of a feature workspace.
# ---------------------------------------------------------------------------

def _worktree_branch(worktree: Path | str) -> str | None:
    """Return the branch checked out at `worktree`, or None if undetectable.

    Empty output, a detached-HEAD `HEAD` result, or a non-zero return code all
    yield None (fail-OPEN — treated as "cannot prove merged" by the caller).
    """
    stdout, _, rc = _run(["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0 or not stdout or stdout == "HEAD":
        return None
    return stdout


def _gh_pr_merged(branch: str, cwd: str | Path | None) -> bool | None:
    """Query `gh` for whether `branch`'s PR is MERGED. None = inconclusive (fail-OPEN).

    Skips entirely (returns None) when QUOIN_WORKSPACE_DISABLE_GH is truthy —
    the escape hatch (risk R-08) and the deterministic switch for the gh-free
    tests. Any failure mode (gh not installed, unauthenticated, no matching
    PR, malformed JSON, JSON that isn't an object) also yields None so the
    caller falls through to the git-reachability path — never an error.
    """
    if os.environ.get("QUOIN_WORKSPACE_DISABLE_GH"):
        return None
    stdout, _, rc = _run(["gh", "pr", "view", branch, "--json", "state,mergedAt"], cwd=cwd)
    if rc != 0 or not stdout:
        return None
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("state") == "MERGED"


def _git_branch_merged(branch: str, repo: str, default: str) -> bool:
    """True if `branch` is reachable from `default` per `git branch --merged`.

    `git branch` (unlike `git branch --merged` alone) lists every local
    branch regardless of which linked worktree currently has it checked out
    (marked with a leading `*`/`+`), so this sees a feature branch checked out
    in a sibling worktree of `repo`. rc != 0 (e.g. bad `default` ref) yields
    False — conservative, matches the fail-OPEN-to-unsafe posture upstream.
    """
    stdout, _, rc = _run(["git", "-C", str(repo), "branch", "--merged", default])
    if rc != 0:
        return False
    names: set[str] = set()
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("* ") or stripped.startswith("+ "):
            stripped = stripped[2:].strip()
        if stripped:
            names.add(stripped)
    return branch in names


def _proven_merged(worktree: Path | str, orig_repo: str | None) -> bool:
    """Is this worktree's branch provably pushed/merged? (S-05, D-06)

    OR semantics: True if `gh` reports the branch's PR as MERGED, OR the
    branch is reachable from the repo's default branch (`git branch
    --merged`). gh is tried first (authoritative for the delete-on-merge case
    where the remote branch is already gone); the git-reachability path is
    the gh-free fallback and also covers locally-merged-not-yet-synced-to-gh
    state. gh inconclusive (None) at any point falls through to the git path
    — never an error. This is the ONLY call site of the proven-merged
    override, consumed by _classify_worktree_safety (teardown) and
    collect_status (status/list).
    """
    branch = _worktree_branch(worktree)
    if branch is None:
        return False
    repo = orig_repo or str(worktree)
    default = default_branch(Path(repo))
    if _gh_pr_merged(branch, cwd=worktree) is True:
        return True
    return _git_branch_merged(branch, repo, default)


def _classify_worktree_safety(
    rc: "branch_hygiene.RepoResult", proven_merged: bool
) -> tuple[bool, list[str]]:
    """Classify a worktree as safe-to-discard (True) or unsafe (False + reasons).

    Conservative S-03 rule — never silently discards work (arch R-06):
      - rc.error truthy                         -> UNSAFE (cannot prove safe)
      - rc.dirty                                -> UNSAFE (uncommitted changes)
      - rc.upstream is None AND not proven_merged -> UNSAFE (never pushed)
      - rc.upstream is not None AND ahead > 0   -> UNSAFE (unpushed commits)
      - else                                    -> SAFE (clean + upstream + not-ahead)

    Reasons accumulate (dirty + no-upstream can co-occur). safe = not reasons.

    Known limitation (critic MIN-3): rc.dirty is best-effort — a `git status`
    failure inside check_repo yields dirty=False WITHOUT setting rc.error, so a
    status-check failure is invisible here and the worktree may score SAFE. The
    git-level backstop is the non-force `git worktree remove`, which itself
    refuses a modified/untracked tree (surfaces as partial_failure/exit 4, not a
    silent discard). Not fixed in S-03 — check_repo is merged/rostered.
    """
    reasons: list[str] = []
    if rc.error:
        reasons.append(f"git error: {rc.error}")
    if rc.dirty:
        reasons.append("uncommitted changes")
    if rc.upstream is None and not proven_merged:
        reasons.append(
            "no upstream (cannot prove pushed/merged; "
            "proven-merged override deferred to S-05)"
        )
    elif rc.upstream is not None and rc.commits_ahead > 0:
        reasons.append(f"{rc.commits_ahead} unpushed commit(s)")
    return (not reasons, reasons)


def discover_workspace_worktrees(ws_root: Path) -> list[Path]:
    """Deterministically enumerate a workspace's worktree subdirs.

    Immediate subdirectories of ws_root that contain a `.git` entry (a linked
    worktree has a `.git` FILE). The `.quoin-workspace.json` marker (a file, not
    a dir) and any stray non-worktree dir are skipped. Returns [] when ws_root
    is absent. Discovery never depends on the ownership record (D-06) — a
    missing/corrupt record can never block teardown.
    """
    if not ws_root.exists():
        return []
    result: list[Path] = []
    try:
        for sub in sorted(ws_root.iterdir()):
            if sub.is_dir() and (sub / ".git").exists():
                result.append(sub)
    except OSError:
        return []
    return result


def _worktree_orig_repo(worktree: Path | str) -> str | None:
    """Derive a worktree's ORIGINAL repo path from `git worktree list --porcelain`.

    The FIRST `worktree ` line is always the main working tree = the original
    repo. Returns its path, or None on git error (worktree then skipped from
    removal — resumable). The S-01 record stores repo NAMES, not original-repo
    absolute paths, so this derivation is what teardown targets with
    `git -C <orig-repo> worktree remove`.
    """
    stdout, _, rc = _run(["git", "-C", str(worktree), "worktree", "list", "--porcelain"])
    if rc != 0:
        return None
    for line in stdout.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):].strip()
    return None


def _read_record_safe(project_root: Path, slug: str) -> dict | None:
    """Read the ownership record, guarding against a valid-JSON non-object (S-02).

    read_ownership_record can return any JSON value (a list, number, or bare
    string) for a corrupt record. Guard isinstance(dict) so a non-object record
    yields None (fall back to deterministic ws_root discovery) instead of an
    AttributeError on `.get()`. The record is advisory only in teardown.
    """
    rec = read_ownership_record(project_root, slug)
    if isinstance(rec, dict):
        return rec
    return None


def _unlink_record_if_present(project_root: Path, slug: str) -> bool:
    """Unlink the ownership record file if it exists. Fail-OPEN (never raises)."""
    path = _record_path(project_root, slug)
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError as exc:
        print(f"[workspace] failed to unlink record {path}: {exc}", file=sys.stderr)
    return False


@dataclasses.dataclass
class WorktreeStatus:
    worktree: str
    orig_repo: str | None
    safe: bool
    reasons: list[str]
    removed: bool
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def takeover_workspace(
    feature: str,
    project_root: Path,
    session_uuid: str | None = None,
    force: bool = False,
    home: str | None = None,
    workspaces_root: Path | None = None,
) -> TakeoverResult:
    """Non-destructively reassign a feature workspace's ownership record (D-05).

    Rewrites ONLY owner_session_uuid/hostname/pid/pid_start_time/last_seen and
    PRESERVES every other field verbatim; performs ZERO git/worktree/folder ops.
    A false-stale misfire therefore only flips a trivially-reversible record and
    byte-preserves all uncommitted work (the R-02 safety net).

    Refuse gate: a LIVE non-self owner refuses unless force=True. Self-owned OR
    stale OR --force all proceed.

    ROUND-3 CLASS HARDENING (C5 in the T-09 census): the ENTIRE body from the
    record read onward is wrapped in a best-effort guard → TakeoverResult(no_record,
    "corrupt record"); ALL record-field reads use .get(...), NEVER a bare subscript.
    """
    slug = slugify(feature)
    project_root = project_root.resolve()
    ws_root = resolve_workspace_root(
        project_root, slug, workspaces_root, _read_record_safe(project_root, slug)
    )
    record_path = _record_path(project_root, slug)

    # Identity resolution — caller-authoritative, derive only if None (MAJ-2).
    resolved_uuid = session_uuid if session_uuid is not None else get_session_uuid.get_session_uuid(
        project_path=str(project_root), phase="ad-hoc"
    )

    base = TakeoverResult(
        slug=slug,
        workspace_path=str(ws_root),
        record_path=str(record_path),
        new_owner=resolved_uuid,
    )

    try:
        record = _read_ownership_record_with_mtime(project_root, slug)  # hardened (T-01)
        if record is None:
            base.no_record = True
            base.message = f"no workspace record for {feature}"
            return base

        is_self = record.get("owner_session_uuid") == resolved_uuid
        live = _owner_is_live(record, home=home)  # owner-keyed JSONL + last_seen/mtime
        if live and not is_self and not force:
            base.refused = True
            base.live = True
            base.message = "owner is live; re-run with --force to override"
            return base

        # NON-DESTRUCTIVE flip: copy the record, drop the internal _record_mtime
        # key, overwrite ONLY the identity fields. PRESERVE everything else.
        new_record = {k: v for k, v in record.items() if k != "_record_mtime"}
        new_record.update(_current_identity_fields(resolved_uuid))
        write_ownership_record(project_root, slug, new_record)

        base.took_over = True
        base.prior_owner = record.get("owner_session_uuid", "")  # .get — no bare subscript (MAJ-1)
        base.message = f"took over {feature}"
        return base
    except Exception as exc:  # noqa: BLE001  (whole-body fail-OPEN, C5 round-3)
        base.no_record = True
        base.message = f"corrupt record: {exc}"
        return base


# ---------------------------------------------------------------------------
# T-06: heartbeat_workspace — owner-only last_seen refresh (D-04, Q-06)
# ---------------------------------------------------------------------------

def _find_workspace_marker(cwd: Path) -> Path | None:
    """Walk UP from cwd to the filesystem root; return the first .quoin-workspace.json.

    Local copy — do NOT import S-02's path_resolve marker helpers (copy-not-import;
    path_resolve returns the artifact root, not the marker path). Fail-OPEN on OSError.
    """
    try:
        current = Path(cwd).resolve()
    except OSError:
        return None
    for candidate in [current, *current.parents]:
        marker = candidate / ".quoin-workspace.json"
        try:
            if marker.is_file():
                return marker
        except OSError:
            continue
    return None


def heartbeat_workspace(cwd: Any, session_uuid: str | None = None, home: str | None = None) -> bool:
    """Refresh last_seen on the ownership record — ONLY for the OWNER session (D-04, Q-06).

    A non-owner session that happens to start inside a workspace must NOT refresh
    last_seen (that would mask the real owner's staleness and defeat takeover).

    Whole function is best-effort (C6 in the T-09 census): ANY exception → return
    False (the hook must never fail a session start); the untrusted marker AND record
    are read with isinstance(dict) + .get(...) discipline, never a bare subscript.
    """
    try:
        marker = _find_workspace_marker(Path(cwd))
        if marker is None:
            return False  # not in a workspace; no-op
        try:
            data = json.loads(marker.read_text())
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(data, dict):
            return False
        artifact_root = data.get("artifact_root")
        feature = data.get("feature")
        if not artifact_root or not feature:
            return False

        project_root = Path(artifact_root)
        slug = slugify(feature)
        record = _read_ownership_record_with_mtime(project_root, slug)
        if record is None:
            return False

        me = session_uuid if session_uuid is not None else get_session_uuid.get_session_uuid(
            project_path=str(project_root), phase="ad-hoc"
        )
        if record.get("owner_session_uuid") != me:
            return False  # non-owner no-op (R-18)

        new_record = {k: v for k, v in record.items() if k != "_record_mtime"}
        new_record["last_seen"] = _utc_now_iso()
        write_ownership_record(project_root, slug, new_record)
        return True
    except Exception:  # noqa: BLE001  (whole-body fail-OPEN, C6 — never fail a start)
        return False


# ---------------------------------------------------------------------------
# S-03 teardown: TeardownResult + teardown_workspace (D-06, FR5)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class TeardownResult:
    slug: str
    workspace_path: str
    per_repo: list[WorktreeStatus] = dataclasses.field(default_factory=list)
    refused: bool = False
    forced: bool = False
    record_removed: bool = False
    folder_removed: bool = False
    partial_failure: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "workspace_path": self.workspace_path,
            "per_repo": [s.to_dict() for s in self.per_repo],
            "refused": self.refused,
            "forced": self.forced,
            "record_removed": self.record_removed,
            "folder_removed": self.folder_removed,
            "partial_failure": self.partial_failure,
            "message": self.message,
        }


def _render_unsafe(statuses: list[WorktreeStatus]) -> str:
    return "; ".join(
        f"{Path(s.worktree).name}: {', '.join(s.reasons)}"
        for s in statuses if not s.safe
    )


def teardown_workspace(
    feature: str,
    project_root: Path,
    force: bool = False,
    workspaces_root: Path | None = None,
) -> TeardownResult:
    """Tear down a feature workspace: guard, remove worktrees, prune, clean up.

    Order is load-bearing (D-06, FR5) — a crash leaves a resumable state:
    discover -> guard (refuse-unless-force) -> remove worktrees FIRST -> prune
    -> rmtree folder+marker (CONFIRM removal) -> unlink record ONLY IF the
    folder is confirmed gone. See architecture.md D-06 and the plan Decisions.
    """
    slug = slugify(feature)
    project_root = project_root.resolve()
    # Record is advisory: it only locates a workspace outside the project, and
    # only under worktree_root() (see resolve_workspace_root).
    ws_root = resolve_workspace_root(
        project_root, slug, workspaces_root, _read_record_safe(project_root, slug)
    )

    worktrees = discover_workspace_worktrees(ws_root)

    # MISSING-WORKSPACE (graceful, exit 0): nothing to remove. If a stray record
    # file lingers, unlink it to clear a dangling record.
    if not ws_root.exists() and not worktrees:
        record_removed = _unlink_record_if_present(project_root, slug)
        return TeardownResult(
            slug=slug,
            workspace_path=str(ws_root),
            record_removed=record_removed,
            message="nothing to tear down",
        )

    # GUARD: classify every worktree (read-only reuse of branch_hygiene).
    statuses: list[WorktreeStatus] = []
    for wt in worktrees:
        orig = _worktree_orig_repo(wt)
        rc = branch_hygiene.check_repo(Path(wt))
        safe, reasons = _classify_worktree_safety(rc, _proven_merged(wt, orig))
        statuses.append(
            WorktreeStatus(
                worktree=str(wt), orig_repo=orig, safe=safe, reasons=reasons,
                removed=False, error=None,
            )
        )

    unsafe = [s for s in statuses if not s.safe]
    if unsafe and not force:
        return TeardownResult(
            slug=slug,
            workspace_path=str(ws_root),
            per_repo=statuses,
            refused=True,
            message=f"refused (unsafe): {_render_unsafe(unsafe)}",
        )

    # FORCE bypass MUST be explicit AND logged (audit trail — critic MIN-1).
    if force and unsafe:
        print(
            f"[workspace] --force teardown: bypassing guard for {slug}; "
            f"unsafe: {_render_unsafe(unsafe)}",
            file=sys.stderr,
        )

    # REMOVE worktrees FIRST (git --force IFF teardown --force: user-force =>
    # git-force). All git ops run -C <orig_repo>, never from the worktree.
    removed_repos: set[str] = set()
    for s in statuses:
        if s.orig_repo is None:
            s.error = "could not derive original repo"
            continue
        wt_path = Path(s.worktree)
        # Idempotency: already-gone worktree -> treat as removed (re-runnable).
        if not wt_path.exists() or not _worktree_registered(
            Path(s.orig_repo), wt_path
        ):
            s.removed = True
            continue
        args = ["git", "-C", s.orig_repo, "worktree", "remove"]
        if force:
            args.append("--force")
        args.append(s.worktree)
        _, err, rc = _run(args)
        if rc == 0:
            s.removed = True
            removed_repos.add(s.orig_repo)
        else:
            s.error = err or "git worktree remove failed"

    # PRUNE stale admin entries for each repo that had a removal.
    for orig in removed_repos:
        _run(["git", "-C", orig, "worktree", "prune"])

    # PARTIAL-FAILURE gate (exit 4): leave folder+record so a re-run finishes.
    if any(s.error and not s.removed for s in statuses):
        failed = [Path(s.worktree).name for s in statuses if s.error and not s.removed]
        return TeardownResult(
            slug=slug,
            workspace_path=str(ws_root),
            per_repo=statuses,
            forced=force,
            partial_failure=True,
            message=f"partial teardown; re-run (failed: {', '.join(failed)})",
        )

    # A workspace outside the project is removed only when nothing but its
    # worktrees and marker lives there, so a misplaced root never takes
    # unrelated folders with it.
    if ws_root != project_root / ".workspaces" / slug:
        known = {str(Path(s.worktree)) for s in statuses} | {str(ws_root / ".quoin-workspace.json")}
        try:
            leftover = [c.name for c in ws_root.iterdir() if str(c) not in known]
        except OSError:
            leftover = []
        if leftover:
            return TeardownResult(
                slug=slug,
                workspace_path=str(ws_root),
                per_repo=statuses,
                forced=force,
                partial_failure=True,
                message=f"worktrees removed; folder kept (unexpected entries: {', '.join(sorted(leftover))})",
            )

    # CLEANUP (all removals succeeded): rmtree folder+marker, CONFIRM removal,
    # unlink record ONLY IF folder confirmed gone (critic MIN-4) — keeps the
    # resumability invariant exact if a locked file leaves ws_root behind.
    shutil.rmtree(ws_root, ignore_errors=True)
    folder_removed = not ws_root.exists()
    record_removed = (
        _unlink_record_if_present(project_root, slug) if folder_removed else False
    )

    return TeardownResult(
        slug=slug,
        workspace_path=str(ws_root),
        per_repo=statuses,
        forced=force,
        folder_removed=folder_removed,
        record_removed=record_removed,
        message="torn down",
    )


# ---------------------------------------------------------------------------
# S-05 status/list (IVG-158): read-only enumeration + post-merge teardown offer.
# ---------------------------------------------------------------------------

def enumerate_workspaces(project_root: Path) -> list[str]:
    """Union of workspace slugs: `.workspaces/*` subdirs + ownership record stems.

    Deterministic (sorted, deduped). Either source alone can be incomplete —
    a record without a live worktree dir (already torn down elsewhere) or a
    worktree dir with a missing/corrupt record — so this unions both rather
    than trusting one exclusively.
    """
    project_root = Path(project_root)
    slugs: set[str] = set()

    ws_parent = project_root / ".workspaces"
    if ws_parent.exists():
        try:
            for sub in ws_parent.iterdir():
                if sub.is_dir():
                    slugs.add(sub.name)
        except OSError:
            pass

    records_dir = project_root / ".workflow_artifacts" / "memory" / "workspaces"
    if records_dir.exists():
        try:
            for rec in records_dir.glob("*.json"):
                slugs.add(rec.stem)
        except OSError:
            pass

    return sorted(slugs)


@dataclasses.dataclass
class WorktreeStatusView:
    worktree: str
    orig_repo: str | None
    branch: str | None
    dirty: bool
    upstream: str | None
    commits_ahead: int
    merged: bool
    safe: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class WorkspaceStatusView:
    feature: str
    slug: str
    workspace_path: str
    owner_session_uuid: str | None
    hostname: str | None
    live: bool
    worktrees: list[WorktreeStatusView] = dataclasses.field(default_factory=list)
    offer_teardown: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "slug": self.slug,
            "workspace_path": self.workspace_path,
            "owner_session_uuid": self.owner_session_uuid,
            "hostname": self.hostname,
            "live": self.live,
            "worktrees": [w.to_dict() for w in self.worktrees],
            "offer_teardown": self.offer_teardown,
            "message": self.message,
        }


def collect_status(
    project_root: Path, workspaces_root: Path | None = None
) -> list[WorkspaceStatusView]:
    """Read-only enumeration + classification of every known workspace.

    READ-ONLY INVARIANT: this never writes, deletes, or mutates any file or
    git state — no record writes, no worktree removal, nothing. The teardown
    OFFER (`offer_teardown`) is data (a bool + a stderr hint from the CLI),
    never an action; declining an offer is simply doing nothing, satisfied by
    construction. Actioning belongs to the (later) skill-wiring stage, which
    is fail-closed under `[no-interactive]`/autonomous.
    """
    project_root = Path(project_root).resolve()
    views: list[WorkspaceStatusView] = []

    for slug in enumerate_workspaces(project_root):
        record = _read_ownership_record_with_mtime(project_root, slug)
        record = record if isinstance(record, dict) else None
        live = _owner_is_live(record) if record else False
        ws_root = resolve_workspace_root(project_root, slug, workspaces_root, record)
        worktrees = discover_workspace_worktrees(ws_root)

        wt_views: list[WorktreeStatusView] = []
        for wt in worktrees:
            orig = _worktree_orig_repo(wt)
            rc = branch_hygiene.check_repo(Path(wt))
            merged = _proven_merged(wt, orig)
            safe, reasons = _classify_worktree_safety(rc, merged)
            wt_views.append(
                WorktreeStatusView(
                    worktree=str(wt),
                    orig_repo=orig,
                    branch=rc.current_branch,
                    dirty=rc.dirty,
                    upstream=rc.upstream,
                    commits_ahead=rc.commits_ahead,
                    merged=merged,
                    safe=safe,
                    reasons=reasons,
                )
            )

        offer_teardown = bool(wt_views) and all(w.merged for w in wt_views)
        if offer_teardown:
            message = "merged — teardown available"
        elif wt_views:
            message = "not yet provably merged"
        else:
            message = "no worktrees found"

        views.append(
            WorkspaceStatusView(
                feature=(record or {}).get("feature", slug),
                slug=slug,
                workspace_path=str(ws_root),
                owner_session_uuid=(record or {}).get("owner_session_uuid"),
                hostname=(record or {}).get("hostname"),
                live=live,
                worktrees=wt_views,
                offer_teardown=offer_teardown,
                message=message,
            )
        )

    return views


# ---------------------------------------------------------------------------
# T-06: CLI + main (create / teardown / takeover / heartbeat / status subcommands)
# ---------------------------------------------------------------------------

def _opt_path(raw: str | None) -> Path | None:
    return Path(raw).expanduser() if raw else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage per-repo git worktree feature workspaces.",
        add_help=True,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create or refresh a feature workspace.")
    create_parser.add_argument("feature", help="Feature name (slugified for the workspace folder).")
    create_parser.add_argument(
        "repos", nargs="*", default=[],
        help="Optional trailing positional repo names to include (union with --repos).",
    )
    create_parser.add_argument(
        "--repos", dest="repos_flag", default=None,
        help="Comma-separated repo names to include (union with trailing positional repos).",
    )
    create_parser.add_argument(
        "--base", default=None,
        help="Base ref for new branches (default: per-repo default_branch()).",
    )
    create_parser.add_argument(
        "--project-root", default=None,
        help="Project root (default: cwd).",
    )
    create_parser.add_argument(
        "--session-uuid", dest="session_uuid", default=None,
        help="Authoritative owner-identity seam (the /workspace skill passes the real "
             "session UUID). Default: best-effort derive via get_session_uuid.",
    )
    create_parser.add_argument(
        "--json", dest="json_output", action="store_true", default=True,
        help="Emit the CreateResult as JSON (default: on).",
    )
    create_parser.add_argument(
        "--branch", default=None,
        help="Branch to check out (default: the feature name).",
    )
    create_parser.add_argument(
        "--repo-path", dest="repo_paths", action="append", default=None,
        help="Repo path to use instead of discovering repos (repeatable).",
    )
    create_parser.add_argument(
        "--workspaces-root", dest="workspaces_root", default=None,
        help="Folder holding workspaces (default: <project>/.workspaces).",
    )

    # takeover — non-destructive ownership-record flip (D-05).
    takeover_parser = subparsers.add_parser("takeover", help="Reassign a stale/owned workspace record.")
    takeover_parser.add_argument("feature", help="Feature name (slugified for the workspace folder).")
    takeover_parser.add_argument(
        "--force", action="store_true", default=False,
        help="Override a LIVE owner (default: refuse a live non-self owner).",
    )
    takeover_parser.add_argument("--project-root", default=None, help="Project root (default: cwd).")
    takeover_parser.add_argument(
        "--session-uuid", dest="session_uuid", default=None,
        help="Authoritative owner-identity seam (default: best-effort derive).",
    )
    # TEST-ONLY seam so subprocess JSONL-liveness tests can inject a fake home.
    takeover_parser.add_argument("--home", default=None, help=argparse.SUPPRESS)
    takeover_parser.add_argument(
        "--workspaces-root", dest="workspaces_root", default=None,
        help="Folder holding workspaces (default: <project>/.workspaces).",
    )
    takeover_parser.add_argument(
        "--json", dest="json_output", action="store_true", default=True,
        help="Emit the TakeoverResult as JSON (default: on).",
    )

    # heartbeat — owner-only last_seen refresh (D-04, Q-06). Always exits 0 (fail-OPEN).
    heartbeat_parser = subparsers.add_parser("heartbeat", help="Refresh last_seen for the owning session.")
    heartbeat_parser.add_argument("--cwd", required=True, help="Session launch dir (walked up to a marker).")
    heartbeat_parser.add_argument(
        "--session-uuid", dest="session_uuid", default=None,
        help="Authoritative owner-identity seam (default: best-effort derive).",
    )
    heartbeat_parser.add_argument("--home", default=None, help=argparse.SUPPRESS)
    heartbeat_parser.add_argument(
        "--json", dest="json_output", action="store_true", default=True,
        help="Emit the refresh result as JSON (default: on).",
    )

    teardown_parser = subparsers.add_parser(
        "teardown", help="Tear down a feature workspace (uncommitted/unpushed guarded)."
    )
    teardown_parser.add_argument(
        "feature", help="Feature name (slugified for the workspace folder)."
    )
    teardown_parser.add_argument(
        "--project-root", default=None,
        help="Project root (default: cwd).",
    )
    teardown_parser.add_argument(
        "--force", action="store_true", default=False,
        help="Explicit escape hatch: bypass the uncommitted/unpushed guard "
             "(logged to stderr) and discard the worktrees anyway.",
    )
    teardown_parser.add_argument(
        "--json", dest="json_output", action="store_true", default=True,
        help="Emit the TeardownResult as JSON (default: on).",
    )
    teardown_parser.add_argument(
        "--workspaces-root", dest="workspaces_root", default=None,
        help="Folder holding workspaces (default: <project>/.workspaces).",
    )

    status_parser = subparsers.add_parser(
        "status", aliases=["list"],
        help="Read-only: list known workspaces with safety + merge state (never mutates).",
    )
    status_parser.add_argument(
        "--project-root", default=None,
        help="Project root (default: cwd).",
    )
    status_parser.add_argument(
        "--json", dest="json_output", action="store_true", default=True,
        help="Emit the status list as JSON (default: on).",
    )
    status_parser.add_argument(
        "--workspaces-root", dest="workspaces_root", default=None,
        help="Folder holding workspaces (default: <project>/.workspaces).",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    if args.command == "create":
        project_root = Path(args.project_root) if args.project_root else Path.cwd()
        named: list[str] = list(args.repos)
        if args.repos_flag:
            named.extend(n.strip() for n in args.repos_flag.split(",") if n.strip())
        named_or_none = named or None

        try:
            result = create_workspace(
                feature=args.feature,
                project_root=project_root,
                named_repos=named_or_none,
                base=args.base,
                session_uuid=args.session_uuid,
                workspaces_root=_opt_path(args.workspaces_root),
                branch=args.branch,
                repo_paths=[Path(p) for p in args.repo_paths] if args.repo_paths else None,
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            print(f"[workspace] {exc}", file=sys.stderr)
            return 3

        print(json.dumps(result.to_dict(), indent=2))
        for r in result.per_repo:
            status = "skipped" if r.skipped else ("created" if r.created else "error")
            print(f"{status} {r.repo} -> {r.dest} ({r.error or 'ok'})", file=sys.stderr)

        if result.refused or (not result.record_written and not result.per_repo):
            return 3
        if not result.per_repo or not any(r.created or r.skipped for r in result.per_repo):
            return 3
        return 0

    if args.command == "takeover":
        project_root = Path(args.project_root) if args.project_root else Path.cwd()
        result = takeover_workspace(
            feature=args.feature,
            project_root=project_root,
            session_uuid=args.session_uuid,
            force=args.force,
            home=args.home,
            workspaces_root=_opt_path(args.workspaces_root),
        )
        print(json.dumps(result.to_dict(), indent=2))
        print(f"[workspace] takeover {args.feature}: {result.message}", file=sys.stderr)
        if result.no_record:
            return 4
        if result.refused:
            return 3
        return 0

    if args.command == "heartbeat":
        refreshed = heartbeat_workspace(
            cwd=args.cwd,
            session_uuid=args.session_uuid,
            home=args.home,
        )
        print(json.dumps({"refreshed": refreshed}, indent=2))
        return 0  # ALWAYS 0 (fail-OPEN, like get_session_uuid)

    if args.command == "teardown":
        project_root = Path(args.project_root) if args.project_root else Path.cwd()

        try:
            result = teardown_workspace(
                feature=args.feature,
                project_root=project_root,
                force=args.force,
                workspaces_root=_opt_path(args.workspaces_root),
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            print(f"[workspace] {exc}", file=sys.stderr)
            return 3

        print(json.dumps(result.to_dict(), indent=2))
        for s in result.per_repo:
            name = Path(s.worktree).name
            if result.refused and not s.safe:
                print(f"refused {name}: {', '.join(s.reasons)}", file=sys.stderr)
            elif s.error and not s.removed:
                print(f"failed {name}: {s.error}", file=sys.stderr)
            elif s.removed:
                print(f"removed {name}", file=sys.stderr)

        if result.refused:
            return 3
        if result.partial_failure:
            return 4
        return 0

    if args.command in ("status", "list"):
        project_root = Path(args.project_root) if args.project_root else Path.cwd()
        views = collect_status(project_root, _opt_path(args.workspaces_root))

        print(json.dumps({"workspaces": [v.to_dict() for v in views]}, indent=2))
        for v in views:
            print(
                f"{v.feature}: live={v.live} offer_teardown={v.offer_teardown}",
                file=sys.stderr,
            )
        return 0  # read-only informational; always 0 (argparse errors stay exit 2)

    return 2


if __name__ == "__main__":
    sys.exit(main())
