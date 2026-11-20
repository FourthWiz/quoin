---
name: cleanup
description: "Moves stale sentinels and old checkpoints to a recoverable trash folder. Use for: /cleanup [--dry-run] [--no-tasks]. Restore by manual mv, NOT /sleep --restore."
model: sonnet
context: fork
---

# Cleanup

*Portable intent doc: `quoin/core/skills/cleanup.md`*

You are the `/cleanup` skill. You trash-move stale workflow sentinels (all sessions except the freshest/current) and old checkpoint files into a recoverable `trash/<date>/` archive. You auto-fire from `/end_of_day` after its report.

## When to use

Run `/cleanup` standalone to trash-move stale sentinels and old checkpoints in the current project's `.workflow_artifacts/memory/` directory. It also auto-fires from `/end_of_day` after its report (default-on, with `--no-tasks`; opt out with `--no-cleanup`).

Use cases:
- After heavy multi-session work, to clear accumulated stale sentinels from prior sessions.
- Manual maintenance when sentinel or checkpoint count grows large.

Use `--dry-run` first to preview what would be trashed without making any moves.

## Core procedure

(The same steps run standalone and when `/end_of_day` auto-fires this skill.)

**Step 1. Resolve MEMORY_DIR:** `<cwd>/.workflow_artifacts/memory`. If the directory does not exist, emit `[cleanup] no memory dir; nothing to do` and skip to `## Task bookkeeping pass`.

**Step 2. Source helpers:** `. ${CLAUDE_PLUGIN_ROOT}/hooks/_lib.sh` (provides `trash_move`). If the file is missing or sourcing fails: emit one-line warning `[cleanup] _lib.sh unavailable; cannot trash-move — skipping sentinel sweeps` and skip to `## Task bookkeeping pass` (fail-OPEN).

**Step 3. Acquire current/freshest session UUID:**
- Priority 1: harness-provided system-context UUID.
- Priority 2: stem of the most-recently-modified `${CLAUDE_PLUGIN_ROOT}/projects/<project-hash>/<uuid>.jsonl` file. `<project-hash>` = project absolute path with `/` replaced by `-`.
- If UUID cannot be obtained: emit `[cleanup] current-session UUID unavailable; skipping sentinel sweep (fail-safe)` and **skip step 4 entirely**. Proceed directly to step 5 (checkpoint sweep). The age-only 30d checkpoint sweep is safe without UUID. NEVER fall back to age-only with any shorter floor for sentinels — if the current session cannot be identified, trash nothing from sentinel families.

**Step 4. Sentinel sweep:** For each of the 2 hardcoded families listed in `## Hardcoded sentinel allow-list`, find candidates under `MEMORY_DIR` at depth 1:
```sh
find "$MEMORY_DIR" -maxdepth 1 -name '<family-glob>' \
  -mtime "+${QUOIN_CLEANUP_SENTINEL_WINDOW:-1}" -print0
```
For each candidate file:
- **UUID check FIRST (before any age check):** SKIP if the filename suffix matches `-<current_uuid>.txt`. This is the current/freshest session's sentinel — invariant, protected regardless of age.
- **Empty-SID orphan eligibility:** Sentinels with suffix `-.txt` (e.g., `pending-restore-.txt`, produced when session UUID was empty or unknown at write time) can NEVER match `-<current_uuid>.txt` (a real UUID is always non-empty). They are therefore always trash-eligible once older than `QUOIN_CLEANUP_SENTINEL_WINDOW`. The existing `find ... -name 'pending-restore-*.txt'` glob already matches `pending-restore-.txt` — no logic change is needed; this note clarifies that the orphan is in scope.
- Otherwise: `trash_move "<path>" "$MEMORY_DIR"`.

**Step 5. Checkpoint sweep:** Find checkpoint files older than `QUOIN_CLEANUP_CKPT_WINDOW` (default 30 days):
```sh
find "${MEMORY_DIR}/checkpoints" -maxdepth 1 -name '*.md' ! -name '*.tmp' \
  -mtime "+${QUOIN_CLEANUP_CKPT_WINDOW:-30}" -print0
```
For each candidate: `trash_move "<path>" "$MEMORY_DIR"`.
(No UUID protection needed: the 30d age window already excludes the just-written checkpoint and any same-day or recent checkpoints.)

**Step 5b. Session temp-file sweep (IVG-137 T-06, data hygiene).** (rationale: memory/lifecycle-guide.md) Find candidates:
```sh
find "${MEMORY_DIR}/sessions" -maxdepth 1 \( -name '*.body.tmp' -o -name '*.tmp' \) \
  -mtime "+${QUOIN_CLEANUP_SENTINEL_WINDOW:-1}" -print0
```
For each candidate: `trash_move "<path>" "$MEMORY_DIR"`. **Never** matches a real `*.md` session file (the glob is `*.body.tmp` / `*.tmp` only) — no UUID protection needed since these files have no "current session" concept (a session's own live write is always fresher than the age window).

**Step 5c. Run-state sweep (IVG-258 T-13, task-keyed resumability records).** Two globs,
two windows: the record/notes pair swept on the same 30-day window as checkpoints, and
abandoned writer scratch files swept on the shorter sentinel window (a `.tmp` file older
than a few days can never be an in-flight write):
```sh
find "$MEMORY_DIR" -maxdepth 1 \
  \( -name 'run-state-*.json' -o -name 'run-notes-*.md' -o -name 'run-notes-*.md.1' \) \
  -mtime "+${QUOIN_CLEANUP_RUNSTATE_WINDOW:-30}" -print0
find "$MEMORY_DIR" -maxdepth 1 -name 'run-state-*.json.*.tmp' \
  -mtime "+${QUOIN_CLEANUP_SENTINEL_WINDOW:-1}" -print0
```
For each candidate: `trash_move "<path>" "$MEMORY_DIR"`. Age-only — no UUID protection
(rationale: memory/lifecycle-guide.md).

**Step 6. Emit summary:**
- If any files were trashed: `[cleanup] trashed <S> sentinel(s) -> .workflow_artifacts/memory/, <T> session temp-file(s) -> .workflow_artifacts/memory/sessions/, <C> checkpoint(s) -> .workflow_artifacts/memory/checkpoints/, <R> run-state file(s) -> .workflow_artifacts/memory/ (recover via: mv .workflow_artifacts/memory/trash/<date>/<file> <original-dir>)`. NOTE: do NOT say "recoverable via /sleep --restore" — `/sleep --restore` only searches `forgotten/` text entries, not `trash/` files.
- If zero files trashed: `[cleanup] nothing stale to clean`.

If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

**Step 7. Cost tracking (conditional, runs from `## Task bookkeeping pass`'s final step; not under `--dry-run`):** append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `cleanup` — format/rules: `${CLAUDE_PLUGIN_ROOT}/memory/cost-ledger-format.md` — IF task context is active (a `.workflow_artifacts/<task>/cost-ledger.md` exists at cwd). Skip if no task context (per Q-02: no ledger write when no task).

<!-- quoin:ledger-self-write -->

## Task bookkeeping pass (standalone only)

Runs after Core procedure Step 7 on a standalone `/cleanup`. The `/end_of_day` auto-fire passes `--no-tasks`, so no folder is ever moved from an automatic hook point. `--no-tasks`: skip steps 1-3, go straight to step 4. Sorts task folders into done, abandoned, nearly-done, in-progress and not-a-task.

1. `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/task_bookkeeping.py classify --format table`; print verbatim. Missing script: `[cleanup] task_bookkeeping.py unavailable; skipping task pass`. Non-zero exit: print its stderr line, go to step 4.
2. `--dry-run`, `[no-interactive]`, `[autonomous]`, or no `AskUserQuestion`: print `[cleanup] task bookkeeping: report-only (<reason>); no folders moved`; when `<reason>` is the no-`AskUserQuestion` case (dispatched subagent), also print `to act on these rows, run: [no-redispatch] /cleanup`. Go to step 4.
3. Else follow `${CLAUDE_PLUGIN_ROOT}/memory/cleanup-task-bookkeeping.md` (report-only if missing): per-task confirmation, moves only via its `apply` command, print `/pr` or `/end_of_task …` for the user — never invoke them.
4. Step 7 (not under `--dry-run`; skip any folder moved above).

## --dry-run

When `/cleanup --dry-run` is invoked:

1. Run steps 1–3 (resolve MEMORY_DIR, source helpers, acquire UUID).
2. Run the sentinel, session temp-file, checkpoint, and run-state enumeration (steps 4–5c `find` commands) but make NO trash-moves.
3. Print the would-trash list (`[cleanup --dry-run] would trash:` then per-bucket headers SENTINELS / SESSION TEMP FILES / CHECKPOINTS / RUN STATE, each followed by its candidate paths; full example: memory/lifecycle-guide.md). If zero candidates: `[cleanup --dry-run] nothing stale to clean (no moves would be made)`.
4. **Makes NO writes** — no trash-moves, no cost-ledger row.
5. Task bookkeeping pass: table only, no prompts, no moves.

To preview what the `/end_of_day` auto-fire would trash, run `/cleanup --dry-run` standalone; pass `--no-cleanup` to `/end_of_day` to suppress the auto-fire.

## Hardcoded sentinel allow-list (2 families)

The sentinel sweep targets ONLY these 2 hardcoded families. This is a hardcoded allow-list — no user-supplied pattern argument, no catch-all globs:

1. `pending-restore-*.txt`
2. `compact-happened-*.txt`

These families are enumerated literally in the sentinel sweep. No user-supplied pattern is accepted. No catch-all glob (`*.txt`, `pending-*.txt`) is used as the sweep target. Retired families that no writer produces any more are reclaimed by the SessionStart sweep, not listed here.

Canonical machine-readable source: `hooks/_lib.sh:sentinel_globs()` — this list and sleep/SKILL.md's list MUST stay byte-identical (drift-guarded by test_sentinel_family_parity.py).

`/cleanup` NEVER targets `lessons-learned.md`, `forgotten/`, or any source file — only the 2 sentinel families above, `checkpoints/*.md`, and the task-keyed `run-state-*.json` / `run-notes-*.md` pair, under `.workflow_artifacts/memory/`.

## Relationship to /sleep --purge --sentinels

`/cleanup` and `/sleep --purge --sentinels` both target the same 2 sentinel families (byte-identical literal lists) but are NOT the same operation (comparison table: memory/lifecycle-guide.md).

The task-keyed `run-state-*.json` / `run-notes-*.md` pair (IVG-258 T-13) is `/cleanup`-only:
it is not a sentinel family, does not appear in `sentinel_globs()`, and `/sleep --purge
--sentinels` never reclaims it.

Use `/cleanup` for routine session hygiene (auto-fires from `/end_of_day`, recoverable).
Use `/sleep --purge --sentinels --older-than Nd` for explicit permanent purge of files you are sure you no longer need.

## Write-target / delete-target restriction

**/cleanup ONLY trash-moves files under `.workflow_artifacts/memory/` matching the 2 sentinel families listed above, `sessions/*.body.tmp` / `sessions/*.tmp` (IVG-137 T-06), `checkpoints/*.md`, or the task-keyed `run-state-*.json` / `run-notes-*.md` pair (IVG-258 T-13); it never touches `lessons-learned.md`, `forgotten/`, or any real `*.md` session file.**

Exception: the standalone task bookkeeping pass moves whole task folders, only through `task_bookkeeping.py apply` and only after per-task confirmation, into `.workflow_artifacts/finalized/` or `.workflow_artifacts/trash/<date>/`.

Any other trash-move or write is a bug.
