---
name: init_workflow
description: "Bootstraps the workflow in a project (.workflow_artifacts/, /discover, quickstart). Use for: /init_workflow, 'set up dev workflow'. Needs install.sh first (rules in ${CLAUDE_PLUGIN_ROOT}/CLAUDE.md)."
model: opus
---

# Initialize Development Workflow

*Portable intent doc: `quoin/core/skills/init_workflow.md`*

You set up the complete development workflow system in a project folder. This is the one-time bootstrap that makes all workflow commands available for a project.

## When to use

- First time setting up the workflow in a new project
- User says `/init_workflow`, "set up the workflow", "initialize", etc.
- A project folder exists but has no `quoin/` structure yet

## Prerequisites

`install.sh` must have been run first. In user scope it installs skills to `${CLAUDE_PLUGIN_ROOT}/skills/` and writes workflow rules to `${CLAUDE_PLUGIN_ROOT}/CLAUDE.md`; in project scope it installs to `<project>/.claude/skills/` and writes workflow rules to `<project>/CLAUDE.md`. This skill handles per-project initialization only — not the one-time machine setup.

## Session bootstrap

If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

Cost tracking (conditional): `/init_workflow` initializes a project, not a task. Cost tracking requires a task context. Append to the cost ledger only if you were invoked as part of a task (e.g., via `/run`). Otherwise, skip cost recording. If the condition holds: append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `init-workflow` — format/rules: `${CLAUDE_PLUGIN_ROOT}/memory/cost-ledger-format.md`.

<!-- quoin:ledger-self-write -->

## Process

### Step 1: Ensure project is initialized with `/init`

Check if the project has a `CLAUDE.md` file at its root. If it does NOT exist:

1. **STOP** and tell the user:
   > "This project hasn't been initialized with Claude Code yet. Please run `/init` first to create a `CLAUDE.md`, then re-run `/init_workflow`."
2. Do not proceed with subsequent steps. The `/init` command sets up the project-level `CLAUDE.md` and `.claude/` directory that the workflow depends on.

If `CLAUDE.md` already exists, skip this step — the project is already initialized. Proceed to Step 2.

This ensures the standard Claude Code foundation is in place before layering on the quoin setup.

### Step 2: Detect project root

Identify the project root. This is typically:
- The current working directory
- Or the folder the user specified

Confirm with the user if ambiguous:
> "I'll set up the workflow in `<path>`. Is this the right project root?"

### Step 2b: Ancestor .workflow_artifacts/ check

Before creating or using `.workflow_artifacts/` at the detected root, walk upward
from the project root, checking each ancestor directory (up to $HOME exclusive) for
a `.workflow_artifacts/` subdirectory: check `../.workflow_artifacts`,
`../../.workflow_artifacts`, and so on. (Note: the Python `_find_nested_ancestor()` in
`path_resolve.py` has no $HOME cap; this $HOME bound applies only to the SKILL.md
guidance for human-readable clarity. The cap is an adapter-layer policy — see D-04.)

If any ancestor directory contains `.workflow_artifacts/`, a parent project already
owns the artifact tree. In this case:

1. Warn the user:
   > ⚠️ A parent-level `.workflow_artifacts/` already exists at `<ancestor-path>`.
   > Using that as the project root to avoid creating nested artifact directories.
2. Set the **effective project root** to `<ancestor-path>` for all subsequent steps.
3. Do NOT create a new `.workflow_artifacts/` at the originally-detected root.

If no ancestor has `.workflow_artifacts/`, proceed normally with the detected root.

### Step 3: Check for existing setup and detect legacy layout

**Case: current layout (`memory/` at project root)**
If `memory/` exists at the project root (but `.workflow_artifacts/` does not), offer migration:

> ⚠️  Legacy layout detected: `memory/` exists at project root.
> The new layout consolidates all workflow artifacts under `.workflow_artifacts/`.
> I can migrate now:
>   - `memory/` → `.workflow_artifacts/memory/`
>   - `finalized/` → `.workflow_artifacts/finalized/` (if it exists)
> Your accumulated knowledge will be preserved.
> Migrate now? (yes/no)

Migration commands (on yes):
```bash
mkdir -p .workflow_artifacts
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py mv "memory" ".workflow_artifacts/memory"
[ -d finalized ] && python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py mv "finalized" ".workflow_artifacts/finalized"
```

**Task folder detection** — also scan the project root for task folders to migrate:
- Candidate: any directory at project root that is NOT a git repo (`[ ! -d "$dir/.git" ]`) and NOT `.workflow_artifacts` itself
- Qualifies as a task folder if it contains BOTH `current-plan.md` AND at least one of `critic-response-*.md` or `review-*.md`
- Also qualifies: any non-git-repo directory containing a `finalized/` subdirectory (parent task container)
- Present detected list to user, ask confirmation per folder before moving
- Move confirmed folders: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py mv "<task-name>" ".workflow_artifacts/<task-name>"`

**Legacy detection:** Check for `quoin/memory/` — this is the old layout where memory was nested inside quoin. If found:

```
⚠️  Legacy layout detected: quoin/memory/ exists.
    The new layout keeps memory/ under .workflow_artifacts/ at the project root.
    I can migrate it now: move quoin/memory/ → .workflow_artifacts/memory/
    Your accumulated knowledge (lessons, sessions, etc.) will be preserved.
    Migrate now? (yes/no)
```

If the user confirms, run:
```bash
mkdir -p .workflow_artifacts
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py mv "quoin/memory" ".workflow_artifacts/memory"
```

If `.workflow_artifacts/memory/` already exists (partial migration), merge by copying missing files only — never overwrite existing ones.

**Old symlinks cleanup:** Check for `.claude/skills/` containing symlinks into `quoin/skills/`. If found, remove them — skills are now global at `${CLAUDE_PLUGIN_ROOT}/skills/`:
```bash
for f in .claude/skills/*/; do
  [ -L "${f%/}" ] && python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py rm "${f%/}"
done
```

**Old skills directory:** If `quoin/skills/` exists, it is no longer needed. Offer to remove it:
```
quoin/skills/ is no longer needed (skills live at ${CLAUDE_PLUGIN_ROOT}/skills/).
Remove it? (yes/no)
```

**Re-init of current layout:** If `.workflow_artifacts/` already exists at the project root, tell the user:
> "This project already has a .workflow_artifacts/ setup. Re-initializing will preserve all memory files. Continue?"

**Never overwrite `.workflow_artifacts/memory/`.** It contains accumulated knowledge.

### Step 4: Configure Claude Code permissions

Create or update `.claude/settings.json` at the project root to allow workflow tool usage and deny destructive commands:

```bash
mkdir -p .claude
```

Then write `.claude/settings.json` using python3 (merge with existing if present):

```python
import json, os

path = ".claude/settings.json"
settings = json.load(open(path)) if os.path.exists(path) else {}

perms = settings.setdefault("permissions", {})
allow = perms.setdefault("allow", [])
deny = perms.setdefault("deny", [])

for p in ["Read", "Glob", "Grep", "Edit", "Write", "Bash(*)", "WebFetch", "WebSearch"]:
    if p not in allow: allow.append(p)

for p in [
    "Bash(rm:*)", "Bash(rm -rf:*)", "Bash(rmdir:*)",
    "Bash(git push --force:*)", "Bash(git push -f:*)",
    "Bash(git reset --hard:*)", "Bash(git clean -f:*)", "Bash(git clean -fd:*)",
    "Bash(git checkout -- .:*)", "Bash(git branch -D:*)",
    "Bash(chmod:*)", "Bash(chown:*)", "Bash(kill -9:*)",
    "Bash(killall:*)", "Bash(mkfs:*)", "Bash(dd:*)"
]:
    if p not in deny: deny.append(p)

# json.dump never emits trailing commas; if a human later hand-edits this file, run python3 -m json.tool to validate.
json.dump(settings, open(path, "w"), indent=2)
```

Run this with `python3 -c "..."` or write the file directly if python3 is unavailable. Tell the user what was configured.

### Step 5: Create the directory structure

Create the memory structure under `.workflow_artifacts/` at the project root:

```
<project-root>/
├── .workflow_artifacts/
│   ├── QUICKSTART.md              ← command reference (copied from user-level path, deployed by install.sh)
│   ├── memory/
│   │   ├── sessions/                  ← per-session state files
│   │   ├── daily/                     ← daily rollup from /end_of_day
│   │   ├── weekly/                    ← weekly rollup from /weekly_review
│   │   ├── repos-inventory.md         ← filled by /discover
│   │   ├── architecture-overview.md   ← filled by /discover
│   │   ├── dependencies-map.md        ← filled by /discover
│   │   ├── git-log.md                 ← filled by /discover, updated by /end_of_day
│   │   ├── lessons-learned.md         ← accumulates over time
│   │   ├── workflow-rules.md          ← workflow memory for Claude
│   │   └── workflow-suggestions.md   ← Tier 3 insight suggestions
```

Note: Skills live at `${CLAUDE_PLUGIN_ROOT}/skills/` (installed by `install.sh`), and the QUICKSTART command
reference is deployed by `install.sh` to `${CLAUDE_PLUGIN_ROOT}/QUICKSTART.md`. The interactive HTML guide lives
in your Quoin source clone at `<your-quoin-clone>/Workflow-User-Guide.html` — the only artifact
/init_workflow still references from the source clone.

Skills are already at `${CLAUDE_PLUGIN_ROOT}/skills/` (installed by `install.sh`). Do not create a `skills/` directory in the project.

```bash
mkdir -p .workflow_artifacts/memory/sessions .workflow_artifacts/memory/daily .workflow_artifacts/memory/weekly
```

For files that should start empty but exist as placeholders:
- `.workflow_artifacts/memory/lessons-learned.md` — create with the template header:
  ```markdown
  # Lessons Learned

  Accumulated insights from completed tasks. Read by /plan and /critic at the start of every session.

  <!-- Add entries using the format:
  ## <date> — <task-name>
  **What happened:** <the surprise, failure, or insight>
  **Lesson:** <the reusable takeaway>
  **Applies to:** <which skills should pay attention>
  -->
  ```
- `.workflow_artifacts/memory/workflow-rules.md` — copy from `quoin/memory/workflow-rules.md` if it exists in the source, or create with header:
  ```markdown
  # Workflow Rules

  Reference summary of the quoin system for Claude.
  See ${CLAUDE_PLUGIN_ROOT}/memory/workflow-catalog.md for the full rules (plus this project's own workflow-rules file, deployed as `<project>/CLAUDE.md` in project scope or `${CLAUDE_PLUGIN_ROOT}/CLAUDE.md` in user scope).
  ```
- `.workflow_artifacts/memory/workflow-suggestions.md` — create with the template header:
  ```markdown
  # Workflow Suggestions

  Suggestions for improvements to the quoin itself, surfaced during project work.
  These are Tier 3 insights — things that would benefit the workflow repo, not just this project.

  **Claude writes here automatically.** You decide whether to apply changes to the workflow repo manually.
  Review suggestions, then update the Status field: `accepted | rejected | deferred`.

  <!-- Entry format:
  ## <YYYY-MM-DD> — <source-task>
  **Suggestion:** <what should change in the workflow>
  **Why:** <the observation that triggered this>
  **Affects:** <which SKILL.md file or CLAUDE.md section>
  **Status:** surfaced
  -->
  ```
- `.workflow_artifacts/memory/repos-inventory.md` — create empty, will be populated by /discover
- `.workflow_artifacts/memory/architecture-overview.md` — create empty, will be populated by /discover
- `.workflow_artifacts/memory/dependencies-map.md` — create empty, will be populated by /discover
- `.workflow_artifacts/memory/git-log.md` — create empty, will be populated by /discover

### Step 5.5: Add `.workflow_artifacts` to project's `.gitignore`

```bash
if [ -f .gitignore ]; then
  grep -qxF '.workflow_artifacts/' .gitignore || echo '.workflow_artifacts/' >> .gitignore
else
  echo '.workflow_artifacts/' > .gitignore
fi
```

This ensures `.workflow_artifacts/` is gitignored in every project.

### Step 6: Run /discover

Before invoking `/discover`, handle the bootstrap marker (crash-safe de-dup with `/discover`'s repo-spec offer, D-05):
1. **Stale pre-clear:** drop any marker left by a prior aborted bootstrap: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py rm ".workflow_artifacts/.init-bootstrap-active"`
2. **Write a fresh marker:** `: > .workflow_artifacts/.init-bootstrap-active` (create empty; `.workflow_artifacts/` is already gitignored)

This marker signals to `/discover` that repo-spec seeding is owned by `/init_workflow` Step 6.7, so `/discover` MUST suppress its own repo-spec draft/refresh offer while the marker is present AND self-clear it after honoring — the concrete, crash-safe de-dup mechanism for R-04/R-31/R-38 (see D-05). The marker path is the shared signal both skills key on.

Automatically invoke `/discover` to scan all repositories in the project folder. This populates:
- `.workflow_artifacts/memory/repos-inventory.md`
- `.workflow_artifacts/memory/architecture-overview.md`
- `.workflow_artifacts/memory/dependencies-map.md`
- `.workflow_artifacts/memory/git-log.md`

Tell the user:
> "Running /discover to scan your codebase..."

If /discover finds no repos (no git repositories in the project folder), that's fine — the memory files stay empty and will be populated when repos are added.

### Step 6.5: Offer/activate Serena code-intelligence

**Read `.workflow_artifacts/.serena-skip` first.** If this sentinel file exists, the user previously chose to skip Serena setup for this project — print one line `[quoin: Serena setup skipped per prior choice]` and proceed to Step 7.

Otherwise, detect Serena using `ToolSearch select:mcp__serena__activate_project`:

**Branch (a) — Serena ABSENT (no schema AND no plugin file):**

If the ToolSearch probe returns no schema, run a filesystem pre-check:
- Plugin file: `${CLAUDE_PLUGIN_ROOT}/plugins/marketplaces/claude-plugins-official/external_plugins/serena/.mcp.json`
- `uvx` on PATH: `command -v uvx`

If neither the schema nor the plugin file is found, offer installation:

```
<!-- decision-gate: best-effort site=init_workflow-prompt-1 -->
AskUserQuestion(
  header="Serena",
  question="Serena gives Claude symbol-level code intelligence (find/rename symbols, references). It's not installed. Install it?",
  multiSelect=false,
  options=[
    {label: "Install Serena", description: "Walk me through installing Serena (requires uv/uvx)."},
    {label: "Skip for now",   description: "Skip Serena setup for this project."}
  ]
)
```

On **"Skip for now"**: write `.workflow_artifacts/.serena-skip` (creates the file, empty is fine) so Step 6.5 short-circuits on any re-run of `/init_workflow` for this project. Print `[quoin: Serena setup skipped per user choice]` and proceed to Step 7.

On **"Install Serena"**: guide the user through the following steps (Serena is a Claude Code plugin launched via `uvx` — quoin cannot install it unattended):

1. **Install `uv`/`uvx`** (if not already present — `command -v uvx` fails):
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   # then restart your shell, OR:
   brew install uv
   ```

2. **Install the Serena plugin** in Claude Code. Preferred: run `/plugin` in your Claude Code session, browse to `claude-plugins-official`, and install `serena`. Manual fallback — add to `${CLAUDE_PLUGIN_ROOT}/.mcp.json` (or the project `.mcp.json`):
   ```json
   {
     "mcpServers": {
       "serena": {
         "command": "uvx",
         "args": ["--from", "git+https://github.com/oraios/serena", "serena", "start-mcp-server", "--open-web-dashboard", "false"]
       }
     }
   }
   ```

   > **Dashboard flag:** `--open-web-dashboard false` keeps the Serena web dashboard running (accessible at its local URL, typically `http://localhost:8080`) but suppresses the auto-open behavior that would otherwise launch a browser tab on every Claude Code session start. If you want to fully disable the dashboard (no local server, no URL), use `--enable-web-dashboard false` instead.

3. **⚠️ SESSION RESTART REQUIRED.** MCP servers load only at session start. Serena tools will NOT be available until you restart Claude Code. After restarting, re-run `/init_workflow` — Step 6.5 will detect Serena as present (branch b) and complete onboarding automatically.

Also idempotently append `.serena/` to the target project's `.gitignore` (see below) on this path, so the user's Serena config isn't accidentally committed before onboarding runs.

**Branch (b) — Serena PRESENT, not yet onboarded:**

If the ToolSearch probe succeeds and `activate_project` returns a message containing "Onboarding has not been performed":

```python
# Load all needed Serena tools
ToolSearch select:mcp__serena__activate_project,mcp__serena__onboarding,mcp__serena__initial_instructions

# Bind the project (pass bare dirname or the name from .serena/project.yml if present)
mcp__serena__activate_project(project="<project-dirname>")

# Run onboarding to generate project memories
mcp__serena__onboarding()   # follow its steps

# Confirm activation
mcp__serena__initial_instructions()
```

Tell the user onboarding is complete and Serena is now active for the project. This is the most common real-world case: Serena installed but never set up for the project.

Write the staleness marker after successful onboarding:
```
# Write/update .workflow_artifacts/memory/serena-onboarded.md
# Content: "Serena onboarded for <project-dirname>.\nTimestamp: <ISO-UTC>\n"
```

**Branch (c) — Serena PRESENT, already onboarded:**

If the ToolSearch probe succeeds and `activate_project` does NOT report onboarding-needed, confirm activation silently:

```python
ToolSearch select:mcp__serena__activate_project
mcp__serena__activate_project(project="<project-dirname>")
```

Print one line: `[quoin: Serena active for <project-dirname> (already onboarded)]`.

Write/update the staleness marker (resets the staleness clock):
```
# Write/update .workflow_artifacts/memory/serena-onboarded.md
# Content: "Serena onboarded for <project-dirname>.\nTimestamp: <ISO-UTC>\n"
```

Proceed to Step 7.

**Target-project `.gitignore` (all setup paths — a/install, b, c):**

On any Serena-setup path, idempotently append `.serena/` to the target project's `.gitignore`:
```bash
if [ -f .gitignore ]; then
  grep -qxF '.serena/' .gitignore || echo '.serena/' >> .gitignore
else
  echo '.serena/' >> .gitignore
fi
```
Serena config and memories are machine-local; they should not be committed to the project repo by default.

### Step 6.6: Offer desktop-commander setup

desktop-commander is an MCP server that gives Claude terminal-process and filesystem tools (long-running processes, REPLs, file search). Never skip this step silently: either it is configured, the user opted out, or the user is asked.

**Read `.workflow_artifacts/.desktop-commander-skip` first.** If this sentinel exists, the user previously chose "Never ask again" for this project: print `[quoin: desktop-commander setup skipped per prior choice]` and proceed to Step 6.7.

Otherwise detect it. It counts as configured if ANY of these hits:
- `ToolSearch select:mcp__desktop-commander__get_config` returns a schema;
- `claude mcp list 2>/dev/null | grep -qi desktop-commander` exits 0 (covers user, local and project scopes);
- the project `.mcp.json` has a `desktop-commander` key under `mcpServers`.

If configured: print `[quoin: desktop-commander configured]` and proceed to Step 6.7.

If not configured and the incoming prompt carries the `[autonomous]` sentinel: do not ask. Print `[quoin: desktop-commander not configured — skipped under autonomous]`, write no sentinel, and proceed to Step 6.7. Otherwise ask once:

```
<!-- decision-gate: best-effort site=init_workflow-prompt-4 -->
AskUserQuestion(
  header="Desktop Cmdr",
  question="desktop-commander gives Claude terminal-process and filesystem tools. It isn't configured. Set it up?",
  multiSelect=false,
  options=[
    {label: "Install now",       description: "Show me the install command (requires Node.js/npx)."},
    {label: "Skip for now",      description: "Skip this time; ask again on the next /init_workflow run."},
    {label: "Never ask again",   description: "Don't offer desktop-commander for this project again."}
  ]
)
```

On **"Install now"**: print these instructions (quoin does not run the install itself, it changes the user's MCP config):

1. Run in a terminal: `claude mcp add desktop-commander -- npx -y @wonderwhy-er/desktop-commander`
2. Docs and alternative setups: https://github.com/wonderwhy-er/DesktopCommanderMCP
3. **⚠️ SESSION RESTART REQUIRED.** MCP servers load only at session start; restart Claude Code before the tools appear.

On **"Skip for now"**: print `[quoin: desktop-commander setup skipped for this run]` and write no sentinel.

On **"Never ask again"**: write `.workflow_artifacts/.desktop-commander-skip` (empty is fine) so this step short-circuits on any re-run, and print `[quoin: desktop-commander setup skipped per user choice]`.

Then proceed to Step 6.7.

### Step 6.7: Seed the repo main spec

**Idempotency/grandfather gate FIRST:** if `.workflow_artifacts/spec.md` already exists, do NOT re-prompt from scratch — print one line noting a repo spec is already present and skip this step (a refresh is offered by `/discover` when run standalone). Only proceed when `.workflow_artifacts/spec.md` is absent.

Use `AskUserQuestion` to ask "What is this repo about?":

```
<!-- decision-gate: best-effort site=init_workflow-prompt-2 -->
AskUserQuestion(
  header="Repo spec",
  question="What is this repo about? A short repo-level spec helps future planning sessions understand context and non-goals at a glance.",
  multiSelect=false,
  options=[
    {label: "Describe it now", description: "Answer a few questions about this repo's purpose, goals, and capabilities now."},
    {label: "Skip — seed later with /discover", description: "Skip for now. /discover offers to draft the repo spec again on a later standalone run."}
  ]
)
```

On **"Skip"**: print a one-line advisory (e.g. `[quoin: repo spec seeding skipped — /discover will offer again later]`) and proceed. No file is created, no error.

On **"Describe it now"**: elicit the repo's purpose, goals, core capabilities, and non-goals in plain prose. Draw on the just-written `repos-inventory.md` and `architecture-overview.md` (from Step 6) to ground the draft in what was actually discovered. Compose `.workflow_artifacts/spec.md` as a Class A doc:

- YAML frontmatter: `title`, `scope: repo`, `date`, `status: draft`
- Body: EXACTLY these five headings, in this order, and no `## For human` block:
  - `## Context`
  - `## Goals`
  - `## Capabilities`
  - `## Acceptance criteria`
  - `## Non-goals`

Write `.workflow_artifacts/spec.md.tmp` with the Write tool, then run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py mv ".workflow_artifacts/spec.md.tmp" ".workflow_artifacts/spec.md"`, then validate:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/validate_artifact.py .workflow_artifacts/spec.md
```

Expect exit 0 — this auto-detects artifact type `spec` (required sections `[## Context, ## Acceptance criteria]`; all 5 headings are within the allowed union).

**Marker cleanup (last action of Step 6.7, run on EVERY branch — describe, skip, or already-exists):**

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py rm ".workflow_artifacts/.init-bootstrap-active"
```

This is a belt-and-suspenders no-op: `/discover` self-clears the marker when it honors it during Step 6, so the marker is normally already gone by the time Step 6.7 runs. `fsops.py rm` is a no-op when the file is already absent.

### Step 7: Copy quickstart guide + legacy detection

**Legacy QUICKSTART detection (run before copying):**

Check if `(project)/dev-workflow/QUICKSTART.md` exists:

```
Legacy QUICKSTART location detected: (project)/dev-workflow/QUICKSTART.md
The new location is .workflow_artifacts/QUICKSTART.md.
Options:
  [m] Move dev-workflow/QUICKSTART.md → .workflow_artifacts/QUICKSTART.md
  [d] Delete the legacy file (a fresh QUICKSTART will be copied below)
  [k] Keep it (you may have cloned the workflow source there — check first)
```

Safety check: if `(project)/dev-workflow/install.sh` OR `(project)/dev-workflow/SETUP.md` is also present, the directory is likely the cloned source repo — print a warning and default to `[k]`. Do NOT auto-delete.

**Copy QUICKSTART from the deployed location:**

`install.sh` deploys `quoin/QUICKSTART.md` to `${CLAUDE_PLUGIN_ROOT}/QUICKSTART.md` at install time. Step 7 reads
from that stable path — no user prompt needed.

- **Source path:** `${CLAUDE_PLUGIN_ROOT}/QUICKSTART.md` (deployed by `install.sh`)
- **Destination:** `.workflow_artifacts/QUICKSTART.md`

```bash
if [ -f "$HOME/.claude/QUICKSTART.md" ]; then
  cp "$HOME/.claude/QUICKSTART.md" .workflow_artifacts/QUICKSTART.md
else
  cat > .workflow_artifacts/QUICKSTART.md <<'EOF'
# Quoin — Quickstart (fallback)

The full QUICKSTART could not be found at ${CLAUDE_PLUGIN_ROOT}/QUICKSTART.md.
This means install.sh has not been run, or was run from an older version.

To get the full command reference, re-run install.sh from your Quoin source clone:
  bash <your-quoin-clone>/install.sh

In the meantime:
  - Type /help inside Claude Code to see available slash commands.
  - Browse the user skills directory at ${CLAUDE_PLUGIN_ROOT}/skills/ for per-skill SKILL.md files.
  - Open the interactive HTML guide at <your-quoin-clone>/Workflow-User-Guide.html in your browser.
EOF
fi
```

The interactive guide lives in the source clone:
```
<your-quoin-clone>/Workflow-User-Guide.html — open in your browser for full walkthrough scenarios.
```

### Step 7.5: Offer scheduled discovery refresh

After the quickstart copy, offer to register the weekly discovery-refresh cron routine (non-blocking; default Skip):

```
<!-- decision-gate: best-effort site=init_workflow-prompt-3 -->
AskUserQuestion(
  question="Would you like to set up a weekly automated discovery refresh?",
  options=[
    {label: "Register weekly refresh",
     description: "Register a /schedule routine (quoin-discovery-refresh) to refresh discovery memory every Monday at 06:00. See ${CLAUDE_PLUGIN_ROOT}/memory/discovery-refresh-routine.md for details."},
    {label: "Skip for now",
     description: "Skip automated refresh setup. The session-start banner (S-5) will prompt you when discovery is stale."}
  ]
)
```

On **"Register weekly refresh"**: instruct the user to run `/schedule` with the payload from
`${CLAUDE_PLUGIN_ROOT}/memory/discovery-refresh-routine.md` (reference the file; do not duplicate the
payload here). Note the cloud-sandbox limitation: the routine may be a no-op if repos live on
local Drive. Print `[quoin: discovery-refresh-routine registered — see discovery-refresh-routine.md for details]`.

On **"Skip for now"**: print `[quoin: discovery-refresh skipped — run /start_of_day to refresh manually when S-5 banner fires]` and proceed to Step 8.

### Step 8: Report

Tell the user:

```
Workflow initialized in <project-root>/.workflow_artifacts/

📁 Structure created:
  - .workflow_artifacts/memory/ (repos, architecture, dependencies, lessons, sessions)
  - .workflow_artifacts/QUICKSTART.md (command reference)
  - <your-quoin-clone>/Workflow-User-Guide.html (interactive guide — in your cloned source)

🔍 /discover completed:
  - Found <N> repositories
  - <brief summary of what was found>

📝 Repo main spec: <seeded at .workflow_artifacts/spec.md | skipped — already existed | skipped per user choice>

Ready to go. Start with:
  - /start_of_day — if resuming existing work
  - /architect — if starting a new feature
  - /plan or /thorough_plan — if you know what to build
  - Open <your-quoin-clone>/Workflow-User-Guide.html for full scenarios
```

## Important behaviors

- **Never overwrite `.workflow_artifacts/memory/`.** If re-initializing, preserve all memory files. They contain accumulated knowledge.
- **Run /discover automatically.** Don't ask — just run it as part of init. The user expects a ready-to-use setup.
- **Keep it fast.** This should feel like a quick bootstrap, not a long ceremony.
- **The quickstart goes in the project folder.** Not buried in skills/ — the user should see it immediately.
