# workspace

Runtime-neutral intent for the workspace skill. Any runtime adapter that
implements this skill should match the contract described here.

## Purpose

Manage per-repo `git worktree` isolation so multiple concurrent sessions can
work on the same project folder without colliding. Four subcommands: `create`
(add an isolated worktree per affected repo, on a feature branch, recorded
under the shared `.workflow_artifacts/` root), `status` (a.k.a. list — surface
every known workspace and its ownership/liveness), `takeover` (reassign a
stale or self-owned workspace record without destroying anything), and
`teardown` (safely remove a workspace, refusing when work is uncommitted or
unpushed unless the caller proves it merged or forces the removal).

## When to use

- A user or orchestrator wants to work on a task in isolation from the main
  tree while another session owns it (e.g. `/run` in a parallel-feature
  scenario).
- A session needs to check which workspaces exist, who owns them, and whether
  they are stale.
- A session needs to resume or reassign a workspace another (possibly dead)
  session created.
- A workspace's work has landed (merged) and it should be cleaned up.
- Always explicitly invoked by the user. Never auto-invoked by any skill or
  orchestrator. One exception covers the script, not this skill: the light
  run pipeline calls `workspace.py` (through `run_isolation.py`) to give a run
  its own worktree when another live run already holds the checkout, or when
  `isolate:` asks for one. Those workspaces sit outside the project, under
  `QUOIN_WORKTREE_ROOT` (default under `~/.quoin/worktrees/<repo>/<task>/`), and
  `status`/`teardown <task>` find them through their ownership record.

## Inputs

- Subcommand: `create` | `status` | `takeover` | `teardown`.
- `create`: a feature name (slugified for the workspace folder) and,
  optionally, an explicit repo list (default: every repo under the project
  root) and a base ref (default: each repo's own default branch).
- `takeover`: a feature name, and `--force` to override a live non-self
  owner.
- `teardown`: a feature name, and `--force` to bypass the unsafe-worktree
  guard.
- `status`: no required arguments — enumerates every known workspace.

## Outputs

- `create`: one `git worktree` per targeted repo on a feature branch, an
  ownership record under `.workflow_artifacts/`, and a workspace marker file.
  The original tree is left untouched.
- `status`: a per-workspace view — owner, liveness, branch, merge state (best
  effort via the host platform CLI when available; fail-open when absent).
- `takeover`: the ownership record's owner fields flipped to the caller,
  non-destructively (no worktree or branch changes).
- `teardown`: on a clean pass, the worktree(s), the ownership record, and the
  workspace folder are removed. On an unsafe pass (uncommitted or unpushed
  work), the operation refuses and reports what is blocking removal, unless
  the caller proves the branch merged or passes `--force`.
- A post-merge teardown OFFER surfaced by `status` when a branch is detected
  merged — teardown is always offered, never auto-run.

## Preconditions

- The project root contains (or can resolve) a shared `.workflow_artifacts/`
  root — workspace ownership records live there, not per-repo.
- Each targeted path is a `git` repository.
- `teardown` and `takeover` require an existing workspace record for the
  named feature.

## Contract

1. **`create`:** slugify the feature name; for each targeted repo, add a
   `git worktree` on a feature branch off the resolved base ref (skip/refresh
   if a matching worktree already exists); write an ownership record keyed on
   the feature slug plus a workspace marker; never touch the original tree.
2. **`status`:** enumerate every known workspace under the shared root;
   report owner, liveness (derived from session activity, not merely record
   existence), branch, and — best effort — whether the branch's PR has
   merged; degrade gracefully (fail-open) when the host CLI is unavailable.
3. **`takeover`:** refuse a LIVE non-self owner unless `--force` is passed;
   otherwise flip the ownership record's owner fields to the caller. Never
   deletes or recreates the worktree.
4. **`teardown`:** classify the worktree's safety (clean vs. uncommitted vs.
   unpushed vs. proven-merged); refuse removal on any unsafe classification
   unless the branch is proven merged or `--force` is passed; on a safe pass,
   remove the worktree, the ownership record, and the workspace folder.
5. **Confirmation gates (adapter-level, not script-level):** before an
   adapter invokes `teardown` against an unsafe worktree, it must confirm with
   the user (proceed-with-force vs. abort). After `status` surfaces a merged
   branch, the adapter must OFFER teardown rather than run it automatically.
   Both gates fail closed under non-interactive/autonomous invocation — the
   offer or refusal is recorded, never actioned, without an interactive human
   choice.

## Key design decision

The mechanism (git-worktree creation, ownership records, safety
classification) lives entirely in the portable core script
(`quoin/core/scripts/workspace.py`) and is intentionally free of any
runtime-specific confirmation logic. The adapter layer is a thin dispatcher
over that script PLUS exactly two confirmation gates — unsafe teardown and
post-merge teardown offer — because those two moments are the only places a
destructive or session-reassigning action needs a human in the loop. This
keeps the mechanism portable (testable and reusable by any adapter) while
keeping the safety-critical human checkpoints where the runtime can actually
surface them.
