# run_hard Codex Adapter

Generated/scaffolded from portable Quoin metadata.

Portable source:

- `quoin/core/skills/run_hard.md`
- `quoin/core/workflow/skills.json`

## Codex invocation

Ask for this workflow phase in natural language. Codex does not get a generated
command file for `run_hard` in this phase.

## Portable workflow contract

Follow the runtime-neutral contract in `quoin/core/skills/run_hard.md`. Preserve Quoin artifact
semantics under the project-root `.workflow_artifacts/` directory:

- phase: `orchestration`
- effort: `max`
- user-facing: `yes`

Use `quoin/core/workflow/` for shared task layout, session state, cost-ledger,
artifact, and skill metadata rules.

## Codex runtime notes

- Treat the repository root containing `AGENTS.md` as the Quoin project root.
- Read and write workflow artifacts at that project root, even when editing code
  in a nested package.
- Use Codex-native planning, progress tracking, approvals, sandboxing,
  repo-scoped instructions, and model or reasoning controls.
- Do not create a Codex global install, command file, approval layer, sandbox
  layer, or model-dispatch mechanism from this adapter file.

## Unsupported Claude-only translations

This adapter file intentionally does not translate Claude runtime mechanics:

- Claude slash-command invocation for this skill is unsupported in Codex.
- Claude skill frontmatter and model tier routing are not Codex packaging.
- Claude subagent dispatch and prompt-cache preamble behavior are not Codex
  requirements.
- Claude session-log and cost-capture plumbing are not implemented for Codex.
- Claude installer routing is not reused for Codex.

See `quoin/adapters/codex/unsupported-claude-behavior.md` for the shared
unsupported-behavior contract.
