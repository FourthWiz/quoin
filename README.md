<p align="center">
  <img src="quoin/docs/images/quoin-hero.png" alt="Quoin" width="420">
</p>

# Quoin

**Workflow state for stateless coding agents.**

Quoin is a workflow-memory toolkit for coding agents. Its core value is the
artifact-centric workflow: plans, architecture notes, review artifacts, gates,
session handoff, lessons learned, and a cost ledger stored in predictable files
under `.workflow_artifacts/`.

Quoin now has two runtime paths:

- **Claude Code:** an installable global adapter that deploys skills, scripts,
  hooks, memory files, and workflow rules to `~/.claude`.
- **Codex:** a repo-local scaffold that generates `AGENTS.md` and points Codex
  at portable workflow contracts. It uses native Codex planning, approvals,
  sandboxing, repo-scoped instructions, and model/reasoning controls.

Quoin does not install global Codex commands, plugins, hooks, command files, or
runtime extensions.

## Why Quoin?

- **Persistent workflow state:** sessions share structured artifacts instead of
  restarting from memory alone.
- **Planning rigor:** architecture, planning, review, and gate phases have
  explicit artifact contracts.
- **Runtime portability:** shared workflow semantics live in `quoin/core/`,
  while Claude and Codex behavior is isolated in adapter directories.
- **Session continuity:** both runtime paths preserve handoff state under
  `.workflow_artifacts/memory/sessions/`.
- **Cost discipline:** Claude has live cost tooling; Codex writes portable cost
  rows with unavailable telemetry marked honestly as `not_available`.

## Install

Install the package:

```bash
pip install quoin
```

Install or update the Claude Code adapter:

```bash
quoin install --runtime claude
quoin doctor --runtime claude
```

When `--scope` is not specified, the installer **prompts interactively** to ask
whether to install globally (`~/.claude/`) or project-level (`./.claude/`). In
non-interactive environments (CI, pipes), it defaults to global without prompting.

`quoin install` and bare `quoin` remain backward-compatible aliases for the
Claude install path.

### Claude install scope: user vs project

The Claude adapter can be installed at two scopes:

**User scope (global)** — installs to `~/.claude/`. Skills, hooks, and workflow
rules are available in every Claude Code session on the machine.

```bash
quoin install --runtime claude --scope user   # explicit global, no prompt
```

**Project scope** — installs to `<project>/.claude/`. Skills and hooks activate
only when Claude Code is opened in that directory. Hooks register in
`<project>/.claude/settings.json` instead of your personal settings file.

```bash
quoin install --runtime claude --scope project          # explicit project, no prompt
quoin install --runtime claude --scope project:/path    # explicit project root
```

Pass `--scope` explicitly to skip the interactive prompt (useful in scripts and CI).

> **Note:** Claude Code resolves personal scope (`~/.claude/skills/`) before
> project scope. If you have a user-scope install, it will shadow a project-scope
> install for skills. Run `quoin doctor --scope project` to detect conflicts.

Generate the Codex repo-local scaffold:

```bash
quoin install --runtime codex --project-root .
quoin install --runtime codex --project-root . --check
quoin doctor --runtime codex --project-root . --smoke
```

The Codex install path writes `AGENTS.md` in the selected project root. The
`--check` form verifies that `AGENTS.md` is up to date without writing files.
The doctor command verifies repo-local readiness; `--smoke` also runs the
deterministic Codex workflow smoke test.

Equivalent Codex setup command:

```bash
quoin codex init --project-root .
quoin codex init --project-root . --check
```

Legacy source install for Claude remains supported (prompts for scope interactively):

```bash
git clone https://github.com/FourthWiz/quoin
cd quoin
bash quoin/install.sh                        # prompts: global or project?
bash quoin/install.sh --scope user           # global, no prompt
bash quoin/install.sh --scope project        # project, no prompt
```

GitHub redirects the old `FourthWiz/claude_dev_workflow` URL to this repository.

## Runtime Support

| Runtime | Setup command | Scope | Implemented behavior |
|---------|---------------|-------|----------------------|
| Claude Code | `quoin install --runtime claude` | Global adapter under `~/.claude` | Skills, scripts, hooks, memory files, `CLAUDE.md` workflow rules, slash-command workflow |
| Codex | `quoin install --runtime codex --project-root .` | Repo-local `AGENTS.md` | Portable artifact workflow, generated root instructions, readiness/smoke checks, handoff validation, cost event writer |

The portable core is shared. Runtime-specific mechanics are adapter-owned:

- Claude adapter files live under `quoin/adapters/claude/` and install to
  `~/.claude`.
- Codex adapter files live under `quoin/adapters/codex/` and are used from the
  repository. They are documentation, generated instructions, and validation
  scripts, not Codex command packages.

## Codex Quickstart

In the project where you want Codex to use Quoin:

```bash
pip install quoin
quoin install --runtime codex --project-root .
quoin doctor --runtime codex --project-root . --smoke
```

Then ask Codex for Quoin phases in natural language:

- "Use Quoin to discover this repository and write the discovery artifacts."
- "Use Quoin to create an architecture artifact for this task."
- "Use Quoin to write a current plan under `.workflow_artifacts/`."
- "Use Quoin to implement the current plan."
- "Use Quoin to review this implementation against the current plan."
- "Use Quoin to run a gate before the next phase."
- "Update Quoin session handoff and lessons learned."
- "Record a Codex cost event for this task."

The practical Codex workflow loop is documented as:

```text
discover -> plan -> implement -> review -> gate
```

Codex procedure docs live in `quoin/adapters/codex/procedures/` and link back
to portable contracts in `quoin/core/skills/`.

## Codex Validation And Utilities

Codex readiness and smoke:

```bash
quoin doctor --runtime codex --project-root .
quoin doctor --runtime codex --project-root . --smoke
```

Handoff validation:

```bash
python3 quoin/adapters/codex/validate_codex_handoff.py --self-test
python3 quoin/adapters/codex/validate_codex_handoff.py --project-root . --file .workflow_artifacts/memory/sessions/<date>-<task>-codex.md
```

Cost event writing and validation:

```bash
python3 quoin/adapters/codex/cost_event.py --self-test
python3 quoin/adapters/codex/cost_event.py write --project-root . --task <task> --phase <phase> --effort <low|medium|high|max|unknown>
python3 quoin/adapters/codex/cost_event.py validate --project-root . --task <task> --expect-codex
```

Codex cost rows use the portable cost-ledger shape. They record known local
values such as runtime, task, phase, timestamp, session id when supplied, and
effort. Token counts, dollar cost, and telemetry source are recorded as
`not_available` because this repository has no verified Codex local telemetry
interface.

## Claude Quickstart

After installing the Claude adapter:

```text
/init_workflow   one-time project bootstrap
/architect       design the solution
/thorough_plan   converge on a plan with critic review
/implement       write code from the plan
/review          verify implementation against the plan
/gate            stop for an explicit quality checkpoint
/end_of_task     finalize, push, and capture lessons
```

Claude-specific command behavior, model assignments, hook behavior, and
cost-capture plumbing are documented in `quoin/CLAUDE.md`.

## Claude Skills

These slash commands are Claude adapter commands. Codex uses the same portable
workflow intent through natural-language phase requests and repo-local docs.

### Planning And Architecture

| Command | Model | What it does |
|---------|-------|-------------|
| `/architect` | Opus | Deep architectural analysis; internal critic loop |
| `/plan` | Opus | Detailed implementation plan |
| `/thorough_plan` | Opus | Triages task size; runs plan -> critic -> revise convergence |
| `/critic` | Opus | Reviews a plan for gaps, risks, and integration issues |
| `/revise` | Opus | Revises plan from critic feedback in strict/large mode |
| `/revise-fast` | Sonnet | Revises plan from critic feedback in medium mode |

### Implementation And Review

| Command | Model | What it does |
|---------|-------|-------------|
| `/implement` | Sonnet | Writes code from the plan |
| `/review` | Opus | Verifies implementation against the plan |
| `/gate` | Sonnet | Automated quality checkpoint between phases; requires approval |
| `/rollback` | Sonnet | Safely undoes an implementation phase or specific tasks |
| `/pr` | Sonnet | Full pull-request lifecycle: optional version bump, push, create PR, wait for merge, switch branch |

### Session Lifecycle

| Command | Model | What it does |
|---------|-------|-------------|
| `/init_workflow` | Opus | One-time project bootstrap; creates `.workflow_artifacts/` and runs discovery |
| `/discover` | Opus | Scans repos; maps architecture, dependencies, and git history |
| `/start_of_day` | Haiku | Morning briefing from daily/session memory |
| `/end_of_day` | Sonnet | Saves session state, dedupes and promotes insights; auto-invokes /sleep |
| `/end_of_task` | Sonnet | Pushes branch, captures lessons, and finalizes task state |
| `/checkpoint` | Sonnet | Save/restore session context mid-session; writes pending-restore sentinel |
| `/continue_work` | Sonnet | Resume context from a prior session using the recent-sessions index |
| `/sleep` | Sonnet | Memory consolidation: promotes insights to lessons-learned, archives stale entries |

### Utilities

| Command | Model | What it does |
|---------|-------|-------------|
| `/run` | Opus | End-to-end pipeline orchestrator; pauses at gates |
| `/cost_snapshot` | Haiku | Live Claude cost: today, lifetime, and per-task breakdown |
| `/triage` | Haiku | Routes the request to the right workflow phase |
| `/weekly_review` | Haiku | Aggregates weekly progress into a structured review |
| `/capture_insight` | Haiku | Logs a pattern or discovery to daily insights |
| `/expand <path>` | Sonnet | Re-renders a terse workflow artifact in readable English |
| `/status` | Haiku | Renders the workflow pipeline graph with the active phase marked (read-only) |
| `/cleanup` | Sonnet | Trash-moves stale sentinels and old checkpoints into recoverable archive |
| `/next-steps` | Haiku | Append-only queue for future work items (`add` / `list` / `done N`) |

## Open-model routing (opt-in)

Quoin can route Claude Code through [claude-code-router](https://github.com/musistudio/claude-code-router)
to use open models from OpenRouter (DeepSeek V4.1, GLM-5.3, and others) when your
Anthropic quota is low. This is fully opt-in — `quoin install` is unchanged.

### Setup

```bash
export OPENROUTER_API_KEY=sk-or-...   # your OpenRouter key
quoin router setup                    # install CCR + scaffold config
```

This installs `claude-code-router` globally (requires Node 22 or newer) and scaffolds
`~/.claude-code-router/config.json` with an OpenRouter provider and a tier routing
map. Any existing config is backed up with a timestamp before changes are applied.

Check the result:

```bash
quoin router status    # installed? config present? proxy running? key set?
quoin doctor           # includes a one-line CCR probe
```

### Launch modes

| Mode | Command | When to use |
|------|---------|-------------|
| Open models via CCR (v2) | `ccr code` | Quota exhausted, cost-sensitive work |
| Open models via CCR (v3) | `ccr default-claude-code` | Same, on CCR v3 — profile exists but routes nothing until configured in `ccr ui` |
| Native Anthropic | `claude` | Normal use, full quoin skill fidelity |

On CCR v2, `ccr code` auto-starts the local proxy and launches the real `claude`
binary in your terminal — quoin's slash commands and skills work normally. CCR v3
has no `code` subcommand; `ccr default-claude-code` selects its profile, but that
profile routes nothing until you configure its models in `ccr ui`.

**Sanity-check:** inside an open-model session, type `/help`. The quoin skill list
should resolve. If it does not, run `quoin doctor` and file an issue.

> **Note:** the model shown in the Claude Code header will still say "Sonnet 4.6" (or
> whatever your default Claude model is). This is expected — CCR routes requests
> transparently at the HTTP layer, below what the Claude Code UI can see. The actual
> model invoked is the one CCR maps to (e.g. DeepSeek V4 for default requests).

### Switching back

Run `claude` directly (not `ccr code` / `ccr default-claude-code`) to use native
Anthropic models. No config changes needed — the CCR config is left intact so you
can flip back by running `ccr code` (v2) or `ccr default-claude-code` (v3) again.

### Model defaults

The tier → model mapping is seeded to `~/.config/quoin/models.json` on first setup.
Edit it any time, or use the `quoin models` command (see below). Re-running
`quoin router setup` re-applies the current `models.json` to the CCR Router
config — it does not reset it.

### quoin models

View and manage the tier → open-model mapping:

```bash
quoin models                  # show current mapping and active launch mode
quoin models set <tier> <slug>  # update one tier's slug
quoin models preset open      # restore all three defaults at once
quoin models reset            # document native-launch; back up CCR config (non-destructive)
quoin models reset --native   # identical to reset (explicit-intent spelling)
```

**Tiers:** `haiku`, `sonnet`, `opus`

**Friendly aliases** (expand to the corresponding default slug):

| Alias | Slug |
|-------|------|
| `flash` | `z-ai/glm-5.3-flash` |
| `pro` | `deepseek/deepseek-v4.1-flash` |
| `glm` | `z-ai/glm-5.3` |

Examples:

```bash
quoin models set opus glm          # set opus → z-ai/glm-5.3 (alias)
quoin models set sonnet pro        # set sonnet → deepseek/deepseek-v4.1-flash (alias)
quoin models set haiku anthropic/claude-3-haiku  # any OpenRouter slug
```

**Slug validation is advisory:** known slugs are accepted silently; unknown-but-plausible
slugs (containing a `/`) are accepted with a warning; only structurally malformed input
is rejected. You can always edit `~/.config/quoin/models.json` directly.

**Secret rule:** `quoin models` never reads or writes `OPENROUTER_API_KEY`. The key lives
only in the CCR config authored by `quoin router setup`. `set` and `preset` update the
provider's `models` list in-place, leaving `api_key` byte-unchanged.

**reset is non-destructive:** `quoin models reset` backs up the CCR config and prints
native-launch instructions, but leaves the Router keys, provider block, and `models.json`
intact. Run `ccr code` (v2) or `ccr default-claude-code` (v3) again to switch back
to open models with no re-setup required.

## Agentdesk

Agentdesk is a Zellij terminal layout launcher bundled with quoin. It opens a
named Zellij session with panes pre-configured for agent-assisted development —
Claude Code, Codex, and a plain shell — in a single command.

### Setup

Agentdesk is deployed automatically when you install the Claude adapter at user
scope:

```bash
quoin install --runtime claude        # or: quoin install --runtime claude --scope user
```

This copies `agentdesk.zsh` and `setup-agentdesk.sh` to `~/.config/agentdesk/`.
Then run the setup script once to install dependencies (Zellij, lazygit, fzf)
and patch your `.zshrc`:

```bash
bash ~/.config/agentdesk/setup-agentdesk.sh
```

After that, `agentdesk` is available in every new shell session.

> Agentdesk is user-scope only — it is not deployed in project-scope installs.

### Usage

```
agentdesk                              # interactive picker (when run in a TTY)
agentdesk <session-name>               # named session with fixed layout
agentdesk --mode solo|duo|trio         # preset layout
agentdesk --mode trio --name my-sess   # named preset-layout session
agentdesk claude [codex] [shell] [...]  # custom layout from window-type tokens
```

**Preset modes:**

| Mode | Panes |
|------|-------|
| `solo` | One Claude Code pane, full-width |
| `duo` | Claude Code + Shell, side-by-side |
| `trio` | Claude Code + Codex + Shell, side-by-side |

**Window-type tokens** (positional, mutually exclusive with `--mode`):

| Token | Pane contents |
|-------|---------------|
| `claude` | Claude Code |
| `codex` | Codex |
| `shell` | Plain zsh shell |

**Examples:**

```bash
agentdesk                    # picker — choose a preset or enter custom tokens
agentdesk pricing            # named session, fixed layout
agentdesk --mode trio        # Claude + Codex + Shell
agentdesk claude codex       # custom two-pane layout
agentdesk claude claude      # two Claude panes side-by-side
```

If Zellij is not running, agentdesk starts a new session. agentdesk never
re-attaches: if a session with the chosen name already exists, it starts a
fresh session with a numeric suffix (foo_1, foo_2, …). To resume an existing
session, use `agentdesk-attach SESSION-NAME`.

## Architecture

![Quoin architecture](quoin/docs/images/quoin-architecture.png)

Quoin separates portable workflow contracts from runtime adapters:

- `quoin/core/workflow/` defines shared rules, task layout, session state, cost
  ledger shape, and skill metadata.
- `quoin/core/skills/` defines runtime-neutral skill intent.
- `quoin/core/scripts/` contains portable helper implementations.
- `quoin/adapters/claude/` contains Claude-specific skill bodies and runtime
  assumptions.
- `quoin/adapters/codex/` contains Codex-facing repo-local instructions,
  procedures, readiness checks, handoff validation, and cost-event tooling.

The project root containing `AGENTS.md` is the Quoin project root for Codex.
Codex writes `.workflow_artifacts/` there even when the code being edited lives
in a nested subdirectory.

## Cost And Effort

Claude and Codex expose different runtime mechanics, so Quoin keeps cost and
effort handling honest:

- Claude skills declare Haiku/Sonnet/Opus model tiers and can self-dispatch
  through Claude's Agent/Skill behavior.
- Claude cost tooling can read Claude session logs and `ccusage` output.
- Codex adapter docs use runtime-neutral effort labels:
  `low`, `medium`, `high`, `max`, and `unknown`.
- Codex cost rows do not infer token counts or dollars from another runtime.
  Unavailable telemetry is recorded as `not_available`.

For runtime-neutral effort vocabulary, see
[`quoin/docs/effort-levels.md`](quoin/docs/effort-levels.md).

## Documentation

- [`quoin/QUICKSTART.md`](quoin/QUICKSTART.md) - Claude command reference table
- [`quoin/CLAUDE.md`](quoin/CLAUDE.md) - Claude workflow rules, model assignments, artifacts, and cost conventions
- [`quoin/adapters/codex/setup.md`](quoin/adapters/codex/setup.md) - Codex repo-local setup and readiness
- [`quoin/adapters/codex/workflow.md`](quoin/adapters/codex/workflow.md) - Codex workflow execution guide
- [`quoin/adapters/codex/handoff.md`](quoin/adapters/codex/handoff.md) - Codex session handoff contract
- [`quoin/adapters/codex/cost.md`](quoin/adapters/codex/cost.md) - Codex cost event behavior
- [`quoin/docs/runtime-portability.md`](quoin/docs/runtime-portability.md) - portable core and runtime adapter boundary
- [`quoin/docs/runtime-portability-status.md`](quoin/docs/runtime-portability-status.md) - current migration status by runtime
- [`quoin/docs/runtime-parity-matrix.md`](quoin/docs/runtime-parity-matrix.md) - evidence-based runtime parity matrix
- [`quoin/docs/effort-levels.md`](quoin/docs/effort-levels.md) - runtime-neutral effort vocabulary

## Boundaries

Quoin intentionally does not:

- install global Codex commands or plugins
- guess Codex local runtime paths
- duplicate Codex approvals or sandboxing
- claim live Codex hooks
- translate Claude slash commands into Codex command files
- infer Codex token or dollar telemetry from Claude tooling

Future runtime integrations should add explicit adapter contracts instead of
copying Claude-specific behavior into the portable core.

## Contributing

Bug reports and PRs welcome. Quoin development uses Quoin itself: keep changes
artifact-aware, runtime boundaries explicit, and documentation honest about what
is implemented versus planned.

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/) — source-available, noncommercial use only.
