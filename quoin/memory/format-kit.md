# Format Kit — v3 Artifact Writing Reference

This file is the v3 content-type-aware writing reference for all quoin skills. It is a Tier 1 hand-edited English file — never apply terse-style compression to it (doing so would break the format-picking guidance it encodes).

**Relationship to `terse-rubric.md`:** The rubric governs prose discipline *inside* prose sections. The format kit picks *which* sections are prose vs. structured. Both files work together: pick a primitive here, then apply the rubric inside prose-shaped primitives. References deployed copy at `__QUOIN_HOME__/memory/terse-rubric.md`.

**Architecture reference:** artifact-format-architecture v3 §5.1 (see `.workflow_artifacts/artifact-format-architecture/architecture.md`).

---

## §1 Primitives (the toolbox)

Seven primitives are available. Skills MUST pick the primitive that fits the section's content type. Skills MUST NOT use prose where a structured primitive fits.

### The seven primitives

**1. YAML** — key/value pairs or flat status snapshots.

Example:
```yaml
status: in_progress
stage: implement task 4 of 7
model: opus
```

**2. Markdown table** — three or more items with parallel attributes.

Example:
```markdown
| ID | Risk | Likelihood | Impact |
|----|------|-----------|--------|
| R-01 | DB migration fails | Medium | High |
```

**3. Terse numbered list** — a sequence with state per item, or ordered steps. Use status glyphs for per-item state.

Example:
```
1. ✓ T-01: Create format-kit.md
2. ⏳ T-02: Create glossary.md
3. pending T-03: Extend install.sh
```

**4. Pseudo-code** — multi-step procedural logic, especially when ≥4 steps with conditionals.

Example:
```
if artifact_type == "current-plan":
  require_section("## For human")
  require_section("## State")
else:
  use_default_section_set()
```

**5. Caveman prose** — rationale, narrative, "why" explanations. Apply `terse-rubric.md` rules inside this primitive. Only use when content is genuinely narrative-shaped (cannot be tabulated or listed).

**6. XML tag** — when a downstream skill MUST extract a single field programmatically. Wrap the value:
```
<verdict>PASS</verdict>
```
Do not use XML tags for decorative structure; use only when machine-extraction is the goal.

**7. ID-based cross-references** — stable references across sections and artifacts. Use namespace prefixes:
- `D-NN` — decisions
- `T-NN` — tasks
- `R-NN` — risks
- `F-NN` — findings
- `Q-NN` — open questions
- `S-NN` — stages

**ID namespaces are file-local.** Definitions and references resolve within a single artifact only. To refer to a task or risk defined in a sibling artifact (e.g., a critic-response referring to a plan task, or a Stage N plan referring to a parent Stage N-1 task), you have two options: (a) use plain English ("the parent Stage 3 smoke task" or "the round-1 critic's CRIT-1 issue"), OR (b) wrap the ID in inline-code backticks (e.g. `` `T-05` `` to cite a sibling plan's task). The backtick form is exempt from V-05 — the validator treats any [DTRFQS]-NN token inside a code span as a cross-artifact reference and skips the local-definition check for it. Important: this exemption is content-blind — any ID inside backticks is exempt regardless of whether it is genuinely defined elsewhere, so writers own the correctness of backticked refs. A bare token (T-05 without backticks) is NOT exempt and will be flagged by V-05 if it has no local definition.

### Pick rules (seven)

```
- If content is key/value pairs OR a flat status snapshot → YAML
- If content is ≥3 items with parallel attributes → markdown table
- If content is a sequence with state per item → terse numbered list with status glyph
- If content is multi-step procedural logic → pseudo-code (preferred when ≥4 steps with conditionals)
  OR terse numbered list (≤4 linear steps with no branching)
- If content is rationale/narrative/why → caveman prose per terse-rubric.md
- If a downstream skill MUST extract a single field → wrap in XML tag <field-name>...</field-name>
- For cross-references → use stable IDs (D-NN, T-NN, R-NN, F-NN, Q-NN, S-NN)
```

### Status glyphs

The four status glyphs are load-bearing for sequenced-items sections. Downstream skills (`/critic`, `/implement`, `/review`) may grep them to extract task state. Use verbatim — do not substitute variants:

| Glyph | Meaning | VERBATIM-keep |
|-------|---------|---------------|
| ✓ | done / completed | yes |
| ✗ | failed | yes |
| ⏳ | pending / in-progress | yes |
| 🚫 | blocked (waiting on external dependency) | yes |

---

## §2 Standard sections per artifact type

For each artifact type, the sections below list: required vs. optional status, the format primitive, and a one-line description. The `class` field (A or B) determines whether a `## For human` Haiku summary is required.

Machine-readable sidecar: `quoin/memory/format-kit.sections.json` (consumed by `validate_artifact.py` invariants V-02 and V-07).

### `current-plan.md` (Class B)

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| `## For human` | REQUIRED (first after frontmatter) | Caveman prose, 5–8 lines | Haiku-generated summary: status / risk / next action / what comes next |
| `## State` | REQUIRED | YAML | Task profile, current stage, model, session UUID |
| `## Tasks` | REQUIRED | Terse numbered list + glyphs + acceptance bullets | All tasks with status glyphs and acceptance criteria |
| `## Decisions` | REQUIRED IF any non-trivial decisions made; else omit | Caveman prose with embedded bulleted consequences | Architectural/scope decisions and their rationale |
| `## Risks` | REQUIRED | Markdown table (ID, Risk, Likelihood, Impact, Mitigation, Rollback) | All identified risks |
| `## Procedures` | OPTIONAL | Pseudo-code | Per-task procedural detail when steps have conditionals |
| `## References` | REQUIRED IF any cross-refs; else omit | Terse list | Cross-references to other artifacts, documents, or external resources |
| `## Notes` | OPTIONAL | Caveman prose | Miscellaneous implementer notes |
| `## Convergence Summary` | OPTIONAL | YAML or terse list | Critic-loop convergence metadata: rounds, verdict, key revisions, remaining concerns. Written by /thorough_plan after convergence; absent on single-pass Small plans. |
| `## Acceptance` | OPTIONAL | Terse numbered list | Task-level acceptance criteria when too detailed for the Tasks section |
| `## Revision history` | OPTIONAL | Terse numbered list or table | Changelog per planning round |

### `architecture.md` (Class B)

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| `## For human` | REQUIRED (first after frontmatter) | Caveman prose, 5–8 lines | Haiku summary: what is being proposed and why |
| `## Context` | REQUIRED | Caveman prose | Problem statement, constraints, business context |
| `## Current state` | REQUIRED | Caveman prose + optional table | How things work today; what's broken or painful |
| `## Proposed architecture` | REQUIRED | Caveman prose + mermaid/ASCII diagram | Target state: components, data flow, API contracts |
| `## Integration analysis` | OPTIONAL | Markdown table or caveman prose | Per-integration-point failure modes and mitigations |
| `## Risk register` | REQUIRED | Markdown table | Identified risks with likelihood, impact, mitigation |
| `## De-risking strategy` | OPTIONAL | Caveman prose or terse numbered list | POC spikes, feature flags, parallel-run strategy |
| `## Stage decomposition` | REQUIRED | Caveman prose + table | Implementable stages with complexity, dependencies, key risks |
| `## Stage Summary Table` | OPTIONAL | Markdown table | Compact stage overview (stage, description, complexity, deps, key risk) |
| `## Next Steps` | OPTIONAL | Terse numbered list | Explicit call to which stage is ready for `/thorough_plan` |
| `## Open questions` | OPTIONAL | Terse numbered list | Unresolved questions and their owner |
| `## Appendix` | OPTIONAL | Any primitive | Supplementary material referenced from main sections |
| `## Revision history` | OPTIONAL | Terse list | Changelog per architecture revision |

### `review-N.md` (Class B)

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| `## For human` | REQUIRED (first after frontmatter) | Caveman prose, 5–8 lines | Haiku summary: verdict, top risk, what must change |
| `## Summary` | REQUIRED | Caveman prose, 2–3 sentences | Overall review outcome |
| `## Verdict` | REQUIRED | XML tag `<verdict>APPROVED\|CHANGES_REQUESTED\|BLOCKED</verdict>` | Machine-extractable verdict |
| `## Plan Compliance` | REQUIRED | Caveman prose | How well implementation matches the plan |
| `## Spec Compliance` | OPTIONAL | Caveman prose | How well the implementation satisfies the task spec's acceptance criteria (grandfathered "No spec — verified against plan only" when absent) |
| `## Issues Found` | REQUIRED | Terse numbered list with severity tags | Critical, major, minor issues with location and fix |
| `## Integration Safety` | REQUIRED | Caveman prose | Assessment of integration risks |
| `## Test Coverage` | REQUIRED | Caveman prose | Adequacy of tests for new code |
| `## Risk Assessment` | REQUIRED | Caveman prose or table | Deployment risks, blast radius, rollback |
| `## Recommendations` | OPTIONAL | Terse numbered list | What to do next |
| `## Dimension Verdicts` | OPTIONAL | table (dimension/verdict/top-issue) | present only on merged multi-dimension reviews |

### `security-review-N.md` (Class B)

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| `## For human` | REQUIRED (first after frontmatter) | Caveman prose, 5–8 lines | Haiku summary: verdict, top finding, what must change |
| `## Summary` | REQUIRED | Caveman prose, 2–3 sentences | Overall security-pass outcome |
| `## Verdict` | REQUIRED | XML tag `<verdict>APPROVED\|CHANGES_REQUESTED\|BLOCKED</verdict>` | Machine-extractable verdict; same enum as review-N.md's Verdict primitive |
| `## Findings` | REQUIRED | Terse numbered list with severity tags | Critical, major, minor findings with location and fix, grouped by OWASP category |
| `## Risk Assessment` | REQUIRED | Caveman prose or table | Deployment risks, blast radius, rollback |
| `## Recommendations` | OPTIONAL | Terse numbered list | What to do next |
| `## Scope` | OPTIONAL | Caveman prose, 1 line | Branch name and diff basis used |

### `critic-response-N.md` (Class A)

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| `## Target` | OPTIONAL | Caveman prose, 1 line | What artifact was reviewed |
| `## Verdict` | REQUIRED | XML tag `<verdict>PASS\|REVISE</verdict>` or heading-line verdict | Machine-extractable verdict |
| `## Summary` | REQUIRED | Caveman prose, 2–3 sentences | Overview of plan quality and main concerns |
| `## Issues` | REQUIRED | Terse numbered list grouped by severity (Critical, Major, Minor) | Issue title, what, why it matters, suggestion. Each CRITICAL/MAJOR entry should include a `- Class:` sub-bullet (last, after `Suggestion:`) with value from: `enumeration\|regex-breadth\|audit-method\|integration\|risk-coverage\|testability\|implementability\|structural-fallback\|other\|unknown`. |
| `## What's good` | REQUIRED | Terse numbered list | Aspects the reviser should preserve |
| `## Scorecard` | REQUIRED | Markdown table | Per-criterion score (good/fair/poor) with brief notes |

### `session/<date>-<task>.md` (Class A)

Shape, field rules and merge rule: `session-schema.md`.

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| `## Status` | REQUIRED | Single word or short phrase | `in_progress`, `completed`, `blocked` |
| `## Current stage` | REQUIRED | Caveman prose, 1 line | Which skill/phase is active |
| `## Completed in this session` | REQUIRED | Terse numbered list | Tasks/rounds finished with commit hashes if applicable |
| `## Unfinished work` | REQUIRED | Terse numbered list | What remains, with exact resume point |
| `## Next command` | OPTIONAL | One line | The command to type next; required by session-schema.md |
| `## Cost` | REQUIRED | Fixed `- key: value` lines per session-schema.md | Session UUID, phase, recorded-in-ledger flag, end_of_day_due, fallback_fires, verification fields |
| `## Decisions made` | OPTIONAL | Terse numbered list | Non-obvious choices and rationale |
| `## Open questions` | OPTIONAL | Terse numbered list | Questions that need answers before resuming |

### `gate-<phase>-<date>.md` (Class A)

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| `## Automated checks` | REQUIRED | Terse numbered list with glyphs | Per-check pass/fail with brief detail |
| `## Verdict` | REQUIRED | Single word `PASS` or `FAIL` or XML tag | Gate verdict |
| `## Failures requiring attention` | OPTIONAL | Terse numbered list | Blocking failures with remediation |
| `## Warnings (non-blocking)` | OPTIONAL | Terse numbered list | Non-blocking issues to be aware of |
| `## Summary of what was produced` | OPTIONAL | Caveman prose | What artifacts the preceding phase created |
| `## What's next` | OPTIONAL | Caveman prose, 1–2 lines | Next step in the workflow |

---

## §3 Pick rules for ambiguous content (the hard cases)

Three rules for situations where the primitive choice is non-obvious:

1. **Mixed content in one logical section** — split into multiple sections, each homogeneous. Do not use prose to narrate over a list that should be a list. Example: if a "State" section has both a YAML snapshot and a prose rationale, put the YAML in `## State` and the rationale in `## Decisions`.

2. **A "decision with a list of consequences"** — Decision entry uses caveman prose body with an embedded bulleted consequence list. The consequence list is part of the decision body, not its own section. Example:
   ```
   ## Decisions
   Chose lazy-import for `anthropic` SDK (D-03).
   Consequences:
   - argparse and --help paths work without SDK installed
   - smoke tests are CI-safe without credentials
   - T-07 acceptance criterion: `grep '^import anthropic'` returns no match at module scope
   ```

3. **A risk with a long mitigation procedure** — the risk table row references a Procedure entry (`see proc:R-02`) that lives in the Procedures section. Do not embed multi-step procedures inside a table cell. Keep the table cell to one line.

---

## §4 Default minimal section set

For any Class A artifact NOT in §2's enumerated list, the default is:

| Section | Status | Primitive | Description |
|---------|--------|-----------|-------------|
| frontmatter | OPTIONAL | YAML | path, hash, updated, updated_by, tokens |
| main content section | REQUIRED | Any primitive appropriate to content | The artifact's primary content |
| `## Notes` | OPTIONAL | Caveman prose or terse list | Additional context |

Class B artifacts that do not appear in §2 should be treated as Class A until a Stage 3+ update adds them to the enumerated list.

---

## §5 Composition with terse-rubric.md

**The composition rule (from architecture §5.1):** The format kit picks which sections are prose vs. structured. The rubric applies *inside* prose-shaped sections. They do not conflict — they operate at different levels.

Concretely:
- Format kit says: "this section is caveman prose."
- Rubric then governs that section's prose: no filler phrases, no hedging, short sentences, tense consistency.
- Format kit says: "this section is a markdown table."
- Rubric does not apply to table cells — structured primitives have their own discipline (column alignment, separator row, bounded cell content).

Reference the deployed copy of the rubric at `__QUOIN_HOME__/memory/terse-rubric.md` when writing prose-shaped sections. The terse-rubric.md and format-kit.md are co-required: reading one without the other gives an incomplete picture of what correct artifact writing looks like.
