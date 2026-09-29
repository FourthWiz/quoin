# Hooks deployed by quoin — full reference table

`bash install.sh` deploys hook scripts to `__QUOIN_HOME__/hooks/` and registers nine (event, matcher) stanzas in `__QUOIN_HOME__/settings.json`:

| Event | Matcher | Script | Timeout | Contract |
|-------|---------|--------|---------|----------|
| UserPromptSubmit | `*` | `userpromptsubmit.sh` | 5s | Context utilization check; advisory only — never blocks a prompt. The advisory's "consider running /checkpoint" nudge is emitted only when a fresh active run-state record exists anywhere in the project (project-scoped, matching `/checkpoint` Step 1.4) |
| PreCompact | `auto` | `precompact.sh` | 10s | ALWAYS allows auto-compaction; appends the STEP 1b `recent-sessions.md` record and a `telemetry/compaction-events.jsonl` pre-event on every row (a `.allow-compact` marker in cwd exits right after the recent-sessions append — telemetry and every later step are skipped); writes a deterministic checkpoint when a fresh active run-state record matches the session or skill pidfiles are present; a plain conversation (no run, no pidfiles) writes no checkpoint and no sentinel — the recent-sessions and telemetry appends still happen on every row; `QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1` restores the checkpoint-plus-`pending-restore` sentinel for that case; `/continue_work` (fed by `recent-sessions.md`) is the recovery path when nothing is written |
| PostCompact | `auto` | `postcompact.sh` | 5s | Writes `postcompact-reset-${session_id}.txt` sentinel; `userpromptsubmit.sh` STEP 0.5 consumes it to confirm compaction occurred and trash-moves the sentinel; as its last statement, appends the "post" half of a `telemetry/compaction-events.jsonl` compaction event (paired with the "pre" half `precompact.sh` wrote), never printing anything to stdout on any path; a `.allow-compact` marker in cwd suppresses this telemetry append ONLY — both sentinels above are still written, unlike the PreCompact row, where the same marker skips the whole hook |
| SessionStart | `startup` | `sessionstart.sh` | 5s | Pending-restore + missing-EOD banner (S-4) — this body runs only on `startup`/`resume`; a `compact` invocation takes the dedicated early-exit branch below instead |
| SessionStart | `resume` | `sessionstart.sh` | 5s | Pending-restore + missing-EOD banner (S-4) — this body runs only on `startup`/`resume`; a `compact` invocation takes the dedicated early-exit branch below instead |
| SessionStart | `compact` | `sessionstart.sh` | 5s | Dedicated early-exit branch (IVG-258 S-4), reached before the startup/resume banner body; on a fresh active run-state record matching the session, emits exactly one JSON object carrying `additionalContext` (always — an active run's task/phase/step, next action, and run-notes path) and `initialUserMessage` (echoing the record's `resume_command` verbatim, when present) — the dual re-entry channel; silent no-op (no stdout, exit 0) when no matching record is found, including a `session_id` mismatch |
| SessionEnd | `*` | `sessionend.sh` | 5s | EOD nudge if `end_of_day_due: yes` |
| WorktreeCreate | `*` | `worktreecreate.sh` | 10s | Nested-git worktree isolation for source-mutating skills. Reads the dispatch sidecar; when a single nested repo resolves and the harness omits path/branch, self-generates `quoin/wt-<ts>-<pid>` + a worktree under `${TMPDIR:-/tmp}/quoin-worktrees` (outside the Drive tree; project `.worktrees/` fallback) and runs `git worktree add` (bounded by `QUOIN_SUBPROCESS_TIMEOUT`), printing the path to stdout. Fail-OPEN (exits 0, no stdout on any skip/error); audit log records `selfgen=1`. Opt-out: `QUOIN_WORKTREE_SELFGEN=0`. |
| Stop | `*` | `stop.sh` | 10s | Continues an interrupted /run in the session that armed it. Two shell early exits (no memory dir, no arm file) before any Python start; decision in `scripts/auto_resume.py`. Fail-OPEN. Opt-out: `QUOIN_AUTO_RESUME=0`. |

All hooks fail-OPEN (exit 0 on any error). jq is a soft-required dependency (`brew install jq`). Tunable constants (`QUOIN_BYTES_PER_TOKEN`, `QUOIN_EFFECTIVE_CONTEXT_LIMIT`, `QUOIN_STOP_BPS`, `QUOIN_BLOCK_BPS`, `QUOIN_COMPACT_FIRST_BPS`, `QUOIN_PANIC_BPS`, etc.) use `${QUOIN_*:-default}` expansion; thresholds use integer basis-points arithmetic (e.g., `8500` = 85.00%, `9000` = 90.00%).

Hook-side tunable constants (defined in `_lib.sh:read_constants()` and exported to all hook scripts):
- `QUOIN_COMPACT_FIRST_BPS` (default 9000 = 90.00%) — threshold at which `/checkpoint` emits a high-util notice and prompts the user to run `/compact` before saving.
- `QUOIN_PANIC_BPS` (default 10000 = 100.00%) — threshold at which `/checkpoint` switches to a minimal panic/degraded-save path, skipping heavy session-state gathering and AskUserQuestion, writing only a skeleton checkpoint + pending-restore sentinel. `compute_utilization` is unclamped so values >10000 are normal; PANIC_BPS=10000 correctly fires for all true overflow (>=100%).
- `QUOIN_STALE_SENTINEL_DAYS` (pre-existing, default 7) — sentinel age threshold used by `sessionstart.sh` as the fallback sweep window when `session_id` is empty (defense-in-depth, per IVG-95 D-02). Also consumed by `/cleanup` and `/sleep --purge --sentinels`.
- `QUOIN_SESSIONSTART_SWEEP_DAYS` (NEW IVG-95, default 1) — UUID-aware tight sweep window for `sessionstart.sh` STEP 2. When `session_id` is known, files older than this window AND not matching the current session's UUID are trash-moved. Also narrows the STEP 4 cross-session `pending-restore` restore-banner fallback window (intentional per IVG-95 D-08).
- `QUOIN_SOD_SENTINEL_WARN` (NEW IVG-95, default 3) — `start_of_day` Step 1b sentinel-health check threshold. Emits a one-line advisory banner if the count of stale sentinels in `memory/` exceeds this value. Read-only — `start_of_day` never mutates files.
- `QUOIN_PRECOMPACT_NORUN_CHECKPOINT` (NEW IVG-258 S-3, default 0) — opt-in: when 1, `precompact.sh` restores the deterministic checkpoint-plus-`pending-restore` sentinel for a plain conversation (no active run-state record, no skill pidfiles); when 0, that row writes nothing.
- `QUOIN_RUN_STATE_STALE_DAYS` — one knob, two independent windows, each with its own default:
  - Selection window, default 1 — read by `/run` Step 0c's `--max-age-days`, `run_state_select` (`_lib.sh`, used by `precompact.sh`'s run-state selection), and `run_state.py`'s own default. Day-granular, deliberately over-inclusive.
  - Probe window, default 14 — read only by `run_state_probe` (`_lib.sh`); its four consumers are the two `userpromptsubmit.sh` advisory branches, `/run`'s 90% self-checkpoint block, and `/checkpoint` Step 1.4's compact-already-ran skip check (not itself a self-checkpoint block; there PROBE_INACTIVE plus a compact-happened sentinel skips the user's redundant save, while PROBE_ACTIVE or an unavailable guard falls through to a real save). Wider on purpose: the probe has no downstream max-age gate, so its window is the final word on "active or not."
  - Raising the knob widens BOTH windows at once — on the hook paths there is no way to widen one without the other, since `read_constants` re-derives its exported `RUN_STATE_STALE_DAYS` from this knob; only outside `read_constants` contexts can a bare `RUN_STATE_STALE_DAYS` env var move the selection window alone (`run_state_select` prefers it; the probe never reads it) — a value meant to widen the probe's 14-day window past its default also widens the tighter 1-day selection window, and a value meant to narrow the 1-day selection window (e.g. down to a stricter same-day cutoff) also narrows the probe's 14-day window to match, which can make a still-active run read as stale sooner than expected.
- `QUOIN_TELEMETRY_MAX_BYTES` (NEW IVG-258 stage 5, default 1048576 = 1 MiB) — compaction-telemetry sink rotation size in bytes. `precompact.sh` checks this at the pair boundary, immediately before appending the next "pre": once the live sink exceeds this size, it is renamed to a single `.1` generation before the new "pre" lands, so a rotation always moves whole pairs. See "Compaction telemetry sink" below for the full schema and the reader that joins both generations back into one stream.

Skill-side picker knobs (read inline in `checkpoint/SKILL.md`, NOT hook constants — do NOT add to `_lib.sh`):
- `QUOIN_RESTORE_SENTINEL_WINDOW` (default 7 days) — mtime filter for pending-restore sentinel enumeration.
- `QUOIN_SESSION_FALLBACK_WINDOW` (default 7 days) — mtime filter for session-state fallback (B3).
- `QUOIN_RESTORE_STALE_DAYS` (default 1 day) — maximum age for a checkpoint to be silently auto-picked; candidates older than this trigger a loud warning and prefer B3 synthesis. **NOT a hook constant** — read inline at the picker site in `checkpoint/SKILL.md` as `${QUOIN_RESTORE_STALE_DAYS:-1}`; no hook reads it; do NOT add to `_lib.sh`.
- `QUOIN_PICKER_DEDUP_WINDOW` (default 7 days) — window for deduplicating voluntary vs precompact checkpoint pairs.

Skill-side sweep knobs (read inline in `cleanup/SKILL.md`, NOT hook constants — do NOT add to `_lib.sh`):
- `QUOIN_CLEANUP_RUNSTATE_WINDOW` (default 30 days) — age threshold for `/cleanup`'s run-state sentinel sweep.

Script-side knobs (read inline by a `core/scripts/` script, neither a hook constant nor skill-side — do NOT add to `_lib.sh`):
- `QUOIN_RUN_NOTES_MAX_BYTES` (default 262144 = 256 KiB) — rotation cap for `run-notes-<task>.md`, read inline at `run_state.py`.
- `QUOIN_AUTO_RESUME` (`0` disables; default on) — read inline at `auto_resume.py`; every path behaves as today and the helper never writes a halt.
- `QUOIN_AUTO_RESUME_MAX` (default 10, clamp 1..100) — bound on continuations per span.
- `QUOIN_AUTO_RESUME_IDLE_SECS` (default 900, min 60) — transcript-idle threshold for a `gone` owner.
- `QUOIN_AUTO_RESUME_HANDOFF_AT` (default 6, clamp 1..7) — in-session block count at which the Stop hook attempts a hand-off.

**Opt-in platform-threshold delegation.** `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` and `CLAUDE_CODE_AUTO_COMPACT_WINDOW` are **not** `QUOIN_*` knobs — they are read natively by Claude Code, not by any quoin hook — and are never written by default. See `quoin/docs/hooks-guide.md` for the full opt-in contract.

Skill-side opt-out knobs (IVG-137, read inline in `end_of_day/SKILL.md` / `end_of_task/SKILL.md`, NOT hook constants — do NOT add to `_lib.sh`):
- `QUOIN_DISABLE_EOD_RECONCILE=1` — skips `/end_of_day --recover-orphans`'s Step 0a covered-but-due
  auto-flip pre-pass (falls through to today's Step 0 orphan-only behavior).
- `QUOIN_DISABLE_EOT_FLAG_FLIP=1` — skips `/end_of_task` Sub-phase B's single-invocation
  `--flip-finalized-task` flag-flip + `finalized_by_end_of_task` marker step (finalized sessions
  remain `end_of_day_due: yes` and surface as ordinary backlog on the next `/end_of_day`).

Verbose details: `quoin/docs/hooks-guide.md`.

## Compaction telemetry sink

`{project-root}/.workflow_artifacts/memory/telemetry/compaction-events.jsonl`
(plus, once rotated, a single previous generation at the same path with a
`.1` suffix — see `QUOIN_TELEMETRY_MAX_BYTES` above). One level below the
`.workflow_artifacts/memory/` depth-1 sentinel sweeps, so it is never a sweep
target. `precompact.sh` appends the "pre" half on every row it reaches,
**except** the `.allow-compact`-marker row: that row exits right after the
STEP 1b `recent-sessions.md` append, before the telemetry code is ever
reached, so the marker suppresses telemetry for that row (matching the
PreCompact table row above — not a second, contradicting behaviour).
`postcompact.sh` appends the "post" half as its last statement, after both
of its own sentinel writes.

**Line schema (`v: 1`, one JSON object per line, both halves):**

| Field | Half | Meaning |
|---|---|---|
| `v` | both | Schema version. A reader treats `v > 1` as schema-forward and skips it rather than guessing at a version it does not know. |
| `half` | both | `"pre"` or `"post"`. |
| `session_id` | both | The correlation key's session component. |
| `event_seq` | both | See "event_seq derivation" below. `null` on a `post` half means no eligible `pre` was found. |
| `ts` | both | UTC timestamp, second granularity. |
| `bytes_before` / `est_tokens_before` | `pre` | Transcript size and its `BPT`-derived token estimate at compaction start; `null` when the transcript path is unreadable. |
| `bytes_after` / `est_tokens_after` | `post` | Same, at compaction end. |
| `task` / `phase` / `subphase` / `step` | `pre` | The active run-state record's fields at compaction start, or empty strings when no run is active. A `post` never restates these — the run may have advanced between the two halves, so a duplicated copy could disagree with its own pair; the pair carries run identity once, on the `pre`. |
| `trigger` | `post` | The compaction trigger string from the hook's own stdin payload. |
| `compact_summary_len` | `post` | A **codepoint count** of the harness-supplied compaction summary — the only observable of summariser behavior this sink records. The summary text itself is never bound to a shell variable and never written. quoin does not pin which model produces the summary. |

The `est_` prefix marks every token figure as a `BPT`-derived estimate, never
an exact count.

**`event_seq` derivation and its two reset boundaries.** `precompact.sh`
derives its own "pre" sequence number by counting `"half":"pre"` lines for
the session in the live sink's last 1 MiB tail window; `postcompact.sh`
derives its "post" sequence number by taking the *highest* `event_seq` on a
`"half":"pre"` line for the session in that same window, but only when it is
strictly ahead of the highest `"half":"post"` line for the session in the
same window — a `pre` no more recent than the session's last recorded `post`
did not belong to the compaction now finishing, and adopting it would
synthesise a false pair. Unmatched halves are tolerated, never synthesised:
a `post` that finds no eligible `pre` writes `event_seq: null`. Two things
reset the counter to 0 for a session: rotation (a fresh file has no prior
"pre" lines to count), and the 1 MiB tail window itself once a session's
earlier rows scroll past it. A third, subtler case: the `pre` half *counts*
matching lines in the window while the `post` half takes their *max* — the
two derivations agree only while the window stays inside
`QUOIN_TELEMETRY_MAX_BYTES`, and can diverge into a same-file duplicate key
once that knob is raised past the hard-coded 1 MiB tail window. Consequently,
`(session_id, event_seq)` is unique **within a joined two-file stream, per
the reader's file-scoped tie-break** described below — never on `event_seq`
alone. `(session_id, ts)` narrows collisions further but is **not
absolute**, since `ts` is only second-granular.

**The reader.** `quoin/core/scripts/compaction_telemetry.py` (wrapper at
`quoin/scripts/compaction_telemetry.py`) reads the rotated generation first,
then the live file, into one chronological stream, each record tagged with
its source file. Pairing is scoped to a single source file, grouped per
`(file, session_id, event_seq)`: exactly one `pre` and one `post` sharing
that key in that file match; either side occurring more than once diverts
every record sharing that key in that file — `pre`s and `post`s alike — into
an `ambiguous` bucket, counted as individual records rather than pairs, and
none of them re-enters the matched/pre-only/post-only accounting; otherwise
(one side present, the other absent) becomes pre-only or post-only. A `post`
whose own `event_seq` is `null` is post-only unconditionally, with no key
grouping involved. Invocation: `compaction_telemetry.py --project-root
<path> [--format text|json] [--task NAME] [--session ID] [--since
YYYY-MM-DD]`. Always exits 0 and never raises on a malformed sink — a blank
line, non-JSON line, non-object line, an object with no `half`, or a
schema-forward `v` is skipped and counted separately, never fatal.
