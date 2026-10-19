---
name: start_of_day
description: "Restores context from the daily cache and unfinished sessions so you can resume where you left off. Use for: /start_of_day, 'what was I working on', 'pick up where I left off', 'morning standup', 'SOD', 'start of day'."
model: haiku
---

# Start of Day

*Portable intent doc: `quoin/core/skills/start_of_day.md`*

You restore context from the previous session(s) so the user can seamlessly resume work. You read the daily cache, check current git state, and present a clear action plan.

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
        description: "start_of_day dispatched at haiku tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in start_of_day. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /start_of_day`).
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

### Step 1a: Resume from cookie

Read `<project-root>/.workflow_artifacts/memory/resume-cookie.md` if present.

- **If absent:** continue to Step 1 (existing behavior — graceful fallback; no error).
- **If present, check `expires` field:** if `expires` < now (ISO comparison), the cookie is stale — ignore it and continue to Step 1.
- **If present and fresh:** use the cookie's `task`, `last_skill`, `branch`, `dirty_count`, and body ("what's next" hint) to seed the Step 5 briefing. Note the task and branch at the start of the briefing so the user knows what was active yesterday.

**Banner check (compose with Step 1 — signal B):** while reading session files for the briefing, also check `.workflow_artifacts/memory/sessions/` for any session-state files written within the last 36 hours that have `end_of_day_due: yes`. Count them as M. If M > 0, signal B is positive, and also compute `all_today` (T-04): compare each flagged file's `<YYYY-MM-DD>` filename-prefix date against today's date as plain ISO strings — `all_today = true` only if every flagged file's prefix equals today's date. Signal B (with `all_today`) is combined with signal A (the insights-file check in Step 1) in a unified banner — see Step 1 for the combined rule.

### Step 1b: Sentinel-health check (read-only, lightweight)

Count files under `.workflow_artifacts/memory/` at depth 1 matching the 2 live sentinel families that are older than `QUOIN_STALE_SENTINEL_DAYS` (default 7) days AND whose filename suffix is NOT the current session's UUID. This step is **read-only** — no trash_move, no rm, no writes of any kind.

The 2 sentinel families are: `pending-restore-*.txt`, `compact-happened-*.txt`.

```sh
# Pseudocode (lightweight count — fail-silent if memory dir absent)
_sod_stale_days=${QUOIN_STALE_SENTINEL_DAYS:-7}
_sod_warn_threshold=${QUOIN_SOD_SENTINEL_WARN:-3}
_sod_count=$(find .workflow_artifacts/memory -maxdepth 1 \
  \( -name 'pending-restore-*.txt' -o -name 'compact-happened-*.txt' \) \
  -mtime +"$_sod_stale_days" 2>/dev/null | wc -l || echo 0)
# Exclude current-session files (any ending in -<current_uuid>.txt):
# In practice Haiku counts all matches; the threshold of 3 provides enough
# margin that the current session's 0-1 sentinels don't trigger a false alarm.
```

If the count exceeds `QUOIN_SOD_SENTINEL_WARN` (default 3), compose a one-line advisory into the unified Step 1 banner:

> "⚠ N stale workflow sentinels in memory/ — run /cleanup to trash-move them (recoverable)."

Compose this advisory INTO the existing unified Step 1 banner (signal A + signal B); do NOT add a separate AskUserQuestion for the sentinel count. If the count is at or below the threshold, skip silently.

Fail-silent if `.workflow_artifacts/memory/` is absent or unreadable — never error on missing dir.

Note: do NOT source any deployed file paths in this pseudocode. The count operates on the project-local `.workflow_artifacts/memory/` path only. No `__QUOIN_HOME__` reference needed.

### Step 1c: Discovery & Serena staleness check

Run `python3 __QUOIN_HOME__/scripts/discovery_staleness.py <project-root> --json` (fail-open if script absent — missing script = no advisory, not an error). Parse the JSON output to check:
- `verdict`: `"stale"` or `"absent"` → discovery needs refresh
- `serena.present_marker`: `true` AND `serena.stale`: `true` → Serena re-onboarding may be needed
- `serena.present_marker`: `false` → absent marker (may need first-time onboarding — Step 6b will probe via ToolSearch)

This step is **read-only**. Do NOT write any files here. Marker writes happen only when the user takes the Serena action in Step 6b.

Store the result for Step 6b. Fold a one-line advisory into the unified Step 1 banner if any staleness is detected:
- Discovery stale/absent: `"Discovery memory is stale or absent — /discover recommended."`
- Serena present-but-stale: `"Serena project memory is stale — refresh available in Step 6b."`
- Both: combine into a single advisory line.

If the script is absent or errors → skip silently (no advisory, no error). Reuse `QUOIN_DISCOVERY_STALE_DAYS` for the threshold — do NOT introduce `QUOIN_SOD_DISCOVERY_STALE_DAYS`.

Note: `QUOIN_DISCOVERY_AUTOREFRESH=1` allows /start_of_day to invoke `/discover` inline without asking (auto-run opt-in). Serena refresh is NEVER auto-run interactively (D-04).

## Session bootstrap

If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

Cost tracking (conditional): `/start_of_day` is a lightweight daily-orientation skill. Append to the cost ledger only if a specific task context is clearly active (the user mentioned a task name or there's a clear active task from session state). If in doubt, skip cost recording — don't guess a task name. If the condition holds: append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `start-of-day` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`.

<!-- quoin:ledger-self-write -->

## Process

### Step 1: Check for missing-EOD signals

**Signal A:** look for `.workflow_artifacts/memory/daily/insights-<yesterday>.md` (yesterday's date). If it exists, count entries tagged `Promote?: yes` or `Promote?: maybe`. Let N = that count (0 if none or file absent).

**Signal B:** (set in Step 1a) — count of session-state files written in the last 36 hours with `end_of_day_due: yes`. Let M = that count. When M > 0, also compute `all_today`: true iff EVERY flagged session's filename date prefix (`<YYYY-MM-DD>-<task>.md`) equals today's date (ISO string compare — ISO date prefixes sort/compare correctly as plain strings, no arithmetic needed); false if any flagged session's date prefix predates today.

**Unified banner composition (T-04 / Round 2 MIN-2 — fire if A OR B is positive, i.e. N > 0 OR M > 0):**
- **Signal A alone (N > 0, M = 0):** no flagged session exists, so there is no date to branch on — keep the existing unconditional recommendation unchanged (do NOT attempt a same-day/cross-day branch with nothing to compare):
  > "⚠ Yesterday's `/end_of_day` did not fire — N insight(s) pending. Recommend running `/end_of_day` before continuing."
  > "Want to run `/end_of_day` now or skip?"
- **Signal B alone (M > 0, N = 0), `all_today = true`** (every flagged session dated today — same-day case):
  > "ℹ M session(s) unflushed today. Run `/end_of_day` when you wrap up (task state resumes with `/run --resume TASK`)."
- **Signal B alone (M > 0, N = 0), `all_today = false`** (a flagged session predates today — cross-day case, a prior day was never wrapped up):
  > "⚠ M session(s) unflushed — a prior day was never wrapped up. Recommend running `/end_of_day` before continuing."
  > "Want to run `/end_of_day` now or skip?"
- **Both signals positive (N > 0 AND M > 0):** the banner follows signal B's branch (a flagged session's date is available to branch on) and separately still mentions the pending insights:
  - `all_today = true`: recommend nothing and print one informational line — "ℹ N insight(s) pending and M session(s) unflushed today. Run `/end_of_day` when you wrap up (task state resumes with `/run --resume TASK`)."
  - `all_today = false`: "⚠ Yesterday's `/end_of_day` did not fire — N insight(s) pending and M session(s) unflushed since a prior day. Recommend running `/end_of_day` before continuing." then "Want to run it now or skip?" (asked only in this case).
- If they want to run the recommended command: invoke it inline before continuing (the promotion flow from `/end_of_day` Step 3b applies)
- If they skip: proceed normally

If neither signal is positive (N = 0 AND M = 0), skip this step silently.

### Step 2: Find the latest daily cache

Look in `.workflow_artifacts/memory/daily/` for the most recent `.md` file. This is what `/end_of_day` saved. If there's no daily cache, check `.workflow_artifacts/memory/sessions/` for any session files and work from those directly.

If neither exists, tell the user there's no saved state and suggest running `/discover` to set up fresh context.

### Step 3: Read context

Read these files in parallel:

1. **Daily cache** — `.workflow_artifacts/memory/daily/<latest>.md` — the consolidated state
2. **Git log** — `.workflow_artifacts/memory/git-log.md` — recent commit history and logic
3. **Active session files** — any `.workflow_artifacts/memory/sessions/*` files with status `in_progress` or `blocked`
4. **Current git state** — for each repo in the project folder:
   ```bash
   git -C <repo> status --short
   git -C <repo> branch --show-current
   git -C <repo> log --oneline -5
   ```
   Check for uncommitted changes, stale branches, open PRs.

### Step 3a: Detect daily-cache format (v2 vs v3)

After reading the daily cache in Step 3, determine its format using the §5.7.1 detection rule below. This governs how to extract the human-facing summary for display in Step 5.

# v3-format detection (architecture.md §5.7.1 — copy verbatim)
# A file is v3-format iff:
#   - the first 50 lines following the closing `---` of the YAML frontmatter
#     contain a heading matching the regex ^## For human\s*$
# Otherwise the file is v2-format.
# On v3-format detection: read sections per format-kit.md for this artifact type.
# On v2-format (or no frontmatter): read the whole file as legacy v2.
# Detection MUST be string-comparison only — no LLM call (per lesson 2026-04-23
# on LLM-replay non-determinism).

Daily cache files have no YAML frontmatter — scan the first 50 lines of the file directly (no frontmatter to skip).

- **v3-format** (file contains `## For human` within the first 50 lines): extract the lines from the line after `## For human` until the next `## ` heading. Pass this text to Step 5 as `<yesterday-summary>`.
- **v2-format** (no `## For human` in the first 50 lines): use the first 2 KB of the file as `<yesterday-summary>` (legacy fallback).

### Step 4: Reconcile

For each unfinished task from the daily cache, run these checks and report the result:

1. **Branch match** — Is the repo on the branch the daily cache says? If not, report the actual branch.
2. **New remote commits** — Run `git log HEAD..origin/<branch> --oneline`. If output is non-empty, report "N new commits from remote."
3. **Uncommitted local changes** — Check `git status --short`. If non-empty, list the changed files.
4. **Stale PRs** — If `gh` is available, run `gh pr list --head <branch> --json number,title,reviewDecision,statusCheckRollup --limit 5`. Report any PRs with new reviews or failed checks. If `gh` is not available, report "PR check skipped — gh CLI not installed."

Report each check result in the briefing's "## Since last session" section (matching the existing Step 5 template). If everything matches the daily cache, say "Git state matches cached state — no drift detected."

## §V Ground-truth verification (execute after the skill's work, before the final report)

<!-- §V-verify-begin -->
Before Step 5 (Present the briefing), reconcile trusted state — never on a hardcoded line
number, always immediately before this skill's own final report step.

Run `python3 __QUOIN_HOME__/scripts/verify_claims.py --reconcile-tasks --project-root
<project-root>` (live gh; this subsumes the existing Step 4 `gh pr list` call — derive any
PR/task-status line you present in the briefing from THIS reconcile table, never from a
daily-cache narrative alone).

For every in-scope end_of_day session read in Step 1/Step 4: treat a MISSING or `no`
`verification_ran` field as a mismatch signal to surface, not a silent pass — an
end_of_day session whose own §V step was silently skipped upstream is itself informative.

If the reconcile exits 8: surface the MISMATCH/coverage lines in the briefing rather than
silently dropping them.
<!-- §V-verify-end -->

### Step 5: Present the briefing

Output a clear, concise briefing:

```markdown
# Good morning 👋

## Yesterday's summary
<yesterday-summary extracted from ## For human block (v3) or first 2 KB of daily cache (v2)>

## Since last session
- <any new commits from others, PR reviews, CI results>
- <any drift or changes to note>

## Unfinished work

### 1. <task-name> — <stage>
**Branch:** `<branch-name>`
**Resume at:** <exactly where to pick up>
**Context:** <key decisions and rationale from last session>
**Next steps:**
1. <first thing to do>
2. <second thing>

### 2. <task-name-2> — <stage>
...

## Blocked items
- <task>: <blocker> — <suggested resolution>

## Suggested priority
<Based on urgency, dependencies, and momentum — what to tackle first and why>
```

Keep the briefing factual. Report what you found in each step — do not speculate about what the user might want to do beyond what the daily cache and git state suggest. Let the user decide priorities.

### Step 6: Offer to resume

After presenting the briefing, use AskUserQuestion to ask the user what they want to work on. Dynamically populate options from the daily cache:

- If unfinished tasks exist: include one option per task (capped at 3), ordered by suggested priority. Label = task name; description = current stage + 1-line resume point. Always include "Start something new" as the last option.
- If no unfinished tasks: show only "Start something new" and "Run /discover" (if no discovery data).
- If >3 unfinished tasks: show top 3 by priority and note the rest in chat.

Example (2 unfinished tasks):
```
<!-- decision-gate: best-effort site=start_of_day-prompt-1 -->
AskUserQuestion(
  question="What would you like to work on today?",
  options=[
    {label: "auth-refactor", description: "implement — resume at T-04: update token refresh logic"},
    {label: "payment-v2", description: "review — run /review to verify the implementation"},
    {label: "Start something new", description: "Begin a new task not listed above."}
  ]
)
```

Example (no tasks):
```
<!-- decision-gate: best-effort site=start_of_day-prompt-2 -->
AskUserQuestion(
  question="What would you like to work on today?",
  options=[
    {label: "Start something new", description: "Begin a new task."},
    {label: "Run /discover", description: "Index the repos first."}
  ]
)
```

### Step 6b: Staleness refresh (separate AskUserQuestion)

Fire this step only when Step 1c detected staleness (discovery stale/absent OR Serena stale/first-time). If no staleness was detected in Step 1c → skip Step 6b entirely.

Present a dedicated second AskUserQuestion for refresh actions (after the main Step 6 task-resume picker). This keeps the main Step 6 picker capped at ≤4 options.

Build options dynamically:

- **If discovery is stale or absent:** include `label: "Refresh discovery"`, `description: "Run /discover (incremental — cheap if HEAD unchanged)"`. If `QUOIN_DISCOVERY_AUTOREFRESH=1`, run `/discover` inline without asking and omit this option from the picker.
- **If `serena.stale=true` (present-but-stale) OR `serena.present_marker=false` (absent marker):** probe with `ToolSearch select:mcp__serena__activate_project`. If probe loads a schema: include `label: "Set up / Refresh Serena memory"`, `description: "Run Serena onboarding or re-onboarding and update the staleness marker — prompt-only per D-04"`. On the user choosing this option: call the `serena-activation.md §Refresh` path (run `mcp__serena__activate_project` then `mcp__serena__onboarding` then `mcp__serena__initial_instructions`) then write/update `.workflow_artifacts/memory/serena-onboarded.md` with a 1-line body and ISO timestamp. If probe loads no schema → omit the Serena option entirely (Graceful Absence — do not mention Serena to users who may not have it).
- **Always include:** `label: "Skip refresh"`, `description: "Continue without refreshing."` as the last option.

Both discovery and Serena options may appear simultaneously if both are stale. The Serena refresh is prompt-only in interactive sessions — NEVER auto-run (D-04).

Example (both stale):
```
<!-- decision-gate: best-effort site=start_of_day-prompt-3 -->
AskUserQuestion(
  question="Discovery memory needs a refresh. What would you like to do?",
  options=[
    {label: "Refresh discovery", description: "Run /discover (incremental — cheap if HEAD unchanged)"},
    {label: "Set up / Refresh Serena memory", description: "Run Serena onboarding and update the staleness marker"},
    {label: "Skip refresh", description: "Continue without refreshing."}
  ]
)
```

## Handling multiple unfinished sessions

If the daily cache has multiple unfinished tasks:
- Present them all, ordered by suggested priority
- Note any dependencies between them (e.g., "Task B depends on Task A's review passing")
- Let the user pick which to resume

## No daily cache found

If there's no daily cache but the project has session files or git history:
- Read the most recent session files from `.workflow_artifacts/memory/sessions/`
- Read `.workflow_artifacts/memory/git-log.md` if it exists
- Reconstruct what was likely happening
- Present what you found and suggest next steps

If nothing exists at all:
- Suggest `/discover` to index the repos
- Ask the user what they'd like to work on

## Important behaviors

- **Be concise.** This is a morning briefing, not a novel. The user wants to know what to do, not re-read everything.
- **Surface surprises.** If overnight CI broke, or someone force-pushed to a branch, or a PR got rejected — lead with that.
- **Respect the user's time.** Don't make them re-read the entire plan. Summarize the resumption point in 2-3 sentences with the exact file/task/line to start at.
- **Check git for real.** The daily cache is what *should* be true. Git state is what *is* true. Always reconcile.
- **Prefer the `## For human` block.** If the daily cache is v3-format, display the `## For human` content as the primary orientation summary — it was written by the previous session's Haiku specifically to orient the next reader. Don't pad or paraphrase it; present it directly.
