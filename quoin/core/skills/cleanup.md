# cleanup

Runtime-neutral intent for the cleanup skill. Any runtime adapter (Claude,
Codex, …) that implements this skill should match the contract described here.

## Purpose

Trash-move stale workflow sentinels, old checkpoint files, and stale session
temp-write leftovers (`sessions/*.body.tmp`, `sessions/*.tmp` — crashed
atomic-writer artifacts, IVG-137 T-06) into a recoverable `trash/<date>/`
archive so the session state directory stays navigable and stale sentinels do
not cause false-positive lifecycle events.

A standalone run (not fired from `/checkpoint`) also offers a task bookkeeping
pass: it sorts `.workflow_artifacts/` task folders into five buckets — done,
abandoned, nearly-done, in-progress, not-a-task — and, after per-task
confirmation, archives or trashes the ones that are safe to move.

## When to use

- User says "/cleanup" or "clean up stale sentinels".
- Automatically fired as the first sub-block of `/checkpoint` unless `--no-cleanup`.

## Inputs

- `.workflow_artifacts/memory/` — all sentinel files (`*.txt`, `*.md` sentinel
  families) and checkpoint files.
- Current session UUID (to identify the freshest/current session's sentinels and
  skip them from cleanup).
- `QUOIN_CLEANUP_SENTINEL_WINDOW` env var (default `1d`): sentinels older than this
  are trashed (except the freshest sentinel per family and the current session's).
- `QUOIN_CLEANUP_CKPT_WINDOW` env var (default `30d`): checkpoint files older than
  this are trashed.

## Output

- Stale sentinels and old checkpoints moved to `.workflow_artifacts/memory/trash/<date>/`.
- Optional dry-run report (when `--dry-run` flag present): lists what WOULD be moved
  without moving anything.
- Standalone only: a classification table (`--no-tasks` to skip it), and, on
  confirmed rows, task folders moved to the top-level `.workflow_artifacts/finalized/`
  or `.workflow_artifacts/trash/<date>/` — a deliberate exception to the trash-only
  restriction above, since these are whole-task archive/trash moves, not sentinel
  sweeps.

## Behavior contract

- UUID-aware skip: before any age check, identify the current/freshest session UUID.
  NEVER trash sentinels belonging to the current or freshest session.
- Recoverable archive: all moves go to `trash/<date>/`, not permanent deletion.
  Recovery is manual `mv` from the trash directory; NOT `/sleep --restore`.
- Dry-run is safe: `--dry-run` MUST make no filesystem changes.
- Missing directories are a no-op (not an error).
- Never modify source files, never commit.
- The task bookkeeping pass never invokes `/pr` or `/end_of_task` — it only prints
  the command for the person to run themselves; it degrades to a report-only
  message (no folders moved) under `--dry-run`, `[no-interactive]`, `[autonomous]`,
  or when no `AskUserQuestion` channel is available (the common case for a
  dispatched Agent-tool subagent), and in that last case also prints a resume hint
  (`[no-redispatch] /cleanup`) so the run isn't a silent dead end.

## Out of scope

- Lessons-learned or daily-cache management (that is `/sleep` and `/end_of_day`).
- Model tier and §0 dispatch grammar — runtime adapter concerns.
