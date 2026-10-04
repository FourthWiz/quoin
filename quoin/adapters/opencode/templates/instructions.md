# Quoin workflow instructions

These rules govern any `/quoin-*` work in this project and take precedence over any other rules file OpenCode also loads for this project.

## Commands

{{COMMAND_LIST}}

`/quoin-implement` and `/quoin-end-of-task` are never started without an explicit user command. No other command may start implementation or finalize a task on the user's behalf.

## Headless coordinator

`quoin run --runtime opencode --workflow` runs the whole sequence headless, one fresh session per phase, with a separate reviewing session for critic and review and a deterministic gate after each phase. `/quoin-run` only names the next step to take. A reviewing session here is a separate context running the same model, not a different model.

## Gate

A gate runs deterministic validators and stops for explicit human approval before the next phase begins. A missing, failing or unrunnable validator is a failed check, never a reason to approve on judgment.

## Artifact layout

Quoin's own workflow artifacts live under `{{ARTIFACT_ROOT}}` at the project root: task subfolders, session state under `{{ARTIFACT_ROOT}}/memory/sessions/`, and the shared knowledge cache under `{{ARTIFACT_ROOT}}/cache/`.

## Roles

{{ROLE_TABLE}}

Every generated subagent runs in a separate context from the role that dispatched it, with no carry-over from the session that started it.

## Limits

{{LIMITS}}

## Not available in OpenCode

{{UNAVAILABLE_LIST}}

## Permissions

Role permissions take precedence over user-level `permission` rules while a Quoin role is active. By default each role may run only the named helper scripts it needs; every other tool a role's permission map does not name, including web fetch and search and any MCP or plugin tool, follows OpenCode's built-in defaults and the user's own `permission` config. A redirection written directly on an allowed script's command asks first; a redirection written on anything that wraps the command — for example a `{ }` group, a `( )` subshell, a loop, an `if` or `case`, a `&&` list or an or-list, or a pipeline — is not part of the matched pattern and does not ask. `quoin opencode script` refuses to run when its own standard output or standard error is a regular file, so the script's output never lands in that file.

The shell still creates or empties the target before the script starts, though, and a redirection of any other descriptor (such as `3>`) is not seen at all. For a redirection written after the last command of an `&&` list, an or-list or a pipeline (for example `a && b > f`, where `b` is an allowed script call), OpenCode's permission check treats the redirection as part of the whole list, so it is not asked about. The planning and gate roles edit only inside the artifact root; the investigator may edit only inside the artifact root too, and its `generate_discovery_map` output is confined to that same artifact root; the coordinator is asked before editing outside it. That artifact-root edit allow matches a directory of that name at the project root or at any depth below it, so a Quoin project nested inside a larger repository still works — it also matches any deeper directory that merely shares that name. Edits that land outside the project are still asked as external-directory prompts. `read` also covers the three MCP resource tools. These per-role limits are defaults and guard rails, not a security boundary, and they hold only while no "always" approval has been given: answering "always" to any prompt, in any agent, allows that kind of action (every file edit for an edit prompt, every command starting with the same words for a shell prompt, redirections included) for every session and agent in this project, Quoin roles included, until OpenCode restarts or the project is reloaded. So whenever Quoin roles are in use, answer "once" to permission prompts. Critic and reviewer stay read-only regardless, because their write, shell and delegation tools are hidden. Approvals are asked in chat, because the question tool is denied to every Quoin role.

## Legacy discovery

OpenCode also reads instructions and skills Claude Code left behind: `~/.claude/skills`, any project-level `.claude/skills` directory found by the walk-up, `~/.claude/CLAUDE.md`, and the project-level `CLAUDE.md` used when no project `AGENTS.md` is found. Set `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS`, `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT` or `OPENCODE_DISABLE_CLAUDE_CODE` to turn these off.

## Core workflow rules

{{CORE_RULES}}
