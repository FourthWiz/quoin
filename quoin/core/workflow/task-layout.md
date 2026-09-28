# Task Layout

Quoin stores workflow state under `.workflow_artifacts/` at the project root.

## Single-Stage Tasks

Single-stage tasks use a root task folder:

```text
.workflow_artifacts/<task-name>/
  architecture.md
  current-plan.md
  critic-response-1.md
  review-1.md
  gate-*.md
  cost-ledger.md
```

Task names are descriptive kebab-case names derived from the task description.

## Multi-Stage Tasks

Multi-stage tasks keep shared artifacts at the task root and stage-scoped artifacts under `stage-N/` folders:

```text
.workflow_artifacts/<task-name>/
  architecture.md
  cost-ledger.md
  stage-1/
    current-plan.md
    critic-response-1.md
    review-1.md
    gate-*.md
  stage-2/
    ...
```

A task is multi-stage when `architecture.md` contains a `## Stage decomposition` section.

## Root-Level Artifacts

These always remain at the task root:

- `architecture.md`
- `cost-ledger.md`

Stage plans, critic responses, reviews, and gate audit logs live in the resolved task directory for the stage.

## Path Resolution

The portable resolver is `quoin/core/scripts/path_resolve.py`. Existing runtime entrypoints may call compatibility wrappers under `quoin/scripts/`.

Resolution order:

1. Integer stage: `stage N of <task>` resolves to `<task-name>/stage-N/`.
2. Stage name: a descriptive stage name is looked up in `architecture.md`.
3. Default: no stage resolves to the task root for legacy and single-stage tasks.

Existing mixed or legacy layouts are not auto-migrated.

## Finalization

Completed work moves to a `finalized/` folder only during explicit task finalization. Planning and implementation must keep active work in the active task folder; the one exception is the cleanup skill's task bookkeeping pass, which may archive a task folder after explicit per-task confirmation.
