# Cross-Runtime Benchmarks

Phase 29 defines a small benchmark framework for comparing Quoin-assisted
workflows with simple Claude and Codex workflows. The suite evaluates workflow
usefulness: quality of decisions, correctness, reusable artifacts, continuity,
and overhead.

This directory contains benchmark design only. It does not contain measured results,
rankings, or claims that Quoin improves outcomes. A result exists only after a
human or automated run fills out `templates/result-template.md` with run
evidence.

## Comparison Modes

Run each scenario in the same fixture repository under four modes:

- `simple-claude` - Claude Code used normally, without Quoin phases or Quoin
  workflow artifacts.
- `quoin-claude` - Claude Code with the supported Claude adapter and Quoin
  artifacts under `.workflow_artifacts/`.
- `simple-codex` - Codex used normally with native planning, approvals,
  sandboxing, and repo-scoped instructions, without Quoin artifacts.
- `quoin-codex` - Codex with repo-local Quoin guidance and portable artifacts,
  without Codex global install assumptions or generated command files.

Simple modes may still keep ordinary notes or final answers, but they should
not be prompted to create Quoin-specific artifacts. Quoin modes should use the
same portable artifact contract documented in `quoin/core/workflow/`.

## Scenario Suite

The suite is intentionally small:

- Fresh repo discovery: orientation and useful first-pass repository map.
- Planning a medium refactor: risk discovery, staging, and validation strategy.
- Implementing a scoped code change: bounded implementation plus relevant checks.
- Reviewing changes: concrete findings, test gaps, and plan mismatch detection.
- Session handoff / memory reuse: continuation quality after prior-session context.

The machine-readable source of truth is `benchmark-suite.json`. Human scenario
instructions live under `scenarios/`.

## Metrics

The required scoring dimensions are:

- Task completion quality.
- Correctness / tests.
- Artifact quality.
- Context reuse.
- Time / turn count.
- Setup overhead.

Cost is optional because runtime cost capture is not equally implemented across
Claude and Codex. When cost is unavailable, record `not available` instead of
estimating it.

## Running A Benchmark

1. Choose a fixture repository and reset it to the same starting revision for
   every mode.
2. Select one scenario from `benchmark-suite.json`.
3. Copy `templates/run-sheet.md` into the run evidence folder and fill in the
   runtime, model or effort setting, start time, and setup steps.
4. Execute the scenario prompt exactly once per mode unless the run sheet
   records a restart.
5. Record raw evidence: final response, transcript or turn count, changed files,
   tests/checks, relevant Quoin artifacts for Quoin modes, and cost if the
   runtime exposes it.
6. Fill out `templates/result-template.md` after the run. Keep observations and
   scores separate from the design files in this directory.
7. Compare modes only after all four mode results for the same scenario and
   fixture are complete.

## Validation

Run the deterministic structure check from the repository root:

```bash
python3 quoin/benchmarks/scripts/validate_benchmarks.py --project-root .
```

This check validates the manifest, scenario files, templates, comparison modes,
and metric coverage. It does not execute benchmark tasks or infer results.

## Quantitative Benchmark Harness (v1)

The `harness/` directory and `scripts/run_benchmark.py` implement an automated,
quantitative benchmark for measuring quoin's effect on coding-agent pass-rate-per-dollar
against external benchmarks (EvalPlus HumanEval+ and SWE-bench Lite).

See `methodology.md` for the full benchmark design.

### Running a v1 Benchmark

**Prerequisites:**
- Claude Code CLI installed and authenticated (`claude --version`)
- Codex CLI installed if running Codex cells (`codex --version`)
- evalplus and swebench installed (`pip install evalplus swebench`)
- Docker available for judge evaluation (SWE-bench Lite)
- statsmodels installed for CI computation (`pip install statsmodels`)

**Step 1: Dry-run to estimate costs (required before any real run)**

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py \
    --suite quoin/benchmarks/suite-v1.json \
    --cells simple-claude,quoin-claude \
    --run-id v1 \
    --dry-run
```

The dry-run prints the planned task × cell matrix and cost estimates without
invoking any agent.

**Step 2: Smoke run (3 tasks, simple-claude only)**

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py \
    --suite quoin/benchmarks/suite-v1.json \
    --cells simple-claude \
    --run-id v0-smoke \
    --max-parallel 1
```

Requires explicit user budget approval (T-14) before proceeding to larger runs.

**Step 3: Calibration run (T-15)**

After budget approval from T-14:
```bash
python3 quoin/benchmarks/scripts/run_benchmark.py \
    --suite quoin/benchmarks/suite-v1.json \
    --cells simple-claude,quoin-claude \
    --run-id v05-calibration \
    --max-parallel 2
```

**Step 4: Full v1 run (T-17, requires T-16 budget gate approval)**

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py \
    --suite quoin/benchmarks/suite-v1.json \
    --cells simple-claude,quoin-claude,simple-codex,quoin-codex \
    --run-id v1 \
    --max-parallel 4 \
    --resume
```

Use `--resume` to continue an interrupted run without re-running completed tasks.

**Step 5: Check invariants and view results**

```bash
python3 quoin/benchmarks/scripts/check_invariants.py --run-id v1
cat .workflow_artifacts/quoin-benchmarks/runs/v1/summary.md
```

### Output Files

Each completed run produces:
```
.workflow_artifacts/quoin-benchmarks/runs/<run-id>/
    run-manifest.yaml              — pinned config + git SHA
    summary.json                   — machine-readable aggregated metrics
    summary.md                     — human-readable Markdown report
    invariants-report.md           — fair-comparison invariant check results
    <cell>/<task-id>/
        prompt.txt                 — prompt sent to agent
        transcript.jsonl           — full agent session transcript
        diff.patch                 — git diff of worktree after agent run
        judge.json                 — verdict (pass/fail/error/timeout)
        metrics.json               — timing and token metrics
        cost.json                  — cost data (or not_available for Codex)
```

### Benchmark Auto-Approve (QUOIN_GATE_AUTO_APPROVE)

For unattended benchmark runs, the `quoin-claude` cell sets both:
- `QUOIN_GATE_AUTO_APPROVE=1`
- `QUOIN_BENCHMARK_RUN=<run-id>`

When BOTH are set, `/gate` auto-approves without human input and records
`auto_approved: true` in the gate audit log. This requires the gate plumbing
implemented in T-19. See `methodology.md` for the auto-mode caveats.

## Tiered paired benchmark (SWE-bench Verified)

`suite-tiered-small.json` holds nine SWE-bench Verified tasks in three
difficulty tiers (4 easy, 3 medium, 2 large). The paired run gives every task
to two arms, one after the other: `simple-claude` (Claude Code with no quoin)
and `quoin-claude` (the same Claude Code with quoin installed). It measures
pass rate, cost, tokens and wall time per tier, with the quoin-minus-plain
difference as the headline figure. Nine tasks is a directional signal, not a
statistically significant result. Provenance and the task list are in
`suite-tiered-small-provenance.md`.

Nothing here has been run against a real model. The test suite exercises the
whole path offline with a fake `claude`, so the first paid run is the first
time the real CLI, the real permission rules and the real judge meet.

### Prerequisites

- Claude Code CLI on `PATH`, with exactly one of `CLAUDE_CODE_OAUTH_TOKEN` or
  `ANTHROPIC_API_KEY` set. Both set is refused.
- git 2.42 or newer. Patch capture reads `.gitattributes` from the base commit
  through `GIT_ATTR_SOURCE`, which older versions ignore.
- For judging: Docker running, and `pip install -r
  quoin/benchmarks/requirements-bench.txt` into the interpreter named by
  `QUOIN_BENCH_SWEBENCH_PYTHON` (default: the current one). The Verified judge
  accepts only `swebench==3.0.17`.
- The SWE-bench images are x86. On Apple Silicon use emulation, set
  `QUOIN_BENCH_SWEBENCH_NAMESPACE=""` to build the images locally, or run the
  sessions here with `--skip-judge` and judge later with `--judge-only` on an
  x86 host.
- Network access for the one-time repository download in step 1.

Before the first paid run, re-check the permission-rule spelling used for the
sandbox profile (`Bash(curl:*)` and the other deny entries in
`harness/arm_sandbox.py`) against the current Claude Code settings
documentation. A rule that no longer matches would silently allow what it was
meant to deny, in both arms.

### Steps

Every command passes `--cells simple-claude,quoin-claude` explicitly. Replace
`RUN` with a new run id.

1. Prepare the repository mirrors. This is the only step that needs network
   access and it calls no model. It also trial-prepares each task and fails if
   a base tree tracks a path under a directory that patch capture skips
   (`node_modules`, `.venv`, `__pycache__` and similar).

   ```bash
   python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
     --cells simple-claude,quoin-claude --prepare-repos
   ```

2. Dry run with the cost cap. It prints the task-by-arm matrix and
   `WORST CASE: $99.00` (cap $5.50 x 9 tasks x 2 arms); the documented target
   is $100 and the enforced ceiling $110.

   ```bash
   python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
     --cells simple-claude,quoin-claude --run-id RUN --dry-run \
     --max-budget-usd-per-task 5.5
   ```

3. Easy-tier pilot (worst case $44), ledgered:

   ```bash
   python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
     --cells simple-claude,quoin-claude --run-id RUN-pilot --tiers easy \
     --max-budget-usd-per-task 5.5 --spend-ledger ledger.jsonl --authorised-usd 50
   ```

   Compare `permission_denials_by_arm` in the pilot's `summary.json`. If one
   arm's denials clearly exceed the other's, stop and adjust the profile for
   both arms before the full run.

4. Full paired run:

   ```bash
   python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
     --cells simple-claude,quoin-claude --run-id RUN \
     --max-budget-usd-per-task 5.5 --spend-ledger ledger.jsonl --authorised-usd 110
   ```

Add `--skip-judge` to run the sessions without Docker. The command exits 0 and
prints a `Judging deferred` line; every row stays judge-pending until

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
  --cells simple-claude,quoin-claude --run-id RUN --judge-only
```

### Resume and interruption

`--resume` never re-pays for a recorded session, including one stopped by the
budget (`budget_stopped`). A row whose spawn was interrupted or that hit an
identity check is re-spawned once and the earlier attempt moves under
`.superseded/`. A row that hit a harness exception is re-spawned once; a second
one for the same task is kept as an `error` row, logged with both directories,
and the remaining tasks still run. Ctrl-C, SIGTERM and SIGHUP stop the run
cleanly: the running session is killed with its process group, the spend
ledger is settled, the sandboxes are removed, and the manifest records the
signal for a later `--resume`.

### What each arm sees

Each arm runs in its own throwaway sandbox with an empty `HOME`, its own
`CLAUDE_CONFIG_DIR`, and an allowlisted environment (`arm_config_mode:
symmetric` in the manifest). Both arms get the same base permission profile:
Read, Edit, Write, Glob, Grep and Bash allowed; `WebFetch`, `WebSearch` and the
Bash prefixes `curl`, `wget`, `pip`, `pip3` and `claude` denied. The quoin arm
additionally receives whatever quoin's own installer writes (skills, rules,
hooks and its `rm -rf` deny rules); those additions are part of what is being
measured. Identity is checked statically before the run and again before every
spawn: the plain arm must hold no quoin skill, the quoin arm must hold the
installed set.

### Reading the summary

`summary.md` and `summary.json["tiered"]` contain:

- per-tier and overall tables for each arm, with the delta row computed as
  quoin minus plain; denominators come from the suite, so a task that did not
  run counts as a failure for that arm;
- a complete-pairs block (only tasks where both arms ran) whenever any pair is
  missing an arm;
- per-task pairs, including whether both arms started from the same tree;
- an overshoot line per arm (the per-task cap, the total spent above it and
  the rows over it) and the permission denials per arm;
- the validity block: it says whether the run is valid, whether results are
  final or provisional (rows awaiting judging count as errors until judged),
  the suite and manifest hashes, the models seen, and every flagged row.

With nine tasks, treat differences as directional.

### Limits

- Agents cannot run each repository's own test environment; the judge does.
- Task descriptions are untrusted upstream text, and the agents run with
  `acceptEdits` and broad Bash on the host.
- Only the web tools and the named Bash prefixes are denied, in both arms. The
  network stays reachable through other commands (`python3 -m pip`,
  `git clone`, a Python one-liner) in both arms.
- Broad Bash can read the operator's real configuration by absolute path, for
  example the real `~/.claude` tree, and no identity check sees that. The access
  is symmetric, but only the plain arm could gain quoin text from it.
- Both arms run as the same user, so a session that knows the absolute path of
  the other arm's sandbox can read it. The run's parent directory is
  traverse-only (mode 0100) so it cannot be listed, but the path is not secret
  from a determined agent.
- The auth value is in each session's environment, so an agent that runs `env`
  can write it into `transcript.jsonl` or a patch. Redact the exact value and
  any proxy credentials before publishing or attaching run evidence.
- Settings, MCP servers, output styles and home dotfiles that a session writes
  persist into the next session of the same arm. Only skills, commands, agents,
  hooks, the rules file and the identity-bearing settings keys are fingerprinted.
- `--usd-cap` counts spend for the current invocation only, so each `--resume`
  leg starts again from zero. The suite ceiling and the authorised ceiling are
  read from the spend ledger and hold across legs.
- Quoin's installer adds its `rm -rf` deny rules to the quoin arm only.
- New files under a git-ignored path are left out of the patch, as in
  SWE-bench's own diff. Files over 100 MiB and dependency or cache directories
  (`node_modules`, `.venv`, `__pycache__` and similar) are skipped and counted
  in the row.
- A nested `claude` started through `python3` escapes the per-task cap even
  though `Bash(claude:*)` is denied.
- The 4 GiB total snapshot cap is kept on purpose; the per-file cap and the
  directory skip bound it in practice.
- There is no sweep for capture scratch directories after a SIGKILL of the
  harness itself.
