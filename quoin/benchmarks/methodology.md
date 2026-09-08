# Benchmark Methodology: Quoin Effect on Coding-Agent Pass-Rate-per-Dollar

## Overview

This document defines the quantitative benchmarking procedure for measuring
whether the Quoin workflow toolkit improves a coding agent's pass-rate-per-dollar
on standardized programming tasks. It covers four comparison cells, two base
agents (Claude Code and Codex CLI), and a 120-task suite.

## Comparison Cells

The benchmark uses four cells, named to match the Phase 29 scaffold's
`REQUIRED_MODE_IDS` (see `scripts/validate_benchmarks.py`):

| Cell ID | Agent | Quoin? | Notes |
|---|---|---|---|
| `simple-claude` | Claude Code CLI | No | Baseline: Claude without Quoin workflow |
| `quoin-claude` | Claude Code CLI | Yes | Full Quoin slash-command workflow; auto-mode gates |
| `simple-codex` | Codex CLI | No | Baseline: Codex without Quoin guidance |
| `quoin-codex` | Codex CLI | Thin | Ambient `AGENTS.md` + portable workflow contracts; no slash-command emulation |

### What "Quoin" Means in This Benchmark (Decision D-01: Auto-Mode)

For the `quoin-claude` cell, "Quoin" means:

1. The agent is invoked with `QUOIN_GATE_AUTO_APPROVE=1` AND `QUOIN_BENCHMARK_RUN=1`
   environment variables set; `/gate` honors this pair to auto-emit PASS (see
   [Auto-mode caveats](#auto-mode-caveats) and T-19 gate plumbing).
2. The agent receives a one-line directive prepending the task: "Use /run
   end-to-end on this task".
3. A clean per-task `.workflow_artifacts/` is initialized from scratch before
   each task.

For the `quoin-codex` cell, "Quoin" means ambient AGENTS.md content plus
bootstrapped `.workflow_artifacts/` layout per the Codex adapter docs
(`quoin/adapters/codex/workflow.md`). No slash-command emulation or §0 dispatch.

**Caveat:** Auto-mode measures the ceiling of quoin's effect — a human who
always approves gates. This overstates real-world benefit relative to a careful
reviewer. The gate-intervention count secondary metric (via `auto_approved: true`
audit fields) lets readers estimate how often a human would have intervened.

For the `quoin-codex` cell specifically, the pre-registered expectation is a
delta of ≤5 percentage points on pass@1 from this thin intervention, which is
below the minimum detectable effect at N=20 SWE-bench Lite tasks. This cell
exists to verify the harness works end-to-end on Codex; it is exploratory-only
in v1 and does not produce a publishable quoin-effect-on-Codex claim.

## Fair-Comparison Invariants

The following 14 invariants are enforced programmatically by
`scripts/check_invariants.py` and must all PASS before results are published:

1. **Same model ID (within base-agent pair):** `simple-claude` and `quoin-claude`
   use identical dated Claude snapshot IDs; `simple-codex` and `quoin-codex` use
   identical dated Codex model IDs.
2. **Same temperature:** 0 for HumanEval+ tasks; 0.2 for SWE-bench Lite tasks
   (following upstream convention).
3. **Same suite:** `suite-v1.json` frozen by SHA; both cells run identical tasks.
4. **Same fixture repo SHAs:** each cell run starts from an identical pinned commit.
5. **Same per-task wall-clock budget:** 600 seconds per task; timeout marks task
   as `timeout`.
6. **Same per-task USD kill-switch:** applied per cell-pair budget (not per
   individual task) to avoid systematically killing only quoin cells.
7. **Zero retries on task failure:** a task that fails is recorded as `fail`,
   not retried.
8. **Network policy:** offline-after-clone for fixture repo.
9. **Docker judge image hash pinned:** same evalplus/swebench Docker image for
   both cells.
10. **Library versions pinned:** `evalplus` and `swebench` versions in
    `requirements-bench.txt`; pinned per run.
11. **Transcripts captured:** full JSONL transcript written for every task.
12. **Cost data captured or explicitly marked `not_available`:** no silent missing
    data.
13. **Per-task isolated `.workflow_artifacts/`:** fresh empty directory per task
    (not per cell); never shared across tasks within a cell.
14. **Zero lessons-learned cross-contamination:** no carried-over `lessons-learned.md`
    or `memory/sessions/` data from prior tasks within the same cell.

## Headline Metric

The **headline metric** is `pass@1 / mean USD per task` for the within-Claude
pair only.

This metric is **UNDEFINED for Codex cells in v1** because
`quoin/adapters/codex/cost.md` forbids token inference ("Do not infer token
counts from chat length, transcript size, model name, elapsed time, or file
changes") and no live Codex telemetry is available. Codex cells report raw
pass@1 and turn-count only.

**Secondary metrics** (all cells):

- Raw pass@1 per cell
- Total wall-clock time to suite completion
- p50 and p95 per-task wall-clock latency
- Gate-intervention count (quoin cells, via `auto_approved: true` audit fields)
- Edit-locality (fraction of changes in a single file vs. spread across files)
- Tokens in / out / cache (Claude cells only, from JSONL session logs)

## Minimum Detectable Effect Table

The following table is pre-registered per D-07. All figures are approximate.

| Benchmark | N per cell | Wilson CI half-width (p=0.3) | Wilson CI half-width (p=0.5) | Wilson CI half-width (p=0.7) | McNemar min. detectable paired delta (80% power, discordance p_d≈0.20) |
|---|---|---|---|---|---|
| HumanEval+ | 100 | ±9 pp | ±10 pp | ±9 pp | ≈12 pp |
| SWE-bench Lite | 20 | ±20 pp | ±22 pp | ±20 pp | ≈28 pp |

**Key conclusions from this table:**

- A 5-point pass@1 gain on SWE-bench Lite is **not detectable** at N=20. Treat
  the SWE-bench Lite slice as qualitative only.
- The v1 primary conclusion is rank-order: "does the quoin cell consistently
  solve tasks the simple cell misses, and vice versa?" not a population
  pass-rate-per-dollar point estimate.
- Absolute scores will not generalize (scaffold-driven distribution shift per
  Epoch AI reference [6]); only rank-order claims are framed as primary.

Note: the discordance rate p_d≈0.20 is an assumption, not an empirically
measured value. The T-15 calibration run will provide empirical variance data.

## Cost Derivation

Cost estimates below assume the `claude-opus-4-7` model ID. Starting with the
4.6 generation, this dateless ID is itself the permanent pinned snapshot — no
separate dated form exists to swap in later. Actual costs are pinned from
T-15 calibration; these are planning estimates.

| Slice | Per-task estimate | N tasks | N cells | Total estimate |
|---|---|---|---|---|
| HumanEval+ (Claude pair) | ~$0.50–$2.00/task (simple); ~$1.00–$5.00/task (quoin) | 100 | 2 | ~$150–$700 |
| SWE-bench Lite (Claude pair) | ~$5–$15/task (Aider leaderboard reference [4]) | 20 | 2 | ~$200–$600 |
| Codex pair | cost: reconcile offline | — | 2 | not_available |
| **Grand total (Claude pair)** | — | 120 | 2 | **~$250–$700** |

If the total estimated Claude-pair budget exceeds $300, the plan recommends
shrinking the SWE-bench Lite subset to 10 tasks.

Per-task kill-switch is set at $10/task applied per cell-pair (not per
individual task) to avoid systematically killing only quoin cells.

Sources: Aider polyglot leaderboard [4] for SWE-bench cost baseline; T-15
calibration for observed per-task cost before T-17 full run.

## Naming: Relationship to Phase 29 Scaffold

The cell names `simple-claude`, `quoin-claude`, `simple-codex`, and `quoin-codex`
match the `REQUIRED_MODE_IDS` in the Phase 29 scaffold (`scripts/validate_benchmarks.py`
lines 24–29).

The Phase 29 qualitative suite (5 scenarios, subjective 0-4 scores on workflow
usefulness dimensions) remains independent and complementary. The new quantitative
procedure adds external-benchmark pass-rate measurement alongside the existing
qualitative rubric. Both are stored under `quoin/benchmarks/`; they serve
distinct purposes and do not replace each other.

## Relationship to Phase 29 Workflow-Usefulness Suite

The Phase 29 suite evaluates workflow usefulness through a small set of
subjective scenarios: discovery quality, planning quality, implementation scope,
review depth, and session-handoff continuity. Scores are 0-4 on each dimension,
filled in by a human reviewer from a run transcript.

The quantitative procedure in this document evaluates a different thing: whether
the quoin workflow toolkit improves a coding agent's ability to correctly solve
programming tasks from external benchmarks (EvalPlus HumanEval+ and SWE-bench
Lite), as measured by pass@1 per unit cost.

The two suites answer different questions:
- Phase 29: "Is the quoin workflow process being followed well, and is it useful
  in a holistic sense?"
- This procedure: "Does following the quoin workflow produce more correct code,
  on standardized tasks, at lower cost per correct solution?"

Results from one suite do not substitute for results from the other.

## Auto-Mode Caveats

The `quoin-claude` cell runs with `QUOIN_GATE_AUTO_APPROVE=1` AND
`QUOIN_BENCHMARK_RUN=1` both set. Both environment variables must be set; if
only one is set, `/gate` behaves normally (blocking). This dual-guard prevents
accidental auto-approval in production user sessions.

The auto-approve plumbing is implemented in `quoin/adapters/claude/skills/gate/SKILL.md`
(see T-19). Each gate event in auto-approve mode writes a gate audit file
containing `auto_approved: true`, the environment variable pair used, the gate
encounter timestamp, and the `QUOIN_BENCHMARK_RUN` run ID.

Who sets `QUOIN_BENCHMARK_RUN`: the `run_benchmark.py` orchestrator sets this
variable to the `--run-id` value for the duration of each cell run. Cell adapters
inherit it. Quoin-cell agent invocations inherit it from the subprocess environment.

## Patch Normalization for SWE-bench Lite

Before computing SWE-bench Lite verdicts, patches are normalized using the
following preprocessing steps (following the SWE-bench Verified harness
patch-application logic [3]):

1. Strip trailing whitespace from every line.
2. Normalize line endings: CRLF → LF.
3. Ensure exactly one trailing newline at end of file.

This normalization is applied in the judge step (`harness/judge.py`) before
passing the patch to the SWE-bench evaluation harness. The goal is to prevent
formatting-only differences from causing spurious `fail` verdicts.

## Statistical Methodology

**Primary test (within-Claude-pair):** McNemar test on per-task paired
pass/fail outcomes. Since both `simple-claude` and `quoin-claude` run the same
tasks, the paired test is more powerful than comparing CI-overlap.

**Reporting:** Wilson 95% confidence intervals on pass@1 per cell using
`statsmodels.stats.proportion.proportion_confint(count, nobs, alpha=0.05,
method='wilson')`. Wilson CIs are preferred over normal-approximation CIs at
sample sizes below ~200.

**Effect size labeling:** Any pairwise delta whose CIs overlap is labeled "not
significant at this sample size." Sub-MDE effects (below the McNemar threshold)
are labeled "below minimum detectable effect."

## External References

1. EvalPlus / HumanEval+ / MBPP+ — https://github.com/evalplus/evalplus and
   https://evalplus.github.io/leaderboard.html
2. SWE-bench main repo and Lite split — https://github.com/SWE-bench/SWE-bench
   and https://www.swebench.com/lite.html
3. SWE-bench Verified (OpenAI human-validated) —
   https://openai.com/index/introducing-swe-bench-verified/ (containerized Docker
   harness; patch-application logic reference for patch normalization)
4. Aider polyglot leaderboard (cost-per-task methodology) —
   https://aider.chat/docs/leaderboards/ (per-task USD cost source for cost
   derivation)
5. LiveCodeBench (contamination-free, date-filtered) —
   https://livecodebench.github.io/ and https://arxiv.org/abs/2403.07974
   (deferred to v2)
6. Epoch AI scaffold-shift data —
   https://epoch.ai/benchmarks/swe-bench-verified (rank-order stable; informs
   primary-conclusion framing)
7. ReliableEval (stochastic LLM eval via Method of Moments) —
   https://arxiv.org/html/2505.22169 (variance characterization; informs
   calibration design)
8. Pass@k sample-size guidance —
   https://leehanchung.github.io/blogs/2025/09/08/pass-at-k/ and Cameron Wolfe's
   Applying Statistics to LLM Evaluations —
   https://cameronrwolfe.substack.com/p/stats-llm-evals
9. Anthropic SWE-bench harness — https://www.swebench.com/ (minimal bash-tool
   agent pattern; relevant for cell adapter design)
10. Claude Code vs Codex CLI 2026 comparison —
    https://www.morphllm.com/comparisons/codex-vs-claude-code (informs cross-agent
    comparison caveats)

## Verify-Fix Loop Evidence (IVG-126)

The automated post-task verify-fix loop added to `/implement` (see
`### Automated verify-fix loop (post-task)` in
`quoin/adapters/claude/skills/implement/SKILL.md`, and the portable contract
in `quoin/core/skills/implement.md`) is expected to reduce how often a task
reaches the post-implement `/gate` in a red state, since fixable failures are
caught and repaired before the task is marked complete rather than surfacing
only at the gate checkpoint.

Reading this effect is an empirical benchmark-run comparison, not a
code-level assertion: it requires running the `verify-fix-loop` scenario
(see `scenarios/verify-fix-loop.md`) across a set of tasks with the loop
enabled versus disabled (`QUOIN_VERIFY_RETRIES=0` disables it) and comparing
the post-implement gate-failure rate between the two configurations. No such
comparison run has been conducted yet; this section documents the intended
evaluation procedure for a future calibration run, following the same
pre-registration discipline as the pass-rate-per-dollar methodology above.
