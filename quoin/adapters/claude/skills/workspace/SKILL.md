---
name: workspace
description: "Manages per-repo git-worktree workspaces: create, status, takeover, teardown. Use for: /workspace, 'create a workspace', 'new worktree', 'tear down this workspace'."
model: sonnet
context: fork
---

# Workspace

*Portable intent doc: `quoin/core/skills/workspace.md`*

## When to use

Whenever a user or orchestrator wants an isolated per-repo `git worktree` for
concurrent session work — most commonly to let `/run` (or a manually-started
task) work on one feature while another session owns the main tree. The user
explicitly invokes `/workspace create|status|takeover|teardown`.

This skill is **never auto-invoked** by any orchestrator or skill. `/run` may
call the `workspace.py` script itself (through `run_isolation.py`) when a
second live run would share a checkout or `isolate:` is given; those
workspaces live under `QUOIN_WORKTREE_ROOT` (default `~/.quoin/worktrees`),
and `status`/`teardown <task>` find them through their record.

## Session bootstrap

On start:
1. Append your session to the cost ledger: `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `workspace` — format/rules: `${CLAUDE_PLUGIN_ROOT}/memory/cost-ledger-format.md`. If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

<!-- quoin:ledger-self-write -->
2. Write session state to `.workflow_artifacts/memory/sessions/<date>-<task-name>.md` per `${CLAUDE_PLUGIN_ROOT}/memory/session-schema.md`

## Process

The mechanism lives entirely in `quoin/core/scripts/workspace.py`. This
adapter is a thin dispatcher over that script's four subcommands, plus the
two confirmation gates below (the skill's only non-thin logic).

### Step 1: `create`

1. Run: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/workspace.py create <feature> [--repos <csv>] [--base <ref>]`.
2. Render the per-repo `CreateResult`: which repos got a fresh worktree, which
   were refreshed, the feature branch name, and the ownership-record path.
3. Confirm the original tree was left untouched (the script guarantees this;
   just surface it).

### Step 2: `status` (a.k.a. list)

1. Run: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/workspace.py status`.
2. Render every known workspace: owner, liveness, branch, and — best effort —
   merge state (surfaced when the host CLI, e.g. `gh`, is available; degrades
   silently when it is not).
3. **CONFIRMATION GATE (b) — post-merge teardown OFFER:** for any workspace
   whose branch `status` reports as merged, OFFER teardown via
   `AskUserQuestion` ("this workspace's branch has merged — tear it down
   now?"). NEVER run teardown automatically from this offer. Under
   `[no-interactive]` or `[autonomous]` invocation, fail CLOSED: record the
   offer in the output text but do NOT action it — no `--force`, no delete,
   without an interactive human choice.

### Step 3: `takeover`

1. Run: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/workspace.py takeover <feature> [--force]`.
2. If the script refuses because the current owner is LIVE and non-self,
   report that refusal — do not silently retry with `--force`; that decision
   belongs to the user (a manual re-invocation with `--force` if they confirm
   the takeover is safe).
3. Render the `TakeoverResult`: confirm the ownership record's owner fields
   were reassigned, and that no worktree or branch changed.

### Step 4: `teardown`

1. Run (without `--force` first): `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/workspace.py teardown <feature>`.
2. **CONFIRMATION GATE (a) — teardown-with-dirty-work:** if the script
   reports any unsafe worktree (uncommitted changes, unpushed commits, and
   not proven-merged), issue an `AskUserQuestion` — "this workspace has
   [unsafe reason]; proceed with `--force` or abort?" — before re-invoking
   with `--force`. NEVER pass `--force` without that confirmation. Under
   `[no-interactive]` or `[autonomous]` invocation, fail CLOSED: report the
   refusal and STOP — do NOT force-remove without an interactive human choice.
3. On a clean pass (or a user-confirmed forced pass), confirm the worktree,
   ownership record, and workspace folder were removed.

## Cost tracking

Append to the task's cost ledger at `.workflow_artifacts/<task-name>/cost-ledger.md`
with phase `workspace` (see cost tracking rules in CLAUDE.md).

## Session state

Write to `.workflow_artifacts/memory/sessions/<date>-<task-name>.md` after
each subcommand invocation completes, to the fixed schema in `${CLAUDE_PLUGIN_ROOT}/memory/session-schema.md`
(Current stage `workspace <op>`; Next command unchanged).
