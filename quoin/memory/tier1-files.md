# Tier 1 — files that always stay English (caveman-token-optimization carve-out)

Relocated from `quoin/CLAUDE.md` (claude-md-trim 2026-06-25). When deciding whether a file is exempt from terse-style writing, consult this catalog.

The following files are explicitly **excluded** from terse-style writing — they stay in human-readable English at all times:

**User-facing rendered output:** chat messages to the user; `/gate` rendered checkpoint summary.

**Hand-edited files:**
- `quoin/CLAUDE.md` (this file).
- `quoin/memory/lessons-learned.md`.
- `quoin/memory/terse-rubric.md` (+ deployed copy at `__QUOIN_HOME__/memory/`) — compressing recreates the v1 CRIT-2 circular dependency.
- `quoin/memory/format-kit.md` (+ deployed copy at `__QUOIN_HOME__/memory/`) — v3 content-type → primitive mapping.
- `quoin/memory/glossary.md` (+ deployed copy at `__QUOIN_HOME__/memory/`) — v3 abbreviation whitelist + status glyphs.
- `quoin/memory/format-kit.sections.json` (+ deployed copy at `__QUOIN_HOME__/memory/`) — machine-readable allowed/required sections sidecar.
- `quoin/memory/summary-prompt.md` (+ deployed copy at `__QUOIN_HOME__/memory/`) — frozen Haiku prompt template for Class B writer Step 2.
- `quoin/memory/format-kit-pitfalls.md` (+ deployed copy at `__QUOIN_HOME__/memory/`) — pre-write reminder block for Class B writers' Step 1.
- `quoin/memory/sleep-signals.yaml` (Tier 1 hand-edited source of truth for sleep importance signals)
- `__QUOIN_HOME__/memory/sleep-signals.yaml` (deployed copy — overwritten on re-install)
- `quoin/memory/cache-guide.md` (Tier 1 hand-edited cache entry format reference)
- `__QUOIN_HOME__/memory/cache-guide.md` (deployed copy — overwritten on re-install)
- `quoin/memory/cost-ledger-format.md` (+ `__QUOIN_HOME__/memory/cost-ledger-format.md` deployed copy — extracted verbose Cost tracking row-format from CLAUDE.md 2026-05-15).
- `quoin/memory/dispatch-guide.md` (+ `__QUOIN_HOME__/memory/dispatch-guide.md` deployed copy — extracted verbose §0 / §0' dispatch details from CLAUDE.md 2026-05-15).
- `quoin/memory/hooks-table.md` (+ `__QUOIN_HOME__/memory/hooks-table.md` deployed copy — extracted hooks event/matcher table from CLAUDE.md 2026-05-15).
- `quoin/memory/lifecycle-guide.md` (+ `__QUOIN_HOME__/memory/lifecycle-guide.md` deployed copy — extracted verbose Lifecycle skills + memory-layers details from CLAUDE.md 2026-05-15).
- `quoin/memory/branch-recovery.md` (+ `__QUOIN_HOME__/memory/branch-recovery.md` deployed copy — canonical safe branch-reset recipe (`git update-ref`) for recovering from "commits on main"; added IVG-77 2026-06-17).
- `quoin/memory/preamble-guide.md` — subagent prompt-cache warm-up; added IVG-50 S-3.
- `quoin/memory/memory-maintenance.md` (+ `__QUOIN_HOME__/memory/memory-maintenance.md` deployed copy — Tier-1 reference doc for memory lifecycle: archive/soft-forget/delete policy, pattern schema, consumer contracts; added IVG-50 S-3 2026-06-18).
- `quoin/memory/memory-maintenance.yaml` (+ `__QUOIN_HOME__/memory/memory-maintenance.yaml` deployed copy — pattern config (ignore/archived/read_only globs) for memory_check.py and /sleep; token-free glob file; added IVG-50 S-3 2026-06-18).
- `quoin/memory/tier1-files.md` (+ `__QUOIN_HOME__/memory/tier1-files.md` deployed copy — relocated Tier-1 catalog; extracted claude-md-trim 2026-06-25)
- `quoin/memory/verification-guide.md` (+ `__QUOIN_HOME__/memory/verification-guide.md` deployed copy — §V ground-truth verification verbose reference, analog of `dispatch-guide.md`; added IVG-115 2026-07-09)
- `quoin/memory/checkpoint-spec.md` (+ `__QUOIN_HOME__/memory/checkpoint-spec.md` deployed copy — behavioral specification of the `/checkpoint` subsystem, source-cited characterization used as the test-harness contract; added checkpoint-spec-harness 2026-07-14)
- `quoin/memory/decision-gate-guard.md` (+ `__QUOIN_HOME__/memory/decision-gate-guard.md` deployed copy — shared fail-closed decision-gate guard reference: the "cannot ask ≠ approved" invariant, the `[no-interactive]` sentinel, the decision-gate contract, the `needs-decision-{task}.md` sentinel schema, the classification-marker/census convention; added IVG-150 2026-07-23)
- `quoin/memory/clean-authored-content.md` (+ `__QUOIN_HOME__/memory/clean-authored-content.md` deployed copy — shared rule for keeping comments, commit messages, and PR descriptions free of planning-process vocabulary; added 2026-08-15)
- `quoin/memory/comment-cleanup-criteria.md` (+ `__QUOIN_HOME__/memory/comment-cleanup-criteria.md` deployed copy — category-3 keep/remove criteria and worked examples for the pre-PR comment cleanup; added IVG-255 2026-09-09)

**Generated Tier-1 files (never hand-edit; regenerate with `quoin/scripts/build_claude_slim.py`):**
- `quoin/memory/workflow-catalog.md` (+ `__QUOIN_HOME__/memory/workflow-catalog.md` deployed copy — full verbatim text of every section dropped from `CLAUDE.slim.md`, generated from `quoin/CLAUDE.md` by `quoin/scripts/build_claude_slim.py`; regenerate with `python3 quoin/scripts/build_claude_slim.py`; added IVG-164 stage 1 2026-08-07). This is the first GENERATED member of `TIER1_MEMORY_FILES` — the precedent for "generated but Tier-1-listed" already exists above in this file's **Source files:** block for `skills/<skill>/preamble.md`, which is listed "only for disambiguation — NOT a Tier 1 hand-edited source file"; the same disambiguation applies here. Tier-1 membership here means always-English + never terse-rewritten — it does NOT mean hand-editable, and `/sleep` must never treat it as a hand-maintained memory file.

NOTE: QUICKSTART.md sits at `quoin/` root and deploys to `__QUOIN_HOME__/QUICKSTART.md` (NOT under `memory/`) — this is intentional. Do NOT normalize paths.
- `quoin/QUICKSTART.md` (+ `__QUOIN_HOME__/QUICKSTART.md` deployed copy).

**Contract-approval files (v3 format):**
- `<task>/architecture.md` — has an English `## For human` summary block at the top (read by humans and `/gate`); body is format-aware structured per `quoin/memory/format-kit.md` (read by skills).
- `<task>/review-<round>.md` — same v3 format as architecture.md.
- `<task>/cost-ledger.md` (structured, not prose; no v3 changes — append-only row format only).
- `<task>/spec.md` — per-task FEATURE spec (Class A always-English; headings `## Context`, `## User stories`, `## Functional requirements`, `## Acceptance criteria`, `## Out of scope`; NO `## For human` block).
- `.workflow_artifacts/spec.md` — REPO main spec (Class A; headings `## Context`, `## Goals`, `## Capabilities`, `## Acceptance criteria`, `## Non-goals`).

**Rendered briefings:** `memory/weekly/*.md`; `memory/daily/<date>.md` (NOT `daily/insights-<date>.md`, which is Tier 3).

**Source files:** `MEMORY.md`; `quoin/skills/**/SKILL.md`; `quoin/dev/tests/fixtures/quoin-stage-1-preamble.md`; `quoin/dev/verify_subagent_dispatch.md`; `quoin/dev/tests/fixtures/path_resolve/**`; `quoin/skills/<skill>/preamble.md` (any of the 7 spawn targets — critic, revise, revise-fast, plan, review, gate, architect) — GENERATED by `quoin/scripts/build_preambles.py` at install time; never hand-edit. The file is machine-generated English content; listed here only for disambiguation — it is NOT a Tier 1 hand-edited source file.

`.planner-trace.md` is a Tier-3 ephemeral: machine-written by `/plan`, read by `/critic` as a search-prior only, deleted by `/end_of_task` before archive; no Haiku summary, no validator.

If adding a new file class: hand-edited or contract-approved → Tier 1; ephemeral or machine-only → Tier 3; user-approves-but-machine-reads → Tier 2.
