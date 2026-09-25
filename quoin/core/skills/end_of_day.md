# end_of_day

Runtime-neutral intent for the end_of_day skill. Any runtime adapter (Claude,
Codex, …) that implements this skill should match the contract described here.

## Purpose

At the end of a working session, consolidate all of today's session-state files
into a single daily-cache file under `.workflow_artifacts/memory/daily/<date>.md`,
promote eligible insights into the lessons-learned file, prune lessons-learned when
oversized, write a resume cookie, and flip `end_of_day_due: no` on processed
session-state files. The skill never makes commits, never edits source files, and
never invokes another workflow phase.

## When to use

- User is wrapping up a working session.
- User says "done for the day", "save my progress", "EOD", "end of day".
- User says "wrapping up" or "closing out".

## Inputs

- All session-state files under `.workflow_artifacts/memory/sessions/` whose date-prefix falls
  within the EOD processing window (lower_bound..today, inclusive) OR whose `end_of_day_due`
  field is `yes`. Lower bound is discovered by finding the most recent prior daily-cache file
  and adding one day; if none exists, lower_bound = today. Legacy files lacking the field are
  treated as `yes`.
- Optional today's insights scratchpad at `.workflow_artifacts/memory/daily/insights-<today>.md`.
- Existing `.workflow_artifacts/memory/lessons-learned.md` (advisory; created if absent).
- Each active task's `.workflow_artifacts/<task-name>/cost-ledger.md` (advisory, for
  session counts and fallback_fires aggregation).
- The optional resume cookie target path at `.workflow_artifacts/memory/resume-cookie.md`.
- Current version-control state across project repos (recent commits per repo).
- Flags: `--skip-sleep` — suppresses the final-step `sleep` memory-consolidation
  invocation described in the Behavior contract below.

## Output

- One daily-cache file at `.workflow_artifacts/memory/daily/<YYYY-MM-DD>.md` (Class B per
  format-kit, with a `## For human` block composed by the runtime adapter's summarization
  mechanism, written directly in the same generation as the body — no separate
  post-processing script).
- Updated git-log file at `.workflow_artifacts/memory/git-log.md` (rolling window of
  recent commits across repos).
- Zero-or-more new lessons appended to `.workflow_artifacts/memory/lessons-learned.md`
  after user confirmation.
- Zero-or-more workflow-suggestion entries appended to
  `.workflow_artifacts/memory/workflow-suggestions.md`.
- An updated resume cookie at `.workflow_artifacts/memory/resume-cookie.md` (2 KB cap,
  allowlisted fields, ISO `expires`).
- Session-state files for today rewritten with `end_of_day_due: no` (atomic-rename
  pattern, flipped only after daily-cache write succeeds).
- A short report rendered to the user.

## Behavior contract

- Read once: today's session files and insights file are read once per invocation; never
  re-evaluated during the same run.
- Error-tolerant: every input lookup MUST tolerate missing or unreadable files as "no
  signal" and continue. A missing insights file or missing cost-ledger file is a no-op,
  not an error.
- Lower_bound discovery: the EOD processing window starts at the day after the most recent
  prior daily-cache file under `daily/<YYYY-MM-DD>.md` (excluding `insights-*.md`). If no
  prior daily exists, lower_bound = today. If the most recent daily IS today, lower_bound =
  today (same-day re-run triggers merge mode per the Behavior contract below).
- Hybrid session selection: a session file is in scope iff its date-prefix is `<= today`
  AND its `end_of_day_due` flag is `yes` — selection is flag-authoritative; `lower_bound`
  plays no role in the selection filter itself, only in scoping the reporting window (see
  below). This catches yes-flagged straggler files from before `lower_bound`. Legacy files
  lacking the flag line are treated as `yes`.
- Authoritative single-invocation mechanism: the runtime adapter uses the shared
  session-selection helper as the single source of truth for this run's window and file
  list — computed ONCE per invocation and reused for both the recent-git-activity gather
  and the daily-cache session selection, never recomputed a second time or hand-derived
  in prose.
- Reporting-window scoping (not a selection filter): the Cost summary and insights-scan
  window spans `[lower_bound, today]` only — it is NOT widened to include a straggler's
  own date. Stragglers are captured via selection (the flag-authoritative rule above), not
  via a widened reporting window; a prior run already reported (or should have reported)
  their own dates, so re-sweeping them here would double-count. The runtime adapter names
  stragglers explicitly in the report instead.
- Same-day re-run: if `daily/<today>.md` already exists, MERGE per proc:D-06 (section-by-
  section algorithm) rather than overwrite. Never replace with a smaller set. If the existing
  file is unreadable (corruption), refuse with a clear error.
- Orphan recovery: the `--recover-orphans` flag triggers a scan for sessions where
  `end_of_day_due: no` AND the task-name slug is absent from every daily file body (proc:T-19
  word-boundary-aware check). Two prompt groups: RECENT (within last 7 days) and HISTORICAL
  (older). User confirms each group separately. Confirmed orphans are treated as `yes` for
  this run only; the flag is permanently flipped to `no` after the daily-cache write succeeds.
- Covered-but-due reconciliation: BEFORE orphan recovery runs, `--recover-orphans` invocations
  auto-flip sessions where `end_of_day_due: yes` AND the task-name slug (via the same
  word-boundary/base-slug technique as orphan detection) IS already present in a daily file body
  — the mirror-image case of an orphan. These flips complete before the run's authoritative
  session-selection window is computed, and are outside the daily-cache build set (that work was
  already captured). Opt-out via a runtime-specific disable knob. Deliberately scoped to a
  documented subset of the slug space this round (e.g. auto-generated orchestrator-suffixed
  sessions) — a broader suffix-family reconciliation is out of scope and must not be silently
  widened without a dedicated collision-safety pass.
- Finalized-task digest preservation: a session already flipped to `end_of_day_due: no` by the
  end_of_task skill (see that skill's contract) but carrying a same-provenance marker dated within
  the current processing window is still included in the daily-cache build (so finalized-task work
  remains visible in "Completed today") even though its flag is already `no`; the flip step for
  such a session is a no-op.
- Crash safety: the `end_of_day_due: yes` → `no` flip happens ONLY after the daily-cache
  write succeeds. A crashed run MUST NOT mark sessions as processed.
- Resume-cookie discipline: writer MUST refuse to include any field outside the allowlist
  (`task`, `last_skill`, `branch`, `dirty_count`, `expires`); cookie body capped at 2 KB;
  written via atomic-rename pattern.
- Lessons-learned promotion is interactive: candidates are presented, user confirms which
  to keep, only confirmed entries are appended.
- Lessons-learned pruning prompt is shown only when entry count exceeds 30.
- Daily cache file is Class B per format-kit: it MUST include a `## For human` block (5-8
  lines, plain English) composed by the runtime adapter's summarization mechanism, written
  in the same generation as the body. Detection: the first 50 lines of the daily-cache file
  (no YAML frontmatter to skip) must contain a heading matching the regex
  `^## For human\s*$` for the file to be recognized as v3-format by downstream readers
  (notably `start_of_day`).
- Cost summary inside the daily cache is built from per-task cost-ledger row counts within
  the processed reporting window `[lower_bound, today]` (not today's rows only); the skill
  MUST NOT invoke a version-control cost-reporting CLI to compute dollar amounts.
- `fallback_fires` aggregation across today's session-state files MUST appear in the
  daily cache when the day total > 0.
- The skill MUST NOT push, commit, or modify source files.
- The skill MUST NOT auto-invoke another workflow phase, with one exception: as its final
  step, after the report has been shown to the user, the skill invokes the `sleep`
  memory-consolidation skill unless the `--skip-sleep` flag was passed. A consolidation
  failure MUST NOT roll back the daily cache — the cache is already durably written by
  the time this step runs.
- Cost-ledger writes by this skill itself are conditional: only when a task context is
  unambiguously named by the user or unambiguously implied by an active session-state file.

## Out of scope

- Model tier and dispatch mechanism — the runtime adapter handles these.
- The §0 self-dispatch grammar — runtime-specific.
- Per-runtime session-file enumeration (Claude reads JSONL session IDs; other runtimes
  may use different mechanisms).
- The exact shell invocations for version-control status, log, and per-repo enumeration.
- Cost-CLI invocations and dollar-amount computation.
- The specific reformatting rules for the daily cache body beyond "Class B per format-kit".

## Notes

- The daily-cache section set is closed: `## For human`, `## Summary`,
  `## Sessions processed` (TABLE — the canonical "this session is covered" anchor used by
  proc:T-19 orphan detection), `## Completed today`, `## Unfinished — carry forward`,
  `## Decisions log`, `## Git activity summary`, `## Cost summary`,
  `## Tomorrow's priorities`. Adapters MUST NOT silently introduce new top-level sections
  into the daily cache.
- The resume-cookie expiry default is 24 hours from the write time.
- Adapters MAY decompose Step 3 into multiple internal sub-steps (e.g., 3a/3b/3c/3d)
  as the current Claude adapter does; the contract here is the closed list of side-effects,
  not the sub-step decomposition.
- This skill WRITES the v3 daily-cache file that `start_of_day` READS. The `## For human`
  block MUST appear within the first 50 lines of the daily-cache file (no frontmatter) so
  that `start_of_day`'s v3-format detection rule fires correctly.
- The `.workflow_artifacts/<task-name>/` path pattern is the canonical location for
  per-task artifacts; the skill reads cost-ledger files there.
