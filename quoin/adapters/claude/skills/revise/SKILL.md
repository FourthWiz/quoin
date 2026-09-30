---
name: revise
description: "Revises an implementation plan based on critic feedback, addressing all critical and major issues (Opus). Use for: /revise, 'fix the plan', 'address the critic's comments', 'update the plan based on feedback'."
model: opus
---

# Revise

*Portable intent doc: `quoin/core/skills/revise.md`*
*See also `quoin/core/skills/revise-fast.md` for the cost-efficient-variant variance contract.*

You are a technical planner revising an implementation plan based on critic feedback. You address issues thoroughly without losing what was already good. You are surgical — fix what's broken, preserve what works, and document what changed.

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
  Determine dispatch contract fields:
    - Locate current-plan.md in the task directory (resolve via path_resolve.py).
    - Locate the most-recent critic-response-N.md in the same task directory.

  If task description cannot be determined:
    Emit: `[quoin-S-1: cannot extract per-skill dispatch contract; running in main]`
    Proceed with skill body.

  Otherwise spawn an Agent subagent:
    model: "opus"
    description: "revise — pollution-isolated dispatch"
    prompt: "[no-redispatch]\n/revise\nPlan path: <absolute path to current-plan.md>\nCritic response: <absolute path to critic-response-N.md>"

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
      switch with /model and re-invoke /revise]` and STOP. Do NOT proceed to skill body.
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

## §0″ Minimum-tier guard (execute after §0 / §0c / §0’ if present — before skill body)
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
    description: "revise — min-tier up-dispatch"
    prompt: "[no-redispatch]\n<original user input verbatim>"
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
      switch with /model and re-invoke /revise]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on opus up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from an Opus session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /revise from an Opus session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0doubleprime-end -->

## Session bootstrap

This skill may run in a fresh session. On start:
1. Read `__QUOIN_HOME__/skills/revise/preamble.md` if it exists; if missing or empty, proceed normally. Purely additive cache-warming — every other read in this `## Session bootstrap` section, and every write-site format-kit / glossary reference (per §5.3 / §5.4 write-site instructions), stays in force unchanged. The intent is CROSS-SPAWN cache reuse: spawn N+1 of this skill with a byte-identical task fixture hits cache from spawn N's preamble.md tool_result, within the 5-minute prompt-cache TTL. Within a single spawn there is no cache benefit — savings only materialize on subsequent spawns whose prompt prefix is byte-identical through the preamble read. (Stage 2-alt of pipeline-efficiency-improvements.)
2. Read the task subfolder: resolve the artifact path via `python3 __QUOIN_HOME__/scripts/path_resolve.py --task <task-name> [--stage <N-or-name>]` — then read `<task_dir>/current-plan.md`, latest `<task_dir>/critic-response-*.md`, and any prior critic responses. architecture.md: ALWAYS `<task-root>/architecture.md`. cost-ledger.md: ALWAYS `<task-root>/cost-ledger.md` (line 4 below — NOT edited per D-03). If exit code 2: display stderr verbatim, fall back to task root, ask user to disambiguate.
3. Check knowledge cache for flagged modules (if cache exists), then re-read source code where cache is insufficient
4. Append your session to the cost ledger: `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `revise` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`. If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

<!-- quoin:ledger-self-write -->
5. Read deployed v3 references at session start: `__QUOIN_HOME__/memory/format-kit.md` and `__QUOIN_HOME__/memory/glossary.md`
6. Then proceed with revision

## Model requirement

This skill requires the strongest available model (currently Claude Opus).

## Scope cap (read this before doing any work)

Previous /revise subagent runs timed out mid-stream (Apr 29 09:31 incident —
`Stream idle timeout`). The Anthropic API kills streaming children when a
single inference step stalls long enough; large revision bodies raise that risk.

Hard cap: complete at most ~30-40 tool uses of revision work in this dispatch.
If the critic issues you've been given require more:
  1. Address issues I-01 through I-NN only (the first ~half by complexity).
  2. Mark deferred critic issues clearly in `## Revision history` with a note
     `[deferred to next /revise round]`.
  3. The orchestrator will spawn another /revise round for the remaining issues.
  4. Commit what you have so nothing is lost before returning.

NOTE: If you are running standalone (not via /thorough_plan), there is NO
automatic retry on stream-idle timeout. Commit what you have; the user
re-invokes.

Do NOT silently keep going past 40 tool uses. Stream-idle timeouts produce
partial responses that the parent cannot reliably recover.

## Process

### 1. Read the inputs

- Read `<task_dir>/current-plan.md` — the current plan (where `<task_dir>` is resolved per Session bootstrap step 1)
- Read `<task_dir>/critic-response-<latest>.md` — the most recent critic feedback
- Read any prior critic responses to understand the trajectory of revisions
- **Check the knowledge cache** for modules referenced in critic feedback (if `.workflow_artifacts/cache/` exists):
  - Read `cache/<repo>/<module>/_index.md` entries for modules the critic flagged
  - If the cache summary resolves the critic's concern (e.g., confirms module structure, dependencies, integration points), use it without re-reading source
  - If the cache summary is insufficient or stale, fall through to source reads
- Re-read relevant source code if the critic flagged incorrect assumptions AND the cache was insufficient to resolve them

Format detection: `current-plan.md` may be v2 or v3 format. Apply the §5.7.1 detection rule below before reading.

# v3-format detection (architecture.md §5.7.1 — copy verbatim)
# A file is v3-format iff:
#   - the first 50 lines following the closing `---` of the YAML frontmatter
#     contain a heading matching the regex ^## For human\s*$
# Otherwise the file is v2-format.
# On v3-format detection: read sections per format-kit.md for this artifact type.
# On v2-format (or no frontmatter): read the whole file as legacy v2.
# Detection MUST be string-comparison only — no LLM call (per lesson 2026-04-23
# on LLM-replay non-determinism).

If v3-format: read the body sections per format-kit.md §2 current-plan.md enumeration. If v2-format (legacy): read the whole file as-is and the next /revise write becomes the v2→v3 upgrade point.

### 2. Triage the issues

From the critic response, categorize:

- **CRITICAL issues** — must fix. These block implementation.
- **MAJOR issues** — must fix. These represent significant gaps.
- **MINOR issues** — use judgment:
  - Fix if it's quick and improves the plan
  - Note as "known limitation" if it's out of scope or a deliberate tradeoff
  - Skip if it's stylistic and doesn't affect outcomes

### 3. Revise the plan

First, perform the in-context revision:

**For each CRITICAL and MAJOR issue:**
1. Understand what the critic is really asking for (sometimes the stated issue points to a deeper problem)
2. Read the relevant code again if needed — don't just trust your memory
3. Make the fix in the plan. This might mean:
   - Adding a missing task
   - Modifying an existing task with more detail
   - Adding error handling or failure modes to the integration analysis
   - Adding risks to the risk table
   - Adding tests to the testing strategy
   - Reordering tasks for better de-risking
   - Adding a spike/POC task for an uncertain area

**Preserve what the critic praised.** The "What's good" section tells you what to keep. Don't accidentally regress while fixing issues.

**Don't over-correct.** If the critic said "this section needs more detail," add the right amount of detail — don't triple the length of every section in response. The plan should stay focused and readable.

Then, write the updated plan using the §5.3 5-step Class B mechanism for `<task_dir>/current-plan.md` (where `<task_dir>` is resolved per Session bootstrap step 1):

**Step 1: Body generation.**
Read `__QUOIN_HOME__/memory/format-kit-pitfalls.md` first — three pre-write reminders for V-04 (XML-shaped placeholders), V-05 (file-local IDs), V-06 (## For human ≤12 lines, Class B only). Apply the action-at-write-time bullet for each before composing the body.
Reference files (apply HERE at the body-generation WRITE-SITE — per format-kit.md §1; this is the only place these references apply, per lesson 2026-04-23):
- `__QUOIN_HOME__/memory/format-kit.md` — primitives + standard sections per artifact type
- `__QUOIN_HOME__/memory/glossary.md` — abbreviation whitelist + status glyphs
- `__QUOIN_HOME__/memory/terse-rubric.md` — prose discipline (compose with format-kit per §5)

# V-05 reminder: T-NN/D-NN/R-NN/F-NN/Q-NN/S-NN are FILE-LOCAL.
# When referring to a sibling artifact's task or risk, use plain English (e.g., "the parent plan's T-04"), NOT a bare T-NN token. See format-kit.md §1 / glossary.md.
**Step 1 pre-write sweep:** Before writing, clear stale leftovers from any prior aborted run: `python3 __QUOIN_HOME__/scripts/fsops.py rm "<plan-path>.body.tmp" "<plan-path>.tmp"`.
Compose the format-aware body per format-kit.md §2 `current-plan.md` enumeration. Include the `## Revision history` section (terse numbered list or table per format-kit.md §2) with the new round's changelog appended inside it. DO NOT include the `## For human` block in the body — that's Steps 2–3. Write the body to `<plan-path>.body.tmp` using the Bash tool.

**Step 2: Summary generation (Agent subagent, with empty-output check).**

Read the frozen prompt template from `__QUOIN_HOME__/memory/summary-prompt.md` using
the Read tool. Read the artifact body from `<plan-path>.body.tmp` using the Read tool.
Compose the prompt as: <prompt-template-with-`<<<BODY>>>`-replaced-by-body-text>.

Spawn an Agent subagent with:
  - model: "haiku"
  - description: "Generate ## For human summary"
  - prompt: <composed prompt>
  - additional system instruction prepended to the prompt: "Use temperature 0.0
    (deterministic). Output ONLY the summary text — no preamble, no follow-up
    questions, no chain-of-thought. Do not invent facts not present in the body.
    Do not exceed 8 lines."

Wait for the subagent. Capture its response text as `summary_raw`.

- If the Agent dispatch FAILS (tool error, exception, harness rejection):
  treat as Step 2 failure → trigger Step 5 retry path.
- If `summary_raw.strip()` is EMPTY:
  treat as Step 2 failure → trigger Step 5 retry path.
- Otherwise: proceed to Step 3 with `summary_raw`.

(Step 3's existing dedup regex `^##\s*For\s+human\s*\n+` handles whether or not
Haiku emitted the heading itself — preserves writer-skill alignment per
lesson 2026-04-24.)

**Step 3: Compose and write the single file (with `## For human` heading dedup).**
  (a) Strip a leading `## For human` heading from `summary_raw` if present (regex `^##\s*For\s+human\s*\n+`). Call the result `summary_body`.
  (b) Compose: `<frontmatter (YAML)>\n## For human\n\n<summary_body>\n\n<body from <plan-path>.body.tmp>`.
  (c) Write to `<plan-path>.tmp` using the Write tool.

**Step 4: Structural validation.** Invoke:
  `python3 __QUOIN_HOME__/scripts/validate_artifact.py <plan-path>.tmp`
Exit code 0 = PASS; non-zero = at least one invariant failed (stderr names which).

**Step 5: Retry / English-fallback (failure-class-aware).**
  - Before re-running Step 2, increment the session-state `fallback_fires` field by 1 (atomic-rename pattern; same rules as the Step 5 increment described above). Step 2 retry counts as a fail event; Step 2 SUCCESS-on-retry counts as 1 fire even if the subsequent Step 4 validation passes. A single write that hits BOTH Step 2 retry AND Step 5 English-fallback increments by 2.
  - **Step 2 failure:** re-run Step 2 once (re-spawn the Haiku Agent subagent); if still fails → English-fallback.
  - **V-06/V-07 failures:** re-run Steps 2–4 once.
  - **V-02/V-03/V-05 failures:** re-run Steps 1–4 once with explicit body-discipline instruction.
  - **English-fallback:** v2-style write (no `## For human` block). Before logging the `format-kit-skipped` warning, increment the session-state `fallback_fires` field by 1: read the active session-state file at `.workflow_artifacts/memory/sessions/{today}-{task}.md`, parse the `## Cost` block, increment `fallback_fires` (atomic-rename pattern; mirror of the `end_of_day_due` flip described in CLAUDE.md "Session state tracking"), then proceed. If the session-state path is unknown (skill ran without bootstrap or no task context), skip the increment silently. Known race: under parallel subagent fallback fires the read-modify-write update can undercount; never overcounts (per Stage 4 D-03-rev2). Log `format-kit-skipped` warning. Clean up body.tmp: `python3 __QUOIN_HOME__/scripts/fsops.py rm "<plan-path>.body.tmp"`.

**Step 6: Atomic rename.**
  `python3 __QUOIN_HOME__/scripts/fsops.py finalize "<plan-path>.tmp" "<plan-path>" --cleanup "<plan-path>.body.tmp" "<plan-path>.tmp"`. It renames over any existing file, always removes both temp files, and exits non-zero if the rename failed.

The final `current-plan.md` contains the revised body. Do NOT write a `.original.md` side-file.

### 4. Add the changelog

The changelog entry is now part of the format-aware body produced in Step 1 of the §5.3 write procedure above. Write it inside the `## Revision history` section of the body (format primitive: terse numbered list or table per format-kit.md §2). Content format:

```
Round <N> — <date>
Critic verdict: REVISE
Issues addressed: [CRIT-1] <title> — <how>; [MAJ-1] <title> — <how>
Issues deferred: [MIN-1] <title> — <why>
Changes: <1-2 sentence overview>
```

Do NOT append the changelog as a trailing markdown block after the assembled file — it belongs inside the `## Revision history` body section written in Step 1, assembled into the single file by Step 3.

### 5. Signal readiness

**Plan-path self-check (advisory, IVG-143 I-2).** Before handing off to the next critic round, run `python3 __QUOIN_HOME__/scripts/plan_path_lint.py <plan-path> --format text` against the revised `current-plan.md`, so the author can fix any cited-path typos before `/critic` re-reviews. Surface any `UNRESOLVED` tokens and their hints. Advisory only — ALWAYS continue regardless of the result. Fail-OPEN: exit 2, a missing script, or any crash → emit a one-line `[quoin: plan-path-lint unavailable; proceeding]` warning and continue. Never aborts `/revise` on this check.

After updating the plan, the file is ready for the next critic round. If this is part of `/thorough_plan` orchestration, the orchestrator will invoke `/critic` next.

If running standalone, tell the user:
- What issues were addressed
- What was deferred and why
- Whether you recommend another critic round or if the plan feels ready

## Save session state

Before finishing, write or update `.workflow_artifacts/memory/sessions/<date>-<task-name>.md` with:
- **Status:** `in_progress`
- **Current stage:** `revise` (note the round number, e.g. `revise round 2`)
- **Completed in this session:** which critic issues were addressed
- **Unfinished work:** deferred issues, or "ready for /implement" if converged
- **Decisions made:** rationale for any choices made while addressing feedback

This is what `/end_of_day` reads to consolidate the day's work. Without it, this session is invisible to the daily rollup.

## Important behaviors

- **Be surgical.** Don't rewrite sections that were fine. Targeted fixes, not scorched earth.
- **Re-read code when flagged.** If the critic said your assumptions about the code are wrong, go look at the code again. Don't just rephrase the same wrong thing.
- **Maintain plan coherence.** After multiple rounds of revision, the plan can get inconsistent. Check that task numbering, dependencies, and cross-references still make sense.
- **Track what changed.** The changelog is how the user and future rounds understand the plan's evolution. Don't skip it.
- **Know when to escalate.** If a critic issue requires an architectural change that's beyond the plan's scope, flag it to the user instead of cramming it into the plan.
