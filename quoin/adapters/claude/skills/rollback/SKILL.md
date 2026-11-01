---
name: rollback
description: "Undoes an implementation phase or specific tasks, mapping commits to plan tasks; asks for confirmation. Use for: /rollback, 'undo the implementation', 'undo task 3'."
model: sonnet
---

# Rollback

*Portable intent doc: `quoin/core/skills/rollback.md`*

You safely undo implementation work by mapping commits to plan tasks and reverting cleanly. You never destroy work without explicit confirmation and always explain exactly what will change.

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
        description: "rollback dispatched at sonnet tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in rollback. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /rollback`).
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


<!-- §0-sidecar-begin -->
  Source-mutating dispatch — two-phase worktree isolation (D-08):

  STEP A0 — Consult the worktree-isolation decider FIRST (default is skip):
     Run via Bash:
       python3 __QUOIN_HOME__/scripts/worktree_isolation.py --decide
     Isolation is opt-in (D-04): the decider prints `skip` unless
     QUOIN_WORKTREE_ISOLATION=on, the dispatch.json config opts in, or a prior probe
     wrote a `works` sentinel. If the output is `skip`, DO NOT write the sidecar and
     DO NOT dispatch with isolation: "worktree" — skip STEP A / STEP B / STEP C and go
     straight to a PLAIN Agent dispatch at the declared cheap-tier model (sonnet), with
     no sidecar write and no worktree round-trip. Only when the output is `attempt` do
     STEP A / STEP B / STEP C run.

  STEP A — Write the dispatch sidecar BEFORE calling the Agent tool:
     Run via Bash:
       PROJECT_ROOT="$(python3 __QUOIN_HOME__/scripts/path_resolve.py --print-project-root)"
       python3 __QUOIN_HOME__/scripts/dispatch_sidecar.py \
           --skill <skill-name> \
           --project-root "$PROJECT_ROOT" \
           --plan "<resolved-plan-path-or-empty>"
     (The WorktreeCreate hook reads this sidecar to resolve the nested git root.)

  STEP B — Phase 1: Agent dispatch WITH isolation: "worktree" (normal path):
     Call the Agent tool with isolation: "worktree" at the declared cheap-tier
     model (sonnet for this skill). The deployed WorktreeCreate hook at
     __QUOIN_HOME__/hooks/worktreecreate.sh reads the sidecar, runs
     git_root_for_dispatch.py, and (when a single nested repo resolves)
     creates a worktree IN the nested git root and returns its path.
     One-time probe (opt-in path only): when the probe sentinel is still unknown,
     instruct the child to record its working directory to a marker; after the Agent
     returns, compare it to the created worktree path and persist the result exactly
     once via
       python3 __QUOIN_HOME__/scripts/worktree_isolation.py --write-probe --result works|broken

  STEP C — Phase 2 retry WITHOUT isolation (on Worktree-class error):
     If Phase 1 fails with a Worktree-class error (regex above), the hook
     either returned skip (no stdout → harness fails) or encountered an error.
     Re-dispatch the Agent call WITHOUT isolation: "worktree", at the SAME
     declared cheap-tier model (sonnet). Do NOT escalate to parent tier.
     Emit one-line audit:
       [quoin-stage-1: worktree dispatch skipped; proceeding at sonnet without isolation]
     Autonomous fail-OPEN: if the incoming prompt carries the `[autonomous]`
     sentinel, then on any worktree-class dispatch error, proceed at current
     tier fail-OPEN and do NOT call AskUserQuestion — this is already
     guaranteed unconditionally by this Phase 2 retry (no AskUserQuestion
     exists in this path to skip), so behavior here is identical with or
     without the sentinel.

  STEP D — Done:
     No child-side coordination required. The harness handles cwd correctly:
     on Phase 1 success, child sees the worktree as cwd; on Phase 2, child
     inherits the parent's session cwd (today's behavior, unchanged).
<!-- §0-sidecar-end -->

  - Worktree-class branch: handled by Phase 2 (§0-sidecar block above).
    Phase 2 retries at the declared cheap-tier model without isolation.
    Do NOT use AskUserQuestion or proceed-current-tier for source-mutating skills.

  - Other-class path (non-worktree Agent errors):
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
    description: "rollback — min-tier up-dispatch"
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
      switch with /model and re-invoke /rollback]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on sonnet up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from a Sonnet session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /rollback from a Sonnet session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0tripleprime-end -->

## Session bootstrap

On start:
0. Parse the `[no-interactive]` sentinel from the incoming prompt (leading, stackable, strip before further parsing) into `_INTERACTIVE=false` (default `_INTERACTIVE=true`). `/rollback` has no `[autonomous]` arm; when `_INTERACTIVE` is false the destructive-undo confirm (Step 4) FAILS CLOSED instead of proceeding. See `__QUOIN_HOME__/memory/decision-gate-guard.md`.
1. Read `<task_dir>/current-plan.md` to understand task structure. Resolve `<task_dir>` via `python3 __QUOIN_HOME__/scripts/path_resolve.py --task <task-name> [--stage <N-or-name>]`. architecture.md: ALWAYS `<task-root>/architecture.md`. cost-ledger.md: ALWAYS `<task-root>/cost-ledger.md` (line 2 below — NOT edited per D-03). If exit code 2: display stderr verbatim, fall back to task root, ask user to disambiguate.
2. Append your session to the cost ledger: `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `rollback` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`. If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

<!-- quoin:ledger-self-write -->

## Core principle

**Show before you act.** Always display what will be reverted, let the user confirm, then execute. Never auto-revert.

## Process

### Step 1: Understand the state

Read:
1. **The plan** — `<task_dir>/current-plan.md` to understand task structure (where `<task_dir>` is resolved per Session bootstrap step 1 via `python3 __QUOIN_HOME__/scripts/path_resolve.py`)
2. **Git log** — map recent commits to plan tasks (by commit message, branch, or scope)
3. **Current diff** — any uncommitted changes that would be affected
4. **Session state** — what phase we're in and what's been completed

Build a commit-to-task map:

```
Task 1: "Add retry logic to payment service"
  → commit abc1234: feat(payment): add exponential backoff to processRefund
  → commit def5678: test(payment): add retry logic tests

Task 2: "Update API gateway routing"
  → commit ghi9012: feat(gateway): add /v2/payments route

Task 3: "Add monitoring dashboards"
  → (no commits — not yet implemented)

Uncommitted:
  → src/payment/config.ts (modified, unstaged)
```

### Step 2: Determine rollback scope

Use AskUserQuestion to ask the user what they want to undo:

<!-- decision-gate: best-effort site=rollback-scope -->
```
AskUserQuestion(
  question="What would you like to roll back?",
  options=[
    {label: "Full phase rollback", description: "Revert everything from /implement to the pre-implementation state. Cleanest option."},
    {label: "Selective task rollback", description: "Revert specific tasks only; dependencies will be flagged."},
    {label: "Last commit only", description: "Quick undo of the most recent commit."}
  ]
)
```

**Full phase rollback** — revert everything from `/implement`:
- Find the commit or branch point where implementation started
- This is the cleanest option

**Selective task rollback** — revert specific tasks:
- Use the commit-to-task map to identify which commits to revert
- Warn about dependencies ("Task 3 depends on Task 2 — reverting Task 2 will also require reverting Task 3")

**Last commit only** — quick undo of the most recent change

### Step 3: Show the impact

Present exactly what will happen:

```markdown
# Rollback preview

## Scope: <full phase / tasks 2-3 / last commit>

### Commits to revert (newest first)
1. `ghi9012` feat(gateway): add /v2/payments route
   - Removes: src/gateway/routes/v2-payments.ts
   - Modifies: src/gateway/routes/index.ts (route registration removed)

2. `def5678` test(payment): add retry logic tests
   - Removes: test/payment/retry.test.ts

3. `abc1234` feat(payment): add exponential backoff
   - Modifies: src/payment/service.ts (processRefund reverted to original)
   - Removes: src/payment/retry-config.ts

### Uncommitted changes that will be affected
- src/payment/config.ts — ⚠️ has unsaved changes, will stash first

### After rollback
- Branch `feat/payment-retry` will be at commit <hash> (pre-implementation state)
- Plan tasks 1-2 will need re-implementation
- Task 3 was never started — unaffected

### Dependencies to check
- No other branches depend on these commits
- No open PRs reference these commits
```

### Step 4: Confirm and execute

Use AskUserQuestion to get explicit user confirmation before proceeding:

<!-- decision-gate: fail-closed site=destructive-undo -->
```
AskUserQuestion(
  question="Proceed with the rollback as shown above?",
  options=[
    {label: "Confirm rollback", description: "Execute the rollback as previewed above."},
    {label: "Cancel", description: "Abort; no changes will be made."}
  ]
)
```

**Non-interactive / `[no-interactive]` fail-closed (rollback has NO `[autonomous]` arm — it is
source-mutating and user-invoked, so it never auto-confirms a destructive undo):** if
`_INTERACTIVE` is false (the `[no-interactive]` sentinel was parsed and stripped at bootstrap,
or `AskUserQuestion` is unavailable — e.g. an Agent subagent, where it is not provisioned),
a human cannot confirm this destructive operation: FAIL CLOSED — do NOT execute the rollback
and do NOT default to "Confirm". Run
`python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill rollback --site destructive-undo --reason "destructive rollback confirmation could not be surfaced" --resume-hint "re-run /rollback interactively"`,
echo its `gate-result: NEEDS-DECISION` block as the final message, and STOP. Rule doc:
`__QUOIN_HOME__/memory/decision-gate-guard.md`.

If the user selects "Cancel": STOP. Tell the user: "Rollback cancelled. No changes were made."

Then (after "Confirm rollback"):

**For full phase rollback:**
```bash
# Stash any uncommitted changes first
git stash push -m "rollback-stash-$(date +%Y%m%d-%H%M%S)"

# Reset to pre-implementation point
git reset --hard <pre-implementation-commit>

# Or if on a feature branch, reset to branch point
git reset --hard <base-branch-merge-base>
```

**For selective task rollback:**
```bash
# Revert specific commits in reverse order (newest first)
git revert --no-commit <commit-hash-3>
git revert --no-commit <commit-hash-2>
git revert --no-commit <commit-hash-1>

# Stage and commit as one revert
git commit -m "revert: undo tasks 2-3 from <task-name>

Reverted commits: ghi9012, def5678
Reason: <user's reason or 'requested by user'>
Tasks reverted: Task 2 (API gateway routing), Task 3 (monitoring)
Tasks preserved: Task 1 (retry logic)"
```

**For last commit:**
```bash
git revert HEAD --no-edit
```

### Step 5: Update session state

After rollback:
- Update the session file (`.workflow_artifacts/memory/sessions/<date>-<task-name>.md`) to the fixed schema in `__QUOIN_HOME__/memory/session-schema.md`: Current stage `rollback`, Next command `/implement`, rolled-back tasks back in Unfinished work
- Note the rollback as a Decisions made item
- Update `.workflow_artifacts/memory/git-log.md` with the revert commits

### Step 6: Report

Tell the user:
- What was reverted
- Current state of the branch
- Which plan tasks are now pending again
- Whether any stashed changes need to be restored (`git stash pop`)
- What to do next (re-implement, modify the plan, etc.)

## Safety rules

- **Never force-push.** Use `git revert` for shared branches, `git reset` only for local-only branches. Ask first.
- **Stash before resetting.** Always preserve uncommitted work.
- **Check for dependents.** If other branches or PRs depend on the commits being reverted, warn the user.
- **Preserve the plan.** Rollback reverts code, not planning artifacts. The plan, critic responses, and review docs stay intact.
- **Log the rollback.** Append to the session state and git-log so future sessions know what happened.

## Edge cases

**Rollback after partial merge/PR:**
- If commits were already pushed, use `git revert` (not reset)
- If a PR was merged, create a revert PR instead

**Rollback with database migrations:**
- Warn the user that code rollback doesn't undo database changes
- List any migration files that were part of the rolled-back commits
- Suggest running down-migrations if they exist

**Rollback across repos:**
- If implementation touched multiple repos, rollback each independently
- Present the full cross-repo impact before confirming
