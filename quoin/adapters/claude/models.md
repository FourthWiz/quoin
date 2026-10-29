# Claude Model Mapping

The Claude adapter currently uses Claude Code skill frontmatter as the active runtime source of model behavior.

`quoin/core/workflow/skills.json` mirrors that behavior through `claude_model` for auditability. It does not replace skill frontmatter yet.

## Mapping

| Claude model tier | Portable effort |
| --- | --- |
| `haiku` | `low` |
| `sonnet` | `medium` |
| `opus` | `high` or `max` |

Use `high` for ordinary Opus planning, architecture, critic, and review work. Use `max` for strict, large, or end-to-end orchestration work that should use the strongest available reasoning.

## Compatibility Rule

Until Quoin has a generator or adapter loader, the `model:` field in each `quoin/adapters/claude/skills/*/SKILL.md` file remains the source of Claude runtime behavior.

Any change to `skills.json` must keep `claude_model` aligned with that frontmatter.
