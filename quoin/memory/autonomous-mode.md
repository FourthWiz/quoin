# Autonomous mode reference (`--autonomous`)

> **Sibling primitive (opposite default):** `[autonomous]` carries pre-authorized
> answers so a decision gate auto-resolves; a background context with NO opt-in has no
> pre-authorized answer, so its only safe move is to FAIL CLOSED. The fail-closed guard,
> the `[no-interactive]` sentinel, and the `needs-decision-{task}.md` sentinel (a distinct
> sibling of the `autonomous-halt` family the supervisor never reads) are documented in
> `decision-gate-guard.md` (IVG-150).


Verbose Tier-1 reference for the opt-in `--autonomous` span on `/run`
(IVG-153, `autonomous-run-mode` Stage 1). This file holds the full
auto-resolution table, the sentinel/propagation rule, the Formulation
quality bar, and the halt-sentinel contract. `run/SKILL.md` and the
per-skill `SKILL.md` bodies carry the operative branches; this file is
the single place that documents the whole surface in one pass.

## The `[autonomous]` sentinel and propagation

Propagation is a `[autonomous]` prompt sentinel prefixed onto every
sub-phase spawn prompt — mirroring the existing `[no-session-age-guard]`
and `[no-redispatch]` sentinel conventions — rather than an env var, so
it stays greppable and deterministic. Leading sentinels stack (e.g.
`[no-redispatch] [autonomous]`).

- `run` prefixes `[autonomous]` directly onto every sub-phase spawn it
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
- The run→`end_of_task` edge is a direct edge, not a deeper one: `run`
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

`quoin run --runtime opencode --phase PHASE TASK` runs a single phase on the
OpenCode runtime and is not part of the autonomous span: it never writes the
whole-task halt sentinel, and the task lock it takes carries `runtime:
opencode`. Auto-resume and takeover treat a lock with that runtime as owned by
another runtime, so a task driven this way is never continued on Claude.

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
blocks, across the 10 Opus-tier leaf skills: `architect`, `plan`,
`critic`, `revise`, `review`, `security_review`, `discover`, `specify`,
`enrich`, `init_workflow`. Under `[autonomous]`, the generated clause
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

## Relocated: Exception: `/run` orchestrator

Moved here verbatim from `CLAUDE.md`'s "Workflow sequence" section
(IVG-153 T-17). `CLAUDE.md` now carries only a short pointer back to
this section, to stay under its size ceiling.

**Exception: `/run` orchestrator.** When the user invokes `/run`, they
have explicitly requested the full end-to-end pipeline. `/run` may
invoke `/implement` and `/end_of_task` on the user's behalf, but still
pauses at each gate checkpoint for confirmation before proceeding. The
user's `/run` invocation constitutes the conscious decision;
the gate confirmations provide the safety checkpoints.

Under `--autonomous`, this reliance extends one step further: once the
Formulation quality bar passes, the run stays unattended from that
point through `/end_of_task`, auto-resolving every interactive
checkpoint and body prompt per the auto-resolution table above instead
of pausing for confirmation. Every hard stop defined in the
halt-sentinel contract above still halts the run and records the
reason rather than proceeding silently, and a PR is never auto-created
in either mode.

## Stage 2 — supervisor + sentinel contract

Stage 1 (above) covers the in-session autonomous span: propagation,
the quality bar, and the halt-sentinel *write* side. Stage 2 adds an
external local supervisor — `quoin run --autonomous <task>` — that can
relaunch fresh `claude -p "/run --resume --autonomous <task>"` sessions
to carry a Large task across context windows a single session cannot
fit in. The supervisor is pure orchestration outside the session; it
never edits skill content and reads the sentinel contract below to
decide whether to relaunch.

**Sentinel contract.** All four sentinels resolve under
`.workflow_artifacts/memory/` — outside the task-scoped folder, so
each survives `/end_of_task`'s later move of that folder into
`finalized/`:

- **Marker** — `autonomous-run-{task}.marker` (single line: task,
  timestamp, `autonomous: true`). Written once at autonomous-span
  entry, in `/run`'s Setup, right after `AUTONOMOUS=true` is set.
  `/run --resume` reads it FIRST, before its own first decision point,
  to re-establish autonomous mode — so a headless resume never reverts
  to interactive and stalls. Re-writing the marker is a no-op-equivalent
  overwrite; plain (non-autonomous) `/run` never writes it.
- **Per-phase completion sentinels** —
  `autonomous-progress-{task}/{phase}.done`, one file per completed
  phase, for the FULL resumable `/run` phase roster (`run/SKILL.md`
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
- **Done sentinel** — `autonomous-done-{task}.md`, written by
  `/end_of_task` LAST — after push, the lessons/cost sub-phase, and the
  archive move all complete — and outside the archived folder, so a
  kill between any two of those steps resumes safely with no duplicated
  work.
- **Halt sentinel** — `autonomous-halt-{task}.md` (Stage 1, unchanged).
  The supervisor checks the done sentinel first, then the halt
  sentinel, before deciding whether to relaunch.

All sentinel writes are atomic: `printf '…' | python3 __QUOIN_HOME__/scripts/fsops.py write-atomic "<f>"` (writes `<f>.tmp`, then renames it over `<f>`).

**Supervisor loop.** `quoin run --autonomous <task>` runs a pure
relaunch loop: on each iteration, check the done sentinel (exit
SUCCESS if present), then the halt sentinel (exit HALTED with the
recorded reason, no further relaunch), then the relaunch cap
(`MAX_RELAUNCH`, exit ABORTED "relaunch cap" if reached). Otherwise it
counts completion sentinels and snapshots the task-branch commits,
relaunches a fresh headless session, and re-checks. A launch made
progress when a new completion sentinel appeared (by the union glob
above) or a new commit landed on the task branch. The commit probe looks
at repos with their own `.git` entry at the project root or in its
immediate child dirs whose current branch equals the task name or ends
with `/` plus it, under one 5 s budget; any failure reads as no commit
signal, and a branch switch or a repo appearing or disappearing never
counts. `QUOIN_SUPERVISOR_HEAD_PROBE=0` disables the commit signal.
Two consecutive launches without progress abort with
"no forward progress". A phase that has its completion marker
(`implement.tasks.done`) but not `{phase}.done` gets up to
`QUOIN_SUPERVISOR_REPAIR_RELAUNCHES` (default 2, clamp 0..5, `0`
restores the old timing) extra relaunches first; if it is still
unrepaired the abort reason is `phase completion not repaired: <phase>`.
A run that hangs on every launch can hold the lock for up to the relaunch cap times the launch timeout, about 15 h at the defaults; the child watcher reports a stall after 30 min.
The supervisor never writes any `.done` file, and the relaunch cap
still bounds everything. Each launch has a timeout of
`QUOIN_SUPERVISOR_LAUNCH_TIMEOUT_SECS` (default 5400, clamp 900..14400),
and every child's environment carries `QUOIN_HEADLESS_CHILD=1`.
Backoff between relaunches is exponential, capped. `MAX_RELAUNCH` alone
guarantees termination even under continual sub-phase progress. The
relaunch string always carries `--autonomous` (belt) alongside the
marker (suspenders), so the fresh session's mode is never ambiguous.
The relaunch subprocess uses the T-01 POC's scoped `--allowedTools`
permission mode by default (`--permission-mode` overrides), runs with
`stdin` redirected from `/dev/null`, and resolves its cwd via
`git rev-parse --show-toplevel` rather than assuming the project root
is itself a git repo.

**`quoin run --autonomous <task>` CLI.** Flags: `task` (positional),
`--autonomous`, `--project-root` (default cwd), `--max-relaunch`
(default 10), `--permission-mode` (default `allowedTools`), and
`--budget` — a nice-to-have cross-session cost ceiling that is a
**no-op stub this release: NOT YET ENFORCED — cost is bounded by
`--max-relaunch` + backoff only.** Exits 0 on SUCCESS, 1 on HALTED, 2
on ABORTED. `--takeover` (see "Taking over a headless child" below) stops
a running supervisor and child instead of starting a run, and cannot be
combined with `--autonomous`.

**Sub-phase-granular idempotent resume.** Each fresh relaunch is a
`/run --resume --autonomous <task>` that MUST land at the correct phase
with no re-work and no skipped work. `/run --resume` reads the marker
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

## In-session continuation and hand-off

Stage 2 above covers the EXTERNAL supervisor. This section covers a
second, cheaper continuation path that runs INSIDE the owning
conversation before any supervisor gets involved: a Stop hook (`stop.sh`)
that notices an interrupted span and nudges the same session to keep
going, escalating to a detached hand-off only once the in-session budget
for that is spent.

**Arm / disarm — a consent model, not a lock.** `/run`'s Setup arms the
owning session (`--entry fresh`) right after the autonomous-span marker
is written; `/run --resume` re-arms it (`--entry resume`) at its own
Step 0. The arm is a file, `run-continue-arm-<sid>.txt`, named after the
session that holds it. Any human-typed prompt in that session disarms it
immediately — `userpromptsubmit.sh` removes the arm on every
`UserPromptSubmit` except one whose `transcript_path` is a subagent
transcript, or whose `prompt` begins with a recorded harness-notification
prefix (currently just `<task-notification>`; see the finding for
probe (a2), which found no other source to add). A person typing
anything at all — even "hold on" — always takes control back; this is a
liveness cost, never a correctness risk, since disarming only ever
stops a continuation that would otherwise have happened. `sessionend.sh`
also drops the arm when a session ends, and separately writes
`session-ended-<sid>.txt`.

**Stop continuation.** When the owning session's Stop hook fires and its
arm is present, `auto_resume.py stop` either emits an in-session
continuation block (the model is told to keep going with the exact
`resume_command` from the record) or, once the chain of blocks reaches
`QUOIN_AUTO_RESUME_HANDOFF_AT`, attempts a hand-off to a detached
supervisor instead of blocking again.

**Startup hand-off and owner liveness.** On `SessionStart`
(`startup`/`resume`), `auto_resume.py start` checks whether the record's
owning session is `gone` (an `session-ended-<sid>.txt` marker exists, or
its transcript — and the widest mtime under its subagent tree — has been
idle at least `QUOIN_AUTO_RESUME_IDLE_SECS`), `live` (recent transcript
activity), or `unknown` (no transcript found by glob). Only `gone`
authorizes a hand-off; both `live` and `unknown` refuse, fail-safe
against ever double-driving the same task from two sessions at once.

**The budget (D-01).** One unit = one continuation event: an in-session
Stop block, a hand-off, or one headless supervisor launch. A hand-off
charges its own unit AND must leave room for at least one launch, so it
is allowed only when `attempts_before + 2 <= QUOIN_AUTO_RESUME_MAX`; its
grant is `G = QUOIN_AUTO_RESUME_MAX - attempts_before - 1` (floored at
1). The Stop path's in-session fallback needs only
`attempts_before + 1 <= QUOIN_AUTO_RESUME_MAX`. Across every combination
of in-session blocks, hand-offs, and headless launches, `attempts` never
exceeds `QUOIN_AUTO_RESUME_MAX` for the span. `attempts` never decays
within a span — it is a hard per-span ceiling, even while `.done`
sentinel counts keep rising — because only a human typing a fresh `/run`
prompt starts a new span (the consent rule below); a task that keeps
making real progress but keeps exhausting its continuation budget is a
sign the budget itself needs raising, not a bug to route around.

**Knobs and defaults:** `QUOIN_AUTO_RESUME` (`0` disables; default on),
`QUOIN_AUTO_RESUME_MAX` (default 10, clamp 1..100), `QUOIN_AUTO_RESUME_IDLE_SECS`
(default 900, min 60), `QUOIN_AUTO_RESUME_HANDOFF_AT` (default 6, clamp 1..7);
supervisor knobs `QUOIN_SUPERVISOR_REPAIR_RELAUNCHES` (default 2, clamp 0..5),
`QUOIN_SUPERVISOR_LAUNCH_TIMEOUT_SECS` (default 5400, clamp 900..14400),
`QUOIN_SUPERVISOR_HEAD_PROBE` (`0` disables the commit signal).

The Stop and hand-off gates apply the same progress and repair rules as
the supervisor. The Stop hook and the SessionStart path skip the commit
probe to stay inside their hook time budgets; only the `handoff`
subcommand probes. The counter file gains `last_heads` and
`repairs_used`; `repairs_used` also resets on progress, and both otherwise reset only by the consent rule.

**Halt reasons** (any of these is terminal — no further continuation):
`auto-resume cap`, `no forward progress`, `relaunch cap`, `session age cap`,
`context exhaustion`, `paused by user`, `taken over by user`,
`supervisor stopped by signal`, `supervisor error`,
`phase completion not repaired: <phase>`.

**The harness's own block cap is an independent outer bound.** Claude
Code itself stops honoring a Stop hook's `"decision": "block"` response
after a fixed number of consecutive fires per turn (observed 8; see the
finding's probe (a)). `QUOIN_AUTO_RESUME_HANDOFF_AT` (default 6) is
chosen to hand off to a detached supervisor before that harness cap is
ever reached, so the two bounds cooperate rather than race.

**A user-typed `quoin run --autonomous` without `--halt-on-abort` leaves
no halt on abort** — that flag is what the automatic hand-off path adds
specifically so a detached run never goes silently stuck. Without it, an
aborted run leaves no sentinel at all, so the next session start may
legitimately hand the same run off again within the remaining budget.

**Consent rule.** Only a typed `/run` prompt (in the owning session,
matched at `userpromptsubmit.sh`, tolerating leading whitespace and both
`/run <task>` and `/run --resume <task>`) resets the continuation budget
by writing `run-continue-consent-<sid>.txt`, consumed by the next `arm`.
Every other typed prompt disarms without resetting anything.

**Liveness residual.** An owner session waiting at a permission prompt
for the full idle window reads as `gone` even though a human may return
to it — there is no way to distinguish "abandoned" from "paused at a
prompt" from outside the session. If a hand-off fires while you are still
there: answer or cancel the pending prompt in the original session, or
kill the pid named in the hand-off notice.

**Opt-out.** `QUOIN_AUTO_RESUME=0` disables the helper entirely; every
path then behaves exactly as it did before this feature, and no halt is
ever written by it.

**State files** (D-02, all under `.workflow_artifacts/memory/`, outside
the task folder): `run-continue-arm-<sid>.txt`, `run-continue-consent-<sid>.txt`,
`session-ended-<sid>.txt`, `auto-resume-<task>.json` (the continuation
counter), `run-supervisor-<task>.pid`/`.result`/`.log` (the single-driver
lock and its outcome), `child-watch-<task>.json` (the parent's child-watch window state).

**Un-registering the continuation hook** (if you need to disable it at
the install level rather than via the opt-out knob above): see the Hooks
Guide's reference entry for the ninth stanza.

### Headless children never yield with pending work

**Trigger.** Any tool result saying a command is running in, or was moved to, the background, or that it timed out: an explicit background launch, or the harness moving a long foreground command to the background on its own (about 120 s by default; the exact threshold and wording are not pinned here).

**Why.** A headless `claude -p` process may exit as soon as the session ends its turn, so the pending command's result is never seen. A headless child may or may not be re-invoked afterwards; never rely on it. An orphaned command may still finish later, which is why every run is keyed by a token that keeps a stale result from being read.

**Rule.** Under `AUTONOMOUS` the session must not end its turn while such work is pending. For a headless child this holds without exception: when `QUOIN_HEADLESS_CHILD=1` is present in the environment, the session is such a child. The supervisor sets it for every headless child it launches; the prose rule applies whether or not it is set. The only exemption is an interactive session that ends its turn after a hand-off with the child watcher armed. Plain foreground calls are fine only for commands known to finish well under 120 s. `Monitor` is not available to headless children.

**Mechanism.** Start the command detached with `python3 __QUOIN_HOME__/scripts/wait_for.py start --rc-file F --token TOK --log L -- CMD...` (no trailing `&`, no `nohup`; the helper detaches). Then repeat FOREGROUND calls of `python3 __QUOIN_HOME__/scripts/wait_for.py wait --file F --token TOK --max-secs 540`, each with the Bash tool `timeout` set to 600000, until a terminal line:
- `READY|<rc>` continues with that exit code.
- `WAITING|<secs>` means call `wait` again.
- `DEAD|<secs>`, `EXPIRED|<secs>` or `ERROR|<reason>` are terminal failures: stop waiting, record FAIL with the tail of the log file, never retry the wait loop.
The overall budget is `QUOIN_WAIT_BUDGET_SECS` (default 3600). The pytest bound of `affected_tests.py` (ceiling 3300 s) plus its 300 s margin must stay inside this budget; raise `QUOIN_PYTEST_TIMEOUT` and `QUOIN_WAIT_BUDGET_SECS` together. If the environment sets `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS` these are only an extra layer; the prose rule stays the guarantee.

**Headless full-suite recipe.** Token = the gate session id plus the UTC start stamp. Name the rc, log and junit files with the token under `.workflow_artifacts/cache/`. Never start one test suite while the other still runs. In the same Bash call, assign PY with `PY="$(python3 __QUOIN_HOME__/scripts/affected_tests.py --print-interpreter --interpreter-anchor repo --interpreter-only --project-root "$PROJECT_ROOT")" && [ -x "$PY" ] || { echo "interpreter_reason: helper-unavailable"; PY=python3; }`, then start `"$PY" -m pytest -rA --junitxml="$JUNIT" quoin/` through `wait_for.py start` with the log as `"$RA"`, wait as above, and read the result only after `READY`. Then run `known_red.py` and `gate_fullsuite_sidecar.py record` exactly as the gate's foreground recipe does, on the same files, taking `RC` from `READY|<rc>`. The same rule covers every long test command in an autonomous run, the affected-area suite included. Record the second helper call's `interpreter*` lines (or `helper-unavailable`) in the audit row: the call prints `interpreter`, `interpreter_reason` and `interpreter_anchor`.

**Recipe notes.** `--project-root` is the outer project root that owns `.workflow_artifacts/`, the same convention `branch_hygiene.py` and `deploy_drift_check.py` take as `$(pwd)`; it is never the git repo root when the two differ, and `/end_of_task`'s `check --project-root` must resolve to the identical directory or reuse silently never fires. When the task profile cannot be determined pass `--task-profile medium`, never `large`, because `large` would silently re-enable the Small/Medium auto-pass that this size scoping exists to close. Each Bash call is a fresh shell: write the literal paths (or re-assign `PROJECT_ROOT`, `RCF`, `TOK`, `RA`, `JUNIT` in every call) rather than relying on variables from an earlier call.

### Taking over a headless child

Each headless child the supervisor launches gets a session id chosen in
advance (`claude -p ... --session-id <uuid>`), recorded before the child
starts: in the run-state record (`child_session_id`, `child_cwd`,
`child_started_at`), in the supervisor lock (plus `child_pid` once the
process exists) and as a `[quoin-autonomous-child]` line in the run notes.
A hand-off pre-generates the first child's id, passes it to the
supervisor through `QUOIN_FIRST_CHILD_SESSION_ID`, and returns it as a
fourth field of the `HANDOFF|<pid>|<n>/<cap>|<uuid>` result.

To work on the run yourself, run `quoin run --takeover <task>`
(`--project-root` as usual). It:

1. writes a halt (`taken over by user`) so nothing relaunches, keeping any
   halt already present;
2. stops the supervisor, then the child (SIGTERM, then SIGKILL after
   `QUOIN_TAKEOVER_GRACE_SECS`, default 5, range 1..60; the wait after
   SIGKILL is `QUOIN_TAKEOVER_WAIT_SECS`, default 20, range 1..300);
3. confirms nothing carrying the child's session id is still running;
4. prints `cd '<cwd>' && claude --resume <uuid>`.

Exit codes: 0 the resume command was printed; 1 no child was recorded or
none ever started; 2 invalid task name; 4 a process survived or could not
be verified, in which case no resume command is printed. Only the
processes that carry the child's session id are signalled (no
process-group kill), after their command line has been checked.

Hand-off notices and halt files carry the child's session id and the
`quoin run --takeover <task>` pointer. The concrete `cd ... && claude
--resume` command appears only in the supervisor's terminal output (the
loop has ended, so no child is alive) and after `--takeover` has confirmed
the child is stopped. `relaunches` in the run result still counts launch
attempts, including one skipped because a halt appeared first.

### Watching a handed-off child

**Command.** `python3 __QUOIN_HOME__/scripts/child_watch.py --project-root <root> --task <task> --supervisor-pid <pid> [--child-session <uuid>] [--once]`. One call is one watch window (default 10 minutes): it polls the run's files, prints exactly one `WATCH|...` line and exits; the parent re-arms it with the `next=` command until a terminal state. `--once` checks without waiting and writes nothing. It compares progress against the watcher's stored baseline only when given the same `--supervisor-pid` as the armed watcher, because without it the key is the lock's current pid, which differs after a supervisor replacement, and the check then reports ALIVE without a progress comparison. A `--once` check may also report STALL one window early.

**Signals.** Each window looks at six things: the done sentinel, the halt sentinel, the needs-decision sentinel (only a new or rewritten file counts), new `.done` completion sentinels under the progress directory, new commits on the task branch, and supervisor and child liveness.

**States.** Terminal (exit 10, `next=report-and-stop`): DONE (done sentinel present), HALTED (halt sentinel, with its reason), NEEDS_DECISION (a new decision request), DEAD (no live supervisor or child), EXPIRED (watched longer than the max hours). Non-terminal (exit 0, `next=` is the exact re-arm command): PROGRESS (a new completion sentinel or task-branch commit), ALIVE (running, nothing new), STALL (nothing new for the stall window count). ERROR (exit 2) reports invalid input. Every line ends with the `observe-only` field and carries a `takeover=` pointer.

**Liveness.** A pid counts only when the supervisor lock names it. Where `ps` works, the supervisor command line must contain `quoin` and the task name, and the child command line the lock's current child session id. DEAD is reported only after a 5 s grace re-read of the sentinels, so a late done or halt wins.

**Stall.** No new `.done` and no task-branch commit for `QUOIN_CHILD_WATCH_STALL_WINDOWS` windows (default 3, so 30 minutes) is reported once as STALL and watching continues; any progress re-arms the report.

**Observe-only.** The watcher and the parent never kill, relaunch, take over or do phase work; the supervisor lock owns the run. A message the user types in the parent ends re-arming.

**No monitor tool.** A parent with no background-capable tool prints the notice plus a `--once` command to run by hand.

**Knobs.** `QUOIN_CHILD_WATCH_INTERVAL_SECS` (default 600, clamp 60..1500), `QUOIN_CHILD_WATCH_STALL_WINDOWS` (default 3, clamp 1..48), `QUOIN_CHILD_WATCH_MAX_HOURS` (default 12, clamp 1..72).

**Hooks.** While the watcher runs as a background command the Stop hook stands down because the stop payload lists background work; a watcher re-invocation arrives as a `<task-notification>` prompt and does not disarm the run. The Stop and SessionStart hand-offs have no parent turn to arm a watcher; use `--once` there.

### How the hand-off finds the CLI

Every hand-off above that starts a detached supervisor — the Stop hook's
escalation, the SessionStart hand-off for a run whose owner is gone, and
`/run`'s own `handoff` subcommand — needs to run the `quoin` CLI as a
real subprocess, without knowing in advance how it was
installed (a plain venv, `pip install --user -e`, `uv tool install`,
`pipx`, or a bare source checkout on `PYTHONPATH`). It does this by reading
an install record instead of guessing via `PATH`.

**The record.** `quoin install` writes `quoin-runtime.json` at the deploy
root (`~/.claude/` in user scope, `<project>/.claude/` in project scope)
as its last step, atomically (write-temp-then-rename). Fields: `schema`
(currently `1`), `python` (`sys.executable`, verbatim — a venv's own
symlink, not resolved), `version` (the installed `quoin.__version__`),
`pythonpath` (the directory to add to `PYTHONPATH` before importing
`quoin`, or `null` when the recorded interpreter can import it unaided),
`quoin_file` (the resolved path `quoin.__file__` pointed at when the
record was written), `source_dir` (the source tree the install ran from),
`source_version` (the version string in that source tree's
`__about__.py`, when it differs from the installed CLI's own — for
example a deployed release running against a newer checkout), and
`installed_at`.

**The launch.** A hand-off relaunches `<python> -c <bootstrap> run
--autonomous …` from the filesystem root (never the caller's own cwd —
see the neutral-cwd note in the resolver's own module docstring), passing
`--project-root` explicitly since the relaunch's cwd is no longer the
project. Legacy PATH lookup (`shutil.which("quoin")`, falling back to
`~/.local/bin/quoin`) fires only when NO record exists at all — a record
that exists but fails validation is never treated as "no record"; it
refuses instead (below), so a bad record can never silently fall back to
whatever happens to be on `PATH`.

**Refusal: `STALE_CLI|<kind>|<message>`.** When the record exists but the
resolver can't stand behind it, every caller sees the same
`STALE_CLI|<kind>|<message>` shape, with `<kind>` one of:

- `record-invalid` — the record file is missing, unreadable, or not
  valid JSON with the expected fields. Remedy: reinstall.
- `interpreter-missing` — the recorded `python` path no longer exists.
  Remedy: reinstall.
- `interpreter-not-executable` — the recorded `python` exists but isn't
  executable, or the probe subprocess itself couldn't start. Remedy:
  reinstall (fix the interpreter's permissions, or reinstall from a
  working one).
- `import-failed` — the version probe ran but exited non-zero, or
  produced no parseable version token. Remedy: reinstall.
- `version-mismatch` — either the recorded `source_version` disagrees
  with the recorded `version` (checked before any probe runs, since no
  probe result could change that answer), or the live probe's reported
  version disagrees with the record. Remedy: reinstall with the source
  tree you actually mean to run.
- `probe-timeout` — the version probe didn't answer within its budget
  (the `handoff` caller retries once before giving up). Remedy: retry,
  or raise `QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS` if this interpreter is
  reliably slow to start.
- `no-safe-cwd` — the filesystem root isn't a safe place to run the
  probe from (it must be a real directory owned by root or by you, with
  no group- or other-write bit). Remedy: fix `/`'s ownership or
  permissions (for example `chmod go-w /`); reinstalling will not help.
- `resolver-error` — any other unexpected failure inside the resolver
  itself (for example a temp-file creation error), caught so the caller
  always gets a `STALE_CLI` shape instead of an unhandled exception.
  Remedy: reinstall; if it recurs, it's a resolver bug.

Each caller surfaces a stale record differently: the in-session hand-off
path treats it as a halt reason (see the halt-reasons list above); the
`SessionStart` hook turns it into an advisory (never blocks startup); the
`Stop` hook includes it in its `systemMessage` alongside the ordinary
in-session continuation nudge — the nudge itself is unchanged, since a
stale CLI only affects the detached-hand-off path, not staying in the
current session; and a hand-off attempt logs the halt line to the run
notes. `quoin doctor`'s "Auto-resume CLI" block (see the Hooks Guide)
runs the same deployed resolver in a scrubbed environment (no inherited
`PYTHONPATH`, minimal `PATH`), so its verdict matches a hook running with
a clean environment. Two differences remain: a hook inherits Claude
Code's environment, which may carry a `PYTHONPATH` doctor scrubs, and
doctor probes with the 8 s hand-off budget, so a slow interpreter can
pass doctor yet time out under SessionStart's 1.5 s or the Stop hook's
3 s budget.

**Probe budgets.** Each caller gets its own time-bounded budget for the
version probe, controlled by one knob,
`QUOIN_AUTO_RESUME_PROBE_TIMEOUT_MS`, which sets both hook-bound values
at once: `start` (`SessionStart`) defaults to 1500 ms, clamped to
250–3000 ms; `stop` (`Stop`) defaults to 3000 ms, clamped to 250–7000 ms.
The `handoff`/`cli-check` budget is a fixed 8000 ms and ignores the knob
entirely — a hand-off is already a deliberate, one-time transition, not a
per-turn hook, so it can afford to wait longer, and it retries a timed-out
probe once before refusing.

**The version-bump trade-off.** Every `quoin` version bump makes existing
hand-offs refuse with `version-mismatch` until `quoin install` is re-run
— the install record is a snapshot, not a live pointer, so a bump alone
(even with no source-tree change otherwise) invalidates it. This is
deliberate: it's the only way to guarantee a hand-off relaunches the CLI
version it actually validated. The `Stop` nudge keeps working regardless,
since it stays in-session and only the detached hand-off path consults
the record.

**Scope leakage.** A `pythonpath` from a Tier-2-style record reaches only
the detached supervisor process itself — `quoin run` strips
`PYTHONPATH`/`QUOIN_HANDOFF_PYTHONPATH` from its own environment before
relaunching `claude`, so the child Claude session never inherits it.
Project-scope records hold machine-specific absolute paths (the
interpreter, the source tree); they're written under `<project>/.claude/`
and, like the rest of that tree, aren't meant to be portable across
machines or committed.
