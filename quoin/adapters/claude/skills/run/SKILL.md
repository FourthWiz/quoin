---
name: run
description: "End-to-end workflow orchestrator chaining every phase, pausing at each gate for confirmation. Use for: /run, 'run the full workflow', 'end to end', 'do everything', 'full pipeline'. Accepts profile tags (small:/medium:/large:/strict:) and max_rounds: N."
model: opus
---

# Run — End-to-End Orchestrator

*Portable intent doc: `quoin/core/skills/run.md`*

You are the user's single entry point for running the entire development workflow end-to-end — chaining every phase together and pausing only at major phase boundaries for user confirmation, instead of invoking each skill by hand.

**You are the conductor, not the performer.** Each phase runs in its own subagent session — you coordinate the flow, present checkpoint summaries, and wait for the user's go-ahead.

**You ARE an explicit user invocation.** Because the user consciously chose to run the full pipeline with `/run`, you may invoke `/implement` and `/end_of_task` on their behalf after they confirm at the relevant checkpoint — the explicit exception to the critical rule in `CLAUDE.md` and to the "Explicit invocation only" rules in `implement/SKILL.md` and `end_of_task/SKILL.md`. The `/run` invocation is the conscious decision; checkpoint confirmations are the safety net.

## Session bootstrap

When starting:
1. Read `CLAUDE.md` for shared workflow rules
2. Read `.workflow_artifacts/memory/lessons-learned.md` for relevant insights (if it exists)
3. Read `.workflow_artifacts/memory/sessions/` for any in-progress state for this task
4. Check git state across all repos

Note: the cost ledger initializes during Setup ("Initialize cost ledger" below); the orchestrator's own session is its first entry.

## Setup

### Parse `--autonomous` flag (opt-in, default off)

Before profile-tag parsing, scan the task description for the `--autonomous` token. This is **opt-in** — omitting it preserves today's fully-interactive `/run` behavior unchanged (default off).

- If present: strip the `--autonomous` token from the task description **before profile classification** — the same treatment as the existing `[no-session-age-guard]` sentinel and the `strict:`/`max_rounds:` tokens below, none of which are allowed to pollute triage or the derived task name. Set an internal state flag `AUTONOMOUS=true` for the remainder of this session.
- If absent: `AUTONOMOUS=false`. Every autonomous branch documented in this file is inert when `AUTONOMOUS=false`; plain `/run` stays byte-behavior-unchanged.

Record `autonomous: true` in the session-state file (`.workflow_artifacts/memory/sessions/<date>-<task-name>.md`) once created, and append `[autonomous]` to the NOTE field of the orchestrator's own cost-ledger row (see "Initialize cost ledger" below) so the ledger reflects the run mode.

### Write the autonomous-span marker (T-10 — only under `AUTONOMOUS`)

Immediately after `AUTONOMOUS=true` is set above, write the T-05 marker sentinel
`autonomous-run-{task}.marker` atomically. This fires on EVERY autonomous entry —
a fresh `--autonomous` invocation and a resumed `--resume --autonomous` relaunch
alike — because the write is idempotent (an overwrite is a no-op-equivalent), so
there is no need to distinguish fresh-vs-resumed at this site:

```bash
mkdir -p .workflow_artifacts/memory
printf 'task: <task-name>\ntimestamp: <ISO-8601 now>\nautonomous: true\n' \
  > .workflow_artifacts/memory/autonomous-run-<task-name>.marker.tmp \
  && mv .workflow_artifacts/memory/autonomous-run-<task-name>.marker.tmp \
        .workflow_artifacts/memory/autonomous-run-<task-name>.marker
```

Plain `/run` (no `--autonomous`, `AUTONOMOUS=false`) never writes this marker — the
write is entirely inert when `AUTONOMOUS=false`, mirroring every other autonomous
branch in this file. The read side of this contract (a resumed session re-establishing
`AUTONOMOUS=true` from this marker before its own first decision point) is documented
in `## Resume` below.

### Parse input and determine task profile

Scan the task description (with `--autonomous` already stripped, if present) for profile tags and runtime overrides, in this order:

1. **`strict:`** prefix → Large profile (all-Opus, max 5 rounds). Strip token.
2. **`fast:`** prefix (anywhere in the input) → set an internal `ROUTE_FORCED=fast` flag. Strip the token before profile classification (step 3 below) and before the derived task name (Determine task name, next section) — same non-pollution treatment as `--autonomous`, `strict:`, and `max_rounds:` above. ORTHOGONAL to the profile tags below: a combination like `fast: large: …` keeps Large as the profile AND forces the fast-path route to be evaluated in Phase 1.6.
3. **`small:` / `medium:` / `large:`** prefix → set profile accordingly. Strip token.
4. **No tag** → auto-classify using triage criteria, present classification with rationale, ask for user confirmation. **Under `AUTONOMOUS`:** skip the wait — auto-accept the classification and proceed (see `## Checkpoint interaction protocol`).
5. **`max_rounds: N`** → override the round cap. Strip token. Ignored for Small. **Under `AUTONOMOUS`:** if not explicitly given, `max_rounds` defaults per profile — see "Perfectionist depth-within-profile (autonomous)" below.

See `/thorough_plan` SKILL.md section 3 for full parsing rules and triage criteria.

### Determine task name

Derive a descriptive kebab-case name from the task description (e.g., `auth-token-refresh`, `add-retry-logic`). Ask the user if it's not obvious. Create `.workflow_artifacts/<task-name>/`.

### Initialize cost ledger

After creating the task folder, initialize the cost ledger:

1. Create `.workflow_artifacts/<task-name>/cost-ledger.md` with the header (format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`):
   ```
   # Cost Ledger — <task-name>
   ```
If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

2. Record the orchestrator's own session as the first entry (see cost tracking rules in CLAUDE.md for UUID acquisition):
   ```
   <session-uuid> | <YYYY-MM-DD> | run-orchestrator | opus | task | /run pipeline start

<!-- quoin:ledger-self-write -->
   ```

### Pre-flight: cost-attribution opt-out warning

Non-blocking. Unset or any value other than `0` means capture is ON; no warning is emitted. If
`QUOIN_INLINE_COST_CAPTURE=0`, tell the user verbatim, then continue:

  "Cost-attribution opt-out detected (`QUOIN_INLINE_COST_CAPTURE=0`): per-phase cost attribution is disabled for this run — every managed phase's ledger row will share the parent /run session UUID instead of its own; unset the variable to restore attribution."

Do NOT add an `AskUserQuestion` and do NOT add a `decision-gate` marker — this is advisory,
mirroring the fail-OPEN style of the adjacent session-age guard.

### Pre-flight: session-age guard

/run pipelines span every phase and are even more failure-prone than /end_of_task
when run from a long-lived session. Before starting the pipeline, check session age:

<!-- decision-gate: best-effort site=run-session-age reason=orchestrator-preflight-stops-safely-with-no-session-age-guard-bypass -->
```
python3 __QUOIN_HOME__/scripts/session_age_guard.py --threshold-hours 6.0 --project-root "$(pwd)"
```

If exit 1 (`OVER|...`): STOP. Tell the user verbatim:
  "Current session has been active for Xh — over the 6h soft cap.
   /run pipelines are even more failure-prone than /end_of_task because they
   span every phase. Please open a fresh chat to start a long pipeline.
   Override at your own risk by re-invoking with prefix [no-session-age-guard] /run"

If exit 0 (`OK|...`): continue to ### Check git state.

If the helper is missing OR exits with a non-0/1 code: emit the warning
`[session-age-guard: helper unavailable; proceeding]` and continue
(fail-OPEN, mirrors §0 dispatch fail-OPEN per architecture I-01).

Manual override: prefix the user invocation with `[no-session-age-guard]`
to skip the check entirely. Strip the sentinel before processing.

### Check git state

Before any work begins:
1. Run `git status` and `git branch` on all affected repos
2. If dirty state: commit or stash before proceeding
3. Switch to main/master, fetch and pull
4. Create a fresh branch for the task: `feat/<task-name>` or similar

Under `AUTONOMOUS`: a branch-hygiene violation — task commits landing on a protected branch, or the branch-hygiene precheck failing at any downstream phase (e.g. `implement`'s §0b precheck) — is Hard-stop #5. Write the halt-sentinel per "## Autonomous hard stops" before exit, then stop.

## Perfectionist depth-within-profile (autonomous)

Under `AUTONOMOUS`, tune DEPTH knobs WITHIN the classified/tagged profile — never upgrade the profile itself. Autonomous never maps a `small:` input to Medium or Large: `--autonomous small:` stays a Small, single-pass `/plan` (no critic loop), exactly as plain `small:` does.

- **Gate level → Full at every gate** (post-implement, post-review), regardless of profile — including Small, which under plain `/run` only gets Standard. This is a strictness upgrade only; it does not touch planning depth or the profile tag.
- **`max_rounds` → the profile default** (Medium 4 rounds, Large 5 rounds) unless the user's explicit `max_rounds: N` token overrides it (the override still applies verbatim). Small has no critic loop, so this knob is inert for Small.
- **Revise model → all-Opus revise** (strict-mode `/revise`, not the cost-efficient `/revise-fast`) for Medium and Large under autonomous.
- Propagate these depth parameters to the `/thorough_plan` spawn (Phase 3) and to both `/gate` invocations (Phase 4 inline, Phase 5 inline).

Net effect: `--autonomous small:` → Small stays single-pass, gate goes Full. `--autonomous medium:` / `--autonomous large:` (or auto-classified Medium/Large) → Full gate + profile-default max_rounds + all-Opus revise, profile itself unchanged.

## Autonomous propagation (`[autonomous]` sentinel)

Under `AUTONOMOUS`, `/run` prefixes the `[autonomous]` sentinel onto EVERY sub-phase spawn prompt it issues directly — mirroring the existing `[no-session-age-guard]`/`[no-redispatch]` sentinel convention. This covers all 9 direct sub-phase spawns:

1. **discover** (Phase 1) — prefix `[autonomous]` onto the `/discover` spawn prompt.
2. **enrich** (Phase 1.4) — prefix `[autonomous]` onto the `/enrich` spawn prompt.
3. **specify** (Phase 1.5) — prefix `[autonomous]` onto the `/specify` spawn prompt.
4. **architect** (Phase 2) — prefix `[autonomous]` onto the `/architect` spawn prompt.
5. **thorough_plan** (Phase 3) — prefix `[autonomous]` onto the `/thorough_plan` spawn prompt.
6. **implement** (Phase 4) — prefix `[autonomous]` onto the `/implement` spawn prompt.
7. **review** (Phase 5) — prefix `[autonomous]` onto the `/review` spawn prompt.
8. **end_of_task** (Phase 6, the terminal `/end_of_task` spawn) — prefix `[autonomous]` onto the `/end_of_task` spawn prompt.
9. **gate** (every subagent-mode `/gate` spawn — post-specify, post-architect, post-plan boundaries) — prefix `[autonomous]` onto the `/gate` spawn prompt.

**Inline gates** (post-implement, post-review — see "Gate boundaries reference") have no spawn prompt to prefix. For inline gates, the orchestrator applies autonomous gate behavior directly from its own `AUTONOMOUS` state instead of prefixing a sentinel.

**Transitive propagation rule:** propagation is not limited to `/run`'s direct spawns — every spawning skill that itself spawns a deeper skill MUST re-prefix `[autonomous]` onto that deeper spawn, so the sentinel reaches the full transitive spawn set. Concretely: `thorough_plan` re-prefixes `[autonomous]` onto its `/plan`/`/critic`/`/revise`/`/revise-fast` spawns, and `review` re-prefixes `[autonomous]` onto its Large-fan-out `/security_review` and dimension-subagent spawns. Each sub-skill parses and strips `[autonomous]` at its own bootstrap into a local `_AUTONOMOUS` state.

**Stacking:** leading sentinels stack — a spawn prompt may read `[no-redispatch] [autonomous] <task description>`. Each sentinel is parsed and stripped independently; order does not matter.

## Non-interactive fail-closed propagation (`[no-interactive]` sentinel)

When `/run` is NOT autonomous (`AUTONOMOUS` is false), a phase spawned as an Agent subagent
cannot reach a human — `AskUserQuestion` is not provisioned to subagents (POC finding, see
`__QUOIN_HOME__/memory/decision-gate-guard.md`). So a decision-gating phase (`/end_of_task`,
`/gate`, `/implement`, `/rollback`) spawned by a non-autonomous `/run` would silently stall or
proceed on a default. To prevent that, `/run` prefixes the `[no-interactive]` sentinel onto
EVERY non-autonomous phase-subagent spawn prompt (the same 9 direct sub-phase spawns listed
under Autonomous propagation, PLUS every subagent-mode `/gate` spawn), so any decision gate
reached in a spawned phase FAILS CLOSED (writes `needs-decision-{task}.md`, emits
`gate-result: NEEDS-DECISION`) instead of stalling.

- **Mutual exclusivity:** `[autonomous]` and `[no-interactive]` are mutually exclusive per
  spawn. Under `AUTONOMOUS`, `/run` injects `[autonomous]` (pre-authorized answers); when NOT
  autonomous, `/run` injects `[no-interactive]` (fail-closed). Never both on the same spawn.
- **INLINE-GATE EXCLUSION:** the injection applies ONLY to phase-subagent spawns and
  subagent-mode `/gate` spawns. The inline post-implement and post-review gates run in the
  FOREGROUND `/run` session where a human IS reachable — `/run` MUST NOT inject
  `[no-interactive]` onto them, or a normal interactive `/run` would wrongly fail-close its own
  inline gate. (This mirrors the inline-vs-spawn boundary the Autonomous propagation section
  states for `[autonomous]` inline gates.)
- **`/thorough_plan` is excluded:** `/run` does NOT inject `[no-interactive]` onto the
  `/thorough_plan` spawn — `/thorough_plan` spawns no fail-closed skill (its deeper
  `/plan`/`/critic`/`/revise` spawns have no decision gate), so injecting it would be a
  false-positive with no benefit (R-06 containment).
- **Stacking:** a spawn prompt may read `[no-redispatch] [no-interactive] <task description>`.

## Routing a NEEDS-DECISION phase return

A spawned phase subagent that fails closed emits a structured
`gate-result: NEEDS-DECISION` block as its final message (and has already written
`needs-decision-{task}.md` under `.workflow_artifacts/memory/`). `/run` MUST recognize
`NEEDS-DECISION` as a phase-return token alongside review-`BLOCKED` and gate-`FAIL`, and route
it the SAME way: surface the block to the user, STOP the pipeline, and do NOT silent-proceed to
the next phase. Under `AUTONOMOUS` this composes with the existing hard-stop/halt-sentinel path
— the phase already wrote its own `needs-decision-{task}.md`, and `/run` treats the
NEEDS-DECISION return as a hard stop (it never auto-resolves a decision the phase could not
surface).

## Autonomous hard stops (halt-sentinel)

Under `AUTONOMOUS`, every hard stop writes a halt-sentinel **before exit**, then stops — Stage 1 forward-compatible groundwork for a future Stage 2 supervisor. Stage 1 only *writes* the sentinel; it never consumes one.

- **Location:** `.workflow_artifacts/memory/autonomous-halt-{task}.md` — deliberately OUTSIDE the task folder, so the record survives `/end_of_task`'s later move of the task folder into `finalized/`.
- **Schema (one line each):** `task`, `phase`, `reason`, `timestamp`, `resume_hint`.
- **The six hard-stop sites** (each documented at its own phase section below; all resolve here):
  1. **Review BLOCKED** (Phase 5) — `phase: review`, `reason: <blocking issues summary>`.
  2. **Gate FAIL after the retry cap** (Checkpoint C, Phase 4) — `phase: gate`, `reason: <failing checks summary>`.
  3. **Review CHANGES_REQUESTED after 3 rounds** (Phase 5) — `phase: review`, `reason: exceeded 3 review rounds without APPROVED`.
  4. **Git conflict** (any phase) — `phase: <phase where the conflict surfaced>`, `reason: <conflict summary>`.
  5. **Branch-hygiene violation** (Setup / any downstream precheck) — `phase: <phase where the violation was detected>`, `reason: <violation summary, e.g. protected-branch commits>`.
  6. **Below-bar formulation** (Formulation quality bar, between Phase 3 and Phase 4) — `phase: formulation`, `reason: <bar failure detail — critic REVISE at cap, or below-threshold Small confidence/smoke gate>`.
<!-- decision-gate: fail-closed site=fast-route-escalation tokens=0 -->
- **Fast-route escalation supplement for sites 1, 2, and 3.** On a fast-route run, sites 1 (Review
  BLOCKED), 2 (Gate FAIL after the retry cap), and 3 (Review CHANGES_REQUESTED after 3 rounds) each
  additionally offer "escalate to full" — but this is a SUPPLEMENT to the halt, never a substitute
  for it: all three sites remain unconditional halt-sentinel writers on the fast route, exactly as
  on the full path (see each site's own "write the halt-sentinel ... then stop" wording below).
  When escalation is the live option under `[autonomous]`, the run ALSO writes
  `needs-decision-{task}.md` (via `decision_gate_guard.py fail-closed`) IN ADDITION to
  `autonomous-halt-{task}.md` — never instead of it. This is deliberate: the Stage-2 supervisor
  (`src/quoin/supervisor.py`) reads ONLY `autonomous-halt-*` and never `needs-decision-*`
  (`decision_gate_guard.py` documents this split explicitly), so a supervised autonomous fast run
  that hits any of these three sites always leaves the supervisor-visible signal that terminates
  it — never an unattended relaunch into the full path. `needs-decision-{task}.md` remains the
  richer, human-readable record of WHY and WHERE to resume, for whoever picks the task back up. See
  each site's own phase section (Phase 4 Checkpoint C, Phase 5 CHANGES_REQUESTED, Phase 5 BLOCKED)
  for the full escalation mechanism.
- **Never auto-creates a PR.** At every one of the six sites, and everywhere else in this file, the orchestrator NEVER auto-creates a pull request — identical in interactive and autonomous mode. PR creation stays `/pr`, a separate explicit user action.
- **Only under `AUTONOMOUS`.** In plain (non-autonomous) `/run`, these six situations still halt the workflow exactly as documented in their own phase sections — they present the existing interactive prompt instead of writing a halt-sentinel.

## Autonomous progress sentinels (Stage 2 sentinel contract)

Under `AUTONOMOUS`, the completion of each of the 9 resumable phases below is recorded by a per-phase completion sentinel, so a future resumed session (or an external supervisor relaunching one) can tell exactly which phases already finished without re-deriving it from session-state prose. This section is the T-05 contract declaration — the entry-marker write (Setup) and the resume-side read/idempotency logic (the later "Resume" section) land in a later Stage-2 task; this section fixes the write-site map they both consume.

- **Directory:** `.workflow_artifacts/memory/autonomous-progress-{task}/` — same OUTSIDE-the-task-folder rationale as the halt-sentinel above.
- **Write-site map** (phase → completion sentinel, all 9 resumable phases, atomic write `printf > f.tmp && mv f.tmp f`):
  1. **discover** (Phase 1) → writes `autonomous-progress-{task}/discover.done`.
  2. **enrich** (Phase 1.4) → writes `autonomous-progress-{task}/enrich.done`.
  3. **specify** (Phase 1.5) → writes `autonomous-progress-{task}/specify.done`.
  4. **fast_path_triage** (Phase 1.6) → writes `autonomous-progress-{task}/fast_path_triage.done`.
  5. **architect** (Phase 2) → writes `autonomous-progress-{task}/architect.done`.
  6. **thorough_plan** (Phase 3) → writes `autonomous-progress-{task}/thorough_plan.done`.
  7. **implement** (Phase 4) → writes `autonomous-progress-{task}/implement.done`.
  8. **review** (Phase 5) → writes `autonomous-progress-{task}/review.done`.
  9. **end_of_task** (Phase 6) → writes `autonomous-progress-{task}/end_of_task.done`.
- **Sub-phase granularity (optional):** a phase MAY additionally write `autonomous-progress-{task}/{phase}.{subphase}.done` for finer-grained progress within itself (e.g. a long `implement` phase checkpointing partial task batches). The counting glob `autonomous-progress-{task}/*.done` is the UNION of both forms.
- **Marker:** `autonomous-run-{task}.marker`, written once at autonomous-span entry (Setup, right after `AUTONOMOUS` is set) — the read/re-establish-on-resume side of this contract is Stage-2 groundwork, documented alongside the later "Resume" section.
- **Done sentinel:** `autonomous-done-{task}.md`, written by `end_of_task` LAST, after its other terminal side effects, outside the archived folder — same rationale as the halt-sentinel.

Alongside the marker and the `.done` directory above sits a third record: `run-state-{task}.json`, at `.workflow_artifacts/memory/run-state-{task}.json`. Unlike the sentinels above, it is written UNCONDITIONALLY — in both autonomous and interactive mode — because it is what makes a plain (non-autonomous) `/run` session resumable too, not only a supervised autonomous one. `/run` is its sole creator; every other writer, including a standalone `/thorough_plan` round boundary, only refines an existing record and never mints a new one. It carries no autonomy field of its own — autonomy stays scoped to the `autonomous-run-{task}.marker` above — and its companion is an append-only notes file, `run-notes-{task}.md`. It never overrides a `.done` sentinel: sentinels remain the authoritative source for which phases and sub-phases finished, and the record only adds finer-grained resume detail on top of them.

## Phase sequence

```
Phase 1: DISCOVER     (conditional — skip if recent)
Phase 1.4: ENRICH     (default-on prompt — user chooses run/skip each time)
Phase 1.5: SPECIFY    (conditional — skip if Small OR task spec.md exists)
          ↓ Checkpoint A0: user confirms spec
Phase 1.6: FAST_PATH_TRIAGE (conditional — evaluates only on Small or `fast:`; silent otherwise)
          ↓ Checkpoint A1: user confirms fast vs full route (evaluating mode only)
Phase 2: ARCHITECT    (conditional — skip if Small, OR skip if route=fast)
          ↓ Checkpoint A: user confirms architecture
Phase 3: THOROUGH_PLAN (conditional — skip if route=fast)
          ↓ Checkpoint B: user confirms plan
Phase 4: IMPLEMENT
          ↓ Checkpoint C: user confirms implementation
Phase 5: REVIEW
          ↓ Checkpoint D: user confirms review outcome
Phase 6: END_OF_TASK
```

## Pre-phase context budget (per heavy phase spawn) — IVG-141

Before each HEAVY phase spawn — Phase 2 (architect), Phase 3 (thorough_plan),
Phase 4 (implement), Phase 5 (review) — run the on-demand context-budget guard in
the FOREGROUND top-level `/run` session (this is the PRIMARY / authoritative
measurement point; it measures the context that is about to spawn the phase). Do
NOT run it before the lighter Phase 1 / 1.4 / 1.5 / 1.6 / 6 (Phase 1.6 is inline, `D-12`, and never spawned; heavy-phase-only scope). This exclusion is unconditionally correct for Phase 1.6's silent no-op mode (Medium/Large, no `fast:` tag — genuinely zero extra reasoning); evaluating mode (Small, or `fast:` from any profile) does non-trivial inline reasoning over the evidence ladder and five eligibility criteria in the orchestrator's own session, so the exclusion there is a deliberate `D-12` scope choice (bounded, small relative to the phases this guard protects), not a claim that evaluating mode is free.
This is additive to the existing checkpoints; it never lowers or touches any hook
threshold.

**Bypass:** if the incoming prompt carries `[no-phase-budget]` (strip at
bootstrap) OR `QUOIN_DISABLE_PHASE_BUDGET=1` is set, SKIP the guard entirely
(power-user path, mirrors `[no-session-age-guard]`) and spawn the phase as today.

**Run the guard (foreground):**
```bash
_cbg_out="$(python3 __QUOIN_HOME__/scripts/context_budget_guard.py --project-root "$PROJECT_ROOT")"
```
- Exit 0 (`OK|...|` or `OK|disabled|`, incl. the `OK|0|` fail-OPEN path) → proceed
  with the phase spawn. On the fail-OPEN/missing-helper `OK|0|` path emit
  `[quoin-budget: guard unavailable; proceeding]`; otherwise emit nothing.
- Exit 1 AND `$_cbg_out` starts with `OVER|` (`OVER|util|path`) → run the ORDERED,
  NON-BLOCKING over-budget sequence (identical in interactive AND autonomous; NO
  prompt, NO `AskUserQuestion`, NO decision-gate marker):
  1. ALWAYS save the boundary checkpoint (durable resume point):
     ```bash
     python3 __QUOIN_HOME__/scripts/boundary_checkpoint.py \
       --project-root "$PROJECT_ROOT" --task "<task>" --skill run \
       --sid "$CLAUDE_CODE_SESSION_ID" --branch "<branch>" \
       --resume-command "/run --resume <task>" \
       --phase-label "before Phase N spawn" --plan-path "<current-plan.md>" || true
     ```
  2. Emit the one-line advisory:
     `[quoin-budget: util NN% ≥ threshold at run boundary; checkpoint saved → /run --resume <task>]`
  3. Then react (this is the ONLY branch that can halt, and only on opt-in):
     - **default** → PROCEED with the phase spawn. Never prompts, never blocks.
       Identical in interactive and autonomous.
     - **`QUOIN_PHASE_BUDGET_BLOCK=1`** (opt-in, default off) → print a
       fresh-session resume instruction (`/run --resume <task>`) and STOP. A
       printed instruction, NOT an `AskUserQuestion`.
  4. **`_AUTONOMOUS`** (`[autonomous]` / `--autonomous`) → the SAME non-blocking
     path as (1)-(3), and ADDITIONALLY perform the existing Hook-cooperation
     self-checkpoint + supervisor relaunch via `/run --resume --autonomous`
     (reuse the "Hook cooperation (autonomous)" contract — no new mechanism).
     Interactive has no supervisor → it continues in-session with the checkpoint
     as a clean-recovery backstop.
- Exit 1 but `$_cbg_out` does NOT start with `OVER|` (helper crashed before
  reaching its own fail-OPEN try/except — e.g. empty output or a traceback, never
  a genuine budget read) → treat identically to the `OK|0|` fail-OPEN path above:
  emit `[quoin-budget: guard unavailable; proceeding]` and proceed with the phase
  spawn. Never infer OVER from bare exit code 1 alone (lessons-learned
  2026-09-04 — a crash outside the guard's own try/except is otherwise
  indistinguishable from a genuine over-budget verdict).

This adds NO new `AskUserQuestion` site and NO decision-gate `best-effort` marker
(the over-budget path is non-blocking). It also does NOT touch `/run`'s existing
`[no-interactive]` injection / `/thorough_plan` exclusion wiring — those are
unrelated to this non-blocking budget check and stay exactly as documented above.

## On-behalf cost capture (`QUOIN_INLINE_COST_CAPTURE`, default-ON, opt-out via =0, D-1/D-2/D-3, IVG-111 stage 3)

Unless `QUOIN_INLINE_COST_CAPTURE=0`, this applies at EVERY managed phase spawn below — discover,
enrich, specify, architect, thorough_plan, implement, review, end_of_task — and the two post-phase
`/gate` subagent spawns (spec→architect boundary at Checkpoint A0, architecture boundary at
Checkpoint A). The implement-phase inline gate (no spawn) is NOT managed — it runs in-session,
nothing to suppress. The fast-path triage step (Phase 1.6) is likewise NOT in this roster — it
runs inline (`D-12`), never as a spawned subagent, so there is no child session to suppress the
self-write for; this list is deliberately unchanged.

At each spawn: (1) prepend `[quoin-onbehalf]` to the spawn prompt — stacks with
`[autonomous]`/`[no-interactive]`/`[no-redispatch]` per the existing propagation rules
(order-independent EXCEPT that any `[no-redispatch]` form must remain the FIRST token whenever one
is present — `implement/SKILL.md`'s §0 and §0‴ dispatch preambles condition on the prompt "starting
with" a `[no-redispatch]` form; on the fast route's Opus `/implement` spawns this ordering is
load-bearing, per "Phase 4 — Implement" below, so `[quoin-onbehalf]` must be prepended AFTER
`[no-redispatch]`, never before it — each leaf still strips its own copy regardless of position) —
so the child SKIPS its own session-start
cost-ledger self-write (T-06 predicate); (2) bind `AID`/`TUID` per `proc:agentid-capture` (stage-3
plan) — `AID` is the `agentId` field from the Agent tool's return for that spawn, transcribed
literally by this orchestrator (model-in-the-loop, not a shell capture); `TUID` is this spawn's own
Agent tool_use id; fallback (agentId absent/empty, R-12): `AID = TUID` if present, else
`"<parent-session-uuid>-<phase>-<utc-ts>"`, forcing `ATTR="src=unresolved"` (discard any sidecar
hit once agentId capture failed); (3) AFTER the subagent returns and its phase artifact is verified on disk, run this on-behalf
ledger write with that phase's model and `uuid=<AID>` — this REPLACES the suppressed child
self-write (D-2, unchanged), but it does NOT replace each phase's existing "verify the cost ledger
has a new entry... if not, best-effort append" step below. That step still runs, immediately AFTER
the on-behalf write, as a POST-CHECK: if the on-behalf write itself failed to land a row (mktemp
failure, sidecar crash, disk error — any failure mode not already handled by the
`ATTR="src=unresolved"` fallback inside the write itself), the best-effort append still fires and
labels the row `(on-behalf write failed)`. Net: at least one row per managed phase always (never
zero — this is what CRIT-2 fixes), and normally exactly one attributable row (the on-behalf write)
plus, only on write failure, a second best-effort-labeled row:
```bash
SID="$CLAUDE_CODE_SESSION_ID"
_ERR=$(mktemp) || { printf 'cost-attr WARN: %s\n' "mktemp failed"; _ERR=/dev/null; }
ATTR="$(python3 __QUOIN_HOME__/scripts/agent_transcript_cost.py \
          --sid "$SID" --agent-id "$AID" --tool-use-id "$TUID" 2>"$_ERR")"
[ -z "$ATTR" ] && ATTR="src=unresolved"   # MIN-1: key on empty stdout, not exit code
[ -s "$_ERR" ] && printf 'cost-attr WARN: %s\n' "$(head -c 500 "$_ERR" | tr '\011\012\015' '   ' | tr -d '\000-\037\177')"
[ "$_ERR" != "/dev/null" ] && rm -f "$_ERR"
printf '%s | %s | %s | %s | task | %s | %s | %s\n' \
  "$AID" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" \
  "on-behalf: PHASE via /run" "0" "$ATTR" >> "$LEDGER"
# F-02 post-check (identifier-keyed, same invocation): verify THIS write's own
# AID landed — immune to a stale same-phase row masking a lost row on re-run,
# unlike a bare phase-name check. If the append above silently failed (mktemp
# failure, disk error, sidecar crash), append a labeled fallback row now.
{ [ -n "$AID" ] && grep -qF -e "$AID | " -- "$LEDGER" 2>/dev/null; } || \
  printf '%s | %s | %s | %s | task | %s | %s\n' \
    "unknown-PHASE-$(date -u +%s)" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" \
    "/run subagent (on-behalf write failed)" "0" >> "$LEDGER"
```
This post-check is embedded in every call site of this bash block — including the two
`/gate` spawns and any other managed phase — so no managed spawn is left with a silent
zero-row path (closes the F-01 gap: a no-fallback spawn is no longer possible here).
Nested-orchestrator case is correct by construction: `/run` prepends `[quoin-onbehalf]` to the
`/architect` and `/thorough_plan` spawns; those children strip their own marker (skip their own
self-write, per T-06/D-9) and independently prepend a FRESH `[quoin-onbehalf]` to THEIR OWN
children (the architect critic spawn; thorough_plan's plan/critic/revise/enrich spawns) — per-spawn,
non-inherited, no double-suppression.

**`end_of_task` note:** `end_of_task` is spawned in Phase 6 but is NOT a bootstrap cost-ledger
self-writer today (it aggregates at task close rather than self-writing a session-start row), so
there is no child self-write to suppress for it — the on-behalf write above still fires for the
`end-of-task` phase (parity with the other 7 phases); it simply has no corresponding T-06 skip
predicate to pair with. This "7" counts the on-behalf MANAGED phase roster (the 8 spawned phases
enumerated above, of which `end_of_task` is one) — the fast-path triage step runs inline (`D-12`)
and is never spawned, so it is deliberately outside this mechanism and this count is unchanged.

**Opt-out (`QUOIN_INLINE_COST_CAPTURE=0`):** every phase spawn reverts to the pre-`IVG-249` behavior
— the child self-writes its own 6/7-col session-start row (col 8 empty), or `end_of_task`
aggregates as before; each phase's existing best-effort ledger-verify fallback continues to run
exactly as it does under default-ON (per the CRIT-2 fix above) — the fallback was never
opt-out-only.

## Handoff envelope (phase dispatch and return)

The normative contract for the fields, ordering, delimiters, and escaping below is `__QUOIN_HOME__/core/workflow/handoff-format.md`; the checkable rules and their interaction cascade are defined in `__QUOIN_HOME__/core/workflow/handoff-format-reference.md`. An absent or unrecognised envelope degrades to today's free-form prose and never blocks a phase.

Dispatch template:
```text
[quoin-handoff/1.1 dispatch]
skill: <spawned-skill-name>
task: <task-name>
task_dir: <resolved task directory>
inputs: <upstream artifact> | <upstream artifact>
return: envelope
[/quoin-handoff]
```

Return template, complete status:
```text
[quoin-handoff/1.0 return]
status: COMPLETE
artifact: <path to the primary artifact written this phase>
verdict: PASS
summary: <one-line plain-English summary of what this phase produced, clamped to 600 B>
[/quoin-handoff]
```

`verdict` shows `PASS` here, the default for a phase with no approve/reject
branch of its own (discover, enrich, specify, architect, end_of_task) — emit
whichever of `PASS`/`REVISE`/`APPROVED`/`CHANGES_REQUESTED`/`BLOCKED`
actually matches this phase's outcome. implement, review, and thorough_plan
inline their own branch-specific return template and use that instead of
this generic one.

Every dispatch appends the COMPLETE return template above plus the trailing
note below to the child spawn prompt, covering all 8 phase dispatches
(discover, enrich, specify, architect, thorough_plan, implement, review,
end_of_task). The trailing note reads, verbatim:

"Emit whichever of PASS/REVISE/APPROVED/CHANGES_REQUESTED/BLOCKED matches
this phase's outcome; prefer this phase's own inlined template if its skill
file has one. This shows the COMPLETE shape only — a fail-closed hard stop
still emits the decision-gate block, not an envelope. Do not open
__QUOIN_HOME__/core/workflow/handoff-format.md unless you need other
shapes/escaping."

With `spec:` no longer part of the dispatch payload, the trailing note above
is the only path to the full contract (needed for the partial/needs-decision/
blocked shapes and the escaping rules this template omits); a compliant
COMPLETE return never requires opening it. The whole envelope (marker to
marker) is clamped to 1,024 B — stated once here, not repeated at each phase
section or re-dispatch site below.

Return template, partial status:
```text
[quoin-handoff/1.0 return]
status: PARTIAL
checkpoint: <path to the saved checkpoint>
phase: <phase name>
remaining: <one line describing what is left, written as prose, never a delimited list>
resume_hint: <one line telling the next dispatch where to pick up>
[/quoin-handoff]
```

The envelope always sits strictly after the leading sentinel-token zone and never wraps it — sentinel tokens such as `[no-redispatch]` and `[autonomous]` stay outside the marker pair, never inside it.

The fail-closed decision-gate block a spawned phase already emits on a hard stop is unchanged by any of this — it stays the token this orchestrator recognises, and a phase taking that path emits no envelope on that return.

## Phase 1 — Discover (conditional)

**Skip condition:** Check if `.workflow_artifacts/cache/_staleness.md` exists AND its modification time is less than 7 days old. Fall back to `.workflow_artifacts/memory/repo-heads.md` if `_staleness.md` does not exist:
```bash
find .workflow_artifacts/cache/_staleness.md -mtime -7 2>/dev/null || find .workflow_artifacts/memory/repo-heads.md -mtime -7 2>/dev/null
```
Also check for `repos-inventory.md` (plural) as secondary confirmation.

- **If skipping:** tell the user "Discovery files are recent (<N> days old) — skipping /discover. Say 'rediscover' to force a fresh scan."
- **If running:** spawn `/discover` as a subagent session (same mechanism as `/thorough_plan` uses for `/critic` — see its "Invoking each agent" section). Pass the project folder path. No gate runs after discover — it feeds directly into architect.

Emit the dispatch envelope (above), skill: `discover`, `return: envelope`.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, the on-behalf write per "On-behalf cost capture" above (phase=discover, model=opus) runs FIRST. Either way (on-behalf write above, or under opt-out the child's own self-write), THEN verify the cost ledger has a new entry for the `discover` phase. If still not present, append a best-effort entry: `unknown-discover-<timestamp> | <date> | discover | opus | task | /run subagent (on-behalf write failed, if capture was ON — else no UUID recorded)`.

Under `AUTONOMOUS`, also write the phase's completion sentinel `autonomous-progress-{task}/discover.done` (atomic write — T-05/T-10 write-site map) — the T-09 resume-reader depends on this file's presence to know Phase 1 finished.

Independent of `AUTONOMOUS` — this write is unconditional in both interactive and autonomous mode, including when Phase 1 itself was skipped (the discovery-freshness check above), so a plain (non-autonomous) `/run` session gets the same phase-boundary resumability record:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "discover" --phase-index 1 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start enrich" --artifact "{task_dir}/current-plan.md" || true
```


## Phase 1.4 — Enrich (default-on prompt)

On entry, PROMPT via `AskUserQuestion`: "Run prompt enrichment on this task, or skip?"

- Header: "Enrich"; multiSelect: false
- Option 1: label "Run /enrich" — description: "Sharpen the raw task prompt before specify/architect (fills genuine gaps, writes enriched-prompt.md)."
- Option 2: label "Skip" — description: "Proceed straight to the next phase without enrichment."

- **If skipping:** tell the user "Skipping /enrich per your choice." and proceed to Phase 1.5.
- **If running:** spawn `/enrich` as a subagent session (same mechanism as the Phase 1.5 specify spawn). Pass the raw task description and the task folder path.

Emit the dispatch envelope (above), skill: `enrich`, `return: envelope`.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, the on-behalf write per "On-behalf cost capture" above (phase=enrich, model=opus) runs FIRST. Either way (on-behalf write above, or under opt-out the child's own self-write), THEN verify the cost ledger has a new entry for the `enrich` phase. If still not present, append a best-effort entry: `unknown-enrich-<timestamp> | <date> | enrich | opus | task | /run subagent (on-behalf write failed, if capture was ON — else no UUID recorded)` (mirrors the Phase 1/Phase 1.5 best-effort append pattern).

Under `AUTONOMOUS`, also write the phase's completion sentinel `autonomous-progress-{task}/enrich.done` (atomic write — T-05/T-10 write-site map) — including when enrich was skipped via the AskUserQuestion prompt above (a skipped phase still counts as reaching this point in the pipeline; resume must not re-offer the prompt).

Independent of `AUTONOMOUS` — this write is unconditional in both interactive and autonomous mode, including when enrich itself was skipped, so a plain (non-autonomous) `/run` session gets the same phase-boundary resumability record:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "enrich" --phase-index 1 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start specify" --artifact "{task_dir}/current-plan.md" || true
```


**No `/gate` spawn after enrich** — proceed straight to Phase 1.5 (Q-1: enrich is a lightweight upstream sharpening pass, not a phase boundary that needs a quality gate).

Under non-interactive dispatch (no way to present `AskUserQuestion`), degrade to best-effort: run `/enrich` anyway and flag assumptions, never block the pipeline waiting for an answer that can't be given.

## Phase 1.5 — Specify (conditional)

**Skip condition:** Task profile is Small OR `<task-root>/spec.md` already exists.

- **If skipping:** tell the user "Skipping /specify (Small task | spec already exists)."
- **If running:** spawn `/specify` as a subagent session (same mechanism as the Phase 2 architect spawn). Pass the task description and the task folder path.

Emit the dispatch envelope (above), skill: `specify`, `return: envelope`.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, the on-behalf write per "On-behalf cost capture" above (phase=specify, model=opus) runs FIRST. Either way (on-behalf write above, or under opt-out the child's own self-write), THEN verify the cost ledger has a new entry for the `specify` phase. If still not present, append a best-effort entry: `unknown-specify-<timestamp> | <date> | specify | opus | task | /run subagent (on-behalf write failed, if capture was ON — else no UUID recorded)` (mirrors the Phase 1/Phase 2 best-effort append pattern).

After specify completes, spawn `/gate` as a subagent session (spec→architect boundary — subagent dispatch, mirrors the post-architect gate at Phase 2; audit-log persistence mandatory). Unless `QUOIN_INLINE_COST_CAPTURE=0`, this gate spawn also follows "On-behalf cost capture" above (phase=gate, model=sonnet); the gate spawn has no SEPARATE verify/append fallback documented for it (round 1 finding, still true) — but the on-behalf write's own F-02 post-check, embedded in the shared bash block above, covers this spawn too: an on-behalf write failure still lands a labeled `(on-behalf write failed)` fallback row here, so this is no longer a silent zero-row path.

Under `AUTONOMOUS`, also write the phase's completion sentinel `autonomous-progress-{task}/specify.done` (atomic write — T-05/T-10 write-site map), including when specify was skipped by its own skip condition.

**If specify was skipped** (Small profile, or `spec.md` already exists — no gate runs on this
branch): independent of `AUTONOMOUS`, write the phase-boundary record immediately, unconditional
across interactive and autonomous mode:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "specify" --phase-index 1 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start fast_path_triage" --artifact "{task_dir}/current-plan.md" || true
```

**If specify ran:** only once the gate result is known, and only on a PASS — never before, and
never at all on a FAIL ("Gates are blocking" below applies) — independent of `AUTONOMOUS`, write
the same phase-boundary record, so a plain (non-autonomous) `/run` session gets the same
resumability record:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "specify" --phase-index 1 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start fast_path_triage" --artifact "{task_dir}/current-plan.md" || true
```

**Checkpoint A0:**
```
Phase complete: Specify
Artifact: .workflow_artifacts/{task-name}/spec.md   (task feature spec — task root)

Summary:
- {goals / user stories — 2-3 bullets}
- {key acceptance criteria}

Gate: PASSED / FAILED

Continue to architecture? (yes / no / show spec)
```

## Phase 1.6 — Fast-path triage (conditional)

`profile` (Small/Medium/Large) keeps today's meaning and classification. `route` is a separate,
orthogonal concept — `full` (default) or `fast`. This phase always executes, so its place in the
resumable-phase roster and its sentinel contract stay whole — but it has two behavioral modes:

- **Silent no-op mode.** Setup profile is Medium or Large AND no `fast:` tag was given → emit
  `route=full` with zero user-facing output, zero prompts, zero extra model calls, and no
  `triage-decision.md`. This mirrors the existing skip behavior at Phase 1.4 and Phase 1.5 exactly.
  A plain (non-autonomous) run adds zero artifacts and zero behavior delta here; an autonomous run
  adds exactly one thing — its own completion sentinel (below) — and nothing else.
- **Evaluating mode.** Fires when the Setup profile is Small, or a `fast:` tag was given from any
  profile. Only in this mode is eligibility assessed, Checkpoint A1 raised (see below), and
  `triage-decision.md` written.

**Evidence ladder.** Read the strongest available input, in this precedence order:
- `<task-root>/spec.md`, when it exists.
- else `<task-root>/enriched-prompt.md`.
- else the raw task description.

A raw task description alone can never satisfy eligibility criterion 5 below, so evaluating mode
with no spec and no enriched prompt degrades to `route=full` — silently, like any other ineligible
evaluation.

**Eligibility — all five must hold:**
- Bounded file set inside a single module.
- No new cross-module or cross-repo integration point.
- Matches a pattern already present in the codebase.
- No data migration, auth change, or public-contract change.
- The evidence's acceptance criteria are already concrete enough to serve directly as an
  implementation checklist.

This is visibly stricter than the existing Small-task threshold: Small alone does not require all
five of these, fast-path eligibility does.

A Small task that evaluates to ineligible is ALSO silent: it emits `route=full`, raises no
checkpoint, and writes no decision artifact.

**Checkpoint A1.** Fires in evaluating mode only, and only once eligibility has passed (an
ineligible evaluation stays silent, per above — no checkpoint, no artifact). Raised inline by the
orchestrator via `AskUserQuestion`, never by a subagent — `AskUserQuestion` is not provisioned to
subagents. This is a prose-described `AskUserQuestion` call site, deliberately outside the
decision-gate census's call-syntax detection scope (the census counts a literal opening-paren call
form as a genuine token and intentionally does not count prose mentions like this one) — confirmed
out-of-scope rather than carrying a classification marker.

Prompt: "Fast-path triage found this task eligible for the fast route (confidence {confidence}).
Take the fast path, take the full path, or see why?"

- Header: "Fast-path triage"; multiSelect: false
- Take the fast path — label "Take fast path" — description: "Skip the architecture and planning
  phases; dispatch `/implement` directly against a mechanically-derived plan stub."
- Take the full path — label "Take full path" — description: "Ignore the fast-route recommendation
  and run the full pipeline exactly as today."
- Show rationale — label "Show rationale" — description: "Print the eligibility reasoning and
  evidence tier used, then re-ask this same question."

When a `fast:` tag was supplied, the checkpoint still fires — evaluating mode is entered by
either trigger — but with "Take fast path" pre-selected as the default option (`D-07`); the user
may still override to the full path.

**If the classified profile is Large:** the prompt text must also warn BY NAME that taking the
fast path drops the performance and architecture/integration review dimensions — the dedicated
`/security_review` OWASP pass is retained unconditionally on Large regardless of route (per
`/review`'s Large carve-out, see its Profile detection and fan-out section), so the fast path's
saving on Large is narrower than on Medium. The full-path option remains available at
this same prompt.

**If the classified profile is Small:** the prompt text must also name the cost tradeoff: the fast
route moves `/implement` from Sonnet to Opus (typically the run's largest token consumer), and
Small's only remaining saving is one Opus `/plan` pass — so on Small the fast route is plausibly
MORE expensive than the full path, not less (see "Small-profile cost honesty" under Phase 4 and
the Cost estimate table). The full-path option remains available at this same prompt.

Under `[no-interactive]` (no human reachable to answer): fail closed to `route=full`. A route is
never guessed as "fast" without a human confirming it at this checkpoint. The same fail-closed
degrade applies if `AskUserQuestion` IS raised but returns empty, errors, or otherwise fails to resolve to one of the three defined options — treat that identically to the `[no-interactive]`
case: fail closed to `route=full` rather than guessing or retrying indefinitely.

Under `[autonomous]`: Checkpoint A1 is skipped entirely — no `AskUserQuestion` is raised — and the
route already produced by eligibility evaluation is taken as-is, subject to the formulation
quality bar described later in this file.

Refer to the existing yes/no checkpoint table only in plain English (e.g. "the existing yes/no
checkpoint table") anywhere near this checkpoint's description — never reproduce its heading text.

**Fast-route plan stub (evaluating mode, `route=fast` only).** When Checkpoint A1 resolves to the
fast path — chosen by the user, or auto-taken under `[autonomous]` per the formulation quality bar
described later in this file — the orchestrator writes `<task_dir>/current-plan.md` itself, once,
subject to the read-before-write guard below and followed by the post-write validation below.

**Read-before-write guard (existence check, MUST run before any write).** Before writing, check
whether `<task_dir>/current-plan.md` already exists. If it exists and does NOT carry
`provenance: fast-path-triage` in its frontmatter — this is a real, critic-reviewed plan a prior
full pipeline run already produced (most plausibly a task that started on an older `/run` version
and is now resuming under this one). The frontmatter marker is the ONLY sanctioned test: do NOT
substitute `autonomous-progress-{task}/thorough_plan.done` as an equivalent check — that sentinel
is written whenever Phase 3 is reached, including when Phase 3 itself was skipped for the fast
route (Phase 3's own `.done` write is unconditional on skip, see its own section below), so a
fast-route stub carries it too and would misread as a real plan; the sentinel is also never
written at all on a plain non-autonomous run, so a genuinely critic-reviewed plan produced by one
would misread as absent-stub — false in both directions, not a usable proxy. NEVER overwrite it:
degrade silently to `route=full` instead (no prompt, no error — the same silent-degrade posture as
every other ineligible evaluation in this section), record `route: full` in `triage-decision.md`
(so Resume Step 0b's fallback source agrees with this in-session degrade), and let Phase 2 and
Phase 3 run normally against the real plan. If the file does not exist, or exists and DOES carry
`provenance: fast-path-triage` (a stub left behind by an earlier interrupted fast-route attempt on
THIS SAME task), proceed to write/overwrite it — overwriting a stub with a fresher stub is safe;
overwriting a real plan is not.

The stub is a valid Class B `current-plan.md`: `## For human`, `## State`, `## Tasks`, and
`## Risks` are all present. Its `## Tasks` section carries one pending task per coherent change,
each with concrete file paths and an acceptance bullet — derived from the evidence's acceptance
criteria when a spec exists, and from the enriched prompt's concrete deliverables otherwise.

**Task-section contract (pending glyph + numbering, MUST match what `/implement` selects on).**
Every task written into `## Tasks` is pending: prefix each with the `⏳` glyph and label it
`T-NN` (`T-01`, `T-02`, ... in emission order) — the identical glyph and numbering shape
`implement/SKILL.md`'s task-confirmation step depends on (it populates its `AskUserQuestion`
options, and its `_AUTONOMOUS` auto-select branch, from "pending tasks (⏳)" in `current-plan.md`).
A stub whose tasks lack the `⏳` glyph or the `T-NN` label makes `/implement` see zero pending
tasks and stop with "All tasks already implemented." on an autonomous fast run that has not
actually implemented anything — this contract exists specifically to prevent that silent no-op.

Four provenance markers, all four required, so a fast-route stub can never be mistaken for a
critic-reviewed plan:
- the frontmatter carries `provenance: fast-path-triage`.
- `## For human` opens with a sentence stating plainly that no planning phase ran for this task.
- the convergence summary line reads `Rounds: 0`.
- a `Route: fast` line, placed inside `## State` (not between `## For human` and `## State`, so it
  never counts toward the `## For human` block's own length cap) — this same line is what a later
  resume reads back to recover the route, described later in this file. Render it bare, alone on
  its own line, at line start — `Route: fast` with nothing else on the line — never inside a bullet
  (`- Route: fast`) and never with leading or trailing content on the same line; being inside a
  fenced `## State` block (as the reference stub fixture in
  `quoin/dev/tests/test_run_fast_path.py` does) is fine, since the line itself is
  still bare at its own line start there. Resume Step 0b's read is line-anchored
  (`^Route:\s*(fast|full)\s*$`); any other rendering (a bullet prefix, inline prose) never matches
  and the stub's route silently reads as absent.

Two further lines, deliberately different values, so a fast-route stub never buys a weaker gate on
a harder task: `Task profile: <the honestly classified profile>` (unchanged — a `fast:`-forced
Large task still gates as Large) and `Review shape: single-pass (fast-path)`.

**Post-write validation (MUST run immediately after the write, before this phase completes).** Run
`python3 __QUOIN_HOME__/scripts/validate_artifact.py <task_dir>/current-plan.md` against the
just-written file. `validate_artifact.py` checks frontmatter/headings/sections only — it does NOT
check task content, so it cannot by itself catch the specific hazard the pending-glyph contract
above exists to prevent (a `## Tasks` block with zero `⏳` lines, which validates cleanly but makes
`/implement` see zero pending tasks and silently no-op). Add an explicit second check alongside the
validator call: the just-written file MUST contain at least one `⏳` glyph, at least one `T-01`
label, and a `Task profile:` line (a stub missing it would land in the
undetermined-plus-override review cell and drop the Large OWASP retention); if any is missing,
treat it identically to a non-zero validator exit. Exit 0 from the
validator AND all three glyph/label/profile checks passing → proceed normally. Any non-zero exit
from the
validator, OR a missing `⏳` glyph, OR a missing `T-01` label, OR a missing `Task profile:` line →
the emitted stub failed its own
format contract; do NOT hand a malformed or silently-empty stub to `/implement` — delete the
just-written file and
degrade to `route=full` (same silent-degrade posture as the read-before-write guard above), and
record `route: full` in `triage-decision.md` (same rewrite as the read-before-write guard's own
degrade, so Resume Step 0b cannot read a stale `fast` recommendation back from the file after the
stub it was written for no longer exists), so Phase 2 and Phase 3 run the full pipeline instead of
dispatching against a broken plan.

**`triage-decision.md`.** Written at the task root, evaluating mode only: the EFFECTIVE route (the
Checkpoint A1 answer; or `full` if either silent-degrade path above overrode it, or if Checkpoint
A1's `[no-interactive]` fail-closed degrade produced it — never the raw A1
answer alone), the rationale, the confidence, and the evidence tier used. This filename is
deliberately not registered
as a distinct artifact type — an unrecognized filename already validates under the default type, so
no format-kit change is needed for it. It is also the fallback a later resume reads when the stub
carries no `Route:` line, described later in this file.

Every bullet and line described above renders as prose or a bullet list, never a pipe-leading table
line — with ONE exception: the stub's `Route:` line, which must render bare on its own line, never
as a bullet, per the provenance-marker contract above (specific beats general here) — and this
task adds no second `.workflow_artifacts/` root and no new artifact family beyond
this one file.

**Ledger row.** Write phase `triage` (the cost-ledger's fixed phase vocabulary is not extended) —
note this is deliberately a DIFFERENT string from the roster/sentinel/heading name
`fast_path_triage` used everywhere else in this file; do not write `fast_path_triage` into the
ledger. Written inline by the orchestrator, since this step runs inline and never as a subagent.
Pin the model explicitly to `opus` (this step runs inline in the `/run` orchestrator's own Opus
session — never let a phase→model writer mapping default this row to the triage skill's own cheap
tier) and pin `uuid` to the orchestrator's own session UUID (`get_session_uuid.py --phase run`),
not a synthesized or borrowed one.

**Session state.** Gains the effective route, its confidence, and the evidence tier that fed the
decision, recorded at this phase boundary like every other phase.

Under `AUTONOMOUS`, once evaluated (both modes), also write the phase's completion sentinel
`autonomous-progress-{task}/fast_path_triage.done` (atomic write — T-05/T-10 write-site map). A
plain (non-autonomous) run never writes this file, in either mode.

Independent of `AUTONOMOUS` — this write is unconditional in both interactive and autonomous mode, so a plain (non-autonomous) `/run` session gets the same phase-boundary resumability record:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "fast_path_triage" --phase-index 1 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start architect" --artifact "{task_dir}/current-plan.md" || true
```


## Phase 2 — Architect (conditional)

**Skip condition:** Task profile is Small, OR the fast route was taken at Phase 1.6 (`route=fast`).

- **If Small:** tell the user "Small task — skipping /architect, proceeding directly to planning."
  There is no gate to wait for on this branch (none runs), so write the phase-boundary record
  immediately, unconditional on `AUTONOMOUS`:
  ```
  python3 __QUOIN_HOME__/scripts/run_state.py --write \
    --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
    --phase "architect" --phase-index 2 --subphase "" --step "" \
    --at-stage-boundary true --route "{route}" --profile "{profile}" \
    --next-action "start thorough_plan" --artifact "{task_dir}/current-plan.md" || true
  ```
- **If fast route:** tell the user "Fast route — skipping /architect, dispatching /implement directly against the routing stub." Same immediate write as the Small branch above (no gate on this
  branch either).
- **If running:** spawn `/architect` as a subagent session, passing the task description, paths to discovery output files (`repos-inventory.md`, `architecture-overview.md`, `dependencies-map.md`), and the path to `<task-root>/spec.md` if it exists (read-if-exists). The rest of this section —
  through the gate-conditional write below — applies only to this branch.
  - **Note:** `/architect` now includes a Phase 4 critic loop (max 2 rounds default, 4 in strict mode); expect 1-2 additional `critic` phase rows in the cost ledger per round. If Phase 4 triggers the cost-guard confirmation (pre-round-2), the architect subagent will pause for user input — watch for the prompt `[critic round 2 starting — ~$10-30 estimated based on body size]` in the subagent output.

Emit the dispatch envelope (above), skill: `architect`, `return: envelope`.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, the on-behalf write per "On-behalf cost capture" above (phase=architect, model=opus) runs FIRST. Either way (on-behalf write above, or under opt-out the child's own self-write), THEN verify the cost ledger has a new entry for the `architect` phase. If still not present, append a best-effort entry with `unknown-architect-<timestamp>`. Also check for `critic` phase rows from Phase 4 (1-2 expected; accept their absence if Phase 4 was skipped via `max_rounds: 0`) — the Phase 4 critic-round rows are architect's OWN on-behalf writes, per its own on-behalf mechanism, independent of this one.

Under `AUTONOMOUS`, also write the phase's completion sentinel `autonomous-progress-{task}/architect.done` (atomic write — T-05/T-10 write-site map), including when architect was skipped for a Small task or for the fast route.

After architect completes, spawn `/gate` as a subagent session (architecture gate — subagent dispatch required for audit-log persistence). No ``[quoin-bundle]`` block is emitted at this gate: the gate reads only the artifact's `## For human` block (729 B-scale), so the corrected expected-delta census (review round 1) shows a bundle here is net-negative — the consumer was descoped.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, this gate spawn also follows "On-behalf cost capture" above (phase=gate, model=sonnet); the gate spawn has no SEPARATE verify/append fallback documented for it (round 1 finding, still true) — but the on-behalf write's own F-02 post-check, embedded in the shared bash block above, covers this spawn too: an on-behalf write failure still lands a labeled `(on-behalf write failed)` fallback row here, so this is no longer a silent zero-row path.

**Only once the gate result is known, and only on a PASS** — never before, and never at all on a FAIL
(the flow does not proceed past a failed gate per "Gates are blocking" below) — present Checkpoint A.
The phase-boundary write itself is deferred one step further, to the point where Checkpoint A
resolves to continue (see immediately below the checkpoint), never here:

**Checkpoint A:**
```
Phase complete: Architecture
Artifact: .workflow_artifacts/<task-name>/architecture.md

Summary:
- <key architectural decisions>
- <stages identified>
- <integration points>
- Critic verdict (Phase 4): PASS / REVISE / skipped

Gate: PASSED / FAILED

Continue to planning? (yes / no / show architecture)
```

Once Checkpoint A resolves to continue — the user answers `yes`/`y`/`continue`/`go`, or under
`AUTONOMOUS` the checkpoint auto-resolves because the gate already PASSED (see `## Checkpoint
interaction protocol`) — independent of `AUTONOMOUS`, write the phase-boundary record, so a plain
(non-autonomous) `/run` session gets the same resumability record. Never on `no`, and never before
this point: a session lost between the gate PASS and the checkpoint answer, or a user who answers
`no`, must not leave `next_action: start thorough_plan` on disk for a continuation that was never
confirmed:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "architect" --phase-index 2 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start thorough_plan" --artifact "{task_dir}/current-plan.md" || true
```

Only then proceed to Phase 3.

## Phase 3 — Thorough Plan

**Skip condition:** the fast route was taken at Phase 1.6 (`route=fast`). The fast route already
wrote its own `current-plan.md` stub in Phase 1.6 — `/thorough_plan` never runs, and the stub is
dispatched to `/implement` directly.

- **If fast route:** tell the user "Fast route — skipping /thorough_plan, the routing stub at
  `<task_dir>/current-plan.md` is the plan for this run."
- **If running:** spawn `/thorough_plan` as a subagent session, passing:
  - Task profile and max_rounds
  - Task description (with tokens stripped)
  - Path to `architecture.md` (if it exists)
  - Path to `spec.md` (if it exists)
  - Repo paths

`/thorough_plan` handles its own internal plan→critic→revise loop and runs its own post-plan smoke
gate, returning `verdict: PASS` (converged) or `verdict: REVISE` (max_rounds hit without
convergence) in its return envelope — read it before writing the phase-boundary record below.

Emit the dispatch envelope (above), skill: `thorough_plan`, `return: envelope`.

After the phase, unless `QUOIN_INLINE_COST_CAPTURE=0`, the on-behalf write per "On-behalf cost capture" above (phase=thorough-plan, model=opus) runs FIRST for the `thorough-plan` row — the plan/critic/revise/enrich rows are thorough_plan's OWN on-behalf writes, per its own on-behalf mechanism, independent of this one. Either way, THEN verify the cost ledger has new entries for `thorough-plan`, `plan`, `critic`, and (if applicable) `revise` phases. If any are still missing, append best-effort entries with `unknown-<phase>-<timestamp>`.

Under `AUTONOMOUS`, also write the phase's completion sentinel `autonomous-progress-{task}/thorough_plan.done` (atomic write — T-05/T-10 write-site map), including when thorough_plan was skipped for the fast route.

**On the fast route** (thorough_plan skipped — no gate runs on this branch): independent of
`AUTONOMOUS`, write the phase-boundary record immediately. Checkpoint A1 already confirmed
continuation to implement, and Checkpoint B never fires on this route (see Phase 4's cross-reference
below), so there is no later checkpoint to defer this write to:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "thorough_plan" --phase-index 3 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start implement" --artifact "{task_dir}/current-plan.md" || true
```

**If running:** only once the returned verdict is `PASS` — never on `REVISE` (the loop did not
converge; "Gates are blocking" below applies) — present Checkpoint B:

**Checkpoint B:**
```
Phase complete: Planning
Artifact: <task_dir>/current-plan.md (where <task_dir> = `python3 __QUOIN_HOME__/scripts/path_resolve.py --task <task-name> [--stage <N-or-name>]`; architecture.md ALWAYS at task root per D-03)
Profile: <Small|Medium|Large>, <N> round(s), verdict: PASS

Summary:
- <what will be built — 3-5 bullets>
- <files affected>
- <key risks noted>

Continue to implementation? (yes / no / show plan)
```

Once Checkpoint B resolves to continue — the user answers `yes`/`y`/`continue`/`go`, or under
`AUTONOMOUS` the checkpoint auto-resolves because the smoke gate already returned `PASS` (see
`## Checkpoint interaction protocol`) — independent of `AUTONOMOUS`, write the phase-boundary
record, so a plain (non-autonomous) `/run` session gets the same resumability record. Never on
`no`, and never before this point: a session lost between the verdict resolving and the checkpoint
answer, or a user who answers `no`, must not leave `next_action: start implement` on disk for a
plan whose continuation was never confirmed:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "thorough_plan" --phase-index 3 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start implement" --artifact "{task_dir}/current-plan.md" || true
```

Only then proceed to Phase 4.

## Formulation quality bar (autonomous)

Only evaluated under `AUTONOMOUS` — plain `/run` never evaluates this bar and proceeds straight from Checkpoint B to Phase 4 exactly as it does today. This bar sits between Phase 3 (Thorough Plan) and Phase 4 (Implement); it is the sole safety substitute for the human checkpoint that autonomous mode removes at Checkpoint B.

- **Medium/Large:** require the `thorough_plan` critic loop to have converged with a **PASS** verdict. Read the verdict `thorough_plan` returns (or its session-state `## Current stage` trail) — exhausting `max_rounds` while the last critic verdict is still REVISE does **NOT** pass the bar, even though `thorough_plan` itself terminates normally at the round cap.
- **Small:** require BOTH (a) the post-plan smoke gate to PASS, AND (b) the confidence signal to be `>= QUOIN_AUTONOMOUS_CONFIDENCE_THRESHOLD` (default `0.7`). Small skips `/specify` and `/architect`, so the confidence signal comes from the single-pass `/plan` skill's own `confidence: <float 0..1>` line (see `plan/SKILL.md`) — optionally combined with an `enrich`-emitted confidence value (if enrich ran and emitted one), in which case take the **minimum** of the two.
- **Fast route:** require `min(triage_confidence, enrich_confidence_if_present) >= QUOIN_FASTPATH_CONFIDENCE_THRESHOLD` (default `0.8`) — stricter than the Small path's `0.7` default, since no plan was critiqued. `triage_confidence` is the confidence value Phase 1.6 recorded at the fast-route decision; the minimum-of-two shape deliberately mirrors the Small bullet's idiom above. This is a deliberate tightening beyond the spec's FR-9 text, recorded here as a spec deviation.
- **Below the bar → HARD STOP.** This is Hard-stop #6 (below-bar formulation) in "## Autonomous hard stops" above — write the halt-sentinel (`phase: formulation`) before exit, then stop. Do **NOT** enter Phase 4 / Execution on a formulation that hasn't cleared the bar. On the fast route this is the SAME hard-stop and the SAME halt-sentinel, with a reason string naming the fast-path route — no seventh hard-stop site, no new schema field, no supervisor reader change.
- The runtime-neutral shape of this bar is mirrored in `quoin/core/skills/run.md`'s "Autonomous mode (opt-in)" section.

## Phase 4 — Implement

Spawn `/implement` as a subagent session, passing path to `<task_dir>/current-plan.md` (where `<task_dir>` is resolved via `python3 __QUOIN_HOME__/scripts/path_resolve.py --task <task-name> [--stage <N-or-name>]` in Setup §) and all repo paths. Because the user invoked `/run` and confirmed at Checkpoint B (or, on the fast route, at Checkpoint A1 — Checkpoint B never fires on that route), the `/run` exception in `implement/SKILL.md` applies.

Emit the dispatch envelope (above), skill: `implement`, `return: envelope` — placed after the sentinel prefix zone per the fast-route ordering rule below.

**On the fast route,** dispatch this spawn with model opus, and the spawn prompt's FIRST token must be bare `[no-redispatch]` — any `[autonomous]` / `[no-interactive]` / `[quoin-onbehalf]` markers follow it, in that order. This order is load-bearing: `implement/SKILL.md`'s §0 conditions on the prompt "starting with" a `[no-redispatch]` form, and §0‴ likewise — the wrong order lets `/implement` silently down-dispatch to Sonnet while every mechanical check still passes. On the full path this dispatch is unchanged (default model tier, no forced sentinel).

**Small-profile cost honesty.** This Opus dispatch is unconditional on the fast route regardless of
task profile, including Small — the profile that triggers Phase 1.6 evaluating mode with no tag at
all. On Small, this is plausibly net MORE expensive than the full path: Small already skips
`/architect` and already gets a single-pass review, so the fast route's only saving on Small is one
Opus planning pass, while this dispatch moves `/implement` from Sonnet to Opus on what is typically
the run's largest token consumer. Medium and Large are genuinely favorable — see the Cost estimate
section below for the honest net-cost direction per profile.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, the on-behalf write per "On-behalf cost capture" above (phase=implement, model=opus on the fast route, sonnet otherwise) runs FIRST. Either way (on-behalf write above, or under opt-out the child's own self-write), THEN verify the cost ledger has a new entry for the `implement` phase. If still not present, append a best-effort entry with `unknown-implement-<timestamp>`.

Under `AUTONOMOUS`, once Checkpoint C confirms (gate passed, continuing to review), also write the phase's completion sentinel `autonomous-progress-{task}/implement.done` (atomic write — T-05/T-10 write-site map).

After implement completes, run `/gate` inline (read `/gate/SKILL.md` from the same session and execute the gate process directly — do not spawn a subagent). Step 5 audit-log persistence applies in inline mode per the gate skill's existing rule.
- Standard level for Small/Medium
- Full level for Large

**Checkpoint C:**
```
Phase complete: Implementation
Gate: PASSED / FAILED

Summary:
- <files created/modified>
- <tests written/passing>
- <any deviations from plan>

Continue to review? (yes / no / show changes)
```

If the gate **failed**: present the failures and ask "Fix and retry, or stop?"
- "fix" → spawn `/implement` again for the failing items (on the fast route, same model-opus / leading-`[no-redispatch]` dispatch as the primary Phase 4 spawn above), then re-run `/gate` inline (post-implement boundary — same inline mechanism as the primary path; audit-log persistence applies per `/gate/SKILL.md`). Re-dispatch inherits the dispatch envelope (above), unchanged.
- "stop" → halt, preserve artifacts

**(fast route only)** a third option, "escalate to full", is also offered here. Escalation is ONE
atomic unit — perform ALL of the following, in order, whenever escalation is chosen (interactively
or via the `[autonomous]` NEEDS-DECISION path below). Steps 1–4 (the durable route-state writes)
apply on BOTH paths; step 5 (re-enter at the architect phase) applies on the interactive path
only — the `[autonomous]` NEEDS-DECISION path writes the halt-sentinel and stops per "## Autonomous
hard stops" instead of re-entering, so a human resuming the task later is what actually re-enters
Phase 2, not this step. **Ordering is crash-safe for route recovery by construction** — the
invariant established is the one stated here, no broader: an interruption after any step can no
longer leave `Route: fast` readable anywhere while the recovery sentinels are already gone.
`triage-decision.md` is rewritten first — it is the durable fallback Resume Step 0b's rule 2
reads, so it must never be left stale; the stub's own `Route:`/`Review shape:` lines are stripped
next, since those are Step 0b's rule-1 source — checked BEFORE the fallback — and therefore the
single most dangerous thing to leave stale; only once both route-recovery sources agree are the
completion sentinels deleted; the stripped stub itself is deleted only after the sentinels are
gone, so no window exists in which `current-plan.md` is absent while `architect.done` /
`thorough_plan.done` still claim planning finished (a resume in that window would select
implement with no plan to dispatch against):
1. Set the orchestrator's in-session `route` variable to `full` for the remainder of this session.
   This is what makes the re-entered architect and planning skip conditions (Phase 2's and Phase
   3's `route=fast` clauses) evaluate false on re-entry — without this step, re-entry would
   re-skip the very phases escalation exists to run.
2. rewrite `triage-decision.md` with the flipped route (`route=full`).
3. When the stub carries `provenance: fast-path-triage`, strip its `Review shape:` line AND, in
   the same step, its `Route:` line (never the review-shape line alone: a surviving `Route: fast`
   line would let Resume Step 0b
   read the very route just abandoned back into effect, re-arming both phase skips on a later
   resume) — since a surviving single-pass declaration would apply the cheapest review precisely on the path taken because the task turned out harder than routed. Do NOT delete the stub outright
   at this step — the outright delete is step 5, after the sentinels are gone. Never strip or
   delete a
   `current-plan.md` that lacks the provenance marker — that means a real plan already superseded
   the stub, and this escalation path does not apply to it.
4. DELETE `autonomous-progress-{task}/architect.done` and `autonomous-progress-{task}/thorough_plan.done`.
   `implement.done` does not exist yet at this site — this escalation offer sits in the gate-FAILED
   branch of Checkpoint C, strictly before Phase 4 writes `implement.done` — so there is nothing to
   delete here; the two review-phase escalation sites below (Phase 5) fire AFTER `implement.done`
   exists and must delete it too, see there.
5. (optional cleanup) delete the stripped stub `current-plan.md` outright — safe now that the
   completion sentinels are gone. Same provenance condition as step 3: this delete applies ONLY
   when the file carries `provenance: fast-path-triage`; if step 3 no-opped because the marker was
   absent (a real critic-reviewed plan), this step no-ops too — never delete it.
6. rewind the run-state record so a resumed session re-enters at architect, not wherever
   the run actually halted:
   ```
   python3 __QUOIN_HOME__/scripts/run_state.py --write \
     --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
     --phase fast_path_triage --phase-index 1 --subphase "" --step "" \
     --at-stage-boundary true --route full --profile "{profile}" \
     --next-action "start architect" || true
   ```
7. re-enter at the architect phase.

Completed implementation work is preserved. On the full path this option does not exist; the
existing "fix" / "stop" choice above is unchanged.

Under `AUTONOMOUS`: auto-select "fix" and retry once (retry cap = 1 automatic retry). If the gate still fails after that one retry, this is Hard-stop #2 (Gate FAIL after the retry cap) — write the halt-sentinel per "## Autonomous hard stops" before exit, then stop. Never fall back to a silent proceed, and never ask. **On a fast-route run, this same escalation option ALSO routes through the existing NEEDS-DECISION return path** (`needs-decision-{task}.md`), writing it in addition to the halt-sentinel already written above — never instead of it — rather than a silent auto-select — no seventh hard-stop, same NEEDS-DECISION mechanism used elsewhere under `[autonomous]`. **Writer, named:** this NEEDS-DECISION sentinel is not written by a spawned subagent here — the `/run` orchestrator itself, inline, invokes the shared guard directly: `python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill run --site fast-route-escalation-checkpoint-c --reason "<gate-failure summary>" --resume-hint "re-run /run --resume <task-name>"`, echoes its `gate-result: NEEDS-DECISION` block, and stops — so an autonomous fast run hitting this branch always leaves a terminal signal on disk, never a silent stall. Once escalation's atomic unit above (route flip, sentinel/stub cleanup) has been performed, this is the durable record of WHY the run stopped and WHERE to resume it.

If the user says "show changes": run `git diff --stat` and display, then re-ask.

**Once the gate has PASSED** — on the first attempt, or after the fix/retry loop above resolves
to a pass — and the run is continuing to review (never on the fast-route escalation branch above,
which writes its own `fast_path_triage` rewind instead): independent of `AUTONOMOUS`, this write is
unconditional across interactive and autonomous mode, so a plain (non-autonomous) `/run` session
gets the same phase-boundary resumability record. Writing only here — after the gate result is
known, never before it — means a crash between the implement dispatch and this point can never
leave `next_action: start review` on disk for a gate that in fact FAILED or is still retrying:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "implement" --phase-index 4 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start review" --artifact "{task_dir}/current-plan.md" || true
```

## Phase 5 — Review

Spawn `/review` as a **fresh subagent session** (unbiased assessment requires clean context). Pass plan path, architecture path, spec path (if it exists), and repo paths.

Emit the dispatch envelope (above), skill: `review`, `return: envelope`.

Then append a ``[quoin-bundle]`` block to the spawn prompt in two steps:

1. Run via the Bash tool (resolve `<task-name>` to the actual task and `<N>` to the resolved stage number; OMIT `--stage` entirely for single-stage/grandfathered tasks):
   ```bash
   python3 "__QUOIN_HOME__/scripts/context_bundle.py" --task "<task-name>" --stage <N> --wrap 2>/dev/null || true
   ```
2. If the command printed anything, append its stdout VERBATIM — as its own lines, markers included (`--wrap` emits `[quoin-bundle]` / `[/quoin-bundle]` on their own lines) — at the END of the spawn prompt, AFTER the sentinel prefix zone (``[autonomous]`` / ``[no-redispatch]`` / ``[no-interactive]`` / ``[quoin-onbehalf]`` stacking unchanged). If it printed nothing (script absent, task unresolvable, or all members missing), append no block — safe degradation to current wholesale-read behavior.

Read the review output (`review-*.md`) and check the verdict.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, the on-behalf write per "On-behalf cost capture" above (phase=review, model=opus) runs FIRST. Either way (on-behalf write above, or under opt-out the child's own self-write), THEN verify the cost ledger has a new entry for the `review` phase. If still not present, append a best-effort entry with `unknown-review-<timestamp>`.

Under `AUTONOMOUS`, once Checkpoint D confirms (APPROVED or accepted, gate passed), also write the phase's completion sentinel `autonomous-progress-{task}/review.done` (atomic write — T-05/T-10 write-site map).

Independent of `AUTONOMOUS`, the phase-boundary run-state write for Phase 5 is verdict-conditional
and never fires before the verdict is known — writing an unconditional `next_action: start
end_of_task` here, before the verdict is read, is what let a plain `/run` resume skip the gate and
re-enter `end_of_task` after a CHANGES_REQUESTED or BLOCKED review. Each verdict branch below writes
its own value, at the point where that branch's outcome is final:

**If APPROVED:** run `/gate` inline (Full level, post-review — read `/gate/SKILL.md` from the same
session and execute the gate process directly). Step 5 audit-log persistence applies in inline mode
per the gate skill's existing rule. Once — and only once — that gate PASSES, proceed to Checkpoint D
(the phase-boundary write itself is deferred to the point where Checkpoint D resolves to continue —
see below the checkpoint, never here). If the post-review gate instead FAILS, do not proceed to
Checkpoint D — "Gates are blocking" below applies.

**If CHANGES_REQUESTED:** present the issues to the user. Offer:
1. **"fix"** → before spawning `/implement` again, write the resume record naming implement as the
   next step (this is the fix for the plain-`/run` resume gap — the record now names the phase this
   branch is about to actually dispatch, not the phase the review request as a whole eventually
   reaches):
   ```
   python3 __QUOIN_HOME__/scripts/run_state.py --write \
     --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
     --phase "review" --phase-index 5 --subphase "" --step "" \
     --at-stage-boundary true --route "{route}" --profile "{profile}" \
     --next-action "start implement" --artifact "{task_dir}/current-plan.md" || true
   ```
   Then spawn `/implement` again with the review issues as the spec (on the fast route, same model-opus / leading-`[no-redispatch]` dispatch as the primary Phase 4 spawn above). Re-dispatch
   inherits the dispatch envelope (above), unchanged. After fix-implement completes, re-run the post-implementation gate inline (same level as before; audit-log persistence per
   `/gate/SKILL.md`). Then re-spawn `/review`. Cap at 3 review rounds to prevent infinite cycling.
2. **"accept"** → treat as approved despite requested changes. Log this decision in session state.
   Proceed to Checkpoint D — accepting is the same terminal outcome as an approved verdict for
   resume purposes, so it reaches the same deferred write below the checkpoint as the APPROVED
   branch above.

**(fast route only)** after the 3-round CHANGES_REQUESTED cap, a third option, "escalate to full",
is also offered here — the same atomic unit as the Checkpoint C escalation above, same crash-safe
order (triage-decision.md rewritten first, then the stub's route lines stripped, then the
sentinels, then the optional outright stub delete — see Checkpoint C's
own ordering rationale): set the in-session `route` to `full`; rewrite `triage-decision.md` with
the flipped route; strip the stub's lines per the same provenance-conditioned rule as Checkpoint
C (strip both its `Review
shape:` line and its `Route:` line together — never the review-shape line alone, and never an
outright delete at this step); THEN, once both
route-recovery sources agree, DELETE `autonomous-progress-{task}/architect.done`,
`autonomous-progress-{task}/thorough_plan.done`, AND `autonomous-progress-{task}/implement.done`
plus any `implement.*.done` sub-sentinels — `implement.done` already exists by this point (Phase 4
wrote it once Checkpoint C confirmed), unlike at the Checkpoint C site above, so it must be deleted
here too, or a resumed escalated run would skip re-implementation entirely and jump straight into
re-reviewing the untouched fast-route code; optionally delete the stripped stub outright now that
the sentinels are gone (provenance-marked stubs only — same condition as Checkpoint C step 5);
THEN rewind the run-state record so a resumed session re-enters at architect, not wherever
the run actually halted:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase fast_path_triage --phase-index 1 --subphase "" --step "" \
  --at-stage-boundary true --route full --profile "{profile}" \
  --next-action "start architect" || true
```

Only THEN re-enter at the architect phase. Completed
implementation work is preserved. On the full path this option does not exist.

Under `AUTONOMOUS`: auto-select "fix" at each round — never auto-select "accept". If still CHANGES_REQUESTED after the 3-round cap, this is Hard-stop #3 (Review CHANGES_REQUESTED after 3 rounds) — write the halt-sentinel per "## Autonomous hard stops" before exit, then stop. **On a fast-route run, offer "escalate to full" as a third option alongside this halt — same mechanism as the Checkpoint C escalation above**, performed per the atomic unit above; under `[autonomous]` this ALSO routes through the same NEEDS-DECISION return path, writing it in addition to the halt-sentinel already written above — never instead of it — rather than a silent auto-select — named writer (same shared guard as Checkpoint C): `python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill run --site fast-route-escalation-changes-requested --reason "<3-round CHANGES_REQUESTED summary>" --resume-hint "re-run /run --resume <task-name>"`. On the full path this branch is unchanged — still a bare halt.

**If BLOCKED:** present the blocking issues. **STOP.** Do not offer to continue. Before stopping,
write a non-parsing `next_action` marker — never `start end_of_task` and never `start review` (the
review just ran and its result was BLOCKED, not something to re-enter): a plain `/run --resume`
that reads this record back sees a `next_action` that fails to match the `start <phase>` format
(mirroring the `run complete` terminal marker) and correctly falls through to the pre-T-09
session-state read rather than resuming into a phase this verdict never cleared:
```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "review" --phase-index 5 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "run blocked" --artifact "{task_dir}/current-plan.md" || true
```
(On the fast-route escalation choice below, this marker is immediately superseded by that branch's
own `fast_path_triage` rewind — write it here regardless, since the user may choose the bare stop
instead of escalating.) Tell the user: "Review found blocking
issues. The workflow cannot continue until these are resolved. Artifacts are preserved at
`.workflow_artifacts/<task-name>/`." **(fast route only)** this bare stop is the full-path behavior; on the fast route, "escalate to full" is offered alongside it instead — the same atomic unit as the Checkpoint C escalation above, same crash-safe order (in-session `route` flip to `full`; rewrite `triage-decision.md`; strip the stub's `Review shape:` and `Route:` lines per the same provenance-conditioned rule — no outright delete yet; THEN DELETE `autonomous-progress-{task}/architect.done`, `autonomous-progress-{task}/thorough_plan.done`, AND `autonomous-progress-{task}/implement.done` plus any `implement.*.done` sub-sentinels, since it already exists at this site too; optionally delete the stripped stub outright once the sentinels are gone (provenance-marked stubs only — same condition as Checkpoint C step 5); then rewind the run-state record so a resumed session re-enters at architect, not wherever the run actually halted: `python3 __QUOIN_HOME__/scripts/run_state.py --write --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" --phase fast_path_triage --phase-index 1 --subphase "" --step "" --at-stage-boundary true --route full --profile "{profile}" --next-action "start architect" || true`; only then re-enter at the architect phase). Completed implementation work is preserved.

Under `AUTONOMOUS`: this is Hard-stop #1 (Review BLOCKED) — write the halt-sentinel per "## Autonomous hard stops" before exit, then stop (no `AskUserQuestion`, no silent proceed). **On a fast-route run, offer escalation rather than a bare stop — same mechanism as the Checkpoint C escalation above**, performed per the atomic unit above; under `[autonomous]` this ALSO routes through the NEEDS-DECISION return path, writing it in addition to the halt-sentinel already written above — never instead of it: the halt-sentinel is what the Stage-2 supervisor reads, and a supervised autonomous fast run hitting review BLOCKED always terminates for a human rather than relaunching unattended into the full path — named writer (same shared guard as Checkpoint C): `python3 __QUOIN_HOME__/scripts/decision_gate_guard.py fail-closed --task <task-name> --skill run --site fast-route-escalation-blocked --reason "<BLOCKED summary>" --resume-hint "re-run /run --resume <task-name>"`. On the full path this branch is unchanged — still a bare stop, since the full path never skipped the phases escalation would recover.

**Checkpoint D** (after APPROVED or accepted):
```
Phase complete: Review
Verdict: APPROVED
Artifact: <task_dir>/review-<N>.md (where <task_dir> = `python3 __QUOIN_HOME__/scripts/path_resolve.py --task <task-name> [--stage <N-or-name>]`; architecture.md ALWAYS at task root per D-03)
Gate: PASSED

Summary:
- <key findings>
- <issues flagged (if any)>

Finalize and push? (yes / no / show review)
```

Once Checkpoint D resolves to continue — the user answers `yes`/`y`/`continue`/`go` (reached only
after an APPROVED verdict with the gate PASSED, or an "accept" of a CHANGES_REQUESTED review), or
under `AUTONOMOUS` the checkpoint auto-resolves because that same condition already held — write
the phase-boundary record. Never before this point, and never on `no`: a session lost between the
verdict/gate resolving and the checkpoint answer, or a user who answers `no`, must not leave
`next_action: start end_of_task` on disk — that gap is what let a lost session or a declined
checkpoint resume straight into Phase 6's commit-push-archive on a confirmation that was never
actually given:

```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "review" --phase-index 5 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "start end_of_task" --artifact "{task_dir}/current-plan.md" || true
```

Only then proceed to Phase 6.

## Phase 6 — End of Task

Spawn `/end_of_task` as a subagent session. Because the user invoked `/run` and confirmed at Checkpoint D, the `/run` exception in `end_of_task/SKILL.md` applies. All 8 steps run as normal (pre-flight, commit, push, lessons, session state, cost aggregation, archive, report).

Emit the dispatch envelope (above), skill: `end_of_task`, `return: envelope`.

Unless `QUOIN_INLINE_COST_CAPTURE=0`, this spawn also follows "On-behalf cost capture" above (phase=end-of-task, model=sonnet) — see the `end_of_task` note in its `## Autonomous mode bootstrap` section (post-`<!-- §0tripleprime-end -->`, not the old `§0-end`-only anchor; no T-06 skip predicate to pair with; this write is additive, not a suppression-replace).

Commit and PR text composed under `/run` follows the same clean-authored-content rule as standalone `/end_of_task`/`/pr` — `/run` composes no commit/PR text itself, only delegates. Full rule: __QUOIN_HOME__/memory/clean-authored-content.md.

**F-09 (known ordering gap, accepted):** this on-behalf `end-of-task` row is appended AFTER the `/end_of_task` subagent returns — but Step 6.4 (cost aggregation) inside that subagent already reads the ledger before this row exists. `/run`'s own final cost report therefore systematically undercounts by exactly one phase (the `end-of-task` row itself) under default-ON. This is pre-existing (was true under opt-in too) and now the default; not fixed this round — note it explicitly in the Checkpoint D / completion report rather than silently under-reporting.

Under `AUTONOMOUS`, once `/end_of_task` returns successfully, also write the phase's completion sentinel `autonomous-progress-{task}/end_of_task.done` (atomic write — T-05/T-10 write-site map). This is a separate sentinel from `end_of_task`'s own terminal `autonomous-done-{task}.md` (T-11) — this one records phase-level progress consistent with the other 8 phases; the done-sentinel is the overall supervisor-loop terminal signal.

Independent of `AUTONOMOUS` — these two writes are unconditional in both interactive and autonomous mode. First the boundary write, THEN the clear — in that exact order, because `--clear` sets `active: false` and a `--write` after it would resurrect the record as active:
```
python3 __QUOIN_HOME__/scripts/run_state.py --write \
  --project-root "$PROJECT_ROOT" --task "{task}" --session-id "$CLAUDE_CODE_SESSION_ID" \
  --phase "end_of_task" --phase-index 6 --subphase "" --step "" \
  --at-stage-boundary true --route "{route}" --profile "{profile}" \
  --next-action "run complete" --artifact "{task_dir}/current-plan.md" || true
python3 __QUOIN_HOME__/scripts/run_state.py --clear --project-root "$PROJECT_ROOT" --task "{task}" || true
```


### end_of_task failure recovery (inline finish)

If the Phase 6 subagent dies (stream-idle timeout, error return, context exhaustion) or is skipped, the orchestrator MUST — before any archive step — perform the cost aggregation itself, following `end_of_task/SKILL.md` Sub-phase B Steps 4 and 5 as a pointer, never a copy: read the ledger, apply the col-8 inline-first precedence rule, and write `.workflow_artifacts/<task-name>/cost-summary.json` at the identical path Sub-phase B resolves. On any aggregation failure, still write a partial `cost-summary.json` carrying `fallback_used: true` and a non-empty `fallback_note` naming the inline-finish origin. Only then archive. Bounded to the same `~15 tool uses` scope cap Sub-phase B already declares.

After completion, present the final report:
```
Task complete: <task-name>

Branch: <branch-name> → pushed to origin
Profile: <Small|Medium|Large>
Route: <full|fast> (fast: planning was skipped by routing, not by profile — see Phase 1.6)
Phases: discover(<skipped|ran>), architect(<skipped|ran>), plan(<N> rounds), implement, review(APPROVED), finalized
Archived: .workflow_artifacts/<task-name>/ → finalized/
Cost ledger: .workflow_artifacts/<task-name>/cost-ledger.md (<N> sessions tracked)
Cost: $X.XX (N sessions tracked)

Next: run /pr to create a pull request from the branch.
```

The `Cost:` line is mandatory in every report — never silently omitted (AC-7). Its value comes from `python3 __QUOIN_HOME__/scripts/cost_summary.py --format json <task-dir>/cost-summary.json`: total present with `is_partial` false → `Cost: $X.XX (N sessions tracked)`; total present with `is_partial` true → `Cost: ~$X.XX (partial) (N sessions tracked)`; total null, file missing, or aggregation skipped → `Cost: totals unavailable — cost aggregation did not complete`. Carry the F-09 one-phase-undercount note above into this line per its own instruction.

## Checkpoint interaction protocol

At every checkpoint, the orchestrator presents a concise summary and waits for explicit user input:

| Response | Action |
|----------|--------|
| `yes` / `y` / `continue` / `go` | Proceed to next phase |
| `no` / `n` / `stop` | Halt workflow; preserve all artifacts; tell user how to resume manually |
| `show <artifact>` | Display the artifact (architecture / plan / changes / review / discover), then re-ask |
| `skip` | Skip the next phase (only valid for optional phases: discover, specify, architect) |
| Any other input | Treat as feedback or clarification; answer and re-ask |
| **Autonomous** (`AUTONOMOUS=true`) | A checkpoint whose gate/verdict is PASS auto-resolves to "continue" — NO `AskUserQuestion` is called and NO wait for user input. A non-PASS checkpoint (gate FAILED, review CHANGES_REQUESTED/BLOCKED, below-bar formulation) is NEVER a silent proceed — it routes to the hard-stop / halt-sentinel logic instead. |

**Never proceed without explicit confirmation** (non-autonomous mode). Ambiguous responses → ask for clarification. Under `AUTONOMOUS`, confirmation is auto-supplied per the row above — but a non-PASS result is never a silent proceed.

## Resume

If invoked as `/run --resume <task-name>` (or "resume the run for <task-name>"):

**Step 0 (T-09) — re-establish autonomous mode from the marker, BEFORE any other
decision point.** Read `.workflow_artifacts/memory/autonomous-run-{task}.marker`
(the T-05/T-10 marker sentinel) for `<task-name>` FIRST — before reading session
state, before checking completion sentinels, before anything else:
- If the marker exists: set `AUTONOMOUS=true` immediately, whether or not the
  resume invocation's own text carries `--autonomous` (belt-and-suspenders, D-06).
  This is what guarantees a headless `claude -p "/run --resume --autonomous
  {task}"` relaunch never reverts to interactive and stalls waiting on a prompt
  no one is present to answer.
- If the marker is absent: `AUTONOMOUS` is determined normally from the invocation
  text — plain (non-autonomous) resume, unchanged from pre-T-09 behavior.

**Step 0b (T-18) — recover the fast-route state BEFORE Step 1 determines the next
phase.** Without this step, a relaunched autonomous fast run silently reverts to
the full path's model tier and loses escalation eligibility. Read, in order:
1. `Route:` from `<task_dir>/current-plan.md`, if that file exists — this is the
   `D-02` provenance marker the fast-route stub (Phase 1.6) already writes; a
   plan a real `/thorough_plan` pass produced carries no `Route:` line at all,
   which reads as `full`. Match the line ANCHORED inside the state block —
   `^Route:\s*(fast|full)\s*$` — never an unanchored whole-file substring
   search: a plan that merely DOCUMENTS the fast route in descriptive prose
   (as this very task's own planning artifacts do, containing the literal
   phrase "Route: fast" outside the state block) must not be misread as
   taking it.
2. If `current-plan.md` does not exist or carries no `Route:` line, fall back to
   `<task_dir>/triage-decision.md`'s recorded route, if that file exists.
3. If neither source yields a route, default to `full` — today's only behavior,
   preserved.
Set the orchestrator's `route` variable from this read before Step 1 runs, so
every route-conditional dispatch (the Opus `/implement` spawns and their leading
`[no-redispatch]` sentinel, and Phase 5's escalation eligibility) behaves
identically whether this is a first pass or a resumed session. This composes
correctly with the mid-flight escalation mechanism: escalation strips BOTH the
stub's `Review shape:` line and its `Route:` line
TOGETHER (never the review-shape line alone — a lone `Review shape:` strip
would leave `Route: fast` matching rule 1 above and reverting the escalation;
see the escalation sites' own atomic-unit description) before re-entry, so
Step 0b naturally falls through to `triage-decision.md`, which escalation also
rewrites with the flipped route — a run that escalated and was then
interrupted resumes as `full`, not `fast`.

**Step 0c — validate the staleness knob before either run-state read below.** Both
reads in Step 1 and Step 1b pass `QUOIN_RUN_STATE_STALE_DAYS` as `--max-age-days`.
Reject anything that is not a plain non-negative integer and fall back to the
documented default of 1 — mirroring the frozen shell reader's own guard at
`run_state_read.sh:26-36` (that reader interpolates the same knob into a `find
-mtime` arithmetic expansion, an injection sink `run_state.py`'s own
`--max-age-days` argparse type does not share, but the guard is duplicated here
anyway so the knob is validated the same way at every read site):
```bash
_RAW_STALE_DAYS="${QUOIN_RUN_STATE_STALE_DAYS:-1}"
case "$_RAW_STALE_DAYS" in
  ''|*[!0-9]*) _RUN_STATE_STALE_DAYS=1 ;;
  *) _RUN_STATE_STALE_DAYS="$_RAW_STALE_DAYS" ;;
esac
```
Use `"$_RUN_STATE_STALE_DAYS"` (quoted) in place of the raw `${QUOIN_RUN_STATE_STALE_DAYS:-1}`
expansion at both call sites below.

**Step 1 (T-09) — determine the next phase from completion sentinels, never from
session-state prose alone, when the sentinel contract is present.** Read
`.workflow_artifacts/memory/autonomous-progress-{task}/` (the T-05/T-10 write-site
directory) for `<task-name>`:
- A phase whose `{phase}.done` sentinel EXISTS is finished — never re-run it, even
  if session-state prose about that phase is ambiguous, stale, or missing.
- A phase whose `{phase}.done` sentinel is ABSENT is not finished — never skip it.
- If a phase's sentinels are only PARTIALLY present at sub-phase granularity
  (`{phase}.{subphase}.done` for some but not all sub-phases of one phase — e.g. a
  long `implement` phase that checkpointed partial task batches), resume that phase
  at the first sub-phase lacking its own completion sentinel, rather than
  restarting the whole phase from scratch.
- **Field invariant (stated once here, applies to every read of this record anywhere in this
  file):** `phase`'s meaning is keyed on `at_stage_boundary` — two cases, not one. At
  `at_stage_boundary: true`, `phase` names the LAST COMPLETED phase — every boundary write sets
  it to the phase that just finished (e.g. `--phase discover --next-action "start enrich"` means
  discover is done and enrich is next), and every escalation rewind sets it to the last phase
  whose output survives the escalation (`fast_path_triage`, since the rewind deletes the
  architect / thorough_plan / implement sentinels), never to the re-entry target itself. At
  `at_stage_boundary: false`, `phase` names the phase IN FLIGHT — see the
  within-sub-phase re-entry rule below for the one writer that uses this case. `next_action`
  names what to start next, in the fixed format `start <phase>`, for every writer except two
  documented terminal markers that deliberately do NOT parse against that format — `run
  complete` (Phase 6's final write, before `--clear`) and `run blocked` (the Phase 5 BLOCKED
  write) — both of which a reader treats as "returned nothing" and falls through past, per the
  extended fallback bullet below. A reader that needs "which phase to resume at" reads
  `next_action`, never `phase` — `phase` alone is ambiguous between "resume here" and "this
  already ran," and `phase_index` is informational only (it is not injective — several phases
  share an index — so it never decides anything by itself).
- If NO `autonomous-progress-{task}/` directory exists for the task (a resume that
  predates the sentinel contract, or a non-autonomous run that never wrote one), check for
  a fresh active run-state record next: `python3 __QUOIN_HOME__/scripts/run_state.py --read
  --project-root "$PROJECT_ROOT" --task "{task}" --max-age-days "$_RUN_STATE_STALE_DAYS"
  --fields active,next_action || true`. If
  that returns `active=true` with a non-empty `next_action` matching the format `start <phase>`
  where `<phase>` is one of the known phase names (`discover`, `enrich`, `specify`,
  `fast_path_triage`, `architect`, `thorough_plan`, `implement`, `review`, `end_of_task` — the same
  roster the `--phase` writers above use), parse the phase name out of it and resume there — this is
  what makes a plain (non-autonomous) `/run`, which writes no `.done` sentinels, first-class
  resumable too. If `next_action` is non-empty but does NOT match that format (e.g. a terminal
  marker like `run complete`, or any future value whose detail lives in `subphase`/`step` instead),
  treat this tier as if it had returned nothing and fall through. Only when this read ALSO returns
  nothing, or returns a `next_action` that fails to parse against the known-phase roster, does the
  plan fall back to the pre-T-09 behavior:
  1. Read `.workflow_artifacts/memory/sessions/<latest>-<task-name>.md` to find the last completed phase.
  2. Identify the next phase.

**Step 1b (`D-53`) — within-sub-phase re-entry.** Read the run-state record once:
```
python3 __QUOIN_HOME__/scripts/run_state.py --read \
  --project-root "$PROJECT_ROOT" --task "{task}" \
  --max-age-days "$_RUN_STATE_STALE_DAYS" \
  --fields schema,active,phase,phase_index,subphase,step,at_stage_boundary,next_action,notes_path,resume_command,updated_at
```
Empty stdout means no usable record — absent, unreadable, schema-forward, or older than the
freshness window — and Resume proceeds to Step 2 exactly as today. When a record IS
returned, it refines — never overrides — the sentinel-derived answer, across four tiers:

1. **which phase** — `{phase}.done` sentinels decide first (Step 1 above); when no
   `autonomous-progress-{task}/` directory exists, a fresh active record's `next_action`
   decides instead (Step 1's extended fallback bullet and field invariant above — never
   `phase`, which names the last completed phase, not the resume target); only when both
   of those are absent does the pre-T-09 session-state prose apply.
2. **which sub-phase** — `{phase}.{subphase}.done` sentinels decide exactly as Step 1 does
   today. The record NEVER overrides them.
3. **where inside the first incomplete sub-phase** — the record's `step`, and only there.
   Sub-phase comparison is STRING EQUALITY against the sub-phase Step 1 selected — the
   first sub-phase with no `.done` sentinel. A `subphase` naming a sub-phase whose `.done`
   already EXISTS is DISCARDED, not obeyed; so is a `subphase` naming anything else — except
   in the no-sub-phase-sentinel case below, where there is no tier-2 selection for it to be
   compared against in the first place. There is no ordering over sub-phase identifiers —
   arbitrary names (e.g. `batch-3`, `round-2-critic`) are permitted and neither ranked nor
   compared numerically.
   **No-sub-phase-sentinel case:** when the selected phase has NO `{phase}.{subphase}.done`
   sentinels written for it at all — distinct from "every one of them is done" — tier 2 has
   no candidate set to select from, and tier 3 accepts the record's own `subphase` directly
   rather than comparing it against an empty selection (the `active`/`at_stage_boundary:
   false` requirements below still apply unchanged). This is the path that exercises tier 3
   in production today: `/thorough_plan`'s round-boundary write is the sole writer of a
   non-empty `subphase` anywhere in the tree, and no phase writes `thorough_plan.round-N-*.done`
   sentinels, so `thorough_plan`'s tier-2 candidate set is always empty.
4. **what to do next** — the record's `next_action` and `notes_path`. A `notes_path` naming
   a file that does not exist means "no notes", never an error or a reason to discard the
   record.

Re-enter at `step` only when the record is fresh (the read above returned something at
all), `active: true`, agrees with the selected phase by string equality and agrees with the
selected sub-phase by string equality (or, in the no-sub-phase-sentinel case above, is accepted
directly since there is no tier-2 selection to compare against), AND
carries `at_stage_boundary: false`; otherwise restart the sub-phase as today. This requirement is
exactly the `at_stage_boundary: false` case of the Field invariant above — `phase` names the
phase IN FLIGHT there, which is what makes the string-equality comparison against the
Step-1-selected phase meaningful instead of comparing against a phase that already finished. A
record whose `schema` exceeds the one this step knows is treated as ABSENT and Resume falls to the
next tier.

**Step 1c — re-anchor the run-state record to THIS session, after the staleness-gated
reads above and before any phase work.** The record's stored `session_id` is the
creator's — a dead session after any resume or autonomous supervisor relaunch — and the
PreCompact hook matches by exact equality, so without this write the run-aware row would
never fire again for the whole resumed run. Adoption is gated behind an explicit flag so
ordinary refresh writes keep preserving the creator id, and the call passes the SAME
validated freshness window Steps 1/1b read with — the writer refuses to adopt a record
older than that window, so a stale record those reads already treated as absent is never
revived by this write (Steps 1/1b are read-only, so running after them changes no input
they consumed):
```bash
python3 __QUOIN_HOME__/scripts/run_state.py --write --require-existing --adopt-session \
  --project-root "$PROJECT_ROOT" --task "{task}" \
  --session-id "$CLAUDE_CODE_SESSION_ID" \
  --max-age-days "$_RUN_STATE_STALE_DAYS" || true
```
Require-existing semantics inherit every field not supplied on this call from the stored
record — the write still refreshes `updated_at` and re-bounds the artifacts list, as
every write does. An absent, cleared, stale, or schema-forward record makes this a no-op
and Resume proceeds normally on the tiers above; an `unknown*` probe-fallback session id
is refused by the writer, leaving the stored id untouched.

**Step 2 — announce and proceed.** Tell the user: "Resuming `<task-name>` from
Phase N (`<phase-name>`). Phases 1–M already completed." Start from the next
uncompleted phase — do not re-run completed phases.

**Headless autonomous path (T-09):** when Step 0 established `AUTONOMOUS=true`
from the marker, resume proceeds directly into Phase N with ZERO
`AskUserQuestion` calls — the sentinel-derived phase selection above IS the
decision, so there is nothing left to ask. A headless
`/run --resume --autonomous {task}` relaunch with the marker present therefore
raises zero `AskUserQuestion` prompts by construction.

Note: `--resume` is a convention the skill checks for in the input, not a CLI flag.

## Session state tracking

Update `.workflow_artifacts/memory/sessions/<date>-<task-name>.md` after each phase completes. Track:
- Current phase and status
- Completed phases and their outcomes (gate results, review verdicts, rounds taken)
- Any stopped-at checkpoint (so the user can resume manually)

## Subagent session management

Each phase runs as a separate subagent session — never inline. This keeps the orchestrator's context lean across a full pipeline run.

- Pass only file paths and parameters to each subagent — never raw content
- After each subagent completes, read its output artifacts from disk
- Spawning mechanism: same as `/thorough_plan`'s "Invoking each agent" section — the `Skill` tool invokes each subagent as a fresh session. Phases are sequential (not parallel).
- Known gate/SKILL.md diagram inconsistency: `gate/SKILL.md` shows a gate after discover, but `CLAUDE.md`'s workflow sequence does not. This skill follows `CLAUDE.md` — no gate after discover. The gate skill determines context from disk artifacts, so the discrepancy has no runtime effect.
- **Within-phase isolation, autonomous (T-12).** Under `AUTONOMOUS`, the "paths and parameters only" rule above is a HARD requirement, not just a lean-context preference: a phase subagent returns a PATH plus a short summary, never raw file content, so the orchestrator's own transcript stays bounded across a multi-relaunch autonomous span. A phase subagent that nears its own limit writes a checkpoint to disk and returns a structured `PARTIAL` signal instead of exhausting itself mid-phase; the orchestrator responds by dispatching a FRESH subagent to CONTINUE that same phase from the checkpoint (see "Within-phase PARTIAL continuation" under "## Error handling" below). This keeps orchestrator context bounded WITHIN a phase, complementing the cross-phase relaunch a supervisor performs between phases.
- **Return envelope on dispatch.** A phase subagent dispatched with `return: envelope` replies with the return envelope (above) instead of its usual English summary; the orchestrator's own Checkpoint A-D chat summaries are unaffected — the envelope replaces only the subagent's prose.
- **Status and verdict vocabulary.** `status` has four members and is the sole shape discriminator; this stage's run-owned phases emit only `COMPLETE`/`PARTIAL`. A fail-closed phase still emits the shipped `gate-result: NEEDS-DECISION` block, no envelope — `NEEDS-DECISION`/`BLOCKED` are reserved for a later wave. `verdict` carries `PASS` for discover/enrich/specify/architect/implement/end_of_task; review uses `APPROVED`/`CHANGES_REQUESTED`/`BLOCKED`; thorough_plan uses `PASS`/`REVISE`.
- **Self-utilization vs. scope-cap fallback.** A phase subagent SHOULD attempt to read its own transcript utilization to decide when to checkpoint-and-return-`PARTIAL` proactively, before it is forced to stop mid-tool-call. When a subagent cannot resolve its own transcript utilization, it MUST fall back to a fixed tool-use scope cap (mirroring `end_of_task`'s existing "Scope cap: ~N tool uses; if blocked, write to disk and return" pattern) so `PARTIAL` is still returned deterministically rather than the subagent silently running until it is killed.

## Parallel tasks

Multiple `/run` sessions can operate simultaneously on different tasks — each uses its own `.workflow_artifacts/<task-name>/` subfolder. Start each in a separate chat session to avoid shared context. There is no interference as long as tasks target different repos or non-overlapping files.

## Cost estimate

Rough estimates only — `/end_of_task` computes actual costs from the cost ledger and presents them in the final report.

| Profile | Approximate total |
|---------|------------------|
| Small | ~$2.75–$3.50 |
| Medium | ~$3.75–$5.50 |
| Large | ~$6.00–$8.50+ |
| Small, fast route | ~$3.00–$3.75 — plausibly MORE than plain Small, not less; see "Small-profile cost honesty" under Phase 4 |
| Medium, fast route | ~$2.75–$4.00 — genuinely favorable: saves the architect + planning passes, net of moving `/implement` to Opus |
| Large, fast route | ~$5.25–$7.50 — genuinely favorable, same architect/planning saving as Medium, offset upward by the retained `/security_review` OWASP subagent this route keeps unconditionally on Large (see the Large carve-out) — an offset Medium's fast route does not carry |

The fast route's saving comes from skipping `/architect` and `/thorough_plan` entirely (their Opus
planning/critic passes), net of the cost added by forcing `/implement` to Opus instead of Sonnet on
that same run. On Medium/Large that trade is favorable — the skipped phases cost more than the
`/implement` tier bump. On Small, the trade reverses: Small already skips `/architect` and already
runs a single-pass review, so there is nothing left to skip except one Opus `/plan` pass, while the
`/implement` tier bump still applies in full — the fast route's sole justification (cost) does not
hold on the Small profile, and choosing it there is a legitimate choice only when the human
confirming Checkpoint A1 (or the formulation-bar-gated autonomous evaluation) has already accepted
that tradeoff.

## Error handling

- **Subagent failure:** inform the user, offer to retry the phase. If the failed subagent was Phase 6 (`/end_of_task`), see "end_of_task failure recovery (inline finish)" under Phase 6 above before archiving.
- **Gate failure:** present failures, offer to fix (re-run the phase) or stop
- **Git errors:** report and let the user resolve. **Git conflict (Hard-stop #4, autonomous):** under `AUTONOMOUS`, a git conflict (merge/rebase/push conflict, at any phase) is a hard stop — write the halt-sentinel per "## Autonomous hard stops" before exit, then stop; never attempt automatic conflict resolution.
- **Context exhaustion:** save state, instruct user to resume with `/run --resume <task-name>`
- **Stream-idle timeout recovery (orchestrator-only).** If a spawned subagent
  returns a tool_result whose content contains `Stream idle timeout - partial response received`:
  do NOT use SendMessage to resume the dead child (this also
  times out — Apr 28 10:19 incident). Instead, re-dispatch a
  FRESH narrower child:
    a. Halve the scope: if the round was processing N critic
       issues, dispatch a new /revise(-fast) targeting the first
       ⌈N/2⌉ issues only; queue the rest for a follow-up dispatch
       in the same round counter.
    b. Pass the partial output (if any artifact was written to
       disk) to the new child as additional context.
    c. Cap retries at 2 per round; on a third stream-idle
       timeout, escalate to the user with the partial artifact
       and ask whether to proceed to the next phase with a flagged
       artifact, or stop.
  NOTE: This retry only fires for subagents dispatched BY /run.
  Standalone invocations of sub-skills have no automatic retry.
- **Within-phase PARTIAL continuation (autonomous, T-12).** When a phase
  subagent returns a structured `PARTIAL` signal (see "Subagent session
  management" above) instead of its normal phase-complete summary:
    a. Read the checkpoint path the subagent wrote to disk (paths-not-content
       — the orchestrator never reads the subagent's raw transcript).
    b. Dispatch a FRESH subagent for the SAME phase, passing the checkpoint
       path so it resumes the phase's own remaining work rather than
       restarting the phase from scratch.
    c. Repeat until the phase returns its normal phase-complete summary
       (not `PARTIAL`) or a hard-stop condition fires.
    d. `PARTIAL` is the return envelope's `status: PARTIAL` shape;
       `checkpoint` carries the path step (a) reads (full field set in the
       handoff-envelope section above).
  This is a WITHIN-phase mechanism — distinct from a supervisor's
  cross-phase relaunch (`autonomous-progress-{task}/{phase}.done`
  sentinels), and distinct from the stream-idle recovery above (which
  responds to a dead/unresponsive child, not a self-reported `PARTIAL`).
  Only fires under `AUTONOMOUS`; a non-autonomous phase subagent that runs
  long is handled by the existing context-exhaustion path above ("save
  state, instruct user to resume").

## Hook cooperation (autonomous)

Under `AUTONOMOUS`, the orchestrator COOPERATES with the existing
context-utilization hooks — it never disables, edits, or lowers any of
their thresholds. This section documents the cooperation mechanism only;
the threshold values themselves live in `hooks/_lib.sh`'s
`read_constants()` and are read here, never redefined.

- **Self-checkpoint before the advisory band.** At `COMPACT_FIRST_BPS`
  (90% utilization) — BEFORE the 70–95% advisory band and well before the
  95% high-context band — the orchestrator self-invokes a checkpoint save
  UNLESS `run_state_probe` (`hooks/_lib.sh`) confirms no active run-state
  record exists project-wide. The check:
  ```sh
  . "__QUOIN_HOME__/hooks/_lib.sh" 2>/dev/null || true
  if ! command -v run_state_probe >/dev/null 2>&1; then
    echo GUARD_UNAVAILABLE
  else
    _rs_root=$(resolve_project_root "$(pwd)")
    if [ -z "$_rs_root" ] || [ ! -d "$_rs_root/.workflow_artifacts/memory" ] || [ ! -r "$_rs_root/.workflow_artifacts/memory" ]; then
      echo GUARD_UNAVAILABLE
    elif run_state_probe "$_rs_root/.workflow_artifacts/memory"; then
      echo PROBE_ACTIVE
    else
      echo PROBE_INACTIVE
    fi
  fi
  ```
  Called project-scoped (no `SESSION_ID` argument, matching `D-27`'s
  project-wide question for every consumer). `PROBE_INACTIVE` is the ONLY
  outcome that skips the save. `GUARD_UNAVAILABLE` — the guard is absent
  (the helper did not source, or the resolved root is empty, or its
  `.workflow_artifacts/memory` directory is missing or unreadable) — and
  `PROBE_ACTIVE` both mean
  the self-checkpoint fires exactly as it does today. This is the
  deliberate fail-safe direction (`D-28`): an undefined, unreachable, or
  unresolvable probe must never silently kill the 90% self-checkpoint,
  because in autonomous mode that save is what carries state across a
  context window — the opposite failure direction from `/checkpoint` Step
  1.4, where "no information" safely means "fall back to a real save"
  rather than "skip." When the save runs it writes a
  `checkpoint-defer-{sid}` marker as before, so the mid-phase advisory in
  the 70–95% band does not re-prompt for a checkpoint the orchestrator
  already took; when the save is skipped, no `checkpoint-defer-{sid}`
  marker is written — writing one after a skip would wrongly suppress the
  70–95% advisory. This bullet does not distinguish a boundary-parked run
  from an in-flight one (`D-26`): `/run` writes the record only at phase
  boundaries and never mid-phase, so the probe will almost always answer
  `PROBE_ACTIVE` and the self-checkpoint fires as it does today. There are
  two realistic skip windows, not one: the narrow one where `--clear` has
  already run but the session continues past 90%, and a second one where
  the record is genuinely still active but has aged past `run_state_probe`'s
  own staleness window — a phase that runs long, or a session paused and
  resumed after that window elapses, reads as `PROBE_INACTIVE` even though
  the run is live. The probe's default window is deliberately wider than
  `run_state_select`'s (14 days vs. 1) specifically to keep this second
  window rare in practice; `QUOIN_RUN_STATE_STALE_DAYS` raises it further
  for an expected multi-week autonomous run. This bullet lives inside `## Hook
  cooperation (autonomous)`, so the conditioning above does not reach an
  interactive (non-autonomous) `/run` session — a scope narrowing worth
  flagging for the `S-7` sweep, not an oversight. The pre-phase budget
  block's item (4) (`:321-326`) is unchanged: it reuses this contract, and
  `boundary_checkpoint.py` (deterministic, no model call) still always
  runs on the over-budget path.
- **There is no block to catch — the band is a pure advisory.** At and
  above `BLOCK_BPS` (95%) the prompt hook returns `additionalContext`
  plus a `systemMessage` and the prompt continues in the same turn; it
  never returns a `"decision": "block"` response on any path. The prompt
  is still appended to `pending-prompt-{sid}.txt`, so the on-disk audit trail is
  unchanged; `/checkpoint --restore` CASE B's *presentation* of that trail was
  corrected (review-1.md issue 2) to say the entries already ran rather than
  calling them blocked. The orchestrator therefore
  has no stop signal to key on here: it reads the advisory as context,
  finishes the stage it is in, and lets platform compaction be the
  backstop. Any block-catch logic keyed on the `"decision"` / `"block"`
  tokens is dead and must not be reintroduced.
- **Hooks fail open; the orchestrator never assumes otherwise.** The
  compaction hook never blocks by design — it only ever allows, and no
  hook in the set emits a stop signal on any path. The orchestrator
  relies on automatic compaction plus a supervisor's cross-phase relaunch
  unconditionally, never on a block signal it might be handed.
- **Hard constraint.** Autonomous mode NEVER writes to any file under
  `hooks/`, and NEVER modifies or lowers a `QUOIN_*_BPS` constant or any
  other hook threshold — anywhere, under any condition. The cooperation
  described above is entirely additive branches in this document; it adds
  no new hook script. Hook scripts change only through ordinary quoin
  development, never from this mode.

## Gate boundaries reference

**Post-architect (Phase 2 boundary):** subagent dispatch (not modified by Stage 3). **Post-implement (Phase 4 boundary primary; recursive recovery paths in the same phase):** all inline — preserve the parent's prompt cache. **Post-review (Phase 5 boundary):** inline. **Post-plan (handled by `/thorough_plan/SKILL.md`):** subagent dispatch. **Post-specify (Phase 1.5 boundary):** subagent dispatch (mirrors post-architect). **There is no `/gate` invocation after `/discover`** (discover feeds directly into specify/architect). Audit-log persistence (`gate-{phase}-{date}.md`) is mandatory at every boundary regardless of mode per `/gate/SKILL.md`. **On the fast route, the post-architect and post-plan gate boundaries do not exist**, because the architect and thorough_plan phases they would follow never run; the post-implement and post-review boundaries are unchanged.

## Important behaviors

- **Orchestrate, don't perform.** Never write plan content, code, or review findings yourself. Always spawn the appropriate subagent skill. **Named exception:** the Phase 1.6 fast-route routing stub (`<task_dir>/current-plan.md`), which the orchestrator writes itself, inline. The stub's provenance markers exist precisely so it is never mistaken for planned content — writing it is a mechanical transcription of acceptance criteria the user already approved upstream, not authored plan content; no design judgment is exercised, which is what this rule exists to prevent.
- **Checkpoints are mandatory.** Even when the user said "run everything" at the start — every phase boundary requires a conscious confirmation.
- **Preserve artifacts on stop.** All work produced before a stop stays in `.workflow_artifacts/<task-name>/`. The user can resume with individual skills or `/run --resume`.
- **Gates are blocking.** Never skip a gate. If a gate fails, do not proceed.
- **Fresh session for review.** `/review` must be a fresh subagent session for unbiased assessment.
- **Keep checkpoint summaries concise.** Key facts only — offer "show <artifact>" for details.
