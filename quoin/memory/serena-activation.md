# Serena Code-Intelligence Activation Protocol

Serena is an optional MCP server that gives Claude symbol-level code intelligence
(find/rename symbols, cross-reference search, language-server-backed navigation).
This file documents how to detect Serena, activate it, and when to do nothing.

## Is Serena Available?

Serena MCP tools are **deferred** — they do not appear in the tool list until their
schema is explicitly loaded. The authoritative runtime probe is:

```
ToolSearch select:mcp__serena__activate_project,mcp__serena__initial_instructions
```

- **Schema loads successfully** → Serena is PRESENT and callable this session.
- **Returns empty / no schema** → Serena is ABSENT. Do nothing (see Graceful Absence below).

A skill CANNOT enumerate its own loaded MCP tools by introspection — the ToolSearch
probe is the only deterministic in-skill detection method. The probe also serves as the
load step, so after a successful probe the tools are callable immediately.

**Install-time filesystem pre-check** (used by `/init_workflow` only, not at task start):
- Plugin `.mcp.json`: `${CLAUDE_PLUGIN_ROOT}/plugins/marketplaces/claude-plugins-official/external_plugins/serena/.mcp.json`
- `uvx` on PATH: `command -v uvx`
If the `.mcp.json` is present and `uvx` is available but the ToolSearch probe fails, Serena
is installed but not yet loaded — a session restart is required.

## Per-Session Activation Protocol

Run this at the start of a coding task **only when the ToolSearch probe succeeds** (Serena present):

1. **Load schemas via ToolSearch** (step above — doubles as the load step):
   ```
   ToolSearch select:mcp__serena__activate_project,mcp__serena__initial_instructions
   ```

2. **Call `activate_project` FIRST** — binds the project so symbol tools target the right
   codebase. Pass the bare project directory name (e.g. `quoin`, `Codex_workflow`), NOT
   the absolute path. Check `.serena/project.yml` for the `project_name:` field if present
   and use that value.
   ```
   mcp__serena__activate_project(project="<project-dirname>")
   ```

3. **If `activate_project` reports onboarding needed** — call `mcp__serena__onboarding`
   and follow its steps to generate project memories before proceeding. Onboarding is
   what makes Serena "sticky" for the project; without it Serena has no project context.
   ```
   mcp__serena__onboarding()
   ```

4. **Call `initial_instructions` SECOND** — read the Serena manual. The tool self-describes
   as "call immediately after you are given your task". Follow the returned instructions.
   ```
   mcp__serena__initial_instructions()
   ```

5. **Use Serena symbol tools** in preference to built-in grep/read for code navigation:
   `mcp__serena__find_symbol`, `mcp__serena__get_symbols_overview`,
   `mcp__serena__find_referencing_symbols`, `mcp__serena__find_implementations`, etc.

## Graceful Absence Rule

If the `ToolSearch select:mcp__serena__activate_project` probe loads no schema (Serena
tools absent), **do nothing** — do not call `activate_project`, do not call
`initial_instructions`, do not call `onboarding`, do not mention Serena. Fall through
to built-in tools silently.

Never instruct a user to call Serena tools that don't exist in their session.

## Refresh / Re-onboarding

### When to refresh

Two paths trigger a Serena refresh:

**Path 1 — Present-but-stale:** `.workflow_artifacts/memory/serena-onboarded.md` marker EXISTS
AND its mtime is older than `QUOIN_SERENA_STALE_DAYS` (default 30 days) AND a ToolSearch probe
loads a schema. This is the normal periodic re-onboarding path — positive evidence Serena was
previously onboarded for this project.

**Path 2 — First-time onboarding (absent marker):** marker is ABSENT AND ToolSearch probe loads a
schema. The hook (`sessionstart.sh` S-5) does NOT emit a Serena banner for absent markers (Graceful
Absence — the hook cannot confirm Serena is installed from a non-session process). `/start_of_day`
Step 6b handles first-time onboarding: it probes via ToolSearch and if successful, offers the
"Set up / Refresh Serena memory" option.

**Graceful Absence:** if `ToolSearch select:mcp__serena__activate_project` loads no schema → do
nothing, no marker write, no mention of Serena.

### How to refresh (§Refresh procedure)

```python
# 1. Probe and load schemas
ToolSearch select:mcp__serena__activate_project,mcp__serena__onboarding,mcp__serena__initial_instructions

# 2. Activate the project
mcp__serena__activate_project(project="<project-dirname>")

# 3. Run onboarding (re-scans project memories)
mcp__serena__onboarding()

# 4. Read the Serena manual
mcp__serena__initial_instructions()

# 5. Write/update the staleness marker
#    Path: .workflow_artifacts/memory/serena-onboarded.md
#    Content: "Serena onboarded for <project-dirname>.\nTimestamp: <ISO-UTC>\n"
```

### Cost and throttle

Re-onboarding is expensive (full project re-scan). Guard with the marker:
- Prompt-only in interactive sessions (NEVER auto-run — D-04).
- At most once per `QUOIN_SERENA_STALE_DAYS` window.
- Cron routine may run it unattended (see `discovery-refresh-routine.md`).

### Marker file

Path: `.workflow_artifacts/memory/serena-onboarded.md`

Written by `/init_workflow` Step 6.5 on first successful onboarding (branches b and c), and by
`/start_of_day` Step 6b when the user chooses "Set up / Refresh Serena memory". The hook and
staleness script read its mtime to determine whether Serena is present-but-stale; an absent marker
means not-yet-onboarded (or marker was lost).

## Why Both Calls Are Required

The root cause of Serena silently not being used was that **neither call was instructed
anywhere** in quoin's workflow rules. The CLAUDE.md conditional block now instructs both,
in the evidence-based order documented here:

- `activate_project` without `initial_instructions` → project bound but no Serena manual read;
  Claude won't know to prefer Serena symbol tools.
- `initial_instructions` without `activate_project` → manual read but no project bound;
  symbol tools return "No active project" errors.
- Both together, in the right order → fully functional Serena session.
