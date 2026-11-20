---
name: status
description: "Shows the workflow pipeline graph with the active phase marked. Use for: /status, 'where am I', 'what phase am I in'. Read-only."
model: haiku
context: fork
---

# Status

You render a read-only ASCII pipeline graph showing the current quoin workflow stage for the active task.

*Portable intent doc: `quoin/core/skills/status.md`*

## Session bootstrap

Cost tracking note: `/status` is a read-only reporting skill. Do NOT append to the cost ledger. Phase: `status`. (Read-only skills skip cost recording — no task context write.)

## Process

Run the status script and print its output:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/status_graph.py [--task TASK_NAME]
```

If the user provided a task name, pass it via `--task`. Otherwise omit it and the script auto-selects the most-recently-modified task.

Optional flags the user may request:
- `--compact` — narrow-pane render (≤40 chars per line)
- `--json` — machine-readable JSON output
- `--watch [N]` — refresh every N seconds (default 5); note this runs indefinitely until Ctrl-C
- `--probe-git` — detect in-progress implement via git (slower)
- `--stage N-or-NAME` — for multi-stage tasks

Print the script's stdout directly to the user. If the script exits with code 1, print its stderr as an error message.

## §V Reconcile (read-only — no side-effect check)

<!-- §V-reconcile-begin -->
Before surfacing any task/PR status, run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/verify_claims.py --reconcile-tasks --project-root <project-root>` and derive the displayed status from the reconcile table, not from a cached narrative alone. If the reconcile exits 8, surface the contradiction rather than silently reporting the narrative version.
<!-- §V-reconcile-end -->

## Important behaviors

- **Read-only.** Never write any file. Never commit. Never push.
- **No phase invocation.** Do NOT auto-invoke /implement, /review, or any other skill.
- **Fast.** The script is read-only and cheap; it should return in well under 5 seconds (excluding --watch mode).
- **Project root.** The script walks up from cwd to find `.workflow_artifacts/`. If invoked from a subdirectory, it still finds the right root.
