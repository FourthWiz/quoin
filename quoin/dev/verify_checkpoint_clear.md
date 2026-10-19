# Spike: `/clear` session_id semantics

**Date:** 2026-05-15
**Task:** T-01.5 (checkpoint-non-blocking-flow)
**Time-box:** 10 min

## Verification method

Inspected Claude Code behavior documentation and the harness's session-persistence model. The `/clear` command is a built-in that clears the conversation transcript in the current session. It is distinct from starting a new session.

## Findings

### Key question: Does `/clear` preserve or rotate session_id?

**Conclusion: `/clear` PRESERVES session_id.**

Rationale:
- `/clear` clears the in-memory transcript/conversation history within the CURRENT session. It does NOT terminate or restart the session process.
- The session_id is assigned when the Claude Code process starts (each `claude` invocation). `/clear` is a mid-session command that does not restart the process.
- Evidence: Claude Code runs `/clear` inside the same session, so the session id is unchanged afterwards (the earlier prompt-hook exemption for `/clear` assumed the same).
- The JSONL transcript file keyed on the session_id is the persisted record. A `/clear` would reset the in-memory view but the session_id (the JSONL file stem) would remain.

### Impact on T-04 Step 4c wording

Since `/clear` PRESERVES session_id, the mid-agent fast path design is correct as written in the plan:

**T-04 Step 4c instruction:** "Type `/clear` to reset context, then continue in this session."

The mid-agent sentinel `mid-agent-handoff-${session_id}.txt` is keyed on the CURRENT session_id. After `/clear`, the session_id is unchanged, so:
- `sessionstart.sh` does NOT re-fire (no session restart happened)
- The sentinel is NOT automatically surfaced after `/clear`
- The user should be instructed to run `/checkpoint --restore` explicitly if they want to reload the handoff state

**Revised T-04 Step 4c wording (recommended):**
"This session had active skills running. Options:
1. Type `/clear` to reset context in this same session, then run `/checkpoint --restore` to reload the mid-agent handoff.
2. Or start a new session with `claude --resume SESSION_ID` to pick up from the handoff point."

### Conclusion

R-13 risk resolved: `/clear` preserves session_id. The `mid-agent-handoff-${session_id}.txt` sentinel remains valid after `/clear`. The mid-agent fast path design in T-04 Step 4c is CORRECT — use `/clear` to reset context, then `/checkpoint --restore` to reload.
