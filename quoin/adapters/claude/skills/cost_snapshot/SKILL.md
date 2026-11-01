---
name: cost_snapshot
description: "Live cost summary: today, project lifetime, per open task. Use for: /cost_snapshot, 'how much have I spent', 'cost report'. Read-only."
model: haiku
---

# Cost Snapshot

You return a live cost summary for the current project. You read cost ledger files to identify sessions, call `ccusage` to get dollar amounts, and print a concise terminal-friendly report.

*Portable intent doc: `quoin/core/skills/cost_snapshot.md`*

## §0 Model dispatch (FIRST STEP — execute before anything else)

This skill is declared `model: haiku`. If the executing agent is running on a model
strictly more expensive than the declared tier, you MUST self-dispatch before doing the
skill's actual work.

Detection:
  - Read your current model from the system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus.
  - Sentinel parsing: the user's prompt is checked for the `[no-redispatch]` family.
      * Bare `[no-redispatch]` (parent-emit form AND user manual override): skip dispatch, proceed to §1 at the current tier.
      * Counter form `[no-redispatch:N]` where N is a positive integer ≥ 2: ABORT (see "Abort rule" below).
      * Counter form `[no-redispatch:1]` is reserved and treated as bare `[no-redispatch]` for forward-compatibility; do not emit it.
  - If current_tier > declared_tier AND prompt does NOT start with any `[no-redispatch]` form:
      Dispatch reason: cost-guardrail handoff. dispatched-tier: haiku.
<!-- §0-1m-decide-begin -->
Pre-dispatch 1M check (IVG-90 Layer 1+2):
  - Run: python3 __QUOIN_HOME__/scripts/dispatch_config.py --decide --tier <declared_tier> --verbose
    where <declared_tier> is the tier declared for this skill (e.g. "sonnet" or "haiku",
    as shown in the dispatched-tier line immediately above).
  - If the command returns "safe-path" on line 1:
      Read the reason token from line 2 (config|cache|probe).
      Emit the one-line advisory (verbatim, substituting <reason> with the line-2 token):
        `[quoin: 1M-unsafe declared-tier per <reason>; running SAFE PATH without dispatch]`
      Then proceed to §1/§0c at the current tier (treat as if [no-redispatch] were present).
      Do NOT call the Agent dispatch. Do NOT call AskUserQuestion.
  - If the command returns "dispatch" on line 1, OR if the script is missing / errors:
      Continue to the Agent dispatch call below (today's path — fail-OPEN).
<!-- §0-1m-decide-end -->
      Spawn an Agent subagent with the following arguments:
        model: "haiku"
        description: "cost_snapshot dispatched at haiku tier"
        prompt: "[no-redispatch]\n<original user input verbatim>"
      Wait for the subagent.
<!-- §0-1m-cachewrite-begin -->
      Cache the safe result (best-effort):
        python3 __QUOIN_HOME__/scripts/dispatch_config.py --write-cache --tier <declared_tier> --result safe
      (Fail-OPEN: if the script errors or is missing, silently skip and continue.)
<!-- §0-1m-cachewrite-end -->
      Return its output as your final response. STOP.
      (Return the subagent's output as your final response.)

Abort rule (recursion guard):
  - If the prompt starts with `[no-redispatch:N]` AND N ≥ 2: ABORT before any tool calls.
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in cost_snapshot. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /cost_snapshot`).
  - Why this is safe to share syntax with the parent-emit form: memory/dispatch-guide.md §0 verbose reference ("Why the bare [no-redispatch] sentinel is dual-source by design").
  - Use this only when intentionally overriding the cost guardrail (e.g., for one-off debugging on a different tier).

<!-- §0-worktree-fallback-begin -->
Fail-graceful path with error-class triage (per architecture I-01):
  - If the Agent tool returns an error during dispatch, classify the error
    message text BEFORE proceeding:

  - Error classification:
      * Worktree-class: the error text contains the substring
        `Cannot create agent worktree`, OR (the substring `worktree` AND
        the substring `not in a git repository`). This is recoverable —
        the harness tried to create a git worktree for isolation and the
        project root is not a git repo. Continue to Worktree-class branch.
      * Other-class: any other tool error, exception, or harness rejection
        — skip to Other-class path below (existing fail-OPEN behavior).

  - 1M-credit-class: if the error text contains the substring
      `Usage credits required for 1M context`:
      This is the 1M-context credit mismatch (IVG-89). The parent session carries
      the `context-1m-2025-08-07` beta header which propagates to all subagent calls;
      the declared-tier model lacks 1M credits. Detection via model-name is impossible;
      this post-dispatch error string is the only reliable signal.
      Emit (verbatim):
        `[quoin: 1M-context credit mismatch on <tier> subagent dispatch; proceeding in-session at parent tier — run /model to switch this session to standard context for a permanent fix]`
<!-- §0-1m-cachewrite-begin -->
      Cache the unsafe result (best-effort):
        python3 __QUOIN_HOME__/scripts/dispatch_config.py --write-cache --tier <declared_tier> --result unsafe
      (Fail-OPEN: if the script errors or is missing, silently skip and continue.)
<!-- §0-1m-cachewrite-end -->
      Then proceed to §1 at the current tier (treat as if `[no-redispatch]` were present).
      Do NOT retry the Agent dispatch. Do NOT call AskUserQuestion.


  - Worktree-class branch:
      Autonomous fail-OPEN (checked FIRST): if the incoming prompt carries
      the `[autonomous]` sentinel, then on this worktree-class dispatch
      error, proceed at current tier fail-OPEN and do NOT call
      AskUserQuestion — skip straight to the Other-class path below (it
      emits the bare warning and the `error-class=worktree` classification
      line), then proceed to §1 at the current tier. Otherwise (no
      `[autonomous]` sentinel — non-autonomous behavior unchanged):
      Worktree creation is hook-driven and cannot be skipped by omitting a
      parameter. Use the AskUserQuestion tool to present the user with one
      option:
        (c) `proceed-current-tier` — Skip dispatch, proceed at the current
            (more expensive) tier. This is the only available recovery path.
      Question header: `Subagent dispatch failed (worktree creation). Proceeding at current tier.`
      Note for the user: "Worktree dispatch failed and no retry mechanism
      is available — worktree creation is unconditional in this harness.
      Proceeding at current tier."

  - Other-class path (also: worktree-class after user acknowledges c):
      Do NOT abort the user's invocation.
      Emit the bare warning (verbatim):
        `[quoin-stage-1: subagent dispatch unavailable; proceeding at current tier]`
      If this path was reached via a worktree-class error, ALSO emit the
      classification line (second, separate):
        `[quoin-stage-1: error-class=worktree; user-choice=c; proceeding at current tier]`
      Then proceed to §1 at the current tier (fail-OPEN per I-01).
<!-- §0-worktree-fallback-end -->
Otherwise (already at or below declared tier, OR prompt has [no-redispatch] sentinel, OR dispatch unavailable): proceed to §1 (skill body).
<!-- §0-end -->

## Session bootstrap

If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

Cost tracking (conditional): `/cost_snapshot` is a read-only reporting skill. Append to the cost ledger only if a task context is clearly active (e.g., you were invoked mid-task and the task name is unambiguous). If in doubt, skip cost recording. If the condition holds: append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `cost-snapshot` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`.

<!-- quoin:ledger-self-write -->

## Process

Pricing parity: this skill (and `cost_from_jsonl.py` fallback) does NOT deduplicate ledger or JSONL entries by message.id — this is intentional ccusage parity (verified 2026-04-27 against ccusage v18.0.11). See `cost_from_jsonl.py:102` for the same note inline.

### Step 1: Collect ledger data

Determine the project root (the directory containing `.workflow_artifacts/`). Then:

- **Active tasks:** scan `.workflow_artifacts/*/cost-ledger.md` (non-finalized task folders)
- **Finalized tasks:** scan `.workflow_artifacts/finalized/*/cost-ledger.md`

For each ledger file found, parse every data line (skip lines starting with `#` and blank lines). Split each line on `|` (bare pipe, NOT ` | `), strip each field. Require at least 6 fields.

- **6 fields** — valid; `fallback_fires=0`, `attribution=""` (legacy, pre-Stage-4).
- **Exactly 7 fields** — take the 7th as `fallback_fires` (parse as int; on parse failure treat as `0` and emit stderr WARN `cost_snapshot.WARN: malformed fallback_fires column at <ledger>:<lineno>`); `attribution=""`.
- **Exactly 8 fields** — take the 7th as `fallback_fires` (same parsing) and the 8th as `attribution` (first-class, Stage-4-reader-partitioning+). An empty 8th field is equivalent to no col 8 (`attribution=""`).
- **9 or more fields** — take cols 7 and 8 as `fallback_fires`/`attribution`; ignore any columns beyond the 8th with a stderr WARN: `cost_snapshot.WARN: extra columns at <ledger>:<lineno> (expected ≤8)`.

The format is:

```
<uuid> | <date> | <phase> | <model> | <category> | <notes> [| <fallback_fires> [| <attribution>]]
```

The 7th column (`fallback_fires`) is OPTIONAL (Stage 4+ only); the 8th column (`attribution`) is OPTIONAL (Stage-4-reader-partitioning+ only). 6-column rows are always valid with `fallback_fires=0`, `attribution=""`.

**Inline-first precedence rule** (per row, applied once `attribution` is extracted — mirrors the core `classify_attribution()` verdict used by the Python readers):
- Col 8 present, carries a parseable `usd`, AND `src` ≠ `unresolved` → the row is **resolved**: use the inline `usd` directly for that UUID and SKIP the ccusage/`cost_from_jsonl.py` JSONL lookup for it (Step 2 excludes resolved-inline UUIDs from the lookup set — see below).
- Col 8 present but `src=unresolved` (or no usable `usd`) → the row is **unresolvable**: count it explicitly (see Step 3); it contributes NOTHING to any total — never fold it into $0.
- Col 8 empty (`attribution=""`) → **legacy**: fall through to the existing UUID→JSONL resolution (Step 2/3), unchanged; a lookup failure here is also counted as unresolvable (Step 3).

Keep the existing `unknown-`-prefixed UUID skip filter (Step 2) as-is — it is orthogonal to this precedence rule (those are fallback entries with no real session at all, not unresolvable col-8 rows).

Build three collections:

- **`today_entries`** — entries where `date` matches today's date (YYYY-MM-DD), from ALL ledgers
- **`all_entries`** — every entry from ALL ledgers (active + finalized), deduplicated by UUID
- **`open_task_entries`** — entries grouped by task name, from active (non-finalized) ledgers only

Also scan today's session-state files at `.workflow_artifacts/memory/sessions/<today>-*.md` for each active task. For each file, read the `## Cost` block and extract the `fallback_fires:` field via regex `^- fallback_fires:\s*(\d+)\s*$`. Sum per task. Store as **`today_fallback_by_task`** (task-name → int). Sessions lacking the `fallback_fires` line (pre-Stage-4) are treated as 0 — no warning emitted.

If no ledger files are found anywhere, print:

```
No cost ledgers found.
Cost tracking starts when skills record sessions to .workflow_artifacts/<task>/cost-ledger.md
```

Then stop.

### Step 2: Run ccusage for each unique UUID

Collect all unique UUIDs from all three collections, THEN build the JSONL-lookup set by EXCLUDING two kinds of UUIDs:
- any UUID starting with `unknown-` (fallback entries with no real session to look up — unchanged, existing behavior);
- any UUID whose row(s) classified as **resolved** under the Step-1 precedence rule (its cost is already known from the inline col-8 `usd` — sending it to ccusage/`cost_from_jsonl.py` is unnecessary and, for on-behalf rows carrying `uuid=<agentId>` with no top-level `<agentId>.jsonl`, would be a guaranteed-failed lookup that risks double-representation: once inline, once in the unresolvable bucket).

So: **JSONL-lookup set = all unique UUIDs − resolved-inline UUIDs − `unknown-`-prefixed UUIDs.** Only this reduced set is sent to ccusage/`cost_from_jsonl.py` below.

**For fewer than 5 unique UUIDs** (in the JSONL-lookup set), run sequentially with a 15-second timeout per call:

```bash
timeout 15 npx ccusage session -i <UUID> --json
```

**For 5 or more unique UUIDs**, use a single bulk call to reduce overhead:

```bash
timeout 30 npx ccusage session --json --since <earliest-date-across-all-entries>
```

Then filter the returned results to only the UUIDs present in your collections.

**Parsing ccusage JSON responses:**

For **per-UUID calls** (`-i UUID`), the response is a single object — parse directly:
  `{"sessionId": "...", "totalCost": 1.23, "totalTokens": 123456, "entries": [...]}`.
  Key: `sessionId` → UUID; `totalCost` → cost.

For **bulk calls** (`--since DATE`), ccusage v20+ returns a top-level wrapper:
  `{"session": [{"period": "UUID", "totalCost": 1.23, "modelBreakdowns": [...], ...}, ...], "totals": {...}}`.
  The array is under key `session` (NOT `sessions`). Each element's UUID is in `period` (NOT `sessionId`).
  If instead the response is a bare array or has a `sessions` key (v18 shape), parse using `sessionId`.
  Version-detection: check for presence of top-level `session` key (array) → v20 path;
  otherwise fall back to v18 path (array elements have `sessionId`).

Extract `totalCost` per UUID from whichever shape is detected. Filter to only UUIDs present in your collections.

If `npx` or `ccusage` is not available (binary not found), OR every ccusage
call returns non-zero, fall back to `cost_from_jsonl.py`:

  # Per-UUID mode (parallel with the ccusage `-i UUID --json` path):
  python3 __QUOIN_HOME__/scripts/cost_from_jsonl.py session -i UUID --json

  # Bulk mode (parallel with `ccusage session --since DATE --json`):
  python3 __QUOIN_HOME__/scripts/cost_from_jsonl.py session --json --since DATE

The output JSON shape is identical to ccusage (see /cost_snapshot Step 2
parser). Parse it the same way.

Before printing the cost summary in Step 3, prepend ONE line of context:

  [fallback: cost_from_jsonl.py — prices as of LAST_UPDATED]

Read LAST_UPDATED from the script via:
  python3 -c "from pathlib import Path; import sys; sys.path.insert(0, str(Path.home() / '.claude' / 'scripts')); \
    import cost_from_jsonl; print(cost_from_jsonl.LAST_UPDATED)"

If even the fallback fails (script missing OR exit code 1 on all UUIDs),
print:
  cost tracking unavailable — neither ccusage nor cost_from_jsonl.py
  could resolve session costs. Session counts: N total across M tasks
Then stop.

For individual call timeouts or errors, record cost as `null` for that UUID and continue — do not abort.

## §V Reconcile (read-only — no side-effect check)

<!-- §V-reconcile-begin -->
Before surfacing any task/PR status, run `python3 __QUOIN_HOME__/scripts/verify_claims.py --reconcile-tasks --project-root <project-root>` and derive the displayed status from the reconcile table, not from a cached narrative alone. If the reconcile exits 8, surface the contradiction rather than silently reporting the narrative version.
<!-- §V-reconcile-end -->

### Step 3: Print summary

Using the UUID-to-cost map from Step 2 PLUS the resolved-inline `usd` values from Step 1, compute per scope (today / lifetime / per-open-task):

- **`resolved_total`** — sum of (a) resolved-inline `usd` (Step-1 precedence rule) and (b) successfully-resolved JSONL costs (legacy rows, Step 2). This is the trustworthy total — it NEVER includes an unresolvable row as $0.
- **`unresolvable_count`** — count of (a) col-8 **unresolvable** rows (Step 1) PLUS (b) legacy rows whose JSONL lookup failed, timed out, or returned null (skip `unknown-`-prefixed UUIDs, per Step 2 — those were never in the lookup set to begin with). This single counter REPLACES the old "sessions with unknown cost" line — do not maintain two competing counters for the same idea; both failure modes are the same "cost not trustworthy for this row" fact.
- **Per-task dedup (IVG-157):** a UUID lives in exactly one task, but a task's rows can share one UUID more than once (e.g. an inline `/checkpoint` writes the same UUID as the phase it ran inside of). When summing an **`open_task_entries`** total for a task, dedup the resolved-cost contribution by UUID **within that task** before summing — mirror the `all_entries` dedup-by-UUID rule above (Step 1) so a shared UUID's cost is added to that task's total exactly ONCE, not once per row/phase sharing it. Session counts for the task may still count every row (they describe activity, not cost); only the dollar sum needs this dedup.

Print in this format:

```
Cost Snapshot — <YYYY-MM-DD>

Today:            $X.XX  (<N> sessions)
Project lifetime: $X.XX  (<N> sessions, <M> tasks)

Open tasks:
  <task-name-1>    $X.XX  (<N> sessions)
  <task-name-2>    $X.XX  (<N> sessions)

[<K> sessions unresolvable — col-8 marked unresolved, or ccusage/JSONL lookup failed/timed out]
```

Each `$X.XX` total is `resolved_total` for that scope. When the corresponding `unresolvable_count > 0`, prefix the total with `~` and append `(partial)` — e.g. `~$X.XX (partial)` — so a partial total is never rendered as if it were exact. Never fold an unresolvable row into $0 to keep a total looking precise.

When today's fallback total (from `today_fallback_by_task`) is > 0 for a task, append ` (<K> fallback fires today)` after the session count for that task in the "Open tasks" block. When 0, no marker is shown. Similarly, if the lifetime 7th-column sum across all ledgers is > 0, append ` (<K> fallback fires)` after the lifetime session count. If today's total fallback fires across all tasks is > 0, append ` (<K> fallback fires today)` after the Today session count. Never print fallback-fire markers when the count is 0.

Formatting rules:
- Right-align the dollar amounts (pad task names to consistent width)
- Omit the "Open tasks" section entirely if there are no active tasks
- Omit the `[K sessions unresolvable]` line if `unresolvable_count == 0` across all scopes
- Show `$0.00` if a total is zero (not blank) — a genuine resolved $0 is shown as `$0.00`, never omitted

## Important behaviors

- **Read-only.** Never write files (except optionally appending to the cost ledger per bootstrap rules). This is a reporting tool only.
- **Fast.** Aim for under 30 seconds. Use the bulk ccusage call when 5+ UUIDs are needed.
- **Graceful degradation.** If ccusage fails or is unavailable, print what you can (session counts, task names) with a clear explanation of what's missing. Do not error out silently.
- **No double-counting.** Deduplicate UUIDs before summing. A UUID appearing in both active and finalized ledgers (shouldn't happen, but possible) counts only once toward the lifetime total. Within a single task's total, a UUID shared by more than one row (e.g. an inline `/checkpoint`) also counts only once (see Step 3's per-task dedup note) — never once per participating phase.
- **Project root detection.** If invoked from a subdirectory, walk up to find the directory containing `.workflow_artifacts/`. If not found, tell the user and stop.
