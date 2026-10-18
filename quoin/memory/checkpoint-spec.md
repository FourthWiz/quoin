---
title: Checkpoint subsystem behavior specification
status: characterization (not a redesign)
date: 2026-07-14
task: checkpoint-spec-harness (stage 1)
tier: 1 (hand-edited, always English)
---

# Checkpoint subsystem — behavior specification

This document characterizes the **existing, as-deployed behavior** of the `/checkpoint`
subsystem as of 2026-07-14. It is not a proposal or a redesign — every normative statement
below either carries an inline `[src: path:line]` citation to source actually opened and
read while writing this document, or is explicitly marked `[unverified]` /
`[needs Linear lookup]` where no such citation could be established. This file is the
contract a later implementation stage (the test harness for the checkpoint subsystem) will
assert against. Line numbers refer to the files as they existed at the time of writing;
re-verify against source before treating a specific line number as durable.

**Citation convention:** every factual/normative claim in this document uses the inline form
`[src: <repo-relative-path>:<line>]`. Where a claim covers a range, the citation gives the
first line of the relevant block.

## Authority note — deployed adapter vs. legacy stub

Two `SKILL.md` files exist for `checkpoint`:

- `quoin/adapters/claude/skills/checkpoint/SKILL.md` (1201 lines) — this is the file
  `bash quoin/install.sh` actually deploys to `~/.claude/skills/checkpoint/SKILL.md`, per its
  own header comment `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:9]` ("Portable
  intent doc: `quoin/core/skills/checkpoint.md`") and the legacy stub's own admission (below).
  **This is the authoritative source for every behavioral claim in this document.**
- `quoin/skills/checkpoint/SKILL.md` (19 lines) — an explicit deprecation stub. Its body reads:
  "DEPRECATED LOCATION. The active Claude adapter SKILL.md for this skill lives at
  `quoin/adapters/claude/skills/checkpoint/SKILL.md`... Do NOT add behavior here."
  `[src: quoin/skills/checkpoint/SKILL.md:9]`. The stub retains only the frontmatter
  (`name`, `description`, `model: sonnet`) so glob-based tests/manifest parsers that expect a
  `SKILL.md` at every `quoin/skills/<name>/` path still find a valid file
  `[src: quoin/skills/checkpoint/SKILL.md:17]`. It carries zero behavioral content — there is
  no discrepancy to reconcile because the stub does not describe any behavior at all.

All remaining citations to "SKILL.md" in this document refer to the adapter path
(`quoin/adapters/claude/skills/checkpoint/SKILL.md`) unless stated otherwise.

---

## Sentinel families

`_lib.sh`'s `sentinel_globs()` function is the single source of truth for the sentinel
family list: "SINGLE SOURCE OF TRUTH for the 9 sentinel families. Consumed by
sessionstart.sh STEP 2. cleanup/SKILL.md and sleep/SKILL.md document the same list and MUST
stay byte-identical (drift test: test_sentinel_family_parity.py)"
`[src: quoin/hooks/_lib.sh:16]`. The function emits exactly **9** globs
`[src: quoin/hooks/_lib.sh:22-33]` — the task brief's assumption of 9 is correct as written
in source; this was independently verified via `grep -c` against the file rather than
assumed.

| # | Glob (byte-identical) | Writer | Reader / consumer | Deleter | Window / lifetime |
|---|---|---|---|---|---|
| 1 | `pending-restore-*.txt` | `/checkpoint` save mode, Step 3 (restore sub-mode only) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:511-518]`; also written by `precompact.sh`, but only on its no-run/no-pidfile row and only under `QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1` (default 0 — since `IVG-258 S-3` a plain conversation writes no sentinel; an active run or skill pidfiles never write this family) `[src: quoin/hooks/precompact.sh:510]`; also by `thorough_plan_checkpoint.py::_write_sentinel` `[src: quoin/core/scripts/thorough_plan_checkpoint.py:190]`. `userpromptsubmit.sh` is not a writer of this family (it no longer reads the transcript at all), so `sessionstart.sh` STEP 3a/4 shows no restore banner for a high-context prompt; voluntary `/checkpoint` remains a seeding path; since `IVG-258 S-3`, `precompact.sh` seeds it only under `QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1`. | `/checkpoint --restore` picker Tier 1 fast path and Tier 3 full enumeration `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:706, 806]`; `sessionstart.sh` STEP 3a/4 banner `[src: quoin/hooks/sessionstart.sh:160-183]` | `/checkpoint --restore` Step 5 on successful restore `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1155-1185]`; age-based sweep in `/cleanup` / `sessionstart.sh` STEP 2 `[src: quoin/hooks/sessionstart.sh:120-148]` | Picker enumeration window `QUOIN_RESTORE_SENTINEL_WINDOW` (default 7d) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:713]`; cleanup sweep `QUOIN_CLEANUP_SENTINEL_WINDOW` (default 1d) `[src: quoin/hooks/_lib.sh (referenced value) / quoin/adapters/claude/skills/checkpoint/SKILL.md:313]` |
| 2 | `pending-prompt-*.txt` | No current writer: `userpromptsubmit.sh` no longer appends blocked prompts. Only files left by older installs exist | `/checkpoint --restore` Step 4 (CASE A/B/C) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1096-1153]`; also Tier 2 cross-reference in the picker `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:710-753]`. With no writer, new sessions produce no Tier 2 anchor and the picker uses Tier 3 full enumeration; the family is read only to handle leftovers. | `/checkpoint --restore` Step 5 (trash-move on CASE B `y`/`n`, or CASE C `delete`) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1155-1159]`; age-based sweep | Tier-2 enumeration window `QUOIN_RESTORE_SENTINEL_WINDOW` (default 7d), same knob as row 1 `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:713]` |
| 3 | `compact-happened-*.txt` | `postcompact.sh` `[src: quoin/hooks/postcompact.sh:43-45]` | `/checkpoint` save-mode Step 1.4 two-arm skip check (dual-sentinel Arm A, or a project-scoped `run_state_probe` no-active-run check when `pending-restore-*` is absent — Arm B) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:269-321]`; restore-mode Step 1.5 same-session detection `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1015-1023]`. Arm B's consumer reads a project-wide question: it asks whether anything is active anywhere in the project, not scoped to this session. | Not trash-moved by name by any hook; it is read by `/checkpoint` Step 1.4 and left for the sweep. As one of the 9 families it is still in scope for the generic age-based `/cleanup` / `sessionstart.sh` sweep `[src: quoin/hooks/sessionstart.sh:120-148]` | Age-based sweep only: `SESSIONSTART_SWEEP_DAYS` (default 1d, when session_id known) or `STALE_DAYS`/`QUOIN_STALE_SENTINEL_DAYS` (default 7d, fallback) `[src: quoin/hooks/_lib.sh:57-58]` |
| 4 | `mid-agent-handoff-*.txt` | `/checkpoint` save mode Step 4c `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:588-609]` | `sessionstart.sh` STEP 3c banner `[src: quoin/hooks/sessionstart.sh:171-172, 199-204]` | No explicit read-then-delete found; relies on the age-based `sentinel_globs()` sweep in `sessionstart.sh` STEP 2 `[src: quoin/hooks/sessionstart.sh:120-148]` | Same age-based sweep as row 3 |
| 5 | `pending-resume-ref-*.txt` | `/checkpoint` save mode Step 4b (load-as-reference sub-mode) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:555-577]` | `sessionstart.sh` STEP 3b banner `[src: quoin/hooks/sessionstart.sh:165-168, 193-198]` | No explicit read-then-delete found; relies on the age-based sweep, same as row 3-4 | Same age-based sweep |
| 6 | `checkpoint-defer-*.txt` | `/checkpoint --defer` mode `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:654-682]` | None. The prompt hook that used to read it for the advisory band is gone, so the marker is a no-op | `/checkpoint` save mode Step 6, on successful voluntary save `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:646-651]`; age-based sweep | Expires on next voluntary save or by age |
| 7 | `postcompact-reset-*.txt` | No current writer: `postcompact.sh` stopped writing it | None: the prompt hook that consumed it is gone | Age-based sweep only (the family stays in the sweep lists so files left by older installs are cleaned) | Short-lived leftovers only |
| 8 | `checkpoint-pending-compact-*.txt` | **No writer found** in any file read for this spec. The only reference is `/checkpoint` Step 0.5, which merely trash-moves this marker **if it exists** as legacy cleanup: "Trash-move any stale `checkpoint-pending-compact-${session_id}.txt` marker for cleanup (if it exists)" `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:189-194]`. This is consistent with the fact that the `--after-compact` flag it was presumably paired with is itself marked deprecated in the same file `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:179, 187-188]`. `[unverified: possible dead/legacy family — no current writer located]` | Same Step 0.5 stale-cleanup reference (reader = deleter in this case) | Same Step 0.5 | One-shot cleanup on next `/checkpoint` save invocation, if a stale file happens to exist |
| 9 | `idle-advisory-pending-*.txt` | No current writer: the idle check left `userpromptsubmit.sh` | None | Older installs' prompt hook deleted (`rm -f`) the file on the next prompt of the same session; now only the age-based `/cleanup` / `sessionstart.sh` sweep reclaims leftovers `[src: quoin/hooks/_lib.sh:32]` | Leftovers only |

**Which two families the restore picker actually reads directly by name (not via the
generic sentinel sweep):** `pending-restore-*.txt` and `pending-prompt-*.txt`
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:706-753 (pending-restore), 1096-1153 (pending-prompt)]`.
The other seven families are consumed either by `sessionstart.sh` banners (rows 3-5) or by
hooks other than the restore picker itself (rows 6-9).

**Discrepancy noted:** `/checkpoint`'s own Step 1.47 auto-cleanup text says "Sentinel sweep:
for each of the **8 families**..." `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:313]`,
but both `_lib.sh` (`sentinel_globs()`, 9 entries) `[src: quoin/hooks/_lib.sh:22-33]` and the
`/cleanup` and `/sleep` SKILL.md files explicitly say "9 families"
`[src: quoin/adapters/claude/skills/cleanup/SKILL.md:212, 230, 234, 249]`
`[src: quoin/adapters/claude/skills/sleep/SKILL.md:359, 370]`. This is a live inconsistency in
the deployed checkpoint SKILL.md's own prose (it undercounts by one) — flagged here rather
than silently "corrected," per the cite-or-omit discipline.

---

## Picker tiers

The restore picker in `/checkpoint --restore` Step 1.0 is organized into four literal
`**Tier N —**` headers, in this exact order, found via direct grep of the SKILL.md
(no invented naming):

1. **Tier 1 — Fast path (current-session sentinel, fast validation)**
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:706]`. Checks
   `pending-restore-${current_session_id}.txt` for the session that is running `--restore`
   right now. Applies a **cross-task identity guard only** (compares the candidate's
   `## Active task` against the freshest `sessions/*.md` filename-derived task) — it does
   **not** apply the staleness guard. The doc is explicit: "The staleness guard is NOT
   applied here; a same-task sentinel that is several days old passes unconditionally"
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:708]`. On cross-task mismatch it
   routes to Tier 4 (B3 synthesis) rather than silently returning.
2. **Tier 2 — Pending-prompt cross-reference (fix #5)**
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:710]`. Fires when Tier 1 misses
   (fresh session with no current-session `pending-restore`). Enumerates all in-window
   `pending-prompt-<SID>.txt` files (window: `QUOIN_RESTORE_SENTINEL_WINDOW`, default 7d
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:713]`), and for each SID checks
   whether a matching `pending-restore-<SID>.txt` also exists — if so, that pairing is the
   strongest possible anchor and is used directly. If no `pending-restore` exists for that
   SID, the loop still extracts a task name from the associated session-state file to seed
   `_anchor_task`, which Tier 3's freshest-task comparison will use.
3. **Tier 3 — Full enumeration with combined gate**
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:753]`. Standard candidate-list
   build from both `pending-restore-*.txt` sentinels ("B1", see below) and a 30-day
   `checkpoints/*.md` disk scan, followed by the "Combined auto-pick gate" (D-03) that
   applies **both** the cross-task guard and the staleness guard
   (`QUOIN_RESTORE_STALE_DAYS`, default 1d) with OR semantics before silently auto-picking
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:879-919]`.
4. **Tier 4 — B3 session-state synthesis**
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:755]`. Fires when Tier 3's
   auto-pick is suppressed by the combined gate, or when `candidate_count == 0`. Synthesizes
   a minimal restore prompt directly from the freshest `sessions/*.md` file rather than from
   any checkpoint file (see the "B3 synthesized template" section below).

> **Addendum (2026-08-01, IVG-160) — distinct-task ambiguity sub-step inside Tier 3.** Tier 3
> now runs a NON-NUMBERED distinct-task ambiguity gate *before* the B3 Clause-A/B trigger and the
> combined auto-pick gate (it is a sub-step of Tier 3, deliberately NOT a new numbered tier — the
> four-tier structure above is unchanged). When ≥ 2 **distinct** `## Active task` values each have
> a candidate whose mtime is within `QUOIN_RESTORE_AMBIGUITY_WINDOW` (**seconds**, default 14400 =
> 4h; `<= 0` disables) of `now`, `select_restore` returns a new `tier == "ambiguous"` Verdict
> carrying a `candidates` list (mtime-descending) instead of silently auto-picking, and SKILL.md
> renders an interactive picker (module stays read-only — the module decides, the SKILL renders).
> Same-task multiplicity collapses to one distinct task and does NOT trigger the picker. Because
> the ~4h window is narrower than `QUOIN_RESTORE_STALE_DAYS` (default 1 day), a days-old cross-task
> checkpoint can never enter the recent set, so the IVG-139 stale-suppression guarantee is
> preserved: when the gate does not fire, control falls into the byte-for-byte-unchanged Clause-B
> and combined stale/cross-task gate. `[src: quoin/core/scripts/checkpoint_picker.py `finalize_ambiguity`;
> quoin/adapters/claude/skills/checkpoint/SKILL.md Step 1.0b + Fallback "Distinct-task ambiguity gate"]`

### B-labels vs. Tier-N structure

The SKILL.md separately uses `B1`, `B2`, `B3` labels in its prose. These are **not** a 1:1
mapping onto the four Tier-N headers above — they name specific sub-behaviors/fixes, not
picker stages:

- **B1** — refers specifically to the mtime-filtered sentinel-candidate-collection step
  inside Tier 3's full enumeration: "All sentinels (**B1** — mtime-filtered)... After
  applying the **B1** mtime filter, check the B3 session-state fallback trigger"
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:806, 854]`. B1 is a sub-step of
  Tier 3, not a tier itself.
- **B2** — refers to the "two-call pattern" sentinel-cleanup fix in restore Step 5: cleaning
  up the *actually-consumed* orphan sentinel (as opposed to only the current-session
  sentinel): "Cleanup logic (**B2** — two-call pattern)"; "consumed_sentinel_path — if
  non-empty AND different from the current-session sentinel (**B2** fix: cleans the
  actually-consumed orphan sentinel from a prior session)"
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1160, 1167]`. B2 belongs to
  Step 5 of the overall restore flow, downstream of all four tiers.
- **B3** — refers to the session-state-fallback synthesis behavior, which is *also* what
  Tier 4 is named after ("Tier 4 — **B3** session-state synthesis"). Here the B-label and the
  Tier-N label do coincide: B3 and Tier 4 name the same mechanism
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:755, 923]`. B3 is triggered either
  by the Tier-3 combined-gate suppression, or by the two-clause OR trigger ("Clause A":
  zero candidates; "Clause B": all candidates older than the freshest session-state file)
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:854-860]`.
- **B4** — grepped for explicitly across the full 1200-line file: **no `B4` label appears
  anywhere in the current SKILL.md** `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md,
  full-file grep, 2026-07-14]`. The task brief's caution that a prior draft fabricated a "B4"
  clause is confirmed: there is nothing in current source to attach a B4 citation to, so no
  B4 behavior is described in this document. Notably, Linear issue **IVG-30** is titled "Fix
  checkpoint restore picker staleness (**B1/B2/B3/B4**)" and its description says "Fixed four
  staleness bugs in the restore picker" — so a B4 label existed at some point in this issue's
  scope, but it does not survive in the current SKILL.md prose (either resolved into one of
  B1-B3, or renamed/consolidated by a later revision). This is an observation about the
  Linear history, not a source-code citation — do not read it as evidence of current B4
  behavior.

**Net relationship:** the four Tier-N headers describe the *sequential* picker resolution
order (fast path → pending-prompt cross-reference → full enumeration → synthesis fallback).
The B-labels are historical/implementation-fix identifiers layered onto specific sub-steps
within that sequence (B1 inside Tier 3's candidate collection, B2 inside the downstream
cleanup step, B3 coinciding with Tier 4). They are cross-cutting annotations, not a parallel
numbering of the same four stages.

---

## Checkpoints writers

Two independent scripts/skills write into `.workflow_artifacts/memory/checkpoints/`:

1. **`/checkpoint` itself** — writes files named
   `<YYYY-MM-DD>T<HHMM>-<task-name>.md` (voluntary save)
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:446-448]`, or
   `<YYYY-MM-DD>-<active_task>-precompact.md` (precompact hook save)
   `[src: quoin/hooks/precompact.sh:354]`, or a panic-mode skeleton using the same
   timestamped shape as the voluntary form
   `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:269]`.
2. **`thorough_plan_checkpoint.py`** — writes a single fixed filename per session:
   `thorough-plan-progress-{sid}.md` (D-03: "one file per session, overwritten at each
   boundary") `[src: quoin/core/scripts/thorough_plan_checkpoint.py:128-131]`. Unlike
   `/checkpoint`'s timestamped files, this file is **overwritten in place** on every
   plan/critic/revise phase boundary, not appended as a new file.

**How a consumer distinguishes one kind of file from the other:**

- **Filename prefix** is the primary discriminant: `thorough-plan-progress-{sid}.md` is a
  distinctive, grep-able prefix `[src: quoin/core/scripts/thorough_plan_checkpoint.py:131]`
  versus `/checkpoint`'s `<date>[...]-<task-name>[...].md` shapes, which have no fixed
  prefix token.
- **Schema markers**: `thorough_plan_checkpoint.py`'s files use `## Last user intent`,
  `## Restore hint`, and a `## Session link` with resume text pointing at
  `get_session_uuid.py` rather than a literal `claude --resume` command
  `[src: quoin/core/scripts/thorough_plan_checkpoint.py:149-171]`. `/checkpoint`'s own files
  use the same top-level section names (`## Status`, `## Current stage`, `## Active task`,
  `## Session ID`, `## Last user intent`, `## In-flight artifacts`, `## Open questions`,
  `## Decisions made`, `## Unfinished work`, `## Restore hint`)
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:456-498]` — the section *names*
  overlap heavily (both have `## Last user intent` and `## Restore hint`), so schema markers
  alone are **not** a reliable discriminant; the filename prefix is the only unambiguous one.
  `thorough_plan_checkpoint.py`'s `## Current stage` value additionally has a distinctive
  format, `thorough-plan:round-{N}-{phase}` `[src: quoin/core/scripts/thorough_plan_checkpoint.py:249]`,
  which is itself documented in `CLAUDE.md` as "a recognized `## Current stage` value" for
  `/start_of_day`/`/end_of_day`/`/status` — this stage-token format is a secondary, reliable
  discriminant when present.

**Shared vs. separate selection pool:** the source is **ambiguous** on this point and this
document states that explicitly rather than resolving it by assertion. The `/checkpoint
--restore` picker's Tier 3 full-enumeration step scans `checkpoints/*.md` with a generic
glob (`find .../checkpoints/ -maxdepth 1 -name '*.md' ! -name '*.tmp' -mtime -30 ...`)
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:823]` — this glob would also match a
`thorough-plan-progress-{sid}.md` file sitting in the same directory, since nothing in that
`find` excludes the `thorough-plan-progress-` prefix. Whether such a file would actually
survive the picker's downstream parsing (e.g., `## Active task` extraction via
`awk '/^## Active task[[:space:]]*$/{getline;...}'` `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:892]`)
was not verified by executing the code — the `thorough_plan_checkpoint.py` template does NOT
emit an `## Active task` heading of that exact form (it uses `## Active task\n{task}\n\n`
which does match the pattern structurally, so it likely *would* parse)
`[src: quoin/core/scripts/thorough_plan_checkpoint.py:152]`. No test file found in
`quoin/dev/tests/` that exercises this specific cross-writer collision scenario end-to-end
(see `test_thorough_plan_checkpoint_roundtrip.py` and
`test_thorough_plan_phase_checkpoint_present.py` below, which test each writer in isolation).
**Conclusion: this document does not assert whether the two writers share one pool or are
logically separate — source and tests do not settle it.**

**Same-session-id collision (`pending-restore-{sid}.txt` written by both):** both writers
can, in principle, write a `pending-restore-{sid}.txt` sentinel for the same session id —
`/checkpoint`'s Step 3 `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:511-518]` and
`thorough_plan_checkpoint.py`'s `_write_sentinel` `[src: quoin/core/scripts/thorough_plan_checkpoint.py:177-191]`.
Both write via an atomic tmp+rename pattern (`_atomic_write`
`[src: quoin/core/scripts/thorough_plan_checkpoint.py:57-61]`; `/checkpoint`'s writes are
plain `>` redirects rather than tmp+rename for this particular file
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:515-518]` — note this is an
asymmetry between the two writers' own robustness, not something this doc resolves further).
Neither writer reads the other's sentinel before writing, and neither file has any embedded
generation/version counter — so **last-writer-by-mtime wins** is the only behavior
observable from source (a plain overwrite of the same path), and this document states that
as the plausible-and-verified answer: whichever of the two last executed its sentinel-write
step has its checkpoint path present in `pending-restore-{sid}.txt` at picker-read time.
This was verified by reading both write sites (both do an unconditional single-line
overwrite of the identical path with no CAS/lock), not merely assumed.

---

## Session-hash and SID derivation

**`project_hash()` regex, quoted verbatim:**
```python
re.sub(r'[^A-Za-z0-9-]', '-', project_path)
```
`[src: quoin/core/scripts/get_session_uuid.py:71]`. The function's own docstring states the
empirical rule this implements: "replace ANY character that is NOT `[A-Za-z0-9-]` with `-`.
This covers `/` -> `-`, `.` -> `-`, `@` -> `-`, `_` -> `-`, ` ` -> `-`, etc."
`[src: quoin/core/scripts/get_session_uuid.py:62-63]`, and explicitly flags that
`CLAUDE.md`'s older description ("project path with / replaced by -") is "a simplification"
`[src: quoin/core/scripts/get_session_uuid.py:66-67]`.

**SID-selection algorithm** (`get_session_uuid()`), in precedence order:
1. The `CLAUDE_CODE_SESSION_ID` environment variable, when it is UUID-shaped
   (`_UUID_RE` + `_env_session_uuid()`), is returned as-is and without checking that a transcript
   exists `[src: quoin/core/scripts/get_session_uuid.py:111-113]`. The harness exports it to Bash
   tool calls and it names the running session exactly, which a newest-file guess cannot do while
   a concurrent session in the same project is writing. A malformed value is ignored (stderr note).
2. Otherwise resolve `~/.claude/projects/<project_hash(project_path)>/`, glob `*.jsonl`, filter out
   Google-Drive conflict copies matching `r" \d{1,3}(\.[^ ]*)?$"` (e.g. `"UUID 2.jsonl"`)
   `[src: quoin/core/scripts/get_session_uuid.py:120]`, then sort the remainder by mtime
   descending and return the stem of the newest file `[src: quoin/core/scripts/get_session_uuid.py:121-128]`.
3. On any exception, or if no files match, fall back to a synthetic
   `unknown-<phase_slug>-<YYYYMMDDTHHMMSSZ>` UUID, where `phase_slug` replaces `-` with `_`
   `[src: quoin/core/scripts/get_session_uuid.py:78-87]`.

`session_age_guard.py` and `context_budget_guard.py` resolve the current transcript with the same
env-first order (explicit `--current-uuid`/`--transcript` still win), looking for `<uuid>.jsonl` in
the project dir and then in any single `~/.claude/projects/*/` dir; if the env session's transcript
is found nowhere they fail OPEN rather than measure a different session.

**`--print-hash` CLI contract:** `python3 get_session_uuid.py --print-hash [--project-path
PATH]` resolves `PATH` (or cwd if absent) and prints `project_hash(PATH)` to stdout, exit 0,
**without** performing the JSONL lookup `[src: quoin/core/scripts/get_session_uuid.py:161-185]`.

**Shell-parity rule:** any shell script that needs the project hash MUST call
`get_session_uuid.py --print-hash --project-path "$cwd"` rather than reimplementing the
regex with `sed`/`tr`. The concrete anti-pattern to avoid is in `sessionend.sh`'s Close
snapshot block:
```sh
proj_hash=$(printf '%s' "$cwd" | sed 's|/|-|g')
```
`[src: quoin/hooks/sessionend.sh:174]`. This `sed` substitution only replaces the `/`
character — it is a **strictly narrower transformation** than the Python regex
(`re.sub(r'[^A-Za-z0-9-]', '-', ...)`), which also replaces `.`, `@`, `_`, spaces, and any
other non-`[A-Za-z0-9-]` character. For any project path containing a `.`, `@`, or space
(the example in `get_session_uuid.py`'s own docstring is a Google-Drive path containing both
`.` and `@` and spaces `[src: quoin/core/scripts/get_session_uuid.py:64-65]`), `sessionend.sh`'s
`sed`-derived hash will **not** match the real on-disk `~/.claude/projects/<hash>/` directory
name, and the subsequent `jsonl_dir="$HOME/.claude/projects/$proj_hash"` lookup
`[src: quoin/hooks/sessionend.sh:175]` will silently fail (`[ -d "$jsonl_dir" ] || exit 0`
`[src: quoin/hooks/sessionend.sh:176]` — fails open, no error surfaced). This is cited here
explicitly as the anti-example the task brief asked for — it is **not** treated as correct
behavior, and any future harness should assert that `sessionend.sh`'s Close-snapshot block
either calls the canonical script or is fixed to match its output.

---

## Task-context matching

The **single decision-time authority** at restore/auto-pick time is the candidate
checkpoint's own `## Active task` heading content, extracted via this awk one-liner
(appearing at multiple call sites with identical logic):
```sh
awk '/^## Active task[[:space:]]*$/{getline; gsub(/\r$/,""); print; exit}' "$cand_cp_file"
```
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:892]` (identical pattern also at
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:776]` for the Tier-1 fast path). This
value is compared against a `freshest_task` baseline whose derivation differs by tier:

- **Tier-3 combined auto-pick gate** (the numbered-picker / single-candidate path): the
  baseline is `freshest_task="${_anchor_task}"` FIRST — the Tier-2 pending-prompt
  cross-reference's seeded task, when Tier-2 found an in-window anchor — and only falls
  back to the freshest-`sessions/*.md` filename-derived task (`YYYY-MM-DD-` prefix
  stripped) when `_anchor_task` is empty
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:884-890]`. This anchor-first
  precedence is exactly the fix for the 2026-05-30 incident: a fresh pending-prompt
  cross-reference found today must out-rank a stale cross-task checkpoint that happens to
  be the sole Tier-3 candidate.
- **Tier-1 fast path**: uses the freshest-`sessions/*.md` filename-derived task directly,
  with no `_anchor_task` fallback, because the Tier-2 loop that assigns `_anchor_task` has
  not yet run at this execution position
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:779-782]`.

A mismatch against the applicable baseline suppresses silent auto-pick and routes to B3
synthesis.

By contrast, `verify_claims.py`'s `check_side_effects()` for `skill == "checkpoint"` computes
its own `task_backstop` by comparing `filename_task(cp_path.name)` (parsed from the
checkpoint's *filename*, not its `## Active task` body) against the freshest session-state
file's filename-derived task, and if they differ appends `f"task_backstop:{ckpt_task}!=
{freshest_task}"` to a `missing` list `[src: quoin/core/scripts/verify_claims.py:412-422]`.
This function only ever returns `{"ok": bool, "missing": [...]}` — it is invoked as an
independent, after-the-fact auditor and has no code path back into the picker's own
decision-making.

**One-sentence asymmetry statement:** the picker's own `## Active task`-body comparison is
what actually *decides* whether a checkpoint is auto-picked or routed to synthesis at
restore time, whereas `verify_claims.py`'s filename-derived `task_backstop` is a
purely advisory, after-the-fact signal that can flag a mismatch for audit purposes but never
itself decides or blocks a restore. As of S-4 (IVG-139), this advisory is now SURFACED at
session start: `sessionstart.sh`'s pending-restore banner (STEP 5) invokes this same
`check_side_effects(skill="checkpoint")` predicate and appends a `[quoin-IVG-139 WARN:
task-context mismatch …]` suffix when `task_backstop` fires — WARN-not-block, it never
alters the restore recommendation, and the picker's `## Active task`-body comparison
remains the sole decision authority. The predicate is deliberately SKIPPED (not just
non-warning) for `thorough-plan-progress-*` checkpoints, since that sentinel shape is
shared with `/thorough_plan`'s own resume mechanism and is not a `/checkpoint`
task-mismatch signal (critic round-1 M-1)
`[src: quoin/hooks/sessionstart.sh (restore ground-truth backstop, IVG-139)]`.

---

## Day-window knobs disambiguation table

Every distinct day/window-count env var found by grepping `_lib.sh` and the checkpoint/
cleanup SKILL.md files (7 found — not assumed to be any particular count in advance):

| Env var | Default | Defined at | Consumed at | Purpose |
|---|---|---|---|---|
| `QUOIN_STALE_SENTINEL_DAYS` (→ `STALE_DAYS`) | **7** | `[src: quoin/hooks/_lib.sh:52]` | `sessionstart.sh` STEP 2 sweep, fallback branch when `session_id` is empty `[src: quoin/hooks/sessionstart.sh:134]` | Conservative age-only sentinel sweep window when no session anchor is available |
| `QUOIN_SESSIONSTART_SWEEP_DAYS` (→ `SESSIONSTART_SWEEP_DAYS`) | **1** | `[src: quoin/hooks/_lib.sh:53]` | `sessionstart.sh` STEP 2 sweep, primary branch when `session_id` is known `[src: quoin/hooks/sessionstart.sh:132]` | Tight, UUID-aware sentinel sweep window at session start |
| `QUOIN_DISCOVERY_STALE_DAYS` | **7** | `[src: quoin/hooks/_lib.sh:56]` | `discovery_staleness.py` (invoked from `sessionstart.sh` S-5 block) `[src: quoin/hooks/sessionstart.sh:101-103]` | Discovery-memory staleness banner threshold — unrelated to checkpoint restore, included because it lives in the same `_lib.sh:read_constants()` |
| `QUOIN_SERENA_STALE_DAYS` | **30** | `[src: quoin/hooks/_lib.sh:57]` | `discovery_staleness.py` (same S-5 call site) | Serena-memory staleness — same caveat as above |
| `QUOIN_RESTORE_SENTINEL_WINDOW` | **7** | `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:713, 806]` (not in `_lib.sh` — defined inline in the skill body) | Restore-picker Tier 2 (pending-prompt cross-reference) and Tier 3 B1 sentinel enumeration | "Narrows the long tail of orphaned sentinels; asymmetric with checkpoint-enum's 30d is INTENTIONAL: sentinels are transient pointers, not durable artifacts" `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:806]` |
| `QUOIN_RESTORE_STALE_DAYS` | **1** | `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:901]` (inline in skill body, not `_lib.sh`) | Combined auto-pick gate (Tier 3), staleness clause | **This is the knob the task brief specifically warned about**: a prior draft of this spec wrongly assumed a default of 7. The verified default, read directly from the SKILL.md line, is **1**, not 7. |
| `QUOIN_SESSION_FALLBACK_WINDOW` | **7** | `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:856, 927]` (inline in skill body) | B3 trigger Clause B, and B3 fallback's own session-state enumeration window | Determines how far back a "freshest session-state file" can be for the B3 fallback / Clause-B same-day-symptom check to consider it |
| `QUOIN_RESTORE_AMBIGUITY_WINDOW` | **14400** | `[src: quoin/core/scripts/checkpoint_picker.py `_DEFAULT_RESTORE_AMBIGUITY_WINDOW`; SKILL.md Step 1.0b + Fallback "Distinct-task ambiguity gate" (IVG-160)]` | Tier-3 distinct-task ambiguity sub-step (`select_restore`, after the Clause-A empty-check and before Clause B) | **Unit is SECONDS, not days** (the seconds/days asymmetry is load-bearing — this knob is compared directly as `(now - mtime) <= window`, NOT via the day-based `_in_window`/`-mtime` machinery every other row above uses). Window during which ≥ 2 distinct-task candidates trigger the interactive ambiguity picker instead of a silent auto-pick. Default 14400 = 4h. **`<= 0` disables the gate** (legacy opt-out). |
| `QUOIN_PICKER_DEDUP_WINDOW` | **7d** | `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:848]` (inline; note the default is written as `7d` with a literal `d` suffix in this one call site, unlike the bare-integer form used elsewhere) | Picker candidate de-duplication (precompact vs. voluntary same-task pairing) | Pairs older than this are treated as independent entries rather than deduplicated |
| `QUOIN_CLEANUP_SENTINEL_WINDOW` | **1** (documented as "1d") | `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:313]`, also documented in `quoin/adapters/claude/skills/cleanup/SKILL.md:3, 168` | `/checkpoint` Step 1.47 auto-cleanup sentinel sweep, and standalone `/cleanup` | Age threshold for trashing sentinel files during auto-cleanup |
| `QUOIN_CLEANUP_CKPT_WINDOW` | **30** | `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:314]`, also `quoin/adapters/claude/skills/cleanup/SKILL.md:3, 175` | Same cleanup sweep, checkpoint-file branch | Age threshold for trashing old `checkpoints/*.md` files |

**Count found: 11 distinct env vars** across these two areas of concern (4 sourced from
`_lib.sh:read_constants()`, 7 defined inline in the checkpoint/cleanup SKILL.md bodies — including
`QUOIN_RESTORE_AMBIGUITY_WINDOW`, the sole SECONDS-unit knob, added for IVG-160) — not
5 as a naive reading of the task brief's example list might suggest, and not exactly the
brief's suggested candidate set either (`QUOIN_DISCOVERY_STALE_DAYS`/`QUOIN_SERENA_STALE_DAYS`
are present in `_lib.sh` but are not restore/cleanup knobs at all — included above for
completeness since the brief asked to check "every distinct day/window-count env var across
`_lib.sh` and the deployed SKILL.md," and they satisfy that literal instruction).

---

## Same-session detection

`/checkpoint --restore` Step 1.5 compares the SID embedded in the resolved checkpoint's
`## Session ID` heading against the current session's own SID:
```sh
if [ -n "$ckpt_sid" ] && [ "$ckpt_sid" != "unknown" ] \
   && [ -n "$current_session_id" ] && [ "$current_session_id" != "unknown" ] \
   && [ "$ckpt_sid" = "$current_session_id" ]; then
  _SAME_SESSION=true
else
  _SAME_SESSION=false
fi
```
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1037-1045]`. This comparison runs
**after** the picker has resolved a checkpoint path (any tier) and **before** Step 2 surfaces
it to the user `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1001-1004]`. It is
short-circuited (skipped entirely, fail-OPEN) if a `compact-happened-${current_session_id}.txt`
sentinel exists, on the stated rationale that a compaction already cleared the context
window, making a same-session restore safe `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1011-1023]`.
If `_SAME_SESSION=true`, the flow surfaces an `AskUserQuestion` prompt offering to proceed
anyway or to show fresh-session instructions, rather than silently blocking
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:1047-1062]`.

---

## Compaction interactions

**Two-arm skip check (Step 1.4, in `/checkpoint` *save* mode — not automatic at
`sessionstart.sh`):** the task brief's phrasing ("at session start") is imprecise; this skip
check is inside `/checkpoint`'s own save-mode flow, evaluated only when the user (or a
hook) invokes `/checkpoint` without an explicit `--mode`, not automatically when a new
session starts. `compact-happened-${sid}.txt` must exist AND EITHER
`pending-restore-${sid}.txt` also exists (Arm A, unchanged from the original dual-sentinel
design) OR the `run_state_probe` helper is available and reports no active run-state
record anywhere in the project (Arm B, project-scoped — no `SESSION_ID` argument,
matching `/run`'s self-checkpoint probe) — verified directly from
source, not assumed:

> "Two-arm check: `_sentinel` must exist AND (`_pending` exists (Arm A) OR the helper is
> available and reports no active run project-wide (Arm B, no `SESSION_ID` argument —
> project-scoped, matching `T-03`'s calls, `D-27`))."
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:290-292]`

`compact-happened-*` alone with an active run (or an unavailable helper) still does NOT
skip — user wants a real save; falls through to Step 1.45. `pending-restore-*` alone (no
compact this session) still does NOT skip — falls through to Step 1.45. Since IVG-258 S-6,
Arm B means a sentinel-incomplete session can also skip, when no active run is in flight to
resume project-wide — the "neither sentinel alone triggers the skip" summary from the
original dual-sentinel design is no longer accurate and is retired by this row.

**High-utilization tiers (basis-points constants from `_lib.sh`):**
- `COMPACT_FIRST_BPS` (env: `QUOIN_COMPACT_FIRST_BPS`), default **9000** (= 90.00%)
  `[src: quoin/hooks/_lib.sh:54]` — "compress-first ordering" notice tier; at or above this,
  `/checkpoint` still runs its save immediately but appends a high-util notice to the Step 5
  report and skips Step 1.47 auto-cleanup ("high-util" reason token); the percentage comes from the real
  transcript usage reading, labeled `(real)`, with a `~… (estimated)` byte-estimate fallback
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:304, 337-347]`.
- `PANIC_BPS` (env: `QUOIN_PANIC_BPS`), default **10000** (= 100.00%)
  `[src: quoin/hooks/_lib.sh:55]` — at or above this, `/checkpoint` Step 1.45 skips all deep
  gathering (Step 1, mid-agent check, `AskUserQuestion`) and writes a minimal skeleton
  checkpoint + sentinel using only cheap bash calls, then stops
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:247-282]`.
- Tier ordering is explicit in source: `COMPACT_FIRST_BPS` (9000, notice-only) <
  `PANIC_BPS` (10000, degrade-to-minimal-save) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:249]`.

**`precompact.sh`/`postcompact.sh` sentinel roles:**
- `precompact.sh` fires on the `PreCompact` hook event, but only for **auto** triggers — a
  `manual` trigger (`/compact` typed by the user) passes through immediately with no sentinel
  activity `[src: quoin/hooks/precompact.sh:30-32]`. For auto triggers, if a
  `pending-restore-${session_id}.txt` already exists (a voluntary `/checkpoint` ran earlier
  this session), the hook **skips** writing its own `-precompact.md` checkpoint entirely, to
  avoid creating an orphaned duplicate `[src: quoin/hooks/precompact.sh:292-295]`. Otherwise the
  checkpoint write is gated on a three-row truth table (`IVG-258 S-3`), branched after the
  write `[src: quoin/hooks/precompact.sh:323]` (STEP 4 row branch: `precompact.sh:484-521`):

  | Row | Condition | `-precompact.md` checkpoint | `pending-restore-${session_id}.txt` |
  |---|---|---|---|
  | 1 | fresh active `run-state-*.json` record whose `session_id` matches this session | written | not written — the run resumes via its own record |
  | 2 | skill pidfiles present (no matching record) | written | not written — workflow must continue |
  | 3 | neither (plain conversation) | not written | not written; `QUOIN_PRECOMPACT_NORUN_CHECKPOINT=1` restores checkpoint + sentinel `[src: quoin/hooks/precompact.sh:508-513]` |

  The row-3 sentinel (knob on) points at the checkpoint the same invocation wrote
  `[src: quoin/hooks/precompact.sh:510]`; rows 1 and 2 never write this family. The write is
  additionally gated on the invocation itself having produced a checkpoint — the guard reads
  `[ "${PRECOMPACT_NORUN_CHECKPOINT:-0}" = "1" ] && [ -n "${checkpoint_file:-}" ]`
  `[src: quoin/hooks/precompact.sh:508]`; on the early-skip path (voluntary sentinel already
  present) `checkpoint_file` is never assigned, so this precondition alone prevents the
  early-skip path from re-writing an existing sentinel `[src: quoin/hooks/precompact.sh:497-498, 514-518]`.

  **Two steps precede the truth table above, and one of them runs unconditionally.** STEP
  1b's `recent-sessions.md` append `[src: quoin/hooks/precompact.sh:40-47]` runs on **every**
  invocation, including the `.allow-compact`-marker case (it precedes that check). STEP 1e's
  telemetry "pre" event, appended into `_tel_sink` via the `jq` call at
  `precompact.sh:269-278` `[src: quoin/hooks/precompact.sh:167-278]`, runs on every row of
  the truth table above — the sole exception is the `.allow-compact` marker
  (`precompact.sh:52-54`), which exits right after the STEP 1b append and before STEP 1e is
  ever reached, so that one case suppresses telemetry only (see the "Compaction telemetry
  sink" section of `hooks-table.md`). Both citations are to the code, not the header
  comment, which only asserts the guarantee in prose and can itself drift.
- `postcompact.sh` fires after compaction completes and has **no decision control** — it
  writes one side-effect marker unconditionally (given a valid `session_id`):
  `compact-happened-${session_id}.txt` (read by `/checkpoint` Step 1.4 and Step 1.5)
  `[src: quoin/hooks/postcompact.sh]`. It no longer writes `postcompact-reset-*`, whose
  only reader was the prompt hook.

---

## Save modes, tiers, and flags

Three distinct categories exist and should not be conflated (the SKILL.md itself does not
name them as three categories explicitly — this grouping is this document's own synthesis
from the underlying mechanics, each element individually cited):

**(1) `--mode` values accepted by the save path** (exactly **3**, all named at one call
site `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:178]`):
- `--mode restore` — writes a full checkpoint + `pending-restore-*.txt` sentinel (Step 3/4a).
- `--mode load-as-reference` — writes a full checkpoint + `pending-resume-ref-*.txt`
  sentinel, skips Step 3 (Step 4b).
- `--mode mid-agent` — skips the full checkpoint entirely, writes only a minimal
  `mid-agent-handoff-*.txt` sentinel (Step 4c).
If no `--mode` is given, Step 1.5's auto-detection sequence (high-util check, mid-agent
pidfile check, then user choice via `AskUserQuestion`) picks one of the same three values
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:324-388]`.

**(2) Utilization-driven behavioral tiers** (NOT user-chosen — computed from
`compute_utilization`, exactly **2** named tiers plus an implicit "normal" band below both):
- High-util notice tier, `>= COMPACT_FIRST_BPS` (9000/90%) — sets `_HIGH_UTIL_NOTICE=true`,
  appends context-percentage guidance to the Step 5 report, and skips auto-cleanup
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:333-347, 632-638]`.
- Panic tier, `>= PANIC_BPS` (10000/100%) — degrades to the minimal skeleton save described
  above and stops immediately `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:247-282]`.

**(3) Standalone flags independent of the above two categories:**
- `--no-cleanup` — suppresses Step 1.47 auto-cleanup regardless of utilization tier
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:180, 301-302]`.
- `--after-compact` — explicitly marked **deprecated**: "it now has no effect —
  compact-already-ran detection is automatic via the compact-happened sentinel"
  `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:179, 188]`. Retained only for
  backward compatibility.
- `--defer` — a wholly separate top-level invocation mode (see below), not a modifier of
  save mode.
- `--restore` — the top-level invocation-mode selector for "Restore mode" as a whole
  (distinct from the `--mode restore` *sub*-mode of Save mode — the shared word "restore" is
  a genuine naming collision in the source, not a documentation error introduced here:
  compare `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:152]` "Save mode (default —
  no `--restore` argument)" against `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:178]`
  "`--mode restore`").
- `--purge` — a destructive `/checkpoint` subcommand. The prompt hook used to carve it out
  of the exempt list so that a high-context advisory named the risk; that hook no longer
  reports context, so the carve-out is gone.

  `IVG-258 D-06`: the `/checkpoint --purge` carve-out from the exempt list is
  retained, but the refusal it used to trigger is retired — acceptance criterion 3
  forbids a block-shaped response on any path, so `--purge` now reaches the
  high-context advisory, whose text names the destructive-command risk
  (`IVG-258 R-15`). Relocating the guard into `/checkpoint`'s own `--purge` path is
  tracked as `IVG-258 Q-06` and is out of scope for stage 2.

  **No corresponding
  `--purge` mode implementation was found anywhere in the 1200-line adapter SKILL.md body**
  — the file describes save/restore/defer modes and the `--mode`/`--no-cleanup`/
  `--after-compact` flags, but no `--purge` handling. `[unverified: --purge appears to be
  reserved/aspirational in the hook's exemption logic with no matching skill-side
  implementation currently in source — flagged rather than invented.]`

**Top-level invocation-mode dispatch (orthogonal to the three `--mode` sub-values above):**
the skill first branches on the raw invocation into exactly 3 top-level modes — Save mode
(default, no `--restore`/`--defer`) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:152-154]`,
Defer mode (`--defer` present) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:654-656]`,
and Restore mode (`--restore` present) `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:684-686]`.
The 3 `--mode` sub-values in category (1) above only apply *within* Save mode.

---

## `/thorough_plan` shared-namespace deterministic template

Two independent template-based synthesis mechanisms exist; both are reproduced below with
named slots, each verified against its actual construction site.

**Template A — `thorough_plan_checkpoint.py`'s `## Last user intent` / restore hint:**
```
## Last user intent
thorough_plan {TASK}: last completed boundary {STAGE}; next phase to run: {NEXT_PHASE}. Re-invoke /thorough_plan {TASK} and resume at {NEXT_PHASE}.
...
## Restore hint
Re-invoke /thorough_plan {TASK} in a fresh session; last completed boundary: {STAGE}; next phase: {NEXT_PHASE}.
```
`[src: quoin/core/scripts/thorough_plan_checkpoint.py:138-147]`, where `{STAGE}` is
`thorough-plan:round-{ROUND_N}-{PHASE}` `[src: quoin/core/scripts/thorough_plan_checkpoint.py:249]`
and `{NEXT_PHASE}` comes from a fixed lookup table `{"plan": "critic", "critic": "revise",
"revise": "critic"}` `[src: quoin/core/scripts/thorough_plan_checkpoint.py:40-44]`.

*Concrete example*, constructed directly from the template and lookup table (not invented):
given `--task ivg-200-foo --round 2 --phase critic`, `STAGE` =
`thorough-plan:round-2-critic`, `NEXT_PHASE` = `_NEXT_PHASE["critic"]` = `revise`, so
`## Last user intent` renders as: `thorough_plan ivg-200-foo: last completed boundary
thorough-plan:round-2-critic; next phase to run: revise. Re-invoke /thorough_plan
ivg-200-foo and resume at revise.`

**Template B — `/checkpoint`'s own B3 session-state-synthesis surfaced prompt** (this is the
Tier-4/B3 mechanism described above, distinct from Template A — Template A belongs to
`thorough_plan_checkpoint.py`; Template B belongs to `/checkpoint` itself):
```
No recent checkpoint files found, but a recent session-state file exists:
  `{SESSION_STATE_FILENAME}`  (mtime: {DATE} {TIME})
  Active task: {ACTIVE_TASK}
  Current stage: {CURRENT_STAGE}
Synthesize a minimal restore from session-state only? [y / n]
```
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:962-969]`, where `{ACTIVE_TASK}` is
derived from the session-state filename (date-prefix stripped)
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:946-950]`, `{CURRENT_STAGE}` from the
session-state's `## Current stage` block or the literal string `(stage unknown)` if absent
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:954-955]`, and on `y` the flow
additionally extracts a single-line `{INTENT}` from `## Unfinished work` by stripping list
glyphs, numbering, and one of four status glyphs (`✓ ✗ ⏳ 🚫`)
`[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:971-987]`.

*Concrete example*: given a session-state `## Unfinished work` block whose first non-empty
line is `- 1. ⏳ T-04 wire up X` — this exact example is given verbatim in the source
comment `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:979]` — the extracted
`{INTENT}` is `T-04 wire up X`.

---

## Source citations / appendix

### Incident-to-rule map (Linear issues → spec section)

All eight issues were retrieved live via the Linear MCP tool (`mcp__linear-server__get_issue`)
on 2026-07-14; none required a `[needs Linear lookup]` fallback.

| Issue | Title (verbatim) | Relates to spec section |
|---|---|---|
| IVG-25 | "Fix three checkpoint bugs (B1 mtime filter, B2 sentinel path, B3 OR trigger)" | Picker tiers — B1/B2/B3 labels |
| IVG-28 | "Fix checkpoint compact ordering" — description: "Added Step 1.4 compact-already-ran skip path via postcompact.sh sentinel; deprecated --after-compact flag" | Compaction interactions (dual-sentinel skip — historical description of what IVG-28 originally shipped, not live documentation; IVG-258 S-6 extended the check to a two-arm design, recorded as an explicit no-change finding for this row); Save modes/flags (`--after-compact` deprecation) |
| IVG-30 | "Fix checkpoint restore picker staleness (B1/B2/B3/B4)" — description: "Fixed four staleness bugs in the restore picker; added regression tests for all four scenarios" | Picker tiers — this is the historical source of the "B4" label the task brief warned about; current source shows no B4 (see B-label discussion above) |
| IVG-57 | "BUG with checkpoint restore" — a long user-feedback report about missing recent checkpoints and confusing overflow messaging; PR title on the linked attachment: "fix: IVG-58 ccusage v20 bulk parse + IVG-57 checkpoint B3 Clause B (0.5.15)" | Picker tiers — B3 Clause B (same-day-symptom mitigation) |
| IVG-61 | "Checkpoint BUG again!" — a detailed debugging transcript concluding "the checkpoint is inside `price_elasticity/` subdirectory, not the project root"; linked PR: "IVG-61: Fix checkpoint project-root resolution across all hooks" | Not directly covered by a dedicated section in this spec, but underlies the `_PROJECT_ROOT`/`resolve_project_root()` resolve-once convention referenced throughout the SKILL.md (e.g. `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:156-173]`) |
| IVG-84 | "Checkpoint restore bug" — user report of wrong-session restores; linked PR: "fix(checkpoint): fix --restore project-hash derivation and bump to sonnet (IVG-84)" | Session-hash and SID derivation |
| IVG-98 | "/thorough_plan: save phase-boundary checkpoint so killed sessions are recoverable" — full acceptance criteria in description match `thorough_plan_checkpoint.py`'s docstring almost verbatim (phase boundaries plan/critic/revise, minimal checkpoint body) | Checkpoints writers (`thorough_plan_checkpoint.py`); `/thorough_plan` shared-namespace deterministic template (Template A) |
| IVG-105 | "Checkpoint bug" — user report: "it shouldn't be starting in the session which is overflown - it should continue in the new session"; linked PR: "IVG-105: Add same-session detection to checkpoint/thorough_plan restore" | Same-session detection |

### Test fixture directory

`quoin/dev/tests/fixtures/checkpoint_picker/` contains exactly one subfolder, `sessions/`,
which in turn contains exactly one file:
`2026-05-17-personal-site-sim-embed.md` `[src: directory listing, verified 2026-07-14]`.
This is **not** a full picker-scenario fixture set (no `checkpoints/` subfolder, no sentinel
`.txt` fixtures) — it appears to be a single session-state fixture, presumably used by the
B3-synthesis-path tests (see `test_checkpoint_b3_clause_b.py` below) to exercise the
session-state-derived fallback without needing a full checkpoint file. This document does
not assert what specific test consumes it beyond that inference from directory contents
alone — the test file itself was not opened to confirm the exact usage.

### Existing tests whose names suggest picker/checkpoint coverage

(Found via `find quoin/dev/tests -iname '*checkpoint*'`; file contents were not opened for
this spec — names and inferred coverage only, from filename semantics and the citations
established above.)

| Test file | Inferred coverage (by name) | Related spec section |
|---|---|---|
| `test_checkpoint_picker_incident_repro.py` | Regression repro for the "5-day-old pep-mvp checkpoint auto-picked instead of today's session" incident referenced in the SKILL.md itself `[src: quoin/adapters/claude/skills/checkpoint/SKILL.md:921]` | Picker tiers — Combined auto-pick gate |
| `test_checkpoint_picker_staleness.sh` | Staleness-gate behavior of the picker | Picker tiers — Combined auto-pick gate, `QUOIN_RESTORE_STALE_DAYS` |
| `test_checkpoint_cross_task_guard.py` | Cross-task identity guard (Tier 1 and Tier 3) | Picker tiers — Tier 1, Combined auto-pick gate |
| `test_checkpoint_fastpath_gate.py` | Tier 1 fast-path gating logic | Picker tiers — Tier 1 |
| `test_checkpoint_b3_clause_b.py` | B3 trigger Clause B (same-day-symptom mitigation) | Picker tiers — B3/Tier 4 |
| `test_checkpoint_same_session_restore.py` | Same-session detection (Step 1.5) | Same-session detection |
| `test_checkpoint_ivg84_hash_and_tier.py` | IVG-84 project-hash/tier regression | Session-hash and SID derivation |
| `test_checkpoint_ivg84_empty_sid.py` | IVG-84-adjacent empty-SID guard behavior | Sentinel families — empty/unknown-SID guards |
| `test_checkpoint_panic_mode.py` | Panic-tier (`PANIC_BPS`) skeleton save | Compaction interactions — high-utilization tiers |
| `test_checkpoint_cleanup_autofire.py` | Step 1.47 auto-cleanup default-on behavior | Sentinel families; Save modes — utilization-driven tiers |
| `test_checkpoint_filename_format.py` | Timestamped vs. legacy vs. precompact filename shapes | Checkpoints writers |
| `test_checkpoint.sh` | General/legacy checkpoint coverage (name is generic; scope not inferable without opening) | `[unverified: scope not determined from filename alone]` |
| `test_thorough_plan_checkpoint_roundtrip.py` | `thorough_plan_checkpoint.py` write/read roundtrip | Checkpoints writers; `/thorough_plan` shared-namespace template (Template A) |
| `test_thorough_plan_phase_checkpoint_present.py` | Presence check for phase-boundary checkpoint after a simulated round | Checkpoints writers |
| `test_sentinel_family_parity.py` | Byte-identical parity of the 9-family list across `_lib.sh`, `/cleanup`, `/sleep` | Sentinel families |

No test file matched a name suggesting direct coverage of the two-writer shared-pool
ambiguity described in "Checkpoints writers" above — this reinforces that section's
conclusion that the question is currently unresolved by both source and test suite.
