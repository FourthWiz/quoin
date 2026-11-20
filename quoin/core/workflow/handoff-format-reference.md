# Agent Handoff Format — Checkable Rules Reference

This file holds the checkable-rule table (`H-01` through `H-21`) and the rule interaction cascade for the inter-agent handoff envelope. The normative reference for the envelope itself — required and optional fields, field ordering, delimiters, escaping, byte bounds, and version-marker rules — is `${CLAUDE_PLUGIN_ROOT}/core/workflow/handoff-format.md`. Read that file first; this one enumerates every checkable rule and how the rules interact when more than one fires on the same payload.

## Checkable Rules

Every checkable rule has a named ID, `H-01` through `H-21`, and a fixed
severity. `FATAL` rules reject a payload (exit 1); `ADVISORY` rules warn on a
conforming-but-suboptimal payload (exit 0); `RECOMMENDED` rules describe a
consumer-side obligation that no payload can violate, so they carry no
validator function and no test.

| Rule | Severity | Description |
|---|---|---|
| H-01 | FATAL | Marker well-formedness: an open marker matching the version-marker shape, with a matching close marker. A payload with no open marker at all is `FAIL H-01` (fail-closed) — this is the one case where "no marker" is itself the violation, rather than falling back per the unrecognised-major rule. |
| H-02 | ADVISORY | Unrecognised major version. Warns and stops further envelope evaluation for this payload (fail-open). |
| H-03 | ADVISORY | Unrecognised minor version within a recognised major. Warns; evaluation continues. |
| H-04 | FATAL | Direction keyword: the marker's trailing word must be `dispatch` or `return`. |
| H-05 | FATAL | Required-field presence, evaluated per (direction, status) against the Status Required Fields table in `handoff-format.md`. |
| H-06 | FATAL | `status` enum membership: the value must be one of the four members in Status And Verdict Vocabulary. |
| H-07 | FATAL | `verdict` vocabulary membership, checked only when `verdict` is present. |
| H-08 | FATAL | Per-value byte bound: no free-text value exceeds the clamp in the Clamps section of `handoff-format.md`. |
| H-09 | FATAL | Envelope byte bound: the marker-to-marker span does not exceed the clamp in the Clamps section of `handoff-format.md`. |
| H-10 | FATAL | Control characters: no value contains a control character. |
| H-11 | FATAL | Sentinel token inside a field value. |
| H-12 | ADVISORY | Duplicate key: warns; the first occurrence wins per the Version Marker section of `handoff-format.md`. |
| H-13 | FATAL | Envelope placement relative to the sentinel zone, per the Envelope Placement section of `handoff-format.md`. |
| H-14 | ADVISORY | Unknown key: warns; the key is ignored per the Version Marker section of `handoff-format.md`. |
| H-15 | FATAL | Tabular per-row arity: a row's field count must match its block's header field count. |
| H-16 | FATAL | Field ordering, per the Field Ordering section of `handoff-format.md`. |
| H-17 | ADVISORY | Escape conformance, per the Delimiter And Escape section of `handoff-format.md`. |
| H-18 | FATAL | An explicit direction assertion that disagrees with the marker's own direction keyword. |
| H-19 | FATAL | Tabular declared-count agreement: a block's bracketed count must agree with its actual row count, distinct from H-15's per-row arity. |
| H-20 | FATAL | Envelope line shape: every line strictly between the two markers is a column-0 `key: value` line, an indented row of a currently open tabular block, or the close marker itself; blank lines are rejected. |
| H-21 | RECOMMENDED | Consumers should split each envelope line on the delimiter's first occurrence when parsing. Not checkable by any payload; no validator function, no fixture. |

## Rule Interaction Cascade

Some rules remove the domain of other rules when they fire — a validator
must know which rules to skip, or a payload constructed to isolate one
violation will spuriously trip others. Five rule-firings are gating: an open
marker missing entirely (`H-01`, no marker at all); an open marker with no
matching close (`H-01`, unmatched); an unrecognised major version (`H-02`);
an invalid direction keyword (`H-04`); an invalid `status` value (`H-06`).
Every other rule is non-gating — its firing changes no other rule's domain.

A rule is skipped exactly when an input its predicate reads is undefined.
The three such inputs are the envelope span (both markers located), the
version (readable from a well-formed open marker), and the direction-keyed
key table (known-key set, canonical order, and per-status required sets —
all three looked up by direction keyword).

| Rule | No open marker | No close marker | Unknown major | Bad direction | Bad status |
|---|---|---|---|---|---|
| H-01 marker shape | FIRES | FIRES | runs, passes | runs, passes | runs, passes |
| H-02 unknown major | skip (no version) | runs | FIRES, stops | runs | runs |
| H-03 unknown minor | skip (no version) | runs | skip (stopped) | runs | runs |
| H-04 direction keyword | skip (no marker) | runs | skip (stopped) | FIRES | runs |
| H-05 required fields | skip (no span) | skip (no span) | skip (stopped) | skip (no key table) | degrades to direction-level set; per-status branch skipped |
| H-06 status enum | skip (no span) | skip (no span) | skip (stopped) | skip (no key table) | FIRES |
| H-07 verdict verbatim | skip (no span) | skip (no span) | skip (stopped) | skip (no key table) | runs (present-only) |
| H-08 value byte bound | skip (no span) | skip (no span) | skip (stopped) | runs | runs |
| H-09 envelope byte bound | skip (no span) | skip (no span) | skip (stopped) | runs | runs |
| H-10 control characters | skip (no span) | skip (no span) | skip (stopped) | runs | runs |
| H-11 sentinel in value | skip (no span) | skip (no span) | skip (stopped) | runs | runs |
| H-12 duplicate key | skip (no span) | skip (no span) | skip (stopped) | runs (lexical) | runs |
| H-13 envelope placement | skip (no marker) | runs (open-marker-only) | skip (stopped) | runs | runs |
| H-14 unknown key | skip (no span) | skip (no span) | skip (stopped) | skip (no key table) | runs |
| H-15 tabular row arity | skip (no span) | skip (no span) | skip (stopped) | runs (header-relative) | runs |
| H-16 field ordering | skip (no span) | skip (no span) | skip (stopped) | skip (no key table) | runs |
| H-17 escape conformance | skip (no span) | skip (no span) | skip (stopped) | runs (lexical) | runs |
| H-18 direction disagreement | skip (no marker) | runs | skip (stopped) | skip (no marker direction) | runs |
| H-19 tabular declared count | skip (no span) | skip (no span) | skip (stopped) | runs (lexical) | runs |
| H-20 envelope line shape | skip (no span) | skip (no span) | skip (stopped) | runs (lexical) | runs |
| H-21 consumer split rule | n/a — RECOMMENDED, no validator function, never runs | n/a | n/a | n/a | n/a |

A rule that fires still runs to completion; skipping is never partial. The
one exception is `H-05` under an invalid `status`, which degrades to the
direction-level required set rather than either firing or running its full
per-status branch — every other cell in this table is either a plain skip or
a plain run.
