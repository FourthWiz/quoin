# Memory Maintenance Reference

This document describes how quoin manages the auto-memory fact-file lifecycle:
when to archive vs. soft-forget vs. delete a fact-file, and how the pattern config
file controls which files are protected from automated cleanup.

## Memory layers

The quoin memory system uses several distinct layers. See the **Workflow memory layers**
table in `${CLAUDE_PLUGIN_ROOT}/memory/workflow-catalog.md` (§ "Workflow memory layers") for the
full table and lifecycle descriptions. This document focuses on the fact-file layer
(auto-memory) and
the insights/lessons layer managed by `/sleep`.

## When to archive vs. soft-forget vs. delete

| Action | When to use | Mechanism |
|--------|-------------|-----------|
| **Archive** | File is obsolete but you want to keep a record; a link in MEMORY.md should remain valid but not raise errors | Add glob to `archived:` in `memory-maintenance.yaml` |
| **Soft-forget** | Insights entry is no longer relevant; recoverable via `/sleep --restore` | Let `/sleep` route it to the `forget` bucket, confirm when prompted |
| **Delete** | File is dangerously outdated and should not be restored | Delete manually; remove the MEMORY.md link if present |
| **Read-only** | File is managed externally or must not be orphan-checked (e.g. feedback from tools) | Add glob to `read_only:` in `memory-maintenance.yaml` |
| **Ignore** | File is present on disk but should not be tracked at all (e.g. drafts) | Add glob to `ignore:` in `memory-maintenance.yaml` |

## Pattern config: memory-maintenance.yaml

The file `${CLAUDE_PLUGIN_ROOT}/memory/memory-maintenance.yaml` is the single source of truth
for maintenance patterns. Both consumers — `memory_check.py` and `/sleep` — load it
using a stdlib-only YAML line parser; PyYAML is not required.

### Schema

```yaml
version: 1
archived:   # list of fnmatch globs
  - "archive_*.md"
read_only:  # list of fnmatch globs
  - "feedback_*.md"
ignore:     # list of fnmatch globs
  - "*.draft.md"
```

Globs are matched against **bare file names** only (no directory path).
Matching is case-sensitive via `fnmatch`. A missing or unreadable file is advisory
only — the consumers behave as if all lists are empty (fail-OPEN).

### Precedence

`ignore` > `archived` > `read_only` > `active` (first match wins).

### Consumer contracts

**`memory_check.py`** (`check()` with `pattern_file=...`):
- `ignore`: file is removed from the fact-file set entirely — never appears in orphans,
  dangling, or any output.
- `archived`: suppresses **both** orphan errors and dangling errors. An archived
  fact-file that is not referenced by MEMORY.md is not an orphan error. An MEMORY.md
  link whose target is classified archived is not a dangling error. The file appears in
  the `archived` info key of the result dict.
- `read_only`: suppresses **orphan errors only**. The file is integrity-validated
  normally; if a MEMORY.md link to it is missing (dangling), that is still reported.
  The file appears in the `read_only` info key of the result dict.
- Neither `archived` nor `read_only` relaxes the global `FORWARD_LINKS_ARE_ERRORS`
  default. A non-archived missing link target remains a dangling error unless
  `--allow-forward-links` is passed.

**`sleep_score.py`** (via `--patterns`):
- `ignore`: source file is skipped entirely — produces zero entries in the scoring run.
- `archived` / `read_only`: entries from these source files are marked `protected=True`
  internally. If the scorer would assign `bucket="forget"`, the bucket is demoted to
  `"middle"` instead — a protected source is never soft-forgotten. The `/sleep` SKILL.md
  NDJSON parser is unchanged because `protected` is omitted from the serialized dict;
  the bucket value already reflects the demotion.

### Updating the pattern file

Edit `quoin/quoin/memory/memory-maintenance.yaml` (the source), then re-run
`bash quoin/install.sh` to deploy the updated copy to `${CLAUDE_PLUGIN_ROOT}/memory/`.
The deploy step runs `${CLAUDE_PLUGIN_ROOT}` substitution on both the `.md` and `.yaml` files
and validates that no raw placeholder tokens survive in the deployed copies.

## Forward-link policy

The `FORWARD_LINKS_ARE_ERRORS = True` constant in `memory_check.py` controls whether
links to not-yet-existing files are treated as errors. Pattern classifications are
orthogonal to this flag:

- `--allow-forward-links` relaxes all dangling errors globally for that run.
- `archived` classification relaxes dangling errors for specific files, regardless of
  the `--allow-forward-links` flag.
- `read_only` classification does **not** relax dangling errors — only orphan errors.

This distinction lets you mark "this link target was intentionally removed" (archived)
separately from "this file is externally managed and should not be an orphan"
(read_only) without weakening the general forward-link policy.
