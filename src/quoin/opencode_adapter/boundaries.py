"""Role boundaries: what a phase run may change, and the child permission check.

Before and after a run the coordinator takes a listing of the paths a role can
legitimately touch plus the source state of every repository, and `verify`
compares the two against the rules for the role that ran. The listing is
deliberately narrow:

- the artifact root, except `memory/`, `cache/` and anything below a
  `finalized/` directory (only that directory's immediate children are listed);
- inside `memory/`, only `continuation/`, `runtime/opencode/` and
  `lessons-learned.md`, because every other memory file is written by other
  tools or is owned by the agent;
- inside `.opencode/` and `.quoin/`, only the paths the install record owns plus
  the two install records, never the files OpenCode itself creates there.

Symlinks are never followed. A result is `ok`, `violation` or `unverified`; a
check that cannot be made is never reported as `ok`.

The second half of the module evaluates permission rules and checks, at
generation time, that a delegated child role is never allowed more than the
parent that delegates to it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from . import install, jsonio, names, runstore, scripts

ARTIFACT_ROOT = ".workflow_artifacts"
_STORE_REL = ARTIFACT_ROOT + "/memory/runtime/opencode/"
_CONTINUATION_REL = ARTIFACT_ROOT + "/memory/continuation/"
_LESSONS_REL = ARTIFACT_ROOT + "/memory/lessons-learned.md"
_MEMORY_REL = ARTIFACT_ROOT + "/memory/"
_DISCOVER_FILES = (
    ARTIFACT_ROOT + "/memory/repos-inventory.md",
    ARTIFACT_ROOT + "/memory/architecture-overview.md",
    ARTIFACT_ROOT + "/memory/dependencies-map.md",
)
_INSTALL_RECORDS = (".quoin/opencode-install.json", ".quoin/runtime.json")
_LEDGER_NAME = "cost-ledger.md"
_HASH_CHUNK = 1 << 20
_MAX_VIOLATIONS = 20
_MAX_LOCK_BYTES = 4096
_MAX_LOCKS = 200

DEFAULT_MAX_ENTRIES = 50000
DEFAULT_MAX_HASH_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024

Entry = Tuple[str, int, int, Optional[str]]


@dataclass(frozen=True)
class Listing:
    """What a listing saw. `entries` maps a project-relative path to
    `(kind, size, mtime_ns, sha256_or_link_text)`; kind is `f` (file), `l`
    (symlink, the slot holds its target text) or `d` (a directory, only as an
    immediate child of a `finalized/` directory). `live_task_locks` names every
    task whose supervisor lock named a live process at listing time. `taken_at`
    is informational and never compared with another clock."""

    entries: Mapping[str, Entry]
    repos: Tuple[Mapping[str, Any], ...] = ()
    live_task_locks: FrozenSet[str] = frozenset()
    taken_at: str = ""
    truncated: bool = False
    error: Optional[str] = None


@dataclass(frozen=True)
class BoundaryResult:
    status: str
    violations: Tuple[Mapping[str, str], ...] = ()
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

class _Walker:
    def __init__(self, root: str, max_entries: int, max_hash_bytes: int, max_total_bytes: int) -> None:
        self.root = root
        self.max_entries = max_entries
        self.max_hash_bytes = max_hash_bytes
        self.max_total_bytes = max_total_bytes
        self.entries: Dict[str, Entry] = {}
        self.hashed = 0
        self.truncated = False

    def full(self) -> bool:
        return self.truncated

    def _put(self, rel: str, entry: Entry) -> None:
        if len(self.entries) >= self.max_entries:
            self.truncated = True
            return
        self.entries[rel] = entry

    def _hash(self, path: str, size: int) -> Optional[str]:
        if size > self.max_hash_bytes or self.hashed + size > self.max_total_bytes:
            # An unhashed file is still compared by size and mtime, so a file
            # too large to hash must not make the whole listing unverifiable.
            return None
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return None
        digest = hashlib.sha256()
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None
            # Read no further than the size recorded for the listing, so a
            # file that grows while it is hashed cannot overrun the budget.
            left = size
            while left > 0:
                chunk = os.read(fd, min(_HASH_CHUNK, left))
                if not chunk:
                    break
                digest.update(chunk)
                left -= len(chunk)
                self.hashed += len(chunk)
        except OSError:
            return None
        finally:
            os.close(fd)
        return digest.hexdigest()

    def add_path(self, rel: str, *, hashed: bool = True) -> None:
        """List one path (never following a link). Absent paths are skipped."""
        path = os.path.join(self.root, rel)
        try:
            info = os.lstat(path)
        except OSError:
            return
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            try:
                text = os.readlink(path)
            except OSError:
                text = ""
            self._put(rel, ("l", len(text), info.st_mtime_ns, text))
        elif stat.S_ISREG(mode):
            digest = self._hash(path, info.st_size) if hashed else None
            self._put(rel, ("f", info.st_size, info.st_mtime_ns, digest))
        elif stat.S_ISDIR(mode):
            self._put(rel, ("d", 0, 0, None))

    def _children(self, rel: str) -> List[os.DirEntry]:
        try:
            with os.scandir(os.path.join(self.root, rel)) as it:
                return sorted(it, key=lambda e: e.name)
        except OSError:
            return []

    def walk(self, rel: str, *, skip_top: Callable[[str], bool] = lambda name: False) -> None:
        """List a directory tree below `rel`. A directory named `finalized`
        contributes only its immediate children; `skip_top` filters the first
        level."""
        path = os.path.join(self.root, rel)
        try:
            info = os.lstat(path)
        except OSError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            self.add_path(rel)
            return
        stack = [rel]
        while stack and not self.full():
            current = stack.pop()
            for child in self._children(current):
                if self.full():
                    return
                name = child.name
                child_rel = current + "/" + name
                if current == rel and skip_top(name):
                    continue
                if name == _LEDGER_NAME:
                    continue
                try:
                    cinfo = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(cinfo.st_mode):
                    if name == "finalized":
                        for sub in self._children(child_rel):
                            self.add_path(child_rel + "/" + sub.name, hashed=False)
                    else:
                        stack.append(child_rel)
                else:
                    self.add_path(child_rel, hashed=not _is_store_history(child_rel))


_STORE_STATE_RE = re.compile(r"(?:task|workflow)-[^/]+\.json\Z")


def _is_store_history(rel: str) -> bool:
    """A run-store file other than the small per-task pointer and workflow
    records: the accumulated run history, which grows without bound and is
    compared by size and mtime only."""
    return rel.startswith(_STORE_REL) and not _STORE_STATE_RE.match(rel[len(_STORE_REL):])


def _iso(clock: Optional[Callable[[], float]]) -> str:
    value = clock() if clock is not None else time.time()
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _live_locks(root: str) -> FrozenSet[str]:
    memory = os.path.join(root, ARTIFACT_ROOT, "memory")
    prefix, suffix = "run-supervisor-", ".pid"
    try:
        found = sorted(
            e.name for e in os.scandir(memory) if e.name.startswith(prefix) and e.name.endswith(suffix)
        )
    except OSError:
        return frozenset()
    live = set()
    for name in found[:_MAX_LOCKS]:
        task = name[len(prefix):len(name) - len(suffix)]
        if not runstore.TASK_RE.match(task):
            continue
        got = jsonio.read_regular_bytes(os.path.join(memory, name), max_bytes=_MAX_LOCK_BYTES)
        if got is None:
            continue
        try:
            data = json.loads(got[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and _pid_alive(data.get("pid")):
            live.add(task)
    return frozenset(live)


def take_listing(
    project_root, *, clock: Optional[Callable[[], float]] = None,
    max_entries: int = DEFAULT_MAX_ENTRIES, max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> Listing:
    """List the boundary scope of the project and read each repository's source
    state. Never raises; a listing that cannot be complete says so through
    `truncated` or `error`."""
    root = os.path.realpath(str(project_root))
    taken_at = _iso(clock)
    walker = _Walker(root, max_entries, max_hash_bytes, max_total_bytes)
    error: Optional[str] = None

    walker.walk(ARTIFACT_ROOT, skip_top=lambda name: name in ("memory", "cache"))
    memory_rel = ARTIFACT_ROOT + "/memory"
    walker.walk(memory_rel + "/continuation")
    walker.walk(memory_rel + "/runtime/opencode")
    walker.add_path(_LESSONS_REL)

    try:
        meta = install.load_metadata(root)
    except (install.InstallError, OSError):
        meta = None
        error = "install-record-unreadable"
    if meta is None:
        if error is None:
            error = "install-record-missing"
    else:
        for rel in sorted(meta.owned):
            walker.add_path(rel)
    for rel in _INSTALL_RECORDS:
        walker.add_path(rel)

    try:
        repos = tuple(runstore.repo_revisions(root, source=True))
    except Exception:  # noqa: BLE001 - the source state stays unverifiable
        repos = ()
        error = error or "source-state-failed"
    return Listing(
        entries=dict(walker.entries), repos=repos, live_task_locks=_live_locks(root),
        taken_at=taken_at, truncated=walker.truncated, error=error,
    )


def listing_to_json(listing: Listing) -> Dict[str, Any]:
    """A JSON-ready form of `listing` that `listing_from_json` reads back."""
    return {
        "version": 1,
        "entries": {rel: list(entry) for rel, entry in sorted(listing.entries.items())},
        "repos": [dict(repo) for repo in listing.repos],
        "live_task_locks": sorted(listing.live_task_locks),
        "taken_at": listing.taken_at,
        "truncated": bool(listing.truncated),
        "error": listing.error,
    }


def _entry_from_json(value: Any) -> Optional[Entry]:
    if not isinstance(value, list) or len(value) != 4:
        return None
    kind, size, mtime, digest = value
    if kind not in ("f", "l", "d"):
        return None
    for number in (size, mtime):
        if not isinstance(number, int) or isinstance(number, bool):
            return None
    if digest is not None and not isinstance(digest, str):
        return None
    return (kind, size, mtime, digest)


def listing_from_json(data: Any) -> Optional[Listing]:
    """The listing `data` describes, or None when any field is malformed."""
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    raw_entries = data.get("entries")
    repos = data.get("repos")
    locks = data.get("live_task_locks")
    taken_at = data.get("taken_at")
    error = data.get("error")
    if not isinstance(raw_entries, dict) or not isinstance(repos, list) or not isinstance(locks, list):
        return None
    if not isinstance(taken_at, str) or not isinstance(data.get("truncated"), bool):
        return None
    if error is not None and not isinstance(error, str):
        return None
    if not all(isinstance(item, str) for item in locks) or not all(isinstance(item, dict) for item in repos):
        return None
    entries: Dict[str, Entry] = {}
    for rel, value in raw_entries.items():
        entry = _entry_from_json(value)
        if not isinstance(rel, str) or entry is None:
            return None
        entries[rel] = entry
    return Listing(
        entries=entries, repos=tuple(dict(repo) for repo in repos), live_task_locks=frozenset(locks),
        taken_at=taken_at, truncated=data["truncated"], error=error,
    )


# ---------------------------------------------------------------------------
# role rules
# ---------------------------------------------------------------------------

_STAGE_DIR_RE = re.compile(r"stage-\d+\Z")
_CRITIC_FILE_RE = re.compile(r"critic-response-\d+\.md\Z")
_REVIEW_FILE_RE = re.compile(r"review-\d+\.md\Z")

_TASK_WRITERS = ("investigator", "architect", "planner", "implementer")
_NOTHING = ("critic-snapshot", "reviewer-snapshot", "continue_work")


def _is_gate_file(base: str) -> bool:
    return base.startswith("gate-") and base.endswith(".md")


@dataclass(frozen=True)
class RoleRule:
    source_unchanged: bool


ROLE_RULES: Dict[str, RoleRule] = {
    "investigator": RoleRule(True),
    "architect": RoleRule(True),
    "planner": RoleRule(True),
    "implementer": RoleRule(False),
    "critic-real": RoleRule(True),
    "reviewer-real": RoleRule(True),
    "critic-snapshot": RoleRule(True),
    "reviewer-snapshot": RoleRule(True),
    "gate": RoleRule(True),
    "checkpoint": RoleRule(True),
    "continue_work": RoleRule(True),
    "end_of_task": RoleRule(False),
}

_PHASE_KEYS = {
    "discover": "investigator", "architect": "architect", "plan": "planner",
    "thorough_plan": "planner", "implement": "implementer", "gate": "gate",
    "checkpoint": "checkpoint", "continue_work": "continue_work", "end_of_task": "end_of_task",
}


def role_key(record: Mapping[str, Any]) -> Optional[str]:
    """The boundary rule key for a run record, or None when it is unknown.

    The request's phase decides for every phase the runtime can start; a
    critic or review run maps to a snapshot key when the request names a
    workspace (the run happened away from the real tree) and to the real-tree
    key otherwise. A record without a recognised phase falls back to the
    prepared role."""
    prepared = record.get("prepared") if isinstance(record.get("prepared"), Mapping) else {}
    request = record.get("request") if isinstance(record.get("request"), Mapping) else {}
    phase = str(request.get("phase") or "").replace("-", "_")
    workspace = bool(request.get("workspace"))
    role = prepared.get("role")

    def sided(kind: str) -> str:
        return "%s-%s" % (kind, "snapshot" if workspace else "real")

    if phase == "critic":
        return sided("critic")
    if phase == "review":
        return sided("reviewer")
    if phase in _PHASE_KEYS:
        return _PHASE_KEYS[phase]
    if role in ("critic", "reviewer"):
        return sided(role)
    if role in ("investigator", "architect", "planner", "implementer", "gate"):
        return role
    return None


def window_exclusions(task: str, run_id: str, prior_run_id: Optional[str] = None) -> FrozenSet[str]:
    """Paths the coordinator process itself writes while a run is open: the
    store files of this run and of the run the task pointer named before it,
    the task pointer, and the supervisor lock and result files."""
    out = {
        _STORE_REL + "task-%s.json" % task,
        _MEMORY_REL + "run-supervisor-%s.pid" % task,
        _MEMORY_REL + "run-supervisor-%s.result" % task,
    }
    for rid in (run_id, prior_run_id):
        if rid:
            for suffix in (".jsonl", ".run.json", ".checkpoint.json"):
                out.add(_STORE_REL + rid + suffix)
    return frozenset(out)


class _Context:
    def __init__(self, role: str, task: str, stage_rel: Optional[str], parent: Optional[str]) -> None:
        self.role = role
        self.task = task
        self.stage_rel = stage_rel
        self.parent = parent
        folder = "%s/%s" % (parent, task) if parent else task
        self.task_prefix = "%s/%s/" % (ARTIFACT_ROOT, folder)
        self.finalized_dests: FrozenSet[str] = frozenset()
        self.dest_added = False

    def scope(self, rel: str) -> Tuple[str, Optional[str]]:
        if rel.startswith(self.task_prefix) or rel == self.task_prefix.rstrip("/"):
            return "task", None
        if rel.startswith(_STORE_REL):
            base = rel[len(_STORE_REL):]
            match = re.match(r"(?:task|workflow)-(.+)\.json\Z", base)
            return "store", match.group(1) if match else None
        if rel.startswith(_CONTINUATION_REL):
            return "continuation", None
        if rel == _LESSONS_REL:
            return "lessons", None
        if rel.startswith(_MEMORY_REL):
            return "memory", None
        if rel.startswith(ARTIFACT_ROOT + "/"):
            parts = rel[len(ARTIFACT_ROOT) + 1:].split("/")
            if len(parts) == 1:
                return "artifact-root", None
            if parts[0] == "finalized" or (len(parts) >= 3 and parts[1] == "finalized"):
                return "finalized", None
            return "other-task", parts[0]
        if rel.startswith(".opencode/") or rel.startswith(".quoin/"):
            return "owned", None
        return "other", None

    def stage_ok(self, sub: str) -> bool:
        """`sub` is the path below the task folder; true for a file directly in
        the task folder or in a stage folder (the named one, when given)."""
        parts = sub.split("/")
        if len(parts) == 1:
            return True
        if len(parts) == 2:
            if self.stage_rel is not None:
                return parts[0] == self.stage_rel
            return bool(_STAGE_DIR_RE.match(parts[0]))
        return False


def _allowed(ctx: _Context, rel: str, change: str, scope: str) -> bool:
    role = ctx.role
    if role in _NOTHING:
        return False
    base = rel.rsplit("/", 1)[-1]
    if scope == "task":
        sub = rel[len(ctx.task_prefix):]
        if sub.split("/", 1)[0] == "finalized" and role != "end_of_task":
            return False
        if role in _TASK_WRITERS:
            return bool(sub) and not _is_gate_file(base)
        if role == "critic-real":
            return change == "added" and bool(_CRITIC_FILE_RE.match(base)) and ctx.stage_ok(sub)
        if role == "reviewer-real":
            return change == "added" and bool(_REVIEW_FILE_RE.match(base)) and ctx.stage_ok(sub)
        if role == "gate":
            return _is_gate_file(base) and ctx.stage_ok(sub)
        if role == "end_of_task":
            return change == "removed" and ctx.dest_added
        return False
    if scope == "artifact-root":
        return role == "investigator" and rel == ARTIFACT_ROOT + "/discovery-map.json"
    if scope == "memory":
        return role == "investigator" and rel in _DISCOVER_FILES
    if scope == "continuation":
        return role == "checkpoint" and rel in (
            _CONTINUATION_REL + "%s.json" % ctx.task, _CONTINUATION_REL + "%s.prev.json" % ctx.task,
        )
    if scope == "lessons":
        return role == "end_of_task"
    if scope == "store":
        return role == "gate" and rel == _STORE_REL + "workflow-%s.json" % ctx.task
    if scope == "finalized":
        return role == "end_of_task" and change == "added" and rel in ctx.finalized_dests
    return False


_RULE_NAMES = {
    "task": "role-task-scope", "other-task": "other-task", "store": "coordinator-owned",
    "continuation": "coordinator-owned", "lessons": "role-memory", "memory": "role-memory",
    "owned": "owned-surface", "finalized": "finalized", "artifact-root": "artifact-root",
    "other": "outside-scope",
}


def _differs(a: Optional[Entry], b: Optional[Entry]) -> bool:
    if a is None or b is None:
        return a is not b
    if a[0] != b[0] or a[1] != b[1]:
        return True
    if a[3] is not None and b[3] is not None:
        return a[3] != b[3]
    return a[2] != b[2]


def _source_state(before: Listing, after: Listing) -> Tuple[str, Optional[str]]:
    """`("same", None)`, `("changed", repo_path)` or `("unverifiable", None)`."""
    if not before.repos or not after.repos:
        return "unverifiable", None
    after_by = {r.get("path"): r for r in after.repos}
    before_by = {r.get("path"): r for r in before.repos}
    if set(after_by) != set(before_by):
        return "unverifiable", None

    def verifiable(entry: Mapping[str, Any]) -> bool:
        if entry.get("error") or entry.get("source_error") or not entry.get("head"):
            return False
        dirty = entry.get("source_dirty")
        if dirty is None:
            return False
        return not (dirty and not entry.get("source_digest"))

    for path in sorted(before_by, key=str):
        if not verifiable(before_by[path]) or not verifiable(after_by[path]):
            return "unverifiable", None
    for path in sorted(before_by, key=str):
        old, new = before_by[path], after_by[path]
        if any(old.get(k) != new.get(k) for k in ("head", "source_dirty", "source_digest")):
            return "changed", str(path)
    return "same", None


def verify(
    role: str, before: Listing, after: Listing, *, task: str, run_id: str,
    prior_run_id: Optional[str] = None, stage_rel: Optional[str] = None, parent: Optional[str] = None,
    window_partial: bool = False, other_task_policy: str = "violation",
) -> BoundaryResult:
    """Compare two listings against the rules for `role`.

    `violation` when a path outside the role's allowance changed, or a
    source-unchanged role changed a repository; `unverified` when the check
    cannot be completed (truncated or failed listing, unknown role,
    unverifiable source state, a change that may belong to a concurrent writer,
    or a window that began before this listing); otherwise `ok`."""
    rule = ROLE_RULES.get(role)
    if rule is None:
        return BoundaryResult("unverified", (), "unknown-role")
    if before.truncated or after.truncated:
        return BoundaryResult("unverified", (), "listing-truncated")

    reasons: List[str] = []
    for listing in (before, after):
        err = listing.error
        if err and not (role == "end_of_task" and err.startswith("install-record")):
            if err not in reasons:
                reasons.append(err)

    ctx = _Context(role, task, stage_rel, parent)
    if role == "end_of_task":
        dests = {"%s/finalized/%s" % (ARTIFACT_ROOT, task)}
        if parent:
            dests.add("%s/%s/finalized/%s" % (ARTIFACT_ROOT, parent, task))
        ctx.finalized_dests = frozenset(dests)
        ctx.dest_added = any(
            d in after.entries and d not in before.entries and after.entries[d][0] == "d" for d in dests
        )

    excluded = window_exclusions(task, run_id, prior_run_id)
    live = (before.live_task_locks | after.live_task_locks) - {task}
    violations: List[Dict[str, str]] = []
    soft: List[str] = []

    for rel in sorted(set(before.entries) | set(after.entries)):
        old, new = before.entries.get(rel), after.entries.get(rel)
        if not _differs(old, new) or rel in excluded:
            continue
        change = "added" if old is None else "removed" if new is None else "changed"
        scope, owner = ctx.scope(rel)
        if new is not None and new[0] == "l":
            violations.append({"path": rel, "change": change, "rule": "symlink"})
            continue
        if _allowed(ctx, rel, change, scope):
            continue
        if scope == "other-task" and owner != task:
            if owner in live:
                soft.append("concurrent-task-run")
                continue
            if other_task_policy == "unverified":
                soft.append("concurrent-writer-unlocked")
                continue
        if scope == "store" and (owner in live if owner is not None else bool(live)) and owner != task:
            soft.append("concurrent-task-run")
            continue
        violations.append({"path": rel, "change": change, "rule": _RULE_NAMES.get(scope, "outside-scope")})

    if rule.source_unchanged:
        state, repo = _source_state(before, after)
        if state == "changed":
            violations.append({"path": repo or ".", "change": "changed", "rule": "source-unchanged"})
        elif state == "unverifiable":
            reasons.append("source-unverifiable")

    if violations:
        return BoundaryResult("violation", tuple(violations[:_MAX_VIOLATIONS]), "boundary-violation")
    reasons.extend(soft[:1])
    if window_partial:
        reasons.append("boundary-window-partial")
    if reasons:
        return BoundaryResult("unverified", (), reasons[0])
    return BoundaryResult("ok", (), None)


# ---------------------------------------------------------------------------
# permission rules and child inheritance
# ---------------------------------------------------------------------------

_ORDINAL = {"deny": 0, "ask": 1, "allow": 2}
_SCRIPT_ALLOW_RE = re.compile(r"quoin opencode script ([a-z_]+) \*\Z")


def _wildcard(pattern: str) -> "re.Pattern[str]":
    if pattern.endswith(" *"):
        body = re.escape(pattern[:-2]).replace(r"\*", ".*")
        return re.compile(r"^%s( .*)?$" % body, re.S)
    return re.compile(r"^%s$" % re.escape(pattern).replace(r"\*", ".*"), re.S)


def evaluate_rule(rule: Any, subject: str) -> Optional[str]:
    """The action a permission rule gives `subject`: a bare action string is
    returned as is; a map is evaluated last-match-wins with `*` matching any run
    of characters (a trailing ` *` also matches the bare command). None when no
    pattern matches."""
    if isinstance(rule, str):
        return rule
    action = None
    for pattern, value in rule.items():
        if _wildcard(pattern).match(subject):
            action = value
    return action


def _may_overlap(left: str, right: str) -> bool:
    def norm(p: str) -> str:
        return p[:-2] + "*" if p.endswith(" *") else p

    a, b = norm(left), norm(right)
    pre_a, pre_b = a.split("*", 1)[0], b.split("*", 1)[0]
    if not (pre_a.startswith(pre_b) or pre_b.startswith(pre_a)):
        return False
    suf_a, suf_b = a.rsplit("*", 1)[-1], b.rsplit("*", 1)[-1]
    return suf_a.endswith(suf_b) or suf_b.endswith(suf_a)


def _resolve(side: Mapping[str, Any], key: str) -> Any:
    if key in side:
        return side[key]
    if "*" in side:
        return side["*"]
    return "allow"


def _patterns(rule: Any) -> List[Tuple[str, str]]:
    return [("*", rule)] if isinstance(rule, str) else list(rule.items())


def _parent_bound(parent_rule: Any, pattern: str) -> str:
    bound = evaluate_rule(parent_rule, pattern)
    if bound not in _ORDINAL:
        bound = "allow"
    if isinstance(parent_rule, str):
        return bound
    items = list(parent_rule.items())
    last = -1
    rx_matches = [i for i, (p, _) in enumerate(items) if _wildcard(p).match(pattern)]
    if rx_matches:
        last = rx_matches[-1]
    for later_pattern, action in items[last + 1:]:
        if action in _ORDINAL and _may_overlap(pattern, later_pattern) and _ORDINAL[action] < _ORDINAL[bound]:
            bound = action
    return bound


def check_inheritance(maps: Mapping[str, Mapping[str, Any]], graph: Mapping[str, Sequence[str]]) -> List[str]:
    """Findings for every allowed delegation edge where the child role may do
    more than its parent.

    `maps` is each role's permission map; `graph` lists each role's delegation
    target agent names. For every permission key either side names, the
    effective action of each child pattern (deny 0, ask 1, allow 2) must not
    exceed the parent's action for that same pattern, lowered by any later
    parent pattern that may overlap it. A key a side leaves out resolves to that
    side's top-level `*` and then to the built-in default, taken as allow. A
    child rule equal to its parent's always passes. Read-only helper scripts
    pass; the write-capable one passes only when the parent may edit the
    artifact root. Every role resolves under the same compiled profile because
    agent files carry no model key, so no profile or classification check is
    needed here."""
    agent_to_role = {names.role_agent_name(role): role for role in maps}
    findings: List[str] = []
    for parent in sorted(graph):
        if parent not in maps:
            continue
        for agent in graph[parent]:
            child = agent_to_role.get(agent)
            if child is None:
                continue
            findings.extend(_check_edge(parent, maps[parent], child, maps[child]))
    return findings


def _check_edge(parent: str, pmap: Mapping[str, Any], child: str, cmap: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for key in sorted(set(pmap) | set(cmap)):
        prule, crule = _resolve(pmap, key), _resolve(cmap, key)
        if prule == crule:
            continue
        for pattern, action in _patterns(crule):
            if action not in _ORDINAL:
                continue
            if _ORDINAL[action] <= _ORDINAL[_parent_bound(prule, pattern)]:
                continue
            if key == "bash" and action == "allow" and _script_exempt(pmap, pattern):
                continue
            out.append(
                "role '%s' (child of '%s'): permission '%s' pattern '%s' is %s, "
                "but the parent allows at most %s"
                % (child, parent, key, pattern, action, _parent_bound(prule, pattern))
            )
    return out


def _script_exempt(parent_map: Mapping[str, Any], pattern: str) -> bool:
    match = _SCRIPT_ALLOW_RE.match(pattern)
    if not match:
        return False
    if match.group(1) not in scripts.WRITE_CAPABLE_SCRIPTS:
        return True
    return evaluate_rule(_resolve(parent_map, "edit"), ARTIFACT_ROOT + "/x") == "allow"
