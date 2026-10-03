"""Portable continuation record for the OpenCode runtime.

The record (`.workflow_artifacts/memory/continuation/TASK.json`) is a
runtime-neutral summary of where a task stands: completed and pending phases,
artifact hashes, repository state and the policy ceiling the work ran under.
The schema, validation and atomic store live in the core script
`continuation_handoff.py`; this module builds records from OpenCode state
(workflow state, run records, the tree), evaluates the policy ceiling from the
compiled configuration, applies the refusal rules when a continuation is
loaded, and computes the advice `quoin opencode handoff show` prints.

Nothing here reads a transcript, and agent-supplied text lands only in the
record's decisions and notes after secret masking.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import errors, evidence, gate, runstore

ORIGIN_RUNTIME = "opencode"
UNAVAILABLE_TELEMETRY = ("child-session-usage", "effective-effort", "parent-cost-scope")
SUBAGENT_DEPTH = 1
NETWORK = "profile-default"
CONTINUATION_PARTS = (".workflow_artifacts", "memory", "continuation")
PROVENANCE_SOURCES = ("workflow-state", "run-records", "artifacts")

# Copied from the driver's native-session rule (the driver is never imported
# here); a test pins the two patterns equal.
NATIVE_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

MAX_LIST = 100
MAX_REASONS = 64
LISTING_CAP = 2000
_TOKEN_BAD_RE = re.compile(r"[^A-Za-z0-9._:/@+_-]")
_CHECKPOINT_RE = r"^\d{4}-\d{2}-\d{2}T\d{4}-%s\.md$"
_SESSION_RE = r"^\d{4}-\d{2}-\d{2}-%s-(?:codex|orchestrator)\.md$"
_ARTIFACT_RE = re.compile(r"^(critic-response|review)-\d+\.md$")
_ARTIFACT_TYPES = {
    "architecture.md": "architecture",
    "current-plan.md": "current-plan",
    "spec.md": "spec",
}
_SKIP_PARENTS = ("memory", "cache", "finalized")
_PHASE_ORDER = ("discover", "architect", "plan", "implement", "review")


class HandoffRefused(Exception):
    """A request was refused; `code` is a stable identifier."""

    def __init__(self, code: str, message: str = "", reasons: Sequence[str] = ()) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.reasons: Tuple[str, ...] = tuple(reasons)


# ---------------------------------------------------------------------------
# core script, locations
# ---------------------------------------------------------------------------


def core(source_dir):
    try:
        return gate.load_core(source_dir, "continuation_handoff")
    except gate.GateRefused:
        raise HandoffRefused("source-unavailable", "the continuation core script cannot be loaded") from None


def _task(task: str) -> str:
    try:
        return runstore.check_task_name(task)
    except runstore.RunStoreError:
        raise HandoffRefused("invalid-task-name", "the task name is not valid") from None


def continuation_dir(project_root, *, create: bool = False) -> Path:
    """The continuation directory, every component checked with `lstat`. A
    symlink or a non-directory refuses; with `create`, missing components are
    made private (mode 700) and a foreign owner refuses."""
    current = Path(project_root)
    for part in CONTINUATION_PARTS:
        current = current / part
        try:
            info = os.lstat(str(current))
        except FileNotFoundError:
            if not create:
                return Path(project_root).joinpath(*CONTINUATION_PARTS)
            try:
                os.mkdir(str(current), 0o700)
            except FileExistsError:
                pass
            except OSError:
                raise HandoffRefused("unsafe-path", "the continuation directory cannot be created") from None
            info = os.lstat(str(current))
        except OSError:
            raise HandoffRefused("unsafe-path", "the continuation directory cannot be inspected") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise HandoffRefused("unsafe-path", "the continuation path is not a plain directory")
    info = os.lstat(str(current))
    if info.st_uid != os.getuid():
        raise HandoffRefused("unsafe-path", "the continuation directory belongs to another user")
    if create and info.st_mode & 0o077:
        os.chmod(str(current), 0o700)
    return current


def record_path(project_root, task: str) -> Path:
    return continuation_dir(project_root) / (_task(task) + ".json")


def _rel(project_root, path) -> str:
    return os.path.relpath(str(path), str(project_root)).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def scope_from_compiled(
    profile: str, classification: str, document: Mapping[str, Any], limit_values: Mapping[str, Any],
) -> Dict[str, Any]:
    """The policy ceiling of a compiled native document. `profile` and
    `classification` are the evaluated values, never a raw flag."""
    from . import merge  # noqa: PLC0415

    limits: Dict[str, Any] = {name: limit_values.get(name) for name in merge.LIMIT_NAMES}
    limits["subagent_depth"] = SUBAGENT_DEPTH
    allowlist = sorted(
        "%s/%s" % (pid, model)
        for pid, entry in document["provider"].items()
        for model in entry["whitelist"]
    )
    return {
        "profile": profile,
        "classification": classification,
        "policy_ceiling": {
            "enabled_providers": sorted(document["enabled_providers"]),
            "provider_allowlist": allowlist,
            "role_models": {agent: entry["model"] for agent, entry in document["agent"].items()},
            "limits": limits,
            "network": NETWORK,
        },
    }


def scope_for_profile(
    project_root, profile: Optional[str], *, env: Mapping[str, str], home, clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    """Evaluate `profile` (None selects the project default, as a run without
    `--profile` does) and build the same compiled document the driver launches
    with."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from . import compiler, merge, paths, roles  # noqa: PLC0415
    from .errors import ConfigErrors  # noqa: PLC0415

    try:
        ev = compiler.evaluate(
            project_root=project_root, profile=profile, env=env, home=home,
            now=datetime.fromtimestamp(clock(), timezone.utc),
        )
        document = compiler.build(ev).document
    except (
        ConfigErrors, paths.AdapterDataMissing, roles.AllowUnqualifiedRefused,
        compiler.CompileBlocked, compiler.CompileGateError,
    ) as exc:
        raise HandoffRefused(
            "scope-unavailable", "the policy scope cannot be evaluated (%s)" % type(exc).__name__,
        ) from None
    limit_values: Dict[str, Any] = {}
    for name in merge.LIMIT_NAMES:
        value = ev.effective.values.get("limits." + name)
        if value is not None and isinstance(value.value, int) and not isinstance(value.value, bool):
            limit_values[name] = value.value
    return scope_from_compiled(ev.profile, ev.effective.classification, document, limit_values)


def _store_or_none(project_root) -> Optional[Path]:
    try:
        return runstore.inspect_store(project_root)
    except (runstore.RunStoreError, OSError):
        raise HandoffRefused("store-unreadable", "the run store cannot be read") from None


def _load_state(directory: Optional[Path], task: str) -> Optional[Dict[str, Any]]:
    if directory is None:
        return None
    try:
        return runstore.load_workflow_state(directory, task)
    except (runstore.RunStoreError, ValueError, OSError) as exc:
        raise HandoffRefused(
            "state-invalid", "the workflow state cannot be used", (getattr(exc, "code", type(exc).__name__),),
        ) from None


def _pointer_record(directory: Optional[Path], task: str) -> Optional[Dict[str, Any]]:
    if directory is None:
        return None
    try:
        pointer = runstore.load_pointer(directory, task)
        if pointer is None:
            return None
        return runstore.load_record(directory, pointer["run_id"])
    except (runstore.RunStoreError, ValueError, OSError, KeyError, TypeError):
        return None


def resolve_profile(
    project_root, task: str, requested: Optional[str], *, evaluate: Callable[[Optional[str]], Dict[str, Any]],
) -> Tuple[Dict[str, Any], str]:
    """The recorded scope and where its profile came from. The recorded profile
    is the workflow state's, then the latest run's effective profile, then that
    run's requested one (a run with neither counts as the project default). A
    `requested` profile must evaluate to the same effective profile."""
    _task(task)
    directory = _store_or_none(project_root)
    state = _load_state(directory, task)
    found = False
    recorded: Optional[str] = None
    source = "operator-flag"
    settings = (state or {}).get("settings") or {}
    if isinstance(settings.get("profile"), str):
        found, recorded, source = True, settings["profile"], "workflow-state"
    else:
        run = _pointer_record(directory, task)
        if run is not None:
            prepared = run.get("prepared") or {}
            request = run.get("request") or {}
            if isinstance(prepared.get("profile"), str):
                recorded = prepared["profile"]
            elif isinstance(request.get("profile"), str):
                recorded = request["profile"]
            found, source = True, "run-record"
    if found:
        scope = evaluate(recorded)
        if requested is not None:
            other = evaluate(requested)
            if other["profile"] != scope["profile"]:
                raise HandoffRefused(
                    "profile-mismatch", "--profile names a different profile than the one recorded for this task",
                )
        return scope, source
    if requested is None:
        raise HandoffRefused("profile-unknown", "no profile is recorded for this task; pass --profile")
    return evaluate(requested), "operator-flag"


# ---------------------------------------------------------------------------
# sequence
# ---------------------------------------------------------------------------


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(str(path)).st_mode)
    except OSError:
        return False


def listed_stages(project_root, task: str, source_dir) -> Optional[List[int]]:
    """Stage numbers in the architecture's stage decomposition; None when the
    task has no architecture or no such section."""
    try:
        arch = gate.task_root(project_root, task) / "architecture.md"
    except (gate.PathUnresolved, gate.GateRefused, ValueError, OSError):
        return None
    if not _regular(arch):
        return None
    text = gate._read_text(arch, gate.MAX_TEXT_BYTES)
    if text is None:
        return None
    resolver = gate.load_core(source_dir, "path_resolve")
    match = resolver.SECTION_RE.search(text)
    if not match:
        return None
    following = resolver.NEXT_H2_RE.search(text, match.end())
    body = text[match.end(): following.start() if following else len(text)]
    return sorted({int(m.group(1)) for m in resolver.ROW_RE.finditer(body)})


def _live_entries(state: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Non-superseded gated entries, one per (phase, stage), in stored order."""
    latest: Dict[Tuple[str, Optional[int]], Mapping[str, Any]] = {}
    for entry in (state or {}).get("entries") or []:
        if entry.get("superseded") or entry.get("phase") not in runstore.GATED_PHASES:
            continue
        latest[(entry["phase"], entry.get("stage"))] = entry
    return list(latest.values())


def sequence(project_root, task: str, state: Optional[Mapping[str, Any]], source_dir) -> List[Tuple[str, Optional[int]]]:
    items: List[Tuple[str, Optional[int]]] = []
    try:
        has_arch = _regular(gate.task_root(project_root, task) / "architecture.md")
    except (gate.PathUnresolved, gate.GateRefused, ValueError, OSError):
        has_arch = False
    has_entry = any(e.get("phase") == "architect" for e in (state or {}).get("entries") or [])
    if has_arch or has_entry:
        items.append(("architect", None))
    stages = listed_stages(project_root, task, source_dir)
    for stage in ([None] if stages is None else stages):
        for phase in ("plan", "implement", "review"):
            items.append((phase, stage))
    return items


# ---------------------------------------------------------------------------
# finalized and legacy detection
# ---------------------------------------------------------------------------


def finalized_location(project_root, task: str) -> Optional[str]:
    """Project-relative path of an archived copy of the task, else None.
    Existence alone counts; nothing is read and no symlink is followed."""
    base = Path(project_root) / ".workflow_artifacts"
    direct = base / "finalized" / task
    if os.path.lexists(str(direct)):
        return _rel(project_root, direct)
    try:
        listing = []
        with os.scandir(str(base)) as it:
            for entry in it:
                listing.append(entry)
                if len(listing) >= LISTING_CAP:
                    break
        for entry in sorted(listing, key=lambda e: e.name):
            if entry.name in _SKIP_PARENTS:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            nested = base / entry.name / "finalized" / task
            if os.path.lexists(str(nested)):
                return _rel(project_root, nested)
    except OSError:
        return None
    return None


def _names(directory: Path) -> List[str]:
    try:
        with os.scandir(str(directory)) as it:
            return sorted(entry.name for entry in it)
    except OSError:
        return []


def legacy_markers(project_root, task: str) -> List[str]:
    """Names of older-format continuation files for the task. Directory
    listings only; no file is ever opened."""
    memory = Path(project_root) / ".workflow_artifacts" / "memory"
    escaped = re.escape(task)
    found: List[str] = []
    patterns = (
        ("checkpoints", re.compile(_CHECKPOINT_RE % escaped)),
        ("sessions", re.compile(_SESSION_RE % escaped)),
    )
    for sub, pattern in patterns:
        for name in _names(memory / sub):
            if pattern.match(name):
                found.append(_rel(project_root, memory / sub / name))
    wanted = {"run-state-%s.json" % task, "run-notes-%s.md" % task, "run-notes-%s.md.1" % task}
    for name in _names(memory):
        if name in wanted:
            found.append(_rel(project_root, memory / name))
    return found


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    return errors.SECRET_SHAPE_RE.sub("<redacted>", text)


def _token(value: Any) -> str:
    text = _TOKEN_BAD_RE.sub("-", str(value))[:256].lstrip("-._:/@+")
    return text or "reason"


def _clean_reasons(values: Any) -> List[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [_token(v) for v in values if isinstance(v, str)][:MAX_REASONS]


def _artifact_type(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    if rel in evidence.DISCOVER_FILES:
        return "discover"
    if name in _ARTIFACT_TYPES:
        return _ARTIFACT_TYPES[name]
    match = _ARTIFACT_RE.match(name)
    if match:
        return match.group(1)
    return "other"


def _gate_of(entry: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    value = entry.get("gate")
    return value if isinstance(value, Mapping) else None


def _passed(entry: Mapping[str, Any]) -> bool:
    value = _gate_of(entry)
    return value is not None and value.get("verdict") == "PASS"


def _completed_pairs(state: Optional[Mapping[str, Any]]) -> set:
    return {(e["phase"], e.get("stage")) for e in _live_entries(state) if _passed(e)}


def _entry_order(position: Mapping[Tuple[str, Optional[int]], int]):
    def key(entry: Mapping[str, Any]):
        item = (entry["phase"], entry.get("stage"))
        if item in position:
            return (0, position[item], 0, 0, str(entry.get("recorded_at", "")))
        stage = entry.get("stage")
        return (
            1, 0 if stage is None else stage, 0 if stage is None else 1,
            _PHASE_ORDER.index(entry["phase"]) if entry["phase"] in _PHASE_ORDER else len(_PHASE_ORDER),
            str(entry.get("recorded_at", "")),
        )

    return key


def _last_run_id(core_mod, entry: Mapping[str, Any]) -> Optional[str]:
    runs = entry.get("runs") or []
    if runs and isinstance(runs[-1], str) and core_mod.TOKEN_RE.fullmatch(runs[-1]):
        return runs[-1]
    return None


def _native(directory: Optional[Path], task: str, pending: Sequence[Tuple[str, Optional[int]]]):
    run = _pointer_record(directory, task)
    if run is None or directory is None:
        return None
    if run.get("state") not in ("interrupted", "running"):
        return None
    request = run.get("request") or {}
    try:
        item = (runstore.entry_phase_for_run(request.get("phase")), runstore.normalize_stage(request.get("stage")))
    except ValueError:
        return None
    if item not in pending:
        return None
    session = None
    try:
        checkpoint = runstore.load_checkpoint(directory, run["run_id"])
    except (runstore.RunStoreError, ValueError, OSError, KeyError):
        checkpoint = None
    if checkpoint is not None:
        value = checkpoint.get("native_session_id")
        if isinstance(value, str) and NATIVE_SESSION_RE.fullmatch(value):
            session = value
    status = "interrupted" if run["state"] == "interrupted" else "in_progress"
    return item, status, {"run_id": run["run_id"], "session_id": session}


def _artifacts(project_root, task: str, state, mod) -> List[Dict[str, str]]:
    has_discover = any(e.get("phase") == "discover" for e in (state or {}).get("entries") or [])
    refs = evidence.DISCOVER_FILES if has_discover else ()

    def excluded(rel: str) -> bool:
        return not (evidence.in_scope(rel, task) or rel in evidence.DISCOVER_FILES)

    try:
        hashes = runstore.hash_inputs(project_root, task, refs, exclude=excluded)
    except (runstore.RunStoreError, OSError):
        raise HandoffRefused("artifacts-incomplete", "the task files cannot be hashed") from None
    if "<truncated>" in hashes or any(not isinstance(v, str) for v in hashes.values()):
        raise HandoffRefused("artifacts-incomplete", "the task files could not all be hashed")
    bad = [rel for rel in hashes if not mod.path_ok(rel)]
    if bad:
        raise HandoffRefused(
            "artifacts-incomplete", "a task file has a name the record cannot hold; rename it",
            [repr(rel) for rel in bad[:20]],
        )
    return [{"path": rel, "sha256": sha, "type": _artifact_type(rel)} for rel, sha in hashes.items()]


def _repo_revisions(project_root, mod, runner, bytes_runner) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in runstore.repo_revisions(project_root, source=True, runner=runner, bytes_runner=bytes_runner):
        head = item.get("head")
        digest = item.get("source_digest")
        dirty = item.get("source_dirty")
        entry: Dict[str, Any] = {
            "path": item["path"],
            "head": head if isinstance(head, str) and mod.HEAD_RE.fullmatch(head) else None,
            "source_dirty": dirty if isinstance(dirty, bool) else None,
            "source_digest": digest if isinstance(digest, str) and mod.SHA_RE.fullmatch(digest) else None,
        }
        error = item.get("source_error") or (item.get("error") if entry["head"] is None else None)
        if isinstance(error, str) and error:
            entry["source_error"] = _token(error)
        out.append(entry)
    return out


def _merge_text(previous: Sequence[Any], new: Sequence[str], key: Callable[[Any], str], make: Callable[[str], Any]) -> List[Any]:
    merged: List[Any] = list(previous)
    seen = {key(item) for item in merged}
    for text in new:
        clean = _redact(text)
        if clean in seen:
            continue
        seen.add(clean)
        merged.append(make(clean))
    return merged[-MAX_LIST:]


def build_record(
    project_root, task: str, *, scope: Mapping[str, Any], scope_source: str, source_dir,
    decisions: Sequence[str] = (), notes: Sequence[str] = (), previous: Optional[Mapping[str, Any]] = None,
    clock: Callable[[], float] = time.time, runner=None, bytes_runner=None,
) -> Dict[str, Any]:
    mod = core(source_dir)
    _task(task)
    if finalized_location(project_root, task) is not None:
        raise HandoffRefused("task-finalized", "the task is finalized")
    try:
        info = os.lstat(str(Path(project_root) / ".workflow_artifacts" / task))
    except OSError:
        info = None
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise HandoffRefused("task-missing", "the task folder does not exist")
    directory = _store_or_none(project_root)
    state = _load_state(directory, task)

    seq = sequence(project_root, task, state, source_dir)
    position = {item: index for index, item in enumerate(seq)}
    live = sorted(_live_entries(state), key=_entry_order(position))
    completed = [
        {"phase": e["phase"], "stage": e.get("stage"), "run_id": _last_run_id(mod, e), "gate": "PASS"}
        for e in live if _passed(e)
    ]
    validation = []
    for e in live:
        gate_value = _gate_of(e)
        if gate_value is not None and gate_value.get("verdict") in mod.VALIDATION_VERDICTS:
            validation.append({
                "phase": e["phase"], "stage": e.get("stage"), "verdict": gate_value["verdict"],
                "reasons": _clean_reasons(gate_value.get("reasons")),
            })
    done = {(c["phase"], c["stage"]) for c in completed}
    pending = [{"phase": p, "stage": s} for (p, s) in seq if (p, s) not in done]
    pending_items = [(p["phase"], p["stage"]) for p in pending]

    phase: Dict[str, Any]
    native = _native(directory, task, pending_items)
    if native is not None:
        item, status, token = native
        phase = {"current": item[0], "stage": item[1], "status": status}
    elif pending:
        phase = {"current": pending[0]["phase"], "stage": pending[0]["stage"], "status": "pending"}
    else:
        phase = {"current": None, "stage": None, "status": "done"}

    prior_decisions: List[Any] = []
    prior_notes: List[Any] = []
    if previous is not None and not mod.validate(dict(previous)) and previous.get("task") == task:
        prior_decisions = list(previous["decisions"])
        prior_notes = list(previous.get("notes") or [])
    merged_decisions = _merge_text(
        prior_decisions, decisions, lambda d: d["text"], lambda t: {"text": t, "source": "checkpoint"},
    )
    merged_notes = _merge_text(prior_notes, notes, lambda n: n, lambda t: t)

    provenance: Dict[str, Any] = {
        "sources": list(PROVENANCE_SOURCES), "transcripts_imported": False,
    }
    if scope_source:
        provenance["scope_source"] = scope_source
    record: Dict[str, Any] = {
        "schema": mod.SCHEMA,
        "task": task,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())),
        "origin_runtime": ORIGIN_RUNTIME,
        "phase": phase,
        "completed": completed,
        "pending": pending,
        "decisions": merged_decisions,
        "artifacts": _artifacts(project_root, task, state, mod),
        "repo_revisions": _repo_revisions(project_root, mod, runner, bytes_runner),
        "validation": validation,
        "provenance": provenance,
        "unavailable_telemetry": list(UNAVAILABLE_TELEMETRY),
        "scope": dict(scope),
    }
    if merged_notes:
        record["notes"] = merged_notes
    if native is not None:
        record["native"] = native[2]
    reasons = mod.validate(record)
    if reasons:
        raise HandoffRefused("record-invalid", "the record built from the task state is invalid", reasons)
    return record


def write(project_root, task: str, record: Mapping[str, Any], *, source_dir) -> Path:
    mod = core(source_dir)
    directory = continuation_dir(project_root, create=True)
    path = directory / (_task(task) + ".json")
    try:
        mod.write_record(str(path), dict(record))
    except mod.RecordError as exc:
        raise HandoffRefused(exc.code, "the record cannot be written", exc.reasons) from None
    return path


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

_SCOPE_ORDER = ("profile-mismatch", "classification-mismatch", "policy-widened")


def load_for_continuation(
    project_root, task: str, *, source_dir, requested_scope: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The validated record, or a `HandoffRefused`. Reads only the record and
    directory listings."""
    _task(task)
    mod = core(source_dir)
    location = finalized_location(project_root, task)
    if location is not None:
        raise HandoffRefused("task-finalized", "the task is finalized (%s); continue it from a new task" % location)
    try:
        path = record_path(project_root, task)
    except HandoffRefused as exc:
        raise HandoffRefused("continuation-invalid", "the continuation location is unsafe", (exc.code,)) from None
    try:
        record = mod.load_record(str(path))
    except mod.RecordError as exc:
        if exc.code == "record-missing":
            markers = legacy_markers(project_root, task)
            if markers:
                raise HandoffRefused(
                    "continuation-legacy-format",
                    "only an older-format continuation exists (%s); those files are not read or converted"
                    % ", ".join(markers[:5]),
                    markers[:5],
                ) from None
            raise HandoffRefused(
                "continuation-missing",
                "no continuation record exists; create one with: quoin opencode handoff write --task %s"
                % shlex.quote(task),
            ) from None
        raise HandoffRefused("continuation-invalid", "the continuation record cannot be used", (exc.code,)) from None
    reasons = mod.validate(record)
    if reasons:
        raise HandoffRefused("continuation-invalid", "the continuation record is invalid", reasons)
    if record["task"] != task:
        raise HandoffRefused("continuation-invalid", "the record belongs to a different task", ("task-mismatch",))
    if requested_scope is not None:
        found = mod.compare_scope(record["scope"], requested_scope)
        if found:
            codes = [code for code, _ in found]
            first = next((c for c in _SCOPE_ORDER if c in codes), codes[0])
            raise HandoffRefused(
                first, "the requested scope is not covered by the recorded one",
                ["%s:%s" % pair for pair in found],
            )
    return record


def _under_root(project_root, rel: str) -> Optional[Path]:
    """The path when no component below the project root is a symlink."""
    current = Path(project_root)
    for part in rel.split("/"):
        current = current / part
        try:
            info = os.lstat(str(current))
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
    return current


def artifact_changes(project_root, record: Mapping[str, Any]) -> List[str]:
    """Record artifacts that are gone, not plain files, too large, or changed."""
    changed: List[str] = []
    for item in record.get("artifacts") or []:
        rel = item["path"]
        path = _under_root(project_root, rel)
        if path is None or not _regular(path):
            changed.append(rel)
            continue
        value = runstore._sha256_file(str(path), runstore.DEFAULT_MAX_FILE_BYTES)
        if value != item["sha256"]:
            changed.append(rel)
    return changed


def latest_shas(state: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    shas: Dict[str, str] = {}
    for entry in (state or {}).get("entries") or []:
        snapshot = entry.get("evidence")
        hashes = snapshot.get("task_hashes") if isinstance(snapshot, Mapping) else None
        if isinstance(hashes, Mapping):
            for path, value in hashes.items():
                if isinstance(value, str):
                    shas[path] = value
        for item in entry.get("harvested") or []:
            if isinstance(item, Mapping) and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str):
                shas[item["path"]] = item["sha256"]
    return shas


def state_agreement(record: Mapping[str, Any], state: Optional[Mapping[str, Any]]) -> List[str]:
    out: List[str] = []
    if state is not None and state.get("task") != record.get("task"):
        out.append("task")
    recorded = {(c["phase"], c.get("stage")) for c in record.get("completed") or []}
    if recorded != _completed_pairs(state):
        out.append("completed")
    shas = latest_shas(state)
    for item in record.get("artifacts") or []:
        if item["path"] in shas and shas[item["path"]] != item["sha256"]:
            out.append("artifact:" + item["path"])
    return out


# ---------------------------------------------------------------------------
# run facts
# ---------------------------------------------------------------------------


def _summary(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    request = record.get("request") or {}
    try:
        item = (runstore.entry_phase_for_run(request.get("phase")), runstore.normalize_stage(request.get("stage")))
    except ValueError:
        item = (None, None)
    return {
        "run_id": record.get("run_id"),
        "state": record.get("state"),
        "created_at": record.get("created_at"),
        "request": {
            "phase": request.get("phase"), "stage": request.get("stage"), "profile": request.get("profile"),
        },
        "item": item,
        "resume_blocked": record.get("resume_blocked"),
        "driver_lost": any(
            isinstance(a, Mapping) and a.get("driver_lost") for a in record.get("attempts") or []
        ),
    }


def run_facts(directory, task: str) -> Optional[Dict[str, Any]]:
    """Pointer run, latest run per item and the count of unreadable records.
    Never reconciles or rewrites anything."""
    if directory is None:
        return None
    pointer = None
    try:
        record = _pointer_record(Path(directory), task)
        pointer = _summary(record) if record is not None else None
    except (runstore.RunStoreError, ValueError):
        pointer = None
    by_item: Dict[Tuple[str, Optional[int]], Dict[str, Any]] = {}
    records, skipped = runstore.list_records(directory, task)
    for record in records:
        summary = _summary(record)
        if summary is None or summary["item"][0] is None:
            continue
        key = (str(summary["created_at"]), str(summary["run_id"]))
        current = by_item.get(summary["item"])
        if current is None or key > (str(current["created_at"]), str(current["run_id"])):
            by_item[summary["item"]] = summary
    if pointer is not None and pointer["item"][0] is not None:
        by_item[pointer["item"]] = pointer
    return {"pointer": pointer, "by_item": by_item, "records_skipped": skipped}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _q(value: Any) -> str:
    return shlex.quote(str(value))


def _stage_flag(stage: Any) -> str:
    return "" if stage is None else " --stage %s" % _q(stage)


def gate_write_command(task: str, stage: Optional[int], phase: str, root) -> str:
    return "quoin opencode gate --task %s%s --phase %s --write --project-root %s" % (
        _q(task), _stage_flag(stage), _q(phase), _q(root))


def handoff_write_command(task: str, root) -> str:
    return "quoin opencode handoff write --task %s --project-root %s" % (_q(task), _q(root))


def resume_command(run: Mapping[str, Any], task: str, root, *, new_run: bool) -> Optional[str]:
    request = run["request"]
    if not isinstance(request.get("profile"), str):
        return None
    return "quoin run --runtime opencode --profile %s --phase %s%s %s --project-root %s%s" % (
        _q(request["profile"]), _q(request["phase"]), _stage_flag(request.get("stage")), _q(task), _q(root),
        " --new-run" if new_run else "")


def restart_command(run: Mapping[str, Any], task: str, profile: str, root) -> str:
    request = run["request"]
    return "quoin run --runtime opencode --profile %s --phase %s%s %s --project-root %s --new-run" % (
        _q(profile), _q(request["phase"]), _stage_flag(request.get("stage")), _q(task), _q(root))


def fresh_run_command(task: str, stage: Optional[int], phase: str, profile: str, root) -> str:
    return "quoin run --runtime opencode --profile %s --phase %s%s %s --project-root %s" % (
        _q(profile), _q(phase), _stage_flag(stage), _q(task), _q(root))


# ---------------------------------------------------------------------------
# classification and advice
# ---------------------------------------------------------------------------


def _primary_artifact_present(project_root, task: str, stage: Optional[int], phase: str, source_dir) -> bool:
    try:
        if phase == "architect":
            return _regular(gate.task_root(project_root, task) / "architecture.md")
        if phase not in ("plan", "review"):
            return False
        sdir = gate.stage_dir(project_root, task, stage, source_dir)
        if phase == "plan":
            return _regular(sdir / "current-plan.md")
        return any(_regular(path) for _, path in gate._numbered(sdir, gate._REVIEW_RE))
    except (gate.PathUnresolved, gate.GateRefused, ValueError, OSError):
        return False


def classify_pending(project_root, record: Mapping[str, Any], state, run, source_dir) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    current = (record["phase"]["current"], record["phase"]["stage"])
    status = record["phase"]["status"]
    pointer = (run or {}).get("pointer")
    for item in record["pending"]:
        key = (item["phase"], item["stage"])
        entry = runstore.current_entry(state, key[1], key[0]) if state is not None else None
        latest = ((run or {}).get("by_item") or {}).get(key)
        present = _primary_artifact_present(project_root, record["task"], key[1], key[0], source_dir)
        gate_value = _gate_of(entry) if entry is not None else None
        facts = {
            "live_origin": entry.get("origin") if entry is not None else None,
            "gate_verdict": gate_value.get("verdict") if gate_value is not None else None,
            "artifact_present": present,
            "run_state": latest["state"] if latest is not None else None,
            "run_id": latest["run_id"] if latest is not None else None,
            "run_records_skipped": (run or {}).get("records_skipped", 0),
        }
        reasons: List[str] = []
        if pointer is not None and pointer["state"] in ("interrupted", "running") and pointer["item"] == key:
            kind = "run-open"
        elif key == current and status in ("interrupted", "in_progress"):
            kind = "record-stale"
        elif entry is not None:
            if gate_value is None:
                kind = "awaiting-gate"
            elif gate_value.get("verdict") == "FAIL":
                kind = "gate-failed"
                reasons = _clean_reasons(gate_value.get("reasons"))
            else:
                kind = "recorded-since"
        elif latest is not None and latest["state"] == "completed":
            kind = "run-completed"
        elif present:
            kind = "unrecorded"
        else:
            kind = "pending"
        out.append({"phase": key[0], "stage": key[1], "kind": kind, "facts": facts, "reasons": reasons})
    return out


def next_step(project_root, record: Mapping[str, Any], state, run, source_dir) -> Dict[str, Any]:
    task = record["task"]
    root = str(project_root)
    profile = record["scope"]["profile"]
    items = classify_pending(project_root, record, state, run, source_dir)
    result: Dict[str, Any] = {
        "phase": record["phase"]["current"], "stage": record["phase"]["stage"], "status": "done",
        "facts": {}, "steps": [], "candidates": [], "reasons": [], "hint": None, "items": items,
    }
    if record["phase"]["current"] is None:
        return result
    key = (record["phase"]["current"], record["phase"]["stage"])
    item = next((i for i in items if (i["phase"], i["stage"]) == key), None)
    if item is None:
        item = {"phase": key[0], "stage": key[1], "kind": "pending", "reasons": [], "facts": {
            "live_origin": None, "gate_verdict": None, "artifact_present": False, "run_state": None,
            "run_id": None, "run_records_skipped": (run or {}).get("records_skipped", 0)}}
    kind, facts, phase, stage = item["kind"], item["facts"], key[0], key[1]
    result.update({"status": kind, "facts": facts, "reasons": list(item["reasons"])})
    steps: List[str] = result["steps"]
    candidates: List[str] = result["candidates"]
    adopt = gate.adopt_command(task, stage, phase, root)
    write_gate = gate_write_command(task, stage, phase, root)
    by_item = (run or {}).get("by_item") or {}
    latest = by_item.get(key)

    if kind == "run-open":
        pointer = run["pointer"]
        if not isinstance(pointer["request"].get("profile"), str):
            candidates.append(restart_command(pointer, task, profile, root))
            result["hint"] = (
                "the stored run has no profile, so the CLI cannot resume it; --new-run starts the phase "
                "over and abandons the partial native session"
            )
        elif pointer["state"] == "interrupted" and pointer["resume_blocked"] is None and not pointer["driver_lost"]:
            steps.append(resume_command(pointer, task, root, new_run=False))
        else:
            candidates.extend([
                resume_command(pointer, task, root, new_run=False),
                resume_command(pointer, task, root, new_run=True),
            ])
            hint = (
                "run %s is %s%s; check quoin opencode status --task %s --project-root %s first; "
                "--new-run starts the phase over and abandons the partial native session"
                % (pointer["run_id"], pointer["state"],
                   " (blocked: %s)" % pointer["resume_blocked"] if pointer["resume_blocked"] else
                   (" (driver lost)" if pointer["driver_lost"] else ""), _q(task), _q(root))
            )
            if pointer["state"] == "running":
                hint += (
                    "; the resume candidate first reconciles the run: a run whose driver has exited is "
                    "relabelled and can then resume, a live run holds the task lock so the command is "
                    "refused lock-held, and a run still marked running after reconciliation (an orphaned "
                    "child alive) is refused run-in-progress, where only the --new-run candidate proceeds"
                )
            result["hint"] = hint
    elif kind == "record-stale":
        steps.append(handoff_write_command(task, root))
        result["hint"] = "the record says this phase was interrupted but the run store no longer shows it open; refresh the record"
    elif kind == "awaiting-gate":
        steps.append(write_gate)
    elif kind == "gate-failed":
        if facts["live_origin"] == "adopted":
            steps.extend([adopt, write_gate])
            result["hint"] = (
                "fix the work named in the reasons first; adopting again replaces the adopted entry, "
                "which is the only way to replace it in this version"
            )
        else:
            candidates.append(fresh_run_command(task, stage, phase, profile, root))
            result["hint"] = (
                "the entry was recorded by a run, and adopting over it would replace run-verified evidence; "
                "recording a replacement entry after a run belongs to the coordinator stage, so check the "
                "reasons and decide whether to run the phase again"
            )
    elif kind == "recorded-since":
        steps.append(handoff_write_command(task, root))
        result["hint"] = "the phase passed its gate after the record was written; refresh the record"
    elif kind == "run-completed":
        steps.extend([adopt, write_gate])
        hint = (
            "run %s completed but is not recorded as a workflow entry (no run writes one yet), so adopt its "
            "result and gate it; running the phase again would repeat finished work" % facts["run_id"]
        )
        if latest is not None and runstore.normalize_phase(latest["request"].get("phase")) in ("critic", "thorough_plan"):
            hint += "; the adopted evidence is the stage plan as it stands after that run"
        result["hint"] = hint
    elif kind == "unrecorded":
        steps.extend([adopt, write_gate])
        hint = (
            "the artifact exists with no workflow entry (work from the TUI, another runtime, or a run that "
            "is no longer the item's latest)"
        )
        if facts["run_state"] in ("failed", "interrupted", "running"):
            hint += "; the item's latest run did not complete, so check the artifact is complete before adopting it"
        result["hint"] = hint
    else:  # pending
        fresh = fresh_run_command(task, stage, phase, profile, root)
        status_cmd = "quoin opencode status --task %s --project-root %s" % (_q(task), _q(root))
        if facts["run_state"] == "running":
            candidates.append(fresh)
            result["hint"] = "run %s is still marked running; check %s first" % (facts["run_id"], status_cmd)
        elif facts["run_records_skipped"] > 0 and latest is None:
            candidates.append(fresh)
            result["hint"] = (
                "%d run record(s) could not be read (unreadable), so a completed run for this item may be "
                "hidden; check %s and the run store before running the phase" % (facts["run_records_skipped"], status_cmd)
            )
        else:
            steps.append(fresh)
            hints: List[str] = []
            if facts["run_state"] in ("failed", "interrupted"):
                hints.append(
                    "run %s did not complete and may have left partial changes in the tree; check them "
                    "(for example with git status) before running again" % facts["run_id"]
                )
            if phase == "implement" and latest is None:
                hints.append(
                    "implementation done with no quoin run at all (by hand or in the TUI) cannot be detected; "
                    "adopt it (quoin opencode adopt --phase implement) and gate it instead of running again"
                )
            if hints:
                result["hint"] = "; ".join(hints)
    return result
