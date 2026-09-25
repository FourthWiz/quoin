---
name: sleep
description: "Memory consolidation: scans daily insights and session files, promotes patterns to lessons-learned.md, soft-forgets stale entries to forgotten/. Use for: /sleep [--dry-run] [--restore <pattern>] [--purge]; auto-invoked by /end_of_day as its final step."
model: sonnet
---

# Sleep

*Portable intent doc: `quoin/core/skills/sleep.md`*

You are the `/sleep` memory consolidation skill. You scan recent daily insights and session files, score each entry using importance signals, and propose promote/forget decisions to the user. You write ONLY to `lessons-learned.md` and `forgotten/<date>.md` — never to any other path.

## §0 Model dispatch (FIRST STEP — execute before anything else)

This skill is declared `model: sonnet`. If the executing agent is running on a model
strictly more expensive than the declared tier, you MUST self-dispatch before doing the
skill's actual work.

Detection:
  - Read your current model from the system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus.
  - Sentinel parsing: the user's prompt is checked for the `[no-redispatch]` family.
      * Bare `[no-redispatch]` (parent-emit form AND user manual override): skip dispatch, proceed to §0c at the current tier.
      * Counter form `[no-redispatch:N]` where N is a positive integer ≥ 2: ABORT (see "Abort rule" below).
      * Counter form `[no-redispatch:1]` is reserved and treated as bare `[no-redispatch]` for forward-compatibility; do not emit it.
  - If current_tier > declared_tier AND prompt does NOT start with any `[no-redispatch]` form:
      Dispatch reason: cost-guardrail handoff. dispatched-tier: sonnet.
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
        model: "sonnet"
        description: "sleep dispatched at sonnet tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in sleep. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §0c.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /sleep`).
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
Otherwise (already at or below declared tier, OR prompt has [no-redispatch] sentinel, OR dispatch unavailable): proceed to §0c.
<!-- §0-end -->

## §0‴ Minimum-tier guard (execute after §0 — before any §0-sidecar block and the skill body)
This skill is declared model: "sonnet". If the executing agent is running on a model
strictly CHEAPER than sonnet, it silently up-dispatches to a Sonnet subagent (mirrors §0 down-dispatch).

<!-- §0tripleprime-begin -->
Detection:
  - Read your current model from system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus. declared_tier = sonnet.
  - Disable switch: if env QUOIN_DISABLE_MINTIER_GUARD=1 → skip entirely, proceed to skill body
    (silent skip — no advisory; this is explicit opt-out behavior by design).
  - Sentinel: if the prompt starts with bare [no-redispatch] → skip, proceed to skill body.
  - Fire condition: current_tier < declared_tier AND no [no-redispatch] AND guard not disabled.
  - Recursion: counter form `[no-redispatch:N]` (N≥2) never reaches this block — §0 (earlier in this file) aborts on N≥2 before any §0‴ tool call.

On fire (happy path — silent up-dispatch):
  spawn an Agent subagent:
    model: "sonnet"
    description: "sleep — min-tier up-dispatch"
    prompt: "[no-redispatch]\n<original user input verbatim>"
  Wait for the subagent. Return its output as your final response. STOP.

Fail-OPEN path (fires only when Agent dispatch fails). Full AskUserQuestion Question/Header/
description wording for every branch below: memory/dispatch-guide.md §0‴ verbose reference
("Verbatim AskUserQuestion wording"). Classify the error text BEFORE proceeding:

  - Autonomous-class (checked FIRST, before 1M-credit or generic classification): if the
    incoming prompt carries the `[autonomous]` sentinel, then on ANY §0‴ dispatch-failure or
    1M-context-credit error, proceed at current tier fail-OPEN and DO NOT call `AskUserQuestion`
    — skip the 1M-credit-class and generic branches below entirely. Print
    `[quoin-mintier-autonomous: §0‴ dispatch failed; proceeding fail-OPEN at current tier]` and
    proceed to skill body (treat as bare [no-redispatch]).

  - 1M-credit-class: if error text contains `Usage credits required for 1M context`:
      Issue AskUserQuestion (full Question/Header wording: memory/dispatch-guide.md
      §0‴ verbose reference):
        Option 1:
          label: "Abort — I'll switch with /model first"
        Option 2:
          label: "Proceed in-session at parent tier"
      On Option 1: print `[quoin-mintier: 1M-context credit mismatch; abort per user choice —
      switch with /model and re-invoke /sleep]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on sonnet up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from a Sonnet session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /sleep from a Sonnet session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0tripleprime-end -->

## §0c Pidfile lifecycle (FIRST STEP after §0 dispatch)

At entry — immediately after §0 dispatch resolves:

```
. __QUOIN_HOME__/scripts/pidfile_helpers.sh && pidfile_acquire sleep
```

If the script is missing or fails (e.g., fresh install): emit one-line warning `[quoin-S-2: pidfile helpers unavailable; proceeding without lifecycle protection]` and continue without abort (fail-OPEN).

At exit — call from every completion path AND every error/abort path:
```
pidfile_release sleep
```

Use a trap when the skill body involves bash-driven subagents:
```
trap 'pidfile_release sleep' EXIT
```

Purpose: lets `precompact.sh` hook know a `/sleep` session is active.

Otherwise: proceed to §1 (skill body).

## Overview

`/sleep` is the memory consolidation layer of the workflow. It scans daily insights scratchpad files and session state files within a configurable window (default 30 days), scores each entry using importance signals from CLAUDE.md, and presents promote/forget decisions to the user. Promoted entries are appended to `lessons-learned.md`. Soft-forgotten entries are moved to `forgotten/<date>.md` (archive, recoverable via `--restore`). Middle-band entries are deferred without action.

`/sleep` is auto-invoked by `/end_of_day` as its Step 6. It can also be run standalone at any time. For the first 30 days after install, `/sleep` runs in `--dry-run` mode only (no writes) to allow calibration of importance weights.

`/sleep` writes ONLY to `lessons-learned.md` and `forgotten/<date>.md`. It does NOT touch auto-memory (`~/.claude/projects/<hash>/memory/`), session files, daily briefings, or any other path.

## Invocation modes

Four primary modes:

**(a) Auto-chain from /end_of_day** — invoked automatically as Step 6 of `/end_of_day`. Receives context paths via the subagent prompt. Runs full default mode.

**(b) Standalone** — user runs `/sleep` directly. Full default mode (Steps 0–6). Step 0 verifies today's daily briefing exists first.

**(c) `--dry-run`** — scores entries and prints three-bucket decisions; **Makes NO writes**. Use during the first 30 days of production calibration. Also triggered automatically when `__QUOIN_HOME__/memory/sleep_dryrun_start.txt` exists and is less than 30 days old. To enter calibration mode: create this file with today's ISO date as its contents (`echo "$(date +%Y-%m-%d)" > __QUOIN_HOME__/memory/sleep_dryrun_start.txt`). Delete the file to exit calibration mode early.

**(d) `--escalate`** — after default scoring, spawns a separate Opus Agent subagent for middle-band candidates. Opus returns revised decisions; user confirms each. NOTE: `[no-redispatch]` guards the parent `/sleep` from §0 re-dispatch only; it does NOT prevent `/sleep` from spawning Opus children via `--escalate`. The Opus subagent is an explicit forward dispatch, not a tier-switch.

Additional subcommands:

- `--restore <pattern>` — search `forgotten/` for entries matching pattern and restore them. See `## --restore <pattern>`.
- `--purge --older-than 90d` — list `forgotten/` files older than threshold for confirmed deletion. See `## --purge --older-than 90d`.
- `--quiet-forget` — suppress per-entry confirmation for soft-forget candidates whose score is at or above `forget_quiet_floor` (default: 4).

## Scan scope

Files `/sleep` reads:
- `daily/insights-<date>.md` files in `.workflow_artifacts/memory/daily/` modified within the scan window (default 30 days).
- `sessions/<date>-<task>.md` files in `.workflow_artifacts/memory/sessions/` within the window.
- `.workflow_artifacts/memory/lessons-learned.md` (for dedup check against promote candidates).
- `.workflow_artifacts/<task-name>/cost-ledger.md` files (for cost_bearing signal — MVP: signal always 0; deferred to post-calibration).

Files `/sleep` does NOT read:
- `forgotten/` — archive only; never re-scored.
- `~/.claude/projects/<hash>/memory/` — auto-memory; strictly off-limits.
- `daily/<date>.md` briefings — rendered output, not raw insights.

## Maintenance patterns

`/sleep` honors `read_only`, `archived`, and `ignore` globs from `__QUOIN_HOME__/memory/memory-maintenance.yaml` via `sleep_score.py --patterns`. Globs are matched against the **bare file name** of each source file being scored:

- `ignore`-matched sources: skipped entirely — produce zero entries.
- `archived` or `read_only`-matched sources: entries are **never soft-forgotten**. If the scorer would assign `bucket="forget"`, the bucket is demoted to `"middle"` instead. The `/sleep` NDJSON parser is unchanged — protected entries already appear as `"middle"` in the output.

The `--patterns` flag is always passed (see Step 2). The loader no-ops on a missing file, so this is safe even on a fresh install.

## Importance scoring

At session start, read sleep config: try `__QUOIN_HOME__/memory/sleep-signals.yaml` first; if missing, use hardcoded defaults (the historical CLAUDE.md-embedded fallback leg was dropped under the slim CLAUDE.md variant, IVG-164 stage 1 — sleep-signals.yaml is always deployed alongside it). Emit `[sleep: config not found; using hardcoded defaults]` only when falling back to hardcoded.

For each candidate entry, compute:
- `promote_score` — sum of matched promote signal weights
- `forget_score` — sum of matched forget signal weights

Three-bucket decision:
- **Promote**: `promote_score >= promote_min_score` AND `forget_score <= promote_max_forget`
- **Soft-Forget**: `forget_score >= forget_min_score` AND `promote_score <= forget_max_promote`
- **Middle-Band**: everything else

Scoring is performed by `python3 __QUOIN_HOME__/scripts/sleep_score.py`. The skill invokes this script and parses its NDJSON output.

## Process (default mode)

### Step 0: Check prerequisites

Verify `daily/<today>.md` exists at `.workflow_artifacts/memory/daily/<today>.md`.

If absent: emit the error message below and exit non-zero:
```
Error: /sleep requires today's daily briefing (daily/<today>.md) — run /end_of_day first.
```

This guard fires only in standalone invocations. In the auto-chain path, `/end_of_day` writes the daily briefing (Step 3) before invoking `/sleep` (Step 6), so the file always exists in that context.

Also check whether dry-run mode should be forced: read `__QUOIN_HOME__/memory/sleep_dryrun_start.txt`. If it exists and the date within is less than 30 days ago, set `forced_dry_run = true` and emit: `Dry-run mode active until <start-date + 30 days>: no writes will occur (calibration period).`

### Step 1: Read config

At session start, read sleep config: try `__QUOIN_HOME__/memory/sleep-signals.yaml` first; if missing, use hardcoded defaults (the historical CLAUDE.md-embedded fallback leg was dropped under the slim CLAUDE.md variant, IVG-164 stage 1 — sleep-signals.yaml is always deployed alongside it). Emit `[sleep: config not found; using hardcoded defaults]` only when falling back to hardcoded.

### Step 2: Run sleep_score.py

Invoke:
```
python3 __QUOIN_HOME__/scripts/sleep_score.py \
  --scan-dir .workflow_artifacts/memory/daily/ \
  --scan-days 30 \
  --lessons-file .workflow_artifacts/memory/lessons-learned.md \
  --patterns __QUOIN_HOME__/memory/memory-maintenance.yaml \
  --output json
```

If the script is unavailable or exits non-zero: emit `[quoin-S-3: scoring unavailable; check sleep_score.py installation]` and exit 0 (fail-OPEN — do not abort a session over an optional consolidation step).

Parse the NDJSON output into three lists: promote candidates, forget candidates, middle-band candidates.

### Step 3: Dedup promote candidates

Filter promote candidates: remove any entry whose text has ≥3-keyword overlap with any existing paragraph in `lessons-learned.md`. This dedup pass is performed inside `sleep_score.py` (the `dedup_against_lessons` function). Confirm the `bucket == "promote"` entries in the output already reflect dedup filtering.

### Step 4: Present promote candidates

For each promote candidate (max 10 at a time):
```
Promote this pattern?
---
<entry text>
---
[y / n / edit]
```

On `y`: **first run the cross-project dedup guard (IVG-119)** before appending. Derive
the slug with the SAME inference that builds the append heading below, then call the
guard:
```
slug="$(python3 __QUOIN_HOME__/scripts/sleep_score.py --slug-from-path "<source_path>")"
python3 __QUOIN_HOME__/scripts/lessons_guard.py \
  --candidate-file <tmp-with-entry-text> \
  --lessons-file .workflow_artifacts/memory/lessons-learned.md \
  --candidate-slug "$slug"
```
Exit 1 → present "⚠️ suspected cross-project duplicate of `<matched-slug>`" and let the
user decide (do not silently append). Indeterminable slug → the guard's None-slug
contract yields exit 0 (fail-open). Exit 0/2/3 or script missing → proceed. This
COMPLEMENTS Step 3's `dedup_against_lessons` (which stays as-is): Step 3 catches
keyword-overlapping repeats; the guard catches different-task verbatim copies.

Then append to `.workflow_artifacts/memory/lessons-learned.md` in standard format:
```markdown
## <today> — <source task name, inferred from source_path>
**What happened:** <entry text>
**Lesson:** <entry text — or user-edited version>
**Applies to:** /sleep, /plan (inferred from entry content)
```

On `edit`: user types revised text; append the revised version.

On `n`: skip. Entry remains in insights file unchanged.

If `forced_dry_run == true`: print decisions but make NO writes.

### Step 5: Present soft-forget candidates

For each soft-forget candidate (max 10 at a time):

If `--quiet-forget` AND `forget_score >= forget_quiet_floor`: skip confirmation, auto-soft-forget.

Otherwise:
```
Soft-forget this entry? (moves to forgotten/ archive; recoverable via /sleep --restore)
---
<entry text>
---
[y / n]
```

On `y`: append to `.workflow_artifacts/memory/forgotten/<today>.md` in the required format:
```
> Source: <absolute-path-to-source-file>:<source_start_line>..<source_end_line>
> Forgotten: <ISO timestamp>
> Score: forget=<forget_score>, promote=<promote_score>

<original entry text verbatim>

---
```

If `forced_dry_run == true`: print decisions but make NO writes.

### Step 6: Print summary

```
/sleep complete: <N> promoted; <M> soft-forgotten; <K> deferred to middle band.
```

## --dry-run mode

Runs Steps 0–3 (scoring and dedup). Prints the three-bucket decision for each candidate entry. **Makes NO writes** — no appends to `lessons-learned.md`, no writes to `forgotten/`. Useful during the first 30-day production calibration period and for spot-checking importance weights before committing.

Output format (text summary):
```
PROMOTE (N):
  1. [score: P=<N>, F=<N>] <first 80 chars of entry text>
  ...

SOFT-FORGET (M):
  1. [score: P=<N>, F=<N>] <first 80 chars of entry text>
  ...

MIDDLE-BAND (K):
  1. [score: P=<N>, F=<N>] <first 80 chars of entry text>
  ...
```

## --restore <pattern>

Search all `forgotten/<date>.md` files for entries whose text contains `<pattern>` (substring match, case-insensitive). For each match:

1. Read the `> Source:` anchor line to get the original file path and line range.
2. Restore target precedence:
   - **(1) Original source exists** → append the entry text to the original source file. Confirm with user before appending.
   - **(2) Original source gone** → append to `.workflow_artifacts/memory/daily/insights-<today>.md` with prefix `> Restored from forgotten/<orig-date>.md`. Confirm with user before appending.
   - **(3) Neither writable** → abort: print entry text to stderr with message "Cannot restore — original path gone and today's insights file not writable. Entry text shown below."; exit non-zero.

After successful restore: remove the entry block from its `forgotten/<date>.md` file (atomic rename pattern: write updated content to `<path>.tmp`, then `os.rename`).

<!-- decision-gate: out-of-scope site=sleep-purge reason=user-invoked-destructive-requires-older-than -->
## --purge --older-than 90d

**Usage forms (mutually exclusive scope flags):**
- `/sleep --purge --older-than Nd` (no scope flag) → targets `forgotten/<date>.md` files ONLY (backward-compat default).
- `/sleep --purge --sentinels --older-than Nd` → targets the 9 sentinel families under `.workflow_artifacts/memory/` ONLY (new).
- `/sleep --purge --all --older-than Nd` → convenience: runs `forgotten/` purge first, then sentinel purge.

Scope flags are mutually exclusive — `--sentinels` and `--all` cannot be combined. Users who want both targets must opt in via `--all`.

Never auto-run `--purge`. Always require explicit `--older-than <Nd>` argument.

### forgotten/ purge (default — no scope flag)

List all `forgotten/<date>.md` files whose date portion is more than `N` days before today. For each file:
```
Permanently delete forgotten/<date>.md? (<N> entries, <M KB>)
This is a true delete — not recoverable.
[y / n]
```

On `y`: delete the file. On `n`: skip.

Emit warning at start: "This permanently deletes archive files. Entries cannot be restored after purge."

### Sentinel purge (--sentinels or --all scope)

**Sentinel families covered (hardcoded allow-list — 9 families):**
1. `pending-restore-*.txt`
2. `pending-prompt-*.txt`
3. `compact-happened-*.txt`
4. `mid-agent-handoff-*.txt`
5. `pending-resume-ref-*.txt`
6. `checkpoint-defer-*.txt`
7. `postcompact-reset-*.txt`
8. `checkpoint-pending-compact-*.txt`
9. `idle-advisory-pending-*.txt`

All 9 families live under `.workflow_artifacts/memory/` (the sentinel directory). The family list is a hardcoded allow-list — no user-supplied pattern argument. This is the primary safety mechanism: only files matching one of the 9 named glob patterns are ever considered for deletion.

**Procedure:**

Emit warning at start: "This permanently deletes sentinel files. After purge, restore requires re-saving via /checkpoint."

For each family in the allow-list, find files matching the glob under `.workflow_artifacts/memory/` with `mtime > N days`:
```
find .workflow_artifacts/memory -maxdepth 1 -name '<family-glob>' -mtime +N -print0
```

Collect all candidates. If no candidates found: emit "No sentinels older than Nd found." and return.

For each candidate file, prompt:
```
Permanently delete <path>? (<size>)
This deletes only the sentinel pointer; the underlying checkpoint file (if any) is NOT touched.
[y / n]
```

On `y`: `rm -f <path>`; emit "deleted <path>". On `n`: emit "skipped <path>".

At end: emit "Done. <deleted_count> deleted, <skipped_count> skipped."

**Note:** `--purge --sentinels` performs `rm` on stale sentinel files under `memory/` — this is a delete, not a write, and is governed by the per-file `[y / n]` prompt and the threshold flag, not the Write-target restriction.

**When `--all` is specified:** run the `forgotten/` purge procedure first (existing behavior), then run the sentinel purge procedure above. Each has its own per-file prompts.

## --escalate flag

After default scoring (Steps 0–3), collect middle-band candidates. Spawn a separate Opus Agent subagent:
```
model: "opus"
description: "sleep --escalate: Opus review of middle-band memory candidates"
prompt: "Review these memory consolidation candidates and return promote/forget/middle decisions with brief rationale for each. No writes — return decisions only as JSON lines.\n\n<middle-band entries as JSON>"
```

The Opus subagent returns revised decisions. Present each revised decision to the user with rationale. User confirms each. On confirm: execute the promote or soft-forget action (same write paths as Steps 4–5).

NOTE: `[no-redispatch]` in the parent `/sleep` prompt guards against §0 re-dispatch of the current skill session. It does NOT prevent `/sleep` from spawning Opus children via `--escalate`. The Opus subagent is an explicit forward dispatch — not a tier-switch of the current session.

## Write-target restriction

**HARD RULE: /sleep ONLY writes to `.workflow_artifacts/memory/lessons-learned.md` AND `.workflow_artifacts/memory/forgotten/<date>.md`.**

Any other write path is a bug. This restriction is tested by `quoin/dev/tests/test_sleep_write_boundary.py`.

Prohibited write targets (non-exhaustive):
- `~/.claude/projects/<hash>/memory/` (auto-memory) — NEVER
- `sessions/<date>-<task>.md` — read-only for /sleep
- `daily/insights-<date>.md` — read-only for /sleep (exception: `--restore` may append here if original source is gone)
- `daily/<date>.md` briefings — never touched

## Cost ledger

If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`

At session start (after Step 0 passes), append a row to the task cost ledger:

<!-- quoin:ledger-self-write -->

```bash
LEDGER=".workflow_artifacts/<task-name>/cost-ledger.md"
uuid=$(python3 __QUOIN_HOME__/scripts/get_session_uuid.py --project-path "$(pwd)" --phase "sleep" 2>/dev/null || echo "unknown-sleep-$(date -u +%Y%m%dT%H%M%SZ)") && printf '%s | %s | %s | %s | task | %s | %s\n' \
  "$uuid" "$(date -u +%Y-%m-%d)" "sleep" "sonnet" "sleep-session" "0" \
  >> "$LEDGER"
```

Skip if no task context is active (e.g., `/sleep` run in a fresh session with no active task). The task name can be inferred from the active session-state filename in `.workflow_artifacts/memory/sessions/`.
