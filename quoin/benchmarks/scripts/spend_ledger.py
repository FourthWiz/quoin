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

import json
from pathlib import Path
from typing import Optional

DEFAULT_AUTHORISED_USD = 50.0


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
    for group in groups.values():
        settlement = group["settlement"]
        if settlement is not None and settlement.get("actual_usd") is not None:
            total += float(settlement["actual_usd"])
        elif group["reservation"] is not None:
            total += float(group["reservation"].get("cap_usd") or 0.0)
        elif settlement is not None:
            # A settlement with actual_usd: null and no matching reservation
            # (e.g. the arm aborted before any cost event) is charged at its
            # own cap_usd — an unmeasured attempt is never free.
            total += float(settlement.get("cap_usd") or 0.0)
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


def precheck(
    path: Path,
    planned_caps: list[float],
    authorised: Optional[float] = None,
) -> tuple[bool, str]:
    """Check whether this invocation's planned spend fits the authorisation.

    `authorised`, when `None`, is DERIVED from the ledger: the
    `new_ceiling_usd` of the latest `reauth-note` row, falling back to
    `DEFAULT_AUTHORISED_USD` (50.0) when there is none. Returns
    `(True, "")` on success, or `(False, <GATE-STOP message>)` on failure —
    the message is the literal text callers (T-07 step 0) print verbatim.
    """
    recorded = recorded_total(path)
    planned = sum(planned_caps)
    if authorised is None:
        authorised = _latest_reauth_ceiling(path)
        if authorised is None:
            authorised = DEFAULT_AUTHORISED_USD

    if recorded + planned > authorised:
        message = (
            f"GATE-STOP: cumulative spend would exceed the ~$50 authorisation "
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
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
