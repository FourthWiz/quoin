#!/usr/bin/env python3
"""continuation_handoff.py - canonical validator and store for the continuation record.

A continuation record lets a new runtime session pick up a task where an earlier
session stopped. It is not the dispatch/return envelope checked by
handoff_validate.py; it is a closed, versioned JSON document kept per task under
.workflow_artifacts/memory/continuation/.

Portable core: stdlib only, no import from any quoin module or adapter script,
and it runs under a bare python3 (3.8 floor).

Usage:
    continuation_handoff.py --validate FILE
    continuation_handoff.py --self-test

Exit codes: 0 pass, 1 invalid, 2 usage error or unreadable file.
"""

from __future__ import annotations

import argparse
import copy
import errno
import json
import os
import re
import stat
import sys
from typing import Any, Dict, List, Optional, Tuple

# -- Constants ----------------------------------------------------------------

SCHEMA = "quoin-continuation/1"
SUPPORTED_MAJOR = 1
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 2000
MAX_TOKEN_CHARS = 256
MAX_PATH_CHARS = 1024
MAX_PARENT_SEGMENTS = 32

LIST_CAPS = {
    "artifacts": 5000,
    "repo_revisions": 64,
    "completed": 1000,
    "pending": 1000,
    "validation": 1000,
    "decisions": 100,
    "notes": 100,
    "reasons": 64,
    "sources": 32,
    "unavailable_telemetry": 32,
    "policy": 256,
    "role_models": 64,
    "limits": 32,
}

GATED_PHASES = ("discover", "architect", "plan", "implement", "review")
STAGELESS_PHASES = ("discover", "architect")
PHASE_STATUSES = ("pending", "in_progress", "interrupted", "done")
VALIDATION_VERDICTS = ("PASS", "FAIL")

TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+_-]{0,255}")
RUNTIME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SHA_RE = re.compile(r"[0-9a-f]{64}")
HEAD_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
SCHEMA_SHAPE_RE = re.compile(r"quoin-continuation/(\d+)")
DRIVE_RE = re.compile(r"[A-Za-z]:")

# Characters refused in free text: C0 controls except newline and tab, DEL, C1,
# line and paragraph separators, and bidirectional controls.
_TEXT_BAD_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f  ؜‎‏‪-‮⁦-⁩]"
)
_ANY_BAD_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f  ؜‎‏‪-‮⁦-⁩]"
)
_MODEL_BAD_RE = re.compile(
    "[\\s\x00-\x1f\x7f-\x9f  ؜‎‏‪-‮⁦-⁩]"
)

# Field table. Names use dots for nesting and [] for list items. The reference
# document carries the same table; a test keeps the two in agreement.
REQUIRED_FIELDS = (
    "schema",
    "task",
    "created_at",
    "origin_runtime",
    "phase",
    "phase.current",
    "phase.stage",
    "phase.status",
    "completed",
    "pending",
    "decisions",
    "artifacts",
    "repo_revisions",
    "validation",
    "provenance",
    "provenance.sources",
    "provenance.transcripts_imported",
    "unavailable_telemetry",
    "scope",
    "scope.profile",
    "scope.classification",
    "scope.policy_ceiling",
    "scope.policy_ceiling.enabled_providers",
    "scope.policy_ceiling.provider_allowlist",
    "scope.policy_ceiling.role_models",
    "scope.policy_ceiling.limits",
    "scope.policy_ceiling.network",
)
OPTIONAL_FIELDS = (
    "notes",
    "native",
    "native.run_id",
    "native.session_id",
    "provenance.scope_source",
)
ITEM_FIELDS = (
    "completed[].phase",
    "completed[].stage",
    "completed[].run_id",
    "completed[].gate",
    "pending[].phase",
    "pending[].stage",
    "decisions[].text",
    "decisions[].source",
    "artifacts[].path",
    "artifacts[].sha256",
    "artifacts[].type",
    "repo_revisions[].path",
    "repo_revisions[].head",
    "repo_revisions[].source_dirty",
    "repo_revisions[].source_digest",
    "repo_revisions[].source_error",
    "validation[].phase",
    "validation[].stage",
    "validation[].verdict",
    "validation[].reasons",
)

REASON_CODES = (
    "record-not-object",
    "field-missing",
    "field-invalid",
    "field-unknown",
    "schema-unsupported",
    "transcripts-imported",
    "path-invalid",
    "text-oversized",
    "text-control-character",
    "list-too-long",
    "duplicate-entry",
    "pending-overlaps-completed",
    "phase-inconsistent",
    "record-too-large",
)


class RecordError(Exception):
    """Raised by load_record and write_record; code is a stable reason token."""

    def __init__(self, code: str, reasons: Any = ()) -> None:
        super().__init__(code)
        self.code = code
        self.reasons = list(reasons)


# -- Validation ---------------------------------------------------------------


MAX_REASONS = 256


def _is_int(value: Any) -> bool:
    return type(value) is int


class _Checker:
    def __init__(self) -> None:
        self.reasons: List[str] = []
        self._seen: set = set()

    def add(self, code: str, name: str = "") -> None:
        reason = code + (":" + name if name else "")
        if reason in self._seen:
            return
        self._seen.add(reason)
        if len(self.reasons) < MAX_REASONS:
            self.reasons.append(reason)
        elif len(self.reasons) == MAX_REASONS:
            self.reasons.append("reasons-truncated")

    # primitives -------------------------------------------------------------

    def obj(self, value: Any, name: str, required: Tuple[str, ...], optional: Tuple[str, ...] = ()) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict):
            self.add("field-invalid", name)
            return None
        prefix = name + "." if name else ""
        for key in required:
            if key not in value:
                self.add("field-missing", prefix + key)
        for key in value:
            if key not in required and key not in optional:
                self.add("field-unknown", prefix + str(key))
        return value

    def lst(self, value: Any, name: str, cap_key: str) -> Optional[List[Any]]:
        if not isinstance(value, list):
            self.add("field-invalid", name)
            return None
        if len(value) > LIST_CAPS[cap_key]:
            self.add("list-too-long", name)
            return None
        return value

    def text(self, value: Any, name: str) -> bool:
        if not isinstance(value, str):
            self.add("field-invalid", name)
            return False
        if len(value) > MAX_TEXT_CHARS:
            self.add("text-oversized", name)
            return False
        if _TEXT_BAD_RE.search(value):
            self.add("text-control-character", name)
            return False
        return True

    def token(self, value: Any, name: str) -> bool:
        if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
            self.add("field-invalid", name)
            return False
        return True

    def model(self, value: Any, name: str) -> bool:
        if not isinstance(value, str) or not 1 <= len(value) <= 256 or _MODEL_BAD_RE.search(value):
            self.add("field-invalid", name)
            return False
        return True

    def match(self, value: Any, rx: Any, name: str) -> bool:
        if not isinstance(value, str) or not rx.fullmatch(value):
            self.add("field-invalid", name)
            return False
        return True

    def nullable(self, value: Any, check: Any, name: str) -> None:
        if value is not None:
            check(value, name)

    def stage(self, value: Any, name: str) -> bool:
        if value is None:
            return True
        if not _is_int(value) or value < 1:
            self.add("field-invalid", name)
            return False
        return True

    def phase_stage(self, item: Dict[str, Any], name: str) -> Optional[Tuple[str, Optional[int]]]:
        phase = item.get("phase")
        ok = True
        if phase not in GATED_PHASES or not isinstance(phase, str):
            self.add("field-invalid", name + ".phase")
            ok = False
        if "stage" in item:
            if not self.stage(item["stage"], name + ".stage"):
                ok = False
            elif ok and phase in STAGELESS_PHASES and item["stage"] is not None:
                self.add("field-invalid", name + ".stage")
                ok = False
        else:
            ok = False
        if not ok:
            return None
        return (phase, item["stage"])

    def path(self, value: Any, name: str, repo: bool = False) -> bool:
        if not isinstance(value, str):
            self.add("field-invalid", name)
            return False
        if not path_ok(value, repo=repo):
            self.add("path-invalid", name)
            return False
        return True


def path_ok(value: str, repo: bool = False) -> bool:
    """Project-relative POSIX path rule; repo=True also accepts the root forms."""
    if repo:
        if value == ".":
            return True
        segments = value.split("/")
        if all(seg == ".." for seg in segments):
            return 0 < len(segments) <= MAX_PARENT_SEGMENTS
    if not value or len(value) > MAX_PATH_CHARS:
        return False
    if value.startswith("~") or "\\" in value or DRIVE_RE.match(value):
        return False
    if _ANY_BAD_RE.search(value):
        return False
    for seg in value.split("/"):
        if seg in ("", ".", ".."):
            return False
    return True


def _check_pairs(chk: _Checker, items: Optional[List[Any]], name: str, extra: Tuple[str, ...], optional: Tuple[str, ...], per_item: Any) -> List[Tuple[str, Optional[int]]]:
    pairs: List[Tuple[str, Optional[int]]] = []
    seen = set()
    if items is None:
        return pairs
    for index, item in enumerate(items):
        label = "%s[%d]" % (name, index)
        obj = chk.obj(item, label, ("phase", "stage") + extra, optional)
        if obj is None:
            continue
        pair = chk.phase_stage(obj, label)
        per_item(obj, label)
        if pair is None:
            continue
        if pair in seen:
            chk.add("duplicate-entry", name)
        seen.add(pair)
        pairs.append(pair)
    return pairs


def _check_scope(chk: _Checker, scope: Any, name: str) -> None:
    obj = chk.obj(scope, name, ("profile", "classification", "policy_ceiling"))
    if obj is None:
        return
    if "profile" in obj:
        chk.token(obj["profile"], name + ".profile")
    if "classification" in obj:
        chk.token(obj["classification"], name + ".classification")
    if "policy_ceiling" not in obj:
        return
    cname = name + ".policy_ceiling"
    ceiling = chk.obj(
        obj["policy_ceiling"],
        cname,
        ("enabled_providers", "provider_allowlist", "role_models", "limits", "network"),
    )
    if ceiling is None:
        return
    if "enabled_providers" in ceiling:
        items = chk.lst(ceiling["enabled_providers"], cname + ".enabled_providers", "policy")
        for i, v in enumerate(items or []):
            chk.token(v, "%s.enabled_providers[%d]" % (cname, i))
    if "provider_allowlist" in ceiling:
        items = chk.lst(ceiling["provider_allowlist"], cname + ".provider_allowlist", "policy")
        for i, v in enumerate(items or []):
            chk.model(v, "%s.provider_allowlist[%d]" % (cname, i))
    if "role_models" in ceiling:
        rm = ceiling["role_models"]
        if not isinstance(rm, dict):
            chk.add("field-invalid", cname + ".role_models")
        elif len(rm) > LIST_CAPS["role_models"]:
            chk.add("list-too-long", cname + ".role_models")
        else:
            for key, value in rm.items():
                chk.token(key, "%s.role_models.%s" % (cname, key))
                chk.model(value, "%s.role_models.%s" % (cname, key))
    if "limits" in ceiling:
        lim = ceiling["limits"]
        if not isinstance(lim, dict):
            chk.add("field-invalid", cname + ".limits")
        elif len(lim) > LIST_CAPS["limits"]:
            chk.add("list-too-long", cname + ".limits")
        else:
            for key, value in lim.items():
                chk.token(key, "%s.limits.%s" % (cname, key))
                if value is not None and (not _is_int(value) or value < 0):
                    chk.add("field-invalid", "%s.limits.%s" % (cname, key))
    if "network" in ceiling:
        chk.token(ceiling["network"], cname + ".network")


def canonical_bytes(record: Dict[str, Any]) -> bytes:
    """Serialization shared by validate (size check) and write_record."""
    text = json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def validate(record: Any) -> List[str]:
    """Return reason strings in discovery order; an empty list means valid."""
    chk = _Checker()
    if not isinstance(record, dict):
        return ["record-not-object"]
    top_required = tuple(f for f in REQUIRED_FIELDS if "." not in f)
    top_optional = tuple(f for f in OPTIONAL_FIELDS if "." not in f)
    obj = chk.obj(record, "", top_required, top_optional)
    assert obj is not None

    if "schema" in obj:
        schema = obj["schema"]
        shape = SCHEMA_SHAPE_RE.fullmatch(schema) if isinstance(schema, str) else None
        if shape is None:
            chk.add("field-invalid", "schema")
        elif int(shape.group(1)) != SUPPORTED_MAJOR or schema != SCHEMA:
            chk.add("schema-unsupported")
    if "task" in obj:
        chk.match(obj["task"], TASK_RE, "task")
    if "created_at" in obj:
        chk.match(obj["created_at"], TIME_RE, "created_at")
    if "origin_runtime" in obj:
        chk.match(obj["origin_runtime"], RUNTIME_RE, "origin_runtime")

    phase_info: Optional[Dict[str, Any]] = None
    if "phase" in obj:
        phase = chk.obj(obj["phase"], "phase", ("current", "stage", "status"))
        if phase is not None:
            phase_info = phase
            if "current" in phase and phase["current"] is not None:
                if phase["current"] not in GATED_PHASES or not isinstance(phase["current"], str):
                    chk.add("field-invalid", "phase.current")
            if "stage" in phase:
                chk.stage(phase["stage"], "phase.stage")
            if "status" in phase and phase["status"] not in PHASE_STATUSES:
                chk.add("field-invalid", "phase.status")

    completed: List[Tuple[str, Optional[int]]] = []
    if "completed" in obj:
        items = chk.lst(obj["completed"], "completed", "completed")

        def completed_item(item: Dict[str, Any], label: str) -> None:
            if "run_id" in item:
                chk.nullable(item["run_id"], chk.token, label + ".run_id")
            if "gate" in item and item["gate"] != "PASS":
                chk.add("field-invalid", label + ".gate")

        completed = _check_pairs(chk, items, "completed", ("run_id", "gate"), (), completed_item)

    pending: List[Tuple[str, Optional[int]]] = []
    if "pending" in obj:
        items = chk.lst(obj["pending"], "pending", "pending")
        pending = _check_pairs(chk, items, "pending", (), (), lambda item, label: None)

    if "decisions" in obj:
        items = chk.lst(obj["decisions"], "decisions", "decisions")
        for index, item in enumerate(items or []):
            label = "decisions[%d]" % index
            sub = chk.obj(item, label, ("text", "source"))
            if sub is None:
                continue
            if "text" in sub:
                chk.text(sub["text"], label + ".text")
            if "source" in sub:
                chk.token(sub["source"], label + ".source")
    if "notes" in obj:
        items = chk.lst(obj["notes"], "notes", "notes")
        for index, item in enumerate(items or []):
            chk.text(item, "notes[%d]" % index)

    if "artifacts" in obj:
        items = chk.lst(obj["artifacts"], "artifacts", "artifacts")
        seen_paths = set()
        for index, item in enumerate(items or []):
            label = "artifacts[%d]" % index
            sub = chk.obj(item, label, ("path", "sha256", "type"))
            if sub is None:
                continue
            if "path" in sub and chk.path(sub["path"], label + ".path"):
                if sub["path"] in seen_paths:
                    chk.add("duplicate-entry", "artifacts")
                seen_paths.add(sub["path"])
            if "sha256" in sub:
                chk.match(sub["sha256"], SHA_RE, label + ".sha256")
            if "type" in sub:
                chk.token(sub["type"], label + ".type")

    if "repo_revisions" in obj:
        items = chk.lst(obj["repo_revisions"], "repo_revisions", "repo_revisions")
        seen_paths = set()
        for index, item in enumerate(items or []):
            label = "repo_revisions[%d]" % index
            sub = chk.obj(item, label, ("path", "head", "source_dirty", "source_digest"), ("source_error",))
            if sub is None:
                continue
            if "path" in sub and chk.path(sub["path"], label + ".path", repo=True):
                if sub["path"] in seen_paths:
                    chk.add("duplicate-entry", "repo_revisions")
                seen_paths.add(sub["path"])
            if "head" in sub:
                chk.nullable(sub["head"], lambda v, n: chk.match(v, HEAD_RE, n), label + ".head")
            if "source_dirty" in sub and sub["source_dirty"] is not None and type(sub["source_dirty"]) is not bool:
                chk.add("field-invalid", label + ".source_dirty")
            if "source_digest" in sub:
                chk.nullable(sub["source_digest"], lambda v, n: chk.match(v, SHA_RE, n), label + ".source_digest")
            if "source_error" in sub:
                chk.nullable(sub["source_error"], chk.token, label + ".source_error")

    if "validation" in obj:
        items = chk.lst(obj["validation"], "validation", "validation")

        def validation_item(item: Dict[str, Any], label: str) -> None:
            if "verdict" in item and item["verdict"] not in VALIDATION_VERDICTS:
                chk.add("field-invalid", label + ".verdict")
            if "reasons" in item:
                reasons = chk.lst(item["reasons"], label + ".reasons", "reasons")
                for i, r in enumerate(reasons or []):
                    chk.token(r, "%s.reasons[%d]" % (label, i))

        _check_pairs(chk, items, "validation", ("verdict", "reasons"), (), validation_item)

    if "provenance" in obj:
        prov = chk.obj(obj["provenance"], "provenance", ("sources", "transcripts_imported"), ("scope_source",))
        if prov is not None:
            if "sources" in prov:
                sources = chk.lst(prov["sources"], "provenance.sources", "sources")
                if sources is not None and not sources:
                    chk.add("field-invalid", "provenance.sources")
                for i, s in enumerate(sources or []):
                    chk.token(s, "provenance.sources[%d]" % i)
            if "transcripts_imported" in prov and prov["transcripts_imported"] is not False:
                chk.add("transcripts-imported")
            if "scope_source" in prov:
                chk.token(prov["scope_source"], "provenance.scope_source")

    if "unavailable_telemetry" in obj:
        items = chk.lst(obj["unavailable_telemetry"], "unavailable_telemetry", "unavailable_telemetry")
        seen_tokens = set()
        for i, t in enumerate(items or []):
            if chk.token(t, "unavailable_telemetry[%d]" % i):
                if t in seen_tokens:
                    chk.add("duplicate-entry", "unavailable_telemetry")
                seen_tokens.add(t)

    if "scope" in obj:
        _check_scope(chk, obj["scope"], "scope")

    if "native" in obj:
        native = chk.obj(obj["native"], "native", (), ("run_id", "session_id"))
        if native is not None:
            if "run_id" in native:
                chk.token(native["run_id"], "native.run_id")
            if "session_id" in native:
                chk.nullable(native["session_id"], chk.token, "native.session_id")

    # cross-field rules
    if set(completed) & set(pending):
        chk.add("pending-overlaps-completed")
    if phase_info is not None and all(k in phase_info for k in ("current", "stage", "status")):
        current = phase_info["current"]
        status = phase_info["status"]
        if status in PHASE_STATUSES and (current is None or current in GATED_PHASES):
            done = status == "done"
            cur_null = current is None
            pend_empty = "pending" in obj and isinstance(obj["pending"], list) and not obj["pending"]
            if "pending" in obj and isinstance(obj["pending"], list):
                if cur_null and phase_info["stage"] is not None:
                    chk.add("phase-inconsistent")
                elif not (done == cur_null == pend_empty):
                    chk.add("phase-inconsistent")
                elif not done and (current, phase_info["stage"]) not in pending:
                    chk.add("phase-inconsistent")

    if not chk.reasons:
        try:
            if len(canonical_bytes(record)) > MAX_RECORD_BYTES:
                chk.add("record-too-large")
        except (TypeError, ValueError):
            chk.add("field-invalid", "record")
    return chk.reasons


# -- Scope comparison ---------------------------------------------------------


def compare_scope(recorded: Any, requested: Any) -> List[Tuple[str, str]]:
    """Return (code, detail) pairs; an empty list means requested only narrows recorded."""
    out: List[Tuple[str, str]] = []
    for label, scope in (("recorded", recorded), ("requested", requested)):
        probe = _Checker()
        _check_scope(probe, scope, "scope")
        if probe.reasons:
            out.append(("scope-invalid", label))
    if out:
        return out
    if recorded["profile"] != requested["profile"]:
        out.append(("profile-mismatch", "profile"))
    if recorded["classification"] != requested["classification"]:
        out.append(("classification-mismatch", "classification"))
    rec = recorded["policy_ceiling"]
    req = requested["policy_ceiling"]
    for field in ("enabled_providers", "provider_allowlist"):
        if set(req[field]) - set(rec[field]):
            out.append(("policy-widened", "policy_ceiling." + field))
    for role in sorted(req["role_models"]):
        if role not in rec["role_models"] or rec["role_models"][role] != req["role_models"][role]:
            out.append(("policy-widened", "policy_ceiling.role_models." + role))
    names = sorted(set(rec["limits"]) | set(req["limits"]))
    for name in names:
        before = rec["limits"].get(name)
        after = req["limits"].get(name)
        if before is None:
            continue
        if after is None or after > before:
            out.append(("policy-widened", "policy_ceiling.limits." + name))
    if rec["network"] != req["network"]:
        out.append(("policy-widened", "policy_ceiling.network"))
    return out


# -- Load and write -----------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError("constant " + name)


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key " + key)
        out[key] = value
    return out


def load_record(path: str) -> Dict[str, Any]:
    """Read one record file. Does not validate; callers call validate()."""
    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    try:
        if stat.S_ISLNK(os.lstat(parent).st_mode):
            raise RecordError("unsafe-path")
    except FileNotFoundError:
        raise RecordError("record-missing")
    except OSError:
        raise RecordError("record-unreadable")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise RecordError("record-missing")
    except OSError:
        raise RecordError("record-unreadable")
    if stat.S_ISLNK(st.st_mode):
        raise RecordError("unsafe-path")
    if not stat.S_ISREG(st.st_mode):
        raise RecordError("record-unreadable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise RecordError("record-unreadable")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecordError("record-unreadable")
        chunks = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError:
        raise RecordError("record-unreadable")
    finally:
        os.close(fd)
    if len(data) > MAX_RECORD_BYTES:
        raise RecordError("record-too-large")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RecordError("json-invalid")
    if not isinstance(value, dict):
        raise RecordError("record-not-object")
    return value


def _fsync_dir(directory: str) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            raise
    finally:
        os.close(fd)


def _atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    name = os.path.basename(path)
    tmp = os.path.join(directory, ".%s.tmp-%d-%s" % (name, os.getpid(), os.urandom(4).hex()))
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_record(path: str, record: Dict[str, Any]) -> None:
    """Validate, then replace the record atomically, keeping the old one as NAME.prev.json."""
    reasons = validate(record)
    if reasons:
        raise RecordError("record-invalid", reasons)
    if str(record.get("task", "")).endswith(".prev"):
        # NAME.prev.json is the backup slot of task NAME.
        raise RecordError("record-invalid", ["field-invalid:task"])
    path = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(path)
    try:
        dst = os.lstat(directory)
    except OSError:
        raise RecordError("unsafe-path")
    if stat.S_ISLNK(dst.st_mode) or not stat.S_ISDIR(dst.st_mode):
        raise RecordError("unsafe-path")
    existing = None
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError:
        raise RecordError("unsafe-path")
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise RecordError("unsafe-path")
    data = canonical_bytes(record)
    if len(data) > MAX_RECORD_BYTES:
        raise RecordError("record-too-large")
    if existing is not None and stat.S_ISREG(existing.st_mode):
        previous = load_bytes(path)
        base = os.path.basename(path)
        stem = base[:-5] if base.endswith(".json") else base
        _atomic_write(os.path.join(directory, stem + ".prev.json"), previous)
    _atomic_write(path, data)
    _fsync_dir(directory)


def load_bytes(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise RecordError("record-unreadable")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecordError("record-unreadable")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > MAX_RECORD_BYTES:
                raise RecordError("record-too-large")
        return b"".join(chunks)
    except OSError:
        raise RecordError("record-unreadable")
    finally:
        os.close(fd)


# -- Self-test and CLI --------------------------------------------------------


def example_record() -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "task": "example-task",
        "created_at": "2026-01-01T00:00:00Z",
        "origin_runtime": "example-runtime",
        "phase": {"current": "implement", "stage": 1, "status": "pending"},
        "completed": [{"phase": "plan", "stage": 1, "run_id": None, "gate": "PASS"}],
        "pending": [{"phase": "implement", "stage": 1}, {"phase": "review", "stage": 1}],
        "decisions": [{"text": "keep the parser strict", "source": "checkpoint"}],
        "artifacts": [{"path": "task/current-plan.md", "sha256": "0" * 64, "type": "current-plan"}],
        "repo_revisions": [
            {"path": ".", "head": "1" * 40, "source_dirty": False, "source_digest": "2" * 64}
        ],
        "validation": [{"phase": "plan", "stage": 1, "verdict": "PASS", "reasons": []}],
        "provenance": {
            "sources": ["workflow-state"],
            "transcripts_imported": False,
            "scope_source": "operator-flag",
        },
        "unavailable_telemetry": ["child-session-usage"],
        "scope": {
            "profile": "personal",
            "classification": "personal",
            "policy_ceiling": {
                "enabled_providers": ["alpha", "beta"],
                "provider_allowlist": ["alpha/model-one", "beta/model-two"],
                "role_models": {"planner": "alpha/model-one"},
                "limits": {"max_run_seconds": 600, "max_turns": None},
                "network": "profile-default",
            },
        },
    }


def self_test() -> Tuple[bool, str]:
    base = example_record()
    cases: List[Tuple[str, Any, Any]] = []

    def case(label: str, got: Any, want: Any) -> None:
        cases.append((label, got, want))

    case("valid record", validate(base), [])
    no_scope = copy.deepcopy(base)
    del no_scope["scope"]
    case("missing scope", validate(no_scope), ["field-missing:scope"])
    imported = copy.deepcopy(base)
    imported["provenance"]["transcripts_imported"] = True
    case("transcripts imported", validate(imported), ["transcripts-imported"])
    traversal = copy.deepcopy(base)
    traversal["artifacts"][0]["path"] = "../x"
    case("path traversal", validate(traversal), ["path-invalid:artifacts[0].path"])
    narrow = copy.deepcopy(base["scope"])
    narrow["policy_ceiling"]["enabled_providers"] = ["alpha"]
    case("narrower ceiling", compare_scope(base["scope"], narrow), [])
    wide = copy.deepcopy(base["scope"])
    wide["policy_ceiling"]["enabled_providers"] = ["alpha", "beta", "gamma"]
    case(
        "wider ceiling",
        compare_scope(base["scope"], wide),
        [("policy-widened", "policy_ceiling.enabled_providers")],
    )
    for label, got, want in cases:
        if got != want:
            return False, "FAIL: %s: got %r, want %r" % (label, got, want)
    return True, "PASS: --self-test (%d embedded fixtures)" % len(cases)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a continuation record.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate", metavar="FILE")
    group.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        ok, message = self_test()
        print(message)
        return 0 if ok else 1
    try:
        record = load_record(args.validate)
    except RecordError as exc:
        print("ERROR " + exc.code)
        return 2
    reasons = validate(record)
    if not reasons:
        print("PASS")
        return 0
    for reason in reasons:
        print("FAIL " + reason)
    return 1


if __name__ == "__main__":
    sys.exit(main())
