# Knowledge Cache Guide

The cache lives under `.workflow_artifacts/cache/`. Three rules govern all skills:
- **(a) Cache is advisory, not authoritative** — a missing or stale cache entry is never an error; skills may always read source files directly.
- **(b) Any skill that modifies source files MUST update the corresponding cache entry** (enforced per-skill in each skill's inline "Cache write-through" section). The `.workflow_artifacts/cache/` directory is the host.
- **(c) Rollback by deletion** — deleting `.workflow_artifacts/cache/` fully restores pre-cache behavior; no skill should fail if the cache directory is absent.

## Cache directory structure

````
.workflow_artifacts/cache/
├── _index.md                     ← Root index: repo list, last-updated timestamps
├── _staleness.md                 ← Git HEAD tracking per repo (replaces repo-heads.md)
├── <repo-name>/
│   ├── _index.md                 ← Repo summary: purpose, stack, entry points, key patterns
│   ├── _deps.md                  ← External deps + internal cross-module deps
│   ├── repo-map.md               ← Generated ranked symbol map (repo_map.py; refreshed by /discover, not write-through)
│   └── <directory>/
│       ├── _index.md             ← Module/directory summary: purpose, exports, patterns
│       └── <file-stem>.md        ← Per-file summary (key files only)
````

## Cache entry format

Every `_index.md` and `<file-stem>.md` uses this structure:

````markdown
---
path: <relative path from project root to source file/directory>
hash: <git hash of file at time of caching, or HEAD for directories>
updated: <ISO timestamp>
updated_by: <skill that wrote/updated this entry>
tokens: <approximate token count of this cache entry>
---

## Purpose
<1-2 sentences>

## Key Exports
- `name(params)` — description

## Dependencies
- imports from: <internal modules>
- external: <key packages>

## Patterns
- <notable patterns>

## Integration Points
- exposes: <APIs, events, exports>
- consumes: <APIs, events, imports>

## Notes
<gotchas, tech debt, non-obvious details>
````

Sections may be omitted when not applicable (e.g., a config file has no Key Exports).

## Staleness tracking

`_staleness.md` stores repo-level HEAD; skills read it to decide which cache entries are still valid; successor to `repo-heads.md` (fall back if absent).

## Per-skill patterns

Cache-read bootstrap and cache write-through patterns live inline in each skill's SKILL.md. Subagents read their own SKILL.md at startup, not this file — see lessons-learned 2026-04-13. Do not replace inline copies with a pointer.
