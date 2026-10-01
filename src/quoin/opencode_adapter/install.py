"""Install, check and uninstall the OpenCode adapter's generated files.

The install side of the adapter: a strict-schema ownership record at
``.quoin/opencode-install.json``, path-safety primitives that never follow a
symlink for reading or writing, and (added by later tasks in this module) a
planner and an applier that make a clean re-install byte-identical to the
tree it started from.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from quoin.opencode_adapter import frontmatter, generate, jsonio, manifest, names

# Largest owned file the launcher or the doctor will read back for checks.
MAX_OWNED_BYTES = 16 * 1024 * 1024

_AGENT_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

METADATA_RELPATH = ".quoin/opencode-install.json"
METADATA_SCHEMA_VERSION = 1

_NAME_BODY = r"[a-z0-9]+(?:-[a-z0-9]+)*"
assert names.NAME_RE.pattern == r"^[a-z0-9]+(-[a-z0-9]+)*$"

# Full-match: the only five relpath shapes an install ever owns.
OWNED_PATH_RE = re.compile(
    r"(?:"
    r"\.opencode/commands/quoin-%(n)s\.md"
    r"|\.opencode/agents/quoin-%(n)s\.md"
    r"|\.opencode/skills/quoin-%(n)s/SKILL\.md"
    r"|\.opencode/quoin/instructions\.md"
    r"|\.opencode/opencode\.jsonc"
    r")" % {"n": _NAME_BODY}
)

# Full-match: every directory an install is ever allowed to create or prune.
CREATABLE_DIRS_RE = re.compile(
    r"(?:"
    r"\.opencode"
    r"|\.opencode/commands"
    r"|\.opencode/agents"
    r"|\.opencode/skills"
    r"|\.opencode/skills/quoin-%(n)s"
    r"|\.opencode/quoin"
    r"|\.quoin"
    r")" % {"n": _NAME_BODY}
)

KINDS = ("agent", "command", "config", "instructions", "skill")
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# Full-match on a directory entry's basename: the temp-file shape `atomic_write`
# creates and renames away from. A file left with this name (an interrupted
# write, before the rename lands) is Quoin's own debris and safe to sweep.
TEMP_NAME_RE = re.compile(r"\.quoin-tmp-[0-9a-f]{16}")

_METADATA_KEYS = frozenset(
    {"schema_version", "quoin_version", "opencode_version", "profile", "owned", "created_dirs"}
)
_OWNED_RECORD_KEYS = frozenset({"sha256", "source_digest", "kind", "id"})


class InstallError(Exception):
    """Raised for a usage, metadata or generation error.

    ``exit_code`` is the process exit code the CLI should return; every
    raise site in this module uses 2 (usage, metadata or generation error).
    """

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Metadata:
    quoin_version: str
    opencode_version: str
    profile: Optional[str]
    owned: Dict[str, dict]
    created_dirs: List[str] = field(default_factory=list)


@dataclass
class PathState:
    state: str
    data: Optional[bytes] = None


def _lstat_or(path: Path, missing_state: str):
    try:
        return os.lstat(str(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError("cannot stat %s: %s" % (path, exc), 2) from exc


def load_metadata(root) -> Optional[Metadata]:
    """Load and strictly validate the ownership record under ``root``.

    Returns ``None`` only when ``.quoin`` is absent, or ``.quoin`` is a real
    directory and the metadata file is absent. Every other irregularity
    (wrong type, symlink, malformed JSON, an owned path or directory outside
    the families this module knows about, ...) raises ``InstallError`` and
    never as a side effect of reading the file's contents into the message.
    """
    root = Path(root)
    quoin_dir = root / ".quoin"
    dir_st = _lstat_or(quoin_dir, "missing")
    if dir_st is None:
        return None
    if stat.S_ISLNK(dir_st.st_mode):
        raise InstallError("%s is a symlink, expected a directory" % quoin_dir, 2)
    if not stat.S_ISDIR(dir_st.st_mode):
        raise InstallError("%s exists but is not a directory" % quoin_dir, 2)

    meta_path = root / METADATA_RELPATH
    file_st = _lstat_or(meta_path, "missing")
    if file_st is None:
        return None
    if stat.S_ISLNK(file_st.st_mode):
        raise InstallError("%s is a symlink, expected a regular file" % meta_path, 2)
    if not stat.S_ISREG(file_st.st_mode):
        raise InstallError("%s exists but is not a regular file" % meta_path, 2)

    try:
        raw = meta_path.read_bytes()
    except OSError as exc:
        raise InstallError("cannot read %s: %s" % (meta_path, exc), 2) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError("%s is not valid UTF-8" % meta_path, 2) from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError("%s is not valid JSON: %s" % (meta_path, exc), 2) from exc

    if not isinstance(obj, dict) or set(obj.keys()) != _METADATA_KEYS:
        raise InstallError("%s does not have the expected top-level keys" % meta_path, 2)
    if obj.get("schema_version") != METADATA_SCHEMA_VERSION:
        raise InstallError(
            "%s schema_version is %r, expected %d" % (meta_path, obj.get("schema_version"), METADATA_SCHEMA_VERSION),
            2,
        )
    quoin_version = obj["quoin_version"]
    opencode_version = obj["opencode_version"]
    if not isinstance(quoin_version, str) or not isinstance(opencode_version, str):
        raise InstallError("%s quoin_version and opencode_version must be strings" % meta_path, 2)

    profile = obj["profile"]
    if profile is not None and (not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile)):
        raise InstallError("%s profile %r is not a valid profile label" % (meta_path, profile), 2)

    owned = obj["owned"]
    if not isinstance(owned, dict):
        raise InstallError("%s 'owned' must be an object" % meta_path, 2)
    for relpath, record in owned.items():
        if not isinstance(relpath, str) or not OWNED_PATH_RE.fullmatch(relpath):
            raise InstallError("%s owned path %r is outside the generated families" % (meta_path, relpath), 2)
        if not isinstance(record, dict) or set(record.keys()) != _OWNED_RECORD_KEYS:
            raise InstallError("%s owned record for %r is malformed" % (meta_path, relpath), 2)
        sha256 = record["sha256"]
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise InstallError("%s owned record for %r has an invalid sha256" % (meta_path, relpath), 2)
        if record["kind"] not in KINDS:
            raise InstallError(
                "%s owned record for %r has an invalid kind %r" % (meta_path, relpath, record["kind"]), 2
            )
        if not isinstance(record["source_digest"], str):
            raise InstallError("%s owned record for %r has a non-string source_digest" % (meta_path, relpath), 2)
        id_value = record["id"]
        if id_value is not None and not isinstance(id_value, str):
            raise InstallError("%s owned record for %r has a non-string id" % (meta_path, relpath), 2)

    created_dirs = obj["created_dirs"]
    if not isinstance(created_dirs, list):
        raise InstallError("%s 'created_dirs' must be a list" % meta_path, 2)
    for entry in created_dirs:
        if not isinstance(entry, str) or not CREATABLE_DIRS_RE.fullmatch(entry):
            raise InstallError("%s created_dirs entry %r is not a creatable directory" % (meta_path, entry), 2)

    return Metadata(
        quoin_version=quoin_version,
        opencode_version=opencode_version,
        profile=profile,
        owned=owned,
        created_dirs=list(created_dirs),
    )


def serialize_metadata(meta: Metadata) -> bytes:
    """Deterministic bytes for ``meta``: sorted keys, no timestamps, no absolute paths."""
    created_dirs = sorted(meta.created_dirs, key=lambda d: (-d.count("/"), d))
    obj = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "quoin_version": meta.quoin_version,
        "opencode_version": meta.opencode_version,
        "profile": meta.profile,
        "owned": meta.owned,
        "created_dirs": created_dirs,
    }
    text = json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    return text.encode("utf-8")


def inspect_path(root, relpath: str) -> PathState:
    """Walk ``relpath`` under ``root`` component by component, never following a symlink."""
    if not isinstance(relpath, str) or not relpath or relpath.startswith("/") or "\\" in relpath:
        raise InstallError("relpath %r is not a relative POSIX path" % (relpath,), 2)
    parts = relpath.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise InstallError("relpath %r has an invalid path component %r" % (relpath, part), 2)

    root = Path(root)
    current = root
    for part in parts[:-1]:
        current = current / part
        st = _lstat_or(current, "missing")
        if st is None:
            return PathState("missing")
        if stat.S_ISLNK(st.st_mode):
            return PathState("symlinked-parent")
        if not stat.S_ISDIR(st.st_mode):
            return PathState("parent-not-dir")

    target = root.joinpath(*parts)
    st = _lstat_or(target, "missing")
    if st is None:
        return PathState("missing")
    if stat.S_ISLNK(st.st_mode):
        return PathState("symlink")
    if not stat.S_ISREG(st.st_mode):
        return PathState("not-regular")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise InstallError("cannot read %s: %s" % (target, exc), 2) from exc
    return PathState("regular", data)


def _fsync_dir_best_effort(dir_path) -> None:
    """Open and fsync `dir_path`, tolerating any failure (directory fsync is
    a durability best-effort, never a correctness requirement: the file
    content itself is already fsynced and renamed into place)."""
    try:
        dir_fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def atomic_write(path, data: bytes, sync_dir: bool = True) -> None:
    """Write ``data`` to ``path`` via a same-directory temp file and ``os.replace``.

    Uses ``O_CREAT | O_EXCL`` (not ``tempfile.mkstemp``) so the process umask
    applies to the written file's mode instead of ``mkstemp``'s fixed 0600.
    On any exception the temp file is unlinked (if it exists) before
    re-raising; nothing is left behind on a failed write.

    ``sync_dir`` fsyncs `path`'s parent directory once this call returns
    (best-effort). Pass ``False`` when the caller will fsync the same
    directory itself after several writes into it, so a directory holding
    many new files is fsynced once instead of once per file.
    """
    path = Path(path)
    tmp_path = path.parent / (".quoin-tmp-%s" % secrets.token_hex(8))
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise

    if sync_dir:
        _fsync_dir_best_effort(path.parent)


# --- planner (no writes) ---

REASON_PARENT_NOT_REAL_DIR = "a parent directory is a symlink or not a directory"
REASON_TARGET_NOT_REGULAR = "target is a symlink or not a regular file"
REASON_UNOWNED_EXISTS = "exists, not owned by Quoin"
REASON_OWNED_MODIFIED = "owned file modified since install"
REASON_STALE_MODIFIED = "stale owned file modified"
REASON_OWNED_MISSING_RECREATED = "owned file missing, recreated"
REASON_ADOPTED = "identical bytes, now owned"

ACTIONS = ("create", "update", "unchanged", "adopt", "delete", "forget", "conflict")


@dataclass
class Action:
    relpath: str
    action: str
    reason: Optional[str] = None
    # Set only for "delete": apply re-checks this against the file's current
    # bytes immediately before unlinking, so a file that changed between plan
    # and apply is left alone instead of removed.
    expected_sha256: Optional[str] = None


@dataclass
class Plan:
    actions: List[Action]
    metadata_action: str
    desired_metadata: bytes
    dirs_to_create: List[str]
    dirs_to_prune: List[str]
    conflicts: List[Action] = field(default_factory=list)
    # Leftover `.quoin-tmp-*` regular files found inside a candidate created
    # directory during planning (an earlier interrupted `atomic_write` that
    # never reached its `os.replace`). `apply_install` sweeps these before
    # pruning; a non-empty list also counts as a change under `--check`.
    temp_leftovers: List[str] = field(default_factory=list)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ancestors(relpath: str):
    """Yield every creatable-family ancestor directory of `relpath`, deepest first."""
    parts = relpath.split("/")
    for i in range(len(parts) - 1, 0, -1):
        candidate = "/".join(parts[:i])
        if CREATABLE_DIRS_RE.fullmatch(candidate):
            yield candidate


def _dir_depth(relpath: str) -> int:
    return relpath.count("/")


def _deepest_first_key(relpath: str):
    return (-_dir_depth(relpath), relpath)


def _shallowest_first_key(relpath: str):
    return (_dir_depth(relpath), relpath)


def _dir_of(relpath: str) -> str:
    return relpath.rsplit("/", 1)[0] if "/" in relpath else ""


def _dir_exists_now(root, relpath: str) -> bool:
    """True when `relpath` and every parent up to `root` are real directories.

    A symlink anywhere in the chain, or any non-directory component, counts
    as not existing (this module never treats a symlinked or shadowed path
    as an existing directory).
    """
    current = Path(root)
    for part in relpath.split("/"):
        current = current / part
        st = _lstat_or(current, "missing")
        if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return False
    return True


def _compute_dirs_to_create(root, create_targets: List[str]) -> Set[str]:
    dirs: Set[str] = set()
    for relpath in create_targets:
        for ancestor in _ancestors(relpath):
            if not _dir_exists_now(root, ancestor):
                dirs.add(ancestor)
    if not _dir_exists_now(root, ".quoin"):
        dirs.add(".quoin")
    return dirs


def _plan_created_dirs(root, rendered: Dict[str, "generate.RenderedFile"], recorded: List[str],
                        actions: List[Action], dirs_to_create: Set[str]):
    """Predict which creatable directories survive apply, and which are pruned.

    A candidate directory is recorded in the desired metadata's
    `created_dirs` when it is already recorded, is one this apply creates,
    or exists now unrecorded and holds nothing but Quoin's own files and
    Quoin directories (the case that lets a rerun after an interrupted
    first install pick up the directories that run made). A recorded
    candidate whose predicted post-apply contents are empty is pruned
    instead of re-recorded.

    A directory entry whose basename matches `TEMP_NAME_RE` is an earlier
    interrupted write's debris, never a user's or OpenCode's own file. It is
    collected into the returned `temp_leftovers` (sorted) and left out of
    `post` entirely — it never counts against a directory being emptied
    (prune-eligible) or against a directory holding only Quoin's own files
    (created-eligible); `apply_install` sweeps it before those directories
    are pruned.
    """
    cands: Set[str] = set(recorded) | {".quoin"}
    for relpath in rendered:
        cands.update(_ancestors(relpath))

    gone = {a.relpath for a in actions if a.action in ("delete", "forget")}
    create_targets = {a.relpath for a in actions if a.action == "create"}
    adds: Set[str] = create_targets | set(dirs_to_create) | {METADATA_RELPATH}

    created: Set[str] = set()
    prune: List[str] = []
    temp_leftovers: List[str] = []
    for d in sorted(cands, key=_deepest_first_key):
        exists_now = _dir_exists_now(root, d)
        if not exists_now and d not in dirs_to_create:
            continue
        entries: List[str] = []
        if exists_now:
            try:
                entries = os.listdir(str(Path(root) / d))
            except OSError:
                entries = []
        leftovers_here = sorted(e for e in entries if TEMP_NAME_RE.fullmatch(e))
        temp_leftovers.extend("%s/%s" % (d, e) for e in leftovers_here)
        post: Set[str] = {"%s/%s" % (d, e) for e in entries if not TEMP_NAME_RE.fullmatch(e)}
        post -= gone
        post -= set(prune)
        post |= {a for a in adds if _dir_of(a) == d}
        if d in recorded and not post:
            prune.append(d)
            continue
        only_quoin = all((x in rendered) or (x == METADATA_RELPATH) or (x in created) for x in post)
        if d in recorded or d in dirs_to_create or (exists_now and only_quoin):
            created.add(d)
    return created, prune, sorted(temp_leftovers)


def plan_install(root, rendered: Dict[str, "generate.RenderedFile"], meta: Optional[Metadata],
                  quoin_version: str, opencode_version: str, profile: Optional[str]) -> Plan:
    """Compute the full set of file and metadata actions. Never writes anything."""
    owned = meta.owned if meta else {}
    actions: List[Action] = []
    for relpath in sorted(set(rendered) | set(owned)):
        if not OWNED_PATH_RE.fullmatch(relpath):
            raise InstallError("rendered path %r is outside the generated families" % (relpath,), 2)
        st = inspect_path(root, relpath)
        if st.state in ("symlinked-parent", "parent-not-dir"):
            actions.append(Action(relpath, "conflict", REASON_PARENT_NOT_REAL_DIR))
            continue
        if st.state in ("symlink", "not-regular"):
            actions.append(Action(relpath, "conflict", REASON_TARGET_NOT_REGULAR))
            continue
        cur = st.data if st.state == "regular" else None
        if relpath in rendered:
            want = rendered[relpath].content
            if cur is None:
                reason = REASON_OWNED_MISSING_RECREATED if relpath in owned else None
                actions.append(Action(relpath, "create", reason))
            elif cur == want:
                if relpath in owned:
                    actions.append(Action(relpath, "unchanged"))
                else:
                    actions.append(Action(relpath, "adopt", REASON_ADOPTED))
            elif relpath not in owned:
                actions.append(Action(relpath, "conflict", REASON_UNOWNED_EXISTS))
            elif _sha256_hex(cur) != owned[relpath]["sha256"]:
                actions.append(Action(relpath, "conflict", REASON_OWNED_MODIFIED))
            else:
                actions.append(Action(relpath, "update"))
        else:
            if cur is None:
                actions.append(Action(relpath, "forget"))
            elif _sha256_hex(cur) == owned[relpath]["sha256"]:
                actions.append(Action(relpath, "delete", expected_sha256=owned[relpath]["sha256"]))
            else:
                actions.append(Action(relpath, "conflict", REASON_STALE_MODIFIED))

    conflicts = [a for a in actions if a.action == "conflict"]

    create_targets = [a.relpath for a in actions if a.action == "create"]
    dirs_to_create = _compute_dirs_to_create(root, create_targets)

    new_owned = {
        relpath: {
            "sha256": _sha256_hex(rendered[relpath].content),
            "source_digest": rendered[relpath].source_digest,
            "kind": rendered[relpath].kind,
            "id": rendered[relpath].source_id,
        }
        for relpath in rendered
    }

    recorded = list(meta.created_dirs) if meta else []
    created_dirs, dirs_to_prune, temp_leftovers = _plan_created_dirs(root, rendered, recorded, actions, dirs_to_create)

    resolved_profile = profile if profile is not None else (meta.profile if meta else None)

    desired_meta_obj = Metadata(
        quoin_version=quoin_version,
        opencode_version=opencode_version,
        profile=resolved_profile,
        owned=new_owned,
        created_dirs=list(created_dirs),
    )
    desired_metadata = serialize_metadata(desired_meta_obj)

    meta_state = inspect_path(root, METADATA_RELPATH)
    current_metadata_bytes = meta_state.data if meta_state.state == "regular" else None
    metadata_action = "unchanged" if desired_metadata == current_metadata_bytes else "update"

    return Plan(
        actions=sorted(actions, key=lambda a: a.relpath),
        metadata_action=metadata_action,
        desired_metadata=desired_metadata,
        dirs_to_create=sorted(dirs_to_create, key=_shallowest_first_key),
        dirs_to_prune=sorted(dirs_to_prune, key=_deepest_first_key),
        conflicts=conflicts,
        temp_leftovers=temp_leftovers,
    )


def format_plan(plan: Plan) -> List[str]:
    """One deterministic line per file action, sweep lines, a metadata line, then a summary line."""
    lines: List[str] = []
    counts: Dict[str, int] = {}
    for action in plan.actions:
        counts[action.action] = counts.get(action.action, 0) + 1
        line = "%-9s %s" % (action.action, action.relpath)
        if action.reason and action.action in ("conflict", "adopt", "create"):
            line += " (%s)" % action.reason
        lines.append(line)
    for relpath in plan.temp_leftovers:
        counts["sweep"] = counts.get("sweep", 0) + 1
        lines.append("%-9s %s" % ("sweep", relpath))
    lines.append("%-9s %s" % ("metadata", plan.metadata_action))
    summary = ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))
    lines.append("summary: %s (metadata %s)" % (summary, plan.metadata_action))
    return lines


# --- apply, check and run_install ---


def apply_install(root, plan: Plan, rendered: Dict[str, "generate.RenderedFile"]) -> None:
    """Apply `plan` to `root`. Never called when `plan.conflicts` is non-empty.

    Order: file writes (each fsynced itself, directory fsync deferred), then
    deletes, then the temp-debris sweep, then the planner's predicted
    directory prunes, then the deferred directory fsyncs, then `.quoin` and
    the metadata (written last, and only when `metadata_action == "update"`,
    with its own directory fsync so the metadata write is never lost even if
    it is the only write this apply makes). An `OSError` partway through
    leaves a tree the next `run_install` call converges from (unowned
    survivors are adopted, owned-but-missing files are forgotten, and the
    created-dir rule re-records directories this run already made).
    """
    root = Path(root)
    for d in sorted((d for d in plan.dirs_to_create if d != ".quoin"), key=_shallowest_first_key):
        target = root / d
        if not target.exists():
            os.mkdir(str(target))

    touched_dirs: Set[str] = set()
    for action in sorted((a for a in plan.actions if a.action in ("create", "update")), key=lambda a: a.relpath):
        target = root / action.relpath
        atomic_write(target, rendered[action.relpath].content, sync_dir=False)
        touched_dirs.add(str(target.parent))

    for action in sorted((a for a in plan.actions if a.action == "delete"), key=lambda a: a.relpath):
        target = root / action.relpath
        st = _lstat_or(target, "missing")
        if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            continue
        if action.expected_sha256 is not None:
            try:
                current_bytes = target.read_bytes()
            except OSError:
                continue
            if _sha256_hex(current_bytes) != action.expected_sha256:
                continue
        os.unlink(str(target))

    # Sweep Quoin's own leftover temp files before computing prunes, so an
    # earlier interrupted write's debris never blocks its directory from
    # being pruned. Only a regular file is unlinked; a symlink or directory
    # sharing the temp-name shape is left alone and still blocks its
    # directory (this module never removes anything it cannot positively
    # identify as its own).
    #
    # Concurrent-install edge (not handled): a sweep can remove another
    # process's in-flight temp file before that process's `os.replace` runs.
    # That process then takes the "interrupted, re-run" exit below. Two
    # installs into one project at once are not supported.
    for relpath in plan.temp_leftovers:
        target = root / relpath
        st = _lstat_or(target, "missing")
        if st is not None and stat.S_ISREG(st.st_mode):
            os.unlink(str(target))

    for d in plan.dirs_to_prune:
        try:
            os.rmdir(str(root / d))
        except OSError:
            pass

    for dir_path in sorted(touched_dirs):
        _fsync_dir_best_effort(dir_path)

    if plan.metadata_action == "update":
        if ".quoin" in plan.dirs_to_create:
            quoin_dir = root / ".quoin"
            if not quoin_dir.exists():
                os.mkdir(str(quoin_dir))
        atomic_write(root / METADATA_RELPATH, plan.desired_metadata)


def _remediation_for(reason: Optional[str], is_config_jsonc: bool) -> str:
    if reason == REASON_UNOWNED_EXISTS:
        if is_config_jsonc:
            return (
                "an unowned .opencode/opencode.jsonc: move those settings into "
                ".opencode/opencode.json or the project opencode.json, which OpenCode "
                "merges the same way, then re-run install."
            )
        return "an unowned file: move or rename it, then re-run install."
    if reason in (REASON_OWNED_MODIFIED, REASON_STALE_MODIFIED):
        return (
            "a modified owned file: keep a copy, delete it, and re-run install (it "
            "recreates the file), or keep it and run `quoin opencode uninstall`."
        )
    if reason == REASON_TARGET_NOT_REGULAR:
        return "a symlink or non-regular target: replace it with a regular file or remove it."
    if reason == REASON_PARENT_NOT_REAL_DIR:
        return (
            "a parent directory that is a symlink or not a directory: make it a real "
            "directory, or install into a different project root."
        )
    return "see the adapter README for how to resolve this conflict."  # pragma: no cover - exhaustive above


def _print_conflicts_and_remediation(plan: Plan, err) -> None:
    for action in plan.conflicts:
        print("%-9s %s (%s)" % (action.action, action.relpath, action.reason), file=err)
    printed: Set[tuple] = set()
    for action in plan.conflicts:
        is_config_jsonc = action.relpath == generate.CONFIG_PATH
        key = (action.reason, is_config_jsonc)
        if key in printed:
            continue
        printed.add(key)
        print("remediation: %s" % _remediation_for(action.reason, is_config_jsonc), file=err)


def run_install(project_root, source_dir, profile, check, out, err,
                 rendered: Optional[Dict[str, "generate.RenderedFile"]] = None,
                 quoin_version: Optional[str] = None, opencode_version: Optional[str] = None) -> int:
    root = Path(project_root)
    if not root.is_dir():
        print("opencode install: project root %s is not a directory" % root, file=err)
        return 2

    if profile is not None and not PROFILE_RE.fullmatch(profile):
        print("opencode install: profile %r is not a valid profile label" % (profile,), file=err)
        return 2

    if quoin_version is None:
        from quoin import __about__

        quoin_version = __about__.__version__

    try:
        if opencode_version is None:
            opencode_version = manifest.read_pinned_version(source_dir)
        if rendered is None:
            rendered = generate.render_source_dir(source_dir)
    except (generate.GenerationError, manifest.ManifestLoadError) as exc:
        print("opencode install: generation failed: %s" % exc, file=err)
        return 2

    try:
        meta = load_metadata(root)
    except InstallError as exc:
        print("opencode install: %s" % exc, file=err)
        return exc.exit_code

    try:
        plan = plan_install(root, rendered, meta, quoin_version, opencode_version, profile)
    except InstallError as exc:
        print("opencode install: %s" % exc, file=err)
        return exc.exit_code

    if plan.conflicts:
        _print_conflicts_and_remediation(plan, err)
        return 3

    if check:
        for line in format_plan(plan):
            print(line, file=out)
        all_unchanged = (
            plan.metadata_action == "unchanged"
            and all(a.action == "unchanged" for a in plan.actions)
            and not plan.temp_leftovers
        )
        return 0 if all_unchanged else 1

    try:
        apply_install(root, plan, rendered)
    except OSError as exc:
        where = exc.filename or "?"
        problem = errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno else str(exc)
        print(
            "opencode install: interrupted: %s on %s; re-run install to finish" % (problem, where),
            file=err,
        )
        return 2

    for line in format_plan(plan):
        print(line, file=out)
    return 0


# --- uninstall ---

REASON_MODIFIED_SINCE_INSTALL = "modified since install"
REASON_LEFT_IN_PLACE = "not a regular file, left in place"

UNINSTALL_ACTIONS = ("delete", "keep", "forget")


@dataclass
class UninstallAction:
    relpath: str
    action: str
    reason: Optional[str] = None
    # Set only for "delete": run_uninstall re-checks this against the file's
    # current bytes immediately before unlinking.
    expected_sha256: Optional[str] = None


@dataclass
class UninstallPlan:
    actions: List[UninstallAction]
    dirs_to_prune: List[str]
    metadata_action: str  # "delete" or "rewrite"
    desired_metadata: Optional[bytes]
    kept: bool
    # Leftover `.quoin-tmp-*` regular files found inside a recorded
    # `created_dirs` entry. `run_uninstall` sweeps these the same way
    # `apply_install` does, before computing prunes.
    temp_leftovers: List[str] = field(default_factory=list)


def plan_uninstall(root, meta: Metadata) -> UninstallPlan:
    """Compute what uninstall would do. Never writes anything.

    Per owned path (sorted): missing files are forgotten, an unmodified
    regular file is deleted, and anything else (modified, a symlink, a
    non-regular target, or a symlinked/non-directory parent) is kept in
    place. Directory pruning candidates are the recorded `created_dirs`
    (excluding `.quoin`, which `run_uninstall` handles alongside the
    metadata file itself): a candidate prunes when its predicted post-apply
    contents, after removing the deletes and any already-pruned child
    directories, are empty.
    """
    actions: List[UninstallAction] = []
    kept_owned: Dict[str, dict] = {}
    for relpath in sorted(meta.owned):
        record = meta.owned[relpath]
        st = inspect_path(root, relpath)
        if st.state == "missing":
            actions.append(UninstallAction(relpath, "forget"))
        elif st.state == "regular":
            if _sha256_hex(st.data) == record["sha256"]:
                actions.append(UninstallAction(relpath, "delete", expected_sha256=record["sha256"]))
            else:
                actions.append(UninstallAction(relpath, "keep", REASON_MODIFIED_SINCE_INSTALL))
                kept_owned[relpath] = record
        else:  # symlink, not-regular, symlinked-parent, parent-not-dir
            actions.append(UninstallAction(relpath, "keep", REASON_LEFT_IN_PLACE))
            kept_owned[relpath] = record

    kept = bool(kept_owned)
    deleted = {a.relpath for a in actions if a.action == "delete"}

    dirs_to_prune: List[str] = []
    temp_leftovers: List[str] = []
    for d in sorted(meta.created_dirs, key=_deepest_first_key):
        if d == ".quoin" or not _dir_exists_now(root, d):
            continue
        try:
            entries = os.listdir(str(Path(root) / d))
        except OSError:
            continue
        leftovers_here = sorted(e for e in entries if TEMP_NAME_RE.fullmatch(e))
        temp_leftovers.extend("%s/%s" % (d, e) for e in leftovers_here)
        post = {"%s/%s" % (d, e) for e in entries if not TEMP_NAME_RE.fullmatch(e)}
        post -= deleted
        post -= set(dirs_to_prune)
        if not post:
            dirs_to_prune.append(d)
    temp_leftovers = sorted(temp_leftovers)

    if kept:
        metadata_action = "rewrite"
        new_created_dirs = [d for d in meta.created_dirs if d not in dirs_to_prune]
        desired_metadata: Optional[bytes] = serialize_metadata(
            Metadata(
                quoin_version=meta.quoin_version,
                opencode_version=meta.opencode_version,
                profile=meta.profile,
                owned=kept_owned,
                created_dirs=new_created_dirs,
            )
        )
    else:
        metadata_action = "delete"
        desired_metadata = None

    return UninstallPlan(
        actions=sorted(actions, key=lambda a: a.relpath),
        dirs_to_prune=dirs_to_prune,
        metadata_action=metadata_action,
        desired_metadata=desired_metadata,
        kept=kept,
        temp_leftovers=temp_leftovers,
    )


def _print_uninstall_plan(plan: UninstallPlan, out) -> None:
    counts: Dict[str, int] = {}
    for action in plan.actions:
        counts[action.action] = counts.get(action.action, 0) + 1
        line = "%-9s %s" % (action.action, action.relpath)
        if action.reason:
            line += " (%s)" % action.reason
        print(line, file=out)
    for relpath in plan.temp_leftovers:
        counts["sweep"] = counts.get("sweep", 0) + 1
        print("%-9s %s" % ("sweep", relpath), file=out)
    print("%-9s %s" % ("metadata", plan.metadata_action), file=out)
    summary = ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()))
    print("summary: %s (metadata %s)" % (summary, plan.metadata_action), file=out)


def run_uninstall(project_root, dry_run, out, err) -> int:
    """Remove everything Quoin owns under `project_root`.

    A path left in place (modified since install, or replaced by anything
    other than a regular file) is never touched or followed; uninstall
    returns 4 and the metadata is rewritten to record only what remains.
    `--dry-run` computes and prints the same plan and writes nothing,
    returning the code the real run would return.
    """
    root = Path(project_root)
    if not root.is_dir():
        print("opencode uninstall: project root %s is not a directory" % root, file=err)
        return 2

    try:
        meta = load_metadata(root)
    except InstallError as exc:
        print("opencode uninstall: %s" % exc, file=err)
        return exc.exit_code

    if meta is None:
        print("opencode uninstall: nothing installed", file=out)
        return 0

    try:
        plan = plan_uninstall(root, meta)
    except InstallError as exc:
        print("opencode uninstall: %s" % exc, file=err)
        return exc.exit_code

    _print_uninstall_plan(plan, out)

    if not dry_run:
        try:
            for action in plan.actions:
                if action.action != "delete":
                    continue
                target = root / action.relpath
                st = _lstat_or(target, "missing")
                if st is None or stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    continue
                try:
                    current_bytes = target.read_bytes()
                except OSError:
                    continue
                if _sha256_hex(current_bytes) != action.expected_sha256:
                    continue
                os.unlink(str(target))

            # Sweep leftover Quoin temp files before pruning, same rule and
            # same concurrent-install caveat as `apply_install`.
            for relpath in plan.temp_leftovers:
                target = root / relpath
                st = _lstat_or(target, "missing")
                if st is not None and stat.S_ISREG(st.st_mode):
                    os.unlink(str(target))

            for d in plan.dirs_to_prune:
                try:
                    os.rmdir(str(root / d))
                except OSError:
                    pass

            if plan.metadata_action == "delete":
                try:
                    os.unlink(str(root / METADATA_RELPATH))
                except OSError:
                    pass
                if ".quoin" in meta.created_dirs:
                    try:
                        os.rmdir(str(root / ".quoin"))
                    except OSError:
                        pass
            else:
                atomic_write(root / METADATA_RELPATH, plan.desired_metadata)
        except OSError as exc:
            where = exc.filename or "?"
            problem = errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno else str(exc)
            print(
                "opencode uninstall: interrupted: %s on %s; re-run uninstall to finish" % (problem, where),
                file=err,
            )
            return 2

    return 4 if plan.kept else 0


class CommandAgentError(Exception):
    """A phase command does not select an installed primary agent."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def command_agent(root, command_rel: str, metadata: "Metadata") -> str:
    """The primary agent the installed command ``command_rel`` selects.

    Raises ``CommandAgentError`` when the command or the agent cannot be read,
    names no agent, names an agent outside the install, or names one that
    cannot run as a primary agent. Shared by the driver's prepare step and
    the doctor so both judge an install the same way."""
    root = Path(root)

    def parsed(rel: str) -> Dict[str, object]:
        got = jsonio.read_regular_bytes(root / rel, max_bytes=MAX_OWNED_BYTES)
        if got is None:
            raise CommandAgentError("%s cannot be read" % rel)
        try:
            fields, _ = frontmatter.parse(got[0].decode("utf-8"))
        except (UnicodeDecodeError, frontmatter.FrontmatterError):
            raise CommandAgentError("%s has unreadable frontmatter" % rel) from None
        return fields

    agent = parsed(command_rel).get("agent")
    if not isinstance(agent, str) or not _AGENT_NAME_RE.match(agent):
        raise CommandAgentError("the phase command does not name an agent")
    agent_rel = ".opencode/agents/%s.md" % agent
    if agent_rel not in metadata.owned:
        raise CommandAgentError("the agent %s is not part of the installed set" % agent)
    if parsed(agent_rel).get("mode") not in ("primary", "all"):
        raise CommandAgentError("the agent %s cannot run as a primary agent" % agent)
    return agent
