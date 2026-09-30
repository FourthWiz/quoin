---
name: end_of_task
description: "Finalizes a completed, reviewed task: commits, pushes the branch, captures lessons, marks complete. Use for: /end_of_task, 'finalize this', 'we're done', 'ship it', 'task complete', 'wrap up this task'. Requires /review first; does NOT create a PR."
model: sonnet
---

# End of Task

*Portable intent doc: `quoin/core/skills/end_of_task.md`*

You finalize a completed task. This is the user's explicit acceptance that the work is done — reviewed, approved, and ready to ship. You handle the git ceremony (commit, push to branch), capture lessons, aggregate task cost, and close out the task cleanly. **You do NOT create a PR** — that's a separate, explicit action the user takes when they're ready.

**CRITICAL: You must verify that `/review` was run before proceeding.** If no `review-*.md` file exists in the task folder, STOP and tell the user to run `/review` first.

**IMPORTANT: Fresh session recommended.** This skill has 8 sequential steps that must all complete (pre-flight, commit, push, lessons, session state, cost aggregation, archive, report). If the current session has been through heavy work (`/thorough_plan`, `/implement`, `/review`), start a fresh session for `/end_of_task` — context compaction mid-skill can silently skip steps like archiving.

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
        description: "end_of_task dispatched at sonnet tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in end_of_task. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /end_of_task`).
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
    description: "end_of_task — min-tier up-dispatch"
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
      switch with /model and re-invoke /end_of_task]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on sonnet up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from a Sonnet session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /end_of_task from a Sonnet session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0tripleprime-end -->

## §0b Session-age guard (FIRST STEP after §0 dispatch)

This skill has 8 sequential steps; running it in a heavy / long-lived
session is a known cause of stream-idle timeouts (Apr 28 18:13 incident).
Before doing any work, check session activity age. For the ordered rule below, first parse
(and strip) the `[no-session-age-guard]`, `[autonomous]`, and `[no-interactive]` leading
sentinels from the incoming prompt (`[autonomous]`→`_AUTONOMOUS=true`,
`[no-interactive]`→`_INTERACTIVE=false`; defaults false/true).

<!-- decision-gate: fail-closed site=session-age -->
```
python3 __QUOIN_HOME__/scripts/session_age_guard.py --threshold-hours 6.0 --project-root "$(pwd)"
```

The guard's exit-code contract is UNCHANGED (still exit 1 only on `OVER`, exit
0 otherwise, fail-OPEN on other codes). On **exit 1 (`OVER|...`)**, evaluate this ORDERED rule
(IVG-146 absorbed; composes AC-7/AC-8 with the fail-closed contract —
`__QUOIN_HOME__/memory/decision-gate-guard.md`):

1. **`[no-session-age-guard]` present** → strip + BYPASS the guard entirely (UNCHANGED
   power-user path); continue to `## When to use`.
2. **`_AUTONOMOUS`** → preserve the current autonomous behavior EXACTLY (pinned, do NOT change):
   STOP on `OVER` with the verbatim message below. Autonomous `/end_of_task` is NOT bypassed
   (`/run` injects `[autonomous]`, never `[no-session-age-guard]`), so an autonomous OVER stops
   exactly as it does today — this task does not newly STOP nor newly bypass an autonomous run.
3. **`[no-interactive]` / non-interactive (and NOT `_AUTONOMOUS`)** → FAIL CLOSED: a human
   cannot pick a session-age option in a background context, so run
   `python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill end_of_task --site session-age --reason "session over the age cap; proceed/checkpoint/abort decision could not be surfaced" --resume-hint "re-run /end_of_task interactively, or prefix [no-session-age-guard]"`,
   echo its `gate-result: NEEDS-DECISION` block as the final message, and STOP — it emits no envelope here even under `return: envelope`.
4. **else (interactive)** → present an `AskUserQuestion` 3-option list (IVG-146 UX):
   - **Proceed in this session** — continue `/end_of_task` now despite the age (override).
   - **Checkpoint and finish in a fresh session (recommended)** — run `/checkpoint`, then STOP
     so the user re-runs `/end_of_task` in a fresh chat.
   - **Abort** — stop with no changes.

Verbatim STOP message (rule step 2, and the informational text for the option list):
  "Current session has been active for Xh — over the 6h soft cap.
   /end_of_task is failure-prone in long sessions. Please:
     1. Run /end_of_day to save state
     2. Open a fresh chat and re-run /end_of_task
   Override at your own risk by re-invoking with prefix
   [no-session-age-guard] /end_of_task"

If exit 0 (`OK|...`): continue to ## When to use.

If the helper is missing OR exits with a non-0/1 code: emit the warning
`[session-age-guard: helper unavailable; proceeding]` and continue
(fail-OPEN, mirrors §0 dispatch fail-OPEN per architecture I-01).

Manual override: prefix the user invocation with `[no-session-age-guard]`
to skip the check entirely. Strip the sentinel before processing.

## Autonomous mode bootstrap

Parse the incoming prompt for the `[autonomous]` sentinel (may stack after `[no-redispatch]`,
e.g. `[no-redispatch] [autonomous]`, since `/run` prefixes it onto this skill's terminal
Phase-6 spawn). If present, set internal state `_AUTONOMOUS=true` for the remainder of this
skill's execution; strip the sentinel before further parsing. Default `_AUTONOMOUS=false`
(opt-in only).

Also parse the `[no-interactive]` sentinel (leading, stackable, stripped before further
parsing — same convention). If present, set `_INTERACTIVE=false`; default `_INTERACTIVE=true`
(interactive). `/run` injects `[no-interactive]` onto every NON-autonomous phase-subagent
spawn so a background decision gate FAILS CLOSED instead of silently proceeding — see
`__QUOIN_HOME__/memory/decision-gate-guard.md`. `[autonomous]` and `[no-interactive]` are
mutually exclusive per spawn (autonomous carries pre-authorized answers; no-interactive has
none, so it fails closed).

`_AUTONOMOUS` gates ONLY the four interactive body-prompt sites in the
"Process" section below (Steps 1b, 2, 3, 4) — it does not change the §0 dispatch / §0b
session-age-guard behavior above, and does not change the order of or preconditions for
(APPROVED review, passed gate) any of the 8 sequential steps. **The "never auto-create a
PR" invariant is unchanged in every mode, including autonomous — `/end_of_task` never
creates a PR, autonomous or not.**

`[quoin-onbehalf]` handling: `/run` prepends this marker to the Phase 6 spawn under default-ON capture. Strip it at bootstrap step 0 (per-spawn, non-inherited — never propagate it to a `§0` dispatch child). This skill has no session-start ledger row to suppress; the orchestrator writes the `end-of-task` row on its behalf.

## When to use

Only after:
1. `/review` has given an APPROVED verdict
2. The final `/gate` has passed
3. The user explicitly says to finalize (e.g., `/end_of_task`, "ship it", "we're done")

This skill is never auto-invoked. The user must consciously accept the work.

**Exception: `/run` orchestrator.** When this skill is spawned by `/run` as a subagent, the user has already confirmed the finalization checkpoint ("yes, finalize and push"). This constitutes explicit user acceptance — the user consciously chose to run the full pipeline and confirmed at Checkpoint D. All preconditions (APPROVED review, passed gate) are still enforced. If you see evidence that you were spawned by `/run`, proceed normally through all 8 steps.

## Process

This skill uses a 3-sub-phase Agent dispatch architecture to limit blast radius per
call. Interactive prompts are handled inline (parent session) BEFORE any sub-phase
is dispatched. Sub-phases receive deterministic file-based inputs only.

### Orchestrator pre-flight (inline — parent session handles all interactive prompts)

Execute these steps inline (never dispatch for interactive steps):

**Step 1: Pre-flight checks**

Before touching git, verify everything is clean:

1. **Review status** — resolve the artifact path via `python3 __QUOIN_HOME__/scripts/path_resolve.py --task <task-name> [--stage <N-or-name>]` (or stage=None for legacy tasks), then look for `<task_dir>/review-*.md`. If exit code 2: display stderr verbatim, fall back to task root, ask user to disambiguate. If no review file exists at the resolved path, STOP and tell the user: "No review found — please run `/review` first." If a review exists, read the latest one and confirm verdict is APPROVED. If not approved, stop and tell the user. (architecture.md and cost-ledger.md ALWAYS at task root per D-03.) **Retain the resolved `<N-or-name>` (or empty string for a legacy/single-stage task) as `<stage-value>`** — Sub-phase B's lessons-append idempotency guard (Step 7, T-11) keys on task+stage, not task alone, so it needs this value.
2. **Tests pass** — first run `python3 __QUOIN_HOME__/scripts/gate_fullsuite_sidecar.py check --project-root "$(pwd)" --format text`, using the same `--project-root` convention `gate/SKILL.md`'s reuse-contract bullet pins (the outer project root that owns `.workflow_artifacts/`, not the git repo root — see that file for the contract, not restated here). Exit 0 → the post-review gate's full-suite result is reusable (already size-aware — a red Small/Medium suite never reaches this branch); report `Tests: reused post-review gate full-suite (verdict PASS, SHA <sha12>, trees clean)`, reading `<sha12>` directly from `check`'s own exit-0 text output, and SKIP the re-run. Any non-zero exit, or the script missing, → run the test suite exactly as today, echoing the emitted `reason` when present (exit 1 and, since the review-round-1 fix, exit 3 both emit one on stdout), else the stderr line (exit 2 argparse failures emit no structured `reason` — echo stderr verbatim instead) — so the operator sees why it re-ran. `QUOIN_DISABLE_FULLSUITE_REUSE=1` forces a re-run. A reused (skipped) verification counts as PASS for every downstream step — it never blocks Steps 6-8, so Sub-phase B's cost aggregation and `cost-summary.json` write happen regardless of whether the suite was re-run; a suite that genuinely fails still halts before commit/push.
3. **Branch state** — check if the branch is up to date with the base branch. If behind, rebase/merge and re-run tests. If push is blocked because task commits are on a protected branch (main/master), do NOT force-push; instead follow the safe reset-to-origin recipe at `__QUOIN_HOME__/memory/branch-recovery.md` — move the mis-placed commits onto a feature branch first, then run the recipe to restore the protected branch to origin.
4. **No secrets** — quick scan of the diff for passwords, API keys, tokens.

Present a pre-flight summary:

```
Pre-flight: end_of_task
✅ Review: APPROVED (review-2.md)
✅ Tests: 47 passed, 0 failed
   (or, when reused: ✅ Tests: reused post-review gate full-suite (verdict PASS, SHA a1b2c3d4e5f6, trees clean))
✅ Branch: feat/refund-flow, up to date with main
✅ No secrets detected
Ready to finalize.
```

**Step 1b: Working-tree cleanup scan**

Before committing, scan the main repo working tree for files that should not
be shipped. Run these checks from the repo root (the nested git root, not the
project root):

1. **Untracked files that match garbage patterns** — run `git ls-files --others --exclude-standard`
   and flag any file matching these patterns:
   - `*.tmp`, `*.bak`, `*.orig`, `*.swp`, `*.swo`
   - `* 2.*`, `* 3.*` (macOS/iCloud duplicates, e.g., "README 2.md")
   - `.planner-trace.md` (Tier-3 ephemeral — deleted by `/end_of_task` before archive, per `__QUOIN_HOME__/memory/tier1-files.md`'s closing paragraph; run `python3 __QUOIN_HOME__/scripts/fsops.py rm ".planner-trace.md"` to clean up)
   - `.expanded-*.md` (expand --save scratch output)
   - `.DS_Store` (if not gitignored)
   - `__pycache__/` directories or `*.pyc` files (if not gitignored)
   - `*.log` files at the repo root
   - Any file or directory whose name starts with `.workflow_artifacts` inside the repo tree
     (these should never leak into the repo — they belong at the project root)

2. **Tracked files that look like debug leftovers** — run `git diff --name-only` (unstaged)
   and `git diff --cached --name-only` (staged) and flag:
   - Files containing `console.log`, `debugger`, `breakpoint()`, `import pdb`, or
     `print("DEBUG` in their diff hunks (use `git diff -G` or read the diff output)
   - This is advisory only — some repos legitimately use these. Flag, don't block.

3. **Present findings** — if any garbage file or debug leftover was found, show a categorized summary:

   ```
   Working-tree cleanup scan:
   ⚠️  Garbage files found (recommend deleting before commit):
      - README 2.md (macOS duplicate)
      - .planner-trace.md (workflow ephemeral)

   ⚠️  Debug leftovers in diff (review before commit):
      - quoin/scripts/foo.py: contains `breakpoint()` (line 42)

   ✅ No .workflow_artifacts/ leak detected.

   ```

   Use AskUserQuestion before continuing to Step 2:
   <!-- decision-gate: fail-closed site=garbage-files -->
   ```
   AskUserQuestion(
     question="Garbage files or debug leftovers found. How would you like to proceed?",
     options=[
       {label: "Delete garbage files", description: "Remove the flagged files before committing."},
       {label: "Proceed as-is", description: "Keep all files; commit everything shown."}
     ]
   )
   ```

   **Autonomous mode:** if `_AUTONOMOUS` is true and findings were reported, skip the
   `AskUserQuestion` — auto-select **"Delete garbage files"** (the recommended cleanup
   default) and proceed to Step 2 without waiting. Print one line:
   `[quoin: autonomous — deleting flagged garbage files, proceeding to Step 2]`.
   Debug leftovers found in tracked-file diffs stay advisory-only and are never
   auto-modified or auto-deleted.

   **Non-interactive / `[no-interactive]` (and NOT `_AUTONOMOUS`):** if `_INTERACTIVE` is
   false (the `[no-interactive]` sentinel was set, or `AskUserQuestion` is unavailable —
   e.g. an Agent subagent, where it is not provisioned — or returns no usable answer),
   FAIL CLOSED — do NOT proceed on a default and do NOT stall: run
   `python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill end_of_task --site garbage-files --reason "working-tree cleanup decision could not be surfaced" --resume-hint "re-run /end_of_task interactively, or pass --autonomous"`,
   echo its `gate-result: NEEDS-DECISION` block as the final message, and STOP — it emits no envelope here even under `return: envelope`.
   Rule doc: `__QUOIN_HOME__/memory/decision-gate-guard.md`.

4. **If nothing found** — print one line and continue:
   ```
   Working-tree cleanup scan: ✅ clean
   ```

**Step 1c: Authored-content lint (advisory)**

This step runs unconditionally, after item 4 above, regardless of what items 1-4 found.
Checks new code against `__QUOIN_HOME__/memory/clean-authored-content.md`.

Run:

    python3 __QUOIN_HOME__/scripts/authored_content_lint.py --basis union --format text

Result mapping:
- exit 0 — nothing to report.
- exit 1 — list every reported `file:line` and its matched token under an advisory heading.
- exit 2, exit 3, or the script is missing — print a one-line non-blocking WARN and continue.

This step never calls `AskUserQuestion`, never blocks, and never changes the Step 2 commit
decision below.

**Step 2: Commit decision (interactive — must resolve before dispatching sub-phases)**

Run `git status`. If there are uncommitted changes:
- Show them to the user
- Use AskUserQuestion to get the commit decision (no stash option — stash manually then re-invoke if needed):
  <!-- decision-gate: fail-closed site=commit-decision -->
  ```
  AskUserQuestion(
    question="There are uncommitted changes. Commit them now or abort?",
    options=[
      {label: "Commit", description: "Commit all uncommitted changes with a conventional message."},
      {label: "Abort", description: "Stop here. Stash manually then re-invoke /end_of_task."}
    ]
  )
  ```
- **Commit message content:** whichever path composes the message (autonomous or interactive), it follows the shared clean-authored-content rule — plain engineering language, no plan/review process vocabulary: __QUOIN_HOME__/memory/clean-authored-content.md.
- **Autonomous mode:** if `_AUTONOMOUS` is true, skip the `AskUserQuestion` — auto-select
  **"Commit"** (NEVER "Abort"). Compose the conventional commit message automatically from
  the diff/plan context. Print one line: `[quoin: autonomous — committing uncommitted
  changes]`. This never creates a PR — the "never auto-create a PR" invariant applies here
  exactly as in interactive mode.
- **Non-interactive / `[no-interactive]` (and NOT `_AUTONOMOUS`):** if `_INTERACTIVE` is
  false (`[no-interactive]` set, or `AskUserQuestion` unavailable/suppressed), FAIL CLOSED —
  the commit/abort decision must never default silently: run
  `python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill end_of_task --site commit-decision --reason "uncommitted-changes commit decision could not be surfaced" --resume-hint "re-run /end_of_task interactively, or pass --autonomous"`,
  echo its `gate-result: NEEDS-DECISION` block as the final message, and STOP — it emits no envelope here even under `return: envelope`. Rule doc:
  `__QUOIN_HOME__/memory/decision-gate-guard.md`.
- If **Commit**: collect a conventional commit message inline.
- If **Abort**: STOP. Tell the user: "Stash manually then re-invoke /end_of_task."
Capture the answer as `commit_or_abort` (`"commit"` or `"abort"`).
If no uncommitted changes: set `commit_or_abort = "commit"` (nothing to do) and skip.

**Step 3: Lessons learned (interactive — capture inline)**

Use AskUserQuestion to check for lessons:
<!-- decision-gate: best-effort site=lessons-prompt -->
```
AskUserQuestion(
  question="Task complete. Anything that surprised you, or that the workflow should handle differently next time?",
  options=[
    {label: "Nothing to add", description: "No lessons to record for this task."},
    {label: "Yes, let me share", description: "I have something to add to lessons-learned."}
  ]
)
```

**Autonomous mode:** if `_AUTONOMOUS` is true, skip the `AskUserQuestion` entirely — no
prompt, no wait. Wire the existing auto-capture triggers below (critic-revise loop > 3
rounds, review requested changes, a rollback happened during this task) to compose
`lessons_text` non-interactively from that context. If none of the triggers fire, set
`lessons_text = ""` and skip cleanly (nothing to capture).

If the user selects "Nothing to add": set `lessons_text = ""`.
If the user selects "Yes, let me share" or uses the "Other" free-text option: capture their input as `lessons_text`.
Capture the response as `lessons_text` (may be empty string if nothing to share).

Auto-capture lessons if:
- The critic-revise loop ran more than 3 rounds (what made convergence hard?)
- The review requested changes (what did /implement miss?)
- A rollback happened during this task (what went wrong?)

**Step 4: Archive type (interactive — capture inline)**

If the task folder lives directly under `.workflow_artifacts/` (not inside a parent feature folder), use AskUserQuestion:
<!-- decision-gate: fail-closed site=archive-type -->
```
AskUserQuestion(
  question="Is the feature '<task-name>' fully complete, or is there more work planned under this folder?",
  options=[
    {label: "Fully complete", description: "Archive the task folder to finalized/."},
    {label: "More work planned", description: "Keep the task folder active; do not archive."}
  ]
)
```

**Autonomous mode:** if `_AUTONOMOUS` is true, skip the `AskUserQuestion` — auto-select the
safe default **"Fully complete"** (`archive_type = "feature"`, top-level archive). Print one
line: `[quoin: autonomous — archiving task folder to finalized/]`.

**Non-interactive / `[no-interactive]` (and NOT `_AUTONOMOUS`):** if `_INTERACTIVE` is false
(`[no-interactive]` set, or `AskUserQuestion` unavailable/suppressed), FAIL CLOSED — do not
default the archive decision: run
`python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill end_of_task --site archive-type --reason "archive-type decision could not be surfaced" --resume-hint "re-run /end_of_task interactively, or pass --autonomous"`,
echo its `gate-result: NEEDS-DECISION` block as the final message, and STOP — it emits no envelope here even under `return: envelope`. Rule doc:
`__QUOIN_HOME__/memory/decision-gate-guard.md`.

Capture as `archive_type`: `"feature"` (fully complete) or `"none"` (more work planned — do not archive).

If the task folder is inside a parent feature folder (detected by presence of planning artifacts or stage-* sibling folders in the parent), set `archive_type = "subtask"` without asking.

**Step 5: Write `eot-preflights.json` — MUST happen BEFORE dispatching any sub-phase**

Write `.workflow_artifacts/<task-name>/eot-preflights.json` (fixed name — no date stamp):

```json
{
  "task_name": "<task-name>",
  "task_dir": "<absolute-path-to-task-dir>",
  "stage": "<stage-value-or-empty-string>",
  "commit_list": ["<file1>", "<file2>"],
  "commit_message": "<conventional commit message or empty string>",
  "commit_or_abort": "commit",
  "lessons_text": "<what the user said, or empty string>",
  "archive_type": "feature"
}
```

`"stage"` is `<stage-value>` from Step 1 (empty string for a legacy/single-stage task) — carried through so Sub-phase B (T-11) can key its lessons-append idempotency guard on task+stage.

The orchestrator OVERWRITES any stale file from a prior run. Each `/end_of_task`
invocation produces exactly one `eot-preflights.json`. Sub-phases MUST NOT
re-derive or re-timestamp the filename — they read the path given inline.

If `commit_or_abort` is `"abort"`: STOP here. Do not dispatch any sub-phase.

**Step 6: Dispatch Sub-phase A (commit + push)**

Spawn an Agent subagent:
- model: `"sonnet"`
- description: `"end_of_task Sub-phase A: commit and push"`
- prompt: |
    You are Sub-phase A of /end_of_task. Your job: commit remaining changes (if any)
    and push the branch to remote. Read the hand-off file, execute, report results.

    Hand-off file: `<absolute-path-to-task-dir>/eot-preflights.json`

    Steps:
    1. Read `eot-preflights.json`. Defensive check: if `commit_or_abort` is `"abort"`,
       exit immediately with "Orchestrator sent abort — Sub-phase A exiting."
    2. If `commit_list` is non-empty and `commit_message` is non-empty:
       - Stage the listed files (`git add <file>` for each, not `git add .`)
       - Commit with the provided `commit_message`
    3. **Push (idempotent, T-11):** `git fetch origin <current-branch-name>` then compare
       `git rev-parse HEAD` against `git rev-parse origin/<current-branch-name>`. If they
       are EQUAL (already pushed — e.g. a kill-after-push resume re-running this
       sub-phase), SKIP the push as a no-op and record `"push_skipped": true` in
       `eot-preflights.json`. Otherwise: `git push -u origin <current-branch-name>`.
       If push fails: report the error clearly; do NOT retry. The user will resolve.
    4. Run `git rev-parse HEAD` and append `"commit_hash": "<sha>"` to `eot-preflights.json`.
    5. Report: branch pushed (or push-skipped-already-current), commit hash, any errors.

    Scope cap: at most ~15 tool uses. If blocked, write what you have to disk and return.

Wait for Sub-phase A result. If it reports a fatal error (push failed, etc.): report to
the user and stop. Do NOT proceed to Sub-phase B if the push failed.

**Step 7: Dispatch Sub-phase B (lessons + session state + cost aggregation)**

Spawn an Agent subagent:
- model: `"sonnet"`
- description: `"end_of_task Sub-phase B: lessons, session state, cost"`
- prompt: |
    You are Sub-phase B of /end_of_task. Your jobs: append lessons to lessons-learned.md,
    update session state to completed, and aggregate task cost. Write cost summary to disk.

    Hand-off file: `<absolute-path-to-task-dir>/eot-preflights.json`
    Cost ledger: `<absolute-path-to-task-dir>/cost-ledger.md`
    Lessons-learned: `.workflow_artifacts/memory/lessons-learned.md`
    Session state dir: `.workflow_artifacts/memory/sessions/`
    Sub-phase B sentinel (autonomous only, T-11): `.workflow_artifacts/memory/autonomous-progress-<task_name>/end_of_task.subphaseB.done`

    **Entry-skip guard (T-11, `_AUTONOMOUS` only) — run BEFORE step 1:** if
    `_AUTONOMOUS` is true and `autonomous-progress-<task_name>/end_of_task.subphaseB.done`
    already exists, this whole sub-phase already ran on a prior attempt (e.g. a
    kill-after-push, before-done-sentinel resume) — SKIP everything below as a
    no-op and report "Sub-phase B already complete (subphaseB.done sentinel
    present) — skipping." Do NOT re-append lessons, re-touch session state, or
    recompute cost. This sentinel is unconditional-safe: it only ever skips
    already-finished work, never unfinished work. It ALSO counts toward the
    T-06 union forward-progress glob (`autonomous-progress-{task}/*.done`).

    Steps:
    1. Read `eot-preflights.json` for `lessons_text`, `task_name`, and `stage`.
    2. **Lessons-append idempotency (T-11, keyed on task+stage, unconditional —
       runs whether or not `_AUTONOMOUS`):** if `lessons_text` is non-empty, first
       grep `lessons-learned.md` for an existing entry heading matching this
       task+stage: `## .* — <task_name>` when `stage` is empty, or
       `## .* — <task_name> \[stage-<stage>\]` when `stage` is non-empty (a
       stage-2 lessons entry must never be false-skipped by a stage-1 entry's
       heading, and vice versa — MINOR fix). If a matching heading is already
       present, SKIP the append (already recorded — belt-and-suspenders behind
       the Sub-phase B entry-skip sentinel above) and note it in the report.

       **Cross-project dedup guard (IVG-119, runs only when the append would
       proceed):** write `lessons_text` to a temp file, then call
       `python3 __QUOIN_HOME__/scripts/lessons_guard.py --candidate-file <tmp> --lessons-file .workflow_artifacts/memory/lessons-learned.md --candidate-slug <task_name>`.
       The guard COMPLEMENTS the heading-grep above (grep catches same-task
       re-appends; the guard catches DIFFERENT-task verbatim copies). Exit 1 →
       do NOT silently append: surface the matched foreign heading + slug to the
       user and let them decide. Under `_AUTONOMOUS`: record the match in the
       Sub-phase B report AND append with a `> WARN (IVG-119): suspected
       cross-project duplicate of <matched-slug>` annotation — never block. Exit
       0/2/3 or script missing → proceed with the append (fail-OPEN).

       Otherwise append to lessons-learned.md, including the stage suffix in the
       heading when `stage` is non-empty:
       ```
       ## <date> — <task_name>[ [stage-<stage>]]
       **What happened:** <lessons_text>
       **Lesson:** <reusable takeaway>
       **Applies to:** <relevant skills>
       ```
    3. Update `.workflow_artifacts/memory/sessions/<date>-<task_name>.md`:
       set status to `completed`, record branch name and commit hash from eot-preflights.json.
    3a. **Flip finalized-task session flags (single invocation; primary survival mechanism for
        IVG-137; opt-out via `QUOIN_DISABLE_EOT_FLAG_FLIP=1`).** Runs AFTER Step 3 (session-state
        marked completed) and AFTER Sub-phase A's commit/push already succeeded (mirrors
        crash-safety ordering — a finalized-task marker on an un-pushed task would be premature).
        Unless `QUOIN_DISABLE_EOT_FLAG_FLIP=1` is set, run:
        ```bash
        python3 __QUOIN_HOME__/core/scripts/select_unprocessed_sessions.py \
            --flip-finalized-task <task_name> \
            --finalization-date <today, YYYY-MM-DD> \
            --project-root <project-root>
        ```
        ONE Bash tool-use, regardless of how many dated session files the task touched — this
        invokes `flip_finalized_task_sessions()`, which scans ALL session files under `sessions/`
        (all dates, no window restriction) for a raw slug EXACTLY equal to `<task_name>` OR
        `<task_name>-orchestrator` (direct string equality — matches base sessions plus the
        orchestrator sibling, if one exists), atomically flips each matched file to
        `end_of_day_due: no`, and writes a `finalized_by_end_of_task: <today>` marker on each in
        the same write (idempotent — safe to re-run; re-running only refreshes the date). Print
        the flipped-file-path list from stdout in the Step 6 report below. The exact-equality
        match means this step never touches another task's sessions, even one whose slug happens
        to end in `-orchestrator`. Round 4 scope note: this exact-equality match does NOT reach
        phase/stage-suffixed sessions of the same task (e.g. `<task_name>-review.md`,
        `<task_name>-implement.md`) — same documented residual as `/end_of_day`'s reconciliation
        pre-pass, not a new gap; those sessions remain `end_of_day_due: yes` and surface as
        ordinary backlog exactly as they do today.

        The `finalized_by_end_of_task` marker is read-only provenance metadata — it does NOT
        change `end_of_day_due` flag-authoritative selection semantics (IVG-103 unchanged). It is
        consumed only by `/end_of_day`'s `find_finalized_marked()` producer (via
        `--include-finalized-marked`, riding the same `--show-window` call), which unions
        same-window marked sessions into `in_scope` so finalized-task work still appears in that
        day's "Completed today" digest even though the flag is already `no` by the time
        `/end_of_day` next runs.

        If the script is unavailable or errors, fail-OPEN: emit `[quoin: end_of_task flag-flip
        unavailable; sessions remain end_of_day_due: yes — will surface as ordinary backlog on the
        next /end_of_day]` and continue — this is best-effort, not load-bearing for finalization
        (the task is still fully finalized: committed, pushed, archived).
    4. Cost aggregation — read cost-ledger.md and compute:
       a. Binary check: `command -v npx` — if unavailable, skip ccusage and use
          cost_from_jsonl.py fallback for ALL UUIDs (see below).
       a2. Inline-first precedence rule (per ledger row, applied BEFORE the UUID
          lookups in (b)/(c) below — mirrors the core `classify_attribution()`
          verdict used by the Python readers): a row whose 8th column carries a
          parseable `usd` with `src` ≠ `unresolved` is **resolved** — use that
          inline `usd` directly for the row's UUID and EXCLUDE it from the
          ccusage/`cost_from_jsonl.py` lookup set below (no lookup needed, no risk
          of a failed or duplicated resolution). A row with `src=unresolved` (or no
          usable `usd`) is **unresolvable** — count it into `unresolvable_count`
          (used in step 5) and contribute NOTHING to any total (never fold it into
          $0). A row with an empty/absent 8th column is **legacy** — unchanged,
          falls through to (b)/(c); if its JSONL lookup also fails, it is ALSO
          counted into `unresolvable_count`.
       b. For each REMAINING (legacy, not-yet-resolved) UUID in ledger (<5 sessions): `timeout 15 npx ccusage session -i <UUID> --json`
          For ≥5 sessions (bulk): `npx ccusage session --json --since <earliest-date-from-ledger>`
          then filter returned sessions against the UUIDs in the ledger.
          Parsing bulk responses: ccusage v20+ wraps results as
          `{"session": [{"period": "UUID", "totalCost": ..., ...}, ...], "totals": {...}}`.
          The UUID is in `period`; the array is under top-level key `session`.
          If the response instead has a `sessionId` field (v18 shape) or is a bare array, use `sessionId`.
          Version-detection: presence of top-level `session` key (array) → v20; else v18 fallback.
          Extract `totalCost` per UUID. Filter to ledger UUIDs only.
          Path-agnostic all-failed gate: whichever of the per-UUID loop or bulk call was taken,
          if NO ledger UUID was successfully resolved, fall back to cost_from_jsonl.py for all UUIDs.
       c. Fallback (from binary-check branch OR all-failed gate):
          Per-UUID mode: `python3 __QUOIN_HOME__/scripts/cost_from_jsonl.py session -i UUID --json`
          Bulk mode: `python3 __QUOIN_HOME__/scripts/cost_from_jsonl.py session --json --since <date>`
          Filter results to only UUIDs in the ledger. Parse output identically to ccusage.
          Prepend: `[fallback: cost_from_jsonl.py — prices as of <LAST_UPDATED>]`
          Read LAST_UPDATED via: `python3 -c "from pathlib import Path; import sys; sys.path.insert(0, str(Path.home() / '.claude' / 'scripts')); import cost_from_jsonl; print(cost_from_jsonl.LAST_UPDATED)"`
       d. Aggregate: per-phase totals, per-model totals, `resolved_total` (sum of
          resolved-inline `usd` from (a2) plus successfully-resolved legacy JSONL
          costs from (b)/(c) — NEVER includes an unresolvable row as $0), and
          `unresolvable_count` (col-8 `unresolvable` rows plus legacy rows whose
          JSONL lookup failed, per (a2)). `grand_total` = `resolved_total` (kept as
          an explicit alias for backward compatibility with existing consumers).
       **Re-runnable (T-11):** this whole computation reads from cost-ledger.md
       (append-only, never mutated by this step) and OVERWRITES cost-summary.json
       (fixed name — see step 5) — re-running it after a kill/resume recomputes the
       same totals from the same ledger and overwrites the same file; it never
       blind-appends a second total row anywhere.
    5. Write `.workflow_artifacts/<task_name>/cost-summary.json` (fixed name, overwritten):
       ```json
       {
         "per_phase": {"plan": 1.23, "implement": 0.45, ...},
         "per_model": {"opus": 1.23, "sonnet": 0.45, "haiku": 0.00},
         "task_total": 1.68,
         "off_topic_total": 0.00,
         "grand_total": 1.68,
         "resolved_total": 1.68,
         "unresolvable_count": 0,
         "fallback_used": false,
         "fallback_note": ""
       }
       ```
       `resolved_total` == `grand_total` (an explicit alias — both are the RESOLVED-only
       total from step 4d, never a total that silently folds an unresolvable row into
       $0). `unresolvable_count` is the count from step 4d (col-8 `unresolvable` rows
       plus legacy rows whose JSONL lookup failed). **Set `"fallback_used": true` and a
       non-empty `"fallback_note"` whenever `unresolvable_count > 0`** (in addition to
       the existing ccusage-unavailable trigger) — this is what makes the existing
       `costService`/`normalize_total` partial-detection fire for cost-attribution
       partiality, not just for the ccusage-fallback case.
       NOTE: `fallback_used=true` means "partial estimate — some ledger UUIDs did not
       resolve to JSONL sessions, OR some ledger rows are col-8-unresolvable". It does
       NOT mean the cost is unavailable. A present `grand_total`/`resolved_total` with
       `fallback_used=true` should be rendered as `~$X (partial)`. Only a null or absent
       total key means unavailable. Consumer map: this file is consumed only by
       `costService.ts` (extension). `/cost_snapshot` and `dashboard_model.py` consume
       `cost-ledger.md` instead — do NOT wire them to this file.
    6. Report: lessons appended (yes/no, or "skipped — already recorded"), session state
       updated, finalized-task sessions flipped (count, or "skipped —
       QUOIN_DISABLE_EOT_FLAG_FLIP set" / "skipped — script unavailable"), cost summary
       written.
    7. **Write the Sub-phase B entry-skip sentinel (T-11, `_AUTONOMOUS` only) — LAST, after
       everything above succeeds:** atomically write
       `autonomous-progress-<task_name>/end_of_task.subphaseB.done`
       (pipe the content into `python3 __QUOIN_HOME__/scripts/fsops.py write-atomic --parents "<f>"`). Inert when
       `_AUTONOMOUS` is false — plain (non-autonomous) `/end_of_task` never writes it,
       matching every other autonomous-only sentinel write in this workflow.

    Scope cap: at most ~15 tool uses. If blocked on cost aggregation, write partial
    data to cost-summary.json and return — partial cost data is better than none.

**Step 8: Dispatch Sub-phase C (archive + final report)**

Spawn an Agent subagent:
- model: `"sonnet"`
- description: `"end_of_task Sub-phase C: archive and report"`
- prompt: |
    You are Sub-phase C of /end_of_task. Your jobs: archive the task folder and
    print the final completion report.

    Hand-off files:
    - `<absolute-path-to-task-dir>/eot-preflights.json` (for archive_type, task_name)
    - `<absolute-path-to-task-dir>/cost-summary.json` (read BEFORE the move — it lives
      inside the task folder which you are about to move)
    Task dir: `<absolute-path-to-task-dir>`
    Done sentinel (autonomous only, T-11): `.workflow_artifacts/memory/autonomous-done-<task_name>.md`

    Steps:
    1. Read `cost-summary.json` from the task dir (BEFORE any move).
    2. Read `eot-preflights.json` for `archive_type` and `task_name`.
    3. Delete planner trace breadcrumb (if present):
       Run: python3 __QUOIN_HOME__/scripts/fsops.py rm "<task_dir>/.planner-trace.md" || true
       Tier-3 ephemeral — must not persist in the finalized archive; runs BEFORE the archive move.
    4. **Archive (idempotent, T-11)** based on `archive_type`:
       - `"subtask"`: target = `.workflow_artifacts/<parent>/finalized/<subtask>/`.
       - `"feature"`: target = `.workflow_artifacts/finalized/<task_name>/`.
       - `"none"`: skip the move entirely — no target, nothing to check.
       For `"subtask"`/`"feature"`: **before the move, check whether the target directory
       already exists** (the task folder was already archived on a prior attempt — e.g.
       a kill-after-archive-before-done-sentinel resume). If it exists, SKIP the move as a
       no-op (do not double-move or error) and note "already archived" in the report.
       Otherwise move the folder: `python3 __QUOIN_HOME__/scripts/fsops.py mv --parents "<task_dir>" "<target>"` (the target is the final path; `--parents` creates only its parent).
    5. Print the final report:
       ```
       Task finalized: <task_name>

       Branch: <branch> → pushed to origin
       Review: APPROVED
       Archived: <task-folder> → <finalized-path>   (or "not archived — more work planned")

       Cost breakdown:
         Phase          | Cost
         ---------------|--------
         plan           | $X.XX
         implement      | $X.XX
         ...
         ---------------|--------
         Task total     | $X.XX
         Grand total    | $X.XX

       Model breakdown: opus: $X.XX | sonnet: $X.XX | haiku: $X.XX
       <fallback note if applicable>

       Lessons captured: <yes/no>
       Session marked as completed.

       Next: when you're ready, create a PR from the branch.
       ```
    6. **Write the done-sentinel (T-11, `_AUTONOMOUS` only) — LAST, after the archive move
       (step 4) AND the report print (step 5) above, never before either:** atomically
       write `.workflow_artifacts/memory/autonomous-done-<task_name>.md`
       (pipe the content into `python3 __QUOIN_HOME__/scripts/fsops.py write-atomic --parents "<f>"`), deliberately
       OUTSIDE the just-archived task folder so the record survives the move. This is the
       final terminal signal an external supervisor's relaunch loop checks for SUCCESS —
       anchoring it here (after both the move and the report, not attached to any earlier
       step) means a kill at any prior boundary in this 8-step process — including
       after-push-before-Sub-phase-B, after-Sub-phase-B-before-archive, and
       after-archive-before-done — safely re-runs from where it left off with no
       duplicated work, and only a kill AFTER this final write is a true, safe SUCCESS.
       Inert when `_AUTONOMOUS` is false — plain (non-autonomous) `/end_of_task` never
       writes it.

    Scope cap: at most ~15 tool uses. If blocked, write what you have to disk and return.

## Important behaviors

- **Run tests one last time.** Even if they passed 5 minutes ago. Code might have changed.
- **Never force-push.** Use regular `git push`. If the branch has diverged, tell the user and let them decide how to resolve.
- **No PR creation.** This skill pushes the branch only. The user creates the PR separately when they're ready. Remind them at the end.
- **Cost aggregation runs before archive.** The ledger file is inside the task folder — it must be read before the folder is moved.
- **This is a celebration, not a chore.** The task is done. Keep the output clean and satisfying.
