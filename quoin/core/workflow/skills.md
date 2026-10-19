# Skill Metadata

`skills.json` is the runtime-neutral skill catalog for Quoin.

It does not drive runtime behavior yet. Existing Claude skill frontmatter remains the active runtime source until a generator or adapter loader is introduced.

## Ownership

The portable core owns these fields:

- `schema_version` — version of the manifest shape.
- `skills` — list of skill metadata entries.
- `name` — stable skill identifier, matching the directory under `quoin/skills/`.
- `phase` — runtime-neutral workflow phase or responsibility.
- `effort` — runtime-neutral effort level from `quoin/docs/effort-levels.md`.
- `user_facing` — whether users should invoke or route to the skill directly.
- `claude_model` — compatibility metadata copied from current Claude `model:` frontmatter.

`effort` is the portable field. Runtime adapters should map effort to their own model or reasoning controls.

`claude_model` exists to keep the manifest auditable against current Claude behavior. It should not be used by non-Claude adapters.

## Current Constraints

- Every `quoin/skills/*/SKILL.md` file must have exactly one manifest entry.
- `claude_model` must match current skill frontmatter.
- `effort` must be one of `low`, `medium`, `high`, or `max`.
- `revise-fast` remains internal with `user_facing: false`.
- No Codex model names belong in this manifest.

## Future Use

Future adapter work may use this manifest to generate runtime-specific command tables, install manifests, or adapter overlays. Do not generate active Claude files from it until compatibility tests cover the generated output.

## Schema version 2 fields

Phase 22 added two optional boolean fields per skill entry. Both default to `false` when absent (backward-compatible with existing schema-v1 readers).

- `section_0` — whether the Claude adapter `SKILL.md` must carry the `## §0 Model dispatch (FIRST STEP — execute before anything else)` block. Derived from the 12 cheap-tier enumeration in `quoin/CLAUDE.md` (§0 Model dispatch preamble section). Skills with `section_0: true` self-dispatch to their declared model tier when invoked from a more expensive session. When adding a new skill, set `section_0: true` if the skill is cheap-tier (any tier below the strongest model) and must carry the §0 dispatch block.

- `spawn_target` — whether the skill is a subagent-preamble spawn target (one of the 7 skills in `build_preambles.py::SPAWN_TARGETS`). Spawn-target skills must have a `preamble.md` alongside the legacy stub at `quoin/skills/<name>/preamble.md` (the path install.sh copies from). When adding a new skill that is a spawn target, generate and commit the preamble via `build_preambles.py`, then set `spawn_target: true`.
- `claude_adapter` — optional boolean, absent means true. `false` marks a runtime-neutral skill that has no Claude Code adapter or legacy stub (other adapters such as OpenCode and Codex are unaffected and keep reading the entry). The drift validator runs only the core-doc and Codex checks for such a skill and reports AD-AD if a Claude adapter or stub reappears.

Both fields are enforced by the adapter drift validator (see below). New skills must declare both fields explicitly in `skills.json` — omitting them defaults both to `false`, which would cause a validator violation if the skill is actually cheap-tier or a spawn target.

## Adapter drift validator

The adapter drift validator (`validate_adapter_drift.py`) checks 16 structural invariants across the three-file adapter pattern for every skill in the manifest.

**Location:**
- Canonical implementation: `quoin/core/scripts/validate_adapter_drift.py` (Claude-adapter-specific in scope)
- Compatibility wrapper: `quoin/scripts/validate_adapter_drift.py`

**How to run locally:**
```
python3 quoin/core/scripts/validate_adapter_drift.py
```
Exit 0 = PASS, exit 2 = DRIFT detected. Use `--json` for structured output.

**How CI runs it:** `pytest quoin/dev/tests/test_validate_adapter_drift.py`

**Invariants enforced (one per skill):**
- `AD-CO` — `quoin/core/skills/<name>.md` exists (core portable intent doc)
- `AD-AD` — `quoin/adapters/claude/skills/<name>/SKILL.md` exists (Claude adapter doc)
- `AD-LS` — `quoin/skills/<name>/SKILL.md` exists (legacy stub for install.sh)
- `AD-FN` — adapter frontmatter `name:` field equals the skill name
- `AD-FM` — adapter frontmatter `model:` field matches `claude_model` in the manifest
- `AD-PT` — adapter body contains `quoin/core/skills/<name>.md` as a substring (Phase 21 substring contract; relaxed from exact pointer-line for revise-fast)
- `AD-FB` — legacy stub frontmatter byte-equals adapter frontmatter (after CRLF normalization)
- `AD-SS` — legacy stub is strictly shorter than adapter (sanity check the stub is really a stub)
- `AD-S0P` — for `section_0: true` skills, adapter matches `^## §0 Model dispatch \(FIRST STEP` (line-anchored regex)
- `AD-S0A` — for `section_0: false` skills, adapter does NOT match the same regex
- `AD-PE` — for `spawn_target: true` skills, `quoin/skills/<name>/preamble.md` exists
- `AD-PA` — for `spawn_target: true` skills, `quoin/adapters/claude/skills/<name>/preamble.md` does NOT exist (no confusing duplicate)
- `AD-PX` — for `spawn_target: false` skills, neither preamble path exists
- `AD-IV` — `install.sh` contains `ADAPTER_<NAME_UPPER>_SRC=` preflight assignment
- `AD-IE` — `install.sh` contains an `if` or `elif` branch matching `$skill_name = "<name>"` (capture_insight uses leading `if` as the Phase 6 pilot; all others use `elif`)
- `AD-IO` — `ADAPTER_<NAME_UPPER>_SRC=` preflight appears before the `for skill_dir in "$SCRIPT_DIR/skills"/*/` loop in install.sh

For newer installer-wrapper layouts, AD-IV/AD-IE/AD-IO are satisfied when
`quoin/install.sh` delegates to `python -m quoin install --source-dir ...` and
`src/quoin/installer.py` owns the adapter-precedence rule:
`adapters/claude/skills/<name>/SKILL.md` is copied when present, otherwise the
legacy `skills/<name>/SKILL.md` stub is used. This preserves Claude install
behavior without keeping duplicate per-skill shell branches in the wrapper.

**Runtime boundary note:** despite living in `core/scripts/` for wrapper-pattern symmetry, the validator is Claude-adapter-specific in scope. It checks `## §0 Model dispatch` headings, `adapters/claude/` paths, and install.sh routing — all Claude-specific concepts. A future Codex adapter drift validator would be a parallel script (`validate_adapter_drift_codex.py`), not a generalization of this one.

**Pilot-test coexistence:** 17 existing `test_*_adapter_pilot.py` files remain as point-in-time regression guards from individual phase migrations. The adapter drift validator is the forward-looking guard for all 28 skills and any future additions. Both coexist; do not delete the pilot tests.
