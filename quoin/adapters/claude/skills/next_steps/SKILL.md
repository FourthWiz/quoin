---
name: next_steps
description: "Append-only queue for future work items. Use for: /next-steps, /next-steps add <text>, /next-steps list, /next-steps list --all, /next-steps done N. Manages a ## Queue section in next-steps.md at the project root."
model: haiku
---

# Next Steps

*Portable intent doc: `quoin/core/skills/next_steps.md`*

Manages an append-only queue of future work items in `next-steps.md`.

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
        description: "next_steps dispatched at haiku tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in next_steps. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /next-steps`).
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

Cost tracking (conditional): `/next-steps` is a lightweight queue-management skill. Append to the cost ledger only when a task context is clearly active. If in doubt, skip cost recording. If the condition holds: append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `next-steps` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`.

<!-- quoin:ledger-self-write -->

## Subcommand parsing

Parse the user's input (everything after the `/next-steps` invocation text) as follows:

- If the input matches `done <integer>` (regex `^done\s+\d+\s*$`) → `done` subcommand; extract N as the integer.
- If the input is exactly `list` or `list --all` → `list` subcommand.
- Everything else (including a bare invocation with trailing text) → `add` subcommand, using the entire trailing text verbatim as the entry text.

## Process

### Shared: resolve project root

Before any subcommand, resolve the project root by walking up from `cwd` looking for `.workflow_artifacts/` then `.git/`. If neither is found at cwd or any parent, emit:

```
error: cannot resolve project root (no .workflow_artifacts/ or .git/ found); refusing to write
```

and stop. Target file: `<project-root>/next-steps.md`.

### add TEXT

1. Trim leading/trailing whitespace from TEXT. If empty after trim, emit `error: empty entry text` and stop.
2. Read the full content of `next-steps.md` (create empty if it doesn't exist).
3. If `## Queue` heading is absent from any line: ensure the file ends with a single newline, then append `\n## Queue\n\n`.
4. Append the line: `- ⏳ YYYY-MM-DD: <text>` where date is the output of `date -u +%Y-%m-%d`.
5. Write back atomically: write full content to `<path>.tmp`, then `python3 __QUOIN_HOME__/scripts/fsops.py mv "<path>.tmp" "<path>"`.
6. Confirm: `Queued: ⏳ YYYY-MM-DD: <text>`

### list [--all]

1. Read `next-steps.md`. If absent, print `(no queue yet — use /next-steps add <text>)` and stop.
2. Locate `## Queue` heading. If absent, print `(no queue yet — use /next-steps add <text>)` and stop.
3. Walk lines from heading + 1 to EOF:
   - `pending` = lines whose stripped form starts with `- ⏳ `
   - `done_items` = lines whose stripped form starts with `- ✓ `
4. Print pending lines numbered 1..N (one per line). If none: print `(queue is empty)`.
5. With `--all` and done_items non-empty: print a blank line, `Done:`, then done_items unnumbered.

### done N

Full R-01 bracket procedure (load-bearing — prevents edits above `## Queue` heading):

```
read full file content into 'lines' (list of strings, no trailing newlines)
heading_idx = first index i where lines[i].rstrip() == "## Queue"
if heading_idx is None:
  print("error: ## Queue heading not found in next-steps.md; refusing to edit")
  exit non-zero
counter = 0
matched_idx = None
matched_line = None
for i in range(heading_idx + 1, len(lines)):
  if lines[i].lstrip().startswith("- ⏳ "):
    counter += 1
    if counter == N:
      matched_idx = i
      matched_line = lines[i]
      break
if matched_idx is None:
  print(f"error: only {counter} pending entries; cannot mark {N}")
  exit non-zero
print(f"match: line {matched_idx + 1}: {matched_line}")
new_line = matched_line.replace("⏳", "✓", 1)
lines[matched_idx] = new_line
write '\n'.join(lines) + '\n' to <path>.tmp
run: python3 __QUOIN_HOME__/scripts/fsops.py mv "<path>.tmp" "<path>"
print(f"Marked done: {new_line}")
```

## Important behaviors

- **Fast.** This skill completes in seconds. One read, one write.
- **Bracket is non-negotiable.** Never edit lines above the `## Queue` heading. If the heading is absent when `done N` is called, refuse with an error — do not fall back to a global search.
- **Atomic writes.** Always write to `<path>.tmp` then `python3 __QUOIN_HOME__/scripts/fsops.py mv "<path>.tmp" "<path>"` for all mutations.
- **Show the match before editing.** For `done N`, always print the matched line before mutating the file so the user sees what will change.
- **Reject empty text.** For `add`, reject empty or whitespace-only text with a one-line error.
