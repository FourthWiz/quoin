# Codex Procedure: discover

Portable contract: `quoin/core/skills/discover.md`

Shared workflow docs:

- `quoin/core/workflow/rules.md`
- `quoin/core/workflow/task-layout.md`
- `quoin/core/workflow/session-state.md`
- `quoin/core/workflow/cost-ledger.md`
- `quoin/core/schemas/discovery-map.md`
- `quoin/adapters/codex/handoff.md`

## Purpose

Build or refresh Quoin's repository inventory, memory artifacts, advisory cache,
and optional structured discovery map under project-root `.workflow_artifacts/`.

## Codex Procedure

1. Resolve the Quoin project root as the repository root containing `AGENTS.md`.
2. Inspect existing discovery state when present:
   - `.workflow_artifacts/discovery-map.json`
   - `.workflow_artifacts/cache/_staleness.md`
   - `.workflow_artifacts/memory/repo-heads.md`
   - `.workflow_artifacts/memory/repos-inventory.md`
   - `.workflow_artifacts/memory/architecture-overview.md`
   - `.workflow_artifacts/memory/dependencies-map.md`
   - `.workflow_artifacts/memory/git-log.md`
3. Enumerate repositories from the project root and compare git HEAD values with
   the staleness files. Treat missing files as no prior signal. If
   `.workflow_artifacts/memory/discovery-dirty` exists, scan every repo
   regardless of HEAD, and delete the marker once every repo scanned
   successfully.
4. Scan source repositories read-only. Use Codex native search and file-read
   behavior, and continue scanning other repos when one repo has a local error.
5. Write the required markdown artifacts:
   - `.workflow_artifacts/memory/repos-inventory.md`
   - `.workflow_artifacts/memory/architecture-overview.md`
   - `.workflow_artifacts/memory/dependencies-map.md`
   - `.workflow_artifacts/memory/git-log.md`
6. Populate or refresh advisory cache files under `.workflow_artifacts/cache/`
   where useful. Cache write failures should be reported but should not fail the
   discovery phase.
7. Generate the optional structured map after markdown artifacts are written:

   ```bash
   python3 quoin/scripts/generate_discovery_map.py "$PROJECT_ROOT" --quiet
   ```

   If generation fails, report the warning and keep the markdown artifacts as
   authoritative discovery output.
8. Update `.workflow_artifacts/memory/sessions/` with what was scanned, what was
   skipped, and any failures. Append a cost-ledger row only when an active task
   context is determinable.
9. When yielding or closing the session, write a Codex handoff file under
   `.workflow_artifacts/memory/sessions/` and validate it with
   `quoin/adapters/codex/validate_codex_handoff.py`.
10. Stop after the user-facing summary. Do not start planning or implementation
   automatically.

## Codex Native Notes

- Use Codex native planning for the live scan checklist.
- Do not edit source files during discovery.
- Use `discovery-map.json` as advisory context on later phases, but verify code
  facts from source before planning or editing.
- Do not introduce global setup, command files, or runtime-specific packaging
  assumptions for this procedure.
