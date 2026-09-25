#!/usr/bin/env python3
"""
inject_pollution_dispatch.py — Generator for §0' Pollution dispatch AND §0″ Minimum-tier guard.

Inserts/refreshes the §0' Pollution dispatch block (and §0c Pidfile lifecycle for
architect + review) AND the §0″ Minimum-tier guard block into the 10 Opus-tier
Claude adapter SKILL.md files at:
  quoin/adapters/claude/skills/<skill>/SKILL.md

This script is the SOURCE-OF-TRUTH owner for both §0' and §0″ content.
§0' was dropped during the Phase-10 adapter migration (commit e732677) and
restored here in a generator-owned form. §0″ was added in IVG-72.

Note: This is a STANDALONE script (stdlib-only on the write path). It does NOT
import from quoin/core/scripts/. Registering it in DEPLOYED_SCRIPTS only is
correct. If this script ever imports a core impl, it becomes a wrapper and MUST
also be added to CORE_SCRIPTS and register in sys.modules before exec (lessons
2026-05-31 / 2026-06-08).

Exit codes:
  0  success
  1  write error (file not found or could not write)
  6  --dry-run and --check are mutually exclusive
  7  --check: drift detected (one or more adapter files missing §0' or §0″, or token mismatch)
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

# ─── Constants (byte-identical to test file literals) ────────────────────────

POLLUTION_HEADING = "## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)"
ZC_HEADING = "## §0c Pidfile lifecycle"

# HEADING BYTE-IDENTITY: the ″ (U+2033 DOUBLE PRIME) must match exactly across
# template, --check regex, and drift test. Copy-paste from here; do not retype.
MINTIER_HEADING = "## §0″ Minimum-tier guard (execute after §0 / §0c / §0’ if present — before skill body)"

# 10 Opus-tier target skills — must carry §0'.
POLLUTION_TARGET_SKILLS = [
    "architect",
    "plan",
    "critic",
    "revise",
    "review",
    "init_workflow",
    "discover",
    "specify",
    "security_review",
    "enrich",
]

# 10 Opus-tier target skills — must carry §0″ (same set as POLLUTION_TARGET_SKILLS).
# Orchestrators /run and /thorough_plan are deliberately excluded (D-04):
# orchestrators carry no §0/§0'/§0c today and route to correctly-tiered children.
MINTIER_TARGET_SKILLS = [
    "architect",
    "plan",
    "critic",
    "revise",
    "review",
    "init_workflow",
    "discover",
    "specify",
    "security_review",
    "enrich",
]

# Skills that also need §0c Pidfile lifecycle (inserted BEFORE §0').
ZC_SKILLS = ["architect", "review"]

# ─── §0‴ Minimum-tier guard (Sonnet tier) — IVG-117 ───────────────────────────
# Anchor heading for the 20 cheap-tier §0 skills (hand-authored, always present).
# SECTION0_HEADING is verified count==1 across the 13 Sonnet §0‴ target files.
SECTION0_HEADING = "## §0 Model dispatch (FIRST STEP — execute before anything else)"

# 13 Sonnet-declared cheap-tier skills that lack an under-tier guard (IVG-117 Gap 1).
# 7 Haiku-declared skills are structurally exempt (bottom tier — nothing cheaper to
# guard against). Orchestrators /run and /thorough_plan are excluded per the
# orchestrator-exclusion rule (mirrors MINTIER_TARGET_SKILLS D-04). Roster mirrors
# skills.json claude_model=="sonnet" && section_0==true.
MINTIER_SONNET_TARGET_SKILLS = [
    "checkpoint",
    "cleanup",
    "continue_work",
    "end_of_day",
    "end_of_task",
    "expand",
    "gate",
    "implement",
    "pr",
    "revise-fast",
    "rollback",
    "sleep",
    "workspace",
]

# HEADING BYTE-IDENTITY: the ‴ (U+2034 TRIPLE PRIME) must match exactly across
# template, --check regex, and drift test. Copy-paste from here; do not retype.
MINTIER_SONNET_HEADING = "## §0‴ Minimum-tier guard (execute after §0 — before any §0-sidecar block and the skill body)"

# ─── §0″ Minimum-tier guard block template ────────────────────────────────────
# Spike result 2026-06-23: up-dispatch adopted as happy path (Option A) per IVG-91.
# The IVG-72 implement session could not confirm up-dispatch (ran on Sonnet without
# live API access). IVG-91 activates Option A: Agent spawn to Opus is the happy path;
# AskUserQuestion (abort / proceed-under-powered) is the fail-open fallback only.
#
# Note: QUOIN_DISABLE_MINTIER_GUARD silent-skip is intentional (explicit opt-out,
# not a bug). No advisory is emitted — emitting a line would be noise for an
# intentional bypass.

# The template body does NOT include the heading line — render_mintier_block prepends
# MINTIER_HEADING to guarantee byte-identity (heading in block == MINTIER_HEADING constant).
# This avoids U+2033 vs other quote character mismatches when the heading is typed inline.
#
# Required tokens in this block (Option A — all must remain verbatim):
#   [no-redispatch]  — in sentinel detection line AND child dispatch prompt prefix
#   model: "opus"    — in declared tier descriptor AND subagent dispatch model field
#   current_tier < declared_tier  — in fire condition line
#   spawn an Agent subagent  — unique discriminator: absent from Option-B body
#   Wait for the subagent. Return its output as your final response. STOP.  — unique discriminator
#   Usage credits required for 1M context  — 1M-credit-class catch branch
#   Abort — run from an Opus session  — generic fallback AskUserQuestion Option 1 label
#   Proceed at current tier (under-powered)  — generic fallback AskUserQuestion Option 2 label
#   [quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]
#     — generic fallback Option 2 advisory line
_MINTIER_BLOCK_BODY = """\

This skill is declared model: "opus". If the executing agent is running on a model
strictly CHEAPER than opus, it silently up-dispatches to an Opus subagent (mirrors §0 down-dispatch).

<!-- §0doubleprime-begin -->
Detection:
  - Read your current model from system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus. declared_tier = opus.
  - Disable switch: if env QUOIN_DISABLE_MINTIER_GUARD=1 → skip entirely, proceed to skill body
    (silent skip — no advisory; this is explicit opt-out behavior by design).
  - Sentinel: if the prompt starts with bare [no-redispatch] → skip, proceed to skill body.
  - Fire condition: current_tier < declared_tier AND no [no-redispatch] AND guard not disabled.

On fire (happy path — silent up-dispatch):
  spawn an Agent subagent:
    model: "opus"
    description: "{skill} — min-tier up-dispatch"
    prompt: "[no-redispatch]\\n<original user input verbatim>"
  Wait for the subagent. Return its output as your final response. STOP.

Fail-OPEN path (fires only when Agent dispatch fails). Full AskUserQuestion Question/Header/
description wording for every branch below: memory/dispatch-guide.md §0″ verbose reference
("Verbatim AskUserQuestion wording"). Classify the error text BEFORE proceeding:

  - Autonomous-class (checked FIRST, before 1M-credit or generic classification): if the
    incoming prompt carries the `[autonomous]` sentinel, then on ANY §0″ dispatch-failure or
    1M-context-credit error, proceed at current tier fail-OPEN and DO NOT call `AskUserQuestion`
    — skip the 1M-credit-class and generic branches below entirely. Print
    `[quoin-mintier-autonomous: §0″ dispatch failed; proceeding fail-OPEN at current tier]` and
    proceed to skill body (treat as bare [no-redispatch]).

  - 1M-credit-class: if error text contains `Usage credits required for 1M context`:
      Issue AskUserQuestion (full Question/Header wording: memory/dispatch-guide.md
      §0″ verbose reference):
        Option 1:
          label: "Abort — I'll switch with /model first"
        Option 2:
          label: "Proceed in-session at parent tier"
      On Option 1: print `[quoin-mintier: 1M-context credit mismatch; abort per user choice —
      switch with /model and re-invoke /{skill}]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on opus up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from an Opus session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /{skill} from an Opus session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0doubleprime-end -->

"""


def render_mintier_block(skill: str) -> str:
    """Render the §0″ Minimum-tier guard block for a given skill.

    Prepends MINTIER_HEADING to guarantee byte-identity between the block heading
    and the constant used by _replace_existing_block and --check. This avoids
    character encoding mismatches (U+2019 vs U+0027, U+2033 vs similar) when the
    heading is written inline in the template string.
    """
    body = _MINTIER_BLOCK_BODY.replace("{skill}", skill)
    return MINTIER_HEADING + body


# ─── §0‴ Minimum-tier guard (Sonnet tier) block template — IVG-117 ────────────
# Tier-swapped derivative of _MINTIER_BLOCK_BODY (D-07: zero edits to the Opus
# template/constants — separate constants keep the 10 deployed Opus files and
# test_mintier_guard.py byte-frozen). Substitution map applied: "an Opus" -> "a
# Sonnet" (article agreement, MIN-3), "Opus" -> "Sonnet", "opus" -> "sonnet",
# "§0″" -> "§0‴", "doubleprime" -> "tripleprime". declared_tier = sonnet.
# ONE net-new line vs the Opus body: the MIN-2 recursion-contract line inside
# Detection (the template-parity guard in test_sonnet_mintier_guard.py strips
# this line, keyed on the literal substring "[no-redispatch:N]", before
# asserting semantic equality with _MINTIER_BLOCK_BODY).
#
# Required tokens in this block (mirrors Opus Option A discriminators):
#   [no-redispatch]  — sentinel detection line AND child dispatch prompt prefix
#   model: "sonnet"  — declared tier descriptor AND subagent dispatch model field
#   current_tier < declared_tier  — fire condition line (tier-agnostic, verbatim)
#   spawn an Agent subagent  — unique discriminator
#   Wait for the subagent. Return its output as your final response. STOP.
#   Usage credits required for 1M context  — 1M-credit-class catch branch
#   Abort — run from a Sonnet session  — generic fallback AskUserQuestion Option 1 label
#   Proceed at current tier (under-powered)  — generic fallback AskUserQuestion Option 2 label
#   [quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]
#   [no-redispatch:N]  — MIN-2 recursion-contract line
_MINTIER_SONNET_BLOCK_BODY = """\

This skill is declared model: "sonnet". If the executing agent is running on a model
strictly CHEAPER than sonnet, it silently up-dispatches to a Sonnet subagent (mirrors §0 down-dispatch).

<!-- §0tripleprime-begin -->
Detection:
  - Read your current model from system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus. declared_tier = sonnet.
  - Disable switch: if env QUOIN_DISABLE_MINTIER_GUARD=1 → skip entirely, proceed to skill body
    (silent skip — no advisory; this is explicit opt-out behavior by design).
  - Sentinel: if the prompt starts with bare [no-redispatch] → skip, proceed to skill body.
  - Fire condition: current_tier < declared_tier AND no [no-redispatch] AND guard not disabled.
  - Recursion: counter form `[no-redispatch:N]` (N≥2) never reaches this block — §0 (earlier in this file) aborts on N≥2 before any §0‴ tool call.

On fire (happy path — silent up-dispatch):
  spawn an Agent subagent:
    model: "sonnet"
    description: "{skill} — min-tier up-dispatch"
    prompt: "[no-redispatch]\\n<original user input verbatim>"
  Wait for the subagent. Return its output as your final response. STOP.

Fail-OPEN path (fires only when Agent dispatch fails). Full AskUserQuestion Question/Header/
description wording for every branch below: memory/dispatch-guide.md §0‴ verbose reference
("Verbatim AskUserQuestion wording"). Classify the error text BEFORE proceeding:

  - Autonomous-class (checked FIRST, before 1M-credit or generic classification): if the
    incoming prompt carries the `[autonomous]` sentinel, then on ANY §0‴ dispatch-failure or
    1M-context-credit error, proceed at current tier fail-OPEN and DO NOT call `AskUserQuestion`
    — skip the 1M-credit-class and generic branches below entirely. Print
    `[quoin-mintier-autonomous: §0‴ dispatch failed; proceeding fail-OPEN at current tier]` and
    proceed to skill body (treat as bare [no-redispatch]).

  - 1M-credit-class: if error text contains `Usage credits required for 1M context`:
      Issue AskUserQuestion (full Question/Header wording: memory/dispatch-guide.md
      §0‴ verbose reference):
        Option 1:
          label: "Abort — I'll switch with /model first"
        Option 2:
          label: "Proceed in-session at parent tier"
      On Option 1: print `[quoin-mintier: 1M-context credit mismatch; abort per user choice —
      switch with /model and re-invoke /{skill}]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on sonnet up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from a Sonnet session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /{skill} from a Sonnet session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0tripleprime-end -->

"""


def render_mintier_sonnet_block(skill: str) -> str:
    """Render the §0‴ Minimum-tier guard (Sonnet tier) block for a given skill.

    Prepends MINTIER_SONNET_HEADING to guarantee byte-identity between the block
    heading and the constant used by _replace_existing_block and --check (mirrors
    render_mintier_block).
    """
    body = _MINTIER_SONNET_BLOCK_BODY.replace("{skill}", skill)
    return MINTIER_SONNET_HEADING + body


# ─── §0 Model dispatch generator conversion — IVG-165 ─────────────────────────
# D-01: uniform explicit end marker closes every §0 region (fixes start_of_day's
# next-`## `-boundary over-capture by construction; see D-01a/D-01b for the
# marker-anchored region mechanics used by inject_section0_block_into_file below
# — this is deliberately NOT _replace_existing_block, which stops at the next
# `(?=^## )` and would not fix start_of_day's over-capture).
SECTION0_END_MARKER = "<!-- §0-end -->"

# 20-entry roster: the full §0-carrying skill set (mirrors SECTION0_SKILLS in
# test_footprint_ceilings.py / test_section0_marker.py and the CLAUDE.md "§0
# Model dispatch preamble" skill list). Each entry is
#   skill -> (tier, proceed_ref, variant, has_autonomous_clause, extra_comment)
# per the 5 template axes (D-03 — variants are template branches keyed to
# these axes, never per-file text blobs):
#   1. skill name (substituted in prose)
#   2. tier: "haiku" (7) | "sonnet" (13, == MINTIER_SONNET_TARGET_SKILLS)
#   3. proceed_ref: "§1" (17) | "§0c" (3: cleanup, sleep, checkpoint)
#   4. variant: "worktree" (15 — 12 with clause + 3 without) | "sidecar" (5,
#      always with clause, distinct wording per D-02)
#   5. has_autonomous_clause: only meaningful for variant="worktree"
#      (selects between the two Worktree-class-branch sub-templates)
#   6. extra_comment: the `§0b: intentionally omitted` line (pr, workspace)
SECTION0_TARGET_SKILLS: dict[str, tuple[str, str, str, bool, bool]] = {
    "capture_insight": ("haiku", "§1", "worktree", True, False),
    "checkpoint": ("sonnet", "§0c", "worktree", True, False),
    "cleanup": ("sonnet", "§0c", "worktree", False, False),
    "continue_work": ("sonnet", "§1", "worktree", False, False),
    "cost_snapshot": ("haiku", "§1", "worktree", True, False),
    "end_of_day": ("sonnet", "§1", "worktree", True, False),
    "end_of_task": ("sonnet", "§1", "sidecar", True, False),
    "expand": ("sonnet", "§1", "worktree", True, False),
    "gate": ("sonnet", "§1", "worktree", True, False),
    "implement": ("sonnet", "§1", "sidecar", True, False),
    "next_steps": ("haiku", "§1", "worktree", True, False),
    "pr": ("sonnet", "§1", "sidecar", True, True),
    "revise-fast": ("sonnet", "§1", "worktree", True, False),
    "rollback": ("sonnet", "§1", "sidecar", True, False),
    "sleep": ("sonnet", "§0c", "worktree", True, False),
    "start_of_day": ("haiku", "§1", "worktree", True, False),
    "status": ("haiku", "§1", "worktree", False, False),
    "triage": ("haiku", "§1", "worktree", True, False),
    "weekly_review": ("haiku", "§1", "worktree", True, False),
    "workspace": ("sonnet", "§1", "sidecar", True, True),
}

# next_steps' skill dir/dict key differs from its slash-invocation name
# (/next-steps) -- the ONLY skill where the two forms diverge. All other
# skill-name substitution sites (description, error message) use the dict key.
_SECTION0_SLASH_NAME_OVERRIDES = {"next_steps": "next-steps"}

# Base block: heading through the end of "Manual kill switch:" — shared
# byte-for-byte across ALL 20 files (verified: sidecar and worktree families
# diverge only AFTER this point). {tier}/{proc}/{skill}/{slash} are the only
# substitution points in this block (D-03 axes 1-3); every other "proceed to
# §1"/"§0c" occurrence elsewhere in the region is FIXED literal text that does
# NOT vary with proceed_ref (verified against the live corpus — a pre-existing
# corpus quirk reproduced byte-exactly per D-02/D-08, not a bug this generator
# introduces or fixes).
_SECTION0_BASE_BODY = """\

This skill is declared `model: {tier}`. If the executing agent is running on a model
strictly more expensive than the declared tier, you MUST self-dispatch before doing the
skill's actual work.

Detection:
  - Read your current model from the system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus.
  - Sentinel parsing: the user's prompt is checked for the `[no-redispatch]` family.
      * Bare `[no-redispatch]` (parent-emit form AND user manual override): skip dispatch, proceed to {proc} at the current tier.
      * Counter form `[no-redispatch:N]` where N is a positive integer ≥ 2: ABORT (see "Abort rule" below).
      * Counter form `[no-redispatch:1]` is reserved and treated as bare `[no-redispatch]` for forward-compatibility; do not emit it.
  - If current_tier > declared_tier AND prompt does NOT start with any `[no-redispatch]` form:
      Dispatch reason: cost-guardrail handoff. dispatched-tier: {tier}.
<!-- §0-1m-decide-begin -->
Pre-dispatch 1M check (IVG-90 Layer 1+2):
  - Run: python3 __QUOIN_HOME__/scripts/dispatch_config.py --decide --tier <declared_tier> --verbose
    where <declared_tier> is the tier declared for this skill (e.g. "sonnet" or "haiku",
    as shown in the dispatched-tier line immediately above).
  - If the command returns "safe-path" on line 1:
      Read the reason token from line 2 (config|cache|probe).
      Emit the one-line advisory (verbatim, substituting <reason> with the line-2 token):
        `[quoin: 1M-unsafe declared-tier per <reason>; running SAFE PATH without dispatch]`
      Then proceed to §1/§0c at the current tier (treat as if [no-redispatch] were present).
      Do NOT call the Agent dispatch. Do NOT call AskUserQuestion.
  - If the command returns "dispatch" on line 1, OR if the script is missing / errors:
      Continue to the Agent dispatch call below (today's path — fail-OPEN).
<!-- §0-1m-decide-end -->
      Spawn an Agent subagent with the following arguments:
        model: "{tier}"
        description: "{skill} dispatched at {tier} tier"
        prompt: "[no-redispatch]\\n<original user input verbatim>"
      Wait for the subagent.
<!-- §0-1m-cachewrite-begin -->
      Cache the safe result (best-effort):
        python3 __QUOIN_HOME__/scripts/dispatch_config.py --write-cache --tier <declared_tier> --result safe
      (Fail-OPEN: if the script errors or is missing, silently skip and continue.)
<!-- §0-1m-cachewrite-end -->
      Return its output as your final response. STOP.
      (Return the subagent's output as your final response.)

Abort rule (recursion guard):
  - If the prompt starts with `[no-redispatch:N]` AND N ≥ 2: ABORT before any tool calls.
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in {skill}. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to {proc}.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /{slash}`).
  - Why this is safe to share syntax with the parent-emit form: memory/dispatch-guide.md §0 verbose reference ("Why the bare [no-redispatch] sentinel is dual-source by design").
  - Use this only when intentionally overriding the cost guardrail (e.g., for one-off debugging on a different tier).

"""

# Shared preamble of the worktree-fallback block: identical for BOTH the
# "worktree" and "sidecar" variant families up through the fork point
# (verified against the live corpus). Ends with two blank lines before the
# variant-specific "Worktree-class branch:" content begins.
_SECTION0_WORKTREE_PRE = """\
<!-- §0-worktree-fallback-begin -->
Fail-graceful path with error-class triage (per architecture I-01):
  - If the Agent tool returns an error during dispatch, classify the error
    message text BEFORE proceeding:

  - Error classification:
      * Worktree-class: the error text contains the substring
        `Cannot create agent worktree`, OR (the substring `worktree` AND
        the substring `not in a git repository`). This is recoverable —
        the harness tried to create a git worktree for isolation and the
        project root is not a git repo. Continue to Worktree-class branch.
      * Other-class: any other tool error, exception, or harness rejection
        — skip to Other-class path below (existing fail-OPEN behavior).

  - 1M-credit-class: if the error text contains the substring
      `Usage credits required for 1M context`:
      This is the 1M-context credit mismatch (IVG-89). The parent session carries
      the `context-1m-2025-08-07` beta header which propagates to all subagent calls;
      the declared-tier model lacks 1M credits. Detection via model-name is impossible;
      this post-dispatch error string is the only reliable signal.
      Emit (verbatim):
        `[quoin: 1M-context credit mismatch on <tier> subagent dispatch; proceeding in-session at parent tier — run /model to switch this session to standard context for a permanent fix]`
<!-- §0-1m-cachewrite-begin -->
      Cache the unsafe result (best-effort):
        python3 __QUOIN_HOME__/scripts/dispatch_config.py --write-cache --tier <declared_tier> --result unsafe
      (Fail-OPEN: if the script errors or is missing, silently skip and continue.)
<!-- §0-1m-cachewrite-end -->
      Then proceed to §1 at the current tier (treat as if `[no-redispatch]` were present).
      Do NOT retry the Agent dispatch. Do NOT call AskUserQuestion.


"""

# Autonomous fail-OPEN prefix (artifact form, D-02 — rendered VERBATIM, zero
# wording edits) prepended to the shared Worktree-class-branch body for the 12
# worktree-variant skills that carry the clause. NOTE: the "§1" occurrence
# inside this fragment is FIXED literal text (verified identical across both
# proceed_ref=§1 and proceed_ref=§0c carriers in the live corpus) — do not
# substitute it.
_SECTION0_AUTONOMOUS_PREFIX = """\
      Autonomous fail-OPEN (checked FIRST): if the incoming prompt carries
      the `[autonomous]` sentinel, then on this worktree-class dispatch
      error, proceed at current tier fail-OPEN and do NOT call
      AskUserQuestion — skip straight to the Other-class path below (it
      emits the bare warning and the `error-class=worktree` classification
      line), then proceed to §1 at the current tier. Otherwise (no
      `[autonomous]` sentinel — non-autonomous behavior unchanged):
      """

# Shared Worktree-class-branch body — present for ALL 15 worktree-variant
# skills regardless of has_autonomous_clause; the 12 clause-bearing skills
# prepend _SECTION0_AUTONOMOUS_PREFIX, the 3 clause-less skills (cleanup,
# continue_work, status) prepend only the base "      " indent.
_SECTION0_WORKTREE_CLASS_BODY = """\
Worktree creation is hook-driven and cannot be skipped by omitting a
      parameter. Use the AskUserQuestion tool to present the user with one
      option:
        (c) `proceed-current-tier` — Skip dispatch, proceed at the current
            (more expensive) tier. This is the only available recovery path.
      Question header: `Subagent dispatch failed (worktree creation). Proceeding at current tier.`
      Note for the user: "Worktree dispatch failed and no retry mechanism
      is available — worktree creation is unconditional in this harness.
      Proceeding at current tier."

"""

# Sidecar block (5 skills: end_of_task, implement, pr, rollback, workspace —
# all sonnet-tier, proceed_ref=§1). Distinct 2-phase worktree-isolation design
# with its own Autonomous fail-OPEN clause form (D-02 — rendered VERBATIM,
# distinct wording from the artifact form above by deliberate design, NOT
# drift: implement/SKILL.md's §0b explicitly forbids AskUserQuestion or
# proceed-current-tier for source-mutating skills, so this form documents an
# unconditional no-op rather than an ordering rule). "(sonnet for this skill)"
# is fixed literal text — all 5 sidecar carriers are sonnet-tier (T-01).
_SECTION0_SIDECAR_BLOCK = """\
<!-- §0-sidecar-begin -->
  Source-mutating dispatch — two-phase worktree isolation (D-08):

  STEP A0 — Consult the worktree-isolation decider FIRST (default is skip):
     Run via Bash:
       python3 __QUOIN_HOME__/scripts/worktree_isolation.py --decide
     Isolation is opt-in (D-04): the decider prints `skip` unless
     QUOIN_WORKTREE_ISOLATION=on, the dispatch.json config opts in, or a prior probe
     wrote a `works` sentinel. If the output is `skip`, DO NOT write the sidecar and
     DO NOT dispatch with isolation: "worktree" — skip STEP A / STEP B / STEP C and go
     straight to a PLAIN Agent dispatch at the declared cheap-tier model (sonnet), with
     no sidecar write and no worktree round-trip. Only when the output is `attempt` do
     STEP A / STEP B / STEP C run.

  STEP A — Write the dispatch sidecar BEFORE calling the Agent tool:
     Run via Bash:
       PROJECT_ROOT="$(python3 __QUOIN_HOME__/scripts/path_resolve.py --print-project-root)"
       python3 __QUOIN_HOME__/scripts/dispatch_sidecar.py \\
           --skill <skill-name> \\
           --project-root "$PROJECT_ROOT" \\
           --plan "<resolved-plan-path-or-empty>"
     (The WorktreeCreate hook reads this sidecar to resolve the nested git root.)

  STEP B — Phase 1: Agent dispatch WITH isolation: "worktree" (normal path):
     Call the Agent tool with isolation: "worktree" at the declared cheap-tier
     model (sonnet for this skill). The deployed WorktreeCreate hook at
     __QUOIN_HOME__/hooks/worktreecreate.sh reads the sidecar, runs
     git_root_for_dispatch.py, and (when a single nested repo resolves)
     creates a worktree IN the nested git root and returns its path.
     One-time probe (opt-in path only): when the probe sentinel is still unknown,
     instruct the child to record its working directory to a marker; after the Agent
     returns, compare it to the created worktree path and persist the result exactly
     once via
       python3 __QUOIN_HOME__/scripts/worktree_isolation.py --write-probe --result works|broken

  STEP C — Phase 2 retry WITHOUT isolation (on Worktree-class error):
     If Phase 1 fails with a Worktree-class error (regex above), the hook
     either returned skip (no stdout → harness fails) or encountered an error.
     Re-dispatch the Agent call WITHOUT isolation: "worktree", at the SAME
     declared cheap-tier model (sonnet). Do NOT escalate to parent tier.
     Emit one-line audit:
       [quoin-stage-1: worktree dispatch skipped; proceeding at sonnet without isolation]
     Autonomous fail-OPEN: if the incoming prompt carries the `[autonomous]`
     sentinel, then on any worktree-class dispatch error, proceed at current
     tier fail-OPEN and do NOT call AskUserQuestion — this is already
     guaranteed unconditionally by this Phase 2 retry (no AskUserQuestion
     exists in this path to skip), so behavior here is identical with or
     without the sentinel.

  STEP D — Done:
     No child-side coordination required. The harness handles cwd correctly:
     on Phase 1 success, child sees the worktree as cwd; on Phase 2, child
     inherits the parent's session cwd (today's behavior, unchanged).
<!-- §0-sidecar-end -->

  - Worktree-class branch: handled by Phase 2 (§0-sidecar block above).
    Phase 2 retries at the declared cheap-tier model without isolation.
    Do NOT use AskUserQuestion or proceed-current-tier for source-mutating skills.

"""

# "Other-class path" closer for the 15 worktree-variant skills. Byte-identical
# text also closes the sidecar variant (below) modulo the header line only.
_SECTION0_WORKTREE_POST = """\
  - Other-class path (also: worktree-class after user acknowledges c):
      Do NOT abort the user's invocation.
      Emit the bare warning (verbatim):
        `[quoin-stage-1: subagent dispatch unavailable; proceeding at current tier]`
      If this path was reached via a worktree-class error, ALSO emit the
      classification line (second, separate):
        `[quoin-stage-1: error-class=worktree; user-choice=c; proceeding at current tier]`
      Then proceed to §1 at the current tier (fail-OPEN per I-01).
<!-- §0-worktree-fallback-end -->
"""

# "Other-class path" closer for the 5 sidecar skills — same body, different
# header line ("non-worktree Agent errors" vs "also: worktree-class after
# user acknowledges c") reflecting the sidecar's distinct Worktree-class
# handling (Phase 2 retry, not an AskUserQuestion prompt).
_SECTION0_SIDECAR_POST = """\
  - Other-class path (non-worktree Agent errors):
      Do NOT abort the user's invocation.
      Emit the bare warning (verbatim):
        `[quoin-stage-1: subagent dispatch unavailable; proceeding at current tier]`
      If this path was reached via a worktree-class error, ALSO emit the
      classification line (second, separate):
        `[quoin-stage-1: error-class=worktree; user-choice=c; proceeding at current tier]`
      Then proceed to §1 at the current tier (fail-OPEN per I-01).
<!-- §0-worktree-fallback-end -->
"""


def render_section0_block(skill: str) -> str:
    """Render the §0 Model dispatch block for a given skill (D-03: pure
    composition over the 5 axes in SECTION0_TARGET_SKILLS — no per-file free
    text). Verified byte-exact (empty diff) against the marker-normalized
    (N1/N2) corpus for all 20 skills before this generator became the owner.
    """
    tier, proc, variant, clause, extra_comment = SECTION0_TARGET_SKILLS[skill]
    slash = _SECTION0_SLASH_NAME_OVERRIDES.get(skill, skill)

    base = (
        _SECTION0_BASE_BODY.replace("{tier}", tier)
        .replace("{proc}", proc)
        .replace("{skill}", skill)
        .replace("{slash}", slash)
    )

    if variant == "worktree":
        worktree_class = "  - Worktree-class branch:\n"
        if clause:
            worktree_class += _SECTION0_AUTONOMOUS_PREFIX + _SECTION0_WORKTREE_CLASS_BODY
        else:
            worktree_class += "      " + _SECTION0_WORKTREE_CLASS_BODY
        body = _SECTION0_WORKTREE_PRE + worktree_class + _SECTION0_WORKTREE_POST
    else:  # sidecar
        body = _SECTION0_WORKTREE_PRE + _SECTION0_SIDECAR_BLOCK + _SECTION0_SIDECAR_POST

    proc_suffix = " (skill body)." if proc == "§1" else "."
    closer = (
        "Otherwise (already at or below declared tier, OR prompt has "
        f"[no-redispatch] sentinel, OR dispatch unavailable): proceed to {proc}{proc_suffix}\n"
    )

    tail = closer
    if extra_comment:
        tail += f"\n<!-- §0b: intentionally omitted — /{skill} has no sub-phase dispatch -->\n"
    tail += SECTION0_END_MARKER + "\n"

    return SECTION0_HEADING + "\n" + base + body + tail


def inject_section0_block_into_file(skill: str, skill_md: pathlib.Path) -> None:
    """Inject/refresh the §0 Model dispatch block into a single adapter
    SKILL.md file — IVG-165.

    D-01a: marker-anchored region replacement (heading through
    SECTION0_END_MARKER inclusive), NOT `_replace_existing_block` (which
    stops at the next `(?=^## )` and would not fix start_of_day's
    over-capture — see D-01a rationale above SECTION0_END_MARKER).

    FAIL LOUD if SECTION0_HEADING or SECTION0_END_MARKER is not exactly
    count==1 in the existing file (both must already be present — this
    generator only REFRESHES an existing marker-normalized region; it does
    not perform first-time insertion for §0, unlike the §0'/§0″/§0‴ inject
    functions above).
    """
    text = skill_md.read_text(encoding="utf-8")

    heading_count = text.count(SECTION0_HEADING)
    if heading_count != 1:
        raise ValueError(
            f"{skill_md}: SECTION0_HEADING appears {heading_count} times (expected exactly 1) "
            "— FAIL LOUD. Cannot determine §0 region start."
        )
    marker_count = text.count(SECTION0_END_MARKER)
    if marker_count != 1:
        raise ValueError(
            f"{skill_md}: SECTION0_END_MARKER appears {marker_count} times (expected exactly 1) "
            "— FAIL LOUD. Cannot determine §0 region end."
        )

    heading_idx = text.index(SECTION0_HEADING)
    marker_idx = text.index(SECTION0_END_MARKER)
    region_end = marker_idx + len(SECTION0_END_MARKER) + 1  # include marker's own trailing \n

    new_block = render_section0_block(skill)
    new_text = text[:heading_idx] + new_block + text[region_end:]

    # Atomic write: .tmp + os.replace(), mirrors the §0″/§0‴ inject functions.
    tmp_path = skill_md.with_suffix(".md.tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    os.replace(tmp_path, skill_md)


# ─── §0c block bodies (verbatim from f81b6ff, §0c path fixed to __QUOIN_HOME__) ─

# NOTE: The sourced helper path `. __QUOIN_HOME__/scripts/pidfile_helpers.sh` uses
# the deploy-root placeholder. installer.py substitutes __QUOIN_HOME__ → deploy root
# and explicitly leaves literal ~/.claude/ UNTOUCHED (lesson 2026-05-15). This is
# the single deliberate divergence from the f81b6ff text (which had `~/.claude/`).

ZC_BLOCK_ARCHITECT = """\
## §0c Pidfile lifecycle

This skill is Opus-tier (no §0 dispatch block). §0c is the only §0-class block in this file — it is both first and last. **Phase-4-only variant:** the actual pidfile acquire/release calls are in Phase 4 (the critic loop), not at skill entry. This block is a pointer comment.

The acquire/release pattern is scoped to Phase 4 only (the internal critic loop). When Phase 4 begins, add:
```
. __QUOIN_HOME__/scripts/pidfile_helpers.sh && pidfile_acquire architect-phase-4
```
When Phase 4 ends (or on any abort from Phase 4): `pidfile_release architect-phase-4`.

If the helper is missing or fails: emit one-line warning `[quoin-S-2: pidfile helpers unavailable; proceeding without lifecycle protection]` and continue (fail-OPEN). The full skill entry/exit does NOT acquire — only Phase 4 inner loop.

Purpose: lets `precompact.sh` hook know an `/architect` Phase 4 session is active.

"""

ZC_BLOCK_REVIEW = """\
## §0c Pidfile lifecycle

This skill is Opus-tier (no §0 dispatch block). §0c is the only §0-class block in this file — it is both first and last.

At entry — immediately after reading this block:

```
. __QUOIN_HOME__/scripts/pidfile_helpers.sh && pidfile_acquire review
```

If the script is missing or fails: emit one-line warning `[quoin-S-2: pidfile helpers unavailable; proceeding without lifecycle protection]` and continue without abort (fail-OPEN).

At exit — call from every completion path AND every error/abort path:
```
pidfile_release review
```

Use a trap when the skill body involves bash-driven subagents:
```
trap 'pidfile_release review' EXIT
```

Purpose: lets `precompact.sh` hook know a `/review` session is active. `precompact.sh` never blocks — it always allows auto-compaction (`precompact.sh:5-6`, `:14`); pidfile presence instead selects row 2 of its three-row truth table (`precompact.sh:323`, `:506-507`): a deterministic checkpoint is written, with no `pending-restore` sentinel, so the workflow continues uninterrupted.

"""

ZC_BLOCKS = {
    "architect": ZC_BLOCK_ARCHITECT,
    "review": ZC_BLOCK_REVIEW,
}

# ─── §0' shared block template ────────────────────────────────────────────────
# Variables: {skill_dispatch_contract}
# All 6 REQUIRED_TOKENS and 3 score-extraction strings are inside this block.

_POLLUTION_BLOCK_TEMPLATE = """\
## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)

This skill runs in the user's current session. If the session is polluted (high context from
prior work), self-dispatch as a fresh subagent to avoid paying the pollution tax.

Detection:
  - Read the most-recent session-state file: `.workflow_artifacts/memory/sessions/<today>-<task>.md`
    OR the fallback `.workflow_artifacts/memory/pollution-score-latest.txt`.
  - Parse the `pollution_score: N` field (integer).
  - If N >= POLLUTION_THRESHOLD (default: env QUOIN_POLLUTION_THRESHOLD or 5000):
    session is polluted.
  - Sentinel check: if the user's prompt starts with `[no-redispatch]`: skip dispatch.
  - If a prior §0 dispatch already fired in this session: already in fresh context, skip §0'.

Dispatch action (when pollution detected AND no sentinel AND no prior §0 dispatch):
{skill_dispatch_contract}
  If task description cannot be determined:
    Emit: `[quoin-S-1: cannot extract per-skill dispatch contract; running in main]`
    Proceed with skill body.

  Otherwise spawn an Agent subagent:
    model: "opus"
    description: "{skill} — pollution-isolated dispatch"
    prompt: "{skill_prompt}"

  Wait for the subagent. Return its output as your final response. STOP.

Fail-OPEN path:
  Full AskUserQuestion Question/Header/description wording for every branch below:
  memory/dispatch-guide.md §0' verbose reference ("Verbatim AskUserQuestion wording").
  If Agent tool unavailable or errors — classify the error first:

  - Autonomous-class (checked FIRST, before 1M-credit or generic classification): if the
    incoming prompt carries the `[autonomous]` sentinel, then on ANY §0' dispatch-failure or
    1M-context-credit error, proceed at current tier fail-OPEN and DO NOT call `AskUserQuestion`
    — skip the 1M-credit-class and generic branches below entirely. Print
    `[quoin-autonomous: §0' dispatch failed; proceeding fail-OPEN at current tier]` and proceed
    with skill body (treat as bare [no-redispatch]).
  - 1M-credit-class: if the error text contains the substring
      `Usage credits required for 1M context`:
      The §0' opus dispatch hit a 1M-context credit mismatch (IVG-89). Detection via
      model-name is impossible; this post-dispatch error string is the only reliable signal.
      Issue an `AskUserQuestion` — Option 1 label "Abort — I'll switch with /model first";
      Option 2 label "Proceed in-session at parent tier".
      On Option 1: print `[quoin: 1M-context credit mismatch; abort per user choice —
      switch with /model and re-invoke /{skill}]` and STOP. Do NOT proceed to skill body.
      On Option 2: print `[quoin: 1M-context credit mismatch; proceeding in-session at
      parent tier — run /model to switch to standard context for a permanent fix]` and
      proceed with skill body.
  - Any other error (non-1M): Issue an `AskUserQuestion` (generic wording; Option 1 = abort,
      Option 2 = proceed in the current polluted session).
      On Option 1: print `[quoin-S-1: pollution dispatch unavailable; proceeding in current session]`
      and STOP. Do NOT proceed to skill body.
      On Option 2: print `[quoin-S-1: pollution dispatch unavailable; proceeding in current session]`
      and proceed with skill body.

Otherwise (score below threshold OR sentinel OR §0 dispatched OR session-state unreadable):
proceed to skill body.

"""

# ─── Per-skill dispatch contract fields ──────────────────────────────────────
# Verbatim from f81b6ff per skill, used to fill {skill_dispatch_contract} in the template.
# Also provides {skill_prompt} substitution.

# Each entry: (contract_body_lines, prompt_string)
DISPATCH_CONTRACTS = {
    "architect": (
        """\
  Determine dispatch contract fields:
    - Extract the task description from the user's invocation.
    - Paths to /discover output are static (relative to cwd):
        `.workflow_artifacts/memory/repos-inventory.md`
        `.workflow_artifacts/memory/architecture-overview.md`
        `.workflow_artifacts/memory/dependencies-map.md`
""",
        "[no-redispatch]\\n/architect <task description>\\nArchitecture context paths:\\n"
        "- .workflow_artifacts/memory/repos-inventory.md\\n"
        "- .workflow_artifacts/memory/architecture-overview.md\\n"
        "- .workflow_artifacts/memory/dependencies-map.md",
    ),
    "plan": (
        """\
  Determine dispatch contract fields:
    - Extract the task description from the user's invocation.
    - Locate architecture.md at the task root (if exists): `.workflow_artifacts/<task>/architecture.md`.
    - Detect stage number from the user prompt if multi-stage.
""",
        "[no-redispatch]\\n/plan <task description>\\nPlan context paths:\\n"
        "- .workflow_artifacts/<task>/architecture.md (if exists)\\n"
        "- Stage: <N> (if multi-stage)",
    ),
    "critic": (
        """\
  Determine dispatch contract fields:
    - Locate the target artifact path from the invocation (e.g., `/critic Target: <path>`
      convention used by /thorough_plan, or the most-recent current-plan.md in the task dir).
""",
        "[no-redispatch]\\n/critic\\nTarget: <absolute path to target artifact>",
    ),
    "revise": (
        """\
  Determine dispatch contract fields:
    - Locate current-plan.md in the task directory (resolve via path_resolve.py).
    - Locate the most-recent critic-response-N.md in the same task directory.
""",
        "[no-redispatch]\\n/revise\\nPlan path: <absolute path to current-plan.md>\\n"
        "Critic response: <absolute path to critic-response-N.md>",
    ),
    "review": (
        """\
  Determine dispatch contract fields:
    - Locate `current-plan.md` in the task directory (resolve via path_resolve.py).
    - Get the current git branch (`git rev-parse --abbrev-ref HEAD`).
""",
        "[no-redispatch]\\n/review\\nPlan path: <absolute path to current-plan.md>\\n"
        "Branch: <current git branch>",
    ),
    "init_workflow": (
        """\
  Determine dispatch contract fields:
    - Use the current working directory as the project root absolute path.
""",
        "[no-redispatch]\\n/init_workflow <project root absolute path>",
    ),
    "discover": (
        """\
  Determine dispatch contract fields:
    - Use the current working directory as the project root absolute path.
""",
        "[no-redispatch]\\n/discover <project root absolute path>",
    ),
    "specify": (
        """\
  Determine dispatch contract fields:
    - Extract the task description from the user's invocation.
    - Resolve the task dir via path_resolve.py (no --stage — spec.md is always at task root).
    - The spec is written to `.workflow_artifacts/<task>/spec.md`.
""",
        "[no-redispatch]\\n/specify <task description>\\n"
        "Spec output path: .workflow_artifacts/<task>/spec.md",
    ),
    "security_review": (
        """\
  Determine dispatch contract fields:
    - Locate `current-plan.md` in the task directory if resolvable (resolve via path_resolve.py); else target the standalone `.workflow_artifacts/security-review/` dir (D-07).
    - Get the current git branch (`git rev-parse --abbrev-ref HEAD`) -- the OWASP-style security pass reviews this branch's diff.
""",
        "[no-redispatch]\\n/security_review\\nBranch: <current git branch>\\n"
        "OWASP security review -- Plan path: <absolute path to current-plan.md, if resolvable>",
    ),
    "enrich": (
        """\
  Determine dispatch contract fields:
    - Extract the raw task description/prompt from the user's invocation.
    - Resolve the task dir via path_resolve.py (no --stage — enriched-prompt.md is
      always at task root).
    - The enriched prompt is written to `.workflow_artifacts/<task>/enriched-prompt.md`.
""",
        "[no-redispatch]\\n/enrich <task description>\\n"
        "Enriched prompt output path: .workflow_artifacts/<task>/enriched-prompt.md",
    ),
}


# ─── Block rendering ──────────────────────────────────────────────────────────

def render_pollution_block(skill: str) -> str:
    """Render the §0' block for a given skill."""
    contract_body, prompt_str = DISPATCH_CONTRACTS[skill]
    return _POLLUTION_BLOCK_TEMPLATE.format(
        skill_dispatch_contract=contract_body,
        skill=skill,
        skill_prompt=prompt_str,
    )


def render_zc_block(skill: str) -> str:
    """Render the §0c block for architect or review."""
    return ZC_BLOCKS[skill]


# ─── File manipulation ────────────────────────────────────────────────────────

def _find_first_h2(lines: list[str]) -> int:
    """Return the 0-indexed line number of the first '## ' heading.

    Skips H1 (^# ) and optional portable-intent line and intro paragraph.
    Returns -1 if no H2 found (caller should FAIL LOUD).
    """
    past_h1 = False
    for i, line in enumerate(lines):
        if not past_h1:
            if line.startswith("# "):
                past_h1 = True
            continue
        if line.startswith("## "):
            return i
    return -1


def _replace_existing_block(text: str, heading: str, new_block: str) -> str:
    """Replace an existing block (heading through line before next '## ') in place."""
    # Find the block: from heading to (but not including) the next '## '
    pattern = re.compile(
        r"^" + re.escape(heading) + r".+?(?=^## )",
        flags=re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(text)
    if match:
        return text[: match.start()] + new_block + text[match.end() :]
    return text


def inject_blocks_into_file(skill: str, skill_md: pathlib.Path) -> str:
    """Return the new content for skill_md with §0' (and §0c if needed) injected.

    Strategy (per plan D-A5 / proc:T-02-anchor):
    - If §0' already present: replace in place (idempotent refresh).
    - If §0' absent: find the first H2, insert before it.
    - For architect/review: handle §0c similarly.
    - FAIL LOUD if no H2 found at all.
    """
    text = skill_md.read_text(encoding="utf-8")
    pollution_block = render_pollution_block(skill)
    needs_zc = skill in ZC_SKILLS

    # ── Refresh path (idempotent): block(s) already present ──
    if POLLUTION_HEADING in text:
        text = _replace_existing_block(text, POLLUTION_HEADING, pollution_block)
        if needs_zc and ZC_HEADING in text:
            text = _replace_existing_block(text, ZC_HEADING, render_zc_block(skill))
        elif needs_zc and ZC_HEADING not in text:
            # §0c absent but §0' present (shouldn't happen on fresh files, but handle):
            # Insert §0c immediately before §0'
            text = text.replace(POLLUTION_HEADING, render_zc_block(skill) + POLLUTION_HEADING, 1)
        return text

    # ── Insert path: block(s) absent, find anchor ──
    lines = text.splitlines(keepends=True)
    anchor_idx = _find_first_h2(lines)
    if anchor_idx == -1:
        raise ValueError(
            f"No '## ' heading found in {skill_md} — cannot determine insertion anchor. "
            "FAIL LOUD: the generator requires at least one H2 heading as an insertion point."
        )

    # Build insertion: for ZC skills insert §0c THEN §0'; for others just §0'.
    if needs_zc:
        insert_text = render_zc_block(skill) + pollution_block
    else:
        insert_text = pollution_block

    # Insert immediately before the anchor line
    lines.insert(anchor_idx, insert_text)
    return "".join(lines)


def inject_mintier_block_into_file(skill: str, skill_md: pathlib.Path) -> None:
    """Inject/refresh §0″ Minimum-tier guard into a single adapter SKILL.md file.

    Handles §0″ injection independently of inject_blocks_into_file (which handles
    §0'/§0c). This is a SEPARATE function to avoid the early-return correctness bug
    in inject_blocks_into_file's refresh path (D-08 in plan).

    Strategy (per proc:T-02-anchor):
    - Refresh path: if MINTIER_HEADING already in text, replace in place (idempotent).
    - Insert path: §0' block ends at the next '## ' heading after POLLUTION_HEADING.
      Locate that heading and insert §0″ immediately before it.
    - FAIL LOUD if no POLLUTION_HEADING found (§0' must be injected first).
    - Atomic write: .tmp + os.replace(), mirror existing pattern.
    """
    text = skill_md.read_text(encoding="utf-8")
    mintier_block = render_mintier_block(skill)

    if MINTIER_HEADING in text:
        # Refresh path (all re-runs after first injection)
        text = _replace_existing_block(text, MINTIER_HEADING, mintier_block)
    elif POLLUTION_HEADING in text:
        # Insert path: all 10 skills have §0' (first run)
        # §0' block ends at the next "## " heading STRICTLY AFTER POLLUTION_HEADING
        p_idx = text.index(POLLUTION_HEADING)
        next_h2_match = re.search(r"^## ", text[p_idx + len(POLLUTION_HEADING):], re.MULTILINE)
        if not next_h2_match:
            raise ValueError(
                f"No heading found after §0' block in {skill_md} — FAIL LOUD. "
                "Cannot determine insertion point for §0″ block."
            )
        insert_pos = p_idx + len(POLLUTION_HEADING) + next_h2_match.start()
        text = text[:insert_pos] + mintier_block + text[insert_pos:]
    else:
        raise ValueError(
            f"No §0' block found in {skill_md} (run §0' injection first) — FAIL LOUD. "
            "inject_mintier_block_into_file requires §0' to already be present."
        )

    # Atomic write: .tmp + os.replace()
    tmp_path = skill_md.with_suffix(".md.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, skill_md)


def inject_mintier_sonnet_block_into_file(skill: str, skill_md: pathlib.Path) -> None:
    """Inject/refresh §0‴ Minimum-tier guard (Sonnet tier) into a single adapter
    SKILL.md file — IVG-117.

    Anchors on SECTION0_HEADING (the hand-authored §0 block, always present in the
    13 Sonnet targets), NOT on §0'/§0″ (Opus-only, disjoint file set). D-06.

    Strategy:
    - Refresh path: if MINTIER_SONNET_HEADING already in text, replace in place
      (idempotent).
    - Insert path: find SECTION0_HEADING; find the next '## ' heading strictly
      after it; insert §0‴ immediately before that next H2. FAIL LOUD if
      SECTION0_HEADING is absent or appears more than once.
    - Atomic write: .tmp + os.replace(), mirrors inject_mintier_block_into_file.
    """
    text = skill_md.read_text(encoding="utf-8")
    sonnet_block = render_mintier_sonnet_block(skill)

    if MINTIER_SONNET_HEADING in text:
        # Refresh path (all re-runs after first injection)
        text = _replace_existing_block(text, MINTIER_SONNET_HEADING, sonnet_block)
    else:
        # Insert path: anchor on SECTION0_HEADING, FAIL LOUD if absent or duplicated
        count = text.count(SECTION0_HEADING)
        if count != 1:
            raise ValueError(
                f"{skill_md}: SECTION0_HEADING appears {count} times (expected exactly 1) "
                "— FAIL LOUD. Cannot determine insertion anchor for §0‴ block."
            )
        s_idx = text.index(SECTION0_HEADING)
        next_h2_match = re.search(r"^## ", text[s_idx + len(SECTION0_HEADING):], re.MULTILINE)
        if not next_h2_match:
            raise ValueError(
                f"No heading found after §0 block in {skill_md} — FAIL LOUD. "
                "Cannot determine insertion point for §0‴ block."
            )
        insert_pos = s_idx + len(SECTION0_HEADING) + next_h2_match.start()
        text = text[:insert_pos] + sonnet_block + text[insert_pos:]

    # Atomic write: .tmp + os.replace()
    tmp_path = skill_md.with_suffix(".md.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, skill_md)


# ─── Main processing ──────────────────────────────────────────────────────────

def _get_adapter_dir() -> pathlib.Path:
    """Return the adapter skills directory (quoin/adapters/claude/skills/)."""
    script_dir = pathlib.Path(__file__).resolve().parent
    # scripts/ is inside quoin/quoin/; adapter dir is at quoin/quoin/adapters/claude/skills/
    quoin_pkg = script_dir.parent   # quoin/quoin/
    return quoin_pkg / "adapters" / "claude" / "skills"


def run_inject(*, dry_run: bool = False) -> int:
    """Inject/refresh §0' (and §0c) AND §0″ into all 10 target adapter SKILL.md files.

    Two loops:
    1. Loop over POLLUTION_TARGET_SKILLS — injects §0'/§0c (inject_blocks_into_file).
    2. Loop over MINTIER_TARGET_SKILLS — injects §0″ (inject_mintier_block_into_file).
    §0″ injection must run AFTER §0' (it uses §0' as the insertion anchor).

    Returns 0 on success, 1 on any error.
    """
    adapter_dir = _get_adapter_dir()
    errors = []

    # ── Loop 1: §0' (and §0c) injection ──
    for skill in POLLUTION_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"MISSING: {skill_md}")
            continue

        try:
            new_content = inject_blocks_into_file(skill, skill_md)
        except (ValueError, OSError) as e:
            errors.append(f"ERROR processing §0' for {skill}: {e}")
            continue

        if dry_run:
            print(f"=== {skill} §0' preview ===")
            # Show only the injected block(s) for brevity
            idx = new_content.find("## §0")
            preview = new_content[idx : new_content.find("\n## ", idx + 4) + 1] if idx != -1 else "(no §0 block found)"
            print(preview[:500])
            continue

        # Atomic write: .tmp + os.replace()
        tmp_path = skill_md.with_suffix(".md.tmp")
        try:
            tmp_path.write_text(new_content, encoding="utf-8")
            os.replace(tmp_path, skill_md)
        except OSError as e:
            errors.append(f"WRITE ERROR for §0' in {skill}: {e}")
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            continue
        print(f"  injected §0' into {skill_md.relative_to(adapter_dir.parent.parent.parent.parent)}")

    # ── Loop 2: §0″ (Minimum-tier guard) injection ──
    for skill in MINTIER_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"MISSING (§0″): {skill_md}")
            continue

        if dry_run:
            # For dry-run: show what §0″ would look like
            print(f"=== {skill} §0″ preview ===")
            block = render_mintier_block(skill)
            print(block[:500])
            continue

        try:
            inject_mintier_block_into_file(skill, skill_md)
        except (ValueError, OSError) as e:
            errors.append(f"ERROR processing §0″ for {skill}: {e}")
            continue
        print(f"  injected §0″ into {skill_md.relative_to(adapter_dir.parent.parent.parent.parent)}")

    # ── Loop 3: §0‴ (Minimum-tier guard — Sonnet tier) injection — IVG-117 ──
    # Independent of Loops 1-2: anchors on SECTION0_HEADING (hand-authored §0),
    # not on §0'/§0″. MINTIER_SONNET_TARGET_SKILLS is disjoint from the Opus-10.
    for skill in MINTIER_SONNET_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"MISSING (§0‴): {skill_md}")
            continue

        if dry_run:
            print(f"=== {skill} §0‴ preview ===")
            block = render_mintier_sonnet_block(skill)
            print(block[:500])
            continue

        try:
            inject_mintier_sonnet_block_into_file(skill, skill_md)
        except (ValueError, OSError) as e:
            errors.append(f"ERROR processing §0‴ for {skill}: {e}")
            continue
        print(f"  injected §0‴ into {skill_md.relative_to(adapter_dir.parent.parent.parent.parent)}")

    # ── Loop 4: §0 (Model dispatch) refresh — IVG-165 ──
    # Independent of Loops 1-3: refreshes the hand-authored §0 block itself
    # (heading through SECTION0_END_MARKER) via marker-anchored replacement
    # (D-01a). SECTION0_TARGET_SKILLS is the full 20-skill §0-carrying roster
    # (disjoint concern from MINTIER_SONNET_TARGET_SKILLS, which targets
    # where §0‴ gets INSERTED after §0, not §0's own content).
    for skill in SECTION0_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            errors.append(f"MISSING (§0): {skill_md}")
            continue

        if dry_run:
            print(f"=== {skill} §0 preview ===")
            block = render_section0_block(skill)
            print(block[:500])
            continue

        try:
            inject_section0_block_into_file(skill, skill_md)
        except (ValueError, OSError) as e:
            errors.append(f"ERROR processing §0 for {skill}: {e}")
            continue
        print(f"  refreshed §0 in {skill_md.relative_to(adapter_dir.parent.parent.parent.parent)}")

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


def run_check() -> int:
    """--check mode: verify all 10 adapter SKILL.md files carry correct §0' (and §0c) AND §0″ blocks.

    Returns 0 if all files are fresh, 7 if any drift detected.
    Also verifies:
    - The 6 REQUIRED_TOKENS from test_quoin_pollution_preamble.py inside §0' block
    - The 3 file-wide strings from test_pollution_score_extraction.py inside §0' block
    - The 6 mintier_required_tokens inside §0″ block (MIN-1)
    - The 2 mintier_required_markers as file-level presence checks (MIN-1)
    - §0″ appears AFTER §0' (ordering guard)

    Note: The literal `5000` threshold value is also checked (MIN-A, per plan).

    Token list discipline (MIN-1): mintier_required_tokens (6 content tokens) are checked
    INSIDE the extracted §0″ block. mintier_required_markers (2 HTML markers) are checked
    for presence in the full file text (outside block extraction).
    All required_tokens present in both Option A and Option B variants.
    """
    adapter_dir = _get_adapter_dir()

    # Required tokens inside §0' block (test_quoin_pollution_preamble.py REQUIRED_TOKENS)
    # IVG-89: §0prime-1m-context-precheck region deleted (dead model-name detection);
    # 1M recovery now in Fail-OPEN path (post-dispatch error classification).
    required_tokens = [
        "[no-redispatch]",
        "[quoin-S-1: cannot extract per-skill dispatch contract; running in main]",
        "[quoin-S-1: pollution dispatch unavailable; proceeding in current session]",
        "pollution_score",
        "POLLUTION_THRESHOLD",
        'model: "opus"',
        "Usage credits required for 1M context",
        "[autonomous]",
        "[quoin-autonomous: §0' dispatch failed; proceeding fail-OPEN at current tier]",
    ]
    # Required file-wide strings (test_pollution_score_extraction.py)
    score_extraction_strings = [
        "pollution_score",
        "pollution-score-latest.txt",
        "sessions/",
    ]
    # MIN-A: literal default threshold value must be in block
    threshold_token = "5000"

    # Required tokens inside §0″ block (Option A: 9 content tokens checked inside block).
    # Lists must stay byte-mirrored with MINTIER_REQUIRED_TOKENS in test_mintier_guard.py.
    mintier_required_tokens = [
        "[no-redispatch]",
        'model: "opus"',
        "current_tier < declared_tier",
        "spawn an Agent subagent",
        "Wait for the subagent. Return its output as your final response. STOP.",
        "Usage credits required for 1M context",
        "Abort — run from an Opus session",
        "Proceed at current tier (under-powered)",
        "[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]",
        "[autonomous]",
        "[quoin-mintier-autonomous: §0″ dispatch failed; proceeding fail-OPEN at current tier]",
    ]
    # Required HTML markers for §0″ (MIN-1: 2 markers checked as file-level presence, NOT inside block extraction)
    mintier_required_markers = [
        "<!-- §0doubleprime-begin -->",
        "<!-- §0doubleprime-end -->",
    ]

    drifted = []

    for skill in POLLUTION_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            drifted.append(f"{skill}: adapter SKILL.md missing at {skill_md}")
            continue

        text = skill_md.read_text(encoding="utf-8")

        # Check §0' heading present
        if POLLUTION_HEADING not in text:
            drifted.append(f"{skill}: §0' heading missing")
            continue

        # Extract §0' block (same regex as test file)
        block_match = re.search(
            r"^## §0' Pollution dispatch \(execute after §0 / §0c if present — before skill body\).+?(?=^## )",
            text,
            flags=re.DOTALL | re.MULTILINE,
        )
        if not block_match:
            drifted.append(f"{skill}: §0' block could not be extracted (no trailing ## heading?)")
            continue
        block = block_match.group(0)

        # Check required tokens inside block
        for token in required_tokens:
            if token not in block:
                drifted.append(f"{skill}: missing required token {token!r} in §0' block")

        # Check score-extraction strings inside block
        for s in score_extraction_strings:
            if s not in block:
                drifted.append(f"{skill}: missing score-extraction string {s!r} in §0' block")

        # MIN-A: literal threshold value
        if threshold_token not in block:
            drifted.append(f"{skill}: missing threshold value {threshold_token!r} in §0' block")

        # Check §0c for ZC skills
        if skill in ZC_SKILLS:
            if ZC_HEADING not in text:
                drifted.append(f"{skill}: §0c heading missing")
            else:
                zc_idx = text.index(ZC_HEADING)
                p_idx = text.index(POLLUTION_HEADING)
                if zc_idx >= p_idx:
                    drifted.append(f"{skill}: §0c appears AFTER §0' (ordering violation)")
                # Check §0c placeholder discipline: __QUOIN_HOME__ present, no literal ~/.claude/ in source line
                zc_end = text.find("\n## ", zc_idx + len(ZC_HEADING))
                zc_block = text[zc_idx : zc_end] if zc_end != -1 else text[zc_idx:]
                if "__QUOIN_HOME__/scripts/pidfile_helpers.sh" not in zc_block:
                    drifted.append(f"{skill}: §0c missing __QUOIN_HOME__ placeholder in pidfile source line")
                # Scan for literal ~/.claude/ in any source line
                for line in zc_block.splitlines():
                    if "~/.claude/scripts/pidfile_helpers" in line:
                        drifted.append(f"{skill}: §0c has literal ~/.claude/ in pidfile source line (must use __QUOIN_HOME__)")
                        break

    # ── §0″ checks (MINTIER_TARGET_SKILLS) ──
    # HEADING BYTE-IDENTITY: the ″ (U+2033) must match exactly — see MINTIER_HEADING constant.
    # Extraction regex mirrors §0' extraction pattern for consistency.
    mintier_heading_escaped = re.escape(MINTIER_HEADING)
    for skill in MINTIER_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            drifted.append(f"{skill}: adapter SKILL.md missing at {skill_md} (§0″ check)")
            continue

        text = skill_md.read_text(encoding="utf-8")

        # Check §0″ heading present exactly once
        count = text.count(MINTIER_HEADING)
        if count == 0:
            drifted.append(f"{skill}: §0″ heading missing")
            continue
        if count > 1:
            drifted.append(f"{skill}: §0″ heading appears {count} times (expected exactly 1)")

        # Check HTML markers at file level (MIN-1: markers outside block extraction)
        for marker in mintier_required_markers:
            if text.count(marker) != 1:
                drifted.append(f"{skill}: §0″ marker {marker!r} missing or duplicated (count={text.count(marker)})")

        # Extract §0″ block for token checks
        mintier_block_match = re.search(
            mintier_heading_escaped + r".+?(?=^## )",
            text,
            flags=re.DOTALL | re.MULTILINE,
        )
        if not mintier_block_match:
            drifted.append(f"{skill}: §0″ block could not be extracted (no trailing ## heading?)")
            continue
        mintier_block = mintier_block_match.group(0)

        # Check required tokens inside §0″ block (MIN-1: 6 content tokens)
        for token in mintier_required_tokens:
            if token not in mintier_block:
                drifted.append(f"{skill}: missing mintier required token {token!r} in §0″ block")

        # Ordering guard: §0″ must appear AFTER §0'
        p_idx = text.find(POLLUTION_HEADING)
        m_idx = text.find(MINTIER_HEADING)
        if p_idx != -1 and m_idx != -1 and m_idx <= p_idx:
            drifted.append(f"{skill}: §0″ appears BEFORE or AT §0' (ordering violation)")

    # ── §0‴ checks (MINTIER_SONNET_TARGET_SKILLS) — IVG-117 ──
    # HEADING BYTE-IDENTITY: the ‴ (U+2034) must match exactly — see MINTIER_SONNET_HEADING.
    mintier_sonnet_required_tokens = [
        "[no-redispatch]",
        'model: "sonnet"',
        "current_tier < declared_tier",
        "spawn an Agent subagent",
        "Wait for the subagent. Return its output as your final response. STOP.",
        "Usage credits required for 1M context",
        "Abort — run from a Sonnet session",
        "Proceed at current tier (under-powered)",
        "[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]",
        "[autonomous]",
        "[quoin-mintier-autonomous: §0‴ dispatch failed; proceeding fail-OPEN at current tier]",
        "[no-redispatch:N]",
    ]
    mintier_sonnet_required_markers = [
        "<!-- §0tripleprime-begin -->",
        "<!-- §0tripleprime-end -->",
    ]
    mintier_sonnet_heading_escaped = re.escape(MINTIER_SONNET_HEADING)
    for skill in MINTIER_SONNET_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            drifted.append(f"{skill}: adapter SKILL.md missing at {skill_md} (§0‴ check)")
            continue

        text = skill_md.read_text(encoding="utf-8")

        count = text.count(MINTIER_SONNET_HEADING)
        if count == 0:
            drifted.append(f"{skill}: §0‴ heading missing")
            continue
        if count > 1:
            drifted.append(f"{skill}: §0‴ heading appears {count} times (expected exactly 1)")

        for marker in mintier_sonnet_required_markers:
            if text.count(marker) != 1:
                drifted.append(f"{skill}: §0‴ marker {marker!r} missing or duplicated (count={text.count(marker)})")

        sonnet_block_match = re.search(
            mintier_sonnet_heading_escaped + r".+?(?=^## )",
            text,
            flags=re.DOTALL | re.MULTILINE,
        )
        if not sonnet_block_match:
            drifted.append(f"{skill}: §0‴ block could not be extracted (no trailing ## heading?)")
            continue
        sonnet_block = sonnet_block_match.group(0)

        for token in mintier_sonnet_required_tokens:
            if token not in sonnet_block:
                drifted.append(f"{skill}: missing mintier-sonnet required token {token!r} in §0‴ block")

        # Ordering guard: §0‴ must appear AFTER §0 (SECTION0_HEADING)
        s_idx = text.find(SECTION0_HEADING)
        t_idx = text.find(MINTIER_SONNET_HEADING)
        if s_idx != -1 and t_idx != -1 and t_idx <= s_idx:
            drifted.append(f"{skill}: §0‴ appears BEFORE or AT §0 (ordering violation)")

    # ── §0 checks (SECTION0_TARGET_SKILLS) — IVG-165 ──
    # Zero-tolerance: the live §0 region (heading through SECTION0_END_MARKER
    # inclusive) must equal render_section0_block(skill) byte-for-byte
    # (proc:empty-diff / D-08). This is stricter than the token-presence
    # checks above (§0'/§0″/§0‴) because §0 itself is now generator-owned.
    for skill in SECTION0_TARGET_SKILLS:
        skill_md = adapter_dir / skill / "SKILL.md"
        if not skill_md.exists():
            drifted.append(f"{skill}: adapter SKILL.md missing at {skill_md} (§0 check)")
            continue

        text = skill_md.read_text(encoding="utf-8")

        heading_count = text.count(SECTION0_HEADING)
        if heading_count != 1:
            drifted.append(f"{skill}: SECTION0_HEADING appears {heading_count} times (expected exactly 1)")
            continue
        marker_count = text.count(SECTION0_END_MARKER)
        if marker_count != 1:
            drifted.append(f"{skill}: SECTION0_END_MARKER appears {marker_count} times (expected exactly 1)")
            continue

        heading_idx = text.index(SECTION0_HEADING)
        marker_idx = text.index(SECTION0_END_MARKER)
        region_end = marker_idx + len(SECTION0_END_MARKER) + 1
        live_block = text[heading_idx:region_end]
        expected_block = render_section0_block(skill)
        if live_block != expected_block:
            drifted.append(f"{skill}: §0 block does not match generator output byte-for-byte (drift)")

    if drifted:
        for msg in drifted:
            print(f"DRIFT: {msg}", file=sys.stderr)
        return 7
    print("inject_pollution_dispatch --check: all 10 adapter files are fresh")
    print("inject_pollution_dispatch --check: all 10 §0doubleprime files are fresh")
    print("inject_pollution_dispatch --check: all 13 §0tripleprime files are fresh")
    print("inject_pollution_dispatch --check: all 20 §0 files are fresh")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rendered blocks to stdout instead of writing to disk",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify all 10 adapter SKILL.md files carry correct §0' blocks; exit 7 if any drift",
    )
    args = parser.parse_args()

    if args.dry_run and args.check:
        print("--dry-run and --check are mutually exclusive", file=sys.stderr)
        return 6

    if args.check:
        return run_check()

    return run_inject(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
