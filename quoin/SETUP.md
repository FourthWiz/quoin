# Development Workflow — Setup Guide

## Install (two steps total)

### Step 1 — User-level setup (once per machine)

```bash
bash /path/to/quoin/install.sh
```

Installs skills to `~/.claude/skills/` and writes workflow rules to `~/.claude/CLAUDE.md`. Safe to re-run — updates everything idempotently.

### Step 2 — Project setup (once per project, no bash needed)

```bash
cd /path/to/your/project
claude
# then type: /init_workflow
```

`/init_workflow` handles all project scaffolding: creates `.workflow_artifacts/` (gitignored), configures `.claude/settings.json` permissions, runs `/discover`, and generates a quickstart reference.

---

## What the script does

**`install.sh` — user-level, run once per machine:**
1. **Copies skills** — all skills into `~/.claude/skills/` (globally available in every project)
2. **Writes workflow rules** — appends the short core rules to `~/.claude/CLAUDE.md` (auto-loaded by Claude Code everywhere); the full rules live in `~/.claude/memory/workflow-catalog.md`

**`/init_workflow` — run once per project inside Claude:**
3. **Configures permissions** — creates `.claude/settings.json` with allow/deny lists
4. **Creates artifact structure** — `.workflow_artifacts/` at project root (gitignored) with `memory/sessions/`, `memory/daily/`, `memory/weekly/` and template files
5. **Adds to .gitignore** — ensures `.workflow_artifacts/` is gitignored in the project
6. **Runs /discover** — scans repos and populates memory
7. **Generates quickstart** — command reference card

> **After pulling workflow updates:** always re-run `bash install.sh` to propagate path changes to `~/.claude/CLAUDE.md` and `~/.claude/skills/`. Without this step, Claude will have contradictory path instructions between the global CLAUDE.md and the updated skill files.

## Prerequisites

- **Claude Code** installed (`claude` command available)
- **Git** installed
- **gh** (GitHub CLI) — optional, for PR creation later

## Manual Installation (if you prefer)

The script is the recommended path — it handles idempotent updates and edge cases. For reference, here's what it does manually:

### 1. Install skills (user-level, once)
```bash
mkdir -p ~/.claude/skills
for skill in /path/to/quoin/adapters/claude/skills/*/; do
  name=$(basename "$skill")
  cp -r "$skill" ~/.claude/skills/$name
done
```

### 2. Write workflow rules (user-level, once)
```bash
echo "" >> ~/.claude/CLAUDE.md
echo "# === DEV WORKFLOW START ===" >> ~/.claude/CLAUDE.md
sed "s|__QUOIN_HOME__|$HOME/.claude|g" /path/to/quoin/CLAUDE.slim.md >> ~/.claude/CLAUDE.md
echo "# === DEV WORKFLOW END ===" >> ~/.claude/CLAUDE.md
```

The default rules are the short core text in `CLAUDE.slim.md`; the sed call fills in the path to the catalog of full rules. To install every rule inline instead, pass `--claude-md-variant full` to `install.sh` (or set `QUOIN_CLAUDE_MD_VARIANT=full`). An explicit `--claude-md-variant` choice is remembered per scope in `.quoin-claude-md-variant` inside the install folder (`~/.claude/` for user scope, `<project>/.claude/` for project scope; commit that file to share the choice with the team, or add it to `.gitignore` to keep it personal). `--claude-md-variant slim` clears it.

### 3. Create project artifact structure
```bash
cd ~/projects/my-app
mkdir -p .workflow_artifacts/memory/sessions .workflow_artifacts/memory/daily .workflow_artifacts/memory/weekly
echo '.workflow_artifacts/' >> .gitignore
```

### 4. Initialize
```bash
claude
# then type: /init_workflow
```

## Result

After installation, your project looks like:
```
~/.claude/
├── CLAUDE.md                ← workflow rules (auto-loaded everywhere)
└── skills/                  ← all workflow skills (global; partial list)
    ├── discover/
    ├── triage/
    ├── architect/
    ├── plan/
    ├── critic/
    ├── revise/
    ├── revise-fast/
    ├── thorough_plan/
    ├── gate/
    ├── implement/
    ├── review/
    ├── rollback/
    ├── run/
    ├── end_of_task/
    ├── end_of_day/
    ├── start_of_day/
    ├── init_workflow/
    ├── weekly_review/
    ├── cost_snapshot/
    ├── expand/
    └── capture_insight/

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

Skills and `~/.claude/CLAUDE.md` are updated. Project `.workflow_artifacts/` is never touched.

> **Always re-run `install.sh` after pulling updates.** Path changes in CLAUDE.md and skills must be propagated to `~/.claude/` — without this, Claude operates with stale path references.

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

**Skills not recognized:** check that skills are in `~/.claude/skills/` — `ls ~/.claude/skills/` should list every workflow skill. Re-run `bash install.sh` to reinstall them.

**Claude doesn't follow workflow rules:** check that `~/.claude/CLAUDE.md` contains the quoin section (look for `# === DEV WORKFLOW START ===`). Re-run `bash install.sh` to rewrite it.

**Claude uses old `memory/` paths:** re-run `bash install.sh` — the updated CLAUDE.md with `.workflow_artifacts/` paths wasn't propagated to `~/.claude/CLAUDE.md`.

**/discover finds nothing:** your repos need to be git repositories (each has a `.git/` folder) inside the project root.

**Context filling up:** normal — each heavy command works best in its own chat session. The file-based memory carries state between sessions.
