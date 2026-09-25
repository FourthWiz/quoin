# Lifecycle skills — full reference

## Consumer audit (extracted 2026-05-15)

Pre-edit audit finding: `checkpoint/SKILL.md` contains a full self-contained specification of mode auto-detection logic (Step 1.5, pidfile checks, COMPACT_FIRST_BPS). It does NOT rely on CLAUDE.md as the authoritative source for that logic. The condensed CLAUDE.md text acts as a human-readable summary only; skills bootstrap from their own SKILL.md. No SKILL.md update needed.

## /checkpoint — full reference

Three skills handle session lifecycle at different granularities (v3 lifecycle separation per architecture):

**`/checkpoint`** — general-purpose state-save (mid-session, between tasks, between sessions). Supports three save modes via `--mode <name>` (auto-detected if omitted):
- `--mode restore` (default): writes the full checkpoint file and `pending-restore-${session_id}.txt` sentinel; use for standard resume-in-new-session flow.
- `--mode load-as-reference`: writes the full checkpoint AND `pending-resume-ref-${session_id}.txt`; instruct user to start a new session with `claude --resume SESSION_ID --fork-session`. The forked session sees the prior transcript as background context; sessionstart.sh emits a banner.
- `--mode mid-agent`: writes a minimal `mid-agent-handoff-${session_id}.txt` only (skips full checkpoint write); for use when another skill is actively running. User can type `/clear` then `/checkpoint --restore` in the same session, or `claude --resume SESSION_ID` in a new terminal.

Does NOT roll up dailies, does NOT touch `lessons-learned.md`, does NOT touch `forgotten/`. Use it mid-session before context exhaustion (when the context-utilization advisory fires), between tasks, between sessions, or proactively before starting new heavy work. **Paths-not-content rule (D-04):** checkpoint files record only PATHS to in-flight artifacts — never file contents. Restore re-fires Read tool on disk artifacts in the new session.

Auto-detection at invocation time: (1) if `compact-happened-${session_id}.txt` exists for this session AND EITHER `pending-restore-${session_id}.txt` also exists OR the run-state probe is available and reports no active run-state record anywhere in the project (when the probe is unavailable, that arm is inert and the check falls back to the dual-sentinel conjunction alone), skip save with a one-line INFO — the INFO names the auto-written checkpoint when `pending-restore-*` is the reason for the skip, or names no path when the no-active-run-state reason applies (nothing was written, and nothing needs resuming). The no-active-run-state check is project-scoped, not session-scoped: it asks "is anything active anywhere in this project," the same project-wide question the userpromptsubmit advisory's checkpoint nudge asks — both consumers share one definition of "active run," not two contrasting scoping choices; (2) else if util >= `COMPACT_FIRST_BPS` (default 90.00%), save immediately and surface a two-option notice (fresh session vs /compact in this session); (3) if other skills are detected via pidfiles, default to mid-agent mode; (4) otherwise, ask user to choose restore vs load-as-reference.

**`--restore` subcommand:** Run `/checkpoint --restore` in a fresh session to re-hydrate state. Locates the most recent checkpoint (or follows the `pending-restore-${session_id}.txt` sentinel from the pre-compaction save flow), surfaces task state, and optionally re-fires the saved pending prompt. Sentinel cleanup happens on restore. Voluntary `/checkpoint` now records `## Session ID` (UUID-anchored; two-line form) to disambiguate session-state lookup; restore picker now covers both pending-restore sentinels and recent (<= 30 day) checkpoint files (with fast-path bypass when current-session sentinel exists); `--defer` writes a marker that suppresses the userpromptsubmit advisory until the next compact or successful voluntary save; `--after-compact` is deprecated as of this release; retained only for command-history backward-compat; emits an INFO line and proceeds to the normal save flow. Cleanup of any legacy `checkpoint-pending-compact-*` markers (from old flow) is automatic on `--after-compact` invocation, via `sessionstart.sh` sweep, and via `/sleep --purge --sentinels --older-than Nd`. The `checkpoint-pending-compact-*` marker is no longer written by any new code path. Same-session detection: if the located checkpoint's `## Session ID` matches the current session UUID (and neither is `unknown`), `/checkpoint --restore` presents an `AskUserQuestion` warning the user they are restoring in the same session that saved; Option B exits with fresh-session instructions and does NOT proceed with restore.

## /end_of_day and /sleep — full reference

**`/end_of_day`** — rolls up daily session state into `.workflow_artifacts/memory/daily/<date>.md`. Touches `lessons-learned.md` if insights are promoted. Run at end of each work session.

**Session hooks (S-4):** `/start_of_day` checks `end_of_day_due: yes` in session files (Signal B, 36 h window). The `sessionstart.sh` hook provides the same check unconditionally at session-open (no need to invoke `/start_of_day`). The `sessionend.sh` hook nudges at session-close if the active session still has `end_of_day_due: yes`. Both hooks are non-blocking (exit 0) and informational only. Dedup: the sentinel file prevents duplicate banners within a 5-minute window.

**`/sleep`** — Sonnet-tier (IVG-263). Auto-invoked by `/end_of_day` as its final step (opt-out via `--skip-sleep`). Scans daily insights + session files within a 30-day window. Three-bucket decisions:
- Promote → `lessons-learned.md` (per-entry user confirmation)
- Soft-Forget → `forgotten/<date>.md` archive (per-entry user confirmation in default mode; skipped above `forget_quiet_floor` score with `--quiet-forget`)
- Middle-Band → deferred

First 30 days of production: `/sleep --dry-run` mode only (no writes); inspect proposed decisions to tune weights.

Subcommands:
- `/sleep --restore <pattern>`: moves entries back from `forgotten/` to their source (or today's insights if source gone)
- `/sleep --purge --older-than Nd`: true-deletes `forgotten/<date>.md` archive files; per-file confirmation; never auto-run (backward-compat default scope)
- `/sleep --purge --sentinels --older-than Nd`: true-deletes stale sentinel files from the 8 live families under `.workflow_artifacts/memory/`; per-file confirmation; never auto-run
- `/sleep --purge --all --older-than Nd`: convenience — runs `forgotten/` purge then sentinel purge (scope flags are mutually exclusive; `--all` opts into both)
- `/sleep --escalate`: re-examines middle-band candidates on Opus for deeper reasoning

**Boundary:** `/sleep` writes ONLY to `lessons-learned.md` and `forgotten/<date>.md`. Does NOT touch `~/.claude/projects/<hash>/memory/` (auto-memory). Distinct from `/checkpoint` (general-purpose state-save) and `/end_of_day` (end-of-workday rollup).

**Boundary summary:**
- `/checkpoint` → general-purpose state-save (`checkpoints/`, `sessions/`, `pending-*` sentinels — mid-session, between tasks, between sessions)
- `/end_of_day` → end-of-workday only — daily rollup (`daily/`, `lessons-learned.md`) + auto-invokes `/sleep`
- `/sleep` → long-term memory promote/forget (S-3 scope)

## /thorough_plan phase-boundary checkpoints (IVG-98)

`/thorough_plan` writes phase-boundary progress checkpoints at each planning-loop boundary
(after `/plan`, after each `/critic`, after each `/revise`/`/revise-fast`).

**Files written at each boundary:**
- `checkpoints/thorough-plan-progress-{sid}.md` — AUTHORITATIVE resume anchor. Fixed filename per
  session (D-03 idempotent overwrite). NOT a timestamped name, so the picker's "Saved" column may
  render a garbage date — cosmetic only; mtime-ordering and task-name display work correctly.
- `pending-restore-{sid}.txt` — sentinel for picker Tier-3 enumeration (written once at first boundary;
  omitted if SID is empty/`unknown`).
- `sessions/{date}-{task}-orchestrator.md` — ORCHESTRATOR-DEDICATED session-state (M-02). Updated at
  each boundary. Subagents write only to the standard `{date}-{task}.md` — these files are fully
  disjoint and serve different consumers.

**Stage token:** `## Current stage` value = `thorough-plan:round-{N}-{phase}` where phase ∈
{`plan`, `critic`, `revise`} and N is the 1-based round number. This token is recognized by
`/start_of_day`, `/end_of_day`, `/status`, `/continue_work`, and B3 awk (free-text readers — the
colon-delimited form is safe).

**PRIMARY resume path (B3-immune):** Re-invoke `/thorough_plan {task}` in a fresh session.
Setup §1b scans `checkpoints/thorough-plan-progress-*.md` directly, matches by `## Active task`,
applies a 7-day age bound, and offers Resume/Start-fresh via `AskUserQuestion`. This scan does NOT
go through the restore picker's B3 Clause-B gate and is therefore immune to it. Same-session guard: if the checkpoint SID (extracted from the filename `thorough-plan-progress-{SID}.md`) matches the current session UUID, §1b adds a third AskUserQuestion option (`(c) Resume in a new session`) and warns that continuing here dispatches subagents into an already-used context window.

**SECONDARY convenience path (B3-limited):** `/checkpoint --restore`. Works when no subagent
wrote `{date}-{task}.md` AFTER the last phase-boundary helper call. In the common
kill-during-subagent window (subagent writes its session file AFTER the last checkpoint), B3
Clause-B fires (`max(candidate mtime) < max(sessions/*.md mtime within 7d)`) and the picker
discards the checkpoint, synthesising a degraded task-level restore from the subagent file.
The sentinel/checkpoint pair is still written but cannot override B3 in that window. Use the
PRIMARY path for phase-precise recovery.

**`/run` transitive coverage:** When `/run` dispatches `/thorough_plan` as a subagent, the
phase-boundary checkpoints fire inside that subagent (SKILL.md edit runs). The planning kill-gap is
covered transitively. The deferred `/run` follow-up (IVG-98 D-06) concerns only the
implement→review inter-phase gap, not planning. Note: under `/run`, the checkpoint sentinel carries
the `/thorough_plan` subagent's SID, so restore re-enters `/thorough_plan` mid-loop rather than
`/run` — acceptable and documented.

## Memory layout

Directory tree and `forgotten/<date>.md` entry format:

```
.workflow_artifacts/memory/
├── sessions/          ← per-session state files
├── daily/             ← rendered briefings + insights scratchpads
├── weekly/            ← weekly review summaries
├── checkpoints/       ← /checkpoint restore sentinels
├── forgotten/         ← soft-forget archive (NEW — S-3)
│   └── <date>.md      ← one file per day /sleep ran with forgets
├── trash/             ← recoverable delete archive (sentinel trash-moves land here)
│   └── <YYYY-MM-DD>/  ←   dated subdirs, one per trash day
├── telemetry/         ← compaction-event sink (precompact.sh appends a "pre" line, postcompact.sh appends the paired "post" line, per auto-compaction)
│   └── compaction-events.jsonl[.1]  ← rotated at QUOIN_TELEMETRY_MAX_BYTES (two-file, bounded footprint); still outside both depth-1 sweeps — schema and reader: hooks-table.md, "Compaction telemetry sink"
├── run-state-<task>.json   ← task-keyed resume record (/run writes, /thorough_plan refines)
├── run-notes-<task>.md[.1] ← its append-only notes log, rotated at QUOIN_RUN_NOTES_MAX_BYTES
└── lessons-learned.md ← long-term institutional memory
```

`run-state-<task>.json` and its `run-notes-<task>.md` companion are swept by `/cleanup`'s
Step 5c on a 30-day age window (`QUOIN_CLEANUP_RUNSTATE_WINDOW`), same as checkpoints —
no UUID protection, independent of `active`. `run-notes-<task>.md` also has a
second appender: `precompact.sh` adds one block per auto-compaction while a fresh active
record for the compacting session says `at_stage_boundary: false` — including on the
early-skip path where a voluntary checkpoint sentinel already exists. The hook never
rewrites the JSON record and never rotates the notes file (rotation stays with the
record's Python writer).

**Session-id anchoring and adoption timing (moved here from the deploy-root rules file's
`### Workflow memory layers` table, IVG-258 `S-7`, to stay under that file's character
ceiling).** The run-state record's `phase`/`next_action`/`step` fields double as the
session-id anchor the compaction hook matches on. That anchor is adopted onto the
*resuming* session only at `/run`'s own Resume entry point, after its freshness-gated
reads — never as a side effect of a refresh write. A refresh write (e.g. `/thorough_plan`
updating `phase` mid-round) never re-anchors the record to the writing session; only
`/run`'s Resume path performs the adoption.

Note: `trash/` accumulates sentinel files moved by `trash_move()` (in `_lib.sh`) instead of hard-deleted. The `/sleep --purge --older-than 90d` scope does not yet include `trash/` — this is a known gap (R-7); a follow-up task will extend `/sleep --purge` to cover `trash/`.

Each `forgotten/<date>.md` file is append-only. Each entry block follows this format:

```
> Source: <absolute-path-to-source-file>:<start-line>..<end-line>
> Forgotten: <ISO timestamp>
> Score: forget=<N>, promote=<N>

<original entry text verbatim>

---
```

The `> Source:` line is the restore anchor for `/sleep --restore`. Use `/sleep --restore <pattern>` to search `forgotten/` and move entries back to their source location.

## Session state: fallback_fires and end_of_day_due — verbose reference

The `end_of_day_due` field defaults to `yes` at every session-state write. `/end_of_day` Step 3d flips it to `no` for each session the hybrid selection rule produced (NOT only today's files — all files in the processed window that had `end_of_day_due: yes`, plus any orphan-recovery files confirmed in Step 0). The flip happens ONLY after the daily-cache write succeeds; a crashed `/end_of_day` run MUST NOT mark sessions as processed. `/start_of_day` reads this field (in addition to the existing insights-file check) as a second signal for the missing-EOD banner — if any session file written within the last 36 hours has `end_of_day_due: yes`, the banner fires.

**Orphaned sessions** (flag=no, never rolled up): If a session-state file already has `end_of_day_due: no` AND was never included in a daily-cache body (because `/end_of_day` ran after it was written and used the old `<today>-*.md` glob), that session is silently skipped by the normal hybrid rule — the existing `no` flag is treated as authoritative. Use `/end_of_day --recover-orphans` to surface these sessions. The subcommand partitions orphans into two groups by file date: RECENT (within last 7 days) and HISTORICAL (older). The user confirms each group separately. Orphan detection uses a word-boundary-aware slug match: a session is an orphan iff its task-name slug (post-date portion of filename) is absent from the body of every daily-cache file, using `r"(?<![\w-])" + re.escape(slug) + r"(?![\w-])"` — hyphens count as part of the slug token so prefix collisions like `json-discovery-map` vs `json-discovery-map-review` do not produce false-positive coverage. Alternatively, manually flip any orphaned session's `end_of_day_due` field back to `yes` and re-run `/end_of_day`.

The `fallback_fires` field counts Class B writer Step 5 English-fallback invocations and Step 2 Haiku dispatch retries during this session. The active skill increments the field in place (atomic-rename pattern, mirror of end_of_day_due flip) immediately before emitting the `format-kit-skipped` warning. Default value is `0` at every session-state write; never decremented. KNOWN ISSUE: under parallel subagent fallback fires (rare; <1/day in practice given pre-Stage-4 finalized-artifact data), the read-modify-write update can undercount — it never overcounts. For low-frequency telemetry visibility this undercounting is acceptable; if a future post-merge measurement shows >5% undercounting, escalate to a per-skill append-only counter file (per Stage 4 D-03-rev2 option 3 — currently deferred).
