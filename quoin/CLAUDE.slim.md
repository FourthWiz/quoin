# Development Workflow — Shared Rules

- **Flow:** `/run` (default): triage, implement, verify, local commit; a review or stages only when triage calls for them. Heavy pipeline, only via `/run hard:`: `/discover → /specify → GATE → /architect → GATE → /thorough_plan → GATE → /implement → GATE → /review → GATE → /end_of_task`.
- **Explicit commands:** no skill auto-invokes `/implement` or `/end_of_task`; only `/run_hard` may (`autonomous-mode.md`). Light `/run` commits locally, never pushes. `/pr` is always a separate user action.
- **Git:** commit locally only; push happens in `/end_of_task`, PRs only via `/pr`. Start each task on a fresh branch from an up-to-date main; never land task commits on main/master. Conventional commit messages (`type(scope): summary`, body says why).
- **Artifacts:** one `.workflow_artifacts/` root per project; task files live in `.workflow_artifacts/<task-name>/` (`stage-N/` for multi-stage tasks). Never move anything into `finalized/` except through `/end_of_task`.
- **Shipped text** (code comments, commits, PR bodies) is plain engineering prose: no plan or finding IDs, gate verdicts or artifact paths (`clean-authored-content.md`).
- **Lessons:** native auto memory (`MEMORY.md`) holds personal facts only; reusable engineering lessons go to the committed `.workflow_artifacts/memory/lessons-learned.md`, which planning skills read. `/capture_insight` logs gotchas to `daily/insights-<date>.md`.
- **Serena:** only when `mcp__serena__*` names appear among the available or deferred tools, follow `serena-activation.md`; otherwise never call them.

Full rules: every section of the workflow rules is kept verbatim, under its original heading, in ${CLAUDE_PLUGIN_ROOT}/memory/workflow-catalog.md; a skill citing CLAUDE.md "<section>" means that section there.
Topic guides (same folder): autonomous-mode.md, cache-guide.md, clean-authored-content.md, cost-ledger-format.md, dispatch-guide.md, hooks-table.md, lifecycle-guide.md, preamble-guide.md, serena-activation.md, tier1-files.md, verification-guide.md
