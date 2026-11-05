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
  rule: references resolve within the same artifact plus its sibling artifacts in the same task folder (task root included for stage-N folders). Backtick-wrapped IDs (e.g. `T-05`) are exempt from V-05 and content-blind, so writers own their correctness. Qualified IDs (plan.md#T-03, "see architecture D-02") and IDs only introduced mid-sentence warn instead of failing. A bare ID defined nowhere in the artifact or its task folder fails V-05. Plain English (e.g., "the parent Stage 3 smoke task") remains acceptable too.

---

The four status glyphs (✓ ✗ ⏳ 🚫) are load-bearing for Class A and Class B "sequenced items with status" sections (per format-kit §1). Glossary entries ensure writers don't invent variant glyph semantics inline. Downstream skills (`/critic`, `/implement`, `/review`) may grep these glyphs to extract task state — VERBATIM-keep is required across all artifacts that use sequenced-item sections.
