# OpenCode compatibility

## Pinned release

```
Version: 1.18.32
Install channel: latest
Verified on: 2026-09-24
Upstream repository: https://github.com/anomalyco/opencode
```

## Evidence rule

A claim is `verified` only when backed by at least one of:

- a tag-pinned GitHub artifact — a `blob`, `tree`, or `releases/tag` URL at the pinned
  tag or an earlier tag on the same release line;
- a docs page from a docs tree that is versioned for the pinned release line;
- a local run of the pinned binary, with the exact command shown in backticks,
  naming the pinned version.

A claim resting only on unversioned documentation is `unverified — documentation not
version-pinned`. Every row must carry a `Status` cell of exactly `verified` or
`unverified — REASON` (reason non-empty).

## Release lines

Two active release lines exist upstream as of the verification date.

| Line | Latest version | Published as | Docs |
|---|---|---|---|
| 1.18.x | 1.18.32 (published 2026-09-21) | GitHub Releases (changelog per release) | unversioned `opencode.ai/docs` |
| 2.0.x | 2.0.16 (tagged 2026-09-24) | git tags only — no GitHub Release published for this line yet | versioned `opencode.ai/v2/docs` |

**Chosen line: 1.18.x, pinned at 1.18.32.**

Evidence for the choice, applying the pin criteria in order:

1. **Default install channel.** The install script at `opencode.ai/install` resolves
   the default (`latest`) channel through the GitHub Releases API
   (`repos/anomalyco/opencode/releases/latest`), which returns `v1.18.32`. The npm
   package `opencode-ai`'s `latest` dist-tag independently agrees: `1.18.32`. The
   2.0.x line has no published GitHub Release, so `releases/latest` cannot resolve
   to it; npm's dist-tags carry no 2.0.x-versioned tag either (only unversioned
   `0.0.0-dev-*` snapshots), so the default channel decides the line by itself.
2. **Published schema.** The unversioned schema at `opencode.ai/config.json` uses a
   singular `provider` map with `npm`/`options.apiKey`/`options.baseURL` fields, a
   flat `mcp` object keyed by server name, and a singular `plugin` list — matching
   the 1.18.x doc examples at `opencode.ai/docs/config`. This is the V1 config shape.
3. **Versioned docs.** Moot for the pin, since rule 1 already selected 1.18.x, but
   recorded for completeness: a versioned docs tree exists at `opencode.ai/v2/docs`
   for the 2.0.x line (confirmed reachable, HTTP 200) and documents a different,
   incompatible shape — plural `providers`, nested `mcp.servers`, and plural
   `plugins`. No equivalent versioned tree exists for 1.18.x; its docs live at the
   unversioned `opencode.ai/docs`.

**V1 vs V2 config shape (for the pinned 1.18.x line, V1 applies):**

| | V1 (1.18.x, pinned) | V2 (2.0.x) |
|---|---|---|
| Providers | `provider` (singular map) | `providers` (plural map) |
| Provider auth/URL | `provider.<id>.options.apiKey` / `.baseURL` | not confirmed under this key path |
| Custom provider package | `provider.<id>.npm` | `providers.<id>.package` (not verified against 2.0.x source) |
| MCP servers | flat `mcp.<name>` | `mcp.servers.<name>` |
| Plugins | `plugin` (singular list) | `plugins` (plural list) |

The Quoin for OpenCode development specification's gateway-qualification guidance
describes the provider configuration in the V1 shape (`provider`, `npm`, `options`).
That agrees with the pinned 1.18.x line and disagrees with the unpublished 2.0.x
line — the specification's guidance is correct for the release actually being
qualified against.

## CLI run invocation and JSON event output

| Claim | Status | Evidence | Note |
|---|---|---|---|
| `opencode run [message..]` executes a single prompt non-interactively and exits, without launching the interactive TUI. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/cli.mdx` (usage line `opencode run [message..]` and worked examples) | Matches the Quoin for OpenCode development specification's assumption of a headless, single-shot invocation. |
| `opencode run` accepts a `--format` flag with two values, `default` (formatted) and `json` (raw JSON events); `--attach` lets it join an already-running server instead of cold-booting. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/cli.mdx` (`--format` flag table, `--attach` example) | The exact shape of a raw JSON event is not documented on this page; confirming the schema would require reading the CLI's own source rather than narrative docs. |

## Configuration sources and precedence

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Config is read from a global file, a project file, and merged (not replaced): global `~/.config/opencode/opencode.json`, project `opencode.json`, plus `.opencode/` subdirectories for agents, commands, skills, plugins and similar. Later sources override earlier ones only for conflicting keys. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/config.mdx` (precedence-order list, "merged together, not replaced") | Agrees with the specification's assumption that project config can layer on top of global defaults. |
| `OPENCODE_CONFIG` names a custom config file path and loads between global and project config; `OPENCODE_CONFIG_DIR` names a custom config directory. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/config.mdx` (env var sections) | |
| A managed/organization config concept exists at three tiers, from lowest to highest precedence overall: remote `.well-known/opencode` (fetched on provider auth), OS-level managed files (for example `/Library/Application Support/opencode/` on macOS), and macOS MDM `.mobileconfig` preferences, which are not user-overridable. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/config.mdx` (Remote config, Managed settings sections) | Full eight-tier order is stated on the page; only the pieces the specification depends on (remote lowest, managed highest) are restated here. |
| `instructions` arrays from every config source are concatenated and de-duplicated, never replaced by the last source. Every `.opencode` directory walked up from the working directory to the worktree root contributes both `opencode.json` and `opencode.jsonc`. A loaded config file missing `$schema` is rewritten to add it. The project `Config.update` call writes `config.json`; a `.gitignore`, a `package.json`, and a background dependency install are also written into each loaded `.opencode` directory. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/config/config.ts` (L40-52, L253-258, L244-249, L452-475, L636-648) | Backs the design choice to always emit `$schema` and to expect an owned `.opencode` directory to gain a `.gitignore`, `package.json` and dependency install from OpenCode itself, not from this adapter. |
| The global config directory is `$XDG_CONFIG_HOME/opencode`, or `~/.config/opencode` when that variable is unset; `OPENCODE_CONFIG_DIR` overrides it. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/core/src/global.ts` (L3-64) | |

## Skills: names and discovery locations

| Claim | Status | Evidence | Note |
|---|---|---|---|
| A skill's `name` must be 1-64 characters, lowercase alphanumeric with single-hyphen separators, must not start or end with `-` or contain `--`, and must match the directory name holding `SKILL.md`; equivalent to the regex `^[a-z0-9]+(-[a-z0-9]+)*$`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/skills.mdx` (Validate names section, regex quoted verbatim on the page) | |
| Skills are discovered from six locations: project `.opencode/skills/<name>/SKILL.md`, `.claude/skills/<name>/SKILL.md`, `.agents/skills/<name>/SKILL.md` (walked up to the git worktree root), plus the three global equivalents under `~/.config/opencode/`, `~/.claude/`, `~/.agents/`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/skills.mdx` (Place files, Understand discovery sections) | Matches the specification's assumption that `.claude/skills` is a recognized discovery path, not only `.opencode/skills`; see the full ordered scan and duplicate-name rule in the row below. |
| Skill frontmatter recognizes `name`, `description`, `license`, `compatibility`, and `metadata` (a string-to-string map); unknown fields are ignored. A `permission.skill` `deny` rule hides that skill from the agent entirely, not just from being invoked. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/skills.mdx` (Write frontmatter, Configure permissions sections) | Confirms the canonical id can travel in skill frontmatter `metadata` without OpenCode rejecting the field. |
| Full skill scan order: global `~/.claude`, then `~/.agents`, then project `.claude`/`.agents` walked up to the worktree root, then config directories including `.opencode`, then `skills.paths` and `skills.urls`. A duplicate skill name logs a warning and the later scan in this order wins. Commands, agents and skills are tracked in separate maps, so the same name can be a command, a skill and an agent without conflict. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/skill/index.ts` (L105-140, L175-225) | Supersedes the six-locations row above with the full ordered scan, including the later-wins rule this adapter's name-collision design relies on. |

## Commands, agents, delegation and permissions

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Custom commands are markdown files with YAML frontmatter (`description`, `agent`, `model`) under `.opencode/commands/` (project) or `~/.config/opencode/commands/` (global); the filename becomes the `/name` command and the body becomes the prompt template. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/commands.mdx` (Create command files section) | |
| An agent's `mode` is `primary`, `subagent`, or `all`; a primary agent can delegate to a subagent automatically (based on the subagent's `description`) or a user can invoke one manually with `@name`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/agents.mdx` (subagent description) | |
| Delegation runs through a built-in Task tool; `permission.task` gates which subagents that tool may invoke, using glob-style patterns against subagent names (for example a `"*": "deny"` default with a named exception). | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/permissions.mdx` | |
| The agent frontmatter's known keys are `name, model, variant, prompt, description, temperature, top_p, mode, hidden, color, steps, maxSteps, options, permission, disable, tools`. Any other key is moved into `options` and passed through to the provider as model options. The deprecated `tools` key maps a boolean onto the edit-type permissions (write, edit, patch) together. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/core/src/v1/config/agent.ts` (L45-57), `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/agents.mdx` (Additional, Tools (deprecated) sections) | The canonical id stays out of agent frontmatter: an unknown key would silently reach the model provider instead of staying inert. |
| The `.opencode` directory scan uses these globs, singular and plural: `{agent,agents}/**/*.md`, `{command,commands}/**/*.md`, `{mode,modes}/*.md`, `{skill,skills}/**/SKILL.md`, plus a plugins glob. Each entry's name is its path with the type prefix and extension stripped. The command schema recognizes `template, description, agent, model, variant, subtask`. A decode failure throws rather than silently skipping the file. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/config/entry-name.ts`, `github.com/anomalyco/opencode/blob/v1.18.32/packages/core/src/v1/config/command.ts`, `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/skill/index.ts` (L23-25) | The command-schema key list is the basis for keeping stage two's generated command files to only these keys. |
| Permission rules use last-match-wins evaluation and preserve user-specified key order. `edit` patterns match against the file path relative to the git worktree root, not the project root, and the `edit` permission covers edit, write and patch operations together. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/core/src/permission.ts` (L76-80) | Backs the generated agent files' pattern choice: an artifact-root allow is emitted as two patterns to cover a nested worktree. |
| A subagent spawned through Task is denied `task` and `todowrite` unless its own permission ruleset carries an explicit rule for them; it inherits the parent session's ruleset, not the parent agent's frontmatter rules, so a primary role's own denies do not automatically carry into the subagents it spawns. `--auto` mode auto-approves only rules that resolve to `ask`; an explicit `deny` still blocks the action. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/agent/subagent-permissions.ts`, `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/permissions.mdx` (Auto mode section) | Confirms the three subagent roles' explicit `task: deny` is not redundant with any inherited default. |
| The Task tool fails with a depth-limit error once the calling session's ancestor chain reaches `subagent_depth`, which defaults to 1. Background subagents are unavailable unless `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` is set. Built-in agent permission defaults start from an allow-all `"*": "allow"` rule. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/tool/task.ts` (L95-117), `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/agent/agent.ts` (L119) | This is why the generated coordinator delegates only one hop, and why the critic and reviewer roles need a leading deny-all rule rather than an allowlist. |
| Each agent's effective permission rules are built by layering built-in defaults, then the user's top-level `permission` config, then the agent file's own `permission` block, with the last matching rule winning. An agent file can therefore override a stricter user-level rule while that agent is active. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/agent/agent.ts` (around L277, L293) | A generated Quoin agent file's own permission block is the final layer, so it is never silently narrowed by a stricter user default. |

## Custom providers

| Claim | Status | Evidence | Note |
|---|---|---|---|
| A self-hosted OpenAI-compatible chat-completions endpoint is configured under `provider.<id>` with `npm: "@ai-sdk/openai-compatible"`, `options.baseURL`, and `options.apiKey`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/providers.mdx` (`npm` field description, repeated worked examples) | This is the V1 shape (`provider`, singular) — see Release lines above. |
| An endpoint implementing the OpenAI Responses API (`/v1/responses`) instead of chat completions uses the `@ai-sdk/openai` package rather than `@ai-sdk/openai-compatible`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/providers.mdx` (line 2617: "If your provider/model uses `/v1/responses`, use `@ai-sdk/openai`") | |

## Models and variants

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Per-model reasoning/response parameters (for example a reasoning-effort level, response verbosity) are set under `provider.<id>.models.<model>.options`, and can be overridden per-agent; the agent-level value wins over the global one. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/models.mdx` (global-options section, agent-override cross-reference) | Example model IDs in the pinned source are real vendor identifiers; not reproduced here (placeholders only, per this document's own rule). |
| A `variants` map under a model definition lets one model expose several named parameter presets without duplicating the whole model entry; OpenCode also ships built-in variants for major providers. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/models.mdx` (Variants, Custom variants sections) | |

## Instructions, rules and AGENTS.md

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Project-level custom instructions live in an `AGENTS.md` at the project root (applies to that directory and subdirectories); a global equivalent lives at `~/.config/opencode/AGENTS.md`. Running `/init` creates or updates the project file in place. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/rules.mdx` | |
| The rules fallback has two independent slots. The global slot takes the first existing of `~/.config/opencode/AGENTS.md` and `~/.claude/CLAUDE.md`, regardless of whether the project has its own `AGENTS.md`. The project slot takes the first of `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md` found walking upward from the working directory. Five flags gate this behavior: `OPENCODE_DISABLE_CLAUDE_CODE`, `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT`, `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS`, `OPENCODE_DISABLE_EXTERNAL_SKILLS`, `OPENCODE_DISABLE_PROJECT_CONFIG`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/instruction.ts` (L60-133), `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/effect/runtime-flags.ts`, `github.com/anomalyco/opencode/blob/v1.18.32/packages/core/src/flag/flag.ts` | A machine that already has a global `~/.claude/CLAUDE.md` feeds it to OpenCode even when the project has its own `AGENTS.md`, since the two slots are independent. |
| Relative entries in the `instructions` config key resolve by glob-up from the working directory to the worktree root; absolute paths and `~/`-prefixed paths are also supported. A URL entry is fetched separately from local-path resolution. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/session/instruction.ts` (L79-89, L135-150) | Confirms a relative `instructions` entry keeps working no matter which subdirectory the session was started from. |
| The `instructions` config key adds extra rule files beyond `AGENTS.md`, accepting local paths, glob patterns, and remote URLs (a 5-second fetch timeout applies to remote entries). | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/config.mdx` (via cross-reference on the rules page; instructions key documented on the config page) | |

## Provider policy and tool permissions

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Tool permissions (`permission` config key) use a three-value grammar per tool — `allow`, `ask`, `deny` — with glob-pattern overrides (for example a catch-all `"*"` combined with a specific tool or argument pattern). | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/permissions.mdx` | |
| `experimental.policies` is a distinct, explicitly experimental config array that controls whether OpenCode may use a named resource (currently only the `provider.use` action against a provider ID), separate from `permission`'s tool-approval gating; an unmatched provider defaults to allowed. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/policies.mdx` ("Policies are separate from permissions...") | Directly relevant to gateway qualification: a maintainer could use `experimental.policies` to deny all providers except the one being qualified, independent of tool permissions. |
| The shell tool parses a command with tree-sitter and evaluates each command node against permission patterns separately. A chained command is therefore checked per part, and an allow rule on one part of the chain never widens to the other parts. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/opencode/src/tool/shell.ts` (around L406-409) | Relevant to the deny-by-default critic and reviewer roles: a bash allow for one command in a chain cannot be used to smuggle in a denied one. |

## Plugins and events

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Plugins load from four sources in order (global config list, project config list, global `~/.config/opencode/plugins/` directory, project `.opencode/plugins/` directory) plus npm packages named in the config's `plugin` array, auto-installed via Bun at startup; all loaded plugins' hooks run in sequence. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/plugins.mdx` (Load order section) | |
| A plugin registers for events by returning a hooks object keyed by event name, including `tool.execute.before` / `tool.execute.after` and a family of `session.*` events (`created`, `compacted`, `deleted`, `diff`, `error`, `idle`, `status`, `updated`); an `experimental.session.compacting` hook also exists. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/plugins.mdx` (Available Hooks/Events list, Compaction hooks section) | No event in the pinned source corresponds one-to-one with `tool.execute.before`/`after` granularity for a partial CLI probe — the plugin surface is broader than what the CLI-only qualification in this adapter currently exercises. |
