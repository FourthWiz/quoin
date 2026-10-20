---
name: pr
description: "Creates a pull request: optional version bump, push, gh pr create, wait for merge. Use for: /pr, 'create a PR', 'open a pull request'."
model: sonnet
---

# PR

*Portable intent doc: `quoin/core/skills/pr.md`*

Adapter override: the active Claude adapter SKILL.md for this skill lives at
`quoin/adapters/claude/skills/pr/SKILL.md`. The runtime-neutral intent doc
lives at `quoin/core/skills/pr.md`.

`bash quoin/install.sh` deploys the adapter file (not this stub) to
`~/.claude/skills/pr/SKILL.md`.

Do NOT add behavior here. Edit the adapter file. This stub remains only
so that `quoin/skills/*/SKILL.md` glob-based tests and the manifest
frontmatter parser continue to find a valid SKILL.md at this path.
