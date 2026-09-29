#!/usr/bin/env python3
"""run_state.py — task-keyed run-state writer/reader (IVG-258 S-1).

Gives `/run` a durable, atomically-written record of which phase/sub-phase a
run last completed, so a within-sub-phase resume (Resume Step 1b) can pick up
partway through a phase instead of only at sentinel granularity. Companion to
the existing `autonomous-progress-{task}/*.done` sentinels — this file never
overrides them; it only refines resume *inside* the first incomplete
sub-phase they already identify.

Record path
-----------
``{project-root}/.workflow_artifacts/memory/run-state-{task}.json`` — one
record per task. `/run` (T-08) is the SOLE CREATOR of a record: every phase
boundary and every escalation rewind calls plain ``--write``. Every other
writer — `/thorough_plan`'s round boundary (T-09) — calls
``--write --require-existing``, which only ever refines a record `/run`
already created and can never resurrect one `--clear` marked inactive.

Serialization
--------------
Hand-rolled, one key per line, fixed field order (see ``_FIELD_ORDER``), valid
JSON. The output alphabet is escape-free by construction: every free-text
value is sanitized (quote/backslash/control-byte substitution, then a
length cap) ONCE in ``_do_write`` before either consumer sees it -- the
record and its companion ``run-notes-{task}.md`` block share the same
sanitized value, so an embedded newline can forge a line in neither. This
describes ``_do_write``'s two consumers specifically; ``--read``'s own
render loop applies the same ``_sanitize`` pass independently (see
``_do_read``), so a record this module did not itself write — hand-edited,
or produced by some future caller that bypasses ``_do_write`` — cannot
forge an extra line on the read side either. The
record is written with ``ensure_ascii=False``, so the companion POSIX-`sh`
reader (``quoin/dev/spikes/run_state_read.sh``) — which does no JSON
unescaping — never meets a byte it cannot extract with a plain `sed`/`awk`
strip.

Concurrency
-----------
Every write goes to a writer-unique temp file
(``run-state-{task}.json.<random>.tmp``, `tempfile.mkstemp`) and lands via
``os.replace`` (atomic on the same filesystem). A fixed shared ``.tmp``
sibling name — the pattern this module's precedent
(`boundary_checkpoint.py`) uses — is unsafe here because run-state is
task-keyed and has more than one writer on the same path.

Fail-open
---------
Every path catches ``Exception``, warns to stderr, and exits 0. ``--read``
never writes anything to stdout on any absence condition (missing file,
unreadable, unparseable, schema-forward, or older than ``--max-age-days``) —
empty stdout is the caller's uniform "no usable record" signal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_FIELD_ORDER = (
    "schema",
    "task",
    "session_id",
    "active",
    "phase",
    "phase_index",
    "subphase",
    "step",
    "at_stage_boundary",
    "route",
    "profile",
    "artifacts",
    "next_action",
    "resume_command",
    "notes_path",
    "updated_at",
)

# Strings sanitized before serialization: substitute the four json.dumps
# escape classes away so the emitted record contains zero backslash bytes,
# then collapse whitespace. Applied to EVERY string field (not only the
# free-text ones the architecture calls out by name) because the no-backslash
# property is a whole-record post-condition, not a per-field one.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RUN_RE = re.compile(r" {2,}")
_AUTONOMOUS_TOKEN_RE = re.compile(r"(?<!\S)--autonomous(?!\S)")

# Per-field cap so one oversized value cannot blow the notes-rotation budget
# or multiply the record size past a small, predictable bound (both consumers
# read the SAME sanitized-and-truncated value -- see _do_write).
_MAX_FIELD_BYTES = 4096
_TRUNCATION_MARKER = "…[truncated]"


def _sanitize(value) -> str:
    """Escape-free, length-bounded normalization for a single free-text
    value. Applied once, before the value reaches either consumer (the JSON
    record and the notes-file block) -- a second application (inside
    _render() at serialization time) is a no-op, since every substitution
    and the truncation marker itself are idempotent under re-sanitization.
    The truncation budget is reserved BELOW _MAX_FIELD_BYTES (not applied
    to it) so the marker's own bytes never push the total over the cap --
    otherwise a 4-byte UTF-8 character starting three bytes before the cut
    decodes as a dangling continuation the marker then follows, and the two
    consumers (record vs. notes block, which apply this function a
    different number of times before render) can diverge on that exact
    boundary."""
    if value is None:
        return ""
    text = str(value)
    text = text.replace('"', "'").replace("\\", "/")
    text = _CONTROL_RE.sub(" ", text)
    text = _SPACE_RUN_RE.sub(" ", text).strip()
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_FIELD_BYTES:
        budget = _MAX_FIELD_BYTES - len(_TRUNCATION_MARKER.encode("utf-8"))
        text = encoded[:budget].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
    return text


def _strip_autonomous(value: str) -> str:
    """Remove any `--autonomous` token from a resume-command value (D-07):
    the record is deliberately autonomy-free."""
    text = _AUTONOMOUS_TOKEN_RE.sub("", value or "")
    text = _SPACE_RUN_RE.sub(" ", text).strip()
    return text


def _render(value) -> str:
    """Render a single field's Python value as the JSON literal that lands
    on its own line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(_sanitize(v), ensure_ascii=False) for v in value) + "]"
    return json.dumps(_sanitize(value), ensure_ascii=False)


def _serialize_record(record: dict) -> str:
    """Hand-rolled, one-key-per-line, fixed-field-order, valid-JSON writer."""
    lines = ["{"]
    last_index = len(_FIELD_ORDER) - 1
    for i, key in enumerate(_FIELD_ORDER):
        value = record.get(key)
        comma = "," if i != last_index else ""
        lines.append(f'  "{key}": {_render(value)}{comma}')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _atomic_write_record(memory_dir: Path, task: str, path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a writer-unique mkstemp temp file and
    an atomic ``os.replace``. On any failure between mkstemp and replace, the
    temp file is unlinked in a ``finally`` so a crashed writer leaves nothing
    for a stale record scan to trip over (D-61)."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(memory_dir), prefix=f"run-state-{task}.json.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(str(tmp_path), str(path))
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _load_record(path: Path):
    """Return the parsed record dict, or ``None`` for WHOLE-RECORD absence —
    no file, unreadable, unparseable, not an object, or schema-forward. This
    is the single absence rule shared by ``--read``, ``--clear``, and
    ``--write --require-existing``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if not isinstance(schema, int) or schema > SCHEMA_VERSION:
        return None
    return data


def _notes_path(memory_dir: Path, task: str) -> Path:
    return memory_dir / f"run-notes-{task}.md"


_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _valid_task(task: str) -> bool:
    if not isinstance(task, str) or not _TASK_RE.match(task):
        return False
    return ".." not in task


def append_note(memory_dir: Path, task: str, text: str, max_bytes: int | None = None) -> bool:
    """Append a single auto-resume note block to ``run-notes-{task}.md``.

    Standalone from the run-state record: this never reads or writes
    ``run-state-{task}.json``, only the companion notes file, so an
    auto-resume continuation can leave a durable trail without touching the
    resume record `/run` owns. ``text`` goes through the same ``_sanitize``
    pass the record's fields use, so it can never forge an extra line in the
    notes file. Returns ``True`` once the append was attempted (best-effort
    — a write failure is warned to stderr, not raised); returns ``False``
    without attempting anything for an invalid ``task``.
    """
    if not _valid_task(task):
        return False
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[run_state] WARNING: could not create {memory_dir}: {exc}", file=sys.stderr)
        return True
    block = f"## {datetime.now(tz=timezone.utc).isoformat()} — auto-resume\n- {_sanitize(text)}\n\n"
    budget = max_bytes if max_bytes is not None else int(
        os.environ.get("QUOIN_RUN_NOTES_MAX_BYTES", "262144")
    )
    _append_notes(_notes_path(memory_dir, task), block, budget)
    return True


def _append_notes(notes_path: Path, block: str, max_bytes: int) -> None:
    """Append ``block`` to ``notes_path``, rotating to a bounded two-file
    footprint (T-04) when the existing file is already over budget. Best
    effort — a notes-write failure is warned and swallowed; it must never
    abort a run-state write that has already landed.

    Symlink-safe like the JSON record write: a plain ``open(path, "a")``
    follows a symlink and writes through it wherever it points, unlike
    ``_atomic_write_record``'s ``os.replace``, which always replaces the
    link itself. Refuse up front if ``notes_path`` is already a symlink,
    and open with ``O_NOFOLLOW`` so a link swapped in between the check and
    the open (TOCTOU) still fails closed instead of writing through it."""
    try:
        if notes_path.is_symlink():
            print(
                f"[run_state] WARNING: notes append refused, {notes_path} is a symlink",
                file=sys.stderr,
            )
            return
        current_size = notes_path.stat().st_size if notes_path.exists() else 0
        if current_size > max_bytes:
            rotated = Path(str(notes_path) + ".1")
            if rotated.exists():
                rotated.unlink()
            notes_path.rename(rotated)
        fd = os.open(
            str(notes_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o644,
        )
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError as exc:
        print(
            f"[run_state] WARNING: notes append failed for {notes_path}: {exc}",
            file=sys.stderr,
        )


def _notes_block(updated_at: str, phase: str, subphase: str, step: str,
                  next_action: str, artifacts: list) -> str:
    lines = [f"## {updated_at} — {phase}/{subphase}"]
    lines.append(f"- step: {step}")
    lines.append(f"- next_action: {next_action}")
    if artifacts:
        lines.append("- artifacts:")
        for a in artifacts:
            lines.append(f"  - {a}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _parse_bool(text: str) -> bool:
    return str(text).strip().lower() == "true"


def _stale_days_default() -> int:
    """Resolve QUOIN_RUN_STATE_STALE_DAYS for the `--max-age-days` default,
    without ever raising. This runs at parser-construction time in main(),
    OUTSIDE the try/except that wraps parse_args()/dispatch below -- a bare
    ``int(os.environ.get(...))`` there let a malformed knob raise before
    that try was ever reached, contradicting this module's fail-open
    contract for all three modes (--read, --write, --clear alike)."""
    raw = os.environ.get("QUOIN_RUN_STATE_STALE_DAYS", "1")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _record_path(memory_dir: Path, task: str) -> Path:
    return memory_dir / f"run-state-{task}.json"


def _do_write(args) -> int:
    memory_dir = Path(args.project_root) / ".workflow_artifacts" / "memory"
    path = _record_path(memory_dir, args.task)
    notes_path = _notes_path(memory_dir, args.task)

    existing = _load_record(path) if path.exists() else None

    # A probe-fallback id ("unknown"...) is never stored by ANY write path:
    # storing it at creation would permanently anchor the record to an id
    # the hook's exact-equality match can never fire on.
    offered_sid = args.session_id or ""
    if offered_sid.startswith("unknown"):
        offered_sid = ""

    if args.require_existing:
        if existing is None or existing.get("active") is not True:
            # Absent, unreadable, unparseable, schema-forward, or already
            # cleared (active: false) — do nothing at all (D-58).
            return 0

        if args.adopt_session and args.max_age_days > 0:
            # Adoption honors the same freshness window the resume reads
            # use: every write refreshes updated_at and the file mtime, so
            # adopting a stale record would revive an entry the staleness
            # gate treats as absent. Refuse instead — without touching the
            # file — and the resume proceeds on its other tiers, exactly
            # as for an absent record.
            try:
                age_seconds = (
                    datetime.now(tz=timezone.utc).timestamp() - path.stat().st_mtime
                )
            except OSError:
                return 0
            if age_seconds > args.max_age_days * 86400:
                return 0

        def pick(field: str, cli_value, default: Any = "") -> Any:
            if cli_value is not None:
                return cli_value
            return existing.get(field, default)

        # The stored session_id anchors the PreCompact hook's exact-equality
        # match to the record's creator. A refresh usually runs in a
        # different session (e.g. a planning subagent), so it must never
        # migrate the id: once set it survives every refresh, and a probe
        # fallback id ("unknown"...) is never stored. The one sanctioned
        # migration is --adopt-session, used from the /run Resume entry
        # point: after a resume the creator's session is dead, and without
        # re-anchoring, the hook's match would never fire again for the
        # whole resumed run.
        existing_sid = existing.get("session_id", "") or ""
        if args.adopt_session and offered_sid:
            session_id = offered_sid
        elif existing_sid:
            session_id = existing_sid
        else:
            session_id = offered_sid
        phase = pick("phase", args.phase)
        phase_index = pick("phase_index", args.phase_index, 0)
        subphase = pick("subphase", args.subphase)
        step = pick("step", args.step)
        at_stage_boundary = pick("at_stage_boundary", args.at_stage_boundary, False)
        route = pick("route", args.route)
        profile = pick("profile", args.profile)
        artifacts = args.artifact if args.artifact is not None else existing.get("artifacts", [])
        next_action = pick("next_action", args.next_action)
        if args.resume_command is not None:
            resume_command = args.resume_command
        else:
            resume_command = existing.get("resume_command", f"/run --resume {args.task}")
        active = True
        wrote = True
        progress_supplied = any(
            v is not None
            for v in (args.phase, args.phase_index, args.subphase, args.step,
                      args.next_action, args.artifact)
        )
    else:
        session_id = offered_sid
        phase = args.phase or ""
        phase_index = args.phase_index if args.phase_index is not None else 0
        subphase = args.subphase or ""
        step = args.step or ""
        at_stage_boundary = bool(args.at_stage_boundary) if args.at_stage_boundary is not None else False
        route = args.route or ""
        profile = args.profile or ""
        artifacts = args.artifact if args.artifact is not None else []
        next_action = args.next_action or ""
        resume_command = args.resume_command if args.resume_command is not None else f"/run --resume {args.task}"
        active = True
        wrote = True
        progress_supplied = True

    # Sanitize once, here, before either consumer of these values -- the
    # JSON record and the notes-file block -- is built. Previously only
    # _render() (JSON-serialization time) sanitized, so the record was clean
    # but _notes_block() below received the raw value: an embedded newline
    # forged extra `## `/`- ` lines in a file Resume Step 1b treats as a
    # resume input (issue 4). _sanitize is idempotent, so _render()'s own
    # sanitize pass over these same values is a no-op.
    phase = _sanitize(phase)
    subphase = _sanitize(subphase)
    step = _sanitize(step)
    next_action = _sanitize(next_action)
    artifacts = [_sanitize(a) for a in artifacts]

    # Bound the artifacts list, newest kept: it serializes as a single line
    # and is inherited across refreshes, so unbounded growth would push the
    # later fields the PreCompact hook reads (next_action, notes_path) past
    # the shell extractor's 64 KiB read ceiling. With every other string
    # field capped at 4096 bytes, a 16 KiB artifacts line keeps the whole
    # record comfortably inside that ceiling by construction.
    _max_count = 64
    _max_bytes = 16384
    _kept: list = []
    _total = 0
    for _a in reversed(artifacts):
        if len(_kept) >= _max_count:
            break
        _cost = len(_a.encode("utf-8")) + 4
        if _kept and _total + _cost > _max_bytes:
            break
        _kept.append(_a)
        _total += _cost
    artifacts = list(reversed(_kept))

    # Sanitize BEFORE stripping `--autonomous`, not after: `_AUTONOMOUS_TOKEN_RE`'s
    # `\S` lookbehind/lookahead does not match a C0 control byte, so an
    # embedded C0 byte inside the token defeats the strip; sanitizing first
    # turns that byte into a space, breaking the token apart before the
    # strip ever runs, so there is nothing left to reconstitute afterward.
    resume_command = _strip_autonomous(_sanitize(resume_command))
    updated_at = datetime.now(tz=timezone.utc).isoformat()

    record = {
        "schema": SCHEMA_VERSION,
        "task": args.task,
        "session_id": session_id,
        "active": active,
        "phase": phase,
        "phase_index": phase_index,
        "subphase": subphase,
        "step": step,
        "at_stage_boundary": at_stage_boundary,
        "route": route,
        "profile": profile,
        "artifacts": list(artifacts),
        "next_action": next_action,
        "resume_command": resume_command,
        "notes_path": str(notes_path),
        "updated_at": updated_at,
    }

    content = _serialize_record(record)
    _atomic_write_record(memory_dir, args.task, path, content)

    # A write that carries no progress fields (e.g. the resume-entry
    # adoption, which only re-anchors session_id) appends no notes block:
    # the notes log records progress, and an inherited-only block would add
    # a spurious no-op entry on every resume.
    if wrote and not at_stage_boundary and progress_supplied:
        max_bytes = int(os.environ.get("QUOIN_RUN_NOTES_MAX_BYTES", "262144"))
        block = _notes_block(updated_at, phase, subphase, step, next_action, list(artifacts))
        _append_notes(notes_path, block, max_bytes)

    return 0


def _do_clear(args) -> int:
    memory_dir = Path(args.project_root) / ".workflow_artifacts" / "memory"
    path = _record_path(memory_dir, args.task)
    if not path.exists():
        return 0
    existing = _load_record(path)
    if existing is None:
        # Unreadable, unparseable, or schema-forward -- do not touch a
        # record we cannot safely round-trip.
        return 0
    existing["active"] = False
    existing["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    content = _serialize_record(existing)
    _atomic_write_record(memory_dir, args.task, path, content)
    return 0


def _do_read(args) -> int:
    memory_dir = Path(args.project_root) / ".workflow_artifacts" / "memory"
    path = _record_path(memory_dir, args.task)

    record = _load_record(path) if path.exists() else None
    if record is None:
        return 0

    if args.max_age_days > 0:
        try:
            age_seconds = datetime.now(tz=timezone.utc).timestamp() - path.stat().st_mtime
        except OSError:
            return 0
        if age_seconds > args.max_age_days * 86400:
            return 0

    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    lines = []
    for field in fields:
        if field not in record:
            lines.append(f"{field}=")
            continue
        value = record[field]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, list):
            rendered = ",".join(_sanitize(v) for v in value)
        else:
            # Sanitize even a record this module did not itself write --
            # _sanitize is idempotent, so this is a no-op for a value
            # _do_write already sanitized, and it strips any embedded
            # newline/control byte a hand-edited or otherwise-produced
            # record might carry, so it cannot forge an extra output line.
            rendered = "" if value is None else _sanitize(value)
        lines.append(f"{field}={rendered}")
    if lines:
        print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Task-keyed run-state writer/reader for /run within-sub-phase "
            "resume (IVG-258 S-1). Always exits 0 (fail-open)."
        ),
        add_help=True,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--clear", action="store_true")
    mode.add_argument("--read", action="store_true")
    mode.add_argument("--note", default=None, metavar="TEXT")

    parser.add_argument("--project-root", required=True, metavar="PATH", dest="project_root")
    parser.add_argument("--task", required=True, metavar="NAME")

    # --write options
    parser.add_argument("--session-id", default=None, dest="session_id")
    parser.add_argument("--phase", default=None)
    parser.add_argument("--phase-index", default=None, type=int, dest="phase_index")
    parser.add_argument("--subphase", default=None)
    parser.add_argument("--step", default=None)
    parser.add_argument("--at-stage-boundary", default=None, type=_parse_bool, dest="at_stage_boundary")
    parser.add_argument("--route", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--artifact", default=None, action="append", dest="artifact")
    parser.add_argument("--next-action", default=None, dest="next_action")
    parser.add_argument("--resume-command", default=None, dest="resume_command")
    parser.add_argument("--require-existing", action="store_true", dest="require_existing")
    parser.add_argument("--adopt-session", action="store_true", dest="adopt_session")

    # --read options
    parser.add_argument("--fields", default="")
    parser.add_argument(
        "--max-age-days",
        default=_stale_days_default(),
        type=int,
        dest="max_age_days",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0

    try:
        if args.write:
            return _do_write(args)
        if args.clear:
            return _do_clear(args)
        if args.read:
            return _do_read(args)
        if args.note is not None:
            memory_dir = Path(args.project_root) / ".workflow_artifacts" / "memory"
            append_note(memory_dir, args.task, args.note)
            return 0
        # No mode selected: nothing to do, fail-open.
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[run_state] WARNING: unexpected error: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
