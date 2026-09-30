---
name: discover
description: "Scans all repos in the project folder and saves inventory, architecture overview, and dependency map to memory. Use for: /discover, 'scan my repos', 'map the codebase', 'what repos do I have', 'index the project', 'learn my codebase'."
model: opus
---

# Discover


*Portable intent doc: `quoin/core/skills/discover.md`*

You scan all repositories in the project folder and produce a structured inventory that gets saved to memory. This is the "onboarding" step — run it once when you set up the workflow, and again whenever the repo landscape changes.

## §0' Pollution dispatch (execute after §0 / §0c if present — before skill body)

This skill runs in the user's current session. If the session is polluted (high context from
prior work), self-dispatch as a fresh subagent to avoid paying the pollution tax.

Detection:
  - Read the most-recent session-state file: `.workflow_artifacts/memory/sessions/<today>-<task>.md`
    OR the fallback `.workflow_artifacts/memory/pollution-score-latest.txt`.
  - Parse the `pollution_score: N` field (integer).
  - If N >= POLLUTION_THRESHOLD (default: env QUOIN_POLLUTION_THRESHOLD or 5000):
    session is polluted.
  - Sentinel check: if the user's prompt starts with `[no-redispatch]`: skip dispatch.
  - If a prior §0 dispatch already fired in this session: already in fresh context, skip §0'.

Dispatch action (when pollution detected AND no sentinel AND no prior §0 dispatch):
  Determine dispatch contract fields:
    - Use the current working directory as the project root absolute path.

  If task description cannot be determined:
    Emit: `[quoin-S-1: cannot extract per-skill dispatch contract; running in main]`
    Proceed with skill body.

  Otherwise spawn an Agent subagent:
    model: "opus"
    description: "discover — pollution-isolated dispatch"
    prompt: "[no-redispatch]\n/discover <project root absolute path>"

  Wait for the subagent. Return its output as your final response. STOP.

Fail-OPEN path:
  Full AskUserQuestion Question/Header/description wording for every branch below:
  memory/dispatch-guide.md §0' verbose reference ("Verbatim AskUserQuestion wording").
  If Agent tool unavailable or errors — classify the error first:

  - Autonomous-class (checked FIRST, before 1M-credit or generic classification): if the
    incoming prompt carries the `[autonomous]` sentinel, then on ANY §0' dispatch-failure or
    1M-context-credit error, proceed at current tier fail-OPEN and DO NOT call `AskUserQuestion`
    — skip the 1M-credit-class and generic branches below entirely. Print
    `[quoin-autonomous: §0' dispatch failed; proceeding fail-OPEN at current tier]` and proceed
    with skill body (treat as bare [no-redispatch]).
  - 1M-credit-class: if the error text contains the substring
      `Usage credits required for 1M context`:
      The §0' opus dispatch hit a 1M-context credit mismatch (IVG-89). Detection via
      model-name is impossible; this post-dispatch error string is the only reliable signal.
      Issue an `AskUserQuestion` — Option 1 label "Abort — I'll switch with /model first";
      Option 2 label "Proceed in-session at parent tier".
      On Option 1: print `[quoin: 1M-context credit mismatch; abort per user choice —
      switch with /model and re-invoke /discover]` and STOP. Do NOT proceed to skill body.
      On Option 2: print `[quoin: 1M-context credit mismatch; proceeding in-session at
      parent tier — run /model to switch to standard context for a permanent fix]` and
      proceed with skill body.
  - Any other error (non-1M): Issue an `AskUserQuestion` (generic wording; Option 1 = abort,
      Option 2 = proceed in the current polluted session).
      On Option 1: print `[quoin-S-1: pollution dispatch unavailable; proceeding in current session]`
      and STOP. Do NOT proceed to skill body.
      On Option 2: print `[quoin-S-1: pollution dispatch unavailable; proceeding in current session]`
      and proceed with skill body.

Otherwise (score below threshold OR sentinel OR §0 dispatched OR session-state unreadable):
proceed to skill body.

## §0″ Minimum-tier guard (execute after §0 / §0c / §0’ if present — before skill body)
This skill is declared model: "opus". If the executing agent is running on a model
strictly CHEAPER than opus, it silently up-dispatches to an Opus subagent (mirrors §0 down-dispatch).

<!-- §0doubleprime-begin -->
Detection:
  - Read your current model from system context ("powered by the model named X").
  - Tier order: haiku < sonnet < opus. declared_tier = opus.
  - Disable switch: if env QUOIN_DISABLE_MINTIER_GUARD=1 → skip entirely, proceed to skill body
    (silent skip — no advisory; this is explicit opt-out behavior by design).
  - Sentinel: if the prompt starts with bare [no-redispatch] → skip, proceed to skill body.
  - Fire condition: current_tier < declared_tier AND no [no-redispatch] AND guard not disabled.

On fire (happy path — silent up-dispatch):
  spawn an Agent subagent:
    model: "opus"
    description: "discover — min-tier up-dispatch"
    prompt: "[no-redispatch]\n<original user input verbatim>"
  Wait for the subagent. Return its output as your final response. STOP.

Fail-OPEN path (fires only when Agent dispatch fails). Full AskUserQuestion Question/Header/
description wording for every branch below: memory/dispatch-guide.md §0″ verbose reference
("Verbatim AskUserQuestion wording"). Classify the error text BEFORE proceeding:

  - Autonomous-class (checked FIRST, before 1M-credit or generic classification): if the
    incoming prompt carries the `[autonomous]` sentinel, then on ANY §0″ dispatch-failure or
    1M-context-credit error, proceed at current tier fail-OPEN and DO NOT call `AskUserQuestion`
    — skip the 1M-credit-class and generic branches below entirely. Print
    `[quoin-mintier-autonomous: §0″ dispatch failed; proceeding fail-OPEN at current tier]` and
    proceed to skill body (treat as bare [no-redispatch]).

  - 1M-credit-class: if error text contains `Usage credits required for 1M context`:
      Issue AskUserQuestion (full Question/Header wording: memory/dispatch-guide.md
      §0″ verbose reference):
        Option 1:
          label: "Abort — I'll switch with /model first"
        Option 2:
          label: "Proceed in-session at parent tier"
      On Option 1: print `[quoin-mintier: 1M-context credit mismatch; abort per user choice —
      switch with /model and re-invoke /discover]` and STOP.
      On Option 2: print `[quoin-mintier: 1M-context credit mismatch on opus up-dispatch;
      proceeding in-session at parent tier — run /model to switch to standard context]`
      and proceed to skill body (treat as bare [no-redispatch]).

  - Any other error: Issue AskUserQuestion (labels verbatim — drift relies on equality):
        Option 1:
          label: "Abort — run from an Opus session"
        Option 2:
          label: "Proceed at current tier (under-powered)"
      On Option 1: print `[quoin-mintier: aborted; re-invoke /discover from an Opus session]` and STOP.
      On Option 2: print `[quoin-mintier: min-tier up-dispatch unavailable; proceeding at current tier per user choice]`, then proceed to skill body (treat as bare [no-redispatch]).
<!-- §0doubleprime-end -->

## Autonomous mode bootstrap

Parse the incoming prompt for the `[autonomous]` sentinel (may stack after `[no-redispatch]`,
e.g. `[no-redispatch] [autonomous]`, since `/run` prefixes it onto this skill's spawn prompt).
If present, set internal state `_AUTONOMOUS=true` for the remainder of this skill's execution;
strip the sentinel before further parsing. Default `_AUTONOMOUS=false` (opt-in only).
`_AUTONOMOUS` gates ONLY the repo main spec offer below (see "Repo main spec (optional offer)").
It does not change §0'/§0″ dispatch behavior above (those are generated separately) or any
other part of the scan.

## Model requirement

Uses the strongest model (Opus) because understanding how services relate requires deep reasoning across multiple codebases.

## Session bootstrap

If your incoming prompt contains `[quoin-onbehalf]`: SKIP this cost-ledger self-write — the spawning orchestrator records this row on your behalf (D-1). Strip `[quoin-onbehalf]` at bootstrap step 0 (per-spawn, non-inherited — do not propagate to children).

Cost tracking (conditional): `/discover` can run standalone (no task context) or as part of a task via `/run`. Only append to the cost ledger if a task name was explicitly provided or is determinable from the invocation context. If running standalone, skip cost recording. If the condition holds: append your session to `.workflow_artifacts/<task-name>/cost-ledger.md` — phase: `discover` — format/rules: `__QUOIN_HOME__/memory/cost-ledger-format.md`.

<!-- quoin:ledger-self-write -->

## What to scan

### Incremental scan — skip unchanged repos

Before scanning each repo, check if a previous scan recorded the repo's HEAD commit:

1. Read `.workflow_artifacts/cache/_staleness.md` if it exists. If not found, fall back to `.workflow_artifacts/memory/repo-heads.md` for backward compatibility with pre-cache workflow versions. This file maps repo names to their `git rev-parse HEAD` values from the last `/discover` run.
2. For each repo in the project folder, run `git rev-parse HEAD` and compare against the stored value.
3. **If HEAD matches the stored value: skip the full scan for that repo.** Its inventory, dependencies, and API surface have not changed. Report to the user: "Skipping <repo-name> — unchanged since last scan (HEAD: <short-hash>)."
4. **If HEAD differs or no stored value exists: perform the full scan** as described below.
5. After completing all scans, write `.workflow_artifacts/cache/_staleness.md` with the current HEAD values for all repos (including unchanged ones). **Clock-reset invariant (D-06): always write the current run timestamp to the `Updated` column for ALL repos on every run, including repos whose HEAD was unchanged and whose scan was skipped.** This resets the staleness clock even when no content changes — so a near-free incremental scan does not leave the discovery memory appearing stale. Without this invariant, a project whose repos never change would trigger the session-start staleness banner (`S-5`) on every session. Create the `.workflow_artifacts/cache/` directory if it does not exist. Also write `.workflow_artifacts/memory/repo-heads.md` with the same data (backward compatibility — `/architect` and `/run` may still read it until they are updated in later stages).

**Session-start staleness trigger:** the `S-5` sessionstart.sh hook and `/start_of_day` Step 1c read this `Updated` timestamp to determine whether discovery memory is stale. Run `/discover` when the session-start banner fires, or when repos have changed significantly.

Format for `.workflow_artifacts/cache/_staleness.md`:

| Repo | HEAD | Updated |
|------|------|---------|
| <repo-name-1> | <full-sha> | <ISO-timestamp> |
| <repo-name-2> | <full-sha> | <ISO-timestamp> |

The `repo-heads.md` backward-compat write uses the old 2-column format (no `Updated` column):

| Repo | HEAD |
|------|------|
| <repo-name-1> | <full-sha> |
| <repo-name-2> | <full-sha> |

**Important:** When the user explicitly requests a full re-scan (e.g., "rescan everything", "force rediscover"), ignore the HEAD cache and scan all repos.

Starting from the project root folder, examine every top-level directory. For each repository/service found:

### Nested-root sanity scan (IVG-119)

Before the per-repo inventory, run one project-wide check for accidental nested/duplicate `.workflow_artifacts/` roots (subproject dirs that create their own root break `/checkpoint --restore` and artifact discovery):

```
PROJECT_ROOT="$(pwd)"
python3 __QUOIN_HOME__/scripts/nested_root_check.py --project-root "$PROJECT_ROOT" --format text
```

Non-blocking — NEVER fails `/discover` (fail-OPEN). Exit 1 lists offending nested roots; fold those paths into the final user-facing summary (below). Exit 0 → single canonical root, nothing to report. Exit 2/3 or script missing → skip silently.

### Per-repo inventory

1. **Identity**
   - Repo name (directory name)
   - Primary language(s) and framework(s) (detect from package.json, go.mod, Cargo.toml, requirements.txt, pom.xml, build.gradle, etc.)
   - Runtime/platform (Node.js, Go, Python, JVM, etc.)
   - Build system (npm, yarn, pnpm, make, gradle, maven, cargo, etc.)

2. **Structure**
   - Key directories and what they contain (src/, lib/, api/, cmd/, internal/, etc.)
   - Entry points (main files, index files, server startup)
   - Configuration files and what they control
   - Test structure (where tests live, test framework used)

3. **External dependencies**
   - Key libraries/frameworks (not every transitive dep — the important ones that define the architecture)
   - External services called (from config files, environment variables, API client code)
   - Database connections (type, connection strings patterns)
   - Message queues, event buses, cache systems

4. **API surface**
   - Exposed endpoints (REST routes, GraphQL schemas, gRPC protos)
   - Published events/messages
   - Shared libraries or packages exported

### Cross-repo analysis

After scanning individual repos, analyze how they connect:

1. **Service communication map**
   - Which service calls which (HTTP, gRPC, message queues)
   - Trace the connections by matching: API client code in one repo → endpoint definitions in another
   - Shared databases (multiple services reading/writing the same DB)
   - Shared message topics/queues (publishers and consumers)

2. **Dependency graph**
   - Shared internal libraries or packages
   - Common configuration or infrastructure patterns
   - Deployment dependencies (what must deploy before what)

3. **High-level architecture**
   - System purpose — what does this collection of services do together?
   - Request flow — how does a typical user request flow through the system?
   - Data flow — where does data enter, how is it transformed, where does it end up?
   - Key architectural patterns (microservices, monolith, event-driven, CQRS, etc.)

## How to scan

Use a combination of approaches for efficiency:

- **File patterns** — glob for known config files (package.json, go.mod, docker-compose.yml, etc.) to quickly identify repos and their tech stacks
- **Grep for connections** — search for HTTP client calls, database connection strings, queue publish/subscribe patterns, environment variable usage
- **Read key files** — entry points, route definitions, config files, README files, docker-compose, Makefiles
- **Don't read everything** — this is a survey, not a deep dive. Read enough to understand the architecture, not every line of business logic.

Use subagents to scan repos in parallel when possible.

## Output

# V-05 reminder: T-NN/D-NN/R-NN/F-NN/Q-NN/S-NN are FILE-LOCAL.
# When referring to a sibling artifact's task or risk, use plain English (e.g., "the parent plan's T-04"), NOT a bare T-NN token. See format-kit.md §1 / glossary.md.
Write `repos-inventory.md`, `architecture-overview.md`, `dependencies-map.md`, and `git-log.md` in v3 format per the §5.4 Class A writer mechanism. Reference files (apply HERE at the body-generation write-site, per format-kit.md §1 / lesson 2026-04-23): `__QUOIN_HOME__/memory/format-kit.md` (primitives — markdown table for the per-repo inventory rows; caveman prose for architecture-overview narrative; markdown table for dependencies-map rows; terse numbered list with date-prefixes for git-log entries), `__QUOIN_HOME__/memory/glossary.md` (abbreviation whitelist), `__QUOIN_HOME__/memory/terse-rubric.md` (prose discipline inside narrative sections of architecture-overview). Per-file primitive picks: `repos-inventory.md` → markdown table for the per-repo rows; `architecture-overview.md` → caveman prose + ASCII diagram; `dependencies-map.md` → markdown table for cross-service deps + terse list for shared resources; `git-log.md` → terse numbered list grouped by repo+branch. After composing each file's body to `{path}.body.tmp`, run `python3 __QUOIN_HOME__/scripts/validate_artifact.py {path}.tmp` (filename auto-detection — each file matches its named type per T-15 format-kit.sections.json additions). On V-failure: retry-once with primitive-discipline reminder; on persistent failure, fall back to v2-style terse-rubric-only write. Atomic rename per the standard Step 6 graceful pattern. Write all cache entries (`_index.md`, `_deps.md`, file-stem entries) per the CLAUDE.md "Knowledge cache" schema (frontmatter with path/hash/updated/updated_by/tokens, then sections: Purpose, Key Exports, Dependencies, Patterns, Integration Points, Notes) — the cache schema pre-dates format-kit and has its own enforced shape; do NOT apply format-kit primitives to cache entries.

Save all findings to `<project-folder>/.workflow_artifacts/memory/`:

### `repos-inventory.md`

```markdown
# Repository Inventory

Last scanned: <date>

## <repo-name-1>

- **Language:** TypeScript
- **Framework:** NestJS
- **Runtime:** Node.js 18
- **Build:** yarn
- **Purpose:** <1-2 sentence description>
- **Key directories:**
  - `src/modules/` — feature modules
  - `src/common/` — shared utilities
  - `test/` — Jest tests
- **Entry point:** `src/main.ts`
- **Key dependencies:** TypeORM, Redis (ioredis), Kafka (kafkajs)
- **Exposed APIs:** REST on port 3000 (see `src/modules/*/controller.ts`)
- **External calls:** Payment service (HTTP), User service (gRPC), PostgreSQL, Redis, Kafka

## <repo-name-2>
...
```

### `architecture-overview.md`

```markdown
# Architecture Overview

Last scanned: <date>

## System purpose
<What this system does, who uses it, 3-5 sentences>

## Service map

<ASCII diagram or structured description of how services connect>

Example:
```
[API Gateway] → [User Service] → [PostgreSQL]
                                → [Redis cache]
             → [Payment Service] → [Stripe API]
                                  → [PostgreSQL]
             → [Notification Service] ← Kafka ← [Payment Service]
                                     → [SendGrid API]
```

## Communication patterns

### Synchronous (HTTP/gRPC)
- <service A> → <service B>: <what for> (<protocol>)

### Asynchronous (queues/events)
- <topic/queue>: published by <service>, consumed by <service(s)> — <what for>

### Shared data stores
- <database/cache>: accessed by <services> — <what data>

## Request flows

### <Flow 1: e.g., User registration>
1. Client → API Gateway → ...
2. ...

### <Flow 2: e.g., Payment processing>
1. ...

## Key architectural decisions
- <Pattern or decision and why it was made, if apparent from the code>

## Deployment topology
- <What deploys where, if discoverable from docker-compose, k8s configs, CI files>
```

### `dependencies-map.md`

```markdown
# Cross-Service Dependencies

Last scanned: <date>

## Dependency graph

### <Service A>
- **Depends on:** <Service B> (HTTP, for auth), PostgreSQL, Redis
- **Depended on by:** <Service C> (Kafka events), <API Gateway> (HTTP)

### <Service B>
...

## Shared resources
- **PostgreSQL (main):** used by <services>
- **Redis:** used by <services> — <for what>
- **Kafka topics:**
  - `payment.completed`: published by Payment, consumed by Notification, Analytics
  - ...

## Deployment order constraints
- <Service B> must be available before <Service A> (hard dependency on auth endpoint)
- <Database migrations> must run before any service deployment

## Integration risks
- <Known fragile integration points, tight coupling, missing error handling observed>
```

### `git-log.md`

As part of discovery, also capture recent git activity across all repos. This gives every downstream skill context on what's been changing and why.

```bash
# For each repo
git -C <repo-path> log --all --oneline --date=short --format="%h %s — %ad" -20
```

For each commit, briefly describe the *logic* of the change (not just file names):
```bash
git -C <repo-path> diff-tree --no-commit-id --name-status -r <hash>
```

Format:

```markdown
# Recent Git Activity

Last updated: <datetime>

## <repo-name>
### <branch-name>
- `<short-hash>` <commit message> — <date>
  <1-line: what changed and why — the logic, not just files>
- `<short-hash>` ...

## <other-repo>
...
```

Keep the last ~50 commits across all repos, newest first. The goal is that any skill reading this file understands the recent momentum and direction of the project.

## Cache population

In addition to the output files above, populate the knowledge cache tree under `.workflow_artifacts/cache/`. This cache provides structured per-file and per-module summaries that downstream skills can read instead of re-reading source files.

### Scan subagent instructions

When scanning repos in parallel, spawn a subagent per repo (or batch 2-3 small repos) with the following instructions. This is the complete instruction template — not an appendix to other instructions.

> You are a read-only repo scanner for the `/discover` skill. Your job is to extract structured facts from this repository AND write structured cache entries. Do NOT do architectural analysis — just report and cache what you find.
>
> Repo path: <repo-path>
> Project root: <project-folder>
>
> **Part 1 — Per-repo inventory (report back as text)**
>
> Scan and report the following:
>
> 1. IDENTITY
>    - Repo name, primary language(s), framework(s), runtime, build system
>    - Detected from: package.json, go.mod, Cargo.toml, requirements.txt, etc.
>
> 2. STRUCTURE
>    - Key directories and what they contain
>    - Entry points (main files, index files, server startup)
>    - Configuration files and what they control
>    - Test structure (where tests live, framework used)
>
> 3. EXTERNAL DEPENDENCIES
>    - Key libraries/frameworks (important ones, not every transitive dep)
>    - External services called (from config, env vars, client code)
>    - Database connections (type, patterns)
>    - Message queues, event buses, cache systems
>
> 4. API SURFACE
>    - Exposed endpoints (REST routes, GraphQL schemas, gRPC protos)
>    - Published events/messages
>    - Shared libraries or packages exported
>
> **Part 2 — Cache output (write files directly)**
>
> In addition to reporting your findings above, write structured cache entries to `.workflow_artifacts/cache/<repo-name>/`. Create:
>
> - `_index.md` — repo summary (200-300 tokens)
> - `_deps.md` — dependencies (100-200 tokens)
> - `<dir>/_index.md` — for each key directory with 3+ source files (150-300 tokens each)
> - `<dir>/<file-stem>.md` — for key files only: entry points, APIs, models, configs (50-150 tokens each)
>
> Use the cache entry format defined in CLAUDE.md (frontmatter with path/hash/updated/updated_by/tokens, then sections: Purpose, Key Exports, Dependencies, Patterns, Integration Points, Notes). Omit sections that don't apply.
>
> Only write cache entries for files you actually read. Do not invent summaries for files you did not examine. It is better to have a sparse cache than an inaccurate one.
>
> Directory creation: create the `.workflow_artifacts/cache/<repo-name>/` directory tree as needed (use `mkdir -p` via Bash or let the Write tool create parent directories).
>
> Token budget enforcement: after writing each cache entry, do a rough token count (word count / 0.75, plus ~20-30% for code-heavy entries). If the entry exceeds the budget by more than 50%, trim it — cut the least important section (usually Notes, then Patterns, then Integration Points).
>
> **Output constraint:** Keep your Part 1 text output under ~3,000 tokens. Be concise — include file paths and brief summaries, not full code excerpts.

### What each scan subagent writes

For each repo scanned, the subagent creates:

1. **Repo index:** `.workflow_artifacts/cache/<repo-name>/_index.md`
   - Derived from the per-repo inventory data (identity, structure, entry points, key dependencies)
   - Target: 200-300 tokens
   - Frontmatter: `path: <repo-name>`, `hash: <HEAD>`, `updated: <ISO>`, `updated_by: /discover`, `tokens: <N>`

2. **Repo deps:** `.workflow_artifacts/cache/<repo-name>/_deps.md`
   - Derived from the external dependencies and API surface data
   - Target: 100-200 tokens
   - Same frontmatter pattern

3. **Module indexes:** `.workflow_artifacts/cache/<repo-name>/<dir-path>/_index.md`
   - One per key directory that contains 3+ source files
   - Summarize the directory's purpose, what files it contains, common patterns
   - Target: 150-300 tokens
   - Only create for directories the subagent actually examined — do not invent summaries for unread directories

4. **File entries:** `.workflow_artifacts/cache/<repo-name>/<dir-path>/<file-stem>.md`
   - Only for **key files**: entry points, API route definitions, model/schema definitions, configuration files, and files with complex business logic (>100 lines with non-trivial logic)
   - Do NOT create file entries for: test files, type definition files, simple utility files (<50 lines), generated files, lock files, or files whose content is adequately captured in the module `_index.md`
   - Target: 50-150 tokens per entry
   - Use the standard cache entry format (see CLAUDE.md "Knowledge cache" section)

### What the main `/discover` session writes

After all subagents complete, verify cache writes (see below), then write the existing output files:

1. **Root index:** `.workflow_artifacts/cache/_index.md`
   - Lists all repos with their purpose (1 sentence each), primary language, and last-updated timestamp
   - Derived from the repos-inventory.md content
   - Target: 100-200 tokens
   - Frontmatter: `path: .`, `hash: <latest HEAD across repos>`, `updated: <ISO>`, `updated_by: /discover`, `tokens: <N>`

2. **Staleness file:** `.workflow_artifacts/cache/_staleness.md` (see incremental scan section above)

### Cache write verification

After all subagents complete, the main `/discover` session must verify that cache writes succeeded:

For each repo that was scanned (not skipped), check that `.workflow_artifacts/cache/<repo-name>/_index.md` exists. If missing:
- Include a warning in the user-facing summary: "WARNING: cache write failed for <repo-name> — cache entries may be incomplete for this repo"
- Do NOT fail the `/discover` run. Cache is advisory; the scan output files (repos-inventory.md, etc.) are the authoritative output.
- Proceed with writing the root `_index.md` and `_staleness.md` regardless.

Report the warning counts in the "After scanning" summary (see below).

### Incremental cache updates

When `/discover` runs incrementally (some repos skipped because HEAD unchanged):
- **Skipped repos:** Do NOT touch their cache entries. They are still valid.
- **Scanned repos:** Overwrite all cache entries for that repo (the subagent produces fresh entries from its reads).
- **Root index:** Always rewrite `_index.md` (it's small and includes all repos, even skipped ones — keep their existing entries and update the scanned repos).

## After scanning

### Structured-output hook (optional)

After writing the four markdown files (`repos-inventory.md`, `architecture-overview.md`,
`dependencies-map.md`, `git-log.md`) and verifying cache writes, invoke:

```
python3 __QUOIN_HOME__/scripts/generate_discovery_map.py "$PROJECT_ROOT" --quiet
```

This emits a structured `discovery-map.json` index at
`<project_root>/.workflow_artifacts/discovery-map.json`. The hook is
best-effort: a non-zero exit prints a single-line warning and continues — never
aborts `/discover`. The JSON index is supplemental; the markdown files remain
the authoritative source.

### Repo main spec (optional offer)

**Suppression guard FIRST (self-clear-on-honor):** check for the bootstrap marker file `.workflow_artifacts/.init-bootstrap-active`. If it EXISTS:
1. Suppress this entire offer.
2. Immediately DELETE the marker as part of this same step: `python3 __QUOIN_HOME__/scripts/fsops.py rm ".workflow_artifacts/.init-bootstrap-active"`.

This self-clear IS the stale-recovery mechanism — it bounds the marker's lifetime to exactly one `/discover` invocation, so even if a prior `/init_workflow` aborted before its Step-6.7 cleanup (e.g. the Step-6.5 Serena "Install Serena" session-restart path), the NEXT `/discover` consumes-and-clears the marker instead of suppressing the offer forever. After clearing, print one advisory line that does NOT assert an active bootstrap (accurate whether the marker came from a live bootstrap or was consumed as stale):

```
[quoin: repo-spec offer suppressed — bootstrap marker present; marker cleared]
```

Then proceed to the "Tell the user" summary below. This is a file-existence check, so it works whether `/discover` runs inline or is dispatched as a subagent. (Init's Step-6.7 marker removal remains a harmless belt-and-suspenders no-op.)

Only when the marker is ABSENT does the offer proceed:

**Autonomous mode:** if `_AUTONOMOUS` is true, skip this entire offer — whether `.workflow_artifacts/spec.md` is absent (the DRAFT prompt) or present (the REFRESH prompt), do NOT call `AskUserQuestion`, and NEVER auto-write or auto-modify `.workflow_artifacts/spec.md` in either case. The repo main spec is a human-owned artifact; autonomous must not fabricate or silently refresh it. Print one advisory line: `[quoin: repo-spec offer skipped — autonomous mode; spec.md not written]`. Then proceed directly to the "Tell the user" summary below.

- If `.workflow_artifacts/spec.md` is ABSENT: `AskUserQuestion` offering to DRAFT a repo main spec from the freshly discovered structure plus a short user prompt ("what is this repo about?").
  - Decline → one-line advisory, no file, no error.
  - Accept → compose the five repo-spec headings (`## Context`, `## Goals`, `## Capabilities`, `## Acceptance criteria`, `## Non-goals`) grounded in the just-written inventory files, write `.workflow_artifacts/spec.md.tmp` with the Write tool, then run `python3 __QUOIN_HOME__/scripts/fsops.py mv ".workflow_artifacts/spec.md.tmp" ".workflow_artifacts/spec.md"`, then `python3 __QUOIN_HOME__/scripts/validate_artifact.py .workflow_artifacts/spec.md` → expect exit 0.
- If `.workflow_artifacts/spec.md` EXISTS: `AskUserQuestion` offering to REFRESH it from the current structure.
  - On accept, draft the updated spec, surface a DIFF against the existing file, and require explicit confirmation before writing — never overwrite silently.
  - On confirm, write `<path>.tmp` with the Write tool, then run `python3 __QUOIN_HOME__/scripts/fsops.py mv "<path>.tmp" "<path>"`, then `validate_artifact.py` exit 0.
  - Decline → one-line advisory, no write, no error.

Tell the user:
- How many repos were found
- Brief summary of the architecture
- Recent git activity highlights (what's been actively worked on)
- Any interesting findings (tight coupling, missing tests, potential risks observed)
- These files are now available to `/architect`, `/plan`, `/critic`, `/review`, and `/start_of_day` as baseline context
- Which repos were skipped (unchanged since last scan) and which were re-scanned
- Knowledge cache populated under `.workflow_artifacts/cache/` — <N> repos cached, <N> module indexes, <N> file entries created (warnings: <N> repos with failed cache writes, if any)
- Nested-root warnings (IVG-119): if the nested-root sanity scan reported exit 1, list the offending `.workflow_artifacts/` paths and note the corrective action (move/remove, or bless via `.quoin-nested-ok` / `QUOIN_NESTED_ROOT_EXCLUDE`). Omit this line when the scan was clean.

## When to re-run

Suggest re-running `/discover` when:
- New repos are added to the project folder
- Major architectural changes happen (new services, new communication patterns)
- Before a large `/architect` session to ensure context is fresh
- `/start_of_day` finds the git-log.md is stale (it will suggest this)
- When you want to force a full re-scan regardless of HEAD changes (say "rescan all repos" or similar)
