"""
spend_ledger.py — Persisted cumulative spend ledger for the three-arm
benchmark gate (T-17, D-19). Blocks every paid task.

Nothing else carries spend ACROSS driver invocations, while several
rollback rows authorise arm re-runs (R-01, R-06, R-07). A candidate
re-run adds its full per-arm cap on top of whatever the first attempt
spent; this ledger is what keeps a re-run inside the ~$50 pilot
authorisation rather than letting it silently exceed it.

Storage: JSONL — one JSON object per line, appended with a single
O_APPEND write. An append-only-list-via-temp-file-and-replace design
would be a read-modify-write and could not survive two concurrent
appends without losing one; a single O_APPEND line write is atomic for
rows of this size and needs no temp file.

Schema, one row per line:
    {
      "ts": iso8601 str,
      "attempt_id": str,           # the fold key — see recorded_total()
      "kind": "reservation" | "settlement",
      "invocation": "rehearsal" | "full" | "reauth-note",
      "gate_id": str,
      "arm": "raw" | "main" | "candidate" | "probe",
      "cap_usd": float,
      "actual_usd": float | null,  # null on every reservation
      "run_id": str | null,
      "new_ceiling_usd": float | null,  # only meaningful on a reauth-note
      "note": str,
    }

`attempt_id` — NOT `run_id` — is the fold key. `run_id` is
`{gate-id}-{arm}` (D-01) and a re-run of an arm MUST reuse it (so a later
attempt can still be paired with the other two arms and land in the same
result directory); folding on `run_id` would therefore let a re-run's
reservation be absorbed into the FIRST run's settlement, under-counting
real spend. Every reservation mints its own `attempt_id`
(uuid4, or `{run_id}#{iso-ts}`) and its settlement repeats that same
value. Probe rows (T-04a, T-04c) carry `run_id: null` but their own
`attempt_id`, so they count individually rather than collapsing into one
`null` group.

A `reauth-note` row records an explicit operator re-authorisation: it
carries `cap_usd: 0.0` (so it is never mistaken for spend) and the raised
ceiling in `new_ceiling_usd`. The plan never raises the ceiling on its
own — a reauth-note is written only when a human explicitly authorises
more than the pilot's ~$50.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Optional

DEFAULT_AUTHORISED_USD = 50.0


def _is_finite_nonneg(value) -> bool:
    """True iff `value` is a real, finite, non-negative number — rejects
    `nan`, `inf`, `-inf` and negative values, all of which either disable
    or invert the spend ceiling this ledger exists to enforce (round-4
    fix: a single poisoned `nan` row used to make `recorded_total` return
    `nan`, and `nan > x` is always `False`, so `precheck` passed any
    planned spend against an already-exhausted ledger)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f >= 0.0


def positive_finite_float(raw: str) -> float:
    """argparse `type=` for any money argument (`--authorised-usd`, every
    `--max-budget-usd-*`) — rejects `nan`, `inf`, `-inf`, and non-positive
    values at parse time (round-4 fix: MAJOR 6). Bare `type=float` accepts
    all of these; a single mistyped `nan` or `-1` there silently removed
    the entire spend ceiling this argument exists to set."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a valid number")
    if not math.isfinite(value) or value <= 0.0:
        raise argparse.ArgumentTypeError(
            f"{raw!r} must be a finite, positive number (got {value})"
        )
    return value


def recorded_total(path: Path) -> float:
    """Sum of all recorded spend, folded per `attempt_id`.

    Each `attempt_id` group contributes its settlement's `actual_usd` when
    a settlement exists for it, and its reservation's `cap_usd` otherwise
    — an attempt that reserved but never settled (aborted mid-arm, killed
    by SIGINT, etc.) is still charged at its cap, never at zero. An absent
    ledger file reads as `0.0` and never errors.
    """
    path = Path(path)
    if not path.exists():
        return 0.0

    groups: dict[str, dict] = {}
    for row in _read_rows(path):
        attempt_id = row.get("attempt_id")
        if not attempt_id:
            continue
        group = groups.setdefault(attempt_id, {"reservation": None, "settlement": None})
        if row.get("kind") == "settlement":
            group["settlement"] = row
        else:
            group["reservation"] = row

    total = 0.0
    for attempt_id, group in groups.items():
        settlement = group["settlement"]
        if settlement is not None and settlement.get("actual_usd") is not None:
            value = settlement["actual_usd"]
            source = "settlement.actual_usd"
        elif group["reservation"] is not None:
            value = group["reservation"].get("cap_usd") or 0.0
            source = "reservation.cap_usd"
        elif settlement is not None:
            # A settlement with actual_usd: null and no matching reservation
            # (e.g. the arm aborted before any cost event) is charged at its
            # own cap_usd — an unmeasured attempt is never free.
            value = settlement.get("cap_usd") or 0.0
            source = "settlement.cap_usd"
        else:
            continue
        # A non-finite or negative value here would silently disable the
        # ceiling: `nan` propagates through `total`, and `nan > x` is
        # always False in `precheck`'s comparison (round-4 fix: MAJOR 7).
        if not _is_finite_nonneg(value):
            raise ValueError(
                f"{path}: attempt_id {attempt_id!r} has a non-finite or "
                f"negative {source} ({value!r}) — refusing to compute a "
                "spend total that could silently disable the authorisation "
                "ceiling"
            )
        total += float(value)
    return total


def _latest_reauth_ceiling(path: Path) -> Optional[float]:
    """Return the `new_ceiling_usd` of the most recently appended
    `reauth-note` row, or `None` if there is none."""
    if not Path(path).exists():
        return None
    latest: Optional[float] = None
    for row in _read_rows(path):
        if row.get("invocation") == "reauth-note" and row.get("new_ceiling_usd") is not None:
            latest = float(row["new_ceiling_usd"])
    return latest


def resolve_authorised_ceiling(path: Path, authorised: Optional[float]) -> float:
    """Resolve the ceiling `precheck` should enforce: `authorised` itself
    when the caller supplied one, otherwise the `new_ceiling_usd` of the
    latest `reauth-note` row in the ledger at `path`, otherwise
    `DEFAULT_AUTHORISED_USD` (50.0).

    This is a SEPARATE, explicit step from `precheck` itself — the
    "derive a ceiling from the same file whose spend it polices" fallback
    used to live silently inside `precheck`, so a caller who forgot to
    pass `authorised` got a ceiling from the ledger with no indication
    that had happened. Call this first and pass its result to `precheck`.
    """
    if authorised is not None:
        return authorised
    derived = _latest_reauth_ceiling(path)
    if derived is not None:
        return derived
    return DEFAULT_AUTHORISED_USD


def precheck(
    path: Path,
    planned_caps: list[float],
    authorised: float,
) -> tuple[bool, str]:
    """Check whether this invocation's planned spend fits `authorised`.

    `authorised` is REQUIRED — this function no longer derives a ceiling
    from the ledger it is prechecking spend against. Callers that want the
    old derive-from-ledger-or-default behaviour call
    `resolve_authorised_ceiling(path, None)` first and pass its result
    here explicitly, so the derivation is visible at the call site rather
    than a silent fallback inside the one function meant to be the
    authoritative check. Returns `(True, "")` on success, or
    `(False, <GATE-STOP message>)` on failure — the message is the literal
    text callers (T-07 step 0) print verbatim.
    """
    # Re-asserted here, not just at argument-parse time (round-4 fix: MAJOR
    # 6) — `precheck` is the one function every spend path funnels through,
    # so this is the last line of defence against a `nan`/`inf`/negative
    # ceiling or planned cap that would otherwise make the comparison below
    # pass unconditionally.
    if not _is_finite_nonneg(authorised) or authorised <= 0.0:
        return False, (
            f"GATE-STOP: --authorised-usd must be a finite, positive number "
            f"(got {authorised!r}) — a non-finite or non-positive ceiling "
            "disables the spend authorisation entirely"
        )
    for cap in planned_caps:
        if not _is_finite_nonneg(cap):
            return False, (
                f"GATE-STOP: a planned spend cap must be a finite, "
                f"non-negative number (got {cap!r})"
            )

    recorded = recorded_total(path)
    planned = sum(planned_caps)

    if recorded + planned > authorised:
        message = (
            f"GATE-STOP: cumulative spend would exceed the ${authorised:.2f} authorisation "
            f"(recorded {recorded:.2f} + this run {planned:.2f}); explicit "
            f"re-authorisation required"
        )
        return False, message
    return True, ""


def append(path: Path, row: dict) -> None:
    """Append one row as a single JSONL line via a single O_APPEND write.

    No read-modify-write: this is what lets two concurrent appends both
    survive as two lines rather than one silently clobbering the other.
    """
    # Validated on write too, not just on read (MAJOR 7) — a `nan`/`inf`
    # cost written today is a poisoned row every future `recorded_total`
    # call trips over; refusing it here is strictly cheaper than refusing
    # it later, once it is already the sole record of real spend.
    for field in ("cap_usd", "actual_usd"):
        value = row.get(field)
        if value is not None and not _is_finite_nonneg(value):
            raise ValueError(
                f"refusing to append a ledger row with a non-finite or "
                f"negative {field} ({value!r}) — attempt_id "
                f"{row.get('attempt_id')!r}"
            )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    # O_APPEND guarantees the write is atomic with respect to other O_APPEND
    # writers to the same file for a write this small (POSIX; also true on
    # the local filesystems this harness runs against).
    import os

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _read_rows(path: Path):
    """Yield every row in the ledger, raising on anything malformed.

    A truncated final line (e.g. a process killed mid-write) or a row
    missing its fold key must not be silently skipped — either would
    under-report `recorded_total` and let real spend past the
    authorisation ceiling. Callers that legitimately want a partial read
    should not use this path.
    """
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: unparseable ledger row: {exc}") from exc
        if not row.get("attempt_id"):
            raise ValueError(f"{path}:{lineno}: ledger row is missing attempt_id")
        yield row
