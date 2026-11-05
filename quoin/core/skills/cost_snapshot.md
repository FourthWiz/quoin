# cost_snapshot

Runtime-neutral intent for the cost_snapshot skill. Any runtime adapter (Claude,
Codex, …) that implements this skill should match the contract described here.

## Purpose

Return a read-only live cost summary for the current project — today's total,
project lifetime total, and a per-open-task breakdown. Never writes file artifacts
(except conditionally appending a cost-ledger row when a task context is
unambiguously active). Never invokes another workflow phase. Never commits or
modifies source.

## When to use

- Any time the user asks for a cost report.
- User says "how much have I spent", "cost report", "show costs",
  "what's the project cost", "how much has this task cost", or "cost snapshot".

## Inputs

- All active task ledgers at `.workflow_artifacts/<task-name>/cost-ledger.md`
  (every non-finalized task directory under `.workflow_artifacts/`).
- All finalized task ledgers at
  `.workflow_artifacts/finalized/<task-name>/cost-ledger.md`.
- All of today's session-state files under
  `.workflow_artifacts/memory/sessions/<today>-*.md` (advisory; sourced for the
  per-task `fallback_fires` aggregate when present).
- A runtime-provided cost-resolution mechanism that maps session UUIDs to dollar
  costs. Its specific name, CLI, JSONL format, model-pricing source, and fallback
  chain are adapter-owned and explicitly out of scope here.

## Output

A terminal-rendered report (chat message) showing:
- Today's cost.
- Project lifetime cost.
- Per-open-task cost (when active tasks exist).
- A `fallback_fires` marker (when greater than zero for any task or lifetime total).
- A per-stage breakdown under an open task whose ledger has rows tagged
  with a stage (parallel stage runs tag their rows this way).

No file artifacts are produced. The exact textual layout is not strictly
contractual, but adapters SHOULD mirror the canonical four-block shape: heading
line with date; "Today" line; "Project lifetime" line; optional "Open tasks" block;
optional trailing note about sessions with unknown cost.

## Behavior contract

- **Read-only.** The skill MUST NOT write any file artifact other than the
  conditional cost-ledger row described in the Inputs section. The conditional
  row is written only when a task context is unambiguously active; when in doubt,
  the skill MUST skip the write.
- **Project-root detection.** The skill MUST walk up from the invocation directory
  to find the directory containing `.workflow_artifacts/`. If not found, the skill
  MUST tell the user and stop without partial output.
- **Ledger parsing.** Each ledger file is parsed line-by-line; lines starting with
  `#` and blank lines are skipped; data lines are split on the bare pipe (NOT ` | `
  with surrounding whitespace as the delimiter — fields are stripped after splitting
  on `|`); at least six fields are required; the seventh field, when present, is a
  non-negative integer `fallback_fires` count.
- **Six-column row tolerance.** Six-column rows are valid forever (read-side
  invariant per `quoin/core/workflow/cost-ledger.md`); the seventh column is
  optional. Malformed seventh fields MUST be treated as `0` with a warning, not as
  a fatal error.
- **UUID dedup.** A UUID appearing in both active and finalized ledgers MUST
  contribute to the lifetime total exactly once. UUIDs of the form `unknown-*`
  MUST be skipped from cost lookup (they are placeholder rows representing sessions
  with no resolvable underlying ID).
- **`fallback_fires` aggregation.** When the per-task today sum is greater than
  zero, the rendered report MUST surface it; when the lifetime sum across all
  ledgers is greater than zero, the rendered report MUST surface it; when both are
  zero, the markers MUST be omitted. Sessions lacking the `fallback_fires` field
  MUST be treated as zero without warning.
- **Graceful degradation.** The skill MUST tolerate: (a) ledger files that are
  missing or unreadable — treat as zero data; (b) the per-runtime cost-resolver
  being unavailable or returning an error — treat as `unknown` and continue;
  (c) timeouts on individual cost-resolver calls — record `null` for the affected
  UUID and continue. The skill MUST NOT abort the user's invocation when a partial
  result is producible.
- **Speed budget.** Rendering the report SHOULD complete in well under 30 seconds.
  Adapters MAY use bulk-lookup optimizations to stay within this budget.
- **No push or commit.** The skill MUST NOT push, commit, or modify source files.
- **No phase invocation.** The skill MUST NOT auto-invoke another workflow phase.

## Out of scope

- Model tier and self-dispatch grammar — the runtime adapter handles these.
- The §0 self-dispatch mechanism — runtime-specific.
- The specific cost-resolution CLI or library invoked (a third-party
  cost-reporting tool, a runtime-shipped JSONL parser, a pricing fallback table —
  all adapter-owned).
- Per-runtime session-ID discovery (different runtimes use different mechanisms to
  map a session to a resolvable identifier).
- The exact pricing model and any pricing-provenance note prepended in fallback
  mode.
- The specific shape of the cost-ledger row beyond "append-only, 6-or-7-column,
  bare-pipe-separated" (row shape is documented in
  `quoin/core/workflow/cost-ledger.md`).
- Any guarantee about message-level dedup within a session (that is an
  adapter-specific implementation detail).

## Notes

- The rendered-report section set is open by design (some adapters may emit
  additional advisory lines), but adapters SHOULD include at minimum the Today
  total, the Project lifetime total, and the Open tasks block when there are active
  tasks.
- The `.workflow_artifacts/<task-name>/` path pattern is the canonical location for
  per-task artifacts; the skill reads each active task's cost-ledger file there.
- Cost-ledger writes by this skill itself are conditional: only when a task context
  is unambiguously named by the user or unambiguously implied by an active
  session-state file (mirrors `end_of_day` and `weekly_review` cost-ledger
  semantics).
