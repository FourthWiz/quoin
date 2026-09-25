---
name: cleanup
description: "Trash-moves stale sentinels and old checkpoints into a recoverable archive (.workflow_artifacts/memory/trash/). Use for: /cleanup [--dry-run]; auto-fires from /checkpoint unless --no-cleanup. Recovery is manual mv — NOT /sleep --restore."
model: sonnet
---

# Cleanup

*Portable intent doc: `quoin/core/skills/cleanup.md`*

You are the `/cleanup` skill. You trash-move stale workflow sentinels (all sessions except the freshest/current) and old checkpoint files into a recoverable `trash/<date>/` archive. You auto-fire as the first sub-block of `/checkpoint` Step 1.5 in save mode.

## §0 Model dispatch (FIRST STEP — execute before anything else)

This skill is declared `model: haiku`. If the executing agent is running on a model
strictly more expensive than the declared tier, you MUST self-dispatch before doing the
skill's actual work.

Detection:
  - Read your current model from the system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus.
  - Sentinel parsing: the user's prompt is checked for the `[no-redispatch]` family.
      * Bare `[no-redispatch]` (parent-emit form AND user manual override): skip dispatch, proceed to §0c at the current tier.
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
        description: "cleanup dispatched at haiku tier"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in cleanup. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §0c.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /cleanup`).
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
Otherwise (already at or below declared tier, OR prompt has [no-redispatch] sentinel, OR dispatch unavailable): proceed to §0c.
<!-- §0-end -->

## §0c Pidfile lifecycle (FIRST STEP after §0 dispatch)

At entry — immediately after §0 dispatch resolves:

```
. __QUOIN_HOME__/scripts/pidfile_helpers.sh && pidfile_acquire cleanup
```

If the script is missing or fails (e.g., fresh install): emit one-line warning `[quoin-S-2: pidfile helpers unavailable; proceeding without lifecycle protection]` and continue without abort (fail-OPEN).

At exit — call from every completion path AND every error/abort path:
```
pidfile_release cleanup
```

Use a trap when the skill body involves bash-driven subagents:
```
trap 'pidfile_release cleanup' EXIT
```

Purpose: lets `precompact.sh` hook know a `/cleanup` session is active.

Otherwise: proceed to §1 (skill body).

## When to use

Run `/cleanup` standalone to trash-move stale sentinels and old checkpoints in the current project's `.workflow_artifacts/memory/` directory. It also auto-fires as the first sub-block of `/checkpoint` Step 1.5 on every non-mid-agent, non-panicked save path (default-on).

Use cases:
- When `/checkpoint --restore` is resurrecting the wrong (stale) session — cleanup removes the stale sentinels that feed those stale restore paths.
- After heavy multi-session work, to clear accumulated stale sentinels from prior sessions.
- Manual maintenance when sentinel or checkpoint count grows large.

Use `--dry-run` first to preview what would be trashed without making any moves.

## Core procedure

(Standalone run — always executes for standalone invocations. When auto-fired from `/checkpoint` Step 1.47, the same procedure runs inline with high-util/panic/mid-agent skip guards applied first.)

**Step 1. Resolve MEMORY_DIR:** `<cwd>/.workflow_artifacts/memory`. If the directory does not exist, emit `[cleanup] no memory dir; nothing to do` and exit 0.

**Step 2. Source helpers:** `. __QUOIN_HOME__/hooks/_lib.sh` (provides `trash_move`). If the file is missing or sourcing fails: emit one-line warning `[cleanup] _lib.sh unavailable; cannot trash-move — exiting without changes` and exit 0 (fail-OPEN).

**Step 3. Acquire current/freshest session UUID** (same procedure as checkpoint Step 1.1):
- Priority 1: harness-provided system-context UUID.
- Priority 2: stem of the most-recently-modified `__QUOIN_HOME__/projects/<project-hash>/<uuid>.jsonl` file. `<project-hash>` = project absolute path with `/` replaced by `-`.
- If UUID cannot be obtained: emit `[cleanup] current-session UUID unavailable; skipping sentinel sweep (fail-safe)` and **skip step 4 entirely**. Proceed directly to step 5 (checkpoint sweep). The age-only 30d checkpoint sweep is safe without UUID. NEVER fall back to age-only with any shorter floor for sentinels — if the current session cannot be identified, trash nothing from sentinel families.

**Step 4. Sentinel sweep:** For each of the 9 hardcoded families listed in `## Hardcoded sentinel allow-list`, find candidates under `MEMORY_DIR` at depth 1:
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

**Step 5b. Session temp-file sweep (IVG-137 T-06, data hygiene).** Crashed Class-A-writer leftovers: the session-state atomic-write mechanism (`implement`/`end_of_day`/etc.) composes a body to `<session-path>.body.tmp`, validates it, then atomically renames to `<session-path>`. A process that crashes or is interrupted mid-write leaves the `.body.tmp` (or a bare `.tmp`) file behind. These are NOT selected by any real reader (`select_unprocessed_sessions.py`'s `file_pattern` is anchored `\.md$`, so `.body.tmp`/`.tmp` files are already excluded from selection today) but they pollute manual `ls`/`grep end_of_day_due: yes` inspection of `sessions/`. Find candidates:
```sh
find "${MEMORY_DIR}/sessions" -maxdepth 1 \( -name '*.body.tmp' -o -name '*.tmp' \) \
  -mtime "+${QUOIN_CLEANUP_SENTINEL_WINDOW:-1}" -print0
```
Reuses the same `QUOIN_CLEANUP_SENTINEL_WINDOW` (default 1 day) age threshold as the sentinel sweep — a legitimate write completes (write → validate → rename) in well under a second, so any survivor older than the window is a crash artifact, never an in-flight write. For each candidate: `trash_move "<path>" "$MEMORY_DIR"`. **Never** matches a real `*.md` session file (the glob is `*.body.tmp` / `*.tmp` only) — no UUID protection needed since these files have no "current session" concept (a session's own live write is always fresher than the age window).

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
For each candidate: `trash_move "<path>" "$MEMORY_DIR"`. Age-only — no UUID protection and
no `active` guard: the record and its notes file are swept INDEPENDENTLY of each other and
of whatever `active` says, so a record kept alive by ongoing boundary writes may outlive
notes that stopped being appended, and vice versa. That is deliberate — pairing them would
require reading the record to sweep it, which this age-only design forbids. The consequence
is that a swept record's `notes_path` may point at a file that no longer resolves; every
reader already tolerates that (T-04, T-11 tier 4).

**Step 6. Emit summary:**
- If any files were trashed: `[cleanup] trashed <S> sentinel(s) -> .workflow_artifacts/memory/, <T> session temp-file(s) -> .workflow_artifacts/memory/sessions/, <C> checkpoint(s) -> .workflow_artifacts/memory/checkpoints/, <R> run-state file(s) -> .workflow_artifacts/memory/ (recover via: mv .workflow_artifacts/memory/trash/<date>/<file> <original-dir>)`. NOTE: do NOT say "recoverable via /sleep --restore" — `/sleep --restore` only searches `forgotten/` text entries, not `trash/` files.
- If zero files trashed: `[cleanup] nothing stale to clean`.

If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

**Step 7. Cost tracking (conditional):** append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `cleanup` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md` — IF task context is active (a `.workflow_artifacts/<task>/cost-ledger.md` exists at cwd). Skip if no task context (per Q-02: no ledger write when no task).

<!-- quoin:ledger-self-write -->

**Step 8.** `pidfile_release cleanup`.

## --dry-run

When `/cleanup --dry-run` is invoked:

1. Run steps 1–3 (resolve MEMORY_DIR, source helpers, acquire UUID).
2. Run the sentinel, session temp-file, checkpoint, and run-state enumeration (steps 4–5c `find` commands) but make NO trash-moves.
3. Print the would-trash list:
   ```
   [cleanup --dry-run] would trash:
     SENTINELS (<S>):
       - <path>
       ...
     SESSION TEMP FILES (<T>):
       - <path>
       ...
     CHECKPOINTS (<C>):
       - <path>
       ...
     RUN STATE (<R>):
       - <path>
       ...
   ```
   If zero candidates: `[cleanup --dry-run] nothing stale to clean (no moves would be made)`.
4. **Makes NO writes** — no trash-moves, no cost-ledger row.

To preview what `/checkpoint` auto-fire would trash without running a full save: use `--no-cleanup` to suppress auto-fire, then run `/cleanup --dry-run` standalone.

## Hardcoded sentinel allow-list (9 families)

The sentinel sweep targets ONLY these 9 hardcoded families. This is a hardcoded allow-list — no user-supplied pattern argument, no catch-all globs:

1. `pending-restore-*.txt`
2. `pending-prompt-*.txt`
3. `compact-happened-*.txt`
4. `mid-agent-handoff-*.txt`
5. `pending-resume-ref-*.txt`
6. `checkpoint-defer-*.txt`
7. `postcompact-reset-*.txt`
8. `checkpoint-pending-compact-*.txt`
9. `idle-advisory-pending-*.txt`

These families are enumerated literally in the sentinel sweep. No user-supplied pattern is accepted. No catch-all glob (`*.txt`, `pending-*.txt`) is used as the sweep target.

Canonical machine-readable source: `hooks/_lib.sh:sentinel_globs()` — sessionstart.sh consumes that; this list and sleep/SKILL.md's list MUST stay byte-identical (drift-guarded by test_sentinel_family_parity.py).

`/cleanup` NEVER targets `lessons-learned.md`, `forgotten/`, or any source file — only the 9 sentinel families above, `checkpoints/*.md`, and the task-keyed `run-state-*.json` / `run-notes-*.md` pair, under `.workflow_artifacts/memory/`.

## Relationship to /sleep --purge --sentinels

`/cleanup` and `/sleep --purge --sentinels` both target the same 9 sentinel families (byte-identical literal lists) but are NOT the same operation:

| Dimension | /cleanup | /sleep --purge --sentinels |
|---|---|---|
| Delete mechanism | `trash_move` (recoverable, to `trash/<date>/`) | `rm -f` (permanent delete) |
| Selection logic | keep-freshest (UUID skip) + age (`QUOIN_CLEANUP_SENTINEL_WINDOW`, default 1d) | age-only (`--older-than Nd`, explicit argument required) |
| Current session | always preserved (UUID-suffix skip BEFORE age check) | no special protection |
| Auto-fire | yes (from `/checkpoint` Step 1.47, default-on) | no (requires explicit invocation) |
| Recovery | `mv .workflow_artifacts/memory/trash/<date>/<file> .workflow_artifacts/memory/` | not recoverable |

The task-keyed `run-state-*.json` / `run-notes-*.md` pair (IVG-258 T-13) is `/cleanup`-only:
it is not a sentinel family, does not appear in `sentinel_globs()`, and `/sleep --purge
--sentinels` never reclaims it.

Use `/cleanup` for routine session hygiene (auto-fires from `/checkpoint`, recoverable).
Use `/sleep --purge --sentinels --older-than Nd` for explicit permanent purge of files you are sure you no longer need.

## Write-target / delete-target restriction

**/cleanup ONLY trash-moves files under `.workflow_artifacts/memory/` matching the 9 sentinel families listed above, `sessions/*.body.tmp` / `sessions/*.tmp` (IVG-137 T-06), `checkpoints/*.md`, or the task-keyed `run-state-*.json` / `run-notes-*.md` pair (IVG-258 T-13); it never touches `lessons-learned.md`, `forgotten/`, or any real `*.md` session file.**

Any other trash-move or write is a bug.
