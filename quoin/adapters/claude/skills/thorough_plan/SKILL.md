---
name: thorough_plan
description: "Triages tasks by size (Small/Medium/Large) and orchestrates the matching planning path. Use for: /thorough_plan, 'plan this', 'plan this thoroughly', 'detailed plan with review', 'plan and critique', 'full planning cycle'. Always the entry point for planned work."
model: opus
---

# Thorough Plan — Orchestrator


*Portable intent doc: `quoin/core/skills/thorough_plan.md`*

This skill orchestrates the planning convergence loop by invoking sub-skills — `/plan`, `/critic`, and `/revise` (or `/revise-fast`) — based on mode and round. See "Model selection per round" for details. It does not do the planning, critiquing, or revising itself — it coordinates the agents that do.

## Session bootstrap

On start:
1. Read `.workflow_artifacts/memory/lessons-learned.md` for past insights
2. Append your session to the cost ledger: `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `thorough-plan` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`. If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

<!-- quoin:ledger-self-write -->

## Setup

### 0. Parse autonomous sentinel

Before anything else, check whether the invocation prompt carries the `[autonomous]` sentinel
(prefixed by `/run` per its "Autonomous propagation" rule when this skill is spawned from an
autonomous `/run`, or passed directly on a standalone invocation). Parse and strip it at
bootstrap into an internal state flag `_AUTONOMOUS`:

- If present: strip `[autonomous]` from the prompt text — leading sentinels stack (e.g.
  `[no-redispatch] [autonomous] <task description>`); strip each independently, order does not
  matter — and set `_AUTONOMOUS=true` for the remainder of this session.
- If absent: `_AUTONOMOUS=false`. Every `[autonomous]` branch documented below is inert when
  `_AUTONOMOUS=false` — a standalone `/thorough_plan` invocation without the sentinel keeps the
  existing interactive behavior unchanged.

### 1. Determine the task subfolder and stage

Before starting the loop, establish the working directory:

- Ask the user for a descriptive task name if not obvious. Use kebab-case
  (`auth-refactor`, `payment-migration`, `api-v2-endpoints`).
- Detect whether the user's invocation includes a stage qualifier:
  - Explicit form: `stage <N> of <task>` (e.g., `stage 3 of quoin-foundation`)
    → set `<stage>` = N (integer).
  - Named form: `stage <name> of <task>` (e.g., `stage model-dispatch of quoin`)
    → set `<stage>` = name (string); the resolver looks up N from
    `<task-name>/architecture.md`'s `## Stage decomposition` section.
  - No qualifier → `<stage>` = None (legacy / single-stage layout).
- Compute the working directory by running:
    `python3 __QUOIN_HOME__/scripts/path_resolve.py --task <task-name> [--stage <N-or-name>]`
  This returns an absolute path. Create the folder if it doesn't exist:
    `mkdir -p "<task_dir>"`
- The architecture and cost-ledger always live at the task root, regardless of
  stage: `.workflow_artifacts/<task-name>/architecture.md` and
  `.workflow_artifacts/<task-name>/cost-ledger.md`.
- Pass `<task_dir>` (NOT the bare task name) to `/plan`, `/critic`, `/revise`,
  `/revise-fast` so they all write into the same resolved subfolder.

Error handling: if `path_resolve.py` exits with code 2 (any rule-1/2/2a/2b/2c/2d
ValueError), interpret the stderr message as "user-recoverable input ambiguity" and:
  (a) display the stderr message verbatim to the user;
  (b) fall back to the task root (rule-3 path: `<project_root>/.workflow_artifacts/<task-name>/`);
  (c) ask the user to disambiguate by re-invoking with the integer form `stage <N> of <task>`.
Do NOT abort the orchestration on exit-code-2.

#### 1b. Startup resume-detection (PRIMARY kill-recovery path — IVG-98)

After resolving `{task_dir}` and `mkdir -p`, scan for a phase-boundary progress checkpoint
that matches THIS task before dispatching any planning work. This is the PRIMARY recovery
path after an external kill — it reads `checkpoints/` DIRECTLY and is fully immune to the
restore picker's B3 Clause-B gate (see `## Phase-boundary checkpoints` for rationale).

**Scan procedure:**

```bash
_MEM="{project-root}/.workflow_artifacts/memory"
_CKPT_GLOB="$_MEM/checkpoints/thorough-plan-progress-*.md"
```

For each file matching the glob (if any):
1. Check `## Active task` header equals THIS task name. Skip if not.
2. Check that `current-plan.md` exists in `task_dir` (proves round-1 plan completed). Skip if not.
3. **Age bound:** checkpoint file mtime must be within the last 7 days OR newer than the
   current `current-plan.md` mtime (whichever bound is more permissive). Silently skip stale checkpoints.
4. If multiple qualifying files exist, pick the one with the most recent mtime (tiebreak: freshest wins).

**If a qualifying checkpoint is found:**

**Same-session check (insert BEFORE presenting the AskUserQuestion options):**

Extract the SID from the qualifying checkpoint's filename:
```sh
_ckpt_sid=$(basename "$ckpt_file" .md | sed 's/^thorough-plan-progress-//')
```

Compare to `_TPCKPT_SID` (acquired at startup via `get_session_uuid.py`):
```sh
if [ -n "$_ckpt_sid" ] && [ "$_ckpt_sid" != "unknown" ] \
   && [ -n "$_TPCKPT_SID" ] && [ "$_TPCKPT_SID" != "unknown" ] \
   && [ "$_ckpt_sid" = "$_TPCKPT_SID" ]; then
  _TP_SAME_SESSION=true
else
  _TP_SAME_SESSION=false
fi
```

**If `_TP_SAME_SESSION=true`:** the AskUserQuestion gains a third option and a warning
in its body:

- **Header:** `Resume planning checkpoint?`
- **Warning line (add above options when same-session):**
  `⚠ Same-session detected: this task was in progress in your CURRENT session.
   Resuming here adds more subagent dispatches to an already-used context window.`
- Use `AskUserQuestion` to offer:
  - **(a) Resume here** — re-hydrate and continue (same-session, not recommended)
  - **(b) Start fresh** — restart round 1, ignore checkpoint (non-destructive)
  - **(c) Resume in a new session** — print instructions and STOP:
    ```
    To resume in a new session:
      • Close this window, then run: claude
      • In the new session, run: /thorough_plan <task-name>
      • The §1b scan will find the same checkpoint there (it is not deleted).
    ```
    STOP — do NOT dispatch any planning work.

**If `_TP_SAME_SESSION=false` (normal case):** use `AskUserQuestion` to offer:
- **(a) Resume** — read the `## Current stage` token (`thorough-plan:round-{N}-{phase}`), parse N and
  phase, re-hydrate by reading the existing `current-plan.md` and any `critic-response-*.md` on disk,
  set the round counter, and re-dispatch the phase that was in flight (the phase AFTER the last
  completed boundary). Surface-first: show the user the current stage and the next phase before
  re-dispatching.
- **(b) Start fresh** — ignore the progress checkpoint and run round 1. Non-destructive: the checkpoint
  file is NOT deleted on "start fresh".

**Fail-OPEN:** if `_ckpt_sid` extraction fails (empty result) or `_TPCKPT_SID == "unknown"`,
treat as `_TP_SAME_SESSION=false` and present the standard 2-option AskUserQuestion.

**Under `[autonomous]`:** skip the `AskUserQuestion` entirely in both variants — auto-select
option **(a) Resume** (same-session variant: **(a) Resume here**) and proceed exactly as
documented for that option: parse the `## Current stage` token, re-hydrate from the existing
`current-plan.md` and any `critic-response-*.md` on disk, set the round counter, and re-dispatch
the phase that was in flight. NEVER auto-select **(c) Resume in a new session** — that option
prints STOP instructions and halts, which would stall an unattended autonomous run.

**If NO qualifying checkpoint exists:** continue silently with round 1 (no AskUserQuestion).

**Convergence cleanup (on PASS or Small single-pass convergence):** after writing the converged plan,
trash-move (or delete) `$_MEM/checkpoints/thorough-plan-progress-$_TPCKPT_SID.md` and
`$_MEM/pending-restore-$_TPCKPT_SID.txt` so a completed run is NOT re-offered as resumable on the
next invocation. Use the SID cached at startup (`$_TPCKPT_SID`). If cleanup fails, log a one-line
warning and continue — fail-OPEN.

### 2. Gather initial context

Collect and pass to `/plan`:

- The user's description of what needs to be built
- Path to `architecture.md` if `/architect` was run first
- Paths to all relevant repositories in the project folder
- Any constraints, preferences, or context the user mentioned

### 3. Parse runtime overrides and determine task profile

Before starting the loop, scan the user's task description for runtime overrides. Parse in this order:

1. **`strict:`** (case-insensitive): If the task description begins with the literal token `strict:`, enable strict mode for this run. Strip the `strict:` token from the description. Strict mode is equivalent to the Large task profile (all-Opus model selection, `max_rounds` defaults to 5). The user can still override `max_rounds` via the `max_rounds: N` token even in strict mode. If `strict:` is present, set task profile to **Large** and skip step 1b.

1b. **Task profile tag** (`small:`, `medium:`, `large:`, case-insensitive): If the task description begins with one of these tokens, set the task profile accordingly and strip the token. If `large:` is specified, it is equivalent to `strict:` (all-Opus, max 5). If no profile tag is found and `strict:` was not found, proceed to step 1c.

1c. **Auto-classification** (only if no explicit tag from steps 1 or 1b): Based on the task description and any available context (architecture docs, referenced files), classify the task as Small, Medium, or Large using the triage criteria (see "Task triage criteria" section below). Present the classification to the user with a brief rationale and ask for confirmation. If the user disagrees, use their choice. If the user does not respond or says "ok" / "yes" / "go", use the auto-classification. **When in doubt, default to Medium** — it is the safe middle ground. **Under `[autonomous]`:** skip presenting the classification for confirmation — accept the auto-classified profile immediately and proceed without waiting for a response.

2. **`max_rounds: N`** (case-insensitive, N = positive integer): If found, use N as the maximum round cap for this run. Strip the `max_rounds: N` token from the description before passing it to the planner skill. If not found, use the default cap (4 for Medium, 5 for Large/strict). If the value is not a positive integer (e.g., zero, negative, or non-numeric), ignore the override and use the default.

3. **Apply task profile defaults.** Based on the determined profile, set defaults that were not already overridden:

   | Profile | Model mode | Default max_rounds | Critic loop | Gate level |
   |---------|-----------|-------------------|-------------|------------|
   | Small | N/A (single pass) | N/A | Skip | Smoke (plan) + Standard (post-implement) + Full (pre-merge) |
   | Medium | Normal (Opus /plan, Sonnet /revise-fast, Opus /critic) | 4 | Full | Smoke (plan) + Standard (post-implement) + Full (pre-merge) |
   | Large | Strict (all Opus) | 5 | Full | Smoke (plan) + Full (post-implement) + Full (pre-merge) |

   If `max_rounds: N` was explicitly provided, it overrides the profile's default.

   **Important:** For Small-profile tasks, `max_rounds` is ignored — there is no critic loop and therefore no round cap applies. If `max_rounds: N` was parsed in step 2, discard it when the profile is Small.

   **Note on auto-classification latency:** Auto-classification (step 1c) adds one user confirmation round-trip before planning begins. Users who want to skip this delay can use explicit tags (`small:`, `medium:`, `large:`).

Examples:
- `/thorough_plan fix the null check in auth.ts` — auto-classifies (likely Small), asks for confirmation
- `/thorough_plan small: fix the null check in auth.ts` — Small profile, single-pass plan, no critic loop
- `/thorough_plan medium: add retry logic to the payment client` — Medium profile, standard critic loop (max 4)
- `/thorough_plan large: redesign the auth token refresh flow` — Large profile (= strict mode), all-Opus, max 5
- `/thorough_plan max_rounds: 6 this migration is gnarly` — auto-classified, cap overridden to 6
- `/thorough_plan strict: handle the auth migration carefully` — Large profile (strict mode), cap = 5
- `/thorough_plan strict: max_rounds: 3 quick but safe` — Large profile, cap = 3
- `/thorough_plan small: max_rounds: 2 add the config endpoint` — Small profile; max_rounds parsed then discarded (no loop)

### 3b. Task triage criteria

Use these criteria when auto-classifying a task (step 1c above) or when verifying a user's explicit tag makes sense:

**Small** — Single-concern, localized changes with no integration risk:
- Touches 1-3 closely related files in a single module
- No integration points affected (no API contract changes, no cross-service calls)
- Well-understood pattern: bug fix, config change, add simple endpoint, rename, typo fix
- Failure is localized — affects one feature, easy to detect and revert
- No data model changes, no auth changes, no shared-state modifications

**Medium** — Multi-file changes with moderate complexity or some integration risk:
- Touches multiple files across 1-2 modules or services
- May affect integration points but contracts remain backward-compatible
- Some unknowns but similar work has been done in this codebase before
- Failure affects a subsystem but is contained and recoverable
- Includes adding a new feature with tests, refactoring a module, adding retry/resilience logic

**Large** — Cross-cutting, high-risk, or architecturally significant changes:
- Touches multiple services, repos, or architectural layers
- Affects data consistency, authentication, authorization, or multi-service contracts
- Significant unknowns, new patterns, or involves migration of existing data/systems
- Failure could affect multiple services or all users
- Includes database migrations, auth overhauls, API versioning, payment flow changes

**When the classification is ambiguous, choose the more cautious (larger) profile.** A Medium task that runs the full critic loop costs a few extra dollars; a Large task misclassified as Small can ship bugs.

### 3b-1. Enrich pre-step (default-on prompt, non-blocking)

Before the spec pre-flight below, offer via `AskUserQuestion` to run `/enrich` on the raw task description first (sharpens the prompt upstream of `/specify` — fills genuine gaps, writes enriched-prompt.md, never writes a spec/plan itself):

- Question: "Run prompt enrichment before planning?"
- Header: "Enrich pre-step"
- multiSelect: false
- Option 1: label: "Run /enrich first" — description: "Sharpen the raw task prompt before continuing (fills genuine gaps against real codebase context)."
- Option 2: label: "Skip enrichment" — description: "Proceed directly to the spec pre-flight / planning loop. Enrichment can always be run later; its absence is a normal, non-blocking outcome."

This is default-on but NON-BLOCKING — the loop proceeds regardless of the answer. If run: unless `QUOIN_INLINE_COST_CAPTURE=0`, the cost row is the on-behalf write described in "Invoking each agent" → "On-behalf cost capture" below (phase=enrich, model=opus) FIRST; either way, THEN append a best-effort cost-ledger row for the `enrich` phase if no row is found: `unknown-enrich-<timestamp> | <date> | enrich | opus | task | /thorough_plan subagent (on-behalf write failed, if capture was ON — else no UUID recorded)` — this fallback now runs regardless of flag state (mirrors the `run/SKILL.md` T-02 CRIT-2 fix), not only under opt-out. No `/gate` runs after enrich.

**Under `[autonomous]`:** skip the `AskUserQuestion` — do not wait for a choice. When `/thorough_plan` was itself spawned with `[autonomous]` from `/run`, enrichment has typically already run at `/run` Phase 1.4 — skip re-running it here. When `/thorough_plan` is invoked standalone under `[autonomous]` (no upstream enrich), run `/enrich` best-effort without blocking; if it is unavailable, errors, or times out, proceed directly to the spec pre-flight below without waiting. Either way this stays non-blocking, exactly as the non-autonomous default already is.

### 3c. Spec pre-flight (advisory, non-blocking)

If NO `<task-root>/spec.md` exists AND the task profile (determined in step 3/3b above) is Medium or Large (Small tasks skip this offer entirely, per the resolved Small-task-skip policy), offer via `AskUserQuestion` to run `/specify` first:

- Question: "Produce a task spec before planning?"
- Header: "Spec pre-flight"
- multiSelect: false
- Option 1: label: "Run /specify first" — description: "Elicit intent and write a task feature spec (spec.md) before the planning loop starts."
- Option 2: label: "Proceed without a spec" — description: "Continue planning now. A spec can always be added later; its absence is a normal, non-blocking outcome (grandfather)."

This is ADVISORY / NON-BLOCKING — the loop proceeds regardless of the answer; grandfather preserved. Small tasks skip the offer entirely.

**Under `[autonomous]`:** skip the `AskUserQuestion`. If `<task-root>/spec.md` already exists, proceed straight to planning. If it does NOT exist, run `/specify` non-interactively (spawn with `[autonomous]` — see `/specify`'s own non-interactive degrade branch, which synthesizes a confidence-scored spec instead of eliciting interactively) rather than waiting on a choice; treat the resulting spec (including its `confidence` field) as planning input. Either way this never blocks — under `[autonomous]` it simply never waits for user input.

### 4. Model selection per round

The orchestrator selects which skill variant to spawn based on the round number and whether strict mode is active:

| Round | Role | Normal mode (default) | Strict mode |
|-------|------|-----------------------|-------------|
| 1 | Planner | `/plan` (Opus) | `/plan` (Opus) |
| 1 | Critic | `/critic` (Opus) | `/critic` (Opus) |
| 2+ | Reviser | `/revise-fast` (Sonnet) | `/revise` (Opus) |
| 2+ | Critic | `/critic` (Opus) | `/critic` (Opus) |

**Key rules:**
- `/plan` (round 1) is ALWAYS Opus, in every mode. The initial plan sets the structural foundation; a strong first plan reduces iteration.
- `/critic` is ALWAYS Opus, every round, in every mode. Never tiered.
- In normal mode, `/revise` rounds (2+) use Sonnet via `/revise-fast`. In strict mode, they use Opus `/revise`.
- The `-fast` variant (`/revise-fast`) is content-identical to its Opus counterpart — same instructions, same output format, different model.

## Small-profile routing (no loop)

If the task profile is Small, do NOT enter the critic loop. Instead:

1. Invoke `/plan` (Opus) — same as round 1 of the normal loop. Output: `current-plan.md`.
2. Run a smoke gate (plan artifact exists, has tasks with file paths and acceptance criteria).
3. Add the convergence summary in `current-plan.md` (below the `## For human` block) with `Task profile: Small`, `Rounds: 1`, and `Key revisions: N/A — single-pass plan`.
4. Print an **inline summary** in the chat (do NOT rely on the user reading `current-plan.md`): (a) "Task classified as Small — produced a single-pass plan in 1 round." (b) 3–5 bullets of what the plan covers in plain language. (c) Remaining concerns or decisions ("none" if clean). (d) Plan path: `<task_dir>/current-plan.md` — note that the plan body is terse and can be `/expand`-ed for detail. (where `<task_dir>` was resolved in Setup §1 via `path_resolve.py`)
5. **STOP.** Do not invoke `/implement`. Wait for the user.

## Medium and Large profiles (critic loop)

```
Round 1:
  /plan    → produces current-plan.md
  /critic  → (FRESH SESSION) reads plan + code → produces critic-response-1.md

  If verdict = PASS → done
  If verdict = REVISE → continue

Round 2:
  /revise  → reads critic-response-1.md → updates current-plan.md
  /critic  → (FRESH SESSION) reads updated plan + code → produces critic-response-2.md

  If verdict = PASS → done
  If verdict = REVISE → continue

...repeat up to Round 4 (or max_rounds if overridden)
```

> **Note:** The diagram above shows `/plan` and `/revise` generically. The actual skill variant spawned each round depends on mode (normal vs. strict) — see the "Model selection per round" table.

### Invoking each agent

**Autonomous re-prefix (`[autonomous]` transitive propagation):** When this orchestrator itself
is running with `_AUTONOMOUS=true` (parsed at bootstrap per Setup "0. Parse autonomous sentinel"
above), prefix `[autonomous]` onto EVERY spawn prompt below — the `/plan`, `/critic`, `/revise`,
and `/revise-fast` spawns, and the post-plan `/gate` subagent spawn — so the sentinel reaches
those leaf skills' own `[autonomous]` / §0' / §0″ branches. Sentinels stack with any existing
prefix (e.g. `[no-redispatch] [autonomous] <spawn instructions>`); each leaf skill parses and
strips its own copy independently at its own bootstrap. This is the deeper-spawn re-prefix rule
referenced by `/run`'s "Transitive propagation rule" (`D-07`).

**On-behalf cost capture (`QUOIN_INLINE_COST_CAPTURE`, default-ON, opt-out via =0, D-1/D-2/D-3, IVG-111 stage 3):**
Unless `QUOIN_INLINE_COST_CAPTURE=0`, this applies to EVERY managed spawn below — `/plan` (round 1),
`/critic` (every round), `/revise`|`/revise-fast` (rounds 2+), and `/enrich` (3b-1): (1) prepend
`[quoin-onbehalf]` to the spawn prompt (stacks with `[autonomous]`/`[no-redispatch]` per the
re-prefix rule above — order-independent, each leaf strips its own copy) so the child SKIPS its
own session-start cost-ledger self-write (T-06 predicate); (2) bind `AID`/`TUID` per
`proc:agentid-capture` — `AID` is the `agentId` field from the Agent tool's return for that spawn,
transcribed literally by this orchestrator (model-in-the-loop, not a shell capture); `TUID` is this
spawn's own Agent tool_use id; fallback (agentId absent/empty, R-12): `AID = TUID` if present, else
`"<parent-session-uuid>-<phase>-<utc-ts>"`, forcing `ATTR="src=unresolved"` (MIN-2: discard any
sidecar hit); (3) AFTER the subagent returns and its artifact is verified on disk, run
`proc:onbehalf-write` with that phase's model (plan=opus, critic=opus, revise=sonnet|opus per the
Model-selection table, enrich=opus) and `uuid=<AID>` — this REPLACES the suppressed child
self-write (D-2, unchanged); net: at least one row per managed phase always (never zero — this is
what CRIT-2 fixes), and normally exactly one attributable row (the on-behalf write) plus, only on
write failure, a second best-effort-labeled row (the embedded F-02 post-check below):
```
SID="$CLAUDE_CODE_SESSION_ID"
_ERR=$(mktemp) || { printf 'cost-attr WARN: %s\n' "mktemp failed"; _ERR=/dev/null; }
ATTR="$(python3 __QUOIN_HOME__/scripts/agent_transcript_cost.py \
          --sid "$SID" --agent-id "$AID" --tool-use-id "$TUID" 2>"$_ERR")"
[ -z "$ATTR" ] && ATTR="src=unresolved"   # MIN-1: key on empty stdout, not exit code
[ -s "$_ERR" ] && printf 'cost-attr WARN: %s\n' "$(head -c 500 "$_ERR" | tr '\011\012\015' '   ' | tr -d '\000-\037\177')"
[ "$_ERR" != "/dev/null" ] && rm -f "$_ERR"
printf '%s | %s | %s | %s | task | %s | %s | %s\n' \
  "$AID" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" \
  "on-behalf: PHASE via /thorough_plan" "0" "$ATTR" >> "$LEDGER"
# F-02 post-check (identifier-keyed, same invocation): verify THIS write's own
# AID landed — immune to a stale same-phase row masking a lost row on re-run
# (this task's own ledger already carries repeated `critic`/`implement` rows).
# If the append above silently failed, append a labeled fallback row now. This
# closes F-01 for the plan/critic/revise spawns, which have no other fallback.
{ [ -n "$AID" ] && grep -qF -e "$AID | " -- "$LEDGER" 2>/dev/null; } || \
  printf '%s | %s | %s | %s | task | %s | %s\n' \
    "unknown-PHASE-$(date -u +%s)" "$(date -u +%Y-%m-%d)" "PHASE" "MODEL" \
    "/thorough_plan subagent (on-behalf write failed)" "0" >> "$LEDGER"
```
This FOLDS the enrich fallback row (3b-1, previously "if the subagent didn't record one") into this same mechanism — under default-ON, enrich's cost row is the on-behalf write rather than a guess; the enrich-only best-effort verify/append fallback (site 1 above; plan/critic/revise have no fallback of that phase-name-keyed shape, but now share the AID-keyed F-02 post-check embedded in the on-behalf write above) still runs afterward as a post-check, in both flag states, per the CRIT-2 fix.
**Opt-out (`QUOIN_INLINE_COST_CAPTURE=0`):** every spawn below reverts to the pre-`IVG-249` behavior — the child self-writes its own 6/7-col row; the enrich fallback (site 1 above) still applies exactly as it does under default-ON.

**`/plan` (Round 1 only)**
- Always spawn `/plan` (Opus) — the initial plan is always Opus-quality regardless of mode
- Pass all context: architecture docs, user requirements, repo paths
- Pass path to `<task-root>/spec.md` if it exists (read-if-exists).
- Output: `<task_dir>/current-plan.md` (where `<task_dir>` was resolved in Setup §1 via `path_resolve.py`)

**`/critic` (every round)**
- **MUST spawn as a new agent session** — fresh context is essential for unbiased critique
- Always Opus. Never tiered. This is non-negotiable.
- Pass: path to `current-plan.md`, path to the project folder (so it can read actual code)
- Output: `<task_dir>/critic-response-<round>.md`

**`/revise` or `/revise-fast` (rounds 2+)**
- **MUST spawn as a new agent session** (same mechanism used for /critic above) — fresh context prevents anchoring on prior orchestrator chatter
- Spawn `/revise-fast` (Sonnet) in normal mode, or `/revise` (Opus) in strict mode — see "Model selection per round" table above.
- Pass: path to `<task_dir>/current-plan.md`, path to latest `<task_dir>/critic-response-<N>.md`, and paths to any files the critic flagged as needing re-examination
- Output: updated `current-plan.md` (in place)

### Convergence rules

The loop stops when ANY of these is true:

1. **Critic gives PASS** — no CRITICAL or MAJOR issues. Plan is ready.
2. **Max rounds reached (default: 4)** — inform the user of remaining issues. The plan may have inherent constraints.
3. **Stuck in a loop** — if round N's critic has the same dominant `surface_family` class among structural CRIT/MAJ issues as round N-1 (same-class recurrence), escalate to the user with the repeated class, the specific issues, and three options: (a) continue revising, (b) add a structural canary task to the plan, (c) accept the plan as-is and proceed to implement. Do NOT auto-continue. **Under `[autonomous]`:** skip escalating to the user — auto-select **(a) continue revising**, up to `max_rounds` (never silently accept an unresolved same-class recurrence, and never auto-add a canary task without review). If `max_rounds` is reached while the same-class recurrence is still unresolved, fall through to rule 2 (max-rounds-reached) handling above.

### After each critic round — classify and decide

After reading each critic response, run:

```
python3 __QUOIN_HOME__/scripts/classify_critic_issues.py \
  --critic-response <task_dir>/critic-response-<N>.md
```

This emits a verdict (`CONTINUE-LOOP` or `BAIL-TO-IMPLEMENT`) on line 1, then a JSON summary with fields `structural_count`, `mechanical_count`, and `issues[]` (each with `id`, `severity`, `title`, `surface_family`).

Note: `--enable-bailout` is currently disabled (default=False) and should NOT be passed until the training corpus achieves ≥95% classifier agreement on the held-out regression corpus. Enable it post-merge once `test_training_corpus_accuracy` passes at ≥95%. See `pipeline-efficiency-improvements/architecture.md` Stage 1 acceptance criteria.

**BAIL-TO-IMPLEMENT verdict handling:** If the classifier returns `BAIL-TO-IMPLEMENT` (only possible when `--enable-bailout` is explicitly passed), stop the critic loop immediately and route directly to `/implement` without further revision rounds. BAIL-TO-IMPLEMENT is NOT emitted by the critic itself — it is synthesized by this orchestrator when all remaining CRITICAL and MAJOR issues are classified as mechanical and the canary precondition holds. When this verdict fires, inform the user that only mechanical issues remain and that implementation will address them directly, then proceed to the gate.

**Same-class detection:** If round N's `structural_count` > 0 AND round N-1's `structural_count` > 0 AND the dominant surface families match (both rounds share the same top-1 `surface_family` among structural CRIT/MAJ issues), escalate to the user as described in rule 3 above. Do NOT auto-continue. **Under `[autonomous]`:** apply the rule-3 autonomous branch above (auto-select "continue revising" up to `max_rounds`) instead of escalating. When `Class:` lines are absent in a critic response (legacy format without per-issue class labels), same-class detection falls back to same-title comparison — comparing the titles of structural CRIT/MAJ issues across rounds instead of their class labels to detect recurrence.

### Between rounds

After each critic round, before continuing:

- Read the critic response yourself (as orchestrator)
- Run the classifier (see `### After each critic round — classify and decide` above) to detect same-class recurrence
- Briefly inform the user: "Round N complete — critic found X critical, Y major issues. Proceeding to revise." or "Round N complete — critic passed. Plan is ready."

## Final output

When converged, add a convergence summary in `current-plan.md` (below the `## For human` block):

```markdown
## Convergence Summary
- **Task profile:** <Small | Medium | Large>
- **Rounds:** <N> (Small tasks: 1, single pass)
- **Final verdict:** PASS
- **Key revisions:** <what the main themes of revision were across rounds, or "N/A — single-pass plan" for Small>
- **Remaining concerns:** <any MINOR issues not addressed, or none>
```

For Small-profile tasks that took the single-pass path, the convergence summary still appears in `current-plan.md` (below the `## For human` block) but with `Rounds: 1` and `Key revisions: N/A — single-pass plan`. This signals to downstream skills that the plan was not critic-reviewed.

**Convergence cleanup (IVG-98):** After writing the converged plan, trash-move or delete the
phase-boundary progress checkpoint and its sentinel so this completed run is NOT re-offered as
resumable on the next `/thorough_plan` invocation:

```bash
_MEM="{project-root}/.workflow_artifacts/memory"
rm -f "$_MEM/checkpoints/thorough-plan-progress-$_TPCKPT_SID.md" || true
rm -f "$_MEM/pending-restore-$_TPCKPT_SID.txt" || true
```

If cleanup fails, log a one-line warning and continue — fail-OPEN. (`$_TPCKPT_SID` is the SID
cached at startup; see `## Phase-boundary checkpoints` §Startup acquisition.)

Then spawn `/gate` as a subagent session (post-plan boundary — subagent dispatch required because the parent has just exited the plan→critic loop and the post-plan checks operate against a different context shape than the loop. Audit-log persistence applies regardless of mode — see `/gate/SKILL.md`.). No ``[quoin-bundle]`` block is emitted at this gate: the gate reads only the artifact's `## For human` block, so the corrected expected-delta census (review round 1) shows a bundle here is net-negative — the consumer was descoped.

Present automated checks and a summary to the user.

**Final-message branch.** If the dispatch carried `return: envelope`, emit the return envelope as the final message instead of the inline summary below — the orchestrator authors its own checkpoint from the artifacts it re-reads. Full contract: __QUOIN_HOME__/core/workflow/handoff-format.md (the shape below is inlined so a compliant emission rarely needs it):
```text
[quoin-handoff/1.0 return]
status: COMPLETE
artifact: <task_dir>/current-plan.md
verdict: PASS
summary: <clamped, one line, <=600 B>
[/quoin-handoff]
```
(`verdict: REVISE` if max-rounds was hit without convergence.) `status` is one of `COMPLETE`/`PARTIAL`/`NEEDS-DECISION`/`BLOCKED`; `verdict` is one of `PASS`/`REVISE`/`APPROVED`/`CHANGES_REQUESTED`/`BLOCKED`. Envelope clamped to 1,024 B. Otherwise — standalone, no envelope — after the gate, print an **inline summary** in the chat as your final user-facing message, plain and human-readable (REQUIRED — do NOT rely on the user reading `current-plan.md`; the plan body is Tier-3 terse). Cover the canonical field set:
1. **What this step produced** — e.g., "Produced a converged Medium-profile plan in N round(s)."
2. **Main planned tasks** — 2–4 bullets in plain language (e.g., "Add retry logic to the payment client", "Write integration tests for the new endpoint") — no terse glyphs, no T-NN shorthand.
3. **How many rounds and main revision themes** — e.g., "1 round, PASS on first critic pass" or "2 rounds: main revision addressed missing error-handling in the queue consumer."
4. **Remaining concerns or decisions** — one line; "none" if clean.
5. **Artifact location** — `<task_dir>/current-plan.md` — note that the plan body is terse and can be `/expand`-ed for full detail.

**STOP HERE.** Do NOT invoke `/implement`. Do NOT offer to start implementing. The user must explicitly type `/implement` to proceed. This is a hard rule — implementation requires a conscious human decision.

## File structure at completion

```
<project-folder>/.workflow_artifacts/<task-name>/
├── architecture.md          (from /architect, if exists)
├── current-plan.md          (final converged plan)
├── critic-response-1.md     (round 1 critic)
├── critic-response-2.md     (round 2 critic, if needed)
├── ...
└── critic-response-N.md     (final round critic)
```

## Important behaviors

- **You are the orchestrator, not the planner.** Don't produce plan content yourself — invoke `/plan`, `/critic`, `/revise` (or `/revise-fast`).
- **Critic MUST be a fresh session.** This is non-negotiable. Same-agent critique is biased and weak.
- **Keep the user informed.** Brief status updates between rounds. Don't go silent for 10 minutes.
- **Detect loops early.** After each critic round, run `classify_critic_issues.py` (without `--enable-bailout`) and compare the dominant `surface_family` of structural issues against the previous round. If the same class recurs, escalate to the user — ask whether to continue revising, add a canary task, or accept the plan as-is.
- **Stream-idle timeout recovery (orchestrator-only).** If a spawned subagent
  (`/plan`, `/critic`, `/revise`, `/revise-fast`) returns a tool_result whose
  content contains `Stream idle timeout - partial response received`:
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
       and ask whether to proceed to /implement with a flagged
       plan, or stop.
  NOTE: This retry only fires for subagents dispatched BY /thorough_plan.
  Standalone /revise and /plan invocations have no automatic retry.
- **Pass context explicitly.** Each agent starts with limited knowledge. Give them the file paths and repo locations they need.

## Phase-boundary checkpoints (IVG-98)

`/thorough_plan` writes a durable progress checkpoint at every phase boundary so an externally
killed session can be resumed by re-invoking `/thorough_plan {task}` in a fresh session (T-04 startup
scan, PRIMARY path — see Setup §1b). `/checkpoint --restore` is a SECONDARY convenience path and
may degrade to task-level restore in the kill-during-subagent window (B3 Clause-B fires when a
subagent's `{date}-{task}.md` is newer than the checkpoint; T-04 direct scan is immune).

Verified: `## Current stage` is free-text for all readers (`/start_of_day`, `/end_of_day`,
`/status`, `/continue_work`, B3 awk); the colon-delimited token `thorough-plan:round-N-{phase}`
is safe and unambiguous.

### Startup acquisition (run ONCE at orchestrator startup)

Acquire and cache the session UUID and current branch before the planning loop. Both acquisitions
are fail-OPEN: a slow or failed call falls back to `unknown` and never stalls the loop.

```bash
_TPCKPT_SID="$(python3 __QUOIN_HOME__/scripts/get_session_uuid.py --phase thorough-plan 2>/dev/null || echo unknown)"
_TPCKPT_BRANCH="$(git -C {project-root} rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
```

### Checkpoint procedure (call at each phase boundary)

After each subagent returns and its artifact is verified on disk, run the helper as the LAST
action of that boundary. The helper always exits 0 (fail-OPEN) and is additionally wrapped
`|| true` as defence-in-depth — a checkpoint failure NEVER blocks the planning round.

```bash
python3 __QUOIN_HOME__/scripts/thorough_plan_checkpoint.py \
  --project-root {root} --task {task} --round {N} --phase {plan|critic|revise} \
  --sid "$_TPCKPT_SID" --branch "$_TPCKPT_BRANCH" \
  --plan-path {task_dir}/current-plan.md \
  [--critic-path {task_dir}/critic-response-{N}.md] \
  --session-state {sessions}/{date}-{task}-orchestrator.md || true
```

The `--session-state` path uses the ORCHESTRATOR-DEDICATED file (`{date}-{task}-orchestrator.md`).
Subagents write only to the standard `{date}-{task}.md` — these two files are fully disjoint (M-02/D-07).
The orchestrator file owns `## Current stage: thorough-plan:round-N-{phase}` and subagents never touch it.

**Pre-round context budget (IVG-141) — EXTENDS this IVG-98 boundary; does NOT
regress it.** AFTER the `thorough_plan_checkpoint.py` call above (which stays the
OWN, IVG-98 writer — do NOT call `boundary_checkpoint.py` here, per D-3), ALSO run
the on-demand budget guard, reusing the already-acquired startup SID
(`$_TPCKPT_SID`) as a best-effort leaf measurement (T-02 spike PASSED):
```bash
_cbg_out="$(python3 __QUOIN_HOME__/scripts/context_budget_guard.py --project-root {root} \
  --current-uuid "$_TPCKPT_SID" || true)"
```
Bypass on `[no-phase-budget]` (strip at bootstrap) or `QUOIN_DISABLE_PHASE_BUDGET=1`.
Branch on stdout content, never on exit code (the trailing `|| true` above
intentionally always yields exit 0, so exit code carries no signal here — this
also closes the gap where the guard's own crash, e.g. lessons-learned
2026-09-04, would otherwise be indistinguishable from a genuine `OVER|`
verdict). If `$_cbg_out` starts with `OVER|` (`OVER|util|path`), react
NON-BLOCKING and uniform in all modes (NO `AskUserQuestion`, NO decision-gate
marker): the IVG-98 checkpoint above is ALREADY durable, so no second
checkpoint is written — just emit the advisory
`[quoin-budget: util NN% ≥ threshold at thorough_plan boundary; checkpoint saved → /thorough_plan {task}]`,
then:
  - **default** → PROCEED with the next planning round. Never prompts, never blocks.
  - **`QUOIN_PHASE_BUDGET_BLOCK=1`** (opt-in, default off) → print a fresh-session
    resume instruction (`/thorough_plan {task}`) and STOP. A printed instruction,
    NOT an `AskUserQuestion`.
  - **`_AUTONOMOUS`** → the SAME non-blocking path, and ADDITIONALLY hand back per
    the existing autonomous behavior so a supervisor resumes in a fresh session.
If `$_cbg_out` does NOT start with `OVER|` (`OK|...`, `OK|disabled|`, `OK|0|`
fail-OPEN, empty output, or a crash traceback with no `OVER|` line) → proceed
with the next planning round; none of the above reactions fire.
No new `[no-interactive]` sentinel parsing is added for this
non-blocking check (it never prompts).

**Round-boundary run-state refresh (third call in this boundary block, AFTER the
`thorough_plan_checkpoint.py` invocation and the budget guard above).** `/thorough_plan`
is a first-class standalone entry point, not only a `/run` subagent, so this call carries
`--require-existing`: it refines a record `/run` already created and is a silent no-op
(no file, no directory, no notes append) when there is none — a standalone `/thorough_plan`
run therefore leaves no run-state trace of its own. The call passes NO `--session-id`:
the record keeps its creator's stored id (the writer preserves a non-empty stored id on
refresh regardless), so the session that created the record still matches the PreCompact
hook's exact-equality check while planning rounds run:
```bash
python3 __QUOIN_HOME__/scripts/run_state.py --write --require-existing \
  --project-root "{root}" --task "{task}" \
  --phase thorough_plan --phase-index 3 --subphase "round-{N}-{plan|critic|revise}" \
  --step "round {N} {phase} returned" --at-stage-boundary false \
  --next-action "start thorough_plan" --artifact "{task_dir}/current-plan.md" || true
```

**`## Current stage` ownership note:** The orchestrator file is authoritative for the round/phase token
but is NOT a reliable B3 Tier-4 fallback: in the kill-during-subagent window, B3 Tier-4 reads the
freshest `sessions/*.md` by mtime, which is the subagent's file. Recovery: re-invoke
`/thorough_plan {task}` — T-04 direct scan always works (B3-immune). The checkpoint file
`thorough-plan-progress-{sid}.md` is the AUTHORITATIVE resume anchor.

### Boundary triggers

Run the checkpoint procedure at each of these points:

1. **After `/plan` returns and `current-plan.md` is written** (round 1):
   `--round 1 --phase plan`
2. **After each `/critic` returns and `critic-response-{N}.md` is written** (every round):
   `--round N --phase critic --critic-path {task_dir}/critic-response-{N}.md`
3. **After each `/revise`/`/revise-fast` returns and `current-plan.md` is updated** (rounds 2+):
   `--round N --phase revise`
4. **Small-profile routing** (single `/plan` pass): after the plan write, same as trigger 1:
   `--round 1 --phase plan`

### Session-state ownership

- **Orchestrator writes:** `{date}-{task}-orchestrator.md` (fully owned; updated at each boundary via helper)
- **Subagents write:** `{date}-{task}.md` (per-phase tracking only; orchestrator never reads this for resume)
- **PRIMARY resume anchor:** `thorough-plan-progress-{sid}.md` checkpoint file (T-04 direct scan)
- **SECONDARY convenience:** `/checkpoint --restore` — best-effort; B3 Clause-B limitation documented above

The sentinel `pending-restore-{sid}.txt` is still written at each boundary and enables Tier-3
picker enumeration. However, it CANNOT override B3 Clause-B when a subagent file is newer. In that
window, T-04 direct scan (not the picker) is the reliable resume path.

The checkpoint file includes `## Last user intent` with the next-phase re-entry string (e.g.,
`"thorough_plan {task}: last completed boundary thorough-plan:round-1-plan; next phase to run: critic.
Re-invoke /thorough_plan {task} and resume at critic."`). This is the field the picker's Step 2
surface renders as re-entry guidance and must always be present and non-empty (C-01).
