# Agent Handoff Format

Quoin defines one envelope family for inter-agent handoff: a dispatch
envelope (orchestrator to subagent) and a return envelope (subagent to
orchestrator), sharing one version marker and validated by one canonical
validator, `quoin/core/scripts/handoff_validate.py` (portable wrapper at
`quoin/scripts/handoff_validate.py`). A reader of this file alone can
determine required/optional fields, ordering, delimiters, escaping, byte
bounds, and version-marker rules for either direction.

This is the run orchestrator's phase-dispatch/phase-return format — not the
Codex session continuation handoff (`quoin/adapters/codex/handoff.md`),
which hands one Codex session off to its successor via an artifact under
`.workflow_artifacts/memory/sessions/`.

## Envelope Placement

A payload has three parts in order: a sentinel prefix zone, the envelope,
and free-text prose. The sentinel zone is the maximal leading run of
whitespace and whitespace-separated bracket tokens matching
`\[[^\]\n]*\]` (e.g. `[no-redispatch]`), computed without reference to
the marker. It ends at the first token beginning `[quoin-handoff/` (the
open marker, excluded) or the first non-whitespace, non-token character.

The open marker is the first `[quoin-handoff/...]` token; the close
marker, `[/quoin-handoff]`, is its match — everything strictly between
them is the envelope, and byte bounds apply only to this span, never the
whole payload. The close marker is recognised only on a line of its own;
a close-marker-shaped substring elsewhere on a line does not terminate
the envelope.

Placement is normative: text between the sentinel zone and the open
marker must be whitespace only, and the open marker must begin its own
line.

## Version Marker

The marker is `quoin-handoff/<major>.<minor>`, never a bare major.

- Unrecognised **major**: ignore the envelope, fall back to free-form
  handling — fail-open; a handoff envelope is an optimization, never a
  correctness dependency.
- Unrecognised **minor** in a recognised major: process normally;
  evaluation continues.
- Unknown **keys** in a recognised major/minor are ignored, never
  rejected. Duplicate keys: first occurrence wins.

A caller may pass an expected direction as an assertion; omitted, the
marker's own keyword (`dispatch`/`return`) governs. Disagreeing, that is
its own violation — the assertion never overrides the marker.

## Dispatch Envelope

Orchestrator to subagent. Required: `skill`, `task`, `task_dir`,
`project_root`, `return` (minor 1 drops `project_root`; `task_dir` then
ABSOLUTE). Optional: `profile`, `inputs`, `spec`, `bundle[N]` (tabular
sub-form, see below).

Canonical field order (known-key set): `skill`, `task`, `task_dir`,
`project_root`, `profile`, `inputs`, `return`, `spec`, `bundle`.

```text
[no-redispatch] [autonomous] [quoin-onbehalf]
[quoin-handoff/1.0 dispatch]
skill: architect
task: my-task
task_dir: /abs/project/.workflow_artifacts/my-task/
project_root: /abs/project
profile: Large
inputs: spec.md | architecture.md
return: envelope
spec: ${CLAUDE_PLUGIN_ROOT}/core/workflow/handoff-format.md
[/quoin-handoff]
<task-specific prose, unchanged>
```

## Return Envelope

Subagent to orchestrator. `status` is the discriminating field: one of
`COMPLETE`, `PARTIAL`, `NEEDS-DECISION`, `BLOCKED` — each with its own
required-field set (Status Required Fields below).

Known-key set/field order (Field Ordering below): `status`, `artifact`,
`verdict`, `summary`, `checkpoint`, `phase`, `reason`, `remaining`,
`resume_hint`, `artifacts`.

`NEEDS-DECISION`/`BLOCKED` are reserved for a subagent's genuine inability
to proceed; a fail-closed hard stop instead emits the shipped
`gate-result: NEEDS-DECISION` block IN PLACE of any envelope there — never
alongside it.

Complete:

```text
[quoin-handoff/1.0 return]
status: COMPLETE
artifact: /abs/path/architecture.md
verdict: PASS
summary: <clamped, one line, <=600 B>
[/quoin-handoff]
```

Partial:

```text
[quoin-handoff/1.0 return]
status: PARTIAL
checkpoint: /abs/path/checkpoint-<sid>.md
phase: architect
remaining: <short, one line>
resume_hint: <short, one line>
[/quoin-handoff]
```

Needs-decision:

```text
[quoin-handoff/1.0 return]
status: NEEDS-DECISION
checkpoint: /abs/path/checkpoint-<sid>.md
phase: implement
reason: <short, one line>
resume_hint: <short, one line>
[/quoin-handoff]
```

Blocked:

```text
[quoin-handoff/1.0 return]
status: BLOCKED
phase: review
reason: <short, one line>
resume_hint: <short, one line>
[/quoin-handoff]
```

## Status Required Fields

The required- and optional-field matrix for both directions — the primary
reference for what a conforming payload must and may carry.

| Direction | Status | Required | Optional |
|---|---|---|---|
| dispatch | n/a | `skill`, `task`, `task_dir`, `project_root`, `return` | `profile`, `inputs`, `spec`, `bundle[N]` |
| return | `COMPLETE` | `status`, `artifact`, `verdict`, `summary` | `artifacts[N]` |
| return | `PARTIAL` | `status`, `checkpoint`, `phase`, `remaining`, `resume_hint` | `summary`, `artifacts[N]` |
| return | `NEEDS-DECISION` | `status`, `phase`, `reason`, `resume_hint`, and at least one of `checkpoint` or `artifact` | `summary`, `verdict`, `artifacts[N]` |
| return | `BLOCKED` | `status`, `phase`, `reason`, `resume_hint` | `artifact`, `checkpoint`, `summary`, `verdict`, `artifacts[N]` |

`verdict` is optional wherever it appears and checked only when present —
an omitted required field is a required-field violation, not a verdict
one (dispatch `project_root`: minor 0 only).

## Status And Verdict Vocabulary

`status` enum, exactly four members: `COMPLETE`, `PARTIAL`,
`NEEDS-DECISION`, `BLOCKED`.

`verdict` vocabulary, verbatim, exactly five members: `PASS`, `REVISE`,
`APPROVED`, `CHANGES_REQUESTED`, `BLOCKED`. These strings are load-bearing
— downstream grep contracts match them verbatim.

## Field Ordering

One canonical list per direction (given above), evaluated over
recognised keys' first occurrences only — unknown and repeated keys are
ignored for ordering. A payload's fields appear as a subsequence of its
direction's list: not every field is required, but present fields must
appear in that relative order.

## Tabular Sub-form

A single record is non-uniform, so it stays in `key: value` form.
Repeated rows sharing a field set use a tabular sub-form instead.

A block opens on a column-0 key matching `^[a-z_][a-z0-9_]*\[\d+\]$`
(e.g. `artifacts[3]`), whose value is a header naming the fields in
order, list-delimited (Delimiter And Escape). Rows are the following
indented lines — indentation discriminates a row from the next key —
same field count/delimiter as the header. The block closes at the next
column-0 `key: value` line, or the close marker.

```text
artifacts[3]: path | kind | status
  /abs/current-plan.md | plan | COMPLETE
  /abs/review-1.md | review | COMPLETE
  /abs/gate-implement.md | gate | NEEDS-DECISION
```

The bracketed count must match the actual row count and is stripped
before the known-key/ordering lookups, so `artifacts[3]`/`artifacts[5]`
are one key, one canonical-order slot. Return declares optional
`artifacts[N]` (`path | kind | status`); dispatch declares optional
`bundle[N]` (`path | summary`). Its list delimiter is structural, not
list-value — see Delimiter And Escape.

## Clamps

Every free-text value, including `summary`, is clamped to 600 bytes. The
whole envelope — the marker-to-marker span defined in Envelope Placement
— is clamped to 1,024 bytes. Both are design bounds, not derived from any
pre-migration payload size.

## Delimiter And Escape

The field delimiter is `: ` on its first occurrence only, so a value may
contain colons. List values use the list delimiter (a pipe, single-space
bounded) with a one-way escape (a broken-bar character, single-space
bounded) for a literal delimiter inside a value; consumers split on the
first occurrence only.

Dispatch `inputs` is the only list-valued key today; its delimiter is
never escape-checked inside it — every other known key is scalar. A
tabular header (e.g. `artifacts[3]: path | kind | status`) is exempted
like a tabular row: its list delimiter is structural, out of scope for
escape checking. An unescaped list delimiter elsewhere (a
non-list-valued, non-tabular value) is a conformance violation, as is a
value carrying the escape sequence itself (one-way — it can never
legitimately appear in source text).

Values are control-character-free (stripped) and checked against the
sentinel-token probe reused from the bundle convention — no
sentinel-style bracket token, including the envelope's own markers, may
appear inside a value. Reused from `quoin/scripts/context_bundle.py`
rather than reinvented; only that script's byte clamp is not reused (see
Clamps above).

## Reference

The checkable-rule table (`H-01` through `H-21`) and the rule interaction cascade move to a companion file: `${CLAUDE_PLUGIN_ROOT}/core/workflow/handoff-format-reference.md`.
