# Quoin — Quickstart

## Your commands

| Command | What it does |
|---------|-------------|
| `/init_workflow` | One-time project bootstrap — creates .workflow_artifacts/, configures permissions, runs /discover |
| `/discover` | Scans all repos, maps architecture and dependencies |
| `/enrich` | Sharpens a raw task prompt before /specify — fills genuine gaps, no spec/plan written. Lighter-weight than /specify (which does full structured intent elicitation) and distinct from /triage (which only routes) |
| `/architect` | Designs solution architecture for a feature/change |
| `/plan` | Creates a detailed implementation plan (single-pass, Opus) |
| `/thorough_plan` | Triages task size and runs plan→critic→revise convergence loop |
| `/critic` | Reviews a plan for gaps, risks, and integration issues |
| `/revise` | Revises a plan based on critic feedback (Opus, used in strict mode) |
| `/revise-fast` | Revises a plan based on critic feedback (Sonnet, cost-efficient) |
| `/implement` | Writes code from the plan (explicit command only) |
| `/review` | Verifies implementation against the plan |
| `/end_of_task` | Pushes branch, captures lessons (explicit command only) |
| `/rollback` | Safely undoes implementation work |
| `/gate` | Quality checkpoint (runs automatically between phases) |
| `/start_of_day` | Morning briefing — restores context |
| `/end_of_day` | Saves session state, promotes captured insights |
| `/weekly_review` | Aggregates the week's progress into a structured review |
| `/capture_insight` | Logs a pattern or gotcha to the daily scratchpad |
| `/continue_work` | Resume context from a prior session using recent-sessions index |
| `/pr` | Full pull-request lifecycle after /end_of_task: version bump, push, create, wait, switch |
| `/sleep` | Memory consolidation; promotes insights to lessons-learned, archives stale entries |
| `/cleanup` | Trash-move stale sentinels/old checkpoints; standalone also offers task-folder archive (`--no-tasks` to skip); auto-runs after /end_of_day (`--no-cleanup` to skip) |
| `/repo_cleanup` | Sweep a target repo: drop redundant Python comments (proof-checked) and trim slow or redundant tests; local commits only (`--dry-run` to preview) |
| `/next-steps` | Append-only queue for future work items (`add` / `list` / `done N`) |
| `/run` | Default pipeline: scope triage → implement on Opus → verify → local commit. Scope triage adds exactly one thing only when its signal fires: r1 (auth, permissions, crypto, input sanitising, schema or migration, public API) adds one review; r2 (several repos, or parts that must land in order) adds a short stage plan, with discovery when stale and a context pack; r3 (novel architecture, data-migration strategy, several consumers) offers the heavy pipeline, opt-in only. Force a class with `r0:`/`r1:`/`r2:`. A second `/run` started while another is live in the same checkout works in its own git worktree (`QUOIN_WORKTREE_ROOT`, default under `~/.quoin/worktrees/<repo>/<task>/`); add `isolate:` to force one. The final report names the worktree and branch; remove them with `/workspace teardown <task>`. r2 work is split into 2–5 stages in a short `architecture.md`, each run as r0 or r1. Add `parallel:` to run independent stages concurrently in separate worktrees (`QUOIN_PARALLEL_MAX`, default 2; optional daily budget `QUOIN_PARALLEL_BUDGET_USD` drops to one stage at a time once reached) |
| `/run hard:` | Full heavy pipeline (`/run_hard`): discover → specify → architect → plan with critic rounds → implement → gate → review → end_of_task. `/run` also switches here itself when a light run hits an escalation trigger (design-level review blocker, repeated unexplained test failures, too many stages), keeping the brief, context pack and diff; `run_hard` hands a well-localized Small/Medium task back to `/run`. One switch per task; `no-switch:` or `QUOIN_RUN_AUTO_SWITCH=0` turns it off |
| `/cost_snapshot` | Shows today's cost, project lifetime cost, and per-task breakdown |
| `/status` | Renders the workflow pipeline graph with the active phase marked (read-only) |
| `/triage` | Suggests which skill fits your request; type the command to confirm |
| `/expand <path>` | Re-renders a terse workflow artifact in English. File-switch (instant) for files with a `.original.md` side-file; LLM re-expansion (lossy, banner-flagged) for ephemeral terse files. No-op for files that are already English. **Never use for contract-file approval — the `/gate` skill already handles that.** |

## Typical flows

**Bug fix or feature (default):**
`/run <task>` → scope triage → implement → verify → local commit (r1 adds one review, r2 a stage plan); then `/pr` when ready

**Work that needs the full plan/critic/gate chain (manual exception):**
`/run hard: <task>`

**Starting your day:**
`/start_of_day`

**Ending your day:**
`/end_of_day`

## Key rules

1. **`/implement` and `/end_of_task` never run automatically.** You must type them.
2. **Quality gates run between every phase.** Claude stops and asks for your approval.
3. **Each heavy command works best in its own chat session.** Context windows fill up — the file artifacts are the shared memory.
4. **`/end_of_task` pushes the branch only.** Create your PR separately when ready.
5. **Lessons accumulate.** The more you use the workflow, the smarter it gets about your codebase.

## Knowledge cache

The workflow maintains a structured summary cache of your code under `.workflow_artifacts/cache/`, so subsequent runs of `/plan`, `/critic`, `/implement`, and `/review` don't re-read unchanged files from scratch.

- `/discover` populates the cache. `/implement` updates entries for files it modifies. Other skills read from it; none require it to exist.
- Safe to delete at any time — skills fall back to reading source directly, and the next `/discover` rebuilds the cache.
- Benefits on larger projects: faster `/architect` runs, reduced re-reads across planning rounds, lower token cost per lifecycle.

## Reading terse artifacts (`/expand`)

Several workflow artifacts (critic responses, session state, cache entries, `/discover` outputs) are written in a compressed "terse" style to save tokens — see `.workflow_artifacts/caveman-token-optimization/architecture.md` for the rationale. To read these in normal English:

```
/expand .workflow_artifacts/<task>/critic-response-1.md
```

The skill auto-detects the file class:
- **Already-English files** (`architecture.md`, `lessons-learned.md`, etc.) → display as-is with a "Tier 1" banner.
- **Files with a `.original.md` side-file** → display the `.original.md` content (instant, exact).
- **Terse-only files** → invoke Sonnet to re-expand into English. This is **lossy** — the result may differ in nuance from the source. A warning banner is shown. **Never use this output to approve a contract file.**

Optionally, `/expand <path> --save` writes the expansion to `<path>.expanded-<timestamp>.md` (gitignored). Use sparingly — these accumulate.

Common use cases: reviewing a terse critic response; reading a compressed cache entry while debugging; spot-checking a session-state file.

## Install scope

`quoin install` (or `bash quoin/install.sh` from a checkout) installs the quoin
Claude Code plugin at user scope, so it loads in every session, including
sessions started inside a repository below a multi-repo project folder. Skills
are named `/quoin:<skill>`. Restart Claude Code after an install or update.

`--scope project` (or `--scope project:/path`) enables the plugin for one
project folder instead. It then loads only in sessions started in that folder,
not in sessions started inside a repository below it.

## Task record

Every session keeps its own record without a skill being invoked:

- A session that only reads and searches leaves nothing.
- The first edit on a task branch (any branch but `main`, `master`, `develop`,
  `trunk` or the remote's default) opens `.workflow_artifacts/<branch>/brief.md`,
  with the request as typed, the sessions and the repositories touched, and
  `journal.md` beside it, which gains one line per turn: the files edited and
  the first line of the answer. The session is titled with the task name, so
  `claude --resume` lists it by name, and a resumed session is pointed at the brief.
- An edit on `main` or outside a repository keeps a provisional journal under
  `.workflow_artifacts/.capture/`, deleted after seven days, and turns into a task
  when a branch is created.
- `/quoin-task <name>` records the session as a task now; `/quoin-skip` turns the
  record off for the session.
- `python3 ~/.local/share/quoin/plugin/core/scripts/task_record.py status` lists
  open tasks.

Where mods cannot load, the plugin's settings hooks keep the same brief and
journal; only the task name (taken from the branch) and the two commands differ.

### One session per checkout

Before a session's first edit in a checkout, the plugin claims that checkout
for it. A single session is never interrupted. If another live Claude Code
session already holds the checkout, an interactive session asks whether to
move to a worktree or continue there; a headless run moves without asking.
Moving runs `EnterWorktree` for the session and applies the edit to the
worktree's copy, so the other session's files are never touched. Claims go
away when a session ends, and a claim left by a killed session stops counting
once its process is gone. `python3 ~/.local/share/quoin/plugin/core/scripts/run_isolation.py status`
lists a checkout's claims. This needs mods; without them sessions are not
claimed.

### Inbox and task status

Sessions are asked one question, whether they showed a reusable lesson or a
standing rule for `CLAUDE.md` or `.claude/rules/`: over the conversation when
it compacts or opens a PR, and, for sessions that ended without it (closed,
killed or read-only, five prompts or more), over their transcript at the next
session start on a small model. Answers land in
`.workflow_artifacts/memory/inbox.md`, skipping anything already there or in
`lessons-learned.md`. Nothing takes effect until you accept it: `/quoin-inbox`
opens a pane where `a` accepts (appending to `lessons-learned.md` or the named
instruction file) and `x` rejects.

A task follows its PRs. `gh pr create` records the PR and moves the task to
review once every repository it touched has one; at session start the plugin
checks, in the background, whether those PRs have merged and archives the task
and releases its claims. `/quoin-done` closes a task that ends without a PR,
and a task with no PR and no activity for seven days shows as stale. Each
`git commit` also runs the discovery check that marks the project map out of
date when the structure changed.

### Interface graph and rules

`/discover` records where one repository provides something another uses: a
package another repository depends on, an HTTP route another repository calls,
a schema file another repository reads, or an event topic another repository
subscribes to. The graph is the `interfaces` section of
`.workflow_artifacts/discovery-map.json` (schema version 2), and
`dependencies-map.md` is generated from it. For each interface the plugin
writes a path-scoped rule under `.claude/rules/quoin-interface-*.md`, so a
session editing a provider file is told which consumers to check. The rules are
regenerated in the background at session start after a commit changes the
project's structure.

`python3 ~/.local/share/quoin/plugin/core/scripts/impact.py --diff main...HEAD`
lists the repositories and interfaces a change touches and the consumer
repositories to check.

### Cross-repository changes

A change that spans repositories runs as a staged `/run` (route r2). Its stage
list comes from the interface graph (`impact.py --plan`): the repositories the
task changes plus the consumers of the interfaces they provide, providers
first, all on one branch name. Stages that hit an r1 signal get their own
review; after the last stage, `/quoin:review-cross` reviews the whole change
once, focused on consumers of changed interfaces, and writes findings by
`repo/path:line` with a fix for each. `/pr` then opens one PR per repository
and links them to each other. `/quoin:review-cross <branch>` also works by
hand on any branch that exists in several repositories.

### Unattended runs

`quoin run --autonomous <task>` runs a staged task with nobody watching: each
stage still marked ⏳ in `.workflow_artifacts/<task>/architecture.md` becomes
one headless `claude -p "/goal ..."` session whose condition is the stage's
acceptance checks, its commit on the task branch, its ✅ mark, and printed
`affected_tests.py` output as evidence. `--model`, `--max-budget-usd` (per
stage) and `--dry-run` are available; each session is logged to
`goal-run.log` in the task folder. A stage left unfinished stops the run
(exit 1). Rerunning continues at the first unfinished stage.

From inside a session, `/quoin:run-unattended <task>` starts the same run in
the background and returns at once, and the staged `/quoin:run` offers it when
you confirm a stage plan (recommended for three or more stages). Sessions it
starts share the checkout the launching session handed over rather than moving
to a worktree, and commit on the plan's `Branch:`.

Claude Code does not document a pause-and-resume for a headless `/goal`
session at a usage limit, so the run stops there with exit code 75 and the
stage still unfinished; rerun it after the limit resets. For overnight runs
that must wait out a limit by themselves, keep using the supervisor from the
`legacy` tag (`pip install "git+https://github.com/FourthWiz/quoin@legacy"`).

## Open models and other agents

- `quoin open setup`, then `quoin open probe` and `quoin open`: Claude Code on
  an open model (Qwen3 Coder through OpenRouter by default; edit
  `~/.config/quoin/open.json`), with `/quoin-open:plan` and
  `/quoin-open:review`. `quoin open remove` undoes setup.
- `quoin install --runtime codex --project-root .`: the kept skills in
  `.agents/skills/` for Codex and OpenCode.
- `quoin/docs/native-coverage.md`: the monthly running list.

## Files

- `~/.local/share/quoin/plugin/` — the staged plugin Claude Code loads: skills, hooks, scripts, memory guides
- `memory/workflow-catalog.md` in the plugin — every workflow rule; the plugin's SessionStart hook adds the short core rules to each session
- `.workflow_artifacts/` — all workflow artifacts: memory, task plans, session state (gitignored)
- `.workflow_artifacts/cache/` — auto-maintained code summary cache (knowledge cache)

## Running benchmarks

The quantitative benchmark harness (`quoin/benchmarks/`) measures pass-rate-per-dollar for quoin-assisted vs. plain agent workflows on HumanEval+ tasks. Full design: `quoin/benchmarks/methodology.md`.

**Prerequisites:** Claude Code CLI authenticated, `evalplus` installed, Docker available for judge.

**Step 1 — dry-run (always first, estimates cost without running agents):**

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py \
    --suite quoin/benchmarks/suite-smoke.json \
    --cells simple-claude,quoin-claude \
    --run-id v0-smoke \
    --dry-run
```

**Step 2 — smoke run (3 tasks, one cell, confirms harness works end-to-end):**

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py \
    --suite quoin/benchmarks/suite-smoke.json \
    --cells simple-claude \
    --run-id v0-smoke \
    --max-parallel 1
```

**Step 3 — full v1 run (requires explicit budget approval from dry-run output):**

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py \
    --suite quoin/benchmarks/suite-v1.json \
    --cells simple-claude,quoin-claude \
    --run-id v1 \
    --max-parallel 2 \
    --resume
```

Use `--resume` to continue an interrupted run without re-running completed tasks.

**Check results:**

```bash
python3 quoin/benchmarks/scripts/check_invariants.py --run-id v1
cat .workflow_artifacts/quoin-benchmarks/runs/v1/summary.md
```

Results land in `.workflow_artifacts/quoin-benchmarks/runs/<run-id>/`. Each cell × task directory contains `judge.json`, `metrics.json`, `cost.json`, and the full agent transcript.
