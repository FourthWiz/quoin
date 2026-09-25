---
name: cleanup
description: "Trash-moves stale sentinels and old checkpoints into a recoverable archive (.workflow_artifacts/memory/trash/). Use for: /cleanup [--dry-run]; auto-fires from /checkpoint unless --no-cleanup. Recovery is manual mv — NOT /sleep --restore."
model: sonnet
---

# Cleanup (deprecated stub)

> **DEPRECATED LOCATION.** The active Claude adapter SKILL.md for this
> skill lives at `quoin/adapters/claude/skills/cleanup/SKILL.md`
> (Phase 22 of the runtime-portability migration). The runtime-neutral
> intent doc lives at `quoin/core/skills/cleanup.md`.
>
> `bash quoin/install.sh` deploys the adapter file (not this stub) to
> `~/.claude/skills/cleanup/SKILL.md`.
>
> Do NOT add behavior here. Edit the adapter file. This stub remains only
> so that `quoin/skills/*/SKILL.md` glob-based tests and the manifest
> frontmatter parser continue to find a valid SKILL.md at this path.
