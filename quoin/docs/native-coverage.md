# Native coverage: the running list

What each part of Quoin was built to make up for, how much of it Claude Code now
covers natively, and what Quoin does about it today. Started from the pivot
audit's verdict table.

**Upkeep, monthly, about ten minutes:** read the Claude Code changelog against
this list, update any row whose native column changed, put today's date in
"Last checked" for every row you looked at, and run one task on the open model
(`quoin open`). When a row's native column covers the whole job, plan the
removal of that part.

| Component | What it made up for | Native in Claude Code | Quoin now | Last checked |
| --- | --- | --- | --- | --- |
| Staged route across repositories (`/quoin:run` r2, `parallel_stages.py`, one branch name everywhere) | No coordination across repositories | None: worktrees, review and memory are per repository | Kept as the core; stage list from `impact.py --plan` | 2026-11-21 |
| Interface graph, `/discover`, discovery map, dirty and staleness markers | No project-level map | None across repositories; language-server plugins cover symbols inside one | Kept; map schema 2 with `interfaces`, generated `dependencies-map.md` and path-scoped rules | 2026-11-21 |
| Cross-repository review (`review-cross`) | Per-repository review misses consumers | `/code-review` reviews one repository's diff | Kept; runs once after a staged change | 2026-11-21 |
| Linked PRs (`/pr` across repositories, `link_prs.py`) | Per-repository PR flow | None for linked PRs | Kept | 2026-11-21 |
| Worktree for a non-git project root (`worktreecreate.sh`, `workspace.py`, `run_isolation.py`) | `--worktree` needs a git repository | `WorktreeCreate` hook is the documented extension point | Kept as that hook; creates worktrees without a dispatch hint | 2026-11-21 |
| Isolation claim (one session per checkout) | Concurrent sessions overwrite each other | None | Kept in the capture mod | 2026-11-21 |
| Deterministic checks (`affected_tests`, `ci_mirror`, `known_red`, `verify_claims`, `branch_hygiene`, `decision_gate_guard`) | Models that skip tests or claim work they did not do | `/goal` judges only what the transcript shows | Kept; `affected_tests.py` output is the evidence in every `/goal` condition | 2026-11-21 |
| Task record, journal and session titles (capture layer) | No record of plain sessions | `--resume`, session titles, auto memory (per repository, machine-local) | Kept; titles feed the native resume picker | 2026-11-21 |
| Lessons and instruction edits (`lessons-learned.md`, inbox, `append_artifact.py`) | No memory across sessions | Auto memory: per repository, machine-local, unreviewed | Kept; harvested into an inbox, nothing applied without accept | 2026-11-21 |
| Model dispatch (Section 0, minimum-tier guard, 1M-credit handling) | Skills could not choose their model | Skill frontmatter `model`, `effort`, `context: fork` | Replaced by frontmatter in the kept skills | 2026-11-21 |
| Context guards (pollution, session age, checkpoints, compaction telemetry) | 200k windows, lossy compaction | 1M-token windows; compaction re-attaches skills and the plan; `/rewind` | Shrinking: one context-budget guard remains, in the open-model profile | 2026-11-21 |
| Heavy pipeline (`run_hard`, architect, plan, critic, revise, specify, enrich, gate, implement, end_of_task) | Models needed plans, critics and gates | Plan mode, `/code-review`, dynamic workflows | Frozen; retirement stage pending; kept on the `legacy` tag | 2026-11-21 |
| `/run` r0 and r1 | A default entry point | Plain session; `/code-review` | Still installed; r0 to be dropped in the retirement stage | 2026-11-21 |
| Day rituals (start/end of day, sleep, weekly review, capture_insight, session files) | No continuity | `--continue`, `--resume`, auto memory, Projects | Shrinking into capture-layer events; skills retire in the retirement stage | 2026-11-21 |
| Cost ledger and cost scripts | No cost visibility | Usage reporting, telemetry export, per-agent headers | To drop from skills in the retirement stage | 2026-11-21 |
| Supervisor, auto-resume, child watch, takeover, Stop and prompt hooks | Runs died at limits or compaction | `/goal` (runs under `-p`; pause at a usage limit documented only for interactive sessions) | Removed; `quoin run --autonomous` drives one `/goal` per stage; supervisor on the `legacy` tag | 2026-11-21 |
| Installer, manifest prune, drift validators, slim-rules generator | No packaging format | Plugins with `${CLAUDE_PLUGIN_ROOT}`, marketplaces, `claude plugin validate` | Replaced by the plugin; `quoin install` stages and registers it | 2026-11-21 |
| Format kit, terse rubric, `/expand`, cache preambles, `/triage`, `/rollback` | Token cost of artifacts, routing | Larger windows; `/rewind` | To drop in the retirement stage | 2026-11-21 |
| Open-model routing (CCR router, `quoin models`) | Claude Code spoke only to Anthropic | `ANTHROPIC_BASE_URL` plus tier-alias variables | Plain environment profile (`quoin open`) tested and working with Qwen3 Coder on OpenRouter; CCR no longer needed for it | 2026-11-21 |
| Open-model pack (plan-first and review skills, gateway probe) | Weaker models skip planning and review; protocol failures look like task failures | None | Kept; loaded only in `quoin open` sessions | 2026-11-21 |
| Codex adapter | Codex had no extension points | Codex reads Agent Skills from `.agents/skills/` | Replaced by the shared skills folder (`quoin install --runtime codex`, `quoin skills build`) | 2026-11-21 |
| OpenCode adapter | A harness for open models | n/a | Split out to `quoin-opencode`; reads the shared skills folder | 2026-11-21 |
| Cockpit (Agentdesk, Stream Deck, VS Code extension, dashboard, context-tracker mod) and workctl | Personal cockpit, broker | Agent view, desktop parallel sessions, mods API | Split out to `quoin-cockpit` and `quoin-opencode` | 2026-11-21 |
| Benchmark harness | Evidence | `claude plugin eval` gives a with-and-without comparison | Kept as is; to move out of the package; open-model arm to add | 2026-11-21 |
