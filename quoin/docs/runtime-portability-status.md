# Runtime Portability Status

This page tracks the current migration state.

For the cross-runtime feature matrix, see
`quoin/docs/runtime-parity-matrix.md`.

For the Phase 29 cross-runtime benchmark framework, see `quoin/benchmarks/`.

## Claude Code

Status: installable.

- `bash quoin/install.sh` remains the supported install command.
- Active Claude rules remain in `quoin/CLAUDE.md`.
- Active Claude skills remain in `quoin/skills/`, EXCEPT the twenty-one skills
  migrated across Phases 6–21 (`capture_insight`, `triage`, `start_of_day`,
  `review`, `plan`, `critic`, `revise`, `revise-fast`, `architect`,
  `thorough_plan`, `gate`, `implement`, `rollback`, `end_of_task`, `run`,
  `end_of_day`, `weekly_review`, `cost_snapshot`, `expand`, `discover`, `init_workflow`), which install from
  `quoin/adapters/claude/skills/<name>/SKILL.md`.
- Portable intent docs for the migrated skills live at
  `quoin/core/skills/capture_insight.md`,
  `quoin/core/skills/triage.md`,
  `quoin/core/skills/start_of_day.md`,
  `quoin/core/skills/review.md`,
  `quoin/core/skills/plan.md`,
  `quoin/core/skills/critic.md`,
  `quoin/core/skills/revise.md`,
  `quoin/core/skills/revise-fast.md`,
  `quoin/core/skills/architect.md`,
  `quoin/core/skills/gate.md`,
  `quoin/core/skills/thorough_plan.md`,
  `quoin/core/skills/implement.md`,
  `quoin/core/skills/rollback.md`,
  `quoin/core/skills/end_of_task.md`,
  `quoin/core/skills/run.md`,
  `quoin/core/skills/end_of_day.md`,
  `quoin/core/skills/weekly_review.md`,
  `quoin/core/skills/cost_snapshot.md`,
  `quoin/core/skills/expand.md`,
  `quoin/core/skills/discover.md`, and
  `quoin/core/skills/init_workflow.md`.
- Compatibility wrappers deploy to `~/.claude/scripts/`.
- Extracted portable implementations deploy to `~/.claude/core/scripts/`.

## Codex

Status: repo-local setup/readiness only; no verified global install target.

- Root `AGENTS.md` provides repo-local instructions.
- Codex adapter docs live under `quoin/adapters/codex/`.
- Codex setup is documented in `quoin/adapters/codex/setup.md`.
- A repo-local setup scaffold is now available:
  - Feature contract: `quoin/adapters/codex/installable-feature.md`
  - Machine-readable manifest: `quoin/adapters/codex/feature-manifest.json`
  - Generator script: `quoin/adapters/codex/generate_codex_assets.py`
  - Readiness script: `quoin/adapters/codex/verify_codex_readiness.py`
  - Runtime smoke script: `quoin/adapters/codex/smoke_codex_workflow.py`
  - Handoff guide: `quoin/adapters/codex/handoff.md`
  - Handoff validator: `quoin/adapters/codex/validate_codex_handoff.py`
  - Cost event guide: `quoin/adapters/codex/cost.md`
  - Cost event writer/checker: `quoin/adapters/codex/cost_event.py`
  - Workflow guide: `quoin/adapters/codex/workflow.md`
  - Procedure docs: `quoin/adapters/codex/procedures/{discover,plan,implement,review,gate}.md`
  - Skill adapter docs: `quoin/adapters/codex/skills/<skill>/README.md`
  - Unsupported behavior notes: `quoin/adapters/codex/unsupported-claude-behavior.md`
  - Run `quoin install --runtime codex --project-root <path>` or `quoin codex init --project-root <path>` to generate repo-local `AGENTS.md`.
  - Run `quoin install --runtime codex --project-root . --check` or `quoin codex init --project-root . --check` to verify an existing `AGENTS.md` is up to date without writing files.
  - Run `quoin doctor --runtime codex` to verify repo-local readiness.
  - Run `quoin doctor --runtime codex --smoke` to also run the deterministic repo-local smoke check.
  - Run `python3 quoin/adapters/codex/generate_codex_assets.py --project-root <path>` to generate `AGENTS.md`.
  - Run with `--check` to verify an existing `AGENTS.md` is up to date.
  - Run `python3 quoin/adapters/codex/generate_codex_assets.py --project-root . --adapter-assets --check` to verify generated Codex adapter skill docs.
  - Run `python3 quoin/adapters/codex/verify_codex_readiness.py --project-root .` to verify repo-local readiness.
  - Run `python3 quoin/adapters/codex/smoke_codex_workflow.py --project-root .` to verify the repo-local Codex setup path reaches portable workflow docs and avoids Claude-only runtime requirements.
  - Run `python3 quoin/adapters/codex/validate_codex_handoff.py --self-test` to validate the bundled handoff fixture.
  - Run `python3 quoin/adapters/codex/validate_codex_handoff.py --project-root . --file .workflow_artifacts/memory/sessions/<date>-<task>-codex.md` to validate a Codex session handoff file.
  - Run `python3 quoin/adapters/codex/cost_event.py --self-test` to validate the Codex cost writer/checker.
  - Run `python3 quoin/adapters/codex/cost_event.py write --project-root . --task <task> --phase <phase> --effort <effort>` to append a repo-local Codex cost row.
  - Run `python3 quoin/adapters/codex/cost_event.py validate --project-root . --task <task> --expect-codex` to validate Codex cost rows.
- There is no global Codex installer.
- There are no Codex command files.
- No global Codex paths are assumed.
- Passing readiness means Codex can use Quoin's repo-local artifact workflow; it
  does not mean a Codex runtime extension has been installed.
- Phase 33 makes the repo-local workflow practically executable in Codex for
  `discover`, `plan`, `implement`, `review`, and `gate` through documentation
  and static checks. It still does not add a global Codex installer or Codex
  command files.
- Phase 34 adds repo-local Codex session handoff procedures and deterministic
  validation for `.workflow_artifacts/memory/sessions/` continuation files. It
  does not add live Codex hooks.
- Phase 35 adds repo-local Codex cost event writing and validation. It appends
  portable cost-ledger rows with known task, phase, timestamp, session id, and
  effort values, and records token counts, dollar cost, and telemetry source as
  `not_available` because no verified Codex local telemetry interface exists.
- Phase 36 adds a consolidated CLI setup/readiness path:
  `quoin codex init` for repo-local `AGENTS.md` generation/checking and
  `quoin doctor --runtime codex` for non-destructive readiness checks. PR 2
  adds `quoin install --runtime codex` as an alias for the same repo-local
  `AGENTS.md` generator/check path while preserving Claude as the default
  `quoin install` runtime. These commands delegate to the existing Codex
  generator, readiness, and optional smoke scripts; they do not add global Codex
  install behavior.

## OpenCode

Status: qualification in progress; no runtime integration.

- Gateway qualification tooling lives in `quoin/adapters/opencode/` (probe and
  offline fake provider).
- Pinned release and claim status in `quoin/adapters/opencode/compatibility.md`.
- Open maintainer decisions in `quoin/adapters/opencode/decisions.md`.
- There is no OpenCode installer, no generated OpenCode assets, and no global
  OpenCode path assumed.

## Portable Core

Status: partially extracted.

- Portable scripts live under `quoin/core/scripts/`.
- Core workflow docs live under `quoin/core/workflow/`.
- Skill metadata lives in `quoin/core/workflow/skills.json`.
- Shared reference material still lives under `quoin/memory/`.

**Phase 23 (2026-05-12):** Extracted the portable cost-event schema (`quoin/core/scripts/cost_event.py`) with a `CostEvent` dataclass and pure functions `parse_row`, `format_row`, and `iter_events`. Expanded the portable cost-ledger contract (`quoin/core/workflow/cost-ledger.md`) from a 35-line stub into a full ~110-line portable contract covering row shape, append-only invariant, schema mapping, tolerated variations, malformed inputs, and out-of-scope boundaries. Runtime-specific cost collection (Claude session-log parsing, ccusage, model pricing) remains adapter-owned at `quoin/scripts/cost_from_jsonl.py`, `session_age_guard.py`, and `measure_revise_crossover_cost.py`, each annotated with a CLAUDE-ADAPTER-OWNED banner. A Codex cost collector is explicitly future work; no Codex pricing or session-ID acquisition is implemented in this phase.

**Phase 25 (2026-05-13):** Verified that this repository contains no stable
Codex global install target or command packaging contract. Codex remains
repo-local setup/readiness only. Added `verify_codex_readiness.py` to check root
instructions, portable workflow docs, Codex adapter docs, manifest scope, no
guessed global Codex paths, and Claude install isolation.

**Phase 26 (2026-05-13):** Generated/scaffolded Codex facing adapter docs for
all 21 migrated portable skills under `quoin/adapters/codex/skills/`. Each doc
references its portable `quoin/core/skills/<skill>.md` contract, records phase
and effort metadata from `quoin/core/workflow/skills.json`, and documents
unsupported Claude-only translations without introducing Codex command files or
global install paths. Extended the Codex generator, manifest, readiness check,
and tests to guard coverage and leakage.

**Phase 27 (2026-05-13):** Added a deterministic repo-local Codex runtime smoke
test at `quoin/adapters/codex/smoke_codex_workflow.py`. The smoke test follows
the documented Codex path from root `AGENTS.md` through Codex adapter setup and
skill docs to portable core skill/workflow docs for a minimal
architecture-plan-review workflow. It verifies `.workflow_artifacts/`,
`architecture.md`, `current-plan.md`, `review-1.md`, and `cost-ledger.md`
semantics are reachable without Claude global paths, Claude slash-command
requirements, Claude install routing, guessed Codex global paths, or `ccusage`
as a required Codex dependency. This is a deterministic repository smoke test;
live Codex runtime execution remains manual and no global Codex installer or
command packaging is implemented.

**Phase 28 (2026-05-13):** Added the evidence-based cross-runtime parity matrix
at `quoin/docs/runtime-parity-matrix.md`. The matrix separates portable core
contracts, Claude-supported behavior, repo-local Codex-supported behavior,
unsupported behavior, and planned work across major workflow semantics and all
21 migrated skills. Static docs tests now require both runtime-portability docs
to link to the matrix, require matrix coverage for all migrated skills and major
semantics, and guard against Codex global install or command-file overclaims.

**Phase 29 (2026-05-13):** Added the design-only cross-runtime benchmark
framework under `quoin/benchmarks/`. The framework defines repeatable scenarios
for fresh repo discovery, medium refactor planning, scoped implementation,
review, and session handoff / memory reuse. It compares `simple-claude`,
`quoin-claude`, `simple-codex`, and `quoin-codex` modes with metrics for task
completion quality, correctness / tests, artifact quality, context reuse, time
/ turn count, optional cost, and setup overhead. The framework includes run and
result templates plus `quoin/benchmarks/scripts/validate_benchmarks.py` for
deterministic structure checks. It does not bundle results or claim measured
benefits before actual benchmark runs exist.

**Phase 33 (2026-05-13):** Added repo-local Codex workflow execution procedures
for `discover`, `plan`, `implement`, `review`, and `gate`. The guide and
per-phase procedure docs live under `quoin/adapters/codex/`, link to the
portable contracts in `quoin/core/skills/`, use project-root
`.workflow_artifacts/`, and use optional `discovery-map.json` context where
present. Readiness, smoke, and pytest checks now guard procedure coverage and
runtime-assumption boundaries. No global Codex installation, command files,
approval replacement, sandbox replacement, or Claude runtime command
compatibility is implemented.

**Phase 34 (2026-05-13):** Added Codex session/handoff guidance at
`quoin/adapters/codex/handoff.md` plus deterministic validation at
`quoin/adapters/codex/validate_codex_handoff.py`. Codex continuation now has a
documented repo-local session file shape under
`.workflow_artifacts/memory/sessions/<date>-<task>-codex.md` covering status,
current stage, completed and unfinished work, decisions, finalized artifact
paths, continuation context, lesson candidates, and cost recording status. The
readiness, smoke, manifest, and pytest coverage now include handoff validation.
This remains explicit docs plus validation because no live Codex hook surface is
verified.

**Phase 35 (2026-05-13):** Added Codex cost event guidance at
`quoin/adapters/codex/cost.md` plus a repo-local writer/checker at
`quoin/adapters/codex/cost_event.py`. The writer uses the portable
`CostEvent` schema and cost-ledger row format, records known local values
(runtime, task, phase, timestamp, session id when supplied, effort, and
fallback fires), and marks token counts, dollar cost, and telemetry source as
`not_available`. Readiness, smoke, manifest, docs, and pytest coverage now
guard that Codex events are valid portable rows and do not depend on another
runtime's cost collector.

**Phase 36 (2026-05-14):** Added repo-local Codex setup/readiness CLI wrappers:
`quoin codex init --project-root <path>` generates `AGENTS.md`,
`quoin codex init --project-root . --check` checks it without writes, and
`quoin doctor --runtime codex` runs the existing readiness check. The optional
`--smoke` flag runs the existing deterministic Codex smoke script. The CLI
surface stays repo-local, reuses the existing scripts, and does not create a
global Codex installer or command-file target.

**Installability PR 2 (2026-05-14):** Added `quoin install --runtime
claude|codex`. Bare `quoin install` and bare `quoin` remain Claude installs to
`~/.claude`; `quoin install --runtime codex --project-root <path>` delegates to
the existing repo-local `AGENTS.md` generator, and `--check` delegates to the
existing check-only path.

## Still Claude-Specific

- Skill bodies in `quoin/skills/` (stubs only for the 21 migrated skills; full bodies now in `quoin/adapters/claude/skills/<name>/SKILL.md`).
- Slash-command invocation model.
- Agent and Skill dispatch instructions.
- Prompt-cache preamble generation.
- Claude model frontmatter.
- Claude JSONL session and cost plumbing.
- `ccusage` integration.

## Not Started

- Generated Claude skill files.
- Split shared skill intent from runtime overlays — partial: `capture_insight`, `triage`, `start_of_day`, `review`, `plan`, `critic`, `revise`, `revise-fast`, `architect`, `thorough_plan`, `gate`, `implement`, `rollback`, `end_of_task`, `run`, `end_of_day`, `weekly_review`, `cost_snapshot`, `expand`, `discover`, and `init_workflow` shipped under the adapter pattern across Phases 6–21.
- Full `cost_snapshot` narrowing to call `cost_event.parse_row` directly in
  every runtime adapter. The portable schema exists, Claude capture is
  unchanged, and Codex can now write explicit unavailable-telemetry rows.
