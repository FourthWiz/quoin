## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)

<!-- Reference template for review only. The 7 SKILL.md blocks are the source
of truth for downstream tests. Per S-1 plan: this fixture is documentation-only
and is intentionally NOT byte-equal to inserted blocks (per-skill dispatch
contract placeholders are substituted at insert time). -->

This skill runs in the user's current session. If the session is polluted (high
context from prior work), the skill should self-dispatch as a fresh subagent to
avoid paying the pollution tax.

Detection:
  - Sentinel check: if the user's prompt starts with `[no-redispatch]`: skip dispatch
    (reuse existing §0 sentinel — same semantics apply to §0'). Do not run the guard.
  - If §0 (model dispatch) already fired and dispatched to a subagent: this skill is
    already running in a fresh context. Skip §0' (no double-dispatch). Do not run the guard.
  - Otherwise run `python3 __QUOIN_HOME__/scripts/pollution_guard.py` once.
    It scores the current session transcript; threshold POLLUTION_THRESHOLD
    (default: env QUOIN_POLLUTION_THRESHOLD or 5000).
  - Output starting `OVER|`: session is polluted.
  - Any other output (`OK|<score>|<path>`, the fail-OPEN `OK|0|`, empty output, an error): proceed to skill body.

Dispatch action (when pollution detected AND no sentinel AND no prior §0 dispatch):
  Spawn an Agent subagent with:
    model: "opus"
    description: "<skill name> — pollution-isolated dispatch"
    prompt: "[no-redispatch]\n/<skill command> <per-skill dispatch contract fields>"

  Per-skill dispatch contract fields:
    <DISPATCH_CONTRACT_PLACEHOLDER>

  Wait for the subagent. Return its output as your final response. STOP.

Refusal path (cannot extract per-skill fields):
  - If the §0' block cannot determine the required per-skill dispatch fields from
    the current session context (e.g., user said "plan this" with no concrete inputs):
  - Emit warning: `[quoin-S-1: cannot extract per-skill dispatch contract; running in main]`
  - Proceed with skill body (run in polluted main — safer than dispatching with
    ambiguous context).

Fail-OPEN path:
  - If the Agent tool is unavailable or errors during dispatch:
  - Emit warning: `[quoin-S-1: pollution dispatch unavailable; proceeding in current session]`
  - Proceed with skill body.

Otherwise (guard not OVER, OR sentinel present, OR §0 already dispatched,
OR guard unavailable): proceed to skill body.
