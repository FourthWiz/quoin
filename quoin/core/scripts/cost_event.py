"""Portable cost-event schema and ledger-row parser.

This module is runtime-neutral and does NOT participate in price computation,
session-UUID acquisition, or adapter-specific session-log parsing. It provides
a typed schema for cost-ledger rows and pure functions to parse and format them.

Warning prefix kept as 'cost_snapshot.WARN' for parity with existing consumers
(test_cost_ledger_7col_parser.py and cost_snapshot/SKILL.md); a future
SKILL.md-narrowing phase will decide whether to rename to 'cost_event.WARN'.

This module is runtime-neutral; it MUST NOT import from any Claude-adapter-owned
helper (the session-log-reading or session-age scripts under quoin/scripts/). The
portable cost-event schema lives here; runtime-specific cost collection lives in
the Claude adapter.

Schema alignment: the field names and parsing rules described here match the
expanded contract in quoin/core/workflow/cost-ledger.md. Both are kept
content-aligned; that document is the prose reference, this module is the
canonical typed implementation.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


class RowParseError(ValueError):
    """Raised when a row is structurally unrecoverable (not used for WARN path)."""


@dataclass(frozen=True)
class CostEvent:
    """A single parsed row from a cost ledger file.

    Field mapping from the 7-column ledger format:
        col 1: uuid            — session identifier (runtime-agnostic)
        col 2: date            — ISO YYYY-MM-DD
        col 3: phase           — workflow phase (e.g. plan, implement, review)
        col 4: model_or_effort — Claude model tier (opus/sonnet/haiku) or effort
                                 level (high/medium/low); field name is neutral
        col 5: category        — always "task" in current practice; skip-line if
                                 any other value
        col 6: note            — free-form note; double-quotes preserved verbatim
        col 7: fallback_fires  — int, defaults to 0 for 6-column rows
        col 8: attribution     — optional micro-map string; "" = none
    """

    uuid: str
    date: str
    phase: str
    model_or_effort: str
    category: str
    note: str
    fallback_fires: int = 0
    attribution: str = ""


def parse_row(
    line: str,
    *,
    source: str = "<unknown>",
    lineno: int = 0,
) -> CostEvent | None:
    """Parse one ledger row into a CostEvent.

    Returns None for rows that should be skipped (blank, comment, too-few
    columns, non-task category). Never raises for well-formed or tolerably
    malformed rows — emits a stderr warning instead and returns the best
    available CostEvent.

    Parsing discipline (bare-pipe-with-strip per D-05):
    - Split on bare '|' (NOT on ' | '), then strip each field.
    - This matches the existing cost_snapshot/SKILL.md Step 1 and
      test_cost_ledger_7col_parser.py parsing behavior.
    - Quoted notes (e.g. "quoted note") are preserved verbatim; the reader
      does NOT strip surrounding double-quotes.
    """
    stripped = line.strip()

    if not stripped or stripped.startswith("#"):
        return None

    parts = stripped.split("|")

    if len(parts) < 6:
        return None

    uuid = parts[0].strip()
    date = parts[1].strip()
    phase = parts[2].strip()
    model_or_effort = parts[3].strip()
    category = parts[4].strip()
    note = parts[5].strip()

    # Defensive guard: skip non-task rows (mirrors test_cost_ledger_7col_parser.py:44)
    if category != "task":
        return None

    # Handle 7th column (fallback_fires) and 8th column (attribution).
    # Exactly 8 columns is first-class (no warning); >=9 columns emits the
    # extra-columns warning and ignores parts[8:].
    fallback_fires = 0
    attribution = ""
    if len(parts) >= 7:
        raw_7th = parts[6].strip()
        try:
            fallback_fires = int(raw_7th)
        except ValueError:
            print(
                f"cost_snapshot.WARN: malformed fallback_fires column at "
                f"{source}:{lineno}: {raw_7th!r}",
                file=sys.stderr,
            )
            fallback_fires = 0
    if len(parts) >= 8:
        attribution = parts[7].strip()
    if len(parts) >= 9:
        print(
            f"cost_snapshot.WARN: extra columns at {source}:{lineno} "
            f"(found {len(parts)}, expected ≤8)",
            file=sys.stderr,
        )

    return CostEvent(
        uuid=uuid,
        date=date,
        phase=phase,
        model_or_effort=model_or_effort,
        category=category,
        note=note,
        fallback_fires=fallback_fires,
        attribution=attribution,
    )


def format_row(event: CostEvent) -> str:
    """Emit the canonical ledger row for a CostEvent.

    Never appends a trailing newline; caller adds one if needed.
    Emits 8 columns only when event.attribution is non-empty; otherwise
    emits exactly the 7-column form (col 8 is omitted, not written as an
    empty trailing segment).

    Round-trip guarantee: format_row(parse_row(line)) == line.strip() for
    (a) every valid 6- or 7-column row (6-column rows upgrade one-way to
    7-column, appending ' | 0', per D-07) and (b) every valid 8-column row
    whose attribution is NON-EMPTY. An 8-column row with an EMPTY col 8
    normalizes one-way to the 7-column form (same class as the 6->7
    upgrade) — NOT a byte-identity case.
    """
    base = (
        f"{event.uuid} | {event.date} | {event.phase} | "
        f"{event.model_or_effort} | {event.category} | "
        f"{event.note} | {event.fallback_fires}"
    )
    if event.attribution:
        return f"{base} | {event.attribution}"
    return base


def parse_attribution(s: str) -> dict:
    """Parse an attribution micro-map string into a dict.

    Grammar: k=v(;k=v)* — semicolon-delimited key=value tokens, each side
    stripped of surrounding whitespace. Tolerant, pure, never raises:
    - Empty/whitespace-only input returns {}.
    - Tokens without '=' are skipped.
    - Tokens with an empty key after stripping (e.g. a lone '=') are
      skipped — this guard prevents a stray '=' from injecting a "" key.
    - Values are NOT coerced (stay str); numeric interpretation of 'usd'/
      'tok' is the reader-precedence stage's responsibility, not this
      module's.

    Canonical asserted cases:
        "usd=0.01;tok=45;src=<tag>" -> {"usd": "0.01", "tok": "45", "src": "<tag>"}
        ""                          -> {}
        "src=unresolved"            -> {"src": "unresolved"}
        "usd=;=;;foo;tok=9"         -> {"usd": "", "tok": "9"}
        " a = b ; c=d "             -> {"a": "b", "c": "d"}
    """
    out: dict = {}
    for tok in s.split(";"):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if not k.strip():
            continue
        out[k.strip()] = v.strip()
    return out


def classify_attribution(attribution: str) -> tuple[str, float | None]:
    """Classify a raw col-8 attribution string into a precedence verdict.

    Provenance-agnostic: the verdict is keyed on usd-presence plus the
    neutral 'unresolved' sentinel, NOT a hand-maintained allowlist of
    resolved 'src' tags — a new resolved src auto-classifies without a
    code change here.

    Returns exactly one of:
        ("legacy", None)        — attribution is empty (no col 8); caller
                                   does its own legacy session-log resolution.
        ("resolved", usd_float) — col 8 present, a parseable 'usd' value
                                   is present, AND 'src' is NOT the
                                   unresolvable sentinel. Never returns
                                   ("resolved", 0.0) for an unresolvable row
                                   — a parsed 0.0 here is a genuine resolved
                                   zero, distinct from "no usd present".
        ("unresolvable", None)  — col 8 present but src == "unresolved",
                                   OR usd is absent/unparseable.

    Pure, tolerant, never raises — mirrors parse_attribution's discipline.

    Canonical asserted cases:
        classify_attribution("") -> ("legacy", None)
        classify_attribution("usd=0.0123;tok=45210;src=<tag>")
            -> ("resolved", 0.0123)
        classify_attribution("tok=45;src=unresolved") -> ("unresolvable", None)
        classify_attribution("src=unresolved") -> ("unresolvable", None)
        classify_attribution("usd=abc;src=<tag>") -> ("unresolvable", None)
        classify_attribution("usd=0.0;tok=9;src=<tag>") -> ("resolved", 0.0)
    """
    if not attribution.strip():
        return ("legacy", None)

    fields = parse_attribution(attribution)
    src = fields.get("src", "")
    if src == "unresolved":
        return ("unresolvable", None)

    raw_usd = fields.get("usd")
    if raw_usd is None:
        return ("unresolvable", None)
    try:
        usd = float(raw_usd)
    except ValueError:
        return ("unresolvable", None)

    return ("resolved", usd)


def iter_events(path: Path) -> Iterator[CostEvent]:
    """Open a ledger file and yield CostEvent for each parseable row.

    Convenience reader for callers that want to iterate over all task events
    in a ledger without dealing with blank lines, comments, or skip-line cases.
    Emits stderr warnings for malformed rows via parse_row but continues.
    """
    with open(path, encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            event = parse_row(raw_line, source=str(path), lineno=lineno)
            if event is not None:
                yield event


def checkpoint_op(note: str) -> str:
    """Classify a checkpoint ledger row's note as 'save' or 'restore'.

    'restore' iff note.strip() == 'restore'; every other value is 'save'.
    Deliberately NOT a substring test (`"restore" in note`) — the real save
    note 'save (restore mode)' contains the substring 'restore' and must
    still classify as 'save'. Only meaningful for rows whose phase is
    'checkpoint'; callers should not apply this to other phases.
    """
    return "restore" if note.strip() == "restore" else "save"


@dataclass(frozen=True)
class CohortResult:
    """Result of session-cohort attribution over a set of parsed ledger rows.

    Field names are runtime-neutral — this module is scanned by a
    core-purity guard test that forbids adapter-specific vocabulary.

    resolved_total: sum of every distinct session's cost, counted exactly
        once each (resolved-inline rows + solo-legacy rows + each distinct
        shared-cohort session).
    by_phase: {phase: {"cost": float, "count": int}} — solo-legacy and
        resolved-inline rows ONLY. A phase that only ever appears via a
        shared cohort is absent here (its participation lives in
        shared_bucket.phases instead) — never a fabricated per-phase dollar
        figure for a shared session.
    by_model: {model: {"cost": float, "count": int}} — same rule as
        by_phase: shared rows contribute neither cost nor count here.
    shared_bucket: {"cost": float, "phases": {phase: {"save": int,
        "restore": int, "count": int}}, "uuids": int} for sessions shared by
        two or more phases (a "cohort"). Empty dict when there is no shared
        cohort in the input. The session cost is counted ONCE per distinct
        UUID, never once per participating phase.
    unresolvable_count: rows whose cost could not be attributed at all —
        col-8-unresolvable rows, plus solo-legacy and shared-cohort UUIDs
        whose session-cost source came back with no cost. Never folded into
        a silent $0.
    unpriced_count: subset of unresolvable_count — specifically the rows/
        UUIDs whose session-cost source had no cost available (as opposed to
        an explicit col-8 "unresolved" marker). Adapters may map this onto
        their own report key (e.g. a session-log-specific counter name) at
        the adapter boundary; this module stays vocabulary-neutral.
    """

    resolved_total: float
    by_phase: dict
    by_model: dict
    shared_bucket: dict
    unresolvable_count: int
    unpriced_count: int


def _cohort_row_get(row, key: str, default=""):
    """Read `key` off a row that may be a CostEvent, a dict with that exact
    key, or a dict using the `model` alias for `model_or_effort` (the shape
    used by some adapter readers). Pure accessor, never raises."""
    if hasattr(row, key):
        return getattr(row, key)
    if isinstance(row, dict):
        if key in row:
            return row[key]
        if key == "model_or_effort" and "model" in row:
            return row["model"]
    return default


def _cohort_resolve_safe(
    resolve_session_cost: Callable[[str], tuple],
    uuid: str,
    memo: dict,
) -> tuple:
    """Call resolve_session_cost(uuid) at most once per distinct uuid,
    memoizing the result in `memo`. A resolver that raises is treated as
    (0.0, False) for that uuid — fail-open, never a crash."""
    if uuid in memo:
        return memo[uuid]
    try:
        result = resolve_session_cost(uuid)
        cost, has_cost = float(result[0]), bool(result[1])
    except Exception:
        cost, has_cost = 0.0, False
    memo[uuid] = (cost, has_cost)
    return memo[uuid]


def _cohort_attribution_impl(
    rows,
    resolve_session_cost: Callable[[str], tuple],
) -> CohortResult:
    by_phase: dict = {}
    by_model: dict = {}
    resolved_total = 0.0
    unresolvable_count = 0
    unpriced_count = 0

    legacy_rows = []
    resolved_rows = []
    for row in rows:
        attribution = _cohort_row_get(row, "attribution", "")
        verdict, inline_usd = classify_attribution(attribution)
        if verdict == "resolved":
            resolved_rows.append((row, inline_usd if inline_usd is not None else 0.0))
        elif verdict == "unresolvable":
            unresolvable_count += 1
        else:  # legacy — candidate for cohort grouping
            legacy_rows.append(row)

    def _accumulate(phase: str, model: str, cost: float) -> None:
        nonlocal resolved_total
        resolved_total += cost
        p_entry = by_phase.setdefault(phase, {"cost": 0.0, "count": 0})
        p_entry["cost"] += cost
        p_entry["count"] += 1
        m_entry = by_model.setdefault(model, {"cost": 0.0, "count": 0})
        m_entry["cost"] += cost
        m_entry["count"] += 1

    for row, usd in resolved_rows:
        phase = _cohort_row_get(row, "phase", "")
        model = _cohort_row_get(row, "model_or_effort", "")
        _accumulate(phase, model, usd)

    counts = Counter(_cohort_row_get(row, "uuid", "") for row in legacy_rows)
    memo: dict = {}

    # uuid -> {"phases": {phase: {"save": int, "restore": int, "count": int}}}
    shared_participants: dict = {}

    for row in legacy_rows:
        uuid = _cohort_row_get(row, "uuid", "")
        phase = _cohort_row_get(row, "phase", "")
        model = _cohort_row_get(row, "model_or_effort", "")

        if counts[uuid] == 1:
            cost, has_cost = _cohort_resolve_safe(resolve_session_cost, uuid, memo)
            if not has_cost:
                unresolvable_count += 1
                unpriced_count += 1
                continue
            _accumulate(phase, model, cost)
        else:
            participant = shared_participants.setdefault(uuid, {"phases": {}})
            phase_entry = participant["phases"].setdefault(
                phase, {"save": 0, "restore": 0, "count": 0}
            )
            phase_entry["count"] += 1
            if phase == "checkpoint":
                note = _cohort_row_get(row, "note", "")
                phase_entry[checkpoint_op(note)] += 1

    shared_bucket: dict = {"cost": 0.0, "phases": {}, "uuids": 0}
    for uuid, participant in shared_participants.items():
        cost, has_cost = _cohort_resolve_safe(resolve_session_cost, uuid, memo)
        if not has_cost:
            unresolvable_count += 1
            unpriced_count += 1
            continue
        shared_bucket["cost"] += cost
        shared_bucket["uuids"] += 1
        resolved_total += cost
        for phase, ph_counts in participant["phases"].items():
            entry = shared_bucket["phases"].setdefault(
                phase, {"save": 0, "restore": 0, "count": 0}
            )
            entry["save"] += ph_counts["save"]
            entry["restore"] += ph_counts["restore"]
            entry["count"] += ph_counts["count"]

    return CohortResult(
        resolved_total=resolved_total,
        by_phase=by_phase,
        by_model=by_model,
        shared_bucket=shared_bucket if shared_bucket["uuids"] > 0 else {},
        unresolvable_count=unresolvable_count,
        unpriced_count=unpriced_count,
    )


def cohort_attribution(rows, resolve_session_cost: Callable[[str], tuple]):
    """Pure session-cohort attribution over parsed ledger rows.

    rows: iterable of objects/dicts exposing uuid/phase/model_or_effort/note/
        attribution (accepts CostEvent instances or the row-dict shape used
        by adapter readers — a small internal accessor normalizes access;
        CostEvent is not hard-required).
    resolve_session_cost: callable(uuid) -> tuple[float, bool] returning
        (whole_session_cost, has_cost). Injected by the caller — each adapter
        supplies its own session-cost source. Called AT MOST ONCE per
        distinct legacy UUID (memoized internally). This injection is what
        keeps this module free of any adapter-owned import: session-cost
        resolution stays entirely the caller's responsibility.

    Algorithm: rows are split into three streams — resolved-inline (their
    own already-priced usd, summed directly), unresolvable (contribute
    nothing, counted honestly), and legacy (no col-8 attribution at all,
    candidates for cohort grouping). Legacy rows are grouped by uuid: a uuid
    used by exactly one row (solo) has its session cost resolved and
    attributed to that row's phase exactly as before (byte-identical to the
    pre-cohort behavior); a uuid shared by two or more legacy rows (a
    "cohort") does NOT charge any participating phase the session cost —
    instead the session cost is resolved once and added to a single labeled
    shared bucket, and each participating phase is recorded (with a
    checkpoint-specific save/restore split) as a shared, not separately
    attributable participant. Every distinct session's cost is counted
    exactly once in resolved_total, regardless of how many rows or phases
    reference its UUID.

    Cohort membership and the `--since` interaction: this function groups
    the rows it is GIVEN — any date-window filtering must happen in the
    caller BEFORE calling this function, so cohorts form over the filtered
    row set. If a `--since` filter splits a shared session across the
    filter boundary, only the in-window rows form the cohort; the session
    cost is still resolved once from the injected session-cost source
    (which reports a lifetime total) — so a windowed cohort may attribute a
    session cost whose rows are partly out of window. This matches today's
    lifetime session-cost semantics and is not a regression; the labeled
    shared bucket makes the situation visible rather than misleading.

    Fail-open: any internal error while computing the cohort returns None,
    signaling the caller to fall back to its own per-row legacy
    accumulation. A `resolve_session_cost` that itself raises for a given
    uuid is treated as (0.0, False) for that uuid only — it does not fail
    the whole call.

    Returns a CohortResult (frozen dataclass), or None on internal failure.
    """
    try:
        return _cohort_attribution_impl(list(rows), resolve_session_cost)
    except Exception:
        return None
