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
- a local run of the pinned binary, with the exact command shown in backticks.

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
| Custom provider package | `provider.<id>.npm` | `providers.<id>.package` (per architecture reference; not independently re-verified this round) |
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

## Skills: names and discovery locations

| Claim | Status | Evidence | Note |
|---|---|---|---|
| A skill's `name` must be 1-64 characters, lowercase alphanumeric with single-hyphen separators, must not start or end with `-` or contain `--`, and must match the directory name holding `SKILL.md`; equivalent to the regex `^[a-z0-9]+(-[a-z0-9]+)*$`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/skills.mdx` (Validate names section, regex quoted verbatim on the page) | |
| Skills are discovered from six locations: project `.opencode/skills/<name>/SKILL.md`, `.claude/skills/<name>/SKILL.md`, `.agents/skills/<name>/SKILL.md` (walked up to the git worktree root), plus the three global equivalents under `~/.config/opencode/`, `~/.claude/`, `~/.agents/`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/skills.mdx` (Place files, Understand discovery sections) | Matches the specification's assumption that `.claude/skills` is a recognized discovery path, not only `.opencode/skills`. |

## Commands, agents, delegation and permissions

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Custom commands are markdown files with YAML frontmatter (`description`, `agent`, `model`) under `.opencode/commands/` (project) or `~/.config/opencode/commands/` (global); the filename becomes the `/name` command and the body becomes the prompt template. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/commands.mdx` (Create command files section) | |
| An agent's `mode` is `primary`, `subagent`, or `all`; a primary agent can delegate to a subagent automatically (based on the subagent's `description`) or a user can invoke one manually with `@name`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/agents.mdx` (subagent description) | |
| Delegation runs through a built-in Task tool; `permission.task` gates which subagents that tool may invoke, using glob-style patterns against subagent names (for example a `"*": "deny"` default with a named exception). | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/permissions.mdx` | |

## Custom providers

| Claim | Status | Evidence | Note |
|---|---|---|---|
| A self-hosted OpenAI-compatible chat-completions endpoint is configured under `provider.<id>` with `npm: "@ai-sdk/openai-compatible"`, `options.baseURL`, and `options.apiKey`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/providers.mdx` (`npm` field description, repeated worked examples) | This is the V1 shape (`provider`, singular) — see Release lines above. |
| An endpoint implementing the OpenAI Responses API (`/v1/responses`) instead of chat completions uses the `@ai-sdk/openai` package rather than `@ai-sdk/openai-compatible`. | unverified — documentation not version-pinned | live page `opencode.ai/docs/providers` (rendered, unversioned) | The tag-pinned `providers.mdx` source was not re-checked line-by-line for this specific Responses-API distinction within this round; only the flat-completions `@ai-sdk/openai-compatible` claim above was confirmed against the pinned source text. |

## Models and variants

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Per-model reasoning/response parameters (for example a reasoning-effort level, response verbosity) are set under `provider.<id>.models.<model>.options`, and can be overridden per-agent; the agent-level value wins over the global one. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/models.mdx` (global-options section, agent-override cross-reference) | Example model IDs in the pinned source are real vendor identifiers; not reproduced here (placeholders only, per this document's own rule). |
| A `variants` map under a model definition lets one model expose several named parameter presets without duplicating the whole model entry; OpenCode also ships built-in variants for major providers. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/models.mdx` (Variants, Custom variants sections) | |

## Instructions, rules and AGENTS.md

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Project-level custom instructions live in an `AGENTS.md` at the project root (applies to that directory and subdirectories); a global equivalent lives at `~/.config/opencode/AGENTS.md`. Running `/init` creates or updates the project file in place. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/rules.mdx` | |
| If no `AGENTS.md` exists, OpenCode falls back to Claude-Code-compatible files: project `CLAUDE.md`, then global `~/.claude/CLAUDE.md`; this fallback can be disabled with `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1`. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/rules.mdx` | Relevant to this adapter: a repository already carrying a `CLAUDE.md` gets picked up by OpenCode automatically unless the env var opts out. |
| The `instructions` config key adds extra rule files beyond `AGENTS.md`, accepting local paths, glob patterns, and remote URLs (a 5-second fetch timeout applies to remote entries). | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/config.mdx` (via cross-reference on the rules page; instructions key documented on the config page) | |

## Provider policy and tool permissions

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Tool permissions (`permission` config key) use a three-value grammar per tool — `allow`, `ask`, `deny` — with glob-pattern overrides (for example a catch-all `"*"` combined with a specific tool or argument pattern). | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/permissions.mdx` | |
| `experimental.policies` is a distinct, explicitly experimental config array that controls whether OpenCode may use a named resource (currently only the `provider.use` action against a provider ID), separate from `permission`'s tool-approval gating; an unmatched provider defaults to allowed. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/policies.mdx` ("Policies are separate from permissions...") | Directly relevant to gateway qualification: a maintainer could use `experimental.policies` to deny all providers except the one being qualified, independent of tool permissions. |

## Plugins and events

| Claim | Status | Evidence | Note |
|---|---|---|---|
| Plugins load from four sources in order (global config list, project config list, global `~/.config/opencode/plugins/` directory, project `.opencode/plugins/` directory) plus npm packages named in the config's `plugin` array, auto-installed via Bun at startup; all loaded plugins' hooks run in sequence. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/plugins.mdx` (Load order section) | |
| A plugin registers for events by returning a hooks object keyed by event name, including `tool.execute.before` / `tool.execute.after` and a family of `session.*` events (`created`, `compacted`, `deleted`, `diff`, `error`, `idle`, `status`, `updated`); an `experimental.session.compacting` hook also exists. | verified | `github.com/anomalyco/opencode/blob/v1.18.32/packages/web/src/content/docs/plugins.mdx` (Available Hooks/Events list, Compaction hooks section) | No event in the pinned source corresponds one-to-one with `tool.execute.before`/`after` granularity for a partial CLI probe — the plugin surface is broader than what the CLI-only qualification in this adapter currently exercises. |
