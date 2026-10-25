# Suite tiered-small Provenance

## Dataset

| Item | Value |
|---|---|
| Dataset | `princeton-nlp/SWE-bench_Verified` |
| Split | `test` |
| Pinned revision | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` |
| Hugging Face `main` at fetch time | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` (same as the pinned revision) |
| Fetch date | 2026-10-22 |
| Parquet file | `data/test-00000-of-00001.parquet`, 2096679 bytes |
| Parquet sha256 | `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd` (equals the Hugging Face LFS object id) |
| Vendored snapshot | `quoin/benchmarks/data/swebench-verified-snapshot.json`, sha256 `8912b2480c5680497e34294a0ffcb2544aa39a6994b38cc5d403df48b26376b0` |
| Vendored problem statements | `quoin/benchmarks/data/swebench-verified-problems.json`, sha256 `e5ea60e22f2c454bd926f470fb3f5b747dada42f003c9c9575da6366a3cef31e` |
| `row_count` | 500 |
| `difficulty_counts` | `1-4 hours`: 42, `15 min - 1 hour`: 261, `<15 min fix`: 194, `>4 hours`: 3 |

The counts were written by the refresh mode from all 500 rows of the file.

## Fetch procedure

Run from the repository root. The download and the parser environment live
outside the repository; `pyarrow` is not a project dependency.

```bash
R=c104f840cc67f8b6eec6f759ebc8b2693d585d4a
curl -fsS "https://huggingface.co/api/datasets/princeton-nlp/SWE-bench_Verified/revision/$R?blobs=true"
mkdir -p "$S/swebv-dl" && curl -fL --max-time 300 -o "$S/swebv-dl/test-00000-of-00001.parquet" \
  "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/resolve/$R/data/test-00000-of-00001.parquet"
shasum -a 256 "$S/swebv-dl/test-00000-of-00001.parquet"   # must equal the sha256 above
uv venv "$S/hf-venv" --python 3.12 && uv pip install --python "$S/hf-venv/bin/python" "pyarrow==18.1.0"
"$S/hf-venv/bin/python" -I quoin/benchmarks/scripts/pick_subset.py --refresh-verified-snapshot \
  --parquet-file "$S/swebv-dl/test-00000-of-00001.parquet" --revision $R \
  --fetched-on 2026-10-22 --seed 1729
```

The download always uses the pinned revision, never `main`. The refresh mode
itself performs no network access; it only reads the downloaded file.

## What is vendored and what is not

Vendored: four fields (`instance_id`, `repo`, `base_commit`, `difficulty`) for
all 500 rows, and the `problem_statement` text of the nine selected instances.
Not vendored: the parquet file, gold patches, test patches, `hints_text`
(excluded, matching standard SWE-bench practice), and the FAIL_TO_PASS and
PASS_TO_PASS lists. A later stage fetches what it needs for grading itself.

## Subset selection

- Tool: `quoin/benchmarks/scripts/pick_subset.py --suite tiered-small`
- Seed: 1729, reused from suite v1; chosen before any result exists
- Per tier, in the order easy, medium, large: sort the instance IDs carrying the
  tier's difficulty label, shuffle with `random.Random(seed + offset)`, take the
  first n, sort the chosen IDs and number them `verified_<tier>_NNN`
- Sub-seed offsets: easy 101, medium 102, large 103
- Counts: 4 easy, 3 medium, 2 large
- The `>4 hours` label has no tier and is never selected

## Tasks

| Task ID | Tier | Upstream instance | Repo | Difficulty label |
|---|---|---|---|---|
| verified_easy_000 | easy | django__django-11119 | django/django | <15 min fix |
| verified_easy_001 | easy | django__django-13023 | django/django | <15 min fix |
| verified_easy_002 | easy | pytest-dev__pytest-7205 | pytest-dev/pytest | <15 min fix |
| verified_easy_003 | easy | sympy__sympy-12481 | sympy/sympy | <15 min fix |
| verified_medium_000 | medium | django__django-14559 | django/django | 15 min - 1 hour |
| verified_medium_001 | medium | matplotlib__matplotlib-25960 | matplotlib/matplotlib | 15 min - 1 hour |
| verified_medium_002 | medium | sympy__sympy-13551 | sympy/sympy | 15 min - 1 hour |
| verified_large_000 | large | django__django-15128 | django/django | 1-4 hours |
| verified_large_001 | large | django__django-15957 | django/django | 1-4 hours |

## Budgets

The quoin arm runs the whole `/run` pipeline (every phase the profile triggers,
including subagents), so the per-session cap, the worst case and the wall clock
are sized from what comparable full-pipeline tasks cost, not from a single
direct fix. Token estimates per tier are advisory and fixed before any result
exists.

| Tier | Estimated tokens in | Estimated tokens out |
|---|---|---|
| easy | 250000 | 12000 |
| medium | 500000 | 25000 |
| large | 900000 | 45000 |

### Derivation

Sources: the finalized task folders of the parent workspace
(`.workflow_artifacts/finalized/*/cost-summary.json`, task root only; stage
files are ignored). A task qualifies when its summary has a `run-orchestrator`
phase (it ran the pipeline) and its total, `resolved_total` when present and
`task_total` otherwise, is at most $100. That cutoff separates the
Small and Medium tasks this benchmark resembles from the programme-sized ones.
The sample is 185 finalized folders, 145 with a cost summary, 40 with a
`run-orchestrator` phase and 19 under the cutoff.

Repricing. The sources were billed at several price lists, and the benchmark
runs the 5.5 models, so each per-model bucket is repriced to 5.5 rates only
when it is known to predate them. An opus bucket is multiplied by 0.8 ($5 to
$4 input) when the task's last ledger date is before 2026-10-08, the date the
5.5 prices were recorded; every other bucket is multiplied by 1.0 (a
Sonnet or Haiku bucket of unknown generation, fable, other and unattributed
spend), so an unknown generation never lowers the figure and the cap stays an
upper bound. The per-task transcripts were not re-walked: most ledger rows
carry inline per-agent costs and the oldest transcript still on disk is from
2026-09-24, so every row below uses the ratio rule.

| Source task | Total used | Last ledger date | Path | Repriced USD |
|---|---|---|---|---|
| ivg-299-quoin-sessions-attach | resolved_total 89.74 | 2026-10-23 | fallback | 89.74 |
| ivg-255-extra-comments-in-pr | resolved_total 82.98 | 2026-09-10 | fallback | 77.08 |
| ivg-250-known-red-false-mismatch | resolved_total 90.58 | 2026-08-20 | fallback | 76.66 |
| ivg-290-real-context-utilization | resolved_total 75.88 | 2026-10-13 | fallback | 75.88 |
| ivg-254-known-red-selector-shape | resolved_total 74.86 | 2026-08-20 | fallback | 62.27 |
| ivg-128-parallel-dimension-reviewers | task_total 63.91 | 2026-07-19 | fallback | 59.87 |
| ivg-119-nested-memory-roots | task_total 66.37 | 2026-07-22 | fallback | 53.10 |
| ivg-152-prompt-improvement | task_total 37.85 | 2026-07-20 | fallback | 37.85 |
| ivg-123-preamble-freshness-gate | resolved_total 39.13 | 2026-08-19 | fallback | 33.99 |
| ivg-263-sleep-cleanup-sonnet-tier | resolved_total 36.14 | 2026-09-25 | fallback | 32.86 |
| streamdeck-zellij-session-grouping | resolved_total 28.09 | 2026-10-15 | fallback | 28.09 |
| ivg-271-opencode-runtime-config | resolved_total 31.66 | 2026-09-30 | fallback | 25.33 |
| ivg-265-pr-no-claude-attribution | resolved_total 27.70 | 2026-09-25 | fallback | 24.34 |
| session-age-guard-current-session | resolved_total 23.28 | 2026-10-14 | fallback | 23.28 |
| ivg-118-registration-manifest | task_total 28.17 | 2026-07-22 | fallback | 22.53 |
| ivg-117-dispatch-guardrail-gaps | task_total 22.80 | 2026-07-21 | fallback | 20.43 |
| context-tracker-mod | resolved_total 16.85 | 2026-10-03 | fallback | 13.48 |
| ivg-282-autonomous-child-takeover | resolved_total 12.61 | 2026-09-30 | fallback | 12.61 |
| workflow-tasks-mod | resolved_total 9.73 | 2026-10-04 | fallback | 9.73 |

Rule applied:

```
cap         = ceil_to_5(max repriced)                     = 90
worst case  = cap x 9 tasks x 2 arms                      = 1620
target      = ceil_to_10(worst case)                      = 1620
ceiling     = ceil_to_10(target x 1.1)                    = 1790
pilot worst = cap x 4 easy tasks x 2 arms                 = 720
```

| Figure | Value |
|---|---|
| Per-session cap | $90 |
| Worst case (9 tasks, 2 arms) | $1,620 |
| Documented target | $1,620 |
| Enforced ceiling | $1,790 |
| Pilot (easy tier) worst case | $720 |

The $100 cutoff, not the sample, sets the cap: the cap is about 0.9 of the
cutoff (and equal to the most expensive repriced source, rounded up), so a
different cutoff would move it in proportion. The cap is a bound, not a
forecast: it is sized so it does not bind on a full-pipeline run, and the
ledger settles what was actually spent.

Spend-rate caveat. The wall clock below uses the spend rate of the earlier
tiered-small run, which was made of direct-fix sessions on Opus only (18
sessions, $38.81 over 4,373.7 session seconds, about $0.00887 per second,
scaled by 4.00/5.00 for the lower 5.5 input price). A pipeline run spreads its
spend over subagents and may spend faster or slower per wall second, so the
wall clock is a guard, and post-plan timeouts are counted separately in the
report's Validity section.

### Wall clock

`wall_clock = max(current, ceil_to_300(1.5 x cap / spend_rate))`, with the
spend rate above: 1.5 x 90 / 0.00710 = 19,015 s, rounded up to **19,200 s**.
The previous per-tier values (1,800, 3,600 and 5,400 s) are below that, so
every tier and both arms get 19,200 s from the suite's `wall_clock_seconds`
field.

### Cap visibility

The cap is not written into either arm's prompt. `--max-budget-usd` stays
armed in both arms, and the CLI reminds the session of its remaining budget
each turn, so the cap is hidden from the prompt rather than from the model
entirely; it is sized to be non-binding instead. Changing the suite changes its
SHA, so a run started on the old suite needs a new `--run-id`.

## Reproducing

Offline, from the repository root:

```bash
python3 quoin/benchmarks/scripts/pick_subset.py --suite tiered-small --seed 1729 \
  --verify quoin/benchmarks/suite-tiered-small.json
```

The suite embeds the snapshot sha256, so a successful verify also shows the
vendored snapshot is unchanged; the task descriptions show the problem
statements are unchanged.

## Contamination surface

The data is public. Models were likely trained on SWE-bench Verified issues and
on the upstream repositories, and each gold fix is reachable in the upstream
history after the base commit. This is accepted and documented; a later stage
handles it through repository preparation. Results should be read as relative
comparisons between arms on the same tasks, not as absolute scores.

## Untrusted descriptions and agent network access

Task descriptions are the upstream issue text, passed to the agent unchanged.
They are untrusted input and may contain instructions aimed at the agent. The
agent runs with broad Bash on the host, and only the web tools and the Bash
prefixes `curl`, `wget`, `pip`, `pip3` and `claude` are denied, so the network
remains reachable through other commands in both arms. The sandbox limits what
the agent can see and keep, not what it can try to do.

## Pair order

Each task is run for the two arms one after the other, plain arm first, then
quoin, task by task. Sessions never overlap, whatever `--max-parallel` says.
