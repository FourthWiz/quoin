# Native Claude Code Workflows: decision record

Decided 2026-10-21 against Claude Code 2.1.306. This is a maintainer note, not deployed.

## Decision

Reject native Claude Code Workflows for the heavy pipeline's stages (`run_hard`, reached via `/run hard:`; it was `/run` when this was decided). `run_hard` stays a skill-driven stage machine backed by `run_state.py` and boundary checkpoints, with `/gate` stops between phases and the `quoin run --autonomous` supervisor for unattended runs. Nothing in `run_hard` changes, and no follow-up work is scoped. The light `/run`, now the default, runs in one session with no gates, run-state record or supervisor, so this record does not concern it.

Three documented properties of Workflows conflict with what `run_hard` relies on. A workflow run takes no mid-run input, so a human gate between phases would have to become a separate workflow per phase. A run resumes only within the same session (or one reopened with `claude --resume`, a backgrounded session, or a cloud session); a fresh session starts it over, while `run_hard`'s resume and the supervisor deliberately continue from new sessions. Under `claude -p`, which the supervisor uses, a run does not pause at a usage limit; the affected agent fails instead.

## Native behaviour checked

| Fact | Quote or paraphrase | URL |
|---|---|---|
| Resume | "Resumable in the same session". "In a session you start fresh, Claude has no earlier run to relaunch and starts the workflow over as a new run." | https://code.claude.com/docs/en/workflows |
| Mid-run input | "No mid-run user input ... For sign-off between stages, run each stage as its own workflow" | https://code.claude.com/docs/en/workflows |
| Headless approval | In `claude -p` and the Agent SDK no approval prompt is shown; the launch needs a `Workflow` allow rule, auto mode, bypass mode, a `PreToolUse` hook or a host callback. The `ultracode` keyword does not start a workflow from a `-p` prompt. | https://code.claude.com/docs/en/workflows |
| Availability | All paid plans plus API, Bedrock, Agent Platform and Foundry; Pro turns it on in `/config`. `disableWorkflows` or `CLAUDE_CODE_DISABLE_WORKFLOWS=1` turn it off. | https://code.claude.com/docs/en/workflows |
| Usage limit | A run pauses at a usage limit only in an interactive session signed in with a claude.ai subscription; "A run doesn't pause in non-interactive mode with `claude -p` or the Agent SDK". | https://code.claude.com/docs/en/workflows |
| Agents are subagents | A workflow "orchestrates many subagents". Subagents nest up to three layers below the main conversation by default, and `AskUserQuestion` and `Workflow` are removed from every subagent. | https://code.claude.com/docs/en/sub-agents |
| Runtime limits | 16 concurrent agents by default, 1,000 agents per run; the script has no filesystem or shell access; `Date.now()` and `Math.random()` throw. | https://code.claude.com/docs/en/workflows |
| Script location | Each run's script is written under the session directory in `~/.claude/projects/`; saved copies live in `.claude/workflows/`, `~/.claude/workflows/` or a plugin `workflows/` directory. | https://code.claude.com/docs/en/workflows |
| Cost visibility | `/workflows` shows each agent's token usage during the run; no per-agent export for an external ledger is documented. | https://code.claude.com/docs/en/workflows |
| Headless runs | A Manual-mode `claude -p` run denies calls that need approval unless something approves them, and stays open until background workflows complete. | https://code.claude.com/docs/en/headless |

## Quoin needs compared

| Need | Where it lives | What Workflows offer | Verdict |
|---|---|---|---|
| Cross-session resume | `/run --resume` of a heavy task, in `quoin/adapters/claude/skills/run_hard/SKILL.md`; `quoin/core/scripts/run_state.py`; `quoin/core/scripts/boundary_checkpoint.py` | Same-session resume, or replay in a session reopened with `claude --resume`; a fresh session starts over | not met |
| Overnight supervisor | `src/quoin/supervisor.py` relaunches fresh `claude -p` children | Launch under `-p` works only with a pre-approval rule; resume does not carry across fresh children | not met |
| Human gates | `quoin/adapters/claude/skills/gate/SKILL.md` ("The workflow never auto-advances") | No mid-run input; one workflow per stage suggested for sign-off | not met |
| Nested dispatch | `run_hard` phases spawn critics, reviewers and §0 model dispatches (`quoin/adapters/claude/skills/thorough_plan/SKILL.md`) | Workflow agents are subagents; nesting depth is capped at three layers by default and `AskUserQuestion` is removed. Whether a workflow agent can run a full phase with its own dispatches is not documented. | unknown |
| Cost attribution | On-behalf ledger rows keyed by the Agent tool's `agentId` (`run_hard` SKILL.md, "On-behalf cost capture") | Per-agent token usage visible in `/workflows`; no documented export | not met |
| Usage-limit survival | Supervisor relaunch loop plus `quoin/hooks/stop.sh` and `quoin/core/scripts/auto_resume.py` | Pause at a usage limit only in interactive subscription sessions; under `claude -p` the agent fails | not met |

## Partial adoption considered

- **Fan-out inside one phase** (parallel critics or reviewers as a workflow): would not touch the gates between phases, but would change the critic's fresh-session contract and lose the per-agent on-behalf cost rows the ledger depends on. Revisit candidate, not scoped.
- **A saved workflow in `.claude/workflows/` wrapping one phase**: needs installer support for deploying workflow scripts, which quoin does not have, and the phase would still run without its gate. Revisit candidate, not scoped.

## Unknowns

- Whether a workflow agent can run a quoin skill end to end, including its own Agent dispatches: unknown (not documented).
- Whether the Stop hook fires around workflow agents: unknown (not documented).
- Whether a run's saved results survive on another machine: unknown (not documented); only cloud sessions are documented to keep them with the conversation history.

None of these were probed with a paid run; none would change the decision, since the resume, gate and usage-limit conflicts are documented.

## Revisit triggers

- Cross-session or cross-machine resume of a workflow run is documented.
- Mid-run user input, or a pause-for-approval step inside a run, is documented.
- A run pauses at a usage limit under `claude -p` or the Agent SDK.
- Per-agent cost attribution for workflow agents is documented in a form an external ledger can read.
