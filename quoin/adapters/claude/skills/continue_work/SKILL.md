---
name: continue_work
description: "Resumes context from a prior session: reads recent-sessions.md, presents a session picker, extracts checkpoint summary and recent messages from the JSONL. Use for: /continue_work, 'resume prior session', 'revive old session', 'where was I', 'switch to last session'."
model: sonnet
---

# Continue Work

*Portable intent doc: `quoin/core/skills/continue_work.md`*

Revives context from a prior Claude Code session by reading session history and extracting checkpoint summaries and recent messages.

## §0 Model dispatch (FIRST STEP — execute before anything else)

This skill is declared `model: sonnet`. If the executing agent is running on a model
strictly more expensive than the declared tier, you MUST self-dispatch before doing the
skill's actual work.

Detection:
  - Read your current model from the system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus.
  - Sentinel parsing: the user's prompt is checked for the `[no-redispatch]` family.
      * Bare `[no-redispatch]` (parent-emit form AND user manual override): skip dispatch, proceed to §1 at the current tier.
      * Counter form `[no-redispatch:N]` where N is a positive integer ≥ 2: ABORT (see "Abort rule" below).
      * Counter form `[no-redispatch:1]` is reserved and treated as bare `[no-redispatch]` for forward-compatibility; do not emit it.
  - If current_tier > declared_tier AND prompt does NOT start with any `[no-redispatch]` form:
      Dispatch reason: cost-guardrail handoff. dispatched-tier: sonnet.
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
        model: "sonnet"
        description: "continue_work dispatched at sonnet tier"
        prompt: "[no-redispatch]\n<original user input verbatim>"
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
  - Print the one-line error: `Quoin self-dispatch hard-cap reached at N=<N> in continue_work. This indicates a recursion bug; aborting before any tool calls. Re-invoke with [no-redispatch] (bare) to override.`
  - Then stop. Do NOT proceed to §1.

Manual kill switch:
  - The user can prefix any user-typed slash invocation with bare `[no-redispatch]` to skip dispatch entirely (e.g., `[no-redispatch] /continue_work`).
  - Why this is safe to share syntax with the parent-emit form: memory/dispatch-guide.md §0 verbose reference ("Why the bare [no-redispatch] sentinel is dual-source by design").
  - Use this only when intentionally overriding the cost guardrail (e.g., for one-off debugging on a different tier).

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


  - Worktree-class branch:
      Worktree creation is hook-driven and cannot be skipped by omitting a
      parameter. Use the AskUserQuestion tool to present the user with one
      option:
        (c) `proceed-current-tier` — Skip dispatch, proceed at the current
            (more expensive) tier. This is the only available recovery path.
      Question header: `Subagent dispatch failed (worktree creation). Proceeding at current tier.`
      Note for the user: "Worktree dispatch failed and no retry mechanism
      is available — worktree creation is unconditional in this harness.
      Proceeding at current tier."

  - Other-class path (also: worktree-class after user acknowledges c):
      Do NOT abort the user's invocation.
      Emit the bare warning (verbatim):
        `[quoin-stage-1: subagent dispatch unavailable; proceeding at current tier]`
      If this path was reached via a worktree-class error, ALSO emit the
      classification line (second, separate):
        `[quoin-stage-1: error-class=worktree; user-choice=c; proceeding at current tier]`
      Then proceed to §1 at the current tier (fail-OPEN per I-01).
<!-- §0-worktree-fallback-end -->
Otherwise (already at or below declared tier, OR prompt has [no-redispatch] sentinel, OR dispatch unavailable): proceed to §1 (skill body).
<!-- §0-end -->

## §0‴ Minimum-tier guard (execute after §0 — before any §0-sidecar block and the skill body)
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
    description: "continue_work — min-tier up-dispatch"
    prompt: "[no-redispatch]\n<original user input verbatim>"
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
      switch with /model and re-invoke /continue_work]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on sonnet up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from a Sonnet session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /continue_work from a Sonnet session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0tripleprime-end -->

## Session bootstrap

Locate `<cwd>/.workflow_artifacts/memory/recent-sessions.md`.
Read the last 5 records. Format per record: `<ISO-timestamp> | <session_id>`

## Step 1: Parse last 5 records

Use the Bash tool to read recent-sessions.md:

```sh
tail -5 "<cwd>/.workflow_artifacts/memory/recent-sessions.md" 2>/dev/null
```

**Case C — File absent or < 2 records:**
  Report: "No session history found yet. recent-sessions.md gets a line each time a
  session starts, resumes, ends or compacts. Nothing to revive."
  STOP.

**Case A — All 5 session_ids identical:**
  Report: "Your current session [session_id] is the only recent session.
  No context revival needed."
  STOP.

**Case B — Multiple distinct session_ids:**
  Deduplicate by session_id, keeping most recent timestamp for each.
  Cap at 4 sessions (most recent first). Use AskUserQuestion to present the picker:

  ```
  <!-- decision-gate: best-effort site=continue_work-prompt-1 -->
  AskUserQuestion(
    question="Which session do you want to revive?",
    options=[
      {label: "[<timestamp>] <session_id_1>", description: "<task name or context if available>"},
      {label: "[<timestamp>] <session_id_2>", description: "<task name or context if available>"},
      ...  # up to 4 options
    ]
    # implicit "Other" covers cancel
  )
  ```

  If the user selects "Other" or types "cancel": STOP with "No session selected."

## Step 2: Locate JSONL for selected session

`selected_uuid` = the session_id from the user's choice.

Use Bash tool (fail-OPEN):

```sh
find "$HOME/.claude/projects" -maxdepth 2 -name "${selected_uuid}.jsonl" 2>/dev/null | head -1
```

If not found:
  Report: "JSONL for session [selected_uuid] not found under ~/.claude/projects/.
  The session may have been pruned. Cannot revive."
  STOP.

## Step 3: Extract checkpoint context and last messages

Run inline Python3 via Bash tool heredoc:

```sh
python3 - "$JSONL_PATH" << 'PYEOF'
import json, sys

path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    lines = f.readlines()

entries = []
for l in lines:
    try:
        d = json.loads(l)
        if d.get('type') in ('user', 'assistant'):
            entries.append(d)
    except Exception:
        pass

# Last 20 entries (10 pairs)
recent = entries[-20:]
msg_pairs = []
for d in recent:
    role = d.get('type')
    content = d.get('message', {}).get('content', '')
    if isinstance(content, list):
        text = ' '.join(
            item.get('text', '') for item in content
            if isinstance(item, dict) and item.get('type') == 'text'
        )
    else:
        text = str(content)
    msg_pairs.append(f"[{role}] {text[:500]}")

# Last checkpoint block
checkpoint_summary = "(none found)"
for d in reversed(entries):
    if d.get('type') == 'assistant':
        content = d.get('message', {}).get('content', '')
        if isinstance(content, list):
            text = ' '.join(
                item.get('text', '') for item in content
                if isinstance(item, dict) and item.get('type') == 'text'
            )
        else:
            text = str(content)
        if '## Checkpoint' in text or 'checkpoint saved' in text.lower() or 'checkpoint file' in text.lower():
            start = max(text.lower().find('checkpoint'), 0)
            checkpoint_summary = text[max(0, start-50):start+2000]
            break

print("=== CHECKPOINT SUMMARY ===")
print(checkpoint_summary[:2000])
print("=== RECENT MESSAGES ===")
for p in msg_pairs[-10:]:
    print(p[:500])
PYEOF
```

## Step 4: Present to user

Present the revived context:

```
Reviving session [selected_uuid].

Last checkpoint context:
<checkpoint_summary or '(none found)'>

Recent conversation tail:
<last ~10 message pairs>

To resume working in this context, run /run --resume TASK for task state,
or `claude --resume SESSION_ID` to reopen this conversation,
OR continue here with this context pre-loaded.
```

## Cost recording

Skip — no task context required. (Follows /triage and /capture_insight pattern.)
