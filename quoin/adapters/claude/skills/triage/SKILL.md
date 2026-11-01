---
name: triage
description: "Routes a prompt to the right skill; propose-only. Use for: /triage, 'what should I run', 'which skill fits this', 'route this', 'pick the right command for me', 'I'm not sure what to do next'."
model: haiku
---

# Triage — Prompt-to-Skill Router

*Portable intent doc: `quoin/core/skills/triage.md`*

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
        description: "triage dispatched at haiku tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in triage. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /triage`).
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

## Summary

`/triage` is a lightweight routing skill. It reads a user's natural-language prompt, inspects the current workflow state on disk, scores each candidate skill against an embedded catalog, and presents a ranked proposal with rationale. After confirming the best-fit skill, `/triage` always tells the user which command to type — it **never invokes skills itself**. This applies to every skill, not just `/implement` and `/end_of_task`. The propose-only posture for all 20 routable skills is intentional and permanent: no `Skill` tool calls happen from `/triage` at any point.

---

## Session Bootstrap

`/triage` is a routing skill. It does minimal setup:

1. **CLAUDE.md is injected by the harness** — no explicit read is needed.
If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

2. **Cost recording — explicit-only:** Record a cost-ledger entry ONLY if the user's prompt explicitly names a task folder (e.g., `/triage task=auth-refactor` or the prompt contains the exact folder name). In all other cases — including when exactly one non-reserved task folder exists — skip cost recording silently. This conservative policy prevents false ledger writes in multi-task-folder contexts. If the condition holds: append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `triage` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`.

<!-- quoin:ledger-self-write -->
3. **Session state:** `/triage` does NOT write `.workflow_artifacts/memory/sessions/` files. Routing is not "meaningful work" in the session-state rule sense.
4. **Trigger-phrase stripping** (see Step 1 below) happens before any scoring.

---

## Process

### Step 1: Strip trigger phrases

Before scoring, remove any exact match of the `/triage` description's trigger phrases (lowercased, trimmed) from the prompt. The trigger list to strip:

- "what should i run"
- "which skill fits this"
- "route this"
- "pick the right command for me"
- "i'm not sure what to do next"
- "/triage"

Stripping prevents the invocation phrase from polluting Signal B scoring. After stripping, normalize whitespace. The resulting text is the **scoring prompt**.

### Step 2: Collect workflow-state signals

Run the following checks. All are **best-effort and read-only** — if a command fails or a file is missing, treat the signal as "unknown" (0 points). Never error out. Evaluate once; do not re-evaluate mid-conversation. Active task folder checks **always exclude** `finalized/`, `memory/`, and `cache/` subdirectories.

| Signal | Detection | Skills boosted | Skills suppressed |
|--------|-----------|----------------|-------------------|
| No `.workflow_artifacts/` at project root | `ls .workflow_artifacts/ 2>/dev/null` returns empty/error | `/init_workflow` +2 | All task-execution skills −1 each |
| `repos-inventory.md` missing or >7 days old | `ls .workflow_artifacts/memory/repos-inventory.md 2>/dev/null` absent, or mtime >7 days (`stat -f %m` macOS / `stat -c %Y` Linux, fallback: treat absent as stale) | `/discover` +2 | `/architect`, `/plan`, `/thorough_plan` −1 each |
| At least one active task folder exists | `ls -d .workflow_artifacts/*/ 2>/dev/null` filtered: exclude `finalized/`, `memory/`, `cache/` | `/review`, `/implement`, `/gate` +1 each | `/init_workflow` −2 |
| Plan found at any depth under active task folder(s) | `find .workflow_artifacts -maxdepth 4 -name current-plan.md -not -path '*/finalized/*' -not -path '*/cache/*' -not -path '*/memory/*' 2>/dev/null` returns any result | `/implement` +2, `/critic` +1 (if no `critic-response-*.md` found near it) | `/plan` −1, `/thorough_plan` −1 |
| Plan found AND recent commits AND no `review-*.md` | Plan found (above) + `git log --since=1.day --oneline 2>/dev/null` non-empty + `find .workflow_artifacts -maxdepth 4 -name 'review-*.md' -not -path '*/finalized/*' 2>/dev/null` returns nothing | `/review` +2 | `/plan` −1, `/thorough_plan` −1 |
| `review-*.md` with verdict APPROVED | `grep -rl "APPROVED" .workflow_artifacts/*/review-*.md 2>/dev/null` non-empty | `/end_of_task` +2 | `/review` −1 |
| Uncommitted changes on current repo | `git status --porcelain 2>/dev/null` non-empty | `/implement` +1 | `/end_of_task` −1 (unless APPROVED) |
| On `main`/`master` with clean working tree | `git status 2>/dev/null` clean + `git branch --show-current 2>/dev/null` is `main` or `master` | `/start_of_day` +1, `/thorough_plan` +1 | `/end_of_task` −2 |
| Today's daily cache exists | `ls .workflow_artifacts/memory/daily/$(date +%Y-%m-%d).md 2>/dev/null` found | `/end_of_day` +1 | `/start_of_day` −1 |
| No daily cache for today AND no session file for today | Daily cache absent + `ls .workflow_artifacts/memory/sessions/$(date +%Y-%m-%d)-*.md 2>/dev/null` absent | `/start_of_day` +2 | — |
| Active session file with status `in_progress` | `awk 'FILENAME ~ /-orchestrator\.md$/{nextfile} FNR==1{s=0} /^## Status: in_progress/{print FILENAME; nextfile} s&&NF{if($0=="in_progress")print FILENAME; s=0} /^## Status$/{s=1}' .workflow_artifacts/memory/sessions/$(date +%Y-%m-%d)-*.md 2>/dev/null` non-empty | `/implement` +1, `/review` +1 | `/init_workflow` −2 |
| It's Friday (local date) | `date +%u 2>/dev/null` == 5 (uses local timezone — correct; `date +%u` respects the system clock) | `/weekly_review` +1 | — |
| Knowledge cache stale (>7 days) | `stat -f %m .workflow_artifacts/cache/_staleness.md 2>/dev/null` (macOS) or `stat -c %Y` (Linux) > 7 days, or file absent | `/discover` +1 | — |

### Step 3: Classify against the skill catalog

Apply the three-signal scoring algorithm:

**Signal A — Explicit command.** If the scoring prompt contains a literal `/<skill-name>` token matching any of the 20 routable skills (case-insensitive), that skill gets score **3** and skip directly to Step 4 proposal. No further scoring needed.

- Special rule: if `/revise-fast` is matched via Signal A, substitute `/revise` in the proposal with note: "`/revise-fast` is an internal variant; proposing `/revise` instead."
- `/revise-fast` is **never** included in any ranked list or ambiguous candidate display.

**Signal B — Trigger phrase match.** Match the scoring prompt (lowercased, whitespace-normalized) against the `Trigger phrases` column in the catalog (Section 6). Each exact phrase match = **2 points**. Each partial primary keyword match = **1 point**. Sum per skill.

**Signal C — Workflow state.** Apply the boost/suppress values from Step 2 to each skill's running total.

### Step 4: Rank candidates

Sort skills by total score (Signal B + Signal C) descending.

- **Single winner:** Top skill score ≥ runner-up by ≥ 2 points → single-candidate flow.
- **Ambiguous:** Top N candidates within 1 point, N ∈ {2, 3} → ambiguous-candidate flow.
- **Decline:** No skill scores above 1, OR N > 3 candidates tied, OR top candidates all have "High" impact with no disambiguating state signal → decline flow.

## §V Reconcile (read-only — no side-effect check)

<!-- §V-reconcile-begin -->
Before surfacing any task/PR status, run `python3 __QUOIN_HOME__/scripts/verify_claims.py --reconcile-tasks --project-root <project-root>` and derive the displayed status from the reconcile table, not from a cached narrative alone. If the reconcile exits 8, surface the contradiction rather than silently reporting the narrative version.
<!-- §V-reconcile-end -->

### Step 5: Present proposal

See Section 9 (Ambiguity Handling) for the exact dialogue format.

### Step 6: Handle confirmation

Once the user confirms any skill from any flow:

1. Print: `→ Type /<skill-name> to proceed.`
2. Stop. Do **not** call the `Skill` tool. This applies universally — no exceptions.

For decline/negation replies, apply the one-question cap (Section 9).

---

## Section 6: Skill Catalog

All 20 user-facing routable skills. `/revise-fast` is excluded (internal, not user-facing).

| Skill | Primary keywords | Trigger phrases | State signals that boost (+) | State signals that suppress (−) | Notes |
|-------|-----------------|-----------------|------------------------------|---------------------------------|-------|
| `/architect` | architect, system design, design, explore | "design the system", "architecture", "how should we build this", "explore the codebase", "technical exploration", "cross-repo" | `architecture.md` missing + task folder exists (+1) | Plan already exists (−1) | Large tasks; use before `/thorough_plan` |
| `/capture_insight` | note, insight, remember, log, save, pattern, gotcha | "note this", "remember this", "log this", "save this as a lesson", "remember that", "note that", "I want to capture" | Active session in_progress (+1) | — | Low-impact, quick logging |
| `/continue_work` | continue, resume, revive, prior session, last session, where was I | "resume prior session", "revive old session", "where was I", "switch to last session", "continue from last session" | `recent-sessions.md` exists (+1) | — | Sonnet tier; idle-session recovery skill |
| `/cost_snapshot` | cost, spend, spent, budget, how much | "how much have I spent", "cost report", "show costs", "project cost", "what has this cost" | — | — | Read-only; safe at any time |
| `/critic` | critic, critique, review the plan, find issues, gaps, risks | "critique this plan", "review the plan", "find issues with this plan", "what's wrong with this", "check the plan" | Plan found (+1), no critic-response yet (+1) | Recent commits exist + plan + no review (−2 — prefer `/review`) | Runs before `/implement` to catch plan gaps |
| `/discover` | discover, scan, map, repos, inventory, dependencies, index | "scan my repos", "map the codebase", "what repos do I have", "index the project", "save the architecture" | `repos-inventory.md` missing/stale (+2), cache stale (+1) | — | Run once on setup or when repos change |
| `/end_of_day` | end of day, EOD, wrapping up, done for the day, save progress | "wrapping up", "done for the day", "save my progress", "end of day", "EOD" | Today's daily cache exists (+1) | No session file today (−1) | Saves state and consolidates work |
| `/end_of_task` | end of task, finalize, push, ship, done, complete, wrap up task | "finalize this", "we're done", "ship it", "task complete", "wrap up this task", "push the branch" | APPROVED review found (+2) | On main/clean (−2), uncommitted changes without APPROVED (−1) | **High-impact.** Requires `/review` APPROVED first |
| `/enrich` | enrich, sharpen, tighten, fill gaps, sharpen the prompt | "sharpen this prompt", "tighten this task description", "fill in the gaps before we spec this" | No `spec.md`/`enriched-prompt.md` yet (+1) | Spec or enriched-prompt already exists (−1) | Route, don't rewrite — sits upstream of `/specify`; never writes a spec/plan |
| `/gate` | gate, check before, quality check, verify before next step | "check before proceeding", "run the gate", "verify before next step", "quality checkpoint" | Active task folder (+1) | No plan, no commits (−1) | Runs between phases; usually auto-invoked by workflow |
| `/implement` | implement, code, write code, build, start coding | "implementing a plan", "writing code from a plan", "start coding", "build this", "write the code", "let's code" | Plan found (+2), uncommitted changes (+1), active in_progress session (+1) | No plan found (−2) | **High-impact.** Requires explicit user command |
| `/init_workflow` | init, initialize, bootstrap, set up workflow, first time | "initialize workflow", "set up dev workflow", "install workflow", "bootstrap workflow", "first time setup" | No `.workflow_artifacts/` (+2) | Active task folder exists (−2), active session (−2) | Run once per project |
| `/plan` | plan, break down, how to implement, task breakdown | "plan this", "create a plan", "break this down", "how should I implement", "implementation plan" | `architecture.md` exists (+1) | Plan already found at any depth (−1) | Single-pass plan without critic loop |
| `/review` | review, verify, check implementation, code review, does this look right | "review my changes", "check the implementation", "verify implementation", "does this look right", "code review" | Plan + recent commits + no review file (+2), active in_progress session (+1) | No recent commits + no plan (−2) | Runs after `/implement`, before `/end_of_task` |
| `/revise` | revise, fix the plan, address feedback, update the plan | "fix the plan", "address the critic's comments", "update the plan based on feedback", "revise the plan" | `critic-response-*.md` exists (+2) | No critic-response exists (−2) | Used after `/critic`; Opus model |
| `/rollback` | rollback, undo, revert, go back, undo task | "undo the implementation", "revert the last changes", "go back to before implement", "undo task N", "reset to pre-implementation" | Uncommitted/recent commits exist (+1) | On main/clean (−1) | **High-impact.** Destructive — reverts commits |
| `/run` | run, do this, fix, build, add, implement, end to end | "do this task", "fix this bug", "implement X", "run the workflow", "end to end" | On main/clean (+1), no plan exists (+1) | Any uncommitted work (−1) | **Default for any task.** Light pipeline: brief, context pack, implement, one review, local commit. `/run hard:` runs the full heavy pipeline. |
| `/start_of_day` | start of day, SOD, morning, resume, what was I working on | "what was I working on", "resume", "pick up where I left off", "morning standup", "start of day", "SOD" | No daily cache today + no session file today (+2) | Today's daily cache exists (−1) | Morning context restore |
| `/thorough_plan` | thorough plan, plan thoroughly, planning cycle | "plan this thoroughly", "detailed plan with review", "plan and critique", "full planning cycle" | No plan exists (+1) | Plan already found (−1) | Heavy pipeline only: use when the user explicitly wants a reviewed plan, or the light `/run` has failed |
| `/weekly_review` | weekly, week, recap, friday, weekly summary, week review | "weekly summary", "what did I do this week", "week recap", "friday review", "weekly standup", "weekly report" | It's Friday (+1) | Recent commits today only (−1 — likely mid-task) | Run on Fridays or for a week-level summary |

---

## Section 7: Cross-Skill Disambiguation Table

When keyword collisions produce a tie, use state signals to break it. These rules layer on top of the catalog scores.

| Keyword(s) | Competing skills | Disambiguator |
|-----------|-----------------|---------------|
| "plan", "break down" | `/plan`, `/thorough_plan`, `/revise`, `/critic` | No plan exists → boost `/thorough_plan` +1, suppress `/revise` −2, suppress `/critic` −2. Plan exists → suppress `/thorough_plan` −1; boost `/revise` +1 if `critic-response-*.md` exists. |
| "review", "check", "look at" | `/review`, `/critic`, `/weekly_review` | Plan + recent commits → boost `/review` +2, suppress `/critic` −2, suppress `/weekly_review` −2. No recent commits, no plan → boost `/critic` +1. Day is Friday → boost `/weekly_review` +1. |
| "implement", "code", "write" | `/implement`, `/run` | No plan found → suppress `/implement` −2, boost `/run` +2. Plan found + no review → boost `/implement` +1. |
| "commit", "ship", "push", "done", "finish" | `/end_of_task`, `/implement` | APPROVED review found → boost `/end_of_task` +2, suppress `/implement` −1. No review yet → suppress `/end_of_task` −2, boost `/implement` +1. |
| "start", "begin", "morning", "resume" | `/start_of_day`, `/thorough_plan`, `/init_workflow` | No daily cache today → boost `/start_of_day` +2. Task folder exists → suppress `/init_workflow` −2. |
| "run", "everything", "full pipeline" | `/run`, `/thorough_plan` | Any uncommitted work → suppress `/run` −1. User says "full" or "end to end" → boost `/run` +1; user says "thorough" or "hard" → propose `/run hard:`. |

---

## Section 8: Invocation Policy

**All 20 routable skills are propose-only.** After the user confirms, `/triage` prints the command to type and stops. No `Skill` tool call is ever made.

**Rationale:**
- No Haiku skill in this workflow uses the `Skill` tool. Only Opus orchestrators (`/run`, `/thorough_plan`) invoke other skills via `Skill` tool dispatch. Running a `Skill` tool call from a Haiku model is unverified and potentially unsupported.
- Universal propose-only eliminates this uncertainty entirely. It also means no exception clauses are needed in any target skill's SKILL.md (per lessons 2026-04-13 and 2026-04-14: when a skill adds exception clauses, the *target* skill's SKILL.md must be updated too — universal propose-only sidesteps this entirely).
- User friction is minimal: the user sees the proposed command and types it.

| Skill | Impact level | Rationale for propose-only |
|-------|-------------|---------------------------|
| `/architect` | Medium | Universal propose-only. User types `/architect`. |
| `/capture_insight` | Low | Universal propose-only. |
| `/continue_work` | Low | Universal propose-only; recovery skill. |
| `/cost_snapshot` | Low | Universal propose-only; read-only. |
| `/critic` | Medium | Universal propose-only. |
| `/discover` | Medium | Universal propose-only. |
| `/end_of_day` | Low | Universal propose-only. |
| `/end_of_task` | **High** | Hard rule in CLAUDE.md — explicit invocation only. Universal policy reinforces this. Per lessons 2026-04-13/2026-04-14. |
| `/gate` | Medium | Universal propose-only; additionally a context-sensitive checkpoint — user should consciously invoke. |
| `/implement` | **High** | Hard rule in CLAUDE.md — explicit invocation only. Universal policy reinforces this. Per lessons 2026-04-13/2026-04-14. |
| `/init_workflow` | Low | Universal propose-only. |
| `/plan` | Medium | Universal propose-only. |
| `/review` | Medium | Universal propose-only. |
| `/revise` | Medium | Universal propose-only. |
| `/rollback` | **High** | Destructive — reverts commits. Universal policy reinforces safety. |
| `/run` | Medium | Default task pipeline; commits locally, never pushes. Universal propose-only policy still applies. |
| `/start_of_day` | Low | Universal propose-only. |
| `/thorough_plan` | Medium | Universal propose-only. Standard planning entry point. |
| `/weekly_review` | Low | Universal propose-only. |

`/revise-fast` is NOT in this table — it is not a user-facing routing target.

---

## Section 9: Ambiguity Handling

### Single-candidate flow

```
Proposed skill: /name
Rationale: <one sentence citing the top 1-2 scoring signals>

→ Type /name to proceed, or say "different skill" to see alternatives.
```

**Reply handling:**
- "different skill" → re-enter with the next-highest candidate.
- Any reply containing a `/<cmd>` token (e.g., "/plan") → treat as Signal A short-circuit on that new command; re-classify.
- Affirmative reply (case-insensitive, ≤ 5 words, no `/<cmd>` token): "yes", "go", "do it", "ok", "sure", "proceed", "yep", "sounds good", "yeah", "correct" → print `→ Type /name to proceed.` and **stop**.
- Negation reply: "no", "cancel", "stop", "nope", "nevermind" → stop. Print: "Would you like to describe what you're trying to do, or type a specific /command?"

**Propose-only output (always):** After any affirmation, print `→ Type /skill-name to proceed.` Never call the `Skill` tool.

### Ambiguous-candidate flow

Triggers when top N candidates are within 1 point of each other, N ∈ {2, 3}.

```
Multiple candidates match your request. Ranked:

1) /first  — <rationale + top signal>
2) /second — <rationale + top signal>
3) /third  — <rationale + top signal>  (only if N=3)

Which would you like? (type a number, describe more, or type the command directly)
```

**Reply handling:**
- "1", "2", or "3" → proceed as single-candidate flow for that skill.
- Reply containing `/<cmd>` → Signal A short-circuit; re-classify.
- Free-text → re-classify with new text appended. If still ambiguous, ask **ONE** clarifying question (hard cap):
  > "Are you planning new work, reviewing existing work, or wrapping up a completed task?"
- If the response to that one question is still ambiguous or vague → **hard exit**: "I couldn't determine which skill fits — please type the command directly or run `/help` to see available skills." No further iteration.

### Decline flow

Triggers when no skill scores above 1, OR N > 3 candidates tied, OR top candidates all have High impact with no disambiguating state signal.

```
I'm not sure which skill fits. Could you clarify:

- Are you planning, implementing, reviewing, or wrapping up?
- Is this a new task or continuation of an existing one?
```

One question cycle only. If still ambiguous → **hard exit**: "Please type the command directly, or run `/help` to see available skills."

---

## Section 10: Edge Cases & Guardrails

**Explicit-command short-circuit (Signal A):** If the scoring prompt (after trigger-phrase stripping) contains a literal `/<skill-name>` token, bypass all other scoring. Propose that skill immediately with score 3. Exception: `/revise-fast` → substitute `/revise` with note.

**Multi-stage plan detection:** The plan existence signal uses recursive `find` (maxdepth 4) — not flat file existence. This correctly handles the stage-subfolder layout from lesson 2026-04-18 (e.g., `.workflow_artifacts/memory-cache/stage-2/current-plan.md`).

**Multi-task-folder handling:** When multiple active task folders exist and the user's prompt does not name one, state signals read across all folders (e.g., "any plan found" fires if any folder has a plan). Cost recording is skipped silently.

**Trigger-phrase stripping:** The stripping in Step 1 is case-insensitive and trimmed. Only exact phrase matches are stripped — not substrings. After stripping, normalize whitespace.

**`revise-fast` hidden:** Never appear in ranked lists, ambiguous candidate lists, or proposals. If Signal A matches `/revise-fast`, substitute `/revise` with the note: "`/revise-fast` is an internal variant; proposing `/revise` instead."

**Error-tolerant state checks:** Every shell command in Step 2 appends `2>/dev/null`. Failed checks contribute 0 points. `/triage` never errors out due to a missing file or failed command.

**One-clarification-question hard cap:** Across all flows (single, ambiguous, decline), `/triage` asks at most one clarifying question per session. If still ambiguous after that one question, always hard exit. No loops.

**`/triage` on itself:** If the user types `/triage` as their prompt, Signal A fires immediately and proposes `/triage` (a self-referential loop). Handle this gracefully: print "You're already running `/triage`. Please describe what you're trying to accomplish so I can route you to the right skill."

---

## Section 11: Important Behaviors

- **Never call the `Skill` tool.** This is a hard constraint, not a preference. Universal propose-only for all 18 skills.
- **Scoring prompt is stripped input.** Always score the trigger-phrase-stripped, whitespace-normalized prompt — never the raw input.
- **State signals are evaluated once** at bootstrap. Do not re-evaluate mid-conversation.
- **`/revise-fast` is invisible to users.** Never surface it in any output.
- **When adding a new skill to the workflow, append a row to the catalog in Section 6.** The catalog is the routing source of truth — without a row, the skill is unroutable via `/triage`.
- **`/triage` is not a gate.** It does not block phase transitions. It is a convenience router, not a checkpoint.
- **Propose-only output is always the same format:** `→ Type /skill-name to proceed.` Consistency helps users trust the output and know they must type the command.
