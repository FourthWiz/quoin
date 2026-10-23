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

Per-task wall clock and token figures are advisory estimates fixed before any
result exists and may be recalibrated later.

| Tier | Wall clock (s) | Estimated tokens in | Estimated tokens out |
|---|---|---|---|
| easy | 1800 | 250000 | 12000 |
| medium | 3600 | 500000 | 25000 |
| large | 5400 | 900000 | 45000 |

The figures follow from a recorded planning-only gate run that used about 110k
cache-write and 12k output tokens for one arm; a full run on a real repository
is several times that. At list rates of $5 and $25 per million tokens the
existing estimator gives roughly $27 per arm, about $54 for both arms. The
estimator prices all input at the full input rate, so it overstates cached
input.

The documented per-task cap is $5.50. The cap-based worst case is
$5.50 x 9 tasks x 2 arms = $99.00, a documented target of about $100. The
enforced ceiling is $110, which leaves headroom above the target for rounding
and a small number of retried tasks.

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
