# Cost estimators: decision record

Decided 2026-10-19 against Claude Code 2.1.303. This is a maintainer note, not deployed.

## Decision

Keep `cost_from_jsonl.py`, `agent_transcript_cost.py` and `cost_ledger_summary.py`. No documented native Claude Code source replaces them for any current consumer today. The transcript `cost-state` record does give a past session's total by id and is the closest candidate for finalize totals; it is not adopted because its scope differs (it includes subagent spend, which the ledger already carries as separate on-behalf rows, so preferring it would double-count), its schema is undocumented, and it is written only on normal exit.

Nothing in the three scripts, the four price-lookup sites or the pricing table changes. All figures from either source are client-side estimates, not authoritative billing.

## Native sources checked

| Source | URL | What it gives | Why it does not replace the estimators |
|---|---|---|---|
| Headless `--output-format json` / stream-json result | https://code.claude.com/docs/en/headless | `total_cost_usd` and a per-model breakdown on a `claude -p` result; "client-side estimates" | only for `-p` runs; quoin phases run interactively and the supervisor uses `--output-format text` |
| Agent SDK cost tracking | https://code.claude.com/docs/en/agent-sdk/cost-tracking | `total_cost_usd`, `modelUsage` including subagents; "client-side estimates, not authoritative billing data" | same scope as headless; no per-subagent split |
| `/usage` Session block | https://code.claude.com/docs/en/costs | session cost computed locally at list price or `modelPricing`; "The figure is an estimate" | interactive display only; per session, not by subagent |
| Status line JSON | https://code.claude.com/docs/en/statusline | `cost.total_cost_usd`, estimated, per session, resets on `/clear` | only the status line command receives it, and quoin does not own it; whether it includes subagent requests is unknown |
| OpenTelemetry | https://code.claude.com/docs/en/monitoring-usage | `claude_code.cost.usage` and `claude_code.token.usage` with `query_source` and `agent.name`; `api_request` event with estimated `cost_usd`; `agent_id` only on beta trace spans | needs a collector the user runs; `agent.name` is a type, not a unique id; exporter variables are stripped from subprocesses |
| Transcript `cost-state` row | https://code.claude.com/docs/en/agent-sdk/cost-tracking (existence and persist-on-exit only; the row schema is undocumented and may change without notice) | per-session `totalCostUSD` and per-model `costUSD` in the local main transcript, includes subagent requests, restored on resume | written on normal exit only; no per-subagent split; undocumented schema; any future use for finalize must skip or subtract the session's on-behalf rows to avoid double-counting |
| `subagentStatusLine` | https://code.claude.com/docs/en/statusline | running `tokenCount` per subagent task id | no cost; a display hook, not a readable record |
| `/usage` plan attribution | https://code.claude.com/docs/en/costs | approximate breakdown including a subagent share | interactive display, approximate |
| Hooks: Agent `tool_response`, `SubagentStop` | https://code.claude.com/docs/en/hooks | `totalTokens` and `usage` of the final request only; `SubagentStop` gives `agent_transcript_path` and no usage | not a whole-run figure; no cost |

## Consumers

Snapshot at commit 116d3d62 (the numbers change as other work lands; nothing tests them). Command, from the repo root: `grep -rlE "cost_from_jsonl|agent_transcript_cost|cost_ledger_summary" --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__ --exclude-dir=dist .` found 55 files. A second pass over the reporting and live-spend scripts, `grep -rlE "spend_monitor|dashboard_cost|analyze_cost_ledger|cost_summary"` with the same excludes, found 48 files, 22 of them already in the first set; the union is 81 files, one row each below.

Consumer classes: on-behalf (a per-subagent row written right after a managed spawn, needs a whole-run cost keyed by agent id), finalize (task total at end of task, needs past sessions' cost by id), reporting (many sessions aggregated), live-spend (today's spend across sessions), budget, benchmark, roster, test or history.

- Autonomous budget bound: no consumer. `quoin run --budget` is documented in `src/quoin/cli.py` as "NOT YET ENFORCED this release", and `src/quoin/supervisor.py` launches `claude -p` with `--output-format text`, so no cost figure is read there today.
- Benchmarks: the harness (`quoin/benchmarks/harness/cells/simple_claude.py`) already reads native `total_cost_usd` and compares it with the `pricing.json` estimate. That stays.
- Price lookup: four sites resolve prices (`cost_from_jsonl.cost_for_entry`, `cost_from_jsonl.parse_session`, `spend_monitor.parse_session_today`, `agent_transcript_cost.price_agent_jsonl`). Keeping all three scripts leaves all four unchanged, so there is no split between sources.

| Consumer | Class | Needs | Native candidate | Verdict |
|---|---|---|---|---|
| `CHANGELOG.md` | docs and history | - | - | no edit |
| `docs/architecture-diagram/data/graph.json` | docs and history | - | - | no edit |
| `docs/architecture-diagram/data/graph.json.js` | docs and history | - | - | no edit |
| `quoin/adapters/claude/skills/architect/SKILL.md` | on-behalf (and finalize fallback in run) | whole-run cost of one subagent by agent id | none reachable | keep |
| `quoin/adapters/claude/skills/cost_snapshot/SKILL.md` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/adapters/claude/skills/end_of_task/SKILL.md` | finalize | past sessions cost by id | cost-state row (partial, see spike) | keep |
| `quoin/adapters/claude/skills/review/SKILL.md` | on-behalf (and finalize fallback in run) | whole-run cost of one subagent by agent id | none reachable | keep |
| `quoin/adapters/claude/skills/run/SKILL.md` | on-behalf (and finalize fallback in run) | whole-run cost of one subagent by agent id | none reachable | keep |
| `quoin/adapters/claude/skills/thorough_plan/SKILL.md` | on-behalf (and finalize fallback in run) | whole-run cost of one subagent by agent id | none reachable | keep |
| `quoin/benchmarks/harness/pricing.json` | benchmark | cross-checked against the price table by a test; harness already reads native total_cost_usd | used already | no edit |
| `quoin/core/scripts/affected_tests.py` | test selection map | - | - | no edit |
| `quoin/core/scripts/cost_summary.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/core/scripts/dashboard_model.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/core/scripts/get_session_uuid.py` | inline copy of project-hash logic, not an import | - | - | no edit |
| `quoin/core/scripts/spend_monitor.py` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `quoin/core/workflow/cost-ledger.md` | docs and history | - | - | no edit |
| `quoin/dev/tests/fixtures/classify_critic_issues/training/stage-2-critic-response-1.md` | test or fixture | - | - | no edit |
| `quoin/dev/tests/fixtures/classify_critic_issues/training/stage-2-critic-response-2.md` | test or fixture | - | - | no edit |
| `quoin/dev/tests/fixtures/classify_critic_issues/training/stage-2-critic-response-3.md` | test or fixture | - | - | no edit |
| `quoin/dev/tests/fixtures/claude_md_citation_dispositions.json` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_affected_tests.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_agent_transcript_cost_warning.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_agent_transcript_cost.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_backfill_cost_attribution.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_benchmark_gate_plumbing.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_codex_cost_event.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_core_no_claude_terms.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_event.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_from_jsonl_wiring.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_from_jsonl.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_ledger_summary_wiring.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_ledger_summary.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_summary.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_dashboard_cost.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_get_session_uuid.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_handoff_measure.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_onbehalf_default_on.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_onbehalf_writer_predicate.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_price_resolver_four_sites.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_review_fanout_ledger.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_runtime_portability_docs.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_scope_install.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_spend_monitor.py` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `quoin/docs/runtime-parity-matrix.md` | docs and history | - | - | no edit |
| `quoin/docs/runtime-portability-status.md` | docs and history | - | - | no edit |
| `quoin/docs/runtime-portability.md` | docs and history | - | - | no edit |
| `quoin/memory/cost-ledger-format.md` | on-behalf (and finalize fallback in run) | whole-run cost of one subagent by agent id | none reachable | keep |
| `quoin/scripts/agent_transcript_cost.py` | the estimators themselves | - | - | keep |
| `quoin/scripts/analyze_cost_ledger.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/scripts/backfill_cost_attribution.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/scripts/cost_from_jsonl.py` | the estimators themselves | - | - | keep |
| `quoin/scripts/cost_ledger_summary.py` | the estimators themselves | - | - | keep |
| `quoin/scripts/dashboard_cost.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/scripts/handoff_measure.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `src/quoin/installer.py` | roster (DEPLOYED_SCRIPTS) | - | - | no edit |
| `quoin/core/scripts/validate_adapter_drift.py` | adapter drift check | - | - | no edit |
| `quoin/dev/tests/test_agentdesk_layout.py` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `quoin/dev/tests/test_agentdesk_standard_layout.py` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `quoin/dev/tests/test_agentdesk_zsh_args.py` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `quoin/dev/tests/test_analyze_cost_ledger.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cohort_attribution.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_cost_attribution_fallback_rate.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_dashboard_model.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_end_of_day_session_selection.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_end_of_task_decomposed.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_eot_resilience_contract.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_installer_dashboard.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_opencode_cost_readers.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_partial_propagation.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_run_inline_finish.py` | test or fixture | - | - | no edit |
| `quoin/dev/tests/test_spend_deploy.py` | test or fixture | - | - | no edit |
| `quoin/scripts/cost_summary.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/scripts/dashboard_server.py` | reporting | many sessions aggregated by day or task | none (interactive /usage only) | no edit |
| `quoin/scripts/spend_monitor.py` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `quoin/tools/agentdesk/agentdesk.zsh` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `quoin/tools/agentdesk/setup-agentdesk.sh` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `vscode-extension/src/costService.ts` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `vscode-extension/src/dataService.ts` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `vscode-extension/src/test/costSummaryParity.test.ts` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `vscode-extension/src/test/dataService.test.ts` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |
| `vscode-extension/src/test/fixtures/cost.ts` | live-spend (and its second-hop consumers) | today's spend across sessions | none | no edit |

## Spike results

Run on 2026-10-19 over every local transcript still on disk (232 main, 2399 subagent), Claude Code 2.1.303, read-only, counts and ratios only.

Part A (returned Agent usage vs whole-run subagent cost): 58 Agent results examined, all resolvable, none single-request. The returned figure equals the whole-run token sum in 0 cases and equals the final request's tokens in 53; the median ratio of returned to whole-run is 0.024. The documentation is right: the returned figure cannot stand in for `agent_transcript_cost.py`.

Part B (the `cost-state` row): 201 of 232 main transcripts (86.6%) have at least one; none of the subagent transcripts do. Of 731 session ids in this repo's cost ledgers, 78 still have a transcript and 63 of those carry a row (80.8%). Compared with a like-for-like re-pricing (main transcript plus summed subagent re-pricing), the row's total has a median ratio of 0.66 for ledger sessions, with none within 5% and 10 of 63 within 0.8 to 1.25; over the whole local corpus the median is 0.68 and 31 of 166 fall in that band. Sessions outside the band are labelled unexplained (candidates: totals reset by clear or compaction, only the last process's totals after a resume, pricing-table differences) and accuracy is not ranked from them. Coverage is under the 90% bar and agreement is far under it, so the finalize class stays on re-pricing.

Reproduce: run a read-only script that walks `~/.claude/projects/*/*.jsonl`, reads only usage and cost fields, resolves each subagent transcript by agent id, and prices with `cost_from_jsonl.parse_session` and `agent_transcript_cost.price_agent_jsonl`. Transcripts older than 30 days (`cleanupPeriodDays`) are already deleted, so the corpus is small and the numbers are indicative.

Verdict per class: on-behalf, reporting and live-spend have no native replacement; finalize is kept because coverage and agreement are low.

## Known differences from Claude Code's own figures

quoin prices at list price from its table (`LAST_UPDATED` in `cost_from_jsonl.py`). Claude Code applies its own managed `modelPricing` table and the 1.1x data-residency multiplier when they apply. Neither is authoritative billing; both say so.

## Revisit triggers

- The Agent tool result or a hook input gains a whole-run cost or token total per subagent.
- A documented local, file-based telemetry sink appears that a skill can read, or OTel `api_request` events gain a unique agent id outside beta traces.
- quoin's supervisor or `/run` moves phases to `claude -p` or the Agent SDK, where `total_cost_usd` per run is native.
- `--budget` gets implemented: then read `total_cost_usd` from `--output-format json` per relaunch instead of re-pricing transcripts, and consider native `--max-budget-usd`.
- Claude Code transcripts stop carrying per-message `usage`; that would break the estimators and force a change.
- The transcript `cost-state` record becomes documented (schema, behaviour on crash, subagent split): re-run the part B comparison like-for-like, handle the on-behalf double-count by skipping or subtracting the session's agent-id rows, and reconsider preferring it for finalize and reporting.

## Unknowns

- Whether the status line `cost.total_cost_usd` includes subagent requests (the docs say it matches the `/usage` Session total; inclusion is stated only indirectly).
- Whether the `cost-state` row schema is stable (undocumented).
- Whether beta trace spans carry cost per `agent_id`.
- Whether a console OpenTelemetry exporter writes anywhere a script can read in an interactive session.
- The accuracy gap between quoin's list-price table and Claude Code's bundled table for current models.

None of these changes the decision, because each would still need infrastructure the user runs or a status line quoin does not own.
