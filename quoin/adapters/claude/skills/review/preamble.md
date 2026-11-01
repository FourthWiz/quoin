---
generated_by: build_preambles.py
kind: full
path: __QUOIN_HOME__/skills/review/preamble.md
source_files:
- quoin/memory/format-kit.md
- quoin/memory/glossary.md
source_hashes:
  quoin/memory/format-kit.md: eef314636e164a15271dde9c2682dab45a12da2a
  quoin/memory/glossary.md: 1dc852b1f090c7cd2c75fb60b468221d6415de17
total_bytes: 4385
---

[format-kit-§3-slice]

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


[glossary]
# Glossary — v3 Workflow Abbreviation Whitelist

This file is the abbreviation whitelist for all quoin skills. It extends `terse-rubric.md`'s "never use abbreviations the reader might not expand correctly" rule: entries here are the *approved* abbreviations; the rubric's rule prevents skills from inventing new ones inline.

**Architecture reference:** artifact-format-architecture v3 §5.2.
**Deployed copy:** `__QUOIN_HOME__/memory/glossary.md` — overwritten on re-install.
**Source:** `quoin/memory/glossary.md` (hand-edited Tier 1; do not apply terse-style compression).

---

## Terms

- term: ack
  expand: acceptance criteria
  rule: use compressed form inside Tasks section bullets; full form on first mention in prose sections

- term: gate
  expand: /gate checkpoint
  rule: full form ("gate checkpoint") when first appearing in a section; compressed ("gate") thereafter

- term: PASS / REVISE / APPROVED / BLOCKED
  rule: VERBATIM — never compressed (downstream skills grep for these exact strings to extract verdicts; any substitution breaks grep-based state tracking)

- term: cfg, w/, b/c
  rule: OK per terse-rubric.md — common enough to be unambiguous; no glossary entry required beyond this acknowledgment

- term: ✓
  expand: done / completed
  rule: VERBATIM — status glyph for completed sequenced items per format-kit §1; downstream skills grep for it to extract task state; do not substitute ☑ or similar

- term: ✗
  expand: failed
  rule: VERBATIM — status glyph for failed items; downstream skills grep for it; do not substitute ✘ or similar

- term: ⏳
  expand: pending / in-progress
  rule: VERBATIM — status glyph for not-yet-started or in-flight items; downstream skills grep for it; do not substitute 🔄 or similar

- term: 🚫
  expand: blocked
  rule: VERBATIM — status glyph for blocked items (waiting on external dependency or explicit blocker); downstream skills grep for it; do not substitute ⛔ or similar

- term: T-NN / D-NN / R-NN / F-NN / Q-NN / S-NN
  expand: file-local cross-reference IDs (Tasks, Decisions, Risks, Findings, Questions, Stages)
  rule: file-local namespace — references resolve only within the same artifact. Cross-artifact references are exempt from V-05 when wrapped in inline-code backticks (e.g. writing `T-05` to cite a sibling plan's task). Note the exemption is content-blind — any ID inside backticks is exempt, including a genuinely-undefined one, so writers own the correctness of backticked refs. A bare cross-artifact token (e.g. T-05 without backticks) still fails V-05. Plain English (e.g., "the parent Stage 3 smoke task") remains acceptable too. The validate_artifact.py V-05 invariant flags any bare reference without a local definition.

---

The four status glyphs (✓ ✗ ⏳ 🚫) are load-bearing for Class A and Class B "sequenced items with status" sections (per format-kit §1). Glossary entries ensure writers don't invent variant glyph semantics inline. Downstream skills (`/critic`, `/implement`, `/review`) may grep these glyphs to extract task state — VERBATIM-keep is required across all artifacts that use sequenced-item sections.
