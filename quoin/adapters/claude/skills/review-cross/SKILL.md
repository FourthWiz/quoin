---
name: review-cross
description: "Reviews a task's change across its repos, focused on consumers of changed interfaces. Use for: /review-cross, 'review across repos'."
model: opus
context: fork
allowed-tools: Read, Grep, Glob, Bash(python3:*), Bash(git:*)
---

# Review across repositories

*Portable intent doc: `quoin/core/skills/review-cross.md`*

You review a change that spans several repositories of one project. The per-stage reviews already saw each repository on its own; your job is the seams: a provider that changed under consumers that did not, or a consumer that no longer matches its provider. You report; you never edit code.

## Inputs

The argument is the task branch, optionally followed by the task name (`/review-cross <branch> [<task>]`). With no argument, use the current branch of the repository you are in. The task name defaults to the branch's last path segment.

## Step 1: Build the pack

From the project root (the folder holding `.workflow_artifacts/`), run:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review_cross_pack.py --branch "<branch>" --project-root "<project root>" --out ".workflow_artifacts/<task>/review-cross-pack.md"
```

Run it as that one command, with absolute or project-relative paths and no `cd` or `&&` chain: this skill may run Python and git commands and nothing else in the shell. Read the pack once. It holds the impact report, each touched interface with the current code of its consumers (with line numbers), and every repository's diff. If it says no repository has changes on that branch, reply with that and stop.

## Step 2: Check each touched interface

For every interface in the pack:

- **Provider side changed:** decide what changed in the contract (a route path or method, a field name or type, a required field, a status code, an event topic or payload, a package's exported name or signature). For each consumer listed, check its code still matches. A consumer the diff did not touch is the most likely place for a break. Read more of a consumer file when the excerpt is not enough, and use the Grep tool (not a shell `grep`) across that repository for other call sites of the same contract.
- **Consumer side changed:** check the new use matches what the provider offers today.
- **Also check** changes that create a contract the graph does not list yet (a new route a client calls, a new topic) and whether both sides agree.

Stay on cross-repository correctness. Leave style and anything a single-repository review already covers.

## Step 3: Write the findings

Save `.workflow_artifacts/<task>/review-cross.md` with one command, the text on stdin:
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fsops.py write-atomic --parents ".workflow_artifacts/<task>/review-cross.md" <<'EOF'` … `EOF`. The file reads:

```markdown
# Cross-repository review: <branch>

Repositories: <repo (n files)>, ...
Interfaces checked: <name (kind)>, ...

## Findings

1. **<blocker | major | minor>** `<repo>/<path>:<line>`: <what breaks and why, naming the interface>.
   Fix: <the concrete change, in that file>.

## Verdict

<APPROVED | CHANGES REQUESTED>: <one line>
```

Severity: **blocker** breaks a consumer at run time or in its build; **major** breaks it in some cases or leaves the two sides disagreeing on data; **minor** is a compatibility risk worth a note. Every finding must name a repository, file and line and give a fix; drop any you cannot pin down. No findings: say so and give `APPROVED`. Any blocker or major: `CHANGES REQUESTED`.

## Step 4: Reply

Reply with the findings as written, one line each, and the verdict. Do not edit code, commit or push.
