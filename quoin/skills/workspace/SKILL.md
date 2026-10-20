---
name: workspace
description: "Manages per-repo git-worktree workspaces: create, status, takeover, teardown. Use for: /workspace, 'create a workspace', 'new worktree', 'tear down this workspace'."
model: sonnet
---

# Workspace

*Portable intent doc: `quoin/core/skills/workspace.md`*

Adapter override: the active Claude adapter SKILL.md for this skill lives at
`quoin/adapters/claude/skills/workspace/SKILL.md`. The runtime-neutral intent doc
lives at `quoin/core/skills/workspace.md`.

`bash quoin/install.sh` deploys the adapter file (not this stub) to
`~/.claude/skills/workspace/SKILL.md`.

Do NOT add behavior here. Edit the adapter file. This stub remains only
so that `quoin/skills/*/SKILL.md` glob-based tests and the manifest
frontmatter parser continue to find a valid SKILL.md at this path.
