# status

Runtime-neutral intent for the status skill. Any runtime adapter (Claude,
Codex, …) that implements this skill should match the contract described here.

## Purpose

Render a read-only ASCII pipeline graph showing the current quoin workflow stage
for the active task. Never writes file artifacts. Never invokes another workflow
phase. Never commits or modifies source.

## When to use

- Any time the user wants to see where they are in the workflow.
- User says "/status", "where am I", "what phase", "show progress",
  "workflow status", "pipeline graph", or "what stage".

## Inputs

Artifact filenames in the resolved task directory under `.workflow_artifacts/`:
- `architecture.md` → architect phase done
- `current-plan.md` → planning phase (or beyond)
- `critic-response-N.md` → N critic rounds completed (in planning loop)
- `gate-post-plan-*.md` or `gate-plan-*.md` → plan-gated (ready to implement)
- `gate-post-implement-*.md` or `gate-implement-*.md` → implement-gated (ready to review)
- `review-N.md` → N review rounds completed
- `gate-post-review-*.md` or `gate-review-*.md` → review-gated (ready to finalize)
- task under `.workflow_artifacts/finalized/` → done
- light `/run` task (`phases.log` or `brief.md`, no `current-plan.md`): the last
  non-skipped `phases.log` marker decides (frame/context → planning,
  implement/verify → implement, review → review, commit → committed); without
  `phases.log`, `review.md` → review, `context.md` → implement, `brief.md` →
  planning. `committed` means the local commit landed and `/pr` is next.

Optional CLI arguments: `--task NAME`, `--project-root PATH`, `--stage N-or-NAME`,
`--json`, `--watch [N]`, `--compact`, `--probe-git`.

## Outputs

ASCII pipeline graph on stdout showing the canonical quoin pipeline and the active
phase marked. Two render modes:

- **Full** (default): horizontal pipeline `discover -> architect -> … -> end_of_task`
  with the active node in brackets `[node]` and a `^ you are here` pointer.
- **Compact** (--compact): single-column layout, max 40 chars per line, with `>>>`
  marking the active node. Designed for agentdesk narrow panes.

`--json` output: a JSON object with keys `task`, `phase`, `critic_rounds`,
`review_rounds`, `stage`, `task_dir`.

`--watch [N]`: refresh loop — clears the terminal and redraws every N seconds
(default 5). Exits cleanly on KeyboardInterrupt.

## Constraints

- **Read-only.** The skill MUST NOT write any file artifact. No cost-ledger row.
- **No phase invocation.** The skill MUST NOT auto-invoke another workflow phase.
- **No commit or push.**
- **Project-root detection.** Walk up from invocation directory to find the
  directory containing `.workflow_artifacts/`. If not found, report and stop.
- **Active-task selection.** When `--task` is omitted, pick the most-recently-
  modified non-finalized task dir by max artifact mtime (not dir mtime — sync
  tools touch dirs). A dir with no non-empty artifacts does not qualify.
  Exclude: `memory/`, `cache/`, `finalized/`. Non-directory entries are naturally
  excluded by the `is_dir()` guard. `finalized/<task>` is reachable only via
  explicit `--task` (intentional asymmetry: auto-select shows in-flight work).
- **Implement-phase accuracy ceiling.** A task mid-implement (code written, gate
  not yet run) is indistinguishable from plan-gated/planning without `--probe-git`.
  This is documented degraded behavior, not a bug.
- **Plain ASCII safety.** Default output uses only printable ASCII plus newlines.
  Allowed set: letters, digits, punctuation in the ASCII range (0x20-0x7E), LF.
  No tab characters. Unicode arrows (→) are permitted but `->` is the fallback.

## Out of scope

- Model tier and self-dispatch grammar — runtime adapter handles these.
- The §0 self-dispatch mechanism — runtime-specific.
- The exact cost-resolution CLI or JSONL fallback — adapter-owned.
- Any guarantee about multi-stage task path resolution beyond delegating to the
  runtime's `path_resolve` utility (stage path is highest-uncertainty and leans
  on the runtime's resolver).
