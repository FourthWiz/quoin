# Core Workflow Rules

These are runtime-neutral Quoin workflow rules. Runtime adapters may translate invocation syntax, model selection, and tool mechanics, but they should preserve these semantics.

## Safety

- Do not move active task folders into `.workflow_artifacts/finalized/` during planning, implementation, or review; the one exception is the cleanup skill's task bookkeeping pass, which may archive a task folder after explicit per-task confirmation.
- Finalization is an explicit user decision.
- Implementation is an explicit user decision unless the user has deliberately invoked an end-to-end orchestrator and confirmed its checkpoints.
- Pull requests and remote publishing are explicit user actions controlled by the active runtime adapter.
- Task artifacts are authoritative workflow state. Do not rely on chat memory when an artifact exists.

## Flow

The full workflow is:

```text
discover -> specify -> gate -> architect -> gate -> plan -> gate -> implement -> gate -> review -> gate -> end-of-task
```

Small tasks may skip architecture and critic loops. Medium and large tasks use stronger planning and review discipline. Gates preserve the human approval boundary between phases.

## Shared Responsibilities

Every runtime adapter should preserve:

- artifact-centric task state under `.workflow_artifacts/`
- task and stage layout rules
- session state handoff
- lessons learned
- artifact validation where available
- cost ledger semantics, even if runtime-specific cost capture differs
- advisory knowledge cache behavior

Runtime adapters own command syntax, subagent mechanics, model mapping, permissions, approvals, sandboxing, and runtime-specific session/cost plumbing.
