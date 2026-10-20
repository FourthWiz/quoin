---
name: weekly_review
description: "Aggregates the week's work into a structured review from daily caches, sessions and git history. Use for: /weekly_review, 'weekly summary', 'what did I do this week'."
model: haiku
---

# Weekly Review

You produce a structured summary of the week's meaningful work — what was accomplished, what's still in flight, key decisions made, and lessons learned. Designed to run on Friday (or whenever the user wants a week-level view).

*Portable intent doc: `quoin/core/skills/weekly_review.md`*

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
        description: "weekly_review dispatched at haiku tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in weekly_review. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /weekly_review`).
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

## Process

### Step 1: Determine the week range

Default to the current week (Monday through today). If the user specifies a different range, use that.

```bash
# Monday of this week
date -v-Mon +%Y-%m-%d 2>/dev/null || date -d 'last monday' +%Y-%m-%d
```

### Step 2: Gather sources

Read these in parallel:

1. **Daily caches** — `.workflow_artifacts/memory/daily/<date>.md` for each day in the range. These are the primary source — they contain completed work, unfinished carry-forwards, decisions, and git summaries.

2. **Session files** — use the shared helper `__QUOIN_HOME__/core/scripts/select_unprocessed_sessions.py`
   with `--lower-bound-source weekly` to enumerate session files in scope:
   ```
   python3 __QUOIN_HOME__/core/scripts/select_unprocessed_sessions.py \
     --window <monday>..<saturday> \
     --lower-bound-source weekly
   ```
   The helper's weekly-mode `lower_bound` is computed as `(most_recent_prior_weekly_file_date + 7 days)`,
   or `(today - 7 days)` if no prior weekly file exists. The selection rule is identical to
   `/end_of_day`: a session file is in scope iff `end_of_day_due: yes` (legacy files without
   the field treated as `yes`) AND file_date is within the window. These have finer-grained
   detail on individual tasks.

   Additionally: session files in the window whose `end_of_day_due: no` flag AND whose slug is
   absent from every daily body covering that week are orphaned sessions. Surface these in the
   weekly review under a new "Orphaned sessions detected" subsection (do NOT include in
   completed-tasks rollup — the user must run `/end_of_day --recover-orphans` separately to
   recover them).

3. **Git history** — for each repo in the project folder:
   ```bash
   git -C <repo> log --all --oneline --after="<monday>" --before="<saturday>" --date=short --format="%h %s — %ad (%an)"
   ```
   Count commits, identify active branches, note merged PRs.

4. **Git log** — `.workflow_artifacts/memory/git-log.md` for the rolling commit log with context.

5. **Lessons learned** — `.workflow_artifacts/memory/lessons-learned.md` — filter for entries dated within this week.

6. **Task folders** — scan `.workflow_artifacts/` for task subfolders that were created or modified this week. Check for `architecture.md`, `current-plan.md`, `review-*.md` artifacts to understand what stages tasks went through.

7. **Cost data** — from the daily caches (source 1), read the `## Cost summary` section for each day. Aggregate session counts per task across the week. Do NOT run `npx ccusage` — Haiku does not orchestrate cost lookups. Dollar amounts come from `/end_of_task` reports.

   Also from the same `## Cost summary` sections, extract each day's `Day total verification mismatches: <K>` / `Window total verification mismatches: <K>` line (via regex `total verification mismatches:\s*(\d+)`, whichever label that day used) and sum `<K>` across all days in the week — `end_of_day` already computed the per-day total, so this step only adds those totals together (reuse, not re-derive). Days lacking that line contribute 0. If the week's sum is > 0, surface a `Verification mismatches this week: <N>` line in the review's Cost/Highlights area with the note "non-zero = a skill's claims contradicted live state during the week; investigate before trusting its output."

### Step 3: Build the review

Produce the weekly review in this format:

```markdown
# Weekly Review — <YYYY-MM-DD> to <YYYY-MM-DD>

## For human

<5-8 line plain-English summary: what was the week's focus; which tasks completed or made major progress; what remains in-flight; what matters most for next week. Written inline by the Haiku writer in the same generation as the body — no separate summarizer call>

## Highlights
<3-5 bullet points: the most important things that happened this week. Lead with outcomes, not activity.>

## Completed Work

### <task-name>
- **What:** <1-2 sentence description of the deliverable>
- **Stages:** <which workflow stages it went through, e.g., architect -> plan -> implement -> review>
- **Branch/PR:** <branch name, PR URL if available>
- **Key commits:** <most significant commits with short descriptions>
- **Impact:** <what this unblocks or enables>

### <task-name-2>
...

## In Progress

### <task-name>
- **Current stage:** <where it's at in the workflow>
- **Progress:** <what's done vs what remains, e.g., "4 of 7 tasks implemented">
- **Branch:** <branch name>
- **Blockers:** <if any>
- **ETA signal:** <is it on track, slower than expected, or blocked?>

### <task-name-2>
...

## Decisions Made
| Decision | Rationale | Task | Date |
|----------|-----------|------|------|
| <what was decided> | <why> | <which task> | <when> |

## Lessons Learned This Week
<entries from lessons-learned.md dated this week, or new insights from reviewing the week>

## Git Activity
- **Commits:** <total> across <N> repos
- **Active branches:** <list>
- **Merged:** <branches merged this week>
- **Repos touched:** <which repos had activity>

## Metrics (if available)
- Tasks completed: <N>
- Tasks started: <N>
- Critic-revise rounds (avg): <N> (if thorough_plan was used)
- Rollbacks: <N>

## Weekly cost summary
<!-- Derived from daily cache cost summaries — no ccusage calls -->
| Task | Sessions | Notes |
|------|----------|-------|
| <task-name> | <N> | phases: <plan, critic, implement, review> |
| <task-name-2> | <N> | phases: <plan, implement> |
| **Week total** | **<N>** | across <M> tasks |

*Dollar amounts: check each task's /end_of_task report, or run `npx ccusage` manually.*

## Next Week
<List the in-progress tasks that should continue, any blockers to resolve, and any new work the user mentioned. Do not speculate about priorities beyond what the data shows.>
```

For the Highlights section: each bullet should state a concrete outcome or deliverable, not a process step. Use this pattern: "<What was delivered/decided> — <why it matters or what it unblocks>". If a task is still in progress, it is not a highlight unless it hit a significant milestone.

### Step 4: Save the review

Compose the `## For human` block directly in the same generation pass as the body — do not invoke any separate summarizer or validator script; the weekly review is written inline by Haiku. The block should be 5-8 lines of plain English placed immediately after the H1 heading.

Write the review to:
```
.workflow_artifacts/memory/weekly/<YYYY-WNN>.md
```

Where `WNN` is the ISO week number (e.g., `2026-W12`). Create the `.workflow_artifacts/memory/weekly/` directory if it doesn't exist.

## §V Ground-truth verification (execute after the skill's work, before the final report)

<!-- §V-verify-begin -->
Before Step 5 (Present to the user), reconcile the week's rollup — never on a hardcoded
line number, always immediately before this skill's own final report step.

Run `python3 __QUOIN_HOME__/scripts/verify_claims.py --reconcile-tasks --project-root
<project-root>` (live gh). Before asserting any task "completed" or "merged" in the
`## Completed Work` section, confirm the reconcile table agrees (`finalized: true` or
`pr: MERGED`) — never assert completion from the daily-cache narrative alone.

If the reconcile exits 8: surface each MISMATCH under `## Decisions Made` (or a dedicated
note) rather than silently completing the rollup.
<!-- §V-verify-end -->

### Step 5: Present to the user

Display the review and ask:

> "Anything to add or correct? Any wins or frustrations I missed?"

If the user adds context, update the review file.

Then ask:

> "Want me to capture any lessons from this week?"

If yes, append to `.workflow_artifacts/memory/lessons-learned.md`.

## Handling sparse data

Not every day will have a daily cache (maybe the user didn't run `/end_of_day` every day). That's fine — fill gaps from:

1. Session files for that date
2. Git history for that date
3. Task folder modification timestamps

If there's very little data for the week, say so honestly. Don't inflate thin activity into a long report. A short week gets a short review.

## Handling multi-week tasks

Some tasks span multiple weeks. For in-progress items that started before this week:
- Note when they started
- Describe only this week's progress, not the full history
- Reference previous weekly reviews if they exist

## Important behaviors

- **Lead with outcomes, not activity.** "Shipped payment retry logic — reduces failed transactions by handling Stripe timeouts" beats "Modified 14 files across 3 services."
- **Be honest about pace.** If a task is taking longer than expected, say so. This helps the user calibrate.
- **Keep it scannable.** This review might be shared with a manager or team. Use tables, bullet points, and clear headers. No walls of text.
- **Connect related work factually.** If tasks are related, note the observable connection (e.g., "Task B was created as a follow-up to Task A"). Do not infer intent, mood, or momentum. When data is sparse, keep the review short rather than padding with interpretation.
- **Don't fabricate.** If you don't have data for a day, say "no recorded activity" rather than guessing.
