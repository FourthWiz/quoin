# sleep

Runtime-neutral intent for the sleep skill. Any runtime adapter (Claude,
Codex, …) that implements this skill should match the contract described here.

## Purpose

Consolidate memory: scan daily insights and session files from the last 30 days,
decide for each entry whether to Promote (write to `lessons-learned.md`), Soft-Forget
(archive to `forgotten/<date>.md`), or Defer (leave for a future run). Auto-invoked
by `/end_of_day` as its final step; also usable standalone.

## When to use

- Called automatically by `/end_of_day` after daily-cache consolidation.
- User says "/sleep", "consolidate memory", "promote insights", "archive stale entries".

## Inputs

- `.workflow_artifacts/memory/daily/insights-<date>.md` files (30-day window).
- `.workflow_artifacts/memory/sessions/<date>-*.md` files (30-day window).
- `.workflow_artifacts/memory/lessons-learned.md` (advisory; created if absent).
- `memory-maintenance.yaml` config (optional): lists protected entry sources that should
  never be promoted to lessons-learned.

## Output

- Zero-or-more new lessons appended to `.workflow_artifacts/memory/lessons-learned.md`
  (user-confirmed before write).
- Soft-forgotten entries moved to `.workflow_artifacts/memory/forgotten/<date>.md`.
- Optional dry-run report (when `--dry-run` flag present).

## Behavior contract

- Write boundary: this skill writes ONLY to `lessons-learned.md` and
  `forgotten/<date>.md`. It MUST NOT touch session files, daily-cache files, or
  source files.
- Protected sources: entries matching patterns in `memory-maintenance.yaml` are
  skipped from promotion consideration.
- Promotion is interactive: present candidates, user confirms which to keep.
- Dry-run is safe: `--dry-run` MUST make no filesystem changes.
- Subcommands: `--restore <pattern>` (copy from forgotten/ back to scratchpad),
  `--purge --older-than 90d` (delete old forgotten entries permanently),
  `--escalate` (re-present middle-band entries for promotion decision).
- `--skip-sleep` is not a subcommand of this skill — it is a flag on the end-of-day
  skill. This skill is auto-invoked by the end-of-day skill as its final step; passing
  `--skip-sleep` to that skill's invocation opts out of the auto-invocation.

## Out of scope

- Daily-cache consolidation (that is `/end_of_day`).
- Sentinel cleanup (that is `/cleanup`).
- Model tier and §0 dispatch grammar — runtime adapter concerns.
- The specific scoring algorithm for Promote/Soft-Forget/Defer decisions —
  adapter-specific; configure via `sleep-signals.yaml`.
