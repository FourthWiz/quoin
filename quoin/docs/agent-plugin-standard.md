# Agent Plugin Packaging Standard

Status: design proposal. No adapter changes are made by this document.

Quoin skills ship fully only to Claude Code today. Codex gets repo-local guidance, and OpenCode gets a generated project scaffold. Each of these adapters was written by hand against one runtime. This document surveys the packaging standards that agent runtimes have converged on during 2026, proposes one runtime-neutral Quoin bundle, and lays out an incremental path to emit it. The aim is to ship Quoin to Codex, Gemini CLI, OpenCode and similar agents through their native extension mechanisms instead of a new bespoke adapter per runtime.

It builds on `quoin/docs/runtime-portability.md` (the portable core and adapter boundary) and `quoin/docs/runtime-parity-matrix.md` (feature status per runtime). Format details below were checked against the published specifications and vendor documentation in early November 2026; the URLs are listed in the References section. Anything marked "unverified" was not confirmed by a primary source.

## Summary

- The Agent Skills format (a `SKILL.md` folder with `name` and `description` frontmatter) is now read natively by Claude Code, Codex, Gemini CLI and OpenCode. It is the lowest common denominator, and it is enough to carry Quoin's skill bodies.
- Agent Plugins 1.0 (agent-plugins.org) packages Agent Skills and MCP servers behind a closed, versioned `plugin.json`, with reverse-domain extension namespaces for runtime-specific additions. Codex uses it as its primary plugin format. Claude Code and Gemini CLI keep their own manifests.
- Quoin should build one bundle: Agent Skills–conformant skills generated from `quoin/core/skills/`, wrapped in an Agent Plugins manifest, with thin per-runtime overlays (`.claude-plugin/plugin.json`, `gemini-extension.json`, `extensions.com.openai`, and OpenCode's generated agents and commands).
- Things no standard covers stay in the overlays or are dropped: model-tier dispatch, hooks, subagent spawning and cost telemetry.

## Current state in the repo

### Layers

Quoin already separates intent from runtime mechanics in three layers:

1. `quoin/core/workflow/` holds runtime-neutral semantics: `rules.md`, `task-layout.md`, `session-state.md`, `cost-ledger.md`, the dispatch envelope in `handoff-format.md`, and `skills.json`. That catalog has one row per skill with `name`, `phase`, `effort`, `user_facing`, `claude_model`, `section_0` and `spawn_target`, plus `claude_adapter: false` for skills with no Claude implementation.
2. `quoin/core/skills/` holds one portable intent document per skill (33, including `checkpoint`, which has no Claude adapter). Each one follows a fixed outline: Purpose, When to use, Inputs, Output, Behavior contract, Out of scope, Notes. These documents describe contracts, not runtime steps.
3. `quoin/adapters/<runtime>/` turns that intent into something a runtime can run.

### Claude adapter

The 32 Claude skills live in `quoin/adapters/claude/skills/<name>/SKILL.md`. Their frontmatter carries `name`, `description` (with trigger phrases such as "Use for: /plan") and `model: haiku|sonnet|opus`. `model` is a Claude Code extension and is not part of the Agent Skills standard. Each body starts by pointing at its portable intent document, then adds Claude-only machinery:

- **§0 model dispatch.** On the 19 cheap-tier Claude skills, a generated block tells the agent to read its own model name, compare it with the declared tier, and re-spawn itself through the Agent tool at the cheaper tier. A `[no-redispatch]` sentinel prevents loops, and a `dispatch_config.py` check runs first.
- **§0' pollution dispatch.** `pollution_guard.py` scores the Claude session transcript and, past a threshold, re-spawns the skill in a fresh subagent.
- **Generated preambles.** The 9 spawn targets carry a `preamble.md` produced by `build_preambles.py`. It holds slices of `format-kit.md` and `glossary.md`, with source hashes, laid out for prompt-cache reuse.
- **Install-time paths.** Every Claude `SKILL.md` refers to helper scripts as `__QUOIN_HOME__/scripts/...`. The installer replaces that placeholder with the absolute deploy root when it copies skills to `~/.claude/skills/`.
- **Subagent orchestration.** Almost every Claude skill names the Agent tool or a `subagent_type`. Orchestrating skills (`run`, `thorough_plan`, `run_hard`) chain other skills this way.
- **Hooks.** Shell hooks registered on nine event stanzas in `~/.claude/settings.json` (prompt submit, pre- and post-compact, session start, session end, worktree create, stop) maintain session history, run-continuation sentinels and end-of-session reconciliation.
- **Cost capture.** `cost_from_jsonl.py`, `agent_transcript_cost.py` and related scripts read Claude Code session JSONL files and a Claude price table.

### Codex and OpenCode adapters

The Codex adapter ships a generated `AGENTS.md`, one `README.md` per skill under `quoin/adapters/codex/skills/` that points back at the portable intent document, procedures, a handoff validator and a cost-row writer. On purpose, it has no command files and no global install.

The OpenCode adapter is much fuller. `quoin install --runtime opencode` generates a repo-local `.opencode/` scaffold from templates (`templates/skill.md`, `command.md`, `agent.md`). It renames each skill to a prefixed kebab-case id (`quoin-continue-work` for `continue_work`), binds each command to a role agent, and resolves a provider, model and effort for each role from layered profiles. Its `feature-manifest.json` classifies every skill as supported, documentation-only or unsupported, and a drift check runs it against the portable catalog. This adapter already does most of what a cross-runtime emitter needs: renaming, a support status per skill, and role mapping.

### What is already portable

| Concern | Portable today | Runtime-specific today |
|---|---|---|
| Artifact layout, stage paths, finalization | `core/workflow/`, `path_resolve.py` | none |
| Artifact validation, critic classification | `validate_artifact.py`, `classify_critic_issues.py` | none |
| Skill intent and contracts | `core/skills/*.md`, `skills.json` | Claude `SKILL.md` bodies |
| Model choice | abstract `effort` in `skills.json` | `claude_model`, §0 dispatch, OpenCode role profiles |
| Subagent dispatch | envelope in `handoff-format.md` | Agent tool calls, `[no-redispatch]` sentinels |
| Context hygiene | none | §0' pollution guard, cache preambles |
| Lifecycle automation | `verify_claims.py` logic | Claude hook scripts and `settings.json` wiring |
| Cost | `cost_event.py` schema, `cost_summary.py` | JSONL parsing, Claude price table, Codex rows |
| Install | none | `install.sh`, OpenCode installer, Codex generator |

## Survey of packaging standards

### Agent Skills

A skill is a directory named after the skill. It holds a required `SKILL.md` and optional `scripts/`, `references/` and `assets/`. Only two frontmatter fields are required. `name` is 1–64 characters of lowercase letters, digits and single hyphens, and must match the directory name. `description` is 1–1024 characters and says what the skill does and when to use it. The optional fields are `license`, `compatibility` (up to 500 characters), `metadata` (a map of strings to strings) and an experimental `allowed-tools`. Agents load skills progressively: name and description at startup, the full body when the skill is activated, and resources on demand. The spec recommends keeping `SKILL.md` under 500 lines, referencing files by paths relative to the skill root, and keeping references one level deep. A `skills-ref validate` tool checks conformance. Anthropic created the format, and it is now an open standard.

Native readers, each confirmed against vendor docs:

- **Claude Code** follows the standard and adds its own fields: `model`, `effort`, `context: fork`, `agent`, `disable-model-invocation`, `user-invocable`, `argument-hint`, `hooks` and others. Every skill becomes a `/name` command. Claude surfaces outside Claude Code (the Skills API, claude.ai) reject any field beyond the six standard ones.
- **Codex** scans `.agents/skills` from the working directory up to the repo root, plus `$HOME/.agents/skills` and `/etc/codex/skills`. Users invoke a skill explicitly with `/skills` or a `$name` mention, or Codex picks it implicitly from the description. An optional `agents/openai.yaml` inside the skill folder sets UI fields, `policy.allow_implicit_invocation` and tool dependencies.
- **Gemini CLI** reads `.gemini/skills/` and `.agents/skills/` (the workspace copy wins), the same folders under the home directory, and skills that extensions provide. The model activates a skill through an `activate_skill` tool, and the user confirms.
- **OpenCode** reads `.opencode/skills/`, `.claude/skills/` and `.agents/skills/`, plus the global equivalents. It loads skills through a native `skill` tool, ignores unknown frontmatter fields, and gates access with `permission.skill` patterns.

`.agents/skills/` is now the de facto shared project location. Whether Claude Code scans it is unverified. Claude Code documents `.claude/skills/` and plugin skill folders.

### Agent Plugins 1.0

Agent Plugins is a vendor-neutral package format published as version 1.0.0. Its technical steering committee has maintainers from Amazon, Cursor, Microsoft, OpenAI and Vercel. A plugin is a directory with these parts:

- a root `plugin.json` with a closed schema. It requires `$schema` (pinned to the 1.0.0 schema URL) and `name` (lowercase letters, digits, hyphens and periods). The only optional fields are `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords` and `extensions`.
- `skills/<name>/SKILL.md`, in the Agent Skills format, discovered one level deep.
- an optional root `mcp.json` declaring `stdio`, `streamable-http` or `sse` servers. `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` expand in `args`, `env` and `cwd`.
- client extensions, as an object keyed by namespace under `extensions.<reverse-domain>`, as a top-level directory named exactly after the namespace, or both.

Hosts must ignore component types and extension namespaces they do not implement. Hosts must also reject any plugin whose core manifest breaks the schema. Commands, agents and hooks are not portable components in 1.0; they can only appear inside a vendor namespace. Third-party write-ups report launch support in ChatGPT, Codex, Cursor, GitHub Copilot, VS Code and Kiro (not verified against each vendor). The same write-ups say Claude Code keeps its own manifest.

### Claude Code plugins

A Claude Code plugin has an optional `.claude-plugin/plugin.json` and components at the plugin root: `skills/`, `commands/`, `agents/`, `hooks/hooks.json`, `.mcp.json`, `.lsp.json`, `bin/`, `settings.json` and others. The manifest is open-ended. It supports `userConfig` prompts, `dependencies`, inline `commands` with a `model` field, and hook files. Components are namespaced under the plugin name, so a skill `plan` in plugin `quoin` is invoked as `/quoin:plan`. `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}` and `${CLAUDE_PROJECT_DIR}` expand in hook commands, MCP configs and skill bodies. They are not exported to commands run through the Bash tool. Marketplaces are JSON catalogs, and `claude plugin validate` is the authoritative check.

A Claude plugin cannot be an Agent Plugins package as-is. The closed 1.0 schema rejects Claude fields such as `userConfig`. One plugin directory can still carry both manifests side by side, with a shared `skills/` tree.

### Codex

- **AGENTS.md.** Plain Markdown with no schema. The nearest file in the directory tree wins. The Agentic AI Foundation under the Linux Foundation now stewards it, and Codex, Gemini CLI, OpenCode, Cursor, Copilot and others read it. Quoin's Codex adapter already generates one.
- **Plugins.** New packages use a root `plugin.json` that declares the Agent Plugins schema. Codex-specific settings go under `extensions.com.openai`: `interface` for display fields, an `onboardingSkill` run after install, `apps`, and `hooks`. `.codex-plugin/plugin.json` remains as a compatibility fallback. It is read only when `extensions.com.openai` is absent, and the two are never merged. Marketplaces live at `$REPO_ROOT/.agents/plugins/marketplace.json` or `~/.agents/plugins/marketplace.json`. A legacy-compatible `.claude-plugin/marketplace.json` is also read. Users manage marketplaces with `codex plugin marketplace add|list|upgrade|remove` and enable plugins per project in `.codex/config.toml`.
- **Subagents.** Custom agents are TOML files in `.codex/agents/` or `~/.codex/agents/`. They hold `name`, `description` and developer instructions, plus an optional `model` and `model_reasoning_effort`. Sandbox and approval settings always come from the parent session.
- **Hooks.** `hooks.json`, or `[hooks]` tables in `config.toml`, at user or project level. Events: session start and end, subagent start and stop, pre- and post-tool-use, permission request, pre- and post-model-request, pre- and post-compact, prompt submit, stop and interrupt. Hooks are on by default. Hooks bundled in a plugin work only for manually installed desktop plugins and are not eligible for the public directory.

Codex now has verified extension points. That changes the premise of `runtime-portability.md`, which says not to create a Codex installer "until a supported Codex extension point is verified". The rule against guessing install paths still holds. The marketplace and `.agents/` locations above are documented paths, not guesses.

### Gemini CLI

An extension is a directory under `~/.gemini/extensions/` with a required `gemini-extension.json`. The manifest's fields are `name`, `version`, `description`, `mcpServers`, `contextFileName` (default `GEMINI.md`), `excludeTools`, `settings` and `themes`. The directory also holds `commands/` (TOML files, where `gcs/sync.toml` becomes `/gcs:sync`), `skills/` in the Agent Skills format, `agents/` (subagent Markdown files, a preview feature), `hooks/hooks.json` and `policies/`. Paths in the manifest and hooks file use `${extensionPath}` and `${workspacePath}`. Users install with `gemini extensions install <git-url|path>`. Subagent frontmatter accepts `model` (default `inherit`), `tools`, `max_turns` and `timeout_mins`. Hooks cover session start and end, before and after agent, model and tool, before tool selection, pre-compress and notification. Google announced that Antigravity CLI replaced Gemini CLI for unpaid and Google One users on June 18, 2026. Gemini CLI's support for Agent Plugins is unverified.

### OpenCode

OpenCode has no plugin-folder package format. It composes several native mechanisms. Skills come from the folders listed above. Commands are Markdown files in `.opencode/commands/`, with `description`, `agent`, `model`, `subtask` and `$ARGUMENTS`. Agents are Markdown files in `.opencode/agents/`, with `mode: primary|subagent`, `model` as `provider/model-id`, and `permission`; subagents run through the Task tool or an `@mention`. Plugins are JS/TS modules in `.opencode/plugins/` or npm packages listed in `opencode.json`. Plugins can handle session, tool, message, permission and compaction events and register custom tools. Quoin's OpenCode adapter already targets these mechanisms.

### MCP

MCP prompts (`prompts/list` and `prompts/get`, spec revision 2025-11-25) are user-controlled templates, and many clients show them as slash commands. MCP tools expose deterministic operations. An MCP server reaches every major runtime, but it is a poor fit for Quoin's skill bodies. A prompt is a single message template with no progressive disclosure and no bundled resources, and serving one needs a running process. MCP does fit Quoin's deterministic helpers: path resolution, artifact validation and cost-row writing.

### Convergence

The ecosystem has settled into three layers. Quoin should align with each one:

1. **Always-on instructions:** `AGENTS.md` (`CLAUDE.md` and `GEMINI.md` are runtime aliases).
2. **On-demand procedures:** Agent Skills folders.
3. **Distribution:** Agent Plugins where hosts support it, and native manifests (Claude, Gemini) everywhere else. Each native manifest points at the same `skills/` tree.

Commands, subagents, hooks and model selection remain per-runtime, with similar but incompatible schemas.

## Design: the Quoin bundle

### Layout

The bundle is a build output. Nobody edits it by hand.

```
quoin-bundle/
  plugin.json                  # Agent Plugins 1.0 core manifest
  AGENTS.md                    # always-on Quoin workflow rules (from core/workflow/rules.md)
  skills/
    quoin-plan/
      SKILL.md                 # standard frontmatter only; body from core/skills/plan.md + procedure
      references/              # copies of the core/workflow docs this skill needs
      scripts/                 # copies of the portable helper scripts this skill calls
    quoin-capture-insight/
    ...
  mcp.json                     # optional, later phase: portable helper tools
  com.openai/                  # optional Codex extension files (agents/openai.yaml data, onboarding)
  .claude-plugin/plugin.json   # Claude overlay manifest
  gemini-extension.json        # Gemini overlay manifest (+ commands/ TOML)
  bundle-manifest.json         # Quoin-internal: source digests, per-runtime support status
```

The root `plugin.json` uses only the closed 1.0 fields, so Agent Plugins hosts accept it. Codex reads `extensions.com.openai` in that same file. Claude Code and Gemini CLI ignore `plugin.json` and read their own manifests, both of which point at the shared `skills/` tree. OpenCode does not consume the bundle as a package. Its installer copies or links `skills/` into `.agents/skills/` or `.opencode/skills/` and generates agents and commands next to them, as it does today.

### Single source, generated outputs

`quoin/core/workflow/skills.json` and `quoin/core/skills/*.md` stay the source. A bundle builder, a sibling of `build_preambles.py` with a matching `--check` mode, renders each skill. Whether a skill is included per runtime comes from the existing support classifications, merged into `bundle-manifest.json`: the OpenCode `feature-manifest.json` and the Codex `feature-manifest.json`. The Claude `SKILL.md` files in `quoin/adapters/claude/skills/` are not inputs to the portable bundle. They carry §0 blocks and Agent-tool instructions that no other runtime can run.

### Naming

Agent Skills names may not contain underscores, and must match the folder name. Twelve Quoin skills (such as `capture_insight`, `end_of_task` and `run_hard`) contain underscores. The bundle adopts the convention the OpenCode adapter already uses: a `quoin-` prefix plus kebab case (`quoin-end-of-task`, `quoin-revise-fast`). The prefix also prevents collisions with other skills in shared folders such as `~/.agents/skills/`. Each skill keeps the canonical Quoin name in `metadata.quoin-id`, so that artifacts, ledgers and handoff envelopes go on using the canonical ids.

### Frontmatter

Portable `SKILL.md` files carry only the six standard fields. Quoin data goes in `metadata`, as string values:

```yaml
---
name: quoin-plan
description: Writes an implementation-ready plan for a Quoin task. Use when ...
license: <repo license>
compatibility: Requires git and python3; uses a .workflow_artifacts/ directory in the project root.
metadata:
  quoin-id: plan
  quoin-phase: planning
  quoin-effort: high
  quoin-tier: deep
  quoin-version: "<package version>"
---
```

`quoin-tier` is an abstract cost tier (`light`, `standard`, `deep`) derived from `skills.json`. It replaces Claude model names in portable output. Each overlay maps the tier to a native setting: Claude `model:` or `effort:`, an OpenCode role profile, a Codex subagent `model_reasoning_effort`, or a Gemini subagent `model`. Descriptions are rewritten for implicit activation ("Use when ..."). They drop the "/plan" trigger phrases, because invocation syntax differs by runtime (`/quoin:plan`, `$quoin-plan`, `/quoin-plan`).

### Body content

Each body is assembled from the portable intent document plus a runtime-neutral procedure. It follows Agent Skills guidance: under 500 lines, one level of references, and paths relative to the skill root. It contains no §0 or §0' block, no Agent-tool instructions and no `__QUOIN_HOME__` placeholders. Where a Claude skill delegates to a subagent, the portable body states the delegation as a contract ("run the critic phase in a fresh context if the runtime supports delegation; otherwise run it inline"), using the envelope in `handoff-format.md`. Each overlay can then bind that contract to a native subagent.

### Shared resources

Agent Skills gives a skill no portable way to reach files outside its own folder. Agent Plugins defines `${PLUGIN_ROOT}` only for MCP subprocesses. Two options were considered:

- **Copy per skill (chosen for the first phase).** The builder copies the `core/workflow` documents and `core/scripts` helpers that each skill references into that skill's `references/` and `scripts/` folders, and records digests in `bundle-manifest.json`. This duplicates bytes but needs no runtime support. The current install already copies files and rewrites paths, so this is the same pattern.
- **Shared helpers through MCP (a later phase).** A small stdio MCP server declared in `mcp.json` exposes `path_resolve`, `validate_artifact` and `cost_event` as tools. Skills then call tools instead of scripts, and duplication goes away for hosts that support MCP.

Portable scripts stay limited to what `runtime-portability.md` already calls portable core. Claude-owned scripts (`cost_from_jsonl.py`, `pollution_guard.py`, `session_age_guard.py`, `build_preambles.py`) never enter the portable bundle.

### Install manifest

`bundle-manifest.json` is Quoin's own record. No runtime reads it. It holds the bundle version, the Agent Plugins and Agent Skills versions targeted, a digest for each file with its source path, and one row per skill and runtime with a status of `supported`, `documentation-only` or `unsupported`. Doctor and readiness commands such as `verify_codex_readiness.py` and `quoin doctor --runtime opencode` read it to detect drift between an installed copy and the source.

## Runtime mapping

| Runtime | Native mechanism | What Quoin ships | Model-tier dispatch | Hooks | Subagents | Slash commands |
|---|---|---|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` plugin, or the current `~/.claude/skills` install | Claude overlay skills (§0, preambles), hooks, agents | Native `model:`/`effort:` frontmatter; §0 kept until plugin parity is shown | Plugin `hooks/hooks.json` can replace `settings.json` wiring | Agent tool; plugin `agents/` | Every skill, namespaced `/quoin:<name>` under a plugin |
| Codex | Agent Plugins `plugin.json` + `extensions.com.openai`; `.agents/skills`; `AGENTS.md` | Portable bundle, `com.openai` interface data, generated `AGENTS.md` | No per-skill model; `quoin-tier` maps to custom-agent TOML `model_reasoning_effort` | Supported, but plugin hooks only for manually installed desktop plugins; ship none at first | Custom agents in `.codex/agents/*.toml` (generated later, optional) | `$quoin-plan` mention or `/skills`; no custom command files |
| Gemini CLI | `gemini-extension.json` extension | Same `skills/` tree, `GEMINI.md` context, `commands/*.toml` for user-facing skills | Subagent `model:` only; skills run on the session model | Extension `hooks/hooks.json`; session-history parity unverified | `agents/*.md` (preview) | TOML commands, namespaced by folder |
| OpenCode | `.agents/skills` or `.opencode/skills`, `.opencode/agents`, `.opencode/commands`, JS plugin | Bundle skills, plus generated role agents and commands (existing adapter) | Per-role `model` in agent frontmatter, resolved from profiles | JS plugin events (`session.*`, `tool.execute.*`, compaction) | Task tool, `mode: subagent` | Command Markdown files |
| Other Agent Plugins hosts (Cursor, Copilot, VS Code, Kiro) | Agent Plugins `plugin.json` | Portable bundle only | None | None | None | Host-defined skill invocation |
| MCP-only clients | MCP server | Optional helper server with tools; prompts for top-level entry points | None | None | None | MCP prompts where the client shows them |

### Gaps no standard closes

- **Model-tier dispatch.** §0 depends on the agent reading its own model name and re-spawning itself through a Claude tool. No other runtime offers this, and Agent Skills has no model field. Portable output drops self-dispatch and declares an abstract tier. Cost control then comes from how the user or the overlay configures roles, not from the skill re-routing itself at run time.
- **Hooks.** Every runtime has hooks, but event names, payloads and installation rules differ, and Agent Plugins does not standardize them. Hook logic that matters across runtimes should move into portable scripts with a thin per-runtime shim. `verify_claims.py` already works this way. Hook wiring stays in overlays.
- **Subagents.** Codex uses TOML, Gemini and OpenCode use Markdown with frontmatter, and Claude uses the Agent tool. The envelope in `handoff-format.md` is the portable contract. Native agent files are overlay output.
- **Slash commands.** Claude exposes skills as commands. Codex uses `$skill` mentions. Gemini and OpenCode need separate command files. The bundle relies on implicit activation plus each runtime's skill picker, and overlays generate command files where the runtime needs them.
- **Prompt-cache preambles and pollution guards.** These are Claude-only optimizations and stay in the Claude overlay.
- **Cost telemetry.** Each runtime exposes usage differently, or not at all. The portable ledger schema stays. Capture stays per adapter, and fields a runtime cannot provide are marked `not_available`, as the Codex adapter does today.

## Recommendation: incremental path

**Phase 1: emit an Agent Skills–conformant bundle.** Add a bundle builder under `quoin/core/scripts/` with a wrapper in `quoin/scripts/`, following the existing wrapper template. It renders `skills/quoin-*/SKILL.md` from `core/skills/` and `skills.json` using the naming and frontmatter rules above, copies referenced docs and scripts, writes `AGENTS.md` and `bundle-manifest.json`, and supports `--check`. Add an offline conformance test that applies the Agent Skills naming and length rules and checks that references stay one level deep. Start with skills that are self-contained and need no subagents: `capture_insight`, `expand`, `status`, `cost_snapshot`, `next_steps`, `triage`. Exit criterion: the bundle loads from `.agents/skills/` in Codex, Gemini CLI and OpenCode, with the commands that show it recorded in the parity matrix.

**Phase 2: wrap the bundle as an Agent Plugins package.** Add the closed `plugin.json` and an `extensions.com.openai.interface` block, and publish a repo-level `.agents/plugins/marketplace.json` entry. This becomes the supported Codex install path and replaces the plan for a bespoke Codex installer. Revise `runtime-portability.md` to record that Codex's extension point is now verified. Expand coverage to the planning and review skills (`plan`, `critic`, `review`, `gate`), with delegation stated as contracts.

**Phase 3: thin native overlays.** Generate `gemini-extension.json`, `GEMINI.md` and TOML commands for user-facing skills. Change the OpenCode installer to take skills from the bundle instead of its own templates, keeping its roles, profiles and commands. Prototype a Claude plugin (`.claude-plugin/plugin.json`) that points at a Claude-overlay skills tree and declares hooks through `hooks/hooks.json`. Then compare it with `install.sh` on behavior, namespacing (`/quoin:plan` versus `/plan`) and `${CLAUDE_PLUGIN_ROOT}` path handling. Do not switch the Claude default until that comparison passes.

**Phase 4 (optional): MCP helper server.** Expose the portable helpers as MCP tools through `mcp.json` to remove the per-skill script copies on hosts that support MCP. Consider MCP prompts only for clients without skill support.

Each phase ships on its own and leaves `bash quoin/install.sh` and the installed Claude workflow unchanged.

## Risks

- **Young standards.** Agent Plugins is at 1.0, Gemini subagents are in preview, and Codex itself documents format churn (the `.codex-plugin/plugin.json` fallback). Pin the targeted schema versions in `bundle-manifest.json`, and gate upgrades the way the OpenCode adapter gates qualified releases.
- **Name drift.** Renaming to `quoin-*` kebab case could split identity between skill folders and artifacts. `metadata.quoin-id` and a drift test that maps every bundle name back to `skills.json` mitigate this.
- **Behavior loss without §0.** Portable runs may cost more, because skills run on whatever model the session uses. The tier metadata must reach users through the overlays and docs, so the cost difference is visible rather than silent.
- **Duplicated resources.** Per-skill copies of docs and scripts can drift from source. Digests plus `--check` in tests catch this, the same mechanism `build_preambles.py` uses.
- **Weak implicit activation.** Skills chosen by description may fire too often or not at all. Orchestration skills should be explicit-only (Codex `allow_implicit_invocation: false`, Claude `disable-model-invocation`), set in overlays.
- **Gemini CLI's future.** The move to Antigravity CLI for some tiers may shift Google's extension surface. Keep the Gemini overlay minimal until that settles.
- **Claude namespacing.** Moving Claude to a plugin changes every command name users type. That is why the Claude switch is deferred and optional.

## Non-goals

- Implementing or changing any adapter. This document only proposes the bundle and its phases.
- Replacing `install.sh` or the current Claude skill tree.
- Portable model-tier self-dispatch, portable hooks, or a portable subagent format. Quoin will not invent standards the ecosystem has not adopted.
- Publishing to public directories or marketplaces run by vendors.
- Recreating Codex approvals, sandboxing or OpenCode permissions inside Quoin. Each runtime keeps its native controls.
- Guessing install paths a runtime does not document.

## Open questions

- Should the bundle live in the repo as a generated tree (reviewable diffs, installable from git) or exist only as a release artifact? Codex and Gemini marketplaces both install from git, which favors committing it.
- Does Claude Code read `.agents/skills/`? If it does, one shared project folder could serve all four runtimes for repo-local installs.
- What should portable bodies say when delegation is unavailable: an inline fallback for every skill, or marking those skills `documentation-only` on that runtime?

## References

- Agent Skills specification: https://agentskills.io/specification
- Agent Skills overview: https://agentskills.io
- Agent Plugins 1.0 specification: https://agent-plugins.org/specification
- Agent Plugins specification repository: https://github.com/agentplugins/agent-plugins-spec
- Claude Code plugin manifest reference: https://code.claude.com/docs/en/plugins-reference
- Claude Code skills: https://code.claude.com/docs/en/skills
- Codex plugin packaging: https://developers.openai.com/codex/plugins/build
- Codex skills: https://learn.chatgpt.com/docs/build-skills
- Codex subagents: https://developers.openai.com/codex/subagents
- Codex hooks: https://learn.chatgpt.com/docs/hooks
- AGENTS.md: https://agents.md
- Gemini CLI extension reference: https://geminicli.com/docs/extensions/reference/
- Gemini CLI skills: https://geminicli.com/docs/cli/skills/
- Gemini CLI subagents: https://geminicli.com/docs/core/subagents/
- Gemini CLI hooks: https://geminicli.com/docs/hooks/
- OpenCode skills: https://opencode.ai/docs/skills/
- OpenCode agents: https://opencode.ai/docs/agents/
- OpenCode commands: https://opencode.ai/docs/commands/
- OpenCode plugins: https://opencode.ai/docs/plugins/
- MCP prompts (2025-11-25): https://modelcontextprotocol.io/specification/2025-11-25/server/prompts
- Third-party report on Agent Plugins launch clients: https://codex.danielvaughan.com/2026/08/08/agent-plugins-1-0-open-standard-codex-cli-portable-skills-mcp-packaging/
