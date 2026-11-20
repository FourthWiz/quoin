# Development Workflow — Setup Guide

## Install (two steps total)

### Step 1 — User-level setup (once per machine)

```bash
pip install quoin && quoin install     # or, from a checkout: bash /path/to/quoin/install.sh
```

Installs the quoin Claude Code plugin at user scope. Skills are named `/quoin:<skill>`, and the short workflow rules reach every session through the plugin's SessionStart hook. Safe to re-run — it updates the plugin in place. Restart Claude Code afterwards.

### Step 2 — Project setup (once per project, no bash needed)

```bash
cd /path/to/your/project
claude
# then type: /init_workflow
```

`/init_workflow` handles all project scaffolding: creates `.workflow_artifacts/` (gitignored), configures `.claude/settings.json` permissions, runs `/discover`, and generates a quickstart reference.

---

## What the script does

**`quoin install` — user-level, run once per machine:**
1. **Stages the plugin** — copies its skills, hooks, scripts and memory guides to `~/.local/share/quoin/plugin/`
2. **Installs it** — registers that folder as the `quoin` marketplace and installs or updates `quoin@quoin` with the `claude` CLI
3. **Removes an earlier install** — the files a pre-plugin `install.sh` copied into `~/.claude` (unless edited), its hook stanzas and its rules block in `~/.claude/CLAUDE.md` (your own lines stay)

**`/init_workflow` — run once per project inside Claude:**
4. **Configures permissions** — creates `.claude/settings.json` with allow/deny lists
5. **Creates artifact structure** — `.workflow_artifacts/` at project root (gitignored) with `memory/sessions/`, `memory/daily/`, `memory/weekly/` and template files
6. **Adds to .gitignore** — ensures `.workflow_artifacts/` is gitignored in the project
7. **Runs /discover** — scans repos and populates memory
8. **Generates quickstart** — command reference card

> **After pulling workflow updates:** re-run `quoin install` (or `bash install.sh`) so Claude Code picks up the new plugin version.

## Prerequisites

- **Claude Code** installed (`claude` command available)
- **Git** installed
- **gh** (GitHub CLI) — optional, for PR creation later

## Manual Installation (if you prefer)

`quoin install` is the recommended path. By hand, from a checkout:

```bash
claude plugin marketplace add /path/to/quoin/quoin --scope user
claude plugin install quoin@quoin --scope user
```

This loads the plugin straight from the checkout's `quoin/` folder (tests and benchmarks included, so the plugin cache is larger than a staged install).

### Project artifact structure
```bash
cd ~/projects/my-app
mkdir -p .workflow_artifacts/memory/sessions .workflow_artifacts/memory/daily .workflow_artifacts/memory/weekly
echo '.workflow_artifacts/' >> .gitignore
```

### Initialize
```bash
claude
# then type: /init_workflow
```

## Result

After installation, your project looks like:
```
~/.local/share/quoin/plugin/   ← the staged quoin plugin (skills, hooks, scripts, memory guides)

~/projects/my-app/
├── .claude/
│   └── settings.json        ← project permissions
├── .workflow_artifacts/     ← all workflow artifacts (gitignored)
│   ├── memory/              ← project memory
│   │   ├── sessions/
│   │   ├── daily/
│   │   ├── weekly/
│   │   ├── lessons-learned.md
│   │   ├── workflow-rules.md
│   │   ├── workflow-suggestions.md
│   │   └── ... (populated by /discover)
│   ├── my-feature/          ← active task artifacts
│   │   ├── current-plan.md
│   │   └── critic-response-1.md
│   └── finalized/           ← completed tasks
├── service-a/               ← your repos (clean root!)
├── service-b/
└── frontend/
```

## Daily Usage

```bash
cd ~/projects/my-app && claude
```

| You want to... | Type |
|---|---|
| Start your day | `/start_of_day` |
| Fix a bug or build a feature | `/run <description>` |
| Run the full plan/critic/gate chain (manual exception) | `/run hard: <description>` |
| Review implementation | `/review` |
| Ship it | `/end_of_task` |
| Wrap up for the day | `/end_of_day` |

## Updating the workflow

```bash
cd /path/to/quoin
git pull
bash quoin/install.sh
```

The plugin is re-staged and updated; restart Claude Code to load it. Project `.workflow_artifacts/` is never touched.

## Migrating a legacy project

If your project has an old layout, run `/init_workflow` in the project:

```bash
cd /path/to/legacy-project
claude
# type: /init_workflow
```

`/init_workflow` detects old layouts and offers to migrate with your confirmation:

- **Oldest layout** (`quoin/memory/`): moves to `.workflow_artifacts/memory/`
- **Previous layout** (`memory/` at project root): moves to `.workflow_artifacts/memory/`; also moves `finalized/` and task folders
- In all cases, your accumulated knowledge is fully preserved — nothing is deleted without confirmation

## Troubleshooting

**Skills not recognized:** run `quoin doctor`; it checks that `quoin@quoin` is installed and enabled. `claude plugin list` shows the same. Re-run `quoin install` and restart Claude Code.

**Claude doesn't follow workflow rules:** the rules come from the plugin's SessionStart hook; check that the plugin is enabled (`claude plugin list`) and restart the session.

**/discover finds nothing:** your repos need to be git repositories (each has a `.git/` folder) inside the project root.

**Context filling up:** normal — each heavy command works best in its own chat session. The file-based memory carries state between sessions.
