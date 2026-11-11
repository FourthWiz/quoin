# Discovery & Serena Refresh Routine

This file documents the `/schedule` registration recipe for an automated weekly
discovery refresh cron routine. The routine re-runs `/discover` (and optionally
Serena re-onboarding) on a schedule to keep discovery memory fresh.

## Execution Environment

**Important constraint:** a cloud-scheduled routine runs in the cloud agent's
sandboxed environment. This environment likely does **NOT** have access to the
user's local Google Drive filesystem or local `uvx`-based Serena MCP.

Consequence:
- `/discover` in the cloud payload may scan no meaningful repos if repos live
  on a Google Drive-mounted path (local-only). The scan will complete but may
  produce an empty or minimal result.
- The Serena re-onboard instruction hits Graceful Absence (no schema loads in the
  sandbox — `ToolSearch select:mcp__serena__activate_project` returns empty).

The routine is therefore **best-effort**: on environments with local Drive and
Serena access it works fully; on sandboxed cloud environments it is effectively
a no-op for both scopes.

**The session-start banner (`S-5`) is the always-present interactive fallback**
and is unaffected by cloud sandboxing. Rely on `S-5` if the cron routine proves
ineffective in your environment.

## /schedule Registration Recipe

Run the following in an interactive Claude Code session to register the routine:

```
/schedule
```

When prompted for the schedule, use:
- **Cron expression:** `QUOIN_DISCOVERY_REFRESH_CRON` default = `0 6 * * 1` (weekly, Monday 06:00)
- **Routine name:** `quoin-discovery-refresh`
- **Payload prompt:** (see Routine Payload section below)

To change the schedule, unregister and re-register with the new cron expression,
or set `QUOIN_DISCOVERY_REFRESH_CRON` in your environment before registration.

## Routine Payload Prompt

Use this self-contained prompt as the routine's payload:

```
[no-redispatch]
You are a quoin maintenance agent. Run the following steps:

1. Run /discover on the current project root to refresh discovery memory and reset the staleness clock.
   Command: /discover

2. Serena re-onboarding (Graceful Absence — do nothing if unavailable):
   - Run: ToolSearch select:mcp__serena__activate_project
   - If schema loads: run mcp__serena__activate_project, mcp__serena__onboarding, mcp__serena__initial_instructions
     then write/update .workflow_artifacts/memory/serena-onboarded.md with current timestamp.
   - If schema does not load: skip silently (do not mention Serena).

Note: this routine runs unattended. If the environment lacks access to local repos
or Serena MCP, the routine will complete silently with no meaningful updates.
The session-start banner (S-5) handles interactive staleness detection.
```

Besides age, a `.workflow_artifacts/memory/discovery-dirty` marker makes discovery
stale immediately. Light `/run` writes it after a commit that changed the project's
structure (top-level directories or packages, dependency manifests, public
interfaces); the next `/run` refreshes discovery, the S-5 banner and
`/start_of_day` report it as stale, and `/discover` deletes it.

## Schedule

Default cron expression: `0 6 * * 1` (weekly, every Monday at 06:00 local time).

Override: set `QUOIN_DISCOVERY_REFRESH_CRON` before registration.

## Disable Instructions

To stop automated refreshes:
- **Delete the routine:** use `/schedule` → manage routines → delete `quoin-discovery-refresh`.
- **Or:** set `QUOIN_DISCOVERY_REFRESH_DISABLE=1` in your shell environment — this suppresses the
  `S-5` session-start banner AND causes `discovery_staleness.py` to return `fresh` unconditionally
  (the routine itself still runs but `/discover` will be a no-op if the script disables it).

## Environment Knobs

| Env Var | Default | Purpose |
|---------|---------|---------|
| `QUOIN_DISCOVERY_REFRESH_DISABLE` | unset | Master off switch: `=1` suppresses all banners and forces fresh verdict |
| `QUOIN_DISCOVERY_STALE_DAYS` | 7 | Days before discovery memory is considered stale |
| `QUOIN_SERENA_STALE_DAYS` | 30 | Days before Serena memory is considered stale (present-but-stale path) |
| `QUOIN_DISCOVERY_AUTOREFRESH` | unset | `=1` lets /start_of_day auto-run /discover without asking |
| `QUOIN_DISCOVERY_REFRESH_CRON` | `0 6 * * 1` | Cron expression for the scheduled routine |

## Deployment

This file is deployed by `install.sh` to `__QUOIN_HOME__/memory/discovery-refresh-routine.md`.
It is registered in `TIER1_MEMORY_FILES` in `src/quoin/installer.py`.
