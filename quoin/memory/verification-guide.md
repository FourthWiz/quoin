# Verification guide — §V ground-truth reconciliation (verbose reference)

## Why this exists (IVG-115)

Two related but distinct incidents motivated §V:

- **Incident #1 (IVG-84, zero-tool-call hallucination):** a Haiku-tier `/checkpoint` restore
  emitted a fabricated generic summary with zero tool calls instead of running the ~430-line
  procedure. This is a tier-capability failure, not a verification-gap failure — it was fixed by
  bumping `/checkpoint` from Haiku to Sonnet, and is **out of §V's reach** (a script cannot
  detect a model that skipped invoking it in the first place; D-11).
- **Incident #2 (2026-07-04, `end_of_day` false narrative):** a Haiku-tier `/end_of_day` run
  wrote a daily cache claiming two already-merged/finalized tasks were "awaiting /pr" and
  "awaiting /end_of_task", and skipped required Step 3c/3d sub-procedures (lessons-learned
  pruning prompt, flipping 8 stale `-orchestrator.md` session files). The subagent trusted its
  own stale session-file narrative instead of checking ground truth. This is the incident §V
  targets: **the model ran, produced output, and that output silently contradicted reality.**

**Thesis:** the fix is deterministic out-of-model reconciliation (a script re-derives truth and
diffs it against a structured claim), not a line-count/tier rule. §V is verification, not a
capability tier.

## Claim model (D-02 / D-10 — the manifest is never trusted as truth)

`verify_claims.py` never regex-scrapes narrative English. Skills that carry §V emit a
**machine-readable claims manifest**: a fenced `yaml` block headed `## Claims` in the skill's
audit artifact, entries shaped `{task_ref: <str>, status: <enum>}` where `status` is a closed
enum `{awaiting_pr, awaiting_end_of_task, in_progress, merged, finalized}`. This manifest is the
**claim under audit** — the verifier independently re-derives truth (`finalized/` folder
presence + `gh pr list` state) and diffs the two. A lying manifest that says "awaiting" while
truth says finalized/merged still produces a loud MISMATCH — that IS the incident-2 catch. This
is distinct from treating the manifest as a source of truth, which D-02 explicitly rejects.

## `verify_claims.py` (canonical: `quoin/core/scripts/verify_claims.py`; compat wrapper:
`quoin/scripts/verify_claims.py`)

Stdlib-only, both DEPLOYED and CORE installer lists.

- `--reconcile-tasks [--claims-file <path>] [--gh-json-file <path>] [--finalized-only]` — for
  every active `.workflow_artifacts/<task>/` and archived `.workflow_artifacts/finalized/<task>/`
  folder, emits `{task, finalized: bool, pr_status}`. `finalized` is 100% deterministic (folder
  presence, no gh needed). With a claims manifest, additionally emits per-claim
  `{task_ref, status, verdict: ok|MISMATCH|unmatched}` plus a `coverage:` advisory line per
  finalized/in-window-active folder with no matching claim (silent-omission surfacing, MIN-c).
  `--finalized-only` forces `pr_status: gh-unavailable` and makes **no** `gh` call — this is the
  mode the SessionEnd hook uses so session-end stays network-free (MAJ-A).
- `--check-side-effects --skill <name> [--checkpoint-file <path>]` — skill-keyed required
  side-effect predicates (end_of_day: daily cache exists+non-empty, in-scope sessions flipped
  `end_of_day_due: no`, resume-cookie present+unexpired, lessons-learned prune decision recorded
  if >30 entries; checkpoint: every `## In-flight artifacts` path exists on disk).
- `--self-test` — embedded fixtures, exit 0/1.
- Exit codes: `0` clean/PASS; `8` MISMATCH/MISSING (a real signal, not a fail-open guard); `4`
  usage/missing-source.
- `canonical_ref(s)` / `match_task(ref, folders)`: `IVG-105` → `ivg-105`; kebab slug otherwise.
  0 matches → `unmatched` (no signal); 1 → matched; >1 sharing the same issue number → unioned;
  disagreeing → `ambiguous` (unmatched, fail-open). No fuzzy prose join.
- `filename_task(name)`: strips `.md`, a trailing `-precompact` suffix, then a leading
  `^\d{4}-\d{2}-\d{2}(T\d{2}:?\d{2})?-` date prefix. Handles all three checkpoint filename shapes
  plus the session-file shape.

### Empty-manifest rule (window-scoped, r5/MAJ-1 — the quiet-day false-positive fix)

A `--claims-file` that parses to **zero** `## Claims` entries is a positive failure (exit 8,
reason `empty-manifest`) **only when the per-run-window coverage set is non-empty** — i.e. a
task was finalized within the run window (`finalized/<task>/` mtime ≥ the run lower bound
`L = max(daily/<date>.md date strictly < today) + 1 day`, else `today`), or (in-session path
only) an in-window active task lacks a claim. This is deliberately **not** keyed on the all-time
`finalized/` archive (104 folders in this repo) — an earlier design did that and false-fired on
every legitimate quiet day. A genuine quiet day (no in-window finalized/active work) writes a
correct zero-claim manifest and exits 0, no banner. `--finalized-only` mode uses portion (a)
alone (in-window-finalized-by-mtime); the in-window-active half is an in-session-only refinement.
Coverage lines alone (≥1 claim present, one folder just isn't referenced) are advisory and never
force exit 8 by themselves.

## Per-skill §V wiring

Generated by `quoin/scripts/inject_verification_step.py` (standalone, DEPLOYED-only, structural
clone of `inject_pollution_dispatch.py`). `--dry-run` prints blocks; re-running is idempotent
(byte-identical); `--check` exits 7 on drift.

- **`end_of_day` (full — early claims + late verify, the only hook-backstopped skill).** Carries
  TWO markers: `<!-- §V-claims-begin/end -->` (heading `## §V Claims manifest (emit as an
  always-run early step)`) injected immediately **before** `### Step 3b: Review and promote daily
  insights` — i.e. **after** the daily-cache write, not at the `### Step 3` heading (an earlier
  anchor position would place the manifest-write before the cache it derives from, yielding an
  empty/garbage manifest). This step always runs as part of normal flow, independent of whether
  the model later reaches §V — this is what gives the SessionEnd hook a claim source even when
  the model silently skips steps (CRIT-1/D-12). And `<!-- §V-verify-begin/end -->` (heading
  `## §V Ground-truth verification (execute after the skill's work, before the final report)`)
  before `### Step 5: Report to user`, running both `--check-side-effects --skill end_of_day`
  (window via `compute_lower_bound(source="daily")`, matching end_of_day's own rollup window —
  imported from `select_unprocessed_sessions.py`, core→core) and
  `--reconcile-tasks --claims-file <manifest>` (live gh, in-session, NOT `--finalized-only`). On
  exit 8: do not finalize the report, surface each MISMATCH/MISSING, self-correct the
  deterministic ones, increment `verification_mismatches`, re-write the audit artifact. Set
  `verification_ran: yes` only on a clean run.
- **`start_of_day` / `weekly_review` (late verify only — consumers, no hook backstop).** Run
  `--reconcile-tasks`; derive any displayed PR/task-status line from the reconcile table, not
  narrative prose. `start_of_day`'s **absent-flag defense (MAJ-1):** for every in-scope
  `end_of_day` session it reads, a **missing or `no` `verification_ran`** field is itself treated
  as a mismatch signal — so a §V silently skipped upstream is still caught downstream.
- **`status` / `triage` / `cost_snapshot` (light — reconcile-only, no side-effect check).**
  Read-only Haiku reporters; run `--reconcile-tasks`, derive displayed status from the table. A
  fixture contradiction is surfaced, not silently reported.
- **`checkpoint` (T-07, deliberately narrow — see Descope below; historical, the Claude skill is no longer shipped and the predicate stays in `verify_claims.py` for other runtimes).**

### `checkpoint` T-07 — descoped, on-disk-only

The cross-task identity guard already present in the adapter (comparing checkpoint `## Active
task` heading against the freshest session-file's filename) is NOT re-implemented by §V — a
script cannot verify "what the model claims to have loaded." T-07 adds only a purely on-disk
predicate: (i) **primary** — every path in the checkpoint's `## In-flight artifacts` list exists
on disk (a checkpoint pointing at vanished artifacts is a real, checkable defect); (ii)
**secondary/largely redundant** — `filename_task()` comparison between the checkpoint and the
freshest session file, differing from the existing heading-based guard only when a checkpoint's
filename and its own `## Active task` disagree. This is labeled a cross-task-guard +
artifact-existence regression, not an incident-1 reproduction (incident-1 is out of §V's reach).

## SessionEnd hook backstop (T-12 — `quoin/quoin/hooks/sessionend.sh`)

The real enforcement layer: a model that silently skips steps (incident #2's exact failure mode)
can skip the in-model §V step too. The hook fires independently of the model and reads T-05's
early-emitted manifest — it does NOT run a claimless reconcile.

- **EOD-class gate (MAJ-B/D-12):** `-f .workflow_artifacts/memory/daily/<today>.md` — NOT
  `end_of_day_due: yes`. This decouples the backstop from the pre-existing early-exit, so a
  *complete-but-lying* end_of_day (flags flipped, false narrative) is still reconciled. A
  zero-tool-call model that writes no daily cache leaves this gate false (out of scope; D-11,
  handled by the prior tier bump only).
- **Three manifest states:** ABSENT → positive failure (model skipped the always-run manifest
  step) → `[quoin-§V] end_of_day ran (daily/<today>.md present) but wrote no verification
  manifest — verification was skipped; re-run /end_of_day verification.` PRESENT-BUT-EMPTY with
  non-empty window-scoped coverage → same failure class as absent, reason `empty-manifest` →
  `[quoin-§V] end_of_day wrote an empty verification manifest (no claims) while <N> task(s)
  remain unclaimed — verification was skipped; re-run /end_of_day verification.` PRESENT with
  ≥1 claim contradicted by re-derived truth → `[quoin-§V] end_of_day claims contradict ground
  truth: <tasks> — re-run /end_of_day verification.` A present manifest that reconciles clean
  (exit 0) → no banner.
- **No network (MAJ-A):** the hook always passes `--finalized-only` — no `gh` call, no timeout
  needed. Live gh reconcile stays in the in-session model §V only, where latency is tolerable.
- **Audit sibling, never the manifest path (MAJ-2):** the hook appends `> hook-reconcile:
  <mismatch-tasks | empty-manifest | manifest-absent> @<ts>` to a **distinct**
  `end_of_day-<today>.hookaudit.md` file — never to `end_of_day-<today>.md` itself. Writing to
  the manifest path on the absent branch would create an empty stub, converting "absent" into
  "present-but-empty" on the next same-day re-fire (self-erasing signal bug, closed by this
  separation).
- **Fail-open discipline (MANDATORY, MINOR-1):** STEP 5b (the §V backstop, inserted between the
  existing STEP 5 and STEP 6) is a fall-through on any error/absence — `continue`/empty
  `verify_msg`, **never `exit`**. Only STEP 4 and the new STEP 7b guard may `exit`. An `exit`
  anywhere between STEP 5 and STEP 7b would also skip STEP 7's pre-existing S-4 nudge and
  re-open a Close-snapshot regression.
- **STEP 8 gate preserved (MAJ-1):** the original `end_of_day_due: yes` hard-exit at STEP 5 is
  relaxed to a boolean (`eod_due`); STEP 7b (`[ "$eod_due" = 1 ] || exit 0`, inserted immediately
  after STEP 7's single systemMessage and before STEP 8/Close-snapshot) restores the original
  gating condition — logically equivalent to the pre-change grep-based exit, not byte-identical.
  STEP 7 composes ONE systemMessage folding the S-4 nudge + any §V banner, preserving the
  single-object hook-output contract.

## Session-state fields

`verification_ran` (default `no`) / `verification_mismatches` (default `0`) live in the same
`## Cost` YAML block as `fallback_fires`, atomic-rename increment, never decremented. Omission or
`no` is read as a **positive failure signal**, never a silent pass (MAJ-1) — this is why
`start_of_day` treats a missing flag on an in-scope session as a mismatch in its own right.
`end_of_day`'s Cost-summary and `weekly_review`'s Cost-data step roll up `verification_mismatches`
per window (same machinery as the existing `fallback_fires` roll-up).

## Deferred skills (`pr`, `end_of_task`, `continue_work`)

Not wired with §V — `gh` (and, for `end_of_task`, the finalized-folder move) is already intrinsic
to their core procedure, so a separate reconcile pass would be redundant. Recorded as an explicit
sibling acknowledgement in `next-steps.md` per the 2026-05-24 lesson (any skill deferring a
cross-cutting extension must name the deferral, not silently drop it).

## Tests

`test_verify_claims.py` (engine: reconcile/side-effect unit tests, incident-2 regression,
cross-task-guard regression, empty-manifest window-scoping including the r5 quiet-day guard).
`test_verification_step.py` (generator drift: heading/marker presence-once, required-token,
claims-before-verify ordering, anchor position, idempotence, `run_check()==0`; mirrors
`test_mintier_guard.py`'s structure; also covers the light-block skills and the
`test_absent_verification_ran_is_mismatch` prose-contract assertion). `test_sessionend_verify.sh`
(POSIX-sh hook harness, 9 sub-tests A–J covering all T-12 acceptance letters against the real
deployed hook + wrapper, including two wrapper-absent cases via a throwaway hooks/ tree with no
sibling `verify_claims.py`). `test_install_verify_claims_deployed.py` (extended with the
`inject_verification_step.py` DEPLOYED assertion).

## Portability classification

`verify_claims.py` — **portable-core-with-gh-seam** (`gh` is a generic git-host CLI, not
Claude-only; `finalized/` + session parsing are portable; the `--gh-json-file` seam and
`shutil.which` guard keep it testable without a live binary).
`inject_verification_step.py` — **Claude-adapter** (edits `adapters/claude/skills/*`).
The `sessionend.sh` T-12 backstop edit — **Claude-adapter** (hooks are Claude-runtime-specific).
See `quoin/docs/runtime-portability.md` for the full bucket definitions.
