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

Run each scenario in the same fixture repository under six modes:

- `simple-claude` - Claude Code used normally, without Quoin phases or Quoin
  workflow artifacts.
- `quoin-claude` - Claude Code with the supported Claude adapter and Quoin
  artifacts under `.workflow_artifacts/`.
- `simple-codex` - Codex used normally with native planning, approvals,
  sandboxing, and repo-scoped instructions, without Quoin artifacts.
- `quoin-codex` - Codex with repo-local Quoin guidance and portable artifacts,
  without Codex global install assumptions or generated command files.
- `simple-opencode` - the pinned OpenCode binary on a chosen model and endpoint,
  without the Quoin scaffold.
- `quoin-opencode` - the same binary, model, endpoint and native configuration,
  with the Quoin OpenCode scaffold installed into the work tree.

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
- Tool-call reliability: a harmless native tool loop, repeated, to separate
  protocol problems from task quality.
- Cross-service context synthesis: a grounded plan from read-only issue, chat
  and mail fixtures, with source provenance. Mutation-approval scenarios run
  only in test tenants and are not part of this suite.

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

## OpenCode cells and pilot qualification

`simple-opencode` and `quoin-opencode` are a paired comparison: both read one
model, one endpoint and one native configuration file from the environment, so
the only difference is the Quoin scaffold. Comparing a model with Quoin against a
different model without it would confound the result, and the report refuses it.

```bash
export QUOIN_BENCH_OPENCODE_MODEL=<provider/model>      # required
export QUOIN_BENCH_OPENCODE_BASE_URL=<endpoint>         # required
export QUOIN_BENCH_OPENCODE_CONFIG=<opencode.json>      # required, used by both cells
export QUOIN_BENCH_OPENCODE_ROUTE=<route label>         # optional provenance
export QUOIN_BENCH_OPENCODE_CONTEXT_LIMIT=<tokens>      # optional provenance
export QUOIN_BENCH_OPENCODE_OUTPUT_LIMIT=<tokens>       # optional provenance
export QUOIN_BENCH_OPENCODE_PERMISSIONS=<label>         # optional provenance
export QUOIN_BENCH_TASK_SEED=<seed>                     # optional provenance
python3 quoin/benchmarks/scripts/run_benchmark.py --cells simple-opencode,quoin-opencode ...
```

Each run records runtime and version, adapter, provider route, endpoint, model,
configuration digest, context and output limits, repository commit,
permissions and task seed in `extra.provenance`, next to turns, tool errors and
setup and wall time. Token counts and cost come from the runtime's own usage
events and stay unknown when it does not report them. The endpoint is stored
without credentials, query or fragment.

A cell refuses to start (verdict `error`, with a `failure_reason`) without a
model, endpoint and configuration file, or when the provider `baseURL` in the
configuration file differs from `QUOIN_BENCH_OPENCODE_BASE_URL`. Both cells run
with a fresh HOME and XDG directories, `OPENCODE_DISABLE_CLAUDE_CODE=1` and
`OPENCODE_DISABLE_EXTERNAL_SKILLS=1`, and without `OPENCODE_CONFIG_CONTENT`,
`OPENCODE_CONFIG_DIR` or `OPENCODE_PERMISSION`, so the user's global OpenCode
and Claude files cannot reach either cell; provider credentials passed as
environment variables are kept. SWE-bench Verified tasks are not supported by
these cells yet, and `--max-budget-usd` does not cap them: bound OpenCode runs
with the wall clock and the provider's own limits.

The pilot thresholds are frozen in `opencode-qualification.json`: at least 19 of
20 harmless gateway tool-loop runs, at least 8 of 10 scoped workflow tasks
completed correctly, and every deterministic safety and artifact invariant
passing. Changing the task set or a threshold needs maintainer approval.
`scripts/opencode_report.py` reads a finished run directory, or one JSON Lines
row per run (failed runs included), and prints each threshold as pass, fail or
pending with the actual counts and failure modes, followed by task quality and
overhead as separate tables. Each count threshold judges the first runs up to
its sample size, in order; later runs are reported as surplus. The tool loop is
judged separately for each cell. These counts are qualification evidence for one model and endpoint,
not a general reliability guarantee.

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

`suite-verified-vague.json` and `suite-verified-vague-ceiling.json` hold 20 of
those tasks with vague and original prompts, to test how `/run` handles vague
requests; see "Vague-prompt suite" below.

The earlier tiered-small run showed that a quoin session can skip the pipeline
and fix the issue directly. The harness now starts the quoin arm with the slash
command `/run --autonomous` and a line saying the task must go through `/run`
(a pilot with the bare slash command still saw a session call an easy issue a
one-line fix and skip the skill). The line defers to `/run`'s own routing: its
scope triage sends most tasks down the plain r0 route (implement, verify,
commit, with no context pack or review subagent), adds a review for r1 and a
stage plan for r2, and that routing is part of what the arm measures. Every
row writes a `brief.md` whose `route: r0` to `r3` line the reports group by,
so it is a valid pipeline row. `QUOIN_BENCH_QUOIN_ROUTE=hard`
restores the earlier `/run --autonomous hard: small:` prompt, which runs the
heavy pipeline's single-pass path; `small:` has no effect in light `/run`.
`QUOIN_BENCH_QUOIN_ROUTE=lean` keeps the light prompt and sets
`QUOIN_RUN_PROFILE=bench-lean` in the quoin arm's session environment only
(the plain arm never sees it). That benchmark-only profile makes `/run` skip its
Step 2 `/discover` refresh (the context pack explores the repository itself) and
the lessons-learned append before the commit; the brief, context pack,
implementation, tests, review and the post-review fixes all still run, so lean
rows pass the same pipeline check through `brief.md`. The lean session also gets
`QUOIN_DISCOVERY_REFRESH_DISABLE=1`, so the session-start hook does not tell it
to run `/discover`. `QUOIN_BENCH_QUOIN_ROUTE=memory` sets
`QUOIN_RUN_PROFILE=bench-memory` instead, which skips only run isolation and
keeps discovery and the lessons read and append (see "Sequence suite" below).
`QUOIN_BENCH_QUOIN_ROUTE=r0` forces `/run`'s plain r0 route with a one-line
`/run --autonomous r0:` prompt and the same `bench-lean` profile and
discovery-banner silencing as lean, so every quoin row is a minimal
implement, verify and commit session; `overhead-audit.md` describes how to
check its cost against plain.
`QUOIN_BENCH_QUOIN_CLASS=r1` or `r2`, with the default, lean or memory route,
replaces the light prompt with `/run --autonomous r1:` or `r2:`, forcing that
scope class instead of letting triage pick (usually r0, which skips discovery
and the context pack). r2 makes both run; under lean, discovery is still
skipped, so memory and lean differ only in the memory they carry.
A paired run refuses any other route value, or a class with the hard or r0
route, before it starts. The
harness records on-disk evidence that the pipeline ran,
and treats a quoin session that left no plan as an `invalid` row (see "Invalid
rows" below). The test suite exercises the whole path offline with a fake
`claude`, so the first paid run with these changes is the first time the real
CLI, the real permission rules and the real judge meet in this form.

### Secondary measures: patch size and blind pairwise review

The primary metric stays SWE-bench Verified `resolved`. Two secondary measures
sit beside it and never change it.

Patch size. Each row's `diff.patch` is measured for added and removed lines
and touched files, split into test and non-test files, and shown per arm, per
tier and per task in the tiered report and `summary.json`. A file counts as a
test file when a directory in its path is named `tests`, `test` or `testing`,
or its name starts with `test_`, ends with `_test.py`, equals or ends with
`tests.py`, or is `conftest.py`. Empty and missing patches are counted
separately.

Blind pairwise review. After a run is finished and judged, a reviewer sees the
issue text and both arms' patches in a seeded A/B order with the arm hidden. It
rates each patch from 1 to 5 on correctness risk (1 lowest risk), minimality
(5 most minimal) and test quality (5 best), and picks a preference or a tie.
Run it only after judging:

```bash
python3 quoin/benchmarks/scripts/pairwise_review.py --run-id <run-id>
```

`quoin bench tiered` prints this command after a clean, fully judged finish.
Notes:

- Spend goes through the run's spend ledger. The per-review cap and the review
  total cap come from the suite (`$2` and `$80` for tiered-balanced) or from
  `--max-review-usd` and `--max-review-total-usd`. The run-level ceiling is
  `--authorised-usd`, else the manifest's `authorised_usd`, else the suite
  ceiling. A run started with a higher cap needs the matching ceiling.
- Review spend is recorded in the same ledger as the arms' sessions, so it
  reduces the run's remaining authorised ceiling. A later resume of the
  benchmark run has that much less headroom.
- A task whose prompt (issue text plus both patches) is over 400,000
  characters is skipped with reason `patch-too-large` before any spend is
  reserved, so one large patch cannot overshoot the per-review cap.
- Patch and issue text are untrusted. Text that reads as one of the prompt's
  section tags is escaped, and binary changes are replaced by a one-line note.
- The reviewer model defaults to the run's plain-arm model; `--effort` falls
  back to the manifest's effort, and the command refuses when neither exists.
- The review is written to `pairwise-review.json` in the run directory and
  resumes: finished reviews are not sent again. Replies that do not fit the
  schema are recorded as invalid; invalid replies are final and are not
  retried (a retry flag is a possible later addition).
- A lock file `pairwise-review.lock` prevents two reviews at once. If a review
  process died, remove the file by hand once no review process is alive.
- Style or size can still reveal the arm to the reviewer, and a reviewer can
  favour the first or second position. The report counts both preferences by
  position so a bias is visible; the review is supporting evidence only.

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
   when a pilot row has no denial count, naming the row and the reason.
   Denials of the base deny rules both arms share (`WebFetch`, `WebSearch` and
   Bash `curl`, `wget`, `pip`, `pip3`, `claude`, for example a blocked
   `pip install` while building a test environment) are printed but not counted
   toward the gap. After reviewing the pilot rows, `--ignore-parity` continues
   past either stop.
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
The report lists the route (`direct`, `light`, `fast`, `full` or `unknown`) the
pipeline took for each row, read from the brief's `route:` line on light `/run`
and from the plan on the hard route (where `run_hard` strips a `fast:` token from
anywhere in its input and issue bodies can contain one). `summary.md` also
splits the quoin arm's pass@1, mean cost and mean wall time by tier and route,
beside the plain arm on the same tasks, and tabulates how often each tier took
each route.

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

### Tier selection and repeated runs

`--tiers` runs only some tiers of the suite (a comma list, for example
`--tiers large` or `--tiers medium,large`). The estimate and the worst-case
prompt count only the selected tasks, and the run's manifest records only the
selected tiers. The pilot runs only when `--pilot-tier` is among the selected
tiers; otherwise the command prints a note and skips the pilot, and the
denial-parity check with it.

`--repeat N` runs the selected tasks N times, one after another, as sibling
run ids `<run-id>-r1` ... `<run-id>-rN`. Each repeat is an ordinary run with
its own manifest, default spend ledger (`<run-dir>/<run-id>-rK.spend-ledger.jsonl`),
summary, judging and paired order, so the per-run summary, `--judge-only` and
`--clean --run-id <run-id>-rK` work on a repeat as on any run. The command
prints the sessions still to spawn per repeat and asks once for all of them.
`--authorised-usd` applies to each repeat's own ledger; pass `--spend-ledger`
to share one ledger, and one ceiling, across the repeats. Re-running the same
command continues the unfinished repeats: finished ones only re-report (and
judge rows still pending), and an interrupted one resumes where it stopped.
A run started with `--tiers` keeps that selection: re-run it with the same
`--tiers`, since adding the pilot tier to it later is refused.

Large tier, three repeats, lean route:

```bash
QUOIN_BENCH_QUOIN_ROUTE=lean quoin bench tiered --suite tiered-balanced \
  --tiers large --repeat 3 --run-id lean-large
```

Then compare the arms across the repeats (the command prints this line when it
finishes):

```bash
RUNS=.workflow_artifacts/quoin-benchmarks/runs
python3 quoin/benchmarks/scripts/multi_run_report.py \
  $RUNS/lean-large-r1 $RUNS/lean-large-r2 $RUNS/lean-large-r3 --json lean-large.json
```

`multi_run_report.py` takes any run directories of the same suite, repeats or
not. A run counts for a task only when both arms ran it and neither row is
invalid. The report gives:

- per task and arm, passes / runs, the pass-rate difference (quoin minus
  plain) and the route the quoin arm took (`mixed` when it differed between
  runs);
- per tier and overall, each arm's mean per-task pass rate, the mean per-task
  difference, an exact sign test and an exact Wilcoxon signed-rank test over
  the per-task differences (two-sided; tasks with no difference are dropped);
- stability: the tasks each arm solved in every run, in none, and in some;
- the same figures per quoin route, when the runs record one per task.

It refuses runs whose suite sha, plain or quoin model, or quoin route differ
(a manifest without a recorded route counts as the default route), and lists
each differing value per run; `--allow-mixed` combines them anyway
and marks the report as mixed. With a handful of tasks the tests have little
power: read a large p-value as "not shown", not as "no difference".

### Resume and interruption

Re-running the same `quoin bench tiered` command continues an interrupted
run: sessions already recorded are kept, the pilot is not repeated once the
full leg has started, and a resume may add tiers to a run but never drop one. The manifest's
`started_at` reflects the latest leg. The manifest also records the quoin
route (`quoin_route`, plus `quoin_run_profile` for lean and memory), and a resume with a
different `QUOIN_BENCH_QUOIN_ROUTE` is refused, so lean, light and hard rows never
mix in one run id (a manifest without the key counts as the default route).

`--resume` never re-pays for a recorded session, including one stopped by the
budget (`budget_stopped`). A row whose spawn was interrupted or that hit an
identity check is re-spawned once and the earlier attempt moves under
`.superseded/`. A row that hit a harness exception is re-spawned once; a second
one for the same task is kept as an `error` row, logged with both directories,
and the remaining tasks still run. Ctrl-C, SIGTERM and SIGHUP stop the run
cleanly: the running session is killed with its process group, the spend
ledger is settled, the sandboxes are removed, and the manifest records the
signal for a later `--resume`.

### Sequence suite (SWE-Bench-CL): memory across tasks

On the tiered suites every task starts from an empty `.workflow_artifacts/`,
so nothing quoin remembers can help a later task. `suite-cl-sequences.json`
measures that: 36 SWE-bench Verified tasks in three per-repository
chronological sequences of 12 (django, sympy, sphinx) taken from SWE-Bench-CL.
Provenance, the task list and the budget are in
`suite-cl-sequences-provenance.md`. Repo preparation, prompt and judge are
the Verified ones.

The suite sets `tiered.sequence_mode`. Its tiers are the sequences, run in
order, each task on a fresh checkout at its own base commit. For the quoin
arm on the memory route, the previous task's `.workflow_artifacts/memory/` (lessons learned,
discovery files, daily insights; never session state such as run-state or
checkpoints) is copied into the next checkout before the session starts;
code, branches, `cache/`, sessions and the arm's auto memory never carry. The
plain arm stays stateless. After each quoin task the row directory gets
`memory-snapshot/` and `memory-carry.json` (what it inherited, from which
task, lessons inherited and written); a resume seeds the next task from the
nearest earlier snapshot in the run. The pilot is the first sequence
(`tiered.pilot_tier`), so it also runs in order.

Run the memory arm and, as the ablation, the lean arm (no carry: each task
starts empty) as separate run ids over the same suite:

```bash
QUOIN_BENCH_QUOIN_ROUTE=memory quoin bench tiered --suite cl-sequences --run-id cl-memory
QUOIN_BENCH_QUOIN_ROUTE=lean quoin bench tiered --suite cl-sequences --run-id cl-lean
```

Left to triage, every quoin task in these runs took the r0 route, so
discovery never ran and little memory carried. To exercise the carry, force
the r2 class on both arms (same suite, same quoin commit, new run ids):

```bash
QUOIN_BENCH_QUOIN_CLASS=r2 QUOIN_BENCH_QUOIN_ROUTE=memory quoin bench tiered --suite cl-sequences --run-id cl-memory-r2
QUOIN_BENCH_QUOIN_CLASS=r2 QUOIN_BENCH_QUOIN_ROUTE=lean quoin bench tiered --suite cl-sequences --run-id cl-lean-r2
```

The class is recorded in the manifest as `quoin_forced_class`, and a resume
with a different class is refused. Expect r2 sessions to cost more than the
estimate below, which is for r0 sessions.

Each run's worst case is $2,520 (36 tasks x 2 arms x $35, ceiling $2,780;
pilot $840). Expected spend from the tiered-balanced easy and medium sessions
is about $160 for the memory run and $145 for the lean run, about $305 for
both. `--tiers` and `--repeat` work as on the other suites; `--tiers` keeps
whole sequences.

Report by sequence position:

```bash
RUNS=.workflow_artifacts/quoin-benchmarks/runs
python3 quoin/benchmarks/scripts/sequence_report.py $RUNS/cl-memory \
  --lean-run $RUNS/cl-lean --json cl-sequences.json
```

It gives, per arm (plain, memory, lean), pass rate, mean cost and mean time
over the first, middle and last third of the sequences and the slope of each
against relative position; the paired per-task differences memory minus lean
and memory minus plain with a sign test; and per task what memory the memory
arm inherited and how many lessons it wrote. Rows still awaiting judging are
left out, not counted as failures, and the report says how many per arm. It
reads no model and costs nothing. `multi_run_report.py` still compares repeats
of one route.

### Vague-prompt suite: does prompt triage help?

Outcome: prompt triage was removed from `/run` in v0.92.0 after the first
vague and ceiling runs, without the repeat runs the decision rule below asks
for. That overrides the rule, which never drops triage on a single run. The
reasons: triage classed 18 of 20 vague requests C1 (the rule needs at least
half classed C2 or C3); the difference in differences was -10 points (on vague
prompts quoin passed 80% and plain 85%); the vague prompts did not hurt plain
Opus (85% on them against 75% on the original issues), so a repeat of the same
suite could not show triage helping; and triage cost about $0.06 a task,
roughly 9% of an r0 session. The suites and `vague_report.py` stay for any
later change to how `/run` handles prompts; on runs made after the removal
briefs carry no class, so the report lists every session as unknown and its
C2-or-C3 share as 0%. The
rest of this section describes the suite and the rule as they were run.

Every other suite gives the agent a fully specified issue, so `/run`'s prompt
triage (which classes a request C1 clear, C2 unstructured or C3
underspecified, and under `--autonomous` records default assumptions instead
of asking) has nothing to fix. `suite-verified-vague.json` holds 20 of the
tiered-balanced tasks (6 easy, 7 medium, 7 large). Each prompt is a short
rewrite that keeps only the symptom: no reproduction, no expected output, no
fix. `suite-verified-vague-ceiling.json` holds the same tasks with the original
issue text.

The rewrites were written once by a model under fixed instructions, reviewed,
and vendored in `data/swebench-verified-vague-rewrites.json`. Each rewrite
carries the hash of the statement it was written from. Both suites regenerate
byte for byte from vendored files, and judging is the usual hidden Verified
tests. The selection, the rewrite procedure, every prompt and the budget
derivation are in `suite-verified-vague-provenance.md`.

The arms:

| Cell | Run | Arm | Prompt |
|---|---|---|---|
| plain-vague | vague run | plain Claude Code | vague rewrite |
| quoin-vague | vague run | `/run --autonomous` (prompt triage, then scope triage, before v0.92.0) | vague rewrite |
| plain-original | ceiling run | plain Claude Code | original issue |
| quoin-original | ceiling run | `/run --autonomous` | original issue |

plain-original is the ceiling. quoin-original comes free with the paired runner,
and it gives the edge `/run` has on fully specified prompts; subtracting it
leaves the part of the quoin edge that appears only when the prompt is vague. Run
both suites with the default quoin route, the same models and the same
checkout:

```bash
quoin bench tiered --suite verified-vague --run-id vague-r1
quoin bench tiered --suite verified-vague-ceiling --run-id vague-ceiling-r1
RUNS=.workflow_artifacts/quoin-benchmarks/runs
python3 quoin/benchmarks/scripts/vague_report.py $RUNS/vague-r1 \
  --ceiling-run $RUNS/vague-ceiling-r1 --json vague-r1.json
```

`--ceiling-run` is optional. The report refuses runs whose models or quoin
route differ, whose suites are not the vague and original variants of the
same tasks, or whose recorded suite sha no longer matches the suite file (a
regenerated suite needs new runs); `--allow-mixed` overrides this.

Cost: each run's worst case is $1,400 (20 tasks x 2 arms x $35, ceiling $1,590;
pilot $420). Expected spend is about $120 per run and $240 for both, scaled
from the tiered-balanced estimate. Quoin sessions on the default r0 route have
cost less since then, so expect about $35 per run at the low end. The report
reads no model and costs nothing.

The report gives:

- per cell, overall and per tier: pass rate with a 95% Wilson interval, mean
  cost and mean time;
- the headline, quoin-vague minus plain-vague, paired per task with a sign
  test, plus its cost and time difference;
- with a ceiling run: the pass rate plain loses to vagueness, the share of that
  loss the quoin arm recovers, and the difference in differences (quoin's edge
  on vague prompts minus its edge on the original prompts);
- how prompt triage classed each counted quoin-vague session, read from the
  brief it wrote: the share classed C2 or C3, and pass rates per class.

Rows awaiting judging are left out, not counted as failures.

Decision rule, fixed before any run. The three criteria:

1. **It notices.** At least half of the counted quoin-vague sessions are
   classed C2 or C3, as the report's triage line shows. A vague prompt the
   rubric calls C1 (clear) is a miss. C2 counts, because the rubric sends a
   request with an obvious reasonable reading to C2 rather than C3; the C3
   share is shown for diagnosis only.
2. **It helps where it should.** The difference in differences is at least
   +10 points (two tasks), and the sign test on quoin-vague minus plain-vague
   has more tasks only quoin passed than only plain passed.
3. **It pays for itself.** The `triage` phase costs less than 10% of the mean
   quoin-vague session cost, per the phase report.

After the first vague and ceiling runs, prompt triage stays if all three hold.
Otherwise repeat both runs with `--repeat 2` and apply the rule to the pooled
rows of all three runs of each suite. Prompt triage then stays if all three
hold and is removed if not. Twenty pairs only detect a large effect, so one
run is enough to keep triage on a clear result but never to drop it. The
report takes several runs of each suite and pairs the arms within each run.
With repeats the sign test counts each run's pair separately, so read its
p-value as optimistic; the rule uses only the direction.

```bash
python3 quoin/benchmarks/scripts/vague_report.py $RUNS/vague-r1 $RUNS/vague-r2-r1 $RUNS/vague-r2-r2 \
  --ceiling-run $RUNS/vague-ceiling-r1 --ceiling-run $RUNS/vague-ceiling-r2-r1 \
  --ceiling-run $RUNS/vague-ceiling-r2-r2 --json vague-pooled.json
```

### SWE-bench Pro suite: larger tasks

Verified fixes are mostly single-file. `suite-pro-balanced.json` holds 40
Python tasks from SWE-bench Pro V2 (Scale AI; ansible, openlibrary,
qutebrowser) in three tiers by reference patch size: 10 small (22-49 changed
lines), 15 medium (50-104), 15 large (104 and up, median across Pro Python
69 lines over 3 files). Provenance, the task list and the budget are in
`suite-pro-balanced-provenance.md`. Repo preparation is the Verified one
(`source: swebench_pro`); the prompt is Scale's standard instruction (problem
statement, requirements, new interfaces; template `pro-v1`); the judge runs
the task's official verifier in its public image, fetched at judge time from a
pinned harness commit and hash-checked. It needs Docker, not swebench; the
images are amd64 only, so pass `--judge-emulation` on an arm64 host, and allow
about 1.25 GB of Docker disk per task.

```bash
quoin bench tiered --suite pro-balanced --run-id pro-r1 --judge-emulation
RUNS=.workflow_artifacts/quoin-benchmarks/runs
python3 quoin/benchmarks/scripts/multi_run_report.py $RUNS/pro-r1 --json pro-r1.json
```

Worst case $2,800 (40 tasks x 2 arms x $35, ceiling $3,170; small-tier pilot
$700). The first run (pro-r1, quoin on the lean route) cost about $64 for its
80 sessions: plain $0.45 and quoin $1.16 per session on average, $2.87 at most.
Deferred judging takes task after task (both arms back to back) and then removes
the task's image, so Docker needs room for one image at a time;
`QUOIN_BENCH_PRO_KEEP_IMAGES=1` keeps them. `QUOIN_BENCH_QUOIN_ROUTE`, `--tiers` and `--repeat` work as
on the Verified suites. A live judge check of one real instance (reference
patch passes, empty patch fails) runs with
`QUOIN_BENCH_PRO_LIVE=1 python3 -m pytest quoin/dev/tests/test_swebench_pro.py -k live`.

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
- a per-phase table for the quoin arm, per tier and overall: mean dollars
  per task, share of cost, mean seconds per task, share of time, and the
  number of sessions where each `/run` phase ran (see "Phase report" below);
- the validity block: it says whether the run is valid, whether results are
  final or provisional (rows awaiting judging count as errors until judged),
  the suite and manifest hashes, the models seen, and every flagged row.

With nine tasks, treat differences as directional.

### Phase report

Light `/run` appends one line per step to
`.workflow_artifacts/<task>/phases.log` (`<UTC time> <phase> [skipped]`, with
an `N:` stage prefix inside a staged task) through `run_phase.py`. The phases are
setup, discover, frame, context, decompose, implement, verify, review,
review-fixes and commit. Logs written before v0.92.0 may also hold a `triage`
line; the report ignores it, so that time counts under `setup`. The quoin cell copies that file with the rest of the
workflow evidence, and `harness/phase_report.py` joins it with the row's
`transcript.jsonl`:

- assistant messages are deduplicated by message id; a main-session message
  belongs to the phase active at its timestamp, and a subagent message belongs
  to the phase that spawned it, even when the subagent runs in the background;
- each message is priced from `pricing.json`. The reconstructed total runs
  well under the billed `modelUsage` total (about half on the light-route run),
  so phase costs are also scaled to the billed total and labelled as an
  estimate. Both totals are kept;
- time is split by the gaps between transcript timestamps. When those gaps add
  up to more than the measured wall clock (a suspended machine or out-of-order
  timestamps), the largest gaps are cut first until the sum equals it.

The result is stored as `metrics.json["phase_report"]` for each quoin row
(`source: markers`). Rows without a `phases.log`, which is every run before the
markers existed, are inferred from the Agent calls' descriptions (`source:
inferred`), and the summary is rebuilt from the transcript when the stored
report is missing. Inferred rows cannot tell verify from implement or see the
commit step. `python3 -m quoin.benchmarks.harness.phase_report <run dir>` prints
the overall table for an existing run.

Limits: the markers for the frame, context and implement steps have no earlier
Bash call to share, so each adds one short tool turn to a quoin session, a small
cost that rows from before the markers did not pay. When a non-Opus session hands
implementation to an Opus subagent, the review fixes it sends to that subagent
count as `implement`, because subagent messages stay with the phase that spawned
them. Stages running in parallel interleave their markers, so their phases blur.

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
