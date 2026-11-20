# End-of-Day Refresh Routine

This file documents the OPT-IN `/schedule` registration recipe for an automated
end-of-day (`/end_of_day`) cron routine. It is OFF BY DEFAULT — no routine is
registered unless you explicitly run `/schedule` and follow the recipe below.

## Primary Mechanism vs. This Cron (read this first)

**`/end_of_task`'s session flag-flip (IVG-137) is the primary, reliable mechanism**
for keeping session state from silently accumulating as orphaned/unflushed work —
it runs deterministically whenever a task is finalized, per-task, with no scheduling
or unattended-execution risk. This cron routine is a **secondary, opt-in** backstop
for the case where `/end_of_day` itself is never run interactively (e.g., you close
out work without explicitly wrapping up the day). Prefer running `/end_of_day`
yourself, or relying on the `/end_of_task` flip; treat this routine as a convenience,
not a guarantee.

## Execution Environment

**Important constraint:** a cloud-scheduled routine runs in the cloud agent's
sandboxed environment. This environment likely does **NOT** have access to the
user's local Google Drive-mounted `.workflow_artifacts/` filesystem.

Consequence:
- `/end_of_day` in the cloud payload may find no session files, no daily cache
  directory, and no insights to promote if `.workflow_artifacts/` lives on a
  Google Drive-mounted path (local-only). The routine will complete but is likely
  a no-op in that environment.
- `/end_of_day` also has interactive `AskUserQuestion` prompts (Step 3b lesson
  promotion, Step 4 confirmation) that a fully unattended cron run cannot answer.
  An unattended run may stall on or silently skip these prompts depending on the
  cloud agent's non-interactive-prompt handling.

The routine is therefore **best-effort at most**: on environments with local Drive
access and no interactive-prompt blocking it may work; on sandboxed cloud
environments (the common case for Drive-mounted project homes like this one) it is
effectively a no-op. This mirrors the same caveat documented for the discovery
refresh routine — see `discovery-refresh-routine.md`.

**`/end_of_task`'s flag-flip (T-03, IVG-137) is the reliable fallback** — it does
not depend on scheduling or unattended execution at all.

## /schedule Registration Recipe

Run the following in an interactive Claude Code session to register the routine:

```
/schedule
```

When prompted for the schedule, use:
- **Cron expression:** `QUOIN_EOD_REFRESH_CRON` default = `0 22 * * *` (daily, 22:00 local time)
- **Routine name:** `quoin-eod-refresh`
- **Payload prompt:** (see Routine Payload section below)

To change the schedule, unregister and re-register with the new cron expression,
or set `QUOIN_EOD_REFRESH_CRON` in your environment before registration.

## Routine Payload Prompt

Use this self-contained prompt as the routine's payload:

```
[no-redispatch]
You are a quoin maintenance agent. Run the following step:

1. Run /end_of_day on the current project root to roll up any unflushed session
   state into the daily cache.
   Command: /end_of_day

Note: this routine runs unattended. If the environment lacks access to the
project's local .workflow_artifacts/ (e.g. a Google Drive-mounted path), the
routine will complete silently with no meaningful updates. If /end_of_day's
interactive prompts (lesson promotion, confirmation) cannot be answered
unattended, accept the default/skip path rather than blocking.
```

## Schedule

Default cron expression: `0 22 * * *` (daily, every day at 22:00 local time).

Override: set `QUOIN_EOD_REFRESH_CRON` before registration.

## Disable Instructions

To stop automated refreshes:
- **Delete the routine:** use `/schedule` → manage routines → delete `quoin-eod-refresh`.
- **Or:** simply don't register it — this routine ships doc-only and off by default;
  no code path auto-registers it.

## Environment Knobs

| Env Var | Default | Purpose |
|---------|---------|---------|
| `QUOIN_EOD_REFRESH_CRON` | `0 22 * * *` | Cron expression for the scheduled routine |
| `QUOIN_DISABLE_EOD_RECONCILE` | unset | `=1` disables the covered-but-due reconciliation pre-pass inside `/end_of_day --recover-orphans` (IVG-137 T-02) — unrelated to this cron, but see `hooks-table.md` for the full knob inventory |
| `QUOIN_DISABLE_EOT_FLAG_FLIP` | unset | `=1` disables `/end_of_task`'s session flag-flip (IVG-137 T-03) — the PRIMARY mechanism this cron backstops |

## Deployment

This file is deployed by `install.sh` to `${CLAUDE_PLUGIN_ROOT}/memory/eod-refresh-routine.md`.
It is registered in `TIER1_MEMORY_FILES` in `src/quoin/installer.py`.
