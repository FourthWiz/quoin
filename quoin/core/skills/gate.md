# gate

Runtime-neutral intent for the gate skill. Any runtime adapter (Claude,
Codex, …) that implements this skill should match the contract described here.

## Purpose

Act as a quality checkpoint between workflow phases: run automated checks
against the output of the completed phase, present a clear go/no-go summary
to the user, and STOP until the user explicitly approves proceeding to the
next phase. Nothing advances without human confirmation.

## When to use

- Between every major workflow phase transition (after architecture, after
  planning, after implementation, after review).
- When automated checks need to be run and presented to a human before
  proceeding.
- When a phase produces artifacts that must be validated before the next
  phase begins.
- When the workflow needs a hard STOP for human review.

## Inputs

- The completed phase's artifacts under `.workflow_artifacts/<task-name>/`
  (architecture.md, current-plan.md, review files, implementation diff).
- Git state: uncommitted changes, branch hygiene, recent commits.
- Session state file under `.workflow_artifacts/memory/sessions/` (advisory;
  absence is a non-fatal skip).
- Optional `architecture.md` at `<task-root>/architecture.md` — for display
  in the gate summary; missing file is a non-fatal skip.

All reads MUST tolerate missing files. The skill MUST apply the
stage-aware path resolver when locating phase artifacts.

## Output

A rendered checkpoint summary (human-facing, always in plain prose) and a
persistent audit-log artifact at
`.workflow_artifacts/<task-name>/gate-{phase}-{date}.md` (Class A per the
artifact-format contract). The audit log is written only after the user
explicitly approves the gate.

The audit-log section set:

- `## Automated checks` — REQUIRED — terse numbered list with pass/fail
  status per check.
- `## Verdict` — REQUIRED — single word: PASS or FAIL.
- `## Failures requiring attention` — OPTIONAL — terse numbered list of
  blocking failures with remediation.
- `## Warnings (non-blocking)` — OPTIONAL — terse numbered list.
- `## Summary of what was produced` — OPTIONAL — caveman prose, 2-3 sentences.
- `## What's next` — OPTIONAL — caveman prose, 1-2 lines.

The skill also updates the session-state file under
`.workflow_artifacts/memory/sessions/`.

## Behavior contract

- The workflow MUST NOT auto-advance through this skill. Every phase
  transition requires explicit user approval, UNLESS the benchmark
  auto-approve guard fires (see below).
- Automated checks MUST run before presenting the summary.
- The audit log MUST be written after user approval and MUST NOT be
  written if the user rejects the gate.
- The skill MUST NOT invoke the next workflow phase — it stops and waits.
- A non-interactive session MUST wait on long test runs in the foreground rather than ending its turn with work pending.
- Cost-ledger writes are conditional: record only when a task context is
  determinable from the surrounding artifacts.
- The skill MUST tolerate missing optional inputs (architecture.md, spec.md,
  session state, lessons-learned) without aborting.
- When automated checks fail, the skill MUST present failures clearly with
  suggested remediation, then wait for the user to fix them or acknowledge
  them before re-running.

## Benchmark auto-approve guard

When the benchmark harness runs a full automated suite, requiring human input
at each gate would block unattended execution. The auto-approve guard allows
the harness to bypass the human STOP step when a dual env-var pair is set.

**Contract:**

Before blocking on user input (after presenting the gate summary), check
whether BOTH of the following environment variables are present in the current
process environment:

  1. `QUOIN_GATE_AUTO_APPROVE=1` (exact value)
  2. `QUOIN_BENCHMARK_RUN` set to any non-empty value

**If and only if BOTH are set:**
- Skip the human STOP step entirely. Do NOT call AskUserQuestion.
- Emit a one-line notice: `[quoin-gate: auto-approved] QUOIN_GATE_AUTO_APPROVE=1 QUOIN_BENCHMARK_RUN=<value>`
- Treat this as an implicit user approval and proceed to writing the audit log.
- The audit log MUST include the following extra fields:
  - `auto_approved: true`
  - `env: QUOIN_GATE_AUTO_APPROVE=1 QUOIN_BENCHMARK_RUN=<run_id>`
  - `gate_encountered_at: <ISO timestamp>`
  - `benchmark_run_id: <QUOIN_BENCHMARK_RUN value>`

**If only ONE or NEITHER variable is set:** the gate behaves normally (blocking).

**Security rationale for the dual guard:**
The `QUOIN_BENCHMARK_RUN` variable is set by the benchmark orchestrator
(`run_benchmark.py`) and is not normally present in interactive user sessions.
The `QUOIN_GATE_AUTO_APPROVE` variable is set by the cell adapter for quoin
cells. Requiring BOTH prevents either variable alone from accidentally
bypassing human review in production sessions.

This contract MUST be implemented identically in every runtime adapter that
provides a gate skill.

## Gate levels

Gates run at three intensity levels depending on the task profile and the
phase transition:

### Smoke gate

Lightweight checks for plan completeness. Used after planning phases.

- Plan artifact exists and is non-empty.
- Plan has tasks with file paths and acceptance criteria.
- For Medium/Large complexity: convergence summary present with PASS verdict.

### Standard gate

Moderate checks for implementation correctness. Used after implementation
for Small and Medium tasks.

- Linter check (if configured).
- Tests for the files touched by the implementation (identified from diff).
- No debug artifacts (breakpoints, temporary print statements, TODO-remove
  comments) left in committed code.
- No credentials or secrets in the diff.
- No uncommitted changes.
- No task commits on a protected branch (main/master): a repo that has
  committed task work onto a protected branch (commits ahead of its upstream
  on main/master) is a **blocking failure**. A clean repo merely sitting on
  a protected branch with zero ahead-commits is NOT a failure (fail-OPEN).
  Recovery: use the canonical safe reset-to-origin recipe at
  `memory/branch-recovery.md` (deployed Tier-1 memory file) — move
  mis-placed commits to a feature branch first, then run the recipe.
- For non-Python deliverables (e.g., a TypeScript package with its own CI
  job), run the same correctness steps the CI job runs — compile, typecheck,
  lint, test (excluding packaging/publish/upload) — and treat a failing or
  unverifiable result as a blocking failure.

Inability to verify a correctness step (missing toolchain, or the check
disabled) is fail-CLOSED / blocking-surface — the workflow MUST surface it
for explicit human acknowledgment, never treat it as a silent pass.

### Full gate

Comprehensive checks. Used after implementation for Large tasks and after
review for all task sizes.

- Everything in the Standard gate, PLUS:
- Full test suite.
- Type checker (if applicable).
- All planned tasks are implemented (cross-reference plan task list).
- Branch is up to date with the base branch.
- No merge conflicts.
- Review verdict is APPROVED (for post-review gates only).
- No task commits on a protected branch (main/master): same rule as Standard
  gate — a repo with task commits ahead of its upstream on a protected branch
  is a blocking failure; a clean on-main state is not.
- For non-Python deliverables (e.g., a TypeScript package with its own CI
  job), run the same correctness steps the CI job runs — compile, typecheck,
  lint, test (excluding packaging/publish/upload) — and treat a failing or
  unverifiable result as a blocking failure (same rule as Standard gate,
  re-verified here since review fixes may have changed code).

Inability to verify a correctness step (missing toolchain, or the check
disabled) is fail-CLOSED / blocking-surface here too — never a silent pass.

## Gate checkpoints

Four checkpoints occur in the canonical workflow, plus one optional
pre-architecture checkpoint when a task feature spec is produced:

0. **Post-specify → pre-architecture.** Verify the task feature spec
   (`<task-root>/spec.md`) is present, non-empty, and carries its required
   sections (Context + Acceptance criteria); no formal gate level — a full
   spec check; a missing spec is not applicable and MUST be tolerated
   (grandfather).
1. **Post-architecture → pre-planning.** Verify architecture.md is present,
   non-empty, and covers the required sections. No formal gate level — always
   a full architecture check.
2. **Post-planning → pre-implementation.** Smoke gate (all task sizes). Verify
   the converged plan is present with a PASS verdict and full task coverage.
3. **Post-implementation → pre-review.** Standard gate (Small/Medium tasks)
   or Full gate (Large tasks). Verify implementation correctness and scope.
4. **Post-review → pre-finalization.** Full gate (all task sizes). Verify
   review APPROVED, tests pass, branch is clean and up to date.

## Out of scope

- The specific model tier used to run checks — that is the runtime adapter's
  responsibility.
- Subagent dispatch mechanism (self-dispatch to a cheaper tier, inline
  invocation, subagent spawn) — adapter-specific.
- Prompt-cache preamble bootstrap — adapter-specific optimization.
- Class A artifact write mechanism (the structured-body writer, format-kit
  references, validator invocation, atomic-rename pattern) — adapter-specific.
- `path_resolve.py` invocation for locating stage artifacts — adapter-specific
  tooling; the contract specifies that stage-aware resolution MUST be used, not
  how to implement it.
- Gate-level dispatch policy (which boundaries run inline vs. subagent) —
  adapter-specific.
- The rendering format of the human-facing checkpoint summary — adapter-specific
  display concern.
- Cost-ledger row format — adapter-specific plumbing.

## v3-format detection rule

The format of phase artifacts is determined by the following verbatim rule.
Every runtime adapter that reads or writes a v3 artifact MUST apply this
rule identically.

# v3-format detection (architecture.md §5.7.1 — copy verbatim)
# A file is v3-format iff:
#   - the first 50 lines following the closing `---` of the YAML frontmatter
#     contain a heading matching the regex ^## For human\s*$
# Otherwise the file is v2-format.
# On v3-format detection: read sections per format-kit.md for this artifact type.
# On v2-format (or no frontmatter): read the whole file as legacy v2.
# Detection MUST be string-comparison only — no LLM call (per lesson 2026-04-23
# on LLM-replay non-determinism).

## Notes

- The audit-log artifact ALWAYS lives at the task root (or task subfolder
  resolved for the current stage):
  `.workflow_artifacts/<task-name>/gate-{phase}-{date}.md`.
- The closed section set above is the contract; adapters MUST NOT silently
  introduce non-standard top-level sections.
- The runtime adapter owns: model tier, dispatch mechanism, prompt-cache
  preamble, audit-log write mechanism, gate-level routing per boundary, and
  cost-ledger plumbing.
- The human-facing checkpoint summary shown to the user is Tier 1 plain prose
  — never compressed. The audit log is the disk-side structured artifact.
- When requirements are ambiguous (e.g., task profile unknown), the skill
  MUST default to the Full gate level (safe fallback).
