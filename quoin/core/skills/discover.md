# discover

Runtime-neutral intent for the discover skill. Any runtime adapter (Claude,
Codex, …) that implements this skill should match the contract described here.

## Purpose

Scan every repository in the project folder, produce a structured inventory
plus a cross-repo architecture overview plus a dependency map plus recent git
activity, and populate the knowledge cache tree under
`.workflow_artifacts/cache/`. Discover is the onboarding step that gives every
downstream skill baseline context about the project's service landscape,
inter-repo relationships, and recent change history.

## When to use

- The user first sets up the workflow in a new project folder.
- The repo landscape has changed materially since the last scan.
- The user explicitly asks to scan, map, index, or learn the codebase.
- A downstream skill (the architect skill, planning, the start-of-day
  skill) detects stale or missing inventory output and requests a refresh.

## Inputs

- Project source code — read via the runtime adapter's scan mechanism.
  Discover MUST treat every top-level directory at the project root as a
  candidate repo and probe for the standard manifest files (package.json,
  go.mod, Cargo.toml, requirements.txt, pom.xml, build.gradle, etc.).
- Optional staleness file at `.workflow_artifacts/cache/_staleness.md`,
  recording per-repo HEAD from the last scan. When absent, the skill MUST
  fall back to the legacy `.workflow_artifacts/memory/repo-heads.md` for
  backward-compatibility until adapter-side consumers of that file are
  migrated to read `_staleness.md`; when both are absent, every repo is
  treated as needing a full scan.
- Existing `.workflow_artifacts/memory/` artifacts produced by prior
  discover runs (`repos-inventory.md`, `architecture-overview.md`,
  `dependencies-map.md`, `git-log.md`) — advisory only; the skill MAY
  inspect them to seed incremental updates but MUST overwrite each on a
  successful full scan.
- Active session-state files under `.workflow_artifacts/memory/sessions/`
  — used to detect whether a task context is active for the conditional
  cost-ledger row (see below).

All reads MUST tolerate missing files. Missing inputs are a "no signal"
outcome, not an error.

## Output

Four memory artifacts under `.workflow_artifacts/memory/` plus a populated
knowledge cache tree under `.workflow_artifacts/cache/`:

- `.workflow_artifacts/memory/repos-inventory.md` — one section per repo
  (identity, structure, entry points, key dependencies, exposed APIs,
  external calls). Closed section shape contracted per the artifact-format
  reference; the per-runtime adapter chooses the formatting primitives.
- `.workflow_artifacts/memory/architecture-overview.md` — system purpose,
  service map (ASCII diagram or structured prose), communication patterns
  (synchronous + asynchronous + shared data stores), representative
  request flows, key architectural decisions, deployment topology where
  discoverable.
- `.workflow_artifacts/memory/dependencies-map.md` — per-service depends-
  on/depended-on-by table, shared resources (databases, caches, message
  topics), deployment-order constraints, observed integration risks.
- `.workflow_artifacts/memory/git-log.md` — recent commit history across
  every repo (last ~50 commits, newest first), with a one-line summary
  of the change logic per commit (not just file names).

Plus the knowledge cache tree:

- `.workflow_artifacts/cache/_index.md` — root index listing every repo
  with a one-sentence purpose, primary language, and last-updated
  timestamp.
- `.workflow_artifacts/cache/_staleness.md` — per-repo HEAD plus
  ISO-timestamp Updated column. Successor to the legacy two-column
  `.workflow_artifacts/memory/repo-heads.md` file, which MUST also be
  written for backward compatibility until adapter-side consumers of that
  file are migrated to read `_staleness.md`.
- `.workflow_artifacts/cache/<repo-name>/_index.md` — per-repo summary.
- `.workflow_artifacts/cache/<repo-name>/_deps.md` — per-repo deps.
- `.workflow_artifacts/cache/<repo-name>/<directory>/_index.md` — per-
  directory summary, created only for directories with three or more
  source files that the adapter actually examined.
- `.workflow_artifacts/cache/<repo-name>/<directory>/<file-stem>.md` —
  per-file summary, created only for key files (entry points, API route
  definitions, model/schema definitions, configuration files, files with
  non-trivial business logic).
- `.workflow_artifacts/cache/<repo-name>/repo-map.md` — generated, token-
  budgeted symbol map ranked by cross-file references (`repo_map.py`,
  budget `QUOIN_REPOMAP_TOKENS`, default 1000). Best effort: a failed
  generation is skipped, never an error.

The cost-ledger row at `.workflow_artifacts/<task-name>/cost-ledger.md` is
written ONLY when a task context is active for this invocation (see
"Behavior contract" — conditional cost recording).

- `.workflow_artifacts/spec.md` (the repo main spec) — CONDITIONAL. The
  skill MAY produce or refresh this file when the user accepts an optional
  post-scan offer (draft if absent, refresh-with-diff if present). It is
  never produced unprompted.

## Discovery map (optional structured output)

Adapters MAY invoke `quoin/scripts/generate_discovery_map.py` after the four
markdown output files are written, in order to emit a structured
`discovery-map.json` index alongside (NOT replacing) the markdown artifacts.

The default write path is `<project_root>/.workflow_artifacts/discovery-map.json`.

The generator is optional — the discover skill MUST NOT fail if generation
fails; the markdown outputs remain the authoritative source.

## Behavior contract

- The skill MUST be read-only against source repos. No commits, no edits
  to repo content, no branch creation.
- The skill MUST be error-tolerant. A scan failure in one repo MUST NOT
  abort the run; the failure is reported in the user-facing summary and
  the remaining repos still produce output.
- The skill MUST perform incremental scanning. For each repo, compare the
  current HEAD against the stored value in the staleness file and skip
  the full scan when HEAD is unchanged. Skipped repos retain their cache
  entries unmodified. A full re-scan MUST be performed when the user
  explicitly requests it (phrases such as "rescan everything", "force
  rediscover").
- Cache writes are best-effort. A failed cache write MUST NOT fail the
  run — the memory artifacts above are the authoritative output. The
  skill MUST report cache-write failures in the user-facing summary.
- Cost-ledger writes are CONDITIONAL. Discover MAY run standalone (no
  task context — invoked by a user setting up the workflow) or as part
  of an active task (invoked by the run orchestrator or by a user inside
  an open task folder). The skill MUST append a row to
  `.workflow_artifacts/<task-name>/cost-ledger.md` only when a task name
  is determinable from the invocation context or active session state;
  otherwise it MUST skip cost recording.
- The skill MUST NOT auto-invoke downstream phases (the architect skill,
  planning, gating, implementation, review, finalization, or any other
  workflow step). After producing output and the user-facing summary,
  control returns to the user.
- After writing the four memory artifacts, the skill MAY offer to draft
  (when `.workflow_artifacts/spec.md` is absent) or refresh (when present,
  diff-surfaced) the repo main spec. The offer is suppressed whenever the
  bootstrap marker `.workflow_artifacts/.init-bootstrap-active` is present,
  and the skill MUST self-clear (delete) that marker when it honors it, so
  the marker survives at most one discovery and an aborted bootstrap can
  never suppress the offer permanently. Declining the offer is a valid
  no-op. A refresh MUST surface a diff and require confirmation before
  overwriting the existing file.
- The knowledge cache obeys three universal rules: cache is advisory and
  never authoritative; any skill that modifies source files MUST update
  the corresponding cache entry (enforced per-skill, not by discover);
  deleting `.workflow_artifacts/cache/` fully restores pre-cache
  behavior and the skill MUST NOT fail on a missing cache directory.

## Out of scope

- The specific scan mechanism (sequential walk, or any other parallelism
  strategy) — the runtime adapter chooses.
- The runtime adapter's chosen model tier — out of scope for the
  contract doc.
- Prompt-cache mechanics, subagent-preamble warming, or any token-
  optimization concern.
- The §0 self-dispatch grammar — a runtime cost-guardrail concern owned
  by the adapter.
- Per-file token-budget enforcement inside cache entries — the contract
  fixes the cache entry shape (per the shared knowledge-cache rules)
  but not the adapter's budget enforcement strategy.

## Notes

- Discover is one of the conditional cost-ledger skills (the other
  conditional skills per the shared rules are the gate, start-of-day,
  capture-insight, and triage skills). When no task is active, every
  other behavior in this contract still applies — only the cost-ledger
  row is suppressed.
- Incremental-scan correctness depends on the staleness file matching
  the per-repo HEAD recorded at the end of the prior scan. The
  staleness file MUST be rewritten on every run (including for skipped
  repos — their existing HEAD value is preserved). The legacy
  `repo-heads.md` MUST also be rewritten for backward compatibility.
- **Clock-reset invariant (D-06): the `Updated` column in `_staleness.md` MUST be
  set to the current run timestamp for ALL repos on every run, including skipped
  (HEAD-unchanged) repos.** This resets the staleness clock so the session-start
  staleness banner (`S-5`, added by IVG-106) does not fire after a successful
  incremental scan that found no changes. An incremental scan that is near-free
  in content terms is NOT stale in time terms — the clock must reflect the run.
- The `S-5` session-start banner and the start-of-day skill Step 1c read `_staleness.md`
  `Updated` values to detect whether discovery is stale. Re-run the discover skill when the
  banner fires, or when repos have changed significantly.
- When discover runs incrementally and some repos are skipped, the
  root `_index.md` MUST still be rewritten to reflect every repo
  (skipped repos keep their existing one-sentence summary; rescanned
  repos get a fresh one).
- The user-facing summary at end-of-scan MUST report: how many repos
  were found, how many were skipped (unchanged HEAD), how many were
  re-scanned, any cache-write failures, and a brief architecture
  highlight plus recent git activity highlight.
