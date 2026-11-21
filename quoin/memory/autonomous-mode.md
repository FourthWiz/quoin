# Autonomous mode reference (`--autonomous`)

> **Sibling primitive (opposite default):** `[autonomous]` carries pre-authorized
> answers so a decision gate auto-resolves; a background context with NO opt-in has no
> pre-authorized answer, so its only safe move is to FAIL CLOSED. The fail-closed guard,
> the `[no-interactive]` sentinel, and the `needs-decision-{task}.md` sentinel (a distinct
> sibling of the `autonomous-halt` family the supervisor never reads) are documented in
> `decision-gate-guard.md` (IVG-150).


Verbose Tier-1 reference for the opt-in `--autonomous` span on the heavy
pipeline, `run_hard` (reached via `/run hard:`, a manually invoked exception)
(IVG-153, `autonomous-run-mode` Stage 1). This file holds the full
auto-resolution table, the sentinel/propagation rule, the Formulation
quality bar, and the halt-sentinel contract. `run_hard/SKILL.md` and the
per-skill `SKILL.md` bodies carry the operative branches; this file is
the single place that documents the whole surface in one pass.

The default light `/run` also accepts `--autonomous`, but there it only means
"never ask the user; record assumptions in the brief; run every stage of a
staged task in one session". It writes no marker,
arm, progress or done sentinel, has no supervisor and no Stop-hook
continuation, so everything below describes `run_hard`.

## The `[autonomous]` sentinel and propagation

Propagation is a `[autonomous]` prompt sentinel prefixed onto every
sub-phase spawn prompt — mirroring the existing `[no-session-age-guard]`
and `[no-redispatch]` sentinel conventions — rather than an env var, so
it stays greppable and deterministic. Leading sentinels stack (e.g.
`[no-redispatch] [autonomous]`).

- `run_hard` prefixes `[autonomous]` directly onto every sub-phase spawn it
  issues: discover, enrich, specify, architect, thorough_plan,
  implement, review, **`end_of_task`** (the terminal Phase-6 spawn),
  and every subagent-mode `gate` boundary. This is the SPAWNED-phase
  roster, not the resumable-phase roster — `fast_path_triage` (Phase
  1.6) runs inline, never as a spawn, so it is deliberately absent here.
- Propagation is **transitive**: every spawning skill re-prefixes
  `[autonomous]` onto the deeper spawns it issues, so the sentinel
  reaches the full transitive spawn set. Concretely, `thorough_plan`
  re-prefixes it onto its `plan`/`critic`/`revise`/`revise-fast`
  spawns, and `review` re-prefixes it onto its Large-fan-out
  `security_review` + dimension subagent spawns.
- Each sub-skill parses/strips `[autonomous]` at bootstrap into its own
  `_AUTONOMOUS` state.
- For **inline** gates (post-implement, post-review) there is no spawn
  prompt to carry a sentinel — the orchestrator applies autonomous gate
  behavior directly from its own `AUTONOMOUS` state.
- The run_hard→`end_of_task` edge is a direct edge, not a deeper one: `run_hard`
  spawns `end_of_task` itself as the terminal Execution phase, so the
  sentinel is prefixed onto that spawn exactly like any other direct
  sub-phase.

## Formulation quality bar

The Formulation→Execution bar is the sole safety substitute for the
human checkpoints that autonomous mode removes. It sits between the
planning phase and Execution (implementation onward); failing it is a
hard stop, never a silent proceed.

- **Medium/Large:** the bar requires the `thorough_plan` critic loop to
  have converged with a PASS verdict — not merely exhausted its round
  cap while still holding a REVISE verdict.
- **Small:** the bar requires the smoke gate to PASS **and** a
  confidence signal at or above `QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD`
  (default `0.7`). On the Small path `/specify` and `/architect` are
  skipped, so the confidence signal is sourced from the single-pass
  `/plan` skill's own `confidence` line — optionally augmented by an
  `enrich`-emitted `confidence` value, in which case the bar takes the
  **minimum** of the two. The underlying number is an Opus
  self-assessment, a soft signal by nature; the smoke-gate PASS
  requirement is the harder half of the bar.
- **Fast route:** the bar requires `min(triage_confidence,
  enrich_confidence_if_present) >= QUOIN_FASTPATH_CONFIDENCE_THRESHOLD`
  (default `0.8`) — stricter than the Small path's `0.7` default, since
  no plan was critiqued on this route.
- Below the bar: hard stop via the halt-sentinel contract below. The
  run never enters Execution on a formulation that hasn't cleared the
  bar.

## Runtime flag

A task lock may carry `runtime: opencode` when it was taken by an OpenCode
phase run from a `legacy` install. Auto-resume and takeover treat such a lock
as owned by another runtime, so that task is never continued on Claude.

## Halt-sentinel contract

Every hard stop writes a halt-sentinel (with reason) **before exit**,
then stops. Stage 1 only *writes* the sentinel — consuming it (resume,
a future supervisor, cross-session pickup) is Stage 2, out of scope
here; this is forward-compatible groundwork only.

- **Location:** `.workflow_artifacts/memory/autonomous-halt-{task}.md`
  — deliberately **outside** the task folder, because `/end_of_task`
  archives the task folder into `finalized/` and a future supervisor
  needs the halt record to survive that move.
- **Schema:** `task`, `phase`, `reason`, `timestamp`, `resume_hint`
  (one line).
- **Hard-stop sites (all six):** review BLOCKED; gate FAIL after the
  retry cap; review CHANGES_REQUESTED after 3 rounds; git conflict;
  branch-hygiene violation; below-bar formulation (the Formulation
  quality bar above).
- **Invariant, restated:** autonomous mode never auto-creates a PR, in
  any mode, at any hard stop or otherwise.

## Auto-resolution table — full transitive spawn set (15 skills)

The transitive spawn closure resolves to 15 skills: `run`, `discover`,
`enrich`, `specify`, `architect`, `thorough_plan`, `plan`, `critic`,
`revise`, `revise-fast`, `implement`, `gate`, `review`,
`security_review`, `end_of_task`. Every genuine interactive site across
that closure has a documented autonomous resolution below. (The live
structural-canary lint that enforces this table against the actual
`SKILL.md` sources is `test_autonomous_askuserquestion_coverage.py`;
it live-derives the spawn closure at test time rather than trusting a
frozen count.)

| Skill | Site | Autonomous resolution |
|---|---|---|
| run | Checkpoints A0/A/B/C/D | PASS auto-continues with no wait; non-PASS routes to the hard-stop/halt-sentinel path, never a silent proceed |
| discover | repo-spec draft offer (`.workflow_artifacts/spec.md` absent) | auto-SKIP the offer; never auto-writes the repo main spec |
| discover | repo-spec refresh offer (spec.md present) | auto-SKIP the offer |
| discover | §0'/§0″ dispatch-failure / 1M-credit prompts (4 sites) | generated fail-OPEN clause — see "§0'/§0″ generated fail-OPEN rows" below |
| enrich | gap-questions prompt | best-effort; flags assumptions in `enriched-prompt.md`; never blocks |
| enrich | §0'/§0″ dispatch prompts | generated fail-OPEN clause |
| specify | intent-elicitation prompt | skipped; synthesizes a confidence-scored spec from the raw prompt + `enriched-prompt.md` + `architecture.md`, records assumptions in `## Context` |
| specify | repo-main-spec-update gate | auto-"Reject" — never auto-writes the repo main spec |
| specify | §0'/§0″ dispatch prompts (4 sites) | generated fail-OPEN clause |
| architect | spec pre-flight prompt | proceed |
| architect | scan-ambiguity / scan-failure prompts | proceed best-effort, flag |
| architect | Phase-4 round-2 cost guard | proceed |
| architect | same-class escalation | continue revising |
| architect | §0'/§0″ dispatch prompts | generated fail-OPEN clause |
| thorough_plan | §1b resume prompt (2-option and same-session 3-option variants) | auto-select "Resume" (parse `## Current stage`, continue) — **never** "Resume in a new session"/STOP |
| thorough_plan | enrich pre-step prompt | skip (already run at Phase 1.4) or best-effort |
| thorough_plan | spec pre-flight prompt | skip if task-root `spec.md` exists, else non-interactive |
| thorough_plan | auto-classify confirm (prose, not an `AskUserQuestion` token) | accept the auto-classification |
| thorough_plan | same-class escalation (prose, not an `AskUserQuestion` token) | continue revising to `max_rounds` |
| thorough_plan | deeper spawns | re-prefixes `[autonomous]` onto `plan`/`critic`/`revise`/`revise-fast` spawns (transitive propagation) |
| plan | §0'/§0″ dispatch prompts | generated fail-OPEN clause |
| critic | §0'/§0″ dispatch prompts | generated fail-OPEN clause |
| revise | §0'/§0″ dispatch prompts | generated fail-OPEN clause |
| revise-fast | §0-worktree-fallback prompt (artifact-only block) | hand-synced fail-OPEN clause — see "§0-worktree hand-synced fail-OPEN rows" below |
| implement | branch-hygiene precheck (protected-branch prompt) | auto-create `feat/{task}` feature branch, no `AskUserQuestion` |
| implement | task-confirm prompt | auto-select "All remaining tasks" |
| implement | §0-worktree-fallback sidecar prompt (source-mutating block) | hand-synced fail-OPEN clause |
| gate | checks-PASS path | auto-approve, proceed to Step 5 audit-log persistence; **FAIL is never auto-approved** — returns the verdict to the orchestrator, which owns the retry/hard-stop decision |
| gate | §0-worktree-fallback prompt | hand-synced fail-OPEN clause |
| review | §0'/§0″ dispatch prompts | generated fail-OPEN clause |
| review | fan-out spawns | re-prefixes `[autonomous]` onto the Large-only `security_review` spawn and the Medium/Large dimension subagents (transitive propagation); verdict emission (APPROVED / CHANGES_REQUESTED / BLOCKED) itself is unchanged — BLOCKED handling stays a `run`-level hard stop |
| security_review | §0'/§0″ dispatch prompts | generated fail-OPEN clause (reached transitively via review's Large fan-out) |
| end_of_task | Step 1 garbage-files prompt | auto-proceed with the cleanup default |
| end_of_task | Step 2 commit-decision prompt | auto-select "Commit" — **never** "Abort" |
| end_of_task | Step 3 lessons-learned prompt | auto-capture via the existing capture triggers, or skip cleanly; no `AskUserQuestion` |
| end_of_task | Step 4 archive-type prompt | auto-select the safe default ("Fully complete" / top-level archive) |
| end_of_task | §0-worktree-fallback sidecar prompt | hand-synced fail-OPEN clause |
| end_of_task | (all sites, restated) | never auto-creates a PR, in any mode |

## §0'/§0″ generated fail-OPEN rows

The §0'/§0″ dispatch-failure and 1M-credit `AskUserQuestion` prompts are
**generated** per-skill by `inject_pollution_dispatch.py`
(`render_pollution_block` / `render_mintier_block`, `{skill}`
substitution) into the `<!-- §0doubleprime-begin/end -->` and §0'
blocks, across the 11 Opus-tier leaf skills: `architect`, `plan`,
`critic`, `revise`, `review`, `security_review`, `discover`, `specify`,
`enrich`, `init_workflow`, `repo_cleanup`. Under `[autonomous]`, the generated clause
reads: on any §0'/§0″ dispatch-failure or 1M-credit error, proceed at
current tier fail-OPEN and do **not** call `AskUserQuestion`. This
clause is added to the **generator template**, then every leaf skill's
`SKILL.md` is regenerated — the generated block is never hand-edited
directly (that would trip the generator's own drift guard).

## §0-worktree hand-synced fail-OPEN rows

The §0-worktree-fallback prompt (e.g. `revise-fast` L105) is **not**
generated by any script (`build_preambles.py` emits only `preamble.md`
warm-up files; `inject_pollution_dispatch.py` covers only §0'/§0″). It
is hand-maintained inline, delimited by
`<!-- §0-worktree-fallback-begin/end -->`, and byte-identity-enforced
across two classes:

- **12 artifact-only skills** — includes the reachable `revise-fast`
  and `gate`.
- **4 source-mutating skills** (sidecar variant) — `implement`,
  `rollback`, `end_of_task`, `pr` — includes the reachable `implement`
  and `end_of_task`.

Under `[autonomous]`, both block variants carry the same clause: on any
worktree-class dispatch error, proceed at current tier fail-OPEN and do
**not** call `AskUserQuestion`. The clause is propagated to **every**
member of both classes, not only the reachable ones, so the
byte-identity contracts stay green; on non-reachable skills the clause
is simply inert (conditional on the absent sentinel).

## Relocated: Exception: `run_hard` orchestrator (`/run hard:`)

Moved here verbatim from `CLAUDE.md`'s "Workflow sequence" section
(IVG-153 T-17). `CLAUDE.md` now carries only a short pointer back to
this section, to stay under its size ceiling.

**Exception: `run_hard` orchestrator.** When the user invokes `/run hard:`, they
have explicitly requested the full end-to-end pipeline. `run_hard` may
invoke `/implement` and `/end_of_task` on the user's behalf, but still
pauses at each gate checkpoint for confirmation before proceeding. The
user's `/run hard:` invocation constitutes the conscious decision;
the gate confirmations provide the safety checkpoints.

Under `--autonomous`, this reliance extends one step further: once the
Formulation quality bar passes, the run stays unattended from that
point through `/end_of_task`, auto-resolving every interactive
checkpoint and body prompt per the auto-resolution table above instead
of pausing for confirmation. Every hard stop defined in the
halt-sentinel contract above still halts the run and records the
reason rather than proceeding silently, and a PR is never auto-created
in either mode.

## Unattended runs and the sentinel contract

Stage 1 (above) covers the in-session autonomous span: propagation,
the quality bar, and the halt-sentinel *write* side. Unattended work across
sessions now goes through Claude Code's own `/goal`: `quoin run --autonomous
<task>` runs each unfinished stage of a staged task as one headless
`claude -p "/goal <condition>"` session (see "Unattended staged runs" below).
The relaunching supervisor, the in-session hand-off and the child watcher
were removed; they stay available from the `legacy` tag.

**Sentinel contract.** All four sentinels resolve under
`.workflow_artifacts/memory/` — outside the task-scoped folder, so
each survives `/end_of_task`'s later move of that folder into
`finalized/`:

- **Marker** — `autonomous-run-{task}.marker` (single line: task,
  timestamp, `autonomous: true`). Written once at autonomous-span
  entry, in `run_hard`'s Setup, right after `AUTONOMOUS=true` is set.
  `run_hard`'s resume reads it FIRST, before its own first decision point,
  to re-establish autonomous mode — so a headless resume never reverts
  to interactive and stalls. Re-writing the marker is a no-op-equivalent
  overwrite; plain (non-autonomous) `run_hard` and the light `/run` never write it.
- **Per-phase completion sentinels** —
  `autonomous-progress-{task}/{phase}.done`, one file per completed
  phase, for the FULL resumable `run_hard` phase roster (`run_hard/SKILL.md`
  `## Phase sequence`): `discover, enrich, specify, fast_path_triage,
  architect, thorough_plan, implement, review, end_of_task` — 9 phases.
  `enrich` (Phase 1.4), `specify` (Phase 1.5), and `fast_path_triage`
  (Phase 1.6) are IN-SET; the roster is never abbreviated as "Phases
  1..6", which would silently drop them. Finer
  progress within one long phase MAY also write
  `autonomous-progress-{task}/{phase}.{subphase}.done`. **Counting
  glob** (consumed by the supervisor's no-forward-progress guard):
  `autonomous-progress-{task}/*.done` — the UNION of phase- and
  sub-phase-granular sentinels, so a phase spanning more than two
  relaunches with only sub-phase progress is not false-aborted.
- **Stage-suffixed sentinels** — on a multi-stage task (`architecture.md`
  has a `## Stage decomposition`), `thorough_plan`, `implement` and `review`
  complete per stage: `{phase}.stage-N.done`, with in-stage progress as
  `{phase}.stage-N.{subphase}.done` (e.g. `implement.stage-4.tasks.done`,
  `implement.stage-5.fix1.done`). Task-level phases keep `{phase}.done`;
  runs that finalized each stage may also have left `end_of_task.stage-N.done`.
  The counting glob covers these too. Legacy runs wrote a separate
  `autonomous-progress-{task}-stage-N/` directory with unsuffixed
  per-stage names instead. The supervisor's repair rule treats `implement.stage-N.tasks.done`
  without `implement.stage-N.done` like `implement.tasks.done` without
  `implement.done`. Full resume rules: `run_hard/SKILL.md` `## Resume` Step 1.
- **Done sentinel** — `autonomous-done-{task}.md`, written by
  `/end_of_task` LAST — after push, the lessons/cost sub-phase, and the
  archive move all complete — and outside the archived folder, so a
  kill between any two of those steps resumes safely with no duplicated
  work.
- **Halt sentinel** — `autonomous-halt-{task}.md` (Stage 1, unchanged).
  The supervisor checks the done sentinel first, then the halt
  sentinel, before deciding whether to relaunch.

All sentinel writes are atomic: `printf '…' | python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py write-atomic "<f>"` (writes `<f>.tmp`, then renames it over `<f>`).

**Unattended staged runs.** `quoin run --autonomous <task> [--project-root
DIR] [--max-budget-usd N] [--dry-run]` reads the task's `## Stage
decomposition` and runs each stage still marked ⏳, in order, as one
`claude -p "/goal <condition>"` session started in the project root with a
fixed `--allowedTools` list (never `--dangerously-skip-permissions`, which an
auto-mode classifier can refuse). The condition is the stage's `Acceptance:`
line plus three requirements the goal's evaluator can see in the transcript:
the stage is committed on the task branch, its row is marked ✅, and the
session printed the output of `affected_tests.py` showing the affected tests
pass. After each session the stage list is read again; a stage left ⏳ stops
the run (exit 1). A task with no stage list runs its `brief.md` as one goal.
Each session's start, end and cost go to `.workflow_artifacts/<task>/goal-run.log`.

**Usage limits.** Claude Code documents no pause-and-resume for a `-p`
`/goal` run at a usage limit. When a session ends on one, the run reports
`paused` and exits 75; rerunning the same command later continues at the
first unfinished stage. For overnight runs that must wait out a limit on
their own, install the `legacy` tag, whose supervisor relaunches with
backoff.

**Sub-phase-granular idempotent resume.** Each fresh relaunch is a
`/run --resume --autonomous <task>` that MUST land at the correct phase
with no re-work and no skipped work. `run_hard`'s resume reads the marker
FIRST (re-establishing `AUTONOMOUS` before any decision point), then
derives the next phase from the `{phase}.done` completion sentinels
rather than from session-state prose alone: a phase whose `{phase}.done`
exists is NEVER re-run; a phase whose `{phase}.done` is absent is NEVER
skipped; a phase with a partial `{phase}.{subphase}.done` set resumes at
the right sub-phase. When no sentinel dir exists (a plain, non-autonomous
resume), the pre-existing session-state resume path is preserved intact.
A headless relaunch raises ZERO `AskUserQuestion` — the marker + the
`--autonomous` flag keep the fresh session autonomous.

**Terminal (`/end_of_task`) step-idempotency.** A kill after
`/end_of_task` pushes but before the done sentinel re-runs the WHOLE
terminal phase, so every side-effecting sub-phase is individually
idempotent, in the verified order push → lessons/cost → archive →
done-sentinel-LAST:

- **Push (Sub-phase A)** is a no-op when the branch already matches
  origin at the same HEAD (fetch, then compare `git rev-parse HEAD`
  against `origin/<branch>`).
- **Lessons + cost (Sub-phase B)** are gated behind an entry-skip
  sentinel `autonomous-progress-{task}/end_of_task.subphaseB.done`
  (checked at Sub-phase B entry, written atomically at its end, and
  counted by the union progress glob). Belt-and-suspenders, the lessons
  append also greps `lessons-learned.md` for an existing entry keyed on
  `{task}` + stage before appending, and cost aggregation recomputes /
  overwrites the task total rather than blind-appending a second row.
- **Archive (Sub-phase C)** checks the `finalized/` target first and
  skips the move if the task folder is already archived.
- **Done sentinel** is written LAST — after the archive move and the
  final report — at the memory-dir location outside the archived folder,
  so a kill at any boundary resumes with no duplicated work. The
  "never auto-create a PR" invariant holds in every mode.

## In-session continuation

A session that runs out of context or budget mid-task stops at its next phase
boundary and writes the halt sentinel; continue it with `/run --resume
<task>`, or, for a staged task, with `quoin run --autonomous <task>`. The
detached hand-off that used to relaunch it (`auto_resume.py`), the child
watcher and `quoin run --takeover` are gone from the default install.

### Headless children never yield with pending work

**Trigger.** Any tool result saying a command is running in, or was moved to, the background, or that it timed out: an explicit background launch, or the harness moving a long foreground command to the background on its own (about 120 s by default; the exact threshold and wording are not pinned here).

**Why.** A headless `claude -p` process may exit as soon as the session ends its turn, so the pending command's result is never seen. A headless child may or may not be re-invoked afterwards; never rely on it. An orphaned command may still finish later, which is why every run is keyed by a token that keeps a stale result from being read.

**Rule.** Under `AUTONOMOUS` the session must not end its turn while such work is pending. For a headless child this holds without exception: when `QUOIN_HEADLESS_CHILD=1` is present in the environment, the session is such a child. `quoin run --autonomous` sets it for every `/goal` session it starts; the prose rule applies whether or not it is set. The only exemption is an interactive session that ends its turn after a hand-off with the child watcher armed. Plain foreground calls are fine only for commands known to finish well under 120 s. `Monitor` is not available to headless children.

**Mechanism.** Start the command detached with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/wait_for.py start --rc-file F --token TOK --log L -- CMD...` (no trailing `&`, no `nohup`; the helper detaches). Then repeat FOREGROUND calls of `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/wait_for.py wait --file F --token TOK --max-secs 540`, each with the Bash tool `timeout` set to 600000, until a terminal line:
- `READY|<rc>` continues with that exit code.
- `WAITING|<secs>` means call `wait` again.
- `DEAD|<secs>`, `EXPIRED|<secs>` or `ERROR|<reason>` are terminal failures: stop waiting, record FAIL with the tail of the log file, never retry the wait loop.
The overall budget is `QUOIN_WAIT_BUDGET_SECS` (default 3600). The pytest bound of `affected_tests.py` (ceiling 3300 s) plus its 300 s margin must stay inside this budget; raise `QUOIN_PYTEST_TIMEOUT` and `QUOIN_WAIT_BUDGET_SECS` together. If the environment sets `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` these are only an extra layer; the prose rule stays the guarantee.

**Headless full-suite recipe.** Token = the gate session id plus the UTC start stamp. Name the rc, log and junit files with the token under `.workflow_artifacts/cache/`. Never start one test suite while the other still runs. In the same Bash call, assign PY with `PY="$(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/affected_tests.py --print-interpreter --interpreter-anchor repo --interpreter-only --project-root "$PROJECT_ROOT")" && [ -x "$PY" ] || { echo "interpreter_reason: helper-unavailable"; PY=python3; }`, then start `"$PY" -m pytest -rA --junitxml="$JUNIT" quoin/` through `wait_for.py start` with the log as `"$RA"`, wait as above, and read the result only after `READY`. Then run `known_red.py` and `gate_fullsuite_sidecar.py record` exactly as the gate's foreground recipe does, on the same files, taking `RC` from `READY|<rc>`. The same rule covers every long test command in an autonomous run, the affected-area suite included. Record the second helper call's `interpreter*` lines (or `helper-unavailable`) in the audit row: the call prints `interpreter`, `interpreter_reason` and `interpreter_anchor`.

**Recipe notes.** `--project-root` is the outer project root that owns `.workflow_artifacts/`, the same convention `branch_hygiene.py` and `deploy_drift_check.py` take as `$(pwd)`; it is never the git repo root when the two differ, and `/end_of_task`'s `check --project-root` must resolve to the identical directory or reuse silently never fires. When the task profile cannot be determined pass `--task-profile medium`, never `large`, because `large` would silently re-enable the Small/Medium auto-pass that this size scoping exists to close. Each Bash call is a fresh shell: write the literal paths (or re-assign `PROJECT_ROOT`, `RCF`, `TOK`, `RA`, `JUNIT` in every call) rather than relying on variables from an earlier call.
