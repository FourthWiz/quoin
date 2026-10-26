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

`suite-tiered-balanced.json` is the larger suite: 40 tasks (10 easy, 15 medium,
15 large) from the same seed and sub-seeds, so it contains every tiered-small
instance. Select it with `--suite tiered-balanced`. Provenance, the task list
and the budget derivation are in `suite-tiered-balanced-provenance.md`.

The earlier tiered-small run showed that a quoin session can skip the pipeline
and fix the issue directly. The harness now starts the quoin arm with the slash
command `/run --autonomous small:` and a line saying the pipeline is required
(a pilot with the bare slash command still saw a session call an easy issue a
one-line fix and skip the pipeline), records on-disk evidence that the pipeline ran,
and treats a quoin session that left no plan as an `invalid` row (see "Invalid
rows" below). The test suite exercises the whole path offline with a fake
`claude`, so the first paid run with these changes is the first time the real
CLI, the real permission rules and the real judge meet in this form.

### Models, effort and aliases

Both arms run `claude-opus-5-5` at `--effort high`. The model table lives in
one place, `BENCH_MODELS` in `harness/cells/simple_claude.py` (opus, sonnet and
haiku, all 5.5), and both arms' sandboxes set `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` and
`ANTHROPIC_SMALL_FAST_MODEL` from it, so a skill's `model:` tier resolves to the
same 5.5 model in both arms. `CLAUDE_CODE_SUBAGENT_MODEL` is never set, because
it would override the skills' own tiers. Estimates use `harness/pricing.json`
(checked against the live pricing page on 2026-10-25); a Claude model with no
entry there is an error, not a guess, and a run that used one is reported as
`Valid: NO (unpriced model: ...)` and exits 6 after the report is written.
Haiku 5.5 is priced higher for prompts over 100,000 tokens; table estimates use
the base rates and are a lower bound, and the runtime-reported cost is the
authoritative figure. The manifest records the effort, the model table, any
`QUOIN_BENCH_CLAUDE_MODEL` override and the models each arm actually used, and
the report lists rows whose models differ from the pin.

`run_benchmark.py --verify-model` makes one capped call for the pin and one per
alias under the alias environment, and reports MATCH or MISMATCH for each. It
is user-initiated and spends real money; `quoin bench tiered` never runs it.

### One command: `quoin bench tiered`

From a source checkout of the quoin repo, one command runs the whole benchmark:

```bash
quoin bench tiered
```

It needs a source checkout because the benchmark scripts are not installed with
the package; `--repo-root` points it at a checkout explicitly. In order, it:

1. Prepares the repository mirrors, only when a task's pinned commit is missing
   from them (this is the only step that needs network access).
2. Prints a worst-case estimate with the absolute run directory, spend ledger
   and checkout it will use, then asks once. `--yes` answers for an unattended
   run; without a terminal and without `--yes` it refuses.
3. Runs the easy-tier pilot on both arms.
4. Compares permission denials between the arms over the pilot. It stops with
   exit 4 when the arms differ by at least `--parity-min-gap` denials (default
   3) and the larger total is at least `--parity-min-ratio` times the smaller
   (default 2; a smaller total of 0 always satisfies the ratio). It also stops
   when a pilot row has no denial count, naming the row and the reason. After
   reviewing the pilot rows, `--ignore-parity` continues past either stop.
5. Runs the remaining tiers on the same run id and the same ledger. Pilot
   sessions already recorded are not paid for again.
6. Judges the sessions when this host can: Docker running, the pinned
   `swebench` importable, and either an x86 host, an empty
   `QUOIN_BENCH_SWEBENCH_NAMESPACE` (build images locally) or
   `--judge-emulation` (on an arm64 host the evaluation runs through a shim
   that reports x86_64 to swebench and pulls images for `linux/amd64`; slow
   under Rosetta; no effect on other hosts). Otherwise it
   prints the exact `--judge-only` command to run on a host that can judge;
   `--skip-judge` defers judging on purpose.
7. Prints where the summary is, whether results are final or provisional, and
   the spend recorded for the run.

Exit codes: 0 finished (including judging deferred or nothing left to do); 1
interrupted, aborted, judging failed or the prompt declined; 2 refused before
spending anything; 3 a spend guard stopped the run; 4 parity stop; 5 the quoin
arm did not run the pipeline in a pilot row (the pilot stopped, or a re-run was
refused); 6 the run finished but a model in it has no pricing entry.

### Invalid rows and the pilot gate

A quoin session that ran but left no plan under `.workflow_artifacts/` did not
run the pipeline, so it is not evidence about the pipeline. The runner records
it as an `invalid` row with one of three reasons: `no-pipeline-artifacts`,
`stopped-before-plan` (the session timed out or hit its budget after starting
the pipeline but before a plan existed) or `no-pipeline-evidence` (the row has
no capture record). Invalid rows are never judged and never retried, and they
are left out of pass rates, cost, token and wall-clock figures, pairs and the
McNemar test; their spend stays visible in the cell's `invalid_total_cost_usd`.
Rows that ended in an error keep their own reason, so an auth failure still
stops the run. A timeout or budget stop after a plan exists is not invalid; the
report counts those separately. The rule applies only to Verified quoin rows.
The report lists the route (`fast`, `full` or `unknown`) the pipeline took for
each row, since `/run` strips a `fast:` token from anywhere in its input.

The easy-tier pilot stops after the first pair whose quoin row is invalid (exit
5), so a broken quoin arm costs one pair. Every later run refuses with exit 5
while a recorded pilot row is invalid, before preparing or spawning anything.
Inspect the row's transcript and `workflow_artifacts_evidence`, fix the cause,
and start a new run id, or continue anyway with `--accept-invalid-pilot`. The
override is not remembered: give it on every re-run. Invalid rows in the later
tiers are reported but never stop a run.

Defaults:

| Setting | Default |
|---|---|
| Suite and run id | `tiered-small` |
| Run directory | `<checkout>/.workflow_artifacts/quoin-benchmarks/runs` |
| Spend ledger | `<run-dir>/<run-id>.spend-ledger.jsonl` |
| Authorised ceiling | the suite's ceiling, $1,790 |
| Per-session cap | the suite's documented cap, $90 |
| Worst case of a fresh run | $1,620 (9 tasks x 2 arms x $90) |
| Pilot (easy tier) worst case | $720 (4 tasks x 2 arms x $90) |
| Wall clock per session | 19,200 s, every tier and both arms |

These figures are derived from the cost of comparable full-pipeline tasks, not
from a single direct fix; `suite-tiered-small-provenance.md` shows the
derivation. The cap is a bound sized not to bind, not a forecast. It is not
written into either arm's prompt; `--max-budget-usd` stays armed in both arms.
The suite changed, so a run started before this change needs a new `--run-id`.

Sequential worst-case durations at 19,200 s per session: about 96 h for a fresh
run (18 sessions) and about 43 h for the pilot (8 sessions); there is no idle
timeout. The ceiling leaves $170 above the worst case, less than two capped
sessions, so re-spawned rows may need a higher `--authorised-usd`. The 5.5 model
pin and `--effort high` apply to every arm, but the sonnet/haiku alias
environment is set only for sandboxed paired runs; non-paired v1 and three-arm
gate runs use the CLI's default aliases.


### Clean

`quoin bench tiered --clean` removes what a run left behind. It takes the same
`--run-id`, `--run-dir`, `--suite`, `--spend-ledger` and `--repo-root` options
as a run, lists every path with `delete`, `keep` or `skip` and the reason, asks
once (`--yes` answers for it; no terminal without `--yes` refuses), then
deletes. It never starts a process and never touches the network.

It deletes:

- the run directory `<run-dir>/<run-id>` (the `run-manifest.yaml`, the
  summaries and the arm cells);
- `<run-dir>/.superseded/<run-id>`;
- the default spend ledger; its recorded total is shown in the listing before
  the question, because the record is gone once the file is;
- `qbench_*`, `qbc_*` and `quoin-benchmarks` directories you own directly
  under the temp directory and under `/tmp`. These are shared by every run, so
  they are removed whichever run id you name; a temp directory set to the home
  directory or a path inside it is not scanned.

It keeps the repository mirror cache unless `--clean-mirrors` is given; with it,
only the `<owner>__<repo>.git` bare mirrors and their `.git.partial-<pid>`
leftovers inside the cache are deleted, and the cache directory itself only if
nothing else is left in it. Nothing is deleted from a cache directory that is
itself protected (the home directory or the checkout, say), and a `.git`
directory, a worktree's `.git` file or any other `*.git` name is never treated
as a mirror. A symlinked cache is listed as kept and its target left alone. It
also keeps an explicit `--spend-ledger` unless `--clean-explicit-ledger` is
given, the directories and ledgers of other run ids, and always the home
directory, `~/.claude`, `~/.config` and the checkout.

Safety rules: a path that contains a kept or protected path is skipped; paths
are compared by file identity, so another spelling of a protected directory
(for example a case variant on a case-insensitive file system) is still
protected; a ledger must be a file, never a directory; a run directory must
look like a benchmark run (it holds a `run-manifest.yaml` file or an arm cell
directory); symlinks are removed as links, marked `link` in the listing, and
their targets are left alone; traverse-only directories the harness leaves are
given back owner permissions before removal, without following links; every
check runs again just before each deletion. Do not run it while any benchmark
run is active, because it cannot tell. Temp roots of the three-arm gate harness
(`quoin-gate-raw-config-*`) are not touched. When it removes a
`quoin-benchmarks` worktree root it prints a reminder to run
`git worktree prune` in the arm checkout.

Exit codes: 0 deleted or nothing to delete; 1 declined or a deletion failed; 2
refused.

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

### Underlying commands

`quoin bench tiered` runs these for you. Every command passes `--cells simple-claude,quoin-claude` explicitly. Replace
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
   `WORST CASE: $1620.00` (cap $90 x 9 tasks x 2 arms); the documented target
   is $1,620 and the enforced ceiling $1,790.

   ```bash
   python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
     --cells simple-claude,quoin-claude --run-id RUN --dry-run \
     --max-budget-usd-per-task 90
   ```

3. Easy-tier pilot (worst case $720), ledgered:

   ```bash
   python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
     --cells simple-claude,quoin-claude --run-id RUN-pilot --tiers easy \
     --max-budget-usd-per-task 90 --spend-ledger ledger.jsonl --authorised-usd 720 --stop-on-invalid-quoin
   ```

   Compare `permission_denials_by_arm` in the pilot's `summary.json`. If one
   arm's denials clearly exceed the other's, stop and adjust the profile for
   both arms before the full run.

4. Full paired run:

   ```bash
   python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
     --cells simple-claude,quoin-claude --run-id RUN \
     --max-budget-usd-per-task 90 --spend-ledger ledger.jsonl --authorised-usd 1790
   ```

Add `--skip-judge` to run the sessions without Docker. The command exits 0 and
prints a `Judging deferred` line; every row stays judge-pending until

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-small \
  --cells simple-claude,quoin-claude --run-id RUN --judge-only
```

### Resume and interruption

Re-running the same `quoin bench tiered` command continues an interrupted
run: sessions already recorded are kept, the pilot is not repeated once the
full leg has started, and a resume may add tiers to a run but never drop one. The manifest's
`started_at` reflects the latest leg.

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
  leg starts again from zero (`quoin bench tiered` does not offer it). The suite ceiling and the authorised ceiling are
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
- There is no automatic sweep for capture scratch directories after a SIGKILL
  of the harness itself; `quoin bench tiered --clean` removes the ones under
  the temp directory.
- `--clean` does not detect a run in progress.
