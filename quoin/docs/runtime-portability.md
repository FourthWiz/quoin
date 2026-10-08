# Runtime Portability

Quoin is moving from a Claude-specific workflow toolkit to a runtime-portable workflow system. The portable value is the artifact-centric workflow, not any single agent runtime.

This document defines the boundary for the first migration pass. It is intentionally conservative: existing Claude Code behavior remains the compatibility baseline.

For the current evidence-based feature-by-feature status, see
`quoin/docs/runtime-parity-matrix.md`.

For the Phase 29 benchmark design, see `quoin/benchmarks/`. The benchmark
framework compares simple Claude, Quoin + Claude, simple Codex, and Quoin +
Codex workflows across repeatable workflow-usefulness scenarios. It is a design
and evidence-capture framework only; it does not record results or claim
cross-runtime performance before actual runs exist.

## Portable Core

The portable core is the workflow contract that every runtime adapter should preserve:

- `.workflow_artifacts/` as the project-local workflow state directory.
- Task folders under `.workflow_artifacts/<task-name>/`.
- Stage-aware task layout with `stage-N/` subfolders.
- Root-level `architecture.md` and `cost-ledger.md` for staged tasks.
- `finalized/` archive semantics.
- Memory folders for sessions, daily rollups, weekly reviews, lessons learned, workflow rules, and workflow suggestions.
- Artifact formats for `architecture.md`, `current-plan.md`, `critic-response-N.md`, `review-N.md`, `gate-*.md`, and session state.
- Runtime-neutral helper behavior now lives in `quoin/core/scripts/`, with compatibility wrappers left under `quoin/scripts/`:
  - `path_resolve.py` is portable because it resolves task and stage artifact paths from `.workflow_artifacts`.
  - `validate_artifact.py` is portable because it validates markdown artifact structure.
  - `classify_critic_issues.py` is portable unless future changes add runtime-specific assumptions.
  - `cost_event.py` is portable because it provides the typed cost-event schema (`CostEvent` dataclass) and pure functions `parse_row`, `format_row`, and `iter_events` for reading and writing cost-ledger rows. It has no runtime-specific dependencies: no pricing tables, no session-log parsing, no UUID-acquisition logic. Canonical implementation at `quoin/core/scripts/cost_event.py`; compat wrapper at `quoin/scripts/cost_event.py`.
  - **IVG-111 cost-attribution surface (col-8 boundary).** `cost_event.py`'s `parse_attribution()`/`classify_attribution()` are the ONLY portable-core pieces of the col-8 cost-attribution surface introduced across ivg-111-cost-attribution stages 1-6: they parse a raw `k=v;k=v` attribution micro-map string into a dict and classify it into a `("legacy"|"resolved"|"unresolvable", usd)` verdict — pure string-in/tuple-out, no pricing table, no JSONL parsing, no UUID acquisition. Everything downstream of that verdict — resolving a UUID to a priced cost, walking `~/.claude` session transcripts, historical backfill — is Claude-adapter-owned (see the corresponding Claude Adapter bullet below). Invariant (architecture `D-04`, risk `R-05`): **`quoin/core/` never imports the adapter pricer/resolver/backfill modules** — verified by a core-purity grep guard, codified in `test_cost_core_no_claude_terms.py` (stage 6, T-07).
  - `cost_summary.py` is portable because it normalizes the `total` value from a `cost-summary.json` dict (the 7-key priority ladder `normalize_total()`), with no pricing tables, no JSONL parsing, no UUID logic — pure stdlib only. Consumer map: `cost-summary.json` is produced by `/end_of_task` and consumed only by `costService.ts` (extension) and `cost_summary.py` (normalizer). `/cost_snapshot` and `dashboard_model.py` consume `cost-ledger.md`, not `cost-summary.json`. `fallback_used=true` in `cost-summary.json` means "partial estimate — some ledger UUIDs did not resolve to JSONL sessions", NOT "cost unavailable"; a present total with `fallback_used=true` should render as `~$X (partial)`. Canonical implementation at `quoin/core/scripts/cost_summary.py`; compat wrapper at `quoin/scripts/cost_summary.py`.
  - `verify_claims.py` (IVG-115, §V ground-truth verification) is portable-with-a-seam: it reconciles claimed task/PR state against re-derived truth using `finalized/` folder presence (fully portable, no gh needed) and an optional `gh pr list` call gated behind `--finalized-only`/`--gh-json-file` (a generic git-host CLI, not Claude-only, and fully testable without a live binary via the JSON-file seam). No Claude-specific session parsing, no pricing tables. Canonical implementation at `quoin/core/scripts/verify_claims.py`; compat wrapper at `quoin/scripts/verify_claims.py`.

### Wrapper template (IVG-118 FR-6)

Every compatibility wrapper under `quoin/scripts/` follows the same
delegation-only shape (see `quoin/scripts/path_resolve.py` or
`quoin/scripts/nested_root_check.py` for live examples):

```python
_CORE_PATH = Path(__file__).resolve().parents[1] / "core" / "scripts" / "<name>.py"
_SPEC = importlib.util.spec_from_file_location("_quoin_core_<name>", _CORE_PATH)
_CORE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CORE)
```

The wrapper's `importlib.util.spec_from_file_location` call is given a
namespaced module name (`_quoin_core_<name>`, not the bare `<name>`) — this is
load-bearing, not cosmetic. `exec_module` does **not** implicitly register the
module in `sys.modules`; if two wrappers (or a wrapper re-imported within the
same process, e.g. under pytest) both call `spec_from_file_location` with the
*same* module name for two *different* file paths, or the same wrapper module
gets `exec_module`'d a second time in-process, the second call can silently
alias to (or clobber) the first module's namespace instead of raising —a
same-name re-import failure that only manifests at runtime, not at install
time. Registering `sys.modules[spec.name] = module` immediately after
`module_from_spec` (before `exec_module`) is the standard fix if a wrapper
ever needs the bare `<name>` (e.g. so a third party can `import <name>`
directly); the current convention sidesteps the issue entirely by always
using the `_quoin_core_` prefix, which guarantees no two wrappers' spec names
collide. New wrappers MUST follow this template — do not drop the prefix to
"simplify" the module name.

This is the third known silent-failure mode in the wrapper/registration
system, alongside a wrapper missing from `installer.DEPLOYED_SCRIPTS` and a
wrapped-core script missing from `installer.CORE_SCRIPTS` (both caught
mechanically by `quoin/dev/check_registration.py`, see IVG-118). Static
detection of the `sys.modules` ordering requirement was judged impractical
(spec FR-6, marked SHOULD not MUST); this documented, tested template is the
satisfying mitigation.

- Runtime-neutral workflow semantics now live in `quoin/core/workflow/`:
  - `rules.md` defines shared phase and safety rules.
  - `task-layout.md` defines task, stage, and finalization layout.
  - `session-state.md` defines handoff, lessons, and daily insight semantics.
  - `cost-ledger.md` defines portable ledger shape while leaving runtime cost capture to adapters.
  - `skills.json` defines runtime-neutral skill metadata while preserving current Claude model mappings.
  - `handoff-format.md` defines the inter-agent dispatch/return envelope, independent of the Codex session-handoff artifact.
  - `handoff-format-reference.md` defines the envelope's checkable-rule table and rule interaction cascade, split out of `handoff-format.md` to keep the core file's own read cost down.
- Shared reference material in `quoin/memory/`, including `format-kit.md`, `format-kit.sections.json`, `glossary.md`, `terse-rubric.md`, `summary-prompt.md`, `workflow-rules.md`, and `lessons-learned.md`.

The portable core should not know where a runtime stores global instructions, commands, sessions, approvals, or sandbox settings.

## Claude Adapter

The current active Claude adapter still lives at the existing paths for backward compatibility.

Claude-specific behavior includes:

- `quoin/install.sh`.
- `quoin/CLAUDE.md`.
- Slash-command invocation such as `/plan`, `/implement`, `/review`, and `/end_of_task`.
- Deployment under `~/.claude/skills`, `~/.claude/memory`, `~/.claude/scripts`, and `~/.claude/CLAUDE.md`.
- Claude Agent and Skill dispatch behavior.
- Prompt-cache preamble generation.
- Claude model tier names: Haiku, Sonnet, and Opus.
- Claude session JSONL lookup.
- `ccusage` integration and the fallback cost parser.
- `cost_from_jsonl.py` is Claude-specific because it reads Claude Code session logs and Claude model pricing. Despite living at `quoin/scripts/` (alongside portable wrappers), its adapter scope is declared by a CLAUDE-ADAPTER-OWNED banner at the top of the file. The portable cost-event schema belongs in `quoin/core/scripts/cost_event.py`; runtime-specific cost collection stays here.
- **IVG-111 cost-attribution surface (nested-transcript resolver/pricer + backfill).** `agent_transcript_cost.py` (`resolve_by_agent_id`/`resolve_by_tooluse`/`price_agent_jsonl`/`resolve_attribution`) and `backfill_cost_attribution.py` are Claude-adapter-owned: both read `~/.claude` session JSONL transcripts and the Claude `PRICES` table to resolve a subagent's per-tool-use cost and to backfill historical col-8 attribution. `analyze_cost_ledger.py`/`dashboard_cost.py` (the readers that apply the resolved-vs-unresolvable reader-precedence rule over col-8) are likewise Claude-adapter-owned, because they call the adapter pricer/resolver on the legacy (no-col-8) path; the pure col-8-only consumption path (`spend_monitor.py`, `dashboard_model.py`) stays in `quoin/core/scripts/` since it never resolves a UUID to a JSONL-priced cost itself. `cost_ledger_summary.py` (finalize-time ledger re-pricing, token-mix estimates) is likewise Claude-adapter-owned: it reads the same transcripts and price table and writes `cost-summary.json`, which core consumes only through the portable `cost_summary.py` normalizer. Core never imports any of `cost_from_jsonl` / `agent_transcript_cost` / `backfill_cost_attribution` / `cost_ledger_summary` (architecture `D-04`/`R-05`).
- `session_age_guard.py` is Claude-specific because it inspects Claude Code session files. A CLAUDE-ADAPTER-OWNED banner at the top of the file declares this scope explicitly.
- `measure_revise_crossover_cost.py` is Claude-specific because it imports `cost_from_jsonl` and references Claude model names. A CLAUDE-ADAPTER-OWNED banner at the top of the file declares this scope explicitly.
- `build_preambles.py` is Claude-specific because it generates Claude skill preambles under `~/.claude`.
- `verify_spawn_prompt_prefix.py` is Claude-specific because it verifies Claude Agent spawn behavior.
- `validate_adapter_drift.py` is Claude-specific because it validates `## §0 Model dispatch` headings, `adapters/claude/` paths, and install.sh routing — all Claude-specific adapter mechanics. Despite living in `core/scripts/` for wrapper-pattern symmetry with `path_resolve.py`, it checks Claude-only concepts. A future Codex adapter drift validator would be a parallel script (`validate_adapter_drift_codex.py`) rather than a generalization of this one.
- `inject_verification_step.py` (IVG-115, §V generator) is Claude-specific because it edits `adapters/claude/skills/*/SKILL.md` files in place — the injection targets (heading anchors, per-skill `SKILL.md` structure) are Claude adapter mechanics, structurally identical in scope to `inject_pollution_dispatch.py`.
- The `sessionend.sh` §V backstop edit (IVG-115/T-12) is Claude-specific because SessionEnd hooks are a Claude Code runtime concept (`~/.claude/hooks/`, registered via `settings.json`); the reconcile logic it calls out to (`verify_claims.py --finalized-only`) is itself portable, but the hook wiring is not.

The first migration pass must not break `bash quoin/install.sh` or change the installed Claude workflow.

## Codex Adapter

The initial Codex adapter is intentionally thin and repo-local:

- Root-level `AGENTS.md` provides repo-local Codex guidance.
- Codex uses the same `.workflow_artifacts/` conventions as the portable core.
- Codex should use native planning, approvals, sandboxing, and repo-scoped instruction handling.
- Quoin should not create a Codex global installer until a supported Codex extension point is verified.
- Quoin should not guess local Codex installation paths.
- Quoin should not duplicate Codex approvals or sandbox enforcement.

Codex support starts as instructions, readiness verification, and workflow
discipline around shared artifacts, not as feature parity with Claude slash
commands.

### Codex repo-local setup

A repo-local setup scaffold ships under `quoin/adapters/codex/`:

- `installable-feature.md` — feature contract (what is and is not supported)
- `feature-manifest.json` — machine-readable manifest; references `quoin/core/workflow/skills.json` by path, not by inline duplication
- `generate_codex_assets.py` — generates `AGENTS.md` at a given `--project-root`
- `verify_codex_readiness.py` — verifies root instructions, portable core docs, Codex adapter docs, manifest scope, no guessed global Codex paths, and Claude install isolation
- `smoke_codex_workflow.py` — validates the documented repo-local Codex path
  from setup instructions to portable workflow artifacts
- `handoff.md` — documents repo-local Codex handoff/session artifacts under
  `.workflow_artifacts/memory/sessions/`
- `validate_codex_handoff.py` — deterministically validates the Codex handoff
  shape and repo-local artifact paths
- `cost.md` — documents Codex cost event behavior and explicitly unavailable
  telemetry fields
- `cost_event.py` — appends and validates Codex cost ledger rows using the
  portable cost core
- `workflow.md` and `procedures/` — repo-local execution procedures for the
  Codex workflow loop without Codex command-file or global install claims

The generator produces only repo-local output. The readiness check reads only
repo-local files. Global Codex install paths, package registry behavior, and
Codex command file formats remain unresolved until a stable extension point is
verified.

### Phase 33 — Codex workflow execution procedures

Phase 33 adds practical Codex-native procedures for the core Quoin loop:

```text
discover -> plan -> implement -> review -> gate
```

The guide at `quoin/adapters/codex/workflow.md` and per-phase procedure docs
under `quoin/adapters/codex/procedures/` tell Codex how to execute each phase
using natural-language requests, native planning/tool/check behavior,
project-root `.workflow_artifacts/`, portable skill contracts, and the optional
`discovery-map.json` produced by `quoin/scripts/generate_discovery_map.py`.

The procedures are documentation and static-check targets only. They do not add
global Codex installation, Codex command files, Codex approval or sandbox
replacement logic, Codex model names, or Claude runtime command compatibility.

### Phase 34 — Codex session handoff validation

Phase 34 adds explicit Codex continuation behavior without claiming live hooks.
Codex writes a session handoff file at:

```text
.workflow_artifacts/memory/sessions/<YYYY-MM-DD>-<task-name>-codex.md
```

The file records status, current stage, completed work, unfinished work,
decisions, finalized artifact paths, continuation context, lesson candidates,
and cost recording status. The next Codex session reads and validates this file
before resuming:

```text
python3 quoin/adapters/codex/validate_codex_handoff.py --self-test
python3 quoin/adapters/codex/validate_codex_handoff.py --project-root . --file .workflow_artifacts/memory/sessions/<date>-<task>-codex.md
```

The validator is deterministic and checks the documented markdown shape,
required metadata, project-root `.workflow_artifacts/` paths, continuation
fields, and unsupported runtime path leakage. It is not a Codex runtime hook and
does not introduce a global Codex install path or command system.

### Phase 35 — Codex cost event writer

Phase 35 adds explicit Codex cost recording without claiming unavailable
runtime telemetry. Codex can append a portable cost-ledger row with:

```text
python3 quoin/adapters/codex/cost_event.py write --project-root . --task <task> --phase <phase> --effort <low|medium|high|max|unknown>
python3 quoin/adapters/codex/cost_event.py validate --project-root . --task <task> --expect-codex
```

The writer uses `quoin/core/scripts/cost_event.py` and the row shape documented
in `quoin/core/workflow/cost-ledger.md`. It records runtime, task, phase,
timestamp, session id when supplied, effort, and fallback fires. It records
`input_tokens`, `output_tokens`, cache token fields, `total_tokens`, `cost_usd`,
and `telemetry_source` as `not_available` because no verified Codex local
telemetry interface exists in this repository.

The Codex row uuid starts with `unknown-codex-` so existing readers treat it as
an unresolved cost source. This is deliberate: unavailable usage must remain
unknown instead of being inferred from another runtime's collector.

### Phase 26 — Codex adapter skill docs

Phase 26 adds generated/scaffolded Codex facing docs for all 21 migrated
portable skills:

- `quoin/adapters/codex/skills/<skill>/README.md` references the corresponding
  `quoin/core/skills/<skill>.md` contract and records the skill phase, effort,
  and user-facing status from `quoin/core/workflow/skills.json`.
- `quoin/adapters/codex/skills/README.md` indexes the 32 skill docs.
- `quoin/adapters/codex/unsupported-claude-behavior.md` documents Claude-only
  behavior that is intentionally not translated into Codex.

These files are adapter docs, not Codex command files. They do not define global
Codex install paths, approval behavior, sandbox behavior, or model-dispatch
mechanics. Codex still uses native runtime behavior and preserves Quoin only at
the artifact/workflow layer.

### Phase 27 — Codex runtime smoke test

Phase 27 adds `quoin/adapters/codex/smoke_codex_workflow.py`, a deterministic
repository smoke test for the minimal Codex workflow path. It does not attempt
to automate live Codex runtime behavior. Instead, it checks that the files and
links a Codex session would need are present and coherent:

- root `AGENTS.md`
- Codex setup and adapter docs
- Codex skill adapter docs for `architect`, `plan`, and `review`
- portable core skill docs
- portable workflow docs for task layout, session state, and cost ledger

The smoke test also guards the Codex path against Claude global paths,
Claude slash-command requirements, Claude install routing, guessed Codex global
paths, and `ccusage` as a required Codex dependency.

### Phase 29 — Cross-runtime workflow benchmarks

Phase 29 adds `quoin/benchmarks/`, a design-only benchmark framework for
comparing Quoin-assisted workflows against simple Claude and simple Codex
workflows. The suite covers fresh repo discovery, medium refactor planning,
scoped implementation, review, and session handoff / memory reuse. It defines
the four comparison modes, scoring metrics, run sheets, result templates, and a
deterministic structure validator at
`quoin/benchmarks/scripts/validate_benchmarks.py`.

This framework intentionally separates benchmark design from benchmark results.
No measured outcomes are bundled, and cost remains optional because runtime cost
capture is not equally implemented across Claude and Codex.

## Candidate Shared Skills

Most skill instructions contain portable workflow intent mixed with Claude runtime mechanics. For now, they stay in place as Claude skills.

Future work should split each skill into:

- Shared workflow intent: inputs, artifacts read, artifacts written, phase semantics, gates, and handoff rules.
- Runtime adapter overlay: invocation syntax, model selection, subagent mechanics, tool names, deployment paths, and runtime-specific cost/session handling.

Do not duplicate skill templates before this split exists. Duplication would make the shared workflow rules drift.

## First-Pass Rule

The first implementation pass established the boundary and guard tests. Portable script implementations may move into `quoin/core/scripts/` only when old `quoin/scripts/` entrypoints remain compatible for Claude installs and existing docs.

### Migrated skills (Phases 6–21)

Twenty-one skills now install from the Claude adapter path
(`quoin/adapters/claude/skills/<name>/SKILL.md`); the runtime-neutral
intents live at `quoin/core/skills/<name>.md`. All other skills still
install from `quoin/skills/<name>/SKILL.md`.

- `capture_insight` shipped in Phase 6 as the pilot.
- `triage` and `start_of_day` followed in Phase 7.
- `review` migrated in Phase 8.
- `plan`, `critic`, `revise`, and `revise-fast` migrated in Phase 9.
- `architect` and `thorough_plan` migrated in Phase 10.
- `gate` migrated in Phase 11.
- `implement` migrated in Phase 12.
- `rollback` migrated in Phase 13.
- `end_of_task` migrated in Phase 14.
- `run` migrated in Phase 15.
- `end_of_day` migrated in Phase 16.
- `weekly_review` migrated in Phase 17.
- `cost_snapshot` migrated in Phase 18.
- `expand` migrated in Phase 19.
- `discover` migrated in Phase 20.
- `init_workflow` migrated in Phase 21.

### Phase 22 — Adapter drift validator

Phase 22 added an adapter drift validator at `quoin/core/scripts/validate_adapter_drift.py` (compat wrapper at `quoin/scripts/validate_adapter_drift.py`) that asserts 16 structural invariants across the three-file adapter pattern. The validator is run by `pytest quoin/dev/tests/test_validate_adapter_drift.py` and can be invoked directly during development.

**What this phase did:**
- Extended `skills.json` `schema_version` from 1 to 2 with two new optional fields: `section_0` (bool) and `spawn_target` (bool).
- Added `validate_adapter_drift.py` in `quoin/core/scripts/` (canonical) and `quoin/scripts/` (compat wrapper), covering 16 invariants (AD-CO through AD-IO) over all 28 skills in the manifest.
- Added 23 tests in `quoin/dev/tests/test_validate_adapter_drift.py` (2 positive, 16 invariant negatives, 5 misc).
- Documented the invariants and new fields in `quoin/core/workflow/skills.md`.

**What this phase did NOT do:**
- Did not consolidate the 17 existing `test_*_adapter_pilot.py` files — they remain as point-in-time regression guards; the new validator is the forward-looking guard.
- Did not implement generator scaffolding (deferred per plan D-01).
- Did not add a Codex-adapter validator (D-09 documents the parallel-script pattern for future runtime adapters).
- Did not add path fields to the manifest (`portable_doc_path`, etc.) — paths are derivable by convention from `name` (D-02).
- Did not deploy `validate_adapter_drift.py` to `~/.claude/scripts/` — it is a repo-development tool invoked only by pytest and developers, not a runtime helper.
- Did not change `install.sh` — the validator is purely a dev/CI tool.

**How to extend for future runtime adapters:** per D-09, a Codex adapter drift validator would be a parallel script (`validate_adapter_drift_codex.py` in `quoin/core/scripts/`), not a generalization of this one. The Claude-specific concepts (§0 dispatch, `adapters/claude/` paths, install.sh routing) are intrinsic to the Claude adapter.


## Discovery Map (additive structured-data layer)

Phase 30 adds a portable structured snapshot format for quoin project state. The schema, validator,
and example fixture are implemented now. The generator integration with `/discover` is FUTURE WORK
and out of scope for Phase 30.

**Implemented files (Phase 30):**
- `quoin/core/schemas/discovery-map.schema.json` — JSON Schema Draft-07 contract for `discovery-map.json`
- `quoin/core/schemas/discovery-map.md` — prose schema reference (field types, path conventions, timestamp conventions, extensions namespace, versioning)
- `quoin/dev/tests/fixtures/discovery-map/example-map.json` — valid example fixture modeled on the Codex_workflow project
- `quoin/core/scripts/validate_discovery_map.py` — stdlib-only validator (9 invariants DM-01..DM-09); compat wrapper at `quoin/scripts/validate_discovery_map.py`

**Non-goals for Phase 30 (future work):**
- No Markdown artifact changes — existing `.workflow_artifacts/` layout is unchanged
- No `/discover` SKILL.md modifications — `/discover` continues to write existing Markdown outputs
- No new external dependencies — validator is pure stdlib
- No `.workflow_artifacts/` layout changes — schema is additive

**Phase 32 generator implementation:** Phase 32 implements the generator. Paths:

- `quoin/core/scripts/generate_discovery_map.py` — canonical stdlib-only generator
- `quoin/scripts/generate_discovery_map.py` — compat wrapper (same two-line diff pattern as validate wrapper)

Adapter wiring is documented as prose-only optional hooks in the `/discover` skill docs:

- `quoin/core/skills/discover.md` — `## Discovery map (optional structured output)` H2 section
- `quoin/adapters/claude/skills/discover/SKILL.md` — `### Structured-output hook (optional)` H3 section inside `## After scanning`

The Claude adapter hook invokes `python3 ~/.claude/scripts/generate_discovery_map.py "$PROJECT_ROOT" --quiet`
after the four markdown files are written. The hook is best-effort; a non-zero exit emits a warning
and continues. The Codex adapter can run `python3 quoin/scripts/generate_discovery_map.py "$PROJECT_ROOT" --quiet`
directly without any global-path assumption.

**Remaining future work (Phase 32 deferred, per D-09):**
- Language detection (`repos[].language`), entry-point detection, and `dependency_hints` derivation.
- `--no-mtimes` flag for cloud-synced repo stability.
- Per Phase 30 D-09, a future third JSON validator (if added) should extract shared scaffolding into
  `quoin/core/scripts/_validator_common.py` rather than duplicating exit-code/CLI conventions.
