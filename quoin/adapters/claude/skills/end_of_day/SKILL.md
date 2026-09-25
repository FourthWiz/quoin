---
name: end_of_day
description: "Consolidates today's work across all sessions into a daily cache for next-day resumption. Use for: /end_of_day, 'wrapping up', 'done for the day', 'save my progress', 'end of day', 'EOD'. Feeds /start_of_day."
model: sonnet
---

# End of Day

You consolidate all of today's work into a daily cache that `/start_of_day` can restore tomorrow. This skill works in any session — you can run it in a fresh session at end of day, or from inside an active working session. Everything except Step 1 reads from disk, so no prior context is needed.

*Portable intent doc: `quoin/core/skills/end_of_day.md`*

## §0 Model dispatch (FIRST STEP — execute before anything else)

This skill is declared `model: sonnet`. If the executing agent is running on a model
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
        description: "end_of_day dispatched at sonnet tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in end_of_day. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /end_of_day`).
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
    description: "end_of_day — min-tier up-dispatch"
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
      switch with /model and re-invoke /end_of_day]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on sonnet up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from a Sonnet session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /end_of_day from a Sonnet session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0tripleprime-end -->

## How sessions and daily caches work

```
.workflow_artifacts/
├── memory/
│   ├── sessions/
│   │   ├── 2026-03-17-auth-refactor.md       ← individual session states
│   │   ├── 2026-03-17-payment-migration.md
│   │   └── 2026-03-18-auth-refactor.md
│   ├── daily/
│   │   ├── 2026-03-17.md                      ← daily rollup (from end_of_day)
│   │   └── 2026-03-18.md
│   ├── git-log.md                             ← rolling log of recent commits
│   ├── repos-inventory.md
│   ├── architecture-overview.md
│   └── dependencies-map.md
├── <task-name>/                               ← active task artifacts
└── finalized/                                 ← completed task archives
```

Multiple sessions can run in a day (parallel tasks, or revisiting a task). Each session writes its own state file. `/end_of_day` reads ALL session files for today, picks out the unfinished ones, and rolls them into one daily cache.

## Process

### Step 0: Check for `--recover-orphans` flag

If the user invoked `/end_of_day --recover-orphans`, run the orphan-detection procedure BEFORE
the normal session-selection (Step 3). The procedure identifies session files that were marked
`end_of_day_due: no` (either by a previous EOD run or manually) but whose task-name slug has
NEVER appeared in any daily-cache file body — these sessions were silently skipped and their
work was never captured.

**Step 0a: Reconcile covered-but-due sessions (runs BEFORE orphan detection below; mirror image of the orphan procedure — orphans are flag=no-but-uncovered, this is flag=yes-but-already-covered):**

If `QUOIN_DISABLE_EOD_RECONCILE=1` is set, skip this step entirely and proceed straight to the
orphan detection procedure below (fail-OPEN default — today's Step 0 behavior, unchanged). If the
shared helper script is unavailable for any reason, also skip Step 0a and fall through unchanged.

1. Run (dry-run — the script makes no writes for this flag):
   ```bash
   python3 __QUOIN_HOME__/core/scripts/select_unprocessed_sessions.py --reconcile-covered --project-root <root>
   ```
   Parse two labelled lists from stdout: `COVERED:` (sessions with `end_of_day_due: yes` whose
   slug — via the same guarded base-slug derivation `find_orphans()` uses — is already present in
   a daily-cache body, i.e. should have been flipped to `no` already but wasn't) and `UNCOVERED:`
   (genuine backlog, unchanged from today's classification). Round 4 scope note: a phase/stage-
   suffixed slug (e.g. `foo-review`, `foo-s3-implement`) whose root task IS covered still classifies
   `UNCOVERED` here — this is documented, intended behavior for this round (only the `-orchestrator`
   suffix is reconciled), not a bug.
2. For each file in `COVERED:`, atomically flip `end_of_day_due: no` — same mechanism as Step 3d:
   open the file, replace the `end_of_day_due: yes` line with `end_of_day_due: no`, write to
   `<path>.tmp`, then `os.rename(tmp, path)`.
3. Print the flipped list for audit: "Reconciled N already-captured sessions: <paths>." If N is 0,
   print "No covered-but-due sessions to reconcile."
4. **Hard ordering rule:** every `COVERED:` file's flip MUST complete on disk BEFORE the mandatory
   `select_unprocessed_sessions.py --show-window` invocation that opens Step 2 below — otherwise a
   covered-but-still-`yes` file would leak into `script_file_list`/`in_scope` and get built into
   that day's daily cache a second time.
5. Covered auto-flips from this step are OUTSIDE `in_scope` for the Step 3 build — that work was
   already captured in an earlier daily body, so these files do NOT enter the Step 3 daily-cache
   build and do NOT enter the §V side-effect predicate (which only inspects `in_scope`).
6. This reconciliation runs ONLY inside `--recover-orphans` invocations, not on every `/end_of_day`
   run (kept opt-in per the plan's "one-time reconciliation mode" framing — see Open questions).
7. Because `find_orphans()` (used by the orphan-detection procedure immediately below) shares the
   same guarded base-slug derivation as this step's `find_covered_due_sessions()`, a covered
   `<task>-orchestrator` session just flipped to `no` here is never re-surfaced as an orphan by the
   orphan-detection procedure that runs next in the same `--recover-orphans` invocation — both
   procedures agree on the same base-slug coverage verdict.

**One-time validation before relying on this by default:** run
`python3 __QUOIN_HOME__/core/scripts/select_unprocessed_sessions.py --reconcile-covered
--project-root <root>` (dry-run, no writes) against the real backlog and manually eyeball the
`COVERED:`/`UNCOVERED:` partition — specifically confirm `-orchestrator.md` files classify
correctly. Separately count, from the same dry-run pass, how many `UNCOVERED:` files are
phase/stage-suffixed (`-review`, `-implement`, `-pr`, `-critic-rN`, `-plan`, `-revise`, `-stageN`,
`-sN`, letter-indexed sub-stages, etc.) versus how many are `-orchestrator`-suffixed — report both
counts separately in the session-state file. These two populations are NOT reconciled the same way
this round (only `-orchestrator` is fixed); recording the phase/stage-suffixed count explicitly
keeps that residual visible rather than silently folded into a single aggregate number.

**Orphan detection procedure (proc:T-19):**
1. Scan all session files under `.workflow_artifacts/memory/sessions/`. For each with
   `end_of_day_due: no`, extract the slug (the portion of the filename AFTER the date prefix
   and BEFORE `.md`; e.g. for `2026-05-13-foo-bar.md` the slug is `foo-bar`).
2. Read every daily file matching `daily/<YYYY-MM-DD>.md` (excluding `insights-*.md`). Build
   the union of all body text across these daily files.
3. A session is an **orphan** iff its slug is absent from the union body text, using a
   **word-boundary-aware** regex: `r"(?<![\w-])" + re.escape(slug) + r"(?![\w-])"`. Hyphens
   count as part of the slug token — so slug `json-discovery-map` does NOT match inside
   `json-discovery-map-review`. This prevents false-positive "covered" verdicts caused by
   prefix collisions.
4. Partition orphans by file date: **RECENT** (file_date >= today - 7 days) and **HISTORICAL**
   (older). The shared helper `__QUOIN_HOME__/core/scripts/select_unprocessed_sessions.py`'s
   `find_orphans()` implements this procedure exactly.
5. If no orphans → print "No orphaned sessions detected." then proceed with normal EOD (Step 1).
6. If RECENT orphans → print a numbered list (file_date + slug for each), then prompt:
   `Include in today's rollup? [y/N]` (default No; empty/n/N = No).
7. If HISTORICAL orphans → after the RECENT prompt completes, print a second prompt:
   `<K> older orphaned sessions detected (pre-7-day window). Include in today's rollup? [y/N]`
   (default No).
8. For each confirmed group: treat those sessions as `end_of_day_due: yes` for THIS run only.
   Do NOT permanently flip the flag until AFTER the daily-cache write succeeds (crash safety).
   After the daily-cache write, Step 3d flips them to `no` per normal flow.
9. Per-session granularity is NOT supported in this revision — group-level confirmation only.
   Per-session selection is a follow-up item tracked in next-steps.md.

### Step 1: Save current session state (skip if no active task)

# V-05 reminder: T-NN/D-NN/R-NN/F-NN/Q-NN/S-NN are FILE-LOCAL.
# When referring to a sibling artifact's task or risk, use plain English (e.g., "the parent plan's T-04"), NOT a bare T-NN token. See format-kit.md §1 / glossary.md.
Write session-state files in v3 format per the §5.4 Class A writer mechanism (reference format-kit.md / glossary.md / terse-rubric.md at the body write-site; session artifact type per format-kit §2; validate via validate_artifact.py with retry-once-then-English-fallback). Write `daily/insights-{date}.md` in v3 format per the §5.4 Class A mechanism — append-only structure (each insight as an entry per the template at Step 3 of /capture_insight); body uses caveman prose with terse-rubric for the entry content; no V-02 section-set strict-match (insights file uses the default minimal section set since each entry is its own implicit "section"); validate via validate_artifact.py and accept default fallback semantics on V-failure. `daily/{date}.md` (the rendered daily briefing) remains Class B per parent Stage 3 work — the file gets a `## For human` block prepended (composed directly by the dispatched model in the same generation as the body — no script invocation). The terse-rubric applies inside prose-shaped sections only (composed with format-kit per format-kit §5).

**If this session has no active task** (e.g. you opened a fresh session just to run `/end_of_day`), skip the session-state write and proceed to Step 2. The existing session files on disk are the source of truth.

**If there is active work in this session**, create or update a session file at `.workflow_artifacts/memory/sessions/<date>-<task-name>.md`.

The session file MUST include these required sections (per format-kit.sections.json session.required_sections):
- **## Status:** `in_progress` | `completed` | `blocked`
- **## Current stage:** which workflow step (architect / plan / critic / revise / implement / review)
- **## Completed in this session:** terse numbered list with status glyphs ✓/⏳/🚫 + commit hashes
- **## Unfinished work:** remaining tasks; where exactly to pick up (file paths, task numbers, branch names)
- **## Cost:** YAML block — Session UUID, Phase, Recorded in cost ledger (see CLAUDE.md "Session state tracking")

Optional sections: `## Decisions made` (important decisions and rationale), `## Open questions` (blockers, unclear requirements).

### Step 2: Update git-log.md

**First, compute this run's processing window (once, shared with Step 3).** Run the shared helper — this is the AUTHORITATIVE, single invocation for the whole `/end_of_day` run; do not hand-compute the window and do not run it a second time in Step 3:

```bash
python3 __QUOIN_HOME__/core/scripts/select_unprocessed_sessions.py --lower-bound-source daily --project-root <root> --show-window --include-finalized-marked
```

Parse the first output line `WINDOW: <lower_bound>..<today>` for `lower_bound` and `today` — the script derives both itself, so no manual date arithmetic is required. **Stdout parse rule (amended):** lines prefixed neither `WINDOW:` nor `FINALIZED:` are `script_file_list`, reused in Step 3. Lines prefixed `FINALIZED: <path>` are `finalized_marked` — in-window sessions that a prior `/end_of_task` run already flipped to `end_of_day_due: no` via its `--flip-finalized-task` step (see end_of_task SKILL.md Sub-phase B), but which still need to appear in THIS run's "Completed today" digest since it hasn't run since. Do NOT fold `FINALIZED:` lines into `script_file_list` — a literal reading of the old wording ("the remaining stdout lines are script_file_list") would incorrectly include them, and the `FINALIZED: ` prefix makes those lines invalid paths if mis-parsed that way.

(`--recover-orphans` runs only) `confirmed_orphans` = the session files confirmed via Step 0's RECENT/HISTORICAL orphan prompts, treated as `end_of_day_due: yes` for THIS run only. The script structurally cannot see these — they are still `end_of_day_due: no` on disk. Define:

`in_scope = script_file_list ∪ confirmed_orphans ∪ finalized_marked`

This union — not `script_file_list` alone — is the definitive set for BOTH the Step 3 daily-cache build and the Step 3d flag-flip. `finalized_marked` sessions are `end_of_day_due: no` by construction (flipped by `/end_of_task`'s single-invocation flip step) — they enter the Step 3 build (so finalized-task work still appears in "Completed today") but Step 3d's flip is a no-op on them (already `no` — idempotent, no special-casing needed). On a normal run (no `--recover-orphans`, none confirmed, and no finalized-marked sessions in window), `confirmed_orphans` and `finalized_marked` are both empty and `in_scope == script_file_list` — unchanged behavior. The `finalized_marked` union does not affect §V's side-effect predicate: those sessions are treated identically to any other already-`no` file.

The script is authoritative for selection. The fallback lower_bound-discovery prose in Step 3 below is retained ONLY as an explanatory fallback for when the script is unavailable.

Scan all repos in the project folder and update `.workflow_artifacts/memory/git-log.md` with recent commits. This is a rolling window — keep the last ~50 commits across all repos, newest first.

```markdown
# Recent Git Activity

Last updated: <datetime>

## <repo-name>
### <branch-name>
- `<short-hash>` <commit message> — <date>
  <1-line summary of what the commit actually changed and why>
- `<short-hash>` <commit message> — <date>
  <1-line summary>

## <other-repo>
...
```

To build this:
```bash
# For each repo directory — single-day window (lower_bound == today): keep the -20 cap unchanged
git -C <repo-path> log --all --oneline --date=short --format="%h %s — %ad" -20

# Multi-day window (lower_bound < today): date-bounded, no arbitrary commit-count cap,
# so a multi-day skip cannot silently truncate commits past a 20-commit ceiling
git -C <repo-path> log --all --since=<lower_bound> --oneline --date=short --format="%h %s — %ad"
```

Then for each commit, briefly describe what it changed (read the diff summary, not the full diff):
```bash
git -C <repo-path> diff-tree --no-commit-id --name-status -r <hash>
```

Keep commit descriptions to one sentence. Base them on the commit message and diff summary — do not speculate about intent beyond what the message states.

The goal is to capture the *logic* of recent changes — not just file lists but *why* things changed. This helps `/start_of_day` and `/architect` understand momentum and recent direction.

### Step 3: Produce the daily cache

**Session selection — script-authoritative `in_scope` set:**

`lower_bound`, `today`, and `script_file_list` were already computed ONCE at the start of Step 2 (the mandatory `select_unprocessed_sessions.py --show-window` run) — reuse them here. Do NOT invoke the script a second time in Step 3.

`in_scope` (`script_file_list ∪ confirmed_orphans`, defined in Step 2) is the definitive set of session files for this run.

For reference, the script's selection rule (authoritative — the fallback prose below is only for when the script is unavailable): a session file at `.workflow_artifacts/memory/sessions/<FILE>` is "unprocessed and in scope" iff ALL of:
  (a) basename matches `^\d{4}-\d{2}-\d{2}-.+\.md$`
  (b) `## Cost` block contains `end_of_day_due: yes` (missing field treated as `yes` per D-02)
  (c) `file_date <= today`. Selection is otherwise flag-authoritative only — `lower_bound` plays NO role in this selection filter; it exists purely to scope the reporting window used further below (Cost / Step 3b insights). Do not wire `lower_bound` into this filter — that would reintroduce the IVG-103 regression this plan fixes.

Legacy session files lacking the `end_of_day_due` line are treated as `yes` (D-02).
Future-dated files (date > today) are excluded.

**Fallback lower_bound discovery** (explanatory only — use ONLY when the script is unavailable; the script is otherwise authoritative):
1. Scan `daily/<YYYY-MM-DD>.md` files (excluding `insights-*.md`). Find the most recent one by
   date. Call its date `last_daily_date`.
2. If no prior daily exists → `lower_bound = today`.
3. If `last_daily_date >= today` → `lower_bound = today` (same-day re-run — merge mode; see
   proc:D-06 below).
4. Otherwise → `lower_bound = last_daily_date + 1 day`.

Example: if last daily is 2026-05-13 and today is 2026-05-16, scan dates 2026-05-14,
2026-05-15, 2026-05-16 (lower_bound = 2026-05-14).

**Same-day re-run — one deterministic rule:** the script always runs exactly once per `/end_of_day` invocation (at the start of Step 2), regardless of merge vs fresh-write mode. Branching happens ONLY at the WRITE step below — there is no "skipped" or short-circuited script-invocation path:
- `daily/<today>.md` does NOT exist → fresh write, build the daily cache from `in_scope`.
- `daily/<today>.md` ALREADY exists → MERGE mode (`proc:D-06`, section-by-section algorithm) rather than overwrite. `in_scope` — which naturally excludes files an earlier same-day run already flipped to `no` — becomes the `new_tasks` / `new_session_rows` inputs to `merge_daily()`. `merge_daily()`'s `## Completed today` algorithm is a task-name set-union, latest wins: overwrite on same key. If unable to merge (proc:D-06 step 12 detects unrecoverable corruption), refuse with:
`daily/<today>.md is malformed; rename to <date>.md.bak and re-run`.

For each file in `in_scope`:
- If status is `completed` → note it as done, no action needed
- If status is `in_progress` or `blocked` → include in the daily cache

Write the daily cache to `.workflow_artifacts/memory/daily/<date>.md`:

```markdown
# Daily Cache — <YYYY-MM-DD>

## For human

<5-8 line plain-English summary: what was the day's focus; which tasks made progress; what is the biggest open blocker; what to do tomorrow. Written directly by the dispatched model in the same generation as the body — NOT via a summary script>

## Summary
<1-2 sentences: what was the day's focus, what got done, what's left. Include inline: "Sessions processed from <lower_bound> to <today> (<K> days, <N> session files)."; append ", including N straggler session(s) from before the window" when in_scope contains stragglers>

## Sessions processed

| Date | Task | Phase | Status | Notes |
|------|------|-------|--------|-------|
| YYYY-MM-DD | task-name | implement | completed | — |
| YYYY-MM-DD | task-name | plan | in_progress | resumed tomorrow |

## Completed today
- **<task-name>**: <what was finished>

## Unfinished — carry forward

### <task-name-1>
**Stage:** <architect / plan round 3 / implement task 4 of 7 / review>
**Branch:** <branch-name>
**Pick up at:** <exactly where to resume — file, task number, next step>
**Key context:**
- <decisions made that affect next steps>
- <blockers to resolve>
- <relevant file paths and PR URLs>
**Remaining work:**
- <specific next actions>

### <task-name-2>
...

## Decisions log
<All decisions made today across all sessions, with rationale>

## Git activity summary
<High-level: N commits across M repos, built from this run's own Step 2 gather (the --since=<lower_bound>-bounded output for multi-day windows, or the -20 output for single-day windows) — NOT solely a reference to git-log.md. Key changes: ...>
<git-log.md is supplementary cross-repo detail only (its own ~50-commit rolling cap is a separate, intentional, longer-lived log) — reference .workflow_artifacts/memory/git-log.md for further detail, but this run's own gather above is the source of truth for the counts here>

## Cost summary
<!-- Session counts from cost-ledger.md files — no ccusage calls -->
- **<task-name>**: <N> sessions tracked today (phases: <plan, implement, ...>)
- **<task-name-2>**: <N> sessions tracked today (phases: ...)
- **Day total**: <N> sessions across <M> tasks
*Dollar amounts: run /end_of_task for each completed task to see the full cost breakdown.*

## Tomorrow's priorities
<Based on what's unfinished, suggest what to tackle first>
```

To populate the **Cost summary** section: for each active task, check if `.workflow_artifacts/<task-name>/cost-ledger.md` exists. If it does, count the data lines (non-header, non-blank) where the date column falls within the processed date window (lower_bound..today, inclusive), and list the unique phase values. Do NOT run `npx ccusage` — this skill does not orchestrate cost lookups. Just report counts and phases. Dollar amounts are computed by `/end_of_task`.

If `in_scope` contains any file dated before `lower_bound` (a straggler — captured via flag-authoritative selection, not via a widened reporting window), append to the Cost summary: "; N straggler session(s) from before the window are included in Sessions processed/Completed today but excluded from this window's cost count." This makes the exclusion visible instead of silent; the straggler's own historical cost-ledger rows and insight files are not re-swept here because a prior `/end_of_day` run already reported (or should have reported) that date.

Also scan all session-state files in the processed window — same selection rule as Step 3 (the set of files selected by the hybrid date-window + flag rule). For each file, read the `## Cost` block and extract the `fallback_fires:` field via regex `^- fallback_fires:\s*(\d+)\s*$`. Sum per task across the processed window. For each task with window fallback total > 0, append the suffix `; <K> fallback fires in window` to that task's Cost summary line. When the window spans more than one day, use `Window total fallback fires: <K>` at the bottom of the Cost summary block; for a single-day window, use `Day total fallback fires: <K>` (backward-compat). After the total line, add: "A non-zero fallback count indicates one or more Class B writers fell back to v2-style write (no `## For human` summary, no validator gate). Investigate which skill emitted `[format-kit-skipped]` and triage before next session." Sessions lacking the `fallback_fires` line (pre-Stage-4) are treated as 0 — no warning emitted.

Using the same in-scope file set, also extract each file's `verification_mismatches:` field via regex `^- verification_mismatches:\s*(\d+)\s*$` and sum per task across the processed window (mirror the `fallback_fires` roll-up above — same scan, same window, same per-task/window-total shape). For each task with a window total > 0, append the suffix `; <K> verification mismatches in window` to that task's Cost summary line. When the window spans more than one day, use `Window total verification mismatches: <K>` at the bottom of the Cost summary block; for a single-day window, use `Day total verification mismatches: <K>`. After the total line, add: "A non-zero verification-mismatch count means a skill's claims contradicted re-derived ground truth (§V) — investigate which skill and re-run its verification before trusting its output." Sessions lacking the `verification_mismatches` line (pre-IVG-115) are treated as 0 — no warning emitted.

## §V Claims manifest (emit as an always-run early step)

<!-- §V-claims-begin -->
This step ALWAYS runs (it is not the verification step — it is the claim source the
SessionEnd-hook backstop reads even when the model later skips §V, CRIT-1) and runs
immediately after the daily cache above is written, never before.

Write a STRUCTURED `## Claims` manifest — a fenced `yaml` block, one entry per in-window
task, `{task_ref: <str>, status: <enum>}` where `status` is one of `{awaiting_pr,
awaiting_end_of_task, in_progress, merged, finalized}` — to
`memory/verification/end_of_day-<today>.md`. Enumerate EVERY in-window task (a task
finalized within the run window, or an in-window active task). This manifest MUST NOT be
written empty WHEN THERE IS IN-WINDOW WORK to claim; a genuine quiet day with no in-window
finalized/active work correctly writes a zero-claim manifest (you are NOT required to
enumerate the full all-time `finalized/` archive).
<!-- §V-claims-end -->

### Step 3b: Review and promote daily insights

Check for insights files `daily/insights-<DATE>.md` for every DATE in the processed window
(lower_bound..today, inclusive). Read and combine all that exist. If none exist, skip this step.

**Pass 1 — Filter:**
- Skip entries tagged `Promote?: no`
- Collect entries tagged `yes` (high-confidence) and `maybe` (review needed)

**Pass 2 — Deduplicate:**
- Read `.workflow_artifacts/memory/lessons-learned.md`
- If a collected entry is substantially similar to an existing lesson, drop it or flag it as a duplicate
- An entry is a duplicate if it describes the same root cause AND the same takeaway as an existing lesson. Entries about the same topic but with different lessons are NOT duplicates — keep both and note the connection.

**Pass 3 — Tier 3 check:**
- Any entry with `Applies to: workflow` or type `workflow-friction` is a Tier 3 candidate
- Append those to `.workflow_artifacts/memory/workflow-suggestions.md` in this format:
  ```markdown
  ## <date> — <source-task>
  **Suggestion:** <what should change in the workflow>
  **Why:** <the observation from the insight entry>
  **Affects:** <relevant SKILL.md or CLAUDE.md section>
  **Status:** surfaced
  ```
- Tell the user: "I've added N workflow improvement suggestion(s) to `.workflow_artifacts/memory/workflow-suggestions.md`."

**Promotion confirmation:**
If there are entries to promote (after filtering and dedup), use AskUserQuestion to let the user select which to keep:

- If ≤4 insights: one AskUserQuestion call with `multiSelect=true`. First two options are always "Keep all" and "Keep none"; remaining slots (up to 2) are individual insight summaries. The implicit "Other" covers any subset not directly representable.
- If >4 insights: present in batches of 4 (each batch as a separate AskUserQuestion with `multiSelect=true`). Include "Keep all remaining" / "Skip remaining" in the final batch.

Example (≤4 insights):
```
<!-- decision-gate: best-effort site=end_of_day-prompt-1 -->
AskUserQuestion(
  question="Which insights to promote to lessons-learned?",
  multiSelect=true,
  options=[
    {label: "Keep all", description: "Promote all N insights."},
    {label: "Keep none", description: "Skip promotion."},
    {label: "<insight_1 summary>", description: "<insight_1 detail>"},
    {label: "<insight_2 summary>", description: "<insight_2 detail>"}
  ]
)
```

After the user responds, append confirmed entries to `.workflow_artifacts/memory/lessons-learned.md`:

```markdown
## <date> — <task-name>
**What happened:** <the insight>
**Lesson:** <the reusable takeaway>
**Applies to:** <relevant skills>
```

When presenting the promotion confirmation, group entries by source-date: `[yes/2026-05-14] <summary>`. If no insights files exist for the processed window or none have promotable entries, skip this step silently.

### Step 3c: Prune lessons-learned if oversized

Check `.workflow_artifacts/memory/lessons-learned.md`. Count the number of lesson entries by matching lines that begin with `## ` followed by a date pattern (YYYY-MM-DD) — i.e., lines matching the regex `^## \d{4}-\d{2}-\d{2}`. Ignore any such patterns inside HTML comments (`<!-- -->`), code blocks, or template examples.

**If the count exceeds 30 entries**, present a pruning prompt via AskUserQuestion:

```
<!-- decision-gate: best-effort site=end_of_day-prompt-2 -->
AskUserQuestion(
  question="lessons-learned.md has grown to N entries (~X tokens). Large files add token cost to every /plan, /critic, and /architect session. Would you like to prune?",
  options=[
    {label: "Auto-prune", description: "Merge related entries, remove stale ones. You review before I save."},
    {label: "Manual prune", description: "List all entries with summaries; you pick which to keep."},
    {label: "Skip", description: "Keep lessons-learned.md as-is."}
  ]
)
```

**Auto-prune rules** (if selected):
1. Group entries by `Applies to:` tag. If 3+ entries have the same tag and similar content, merge them into one consolidated entry preserving all unique information.
2. Entries older than 90 days that are generic advice (e.g., "always run tests", "check error handling") can be removed — the behavior should be internalized by now.
3. Entries that reference specific files or functions that no longer exist in the codebase can be removed (check with a quick file-existence test).
4. Always preserve: entries tagged as applying to `/architect` or `/critic` (highest-leverage skills), entries less than 30 days old, entries the user explicitly marked as important.
5. Show the proposed changes to the user in a before/after summary and wait for explicit confirmation before overwriting.

**If the count is 30 or fewer**, skip this step silently.

### Step 3d: Write resume cookie + flip end_of_day_due

**After** the daily-cache write (Step 3) succeeds, do two things:

**1. Flip `end_of_day_due: no`** in each session-state file that was rolled into the daily cache:
- For each session file in `in_scope` (the script's file list unioned with any Step 0 `--recover-orphans` confirmed orphans — the SAME set used for the Step 3 build; see Step 2), edit the file in place to set `end_of_day_due: no`. The build set and the flip set must never diverge — both are `in_scope`, always.
- Use an atomic write: open the file, replace the `end_of_day_due: yes` line with `end_of_day_due: no`, write to `<path>.tmp`, then `os.rename(tmp, path)`.
- Flip ONLY after the daily-cache write succeeded — a crashed `/end_of_day` must NOT mark sessions as processed.
- This is one of two signals `/start_of_day` reads to detect a missing-EOD condition (the other is the existing insights-file check).

**2. Write resume cookie** to `<project-root>/.workflow_artifacts/memory/resume-cookie.md`:
- Cookie size cap: 2 KB. If the body would exceed 2 KB, truncate the "what's next" hint to ≤2 lines.
- Cookie content (Tier 3, terse-only):
  ```yaml
  ---
  task: <active-task-name or empty>
  last_skill: end_of_day
  branch: <current git branch>
  dirty_count: <N uncommitted changes>
  expires: <now + 24h ISO datetime, e.g. if now is 2026-04-30T14:00:00Z then expires is 2026-05-01T14:00:00Z>
  ---
  <1-2 line "what's next" hint, free text>
  ```
- **Field allowlist** (hardcoded): `task`, `last_skill`, `branch`, `dirty_count`, `expires`. The writer MUST refuse to include any field not in this list. This prevents sensitive data from leaking into the cookie.
- **Atomic-rename pattern**: write to `resume-cookie.md.tmp` via Python `open(path, 'w')`, then `os.rename(tmp_path, final_path)`. Do NOT use shell `mv` in Python contexts — use `os.rename`.
- Cookie path: `<project-root>/.workflow_artifacts/memory/resume-cookie.md` (under `.workflow_artifacts/` which is gitignored — cookie is never committed).

### Step 4: Prompt for lessons learned

Use AskUserQuestion to check if the user has anything else to add (this catches anything Claude missed from Step 3b):

```
<!-- decision-gate: best-effort site=end_of_day-prompt-3 -->
AskUserQuestion(
  question="Anything else that surprised you today, or that should work differently next time?",
  options=[
    {label: "Nothing to add", description: "No additional lessons today."},
    {label: "Yes, let me share", description: "I have something to add."}
  ]
)
```

If the user selects "Nothing to add" or doesn't respond: skip silently.
If the user selects "Yes, let me share" or uses the "Other" free-text option: capture their input and append it to `.workflow_artifacts/memory/lessons-learned.md` in the same format.

Also: if any tasks were rolled back today, or if the critic-revise loop ran more than 3 rounds, auto-add a lesson capturing what made it difficult.

## §V Ground-truth verification (execute after the skill's work, before the final report)

<!-- §V-verify-begin -->
Run ground-truth reconciliation before composing the Step 5 report — never on a hardcoded
line number, always immediately before this skill's own final report step.

1. Run `python3 __QUOIN_HOME__/scripts/verify_claims.py --check-side-effects --skill end_of_day
   --project-root <project-root>` (side-effect predicates: daily cache written and non-empty;
   every in-scope session flipped `end_of_day_due: no`; resume-cookie present and unexpired;
   lessons-learned prune handled if oversized).
2. Run `python3 __QUOIN_HOME__/scripts/verify_claims.py --reconcile-tasks --claims-file
   <the manifest this skill's own §V Claims manifest step wrote> [--gh-json-file <path> if
   available]` — the in-session model path (live gh, NOT `--finalized-only`; that flag is
   reserved for the SessionEnd hook backstop).
3. If either command exits 8: do NOT finalize a clean report. Surface every MISSING/MISMATCH
   line to the user, self-correct the deterministic gaps you can before composing the report
   (re-flip any session still `end_of_day_due: yes` — idempotent with Step 3d; re-run any
   skipped lessons-learned prune prompt), increment `verification_mismatches` in the
   session-state `## Cost` block, and (re-)write `memory/verification/end_of_day-<today>.md`.
   Leave `verification_ran: no`.
4. If both commands exit 0: set `verification_ran: yes` in the session-state `## Cost` block
   and proceed to the Step 5 report.

Consumers (`/start_of_day`, `/weekly_review`) treat an absent or `no` `verification_ran` on an
in-scope end_of_day session as a mismatch signal — always write this field, never omit it.
<!-- §V-verify-end -->

### Step 5: Report to user

Tell the user:
- What was saved
- How many sessions were active today, how many completed vs unfinished
- What the daily cache recommends for tomorrow
- Remind them to run `/start_of_day` when they resume

### Step 6: Invoke /sleep (memory consolidation)

After Step 5 completes and the user has been shown the report, check for `--skip-sleep` flag:
- If `--skip-sleep` was passed in the invocation: print "Skipping /sleep (--skip-sleep passed). Run /sleep standalone to consolidate memory." and stop.
- Otherwise: spawn a Sonnet Agent subagent:
    model: "sonnet"
    description: "sleep — memory consolidation after end_of_day"
    prompt: "[no-redispatch]\n/sleep\ncontext:\n- daily briefing: .workflow_artifacts/memory/daily/<today>.md\n- lessons: .workflow_artifacts/memory/lessons-learned.md\n- forgotten: .workflow_artifacts/memory/forgotten/\n- scan window: <QUOIN_SLEEP_SCAN_WINDOW_DAYS or 30> days"
  Wait for the subagent. Print the subagent's output inline.
  If the subagent fails (tool error, dispatch unavailable): print "[quoin-S-3: /sleep invocation failed; daily briefing is durable; run /sleep standalone to retry]" and exit 0. DO NOT roll back the daily briefing — /sleep is the LAST step and the briefing is already written.

## Important behaviors

- **Capture decisions.** The hardest thing to remember across days isn't *what* you were doing — it's *why* you made certain choices. Always capture decision rationale.
- **Be specific about resumption points.** "Continue implementing" is useless. "Continue with Task 4 (add retry logic to payment.service.ts:processRefund), branch `feat/payment-retry`, tests for Tasks 1-3 passing" is useful.
- **Don't overwrite previous sessions.** If there's already a session file for this task today (from an earlier session), update it rather than replacing it — preserve the history of what was done earlier.
- **Git log captures logic, not just files.** "Modified 3 files" tells you nothing. "Added exponential backoff to payment retries because Stripe recommends it for idempotent requests" tells you everything.
- **--skip-sleep flag.** Pass `--skip-sleep` to `/end_of_day` to suppress the automatic Step 6 `/sleep` invocation. Use this when memory consolidation is not needed (e.g., brief sessions, first-run calibration, or when running `/sleep` standalone later).
