# Suite tiered-balanced Provenance

## Dataset

| Item | Value |
|---|---|
| Dataset | `princeton-nlp/SWE-bench_Verified` |
| Split | `test` |
| Pinned revision | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` |
| Parquet file | `data/test-00000-of-00001.parquet`, the same file as the tiered-small snapshot |
| Parquet sha256 | `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd` (re-downloaded at the pinned revision on 2026-10-26; equals the snapshot's `source_file_sha256`) |
| Vendored snapshot | `quoin/benchmarks/data/swebench-verified-snapshot.json`, sha256 `8912b2480c5680497e34294a0ffcb2544aa39a6994b38cc5d403df48b26376b0` (shared with tiered-small, unchanged) |
| Vendored problem statements | `quoin/benchmarks/data/swebench-verified-problems-tiered-balanced.json`, sha256 `128544ecb2e0656a48063028cf32f581572aa8ab96b8e2305ec5641d50c7fcab` |

The snapshot is not rewritten for this suite. Only the problem statements of
the 40 selected instances are new; the `problem_statement` text is verbatim
upstream text and `hints_text` is excluded.

## Subset selection

- Tool: `quoin/benchmarks/scripts/pick_subset.py --suite tiered-balanced`
- Seed: 1729 with sub-seed offsets easy 101, medium 102, large 103, the same
  as tiered-small, so every tiered-small instance is also in the same tier here
- Per tier: sort the instance IDs carrying the tier's difficulty label, shuffle
  with `random.Random(seed + offset)`, take the first n, sort the chosen IDs and
  number them `verified_<tier>_NNN`
- Counts: 10 easy, 15 medium, 15 large (40 tasks)
- The `>4 hours` label has no tier and is never selected

## Tasks

| Task ID | Tier | Upstream instance | Repo | Difficulty label |
|---|---|---|---|---|
| verified_easy_000 | easy | django__django-10999 | django/django | <15 min fix |
| verified_easy_001 | easy | django__django-11119 | django/django | <15 min fix |
| verified_easy_002 | easy | django__django-11951 | django/django | <15 min fix |
| verified_easy_003 | easy | django__django-13023 | django/django | <15 min fix |
| verified_easy_004 | easy | pylint-dev__pylint-4970 | pylint-dev/pylint | <15 min fix |
| verified_easy_005 | easy | pytest-dev__pytest-10081 | pytest-dev/pytest | <15 min fix |
| verified_easy_006 | easy | pytest-dev__pytest-7205 | pytest-dev/pytest | <15 min fix |
| verified_easy_007 | easy | sphinx-doc__sphinx-7889 | sphinx-doc/sphinx | <15 min fix |
| verified_easy_008 | easy | sphinx-doc__sphinx-9711 | sphinx-doc/sphinx | <15 min fix |
| verified_easy_009 | easy | sympy__sympy-12481 | sympy/sympy | <15 min fix |
| verified_medium_000 | medium | django__django-12155 | django/django | 15 min - 1 hour |
| verified_medium_001 | medium | django__django-13516 | django/django | 15 min - 1 hour |
| verified_medium_002 | medium | django__django-14559 | django/django | 15 min - 1 hour |
| verified_medium_003 | medium | django__django-15252 | django/django | 15 min - 1 hour |
| verified_medium_004 | medium | django__django-15916 | django/django | 15 min - 1 hour |
| verified_medium_005 | medium | django__django-16950 | django/django | 15 min - 1 hour |
| verified_medium_006 | medium | matplotlib__matplotlib-25960 | matplotlib/matplotlib | 15 min - 1 hour |
| verified_medium_007 | medium | psf__requests-2931 | psf/requests | 15 min - 1 hour |
| verified_medium_008 | medium | pydata__xarray-3151 | pydata/xarray | 15 min - 1 hour |
| verified_medium_009 | medium | pydata__xarray-7229 | pydata/xarray | 15 min - 1 hour |
| verified_medium_010 | medium | pytest-dev__pytest-5840 | pytest-dev/pytest | 15 min - 1 hour |
| verified_medium_011 | medium | scikit-learn__scikit-learn-25931 | scikit-learn/scikit-learn | 15 min - 1 hour |
| verified_medium_012 | medium | sphinx-doc__sphinx-7985 | sphinx-doc/sphinx | 15 min - 1 hour |
| verified_medium_013 | medium | sympy__sympy-13551 | sympy/sympy | 15 min - 1 hour |
| verified_medium_014 | medium | sympy__sympy-15599 | sympy/sympy | 15 min - 1 hour |
| verified_large_000 | large | astropy__astropy-14369 | astropy/astropy | 1-4 hours |
| verified_large_001 | large | django__django-11400 | django/django | 1-4 hours |
| verified_large_002 | large | django__django-11885 | django/django | 1-4 hours |
| verified_large_003 | large | django__django-12325 | django/django | 1-4 hours |
| verified_large_004 | large | django__django-13344 | django/django | 1-4 hours |
| verified_large_005 | large | django__django-13449 | django/django | 1-4 hours |
| verified_large_006 | large | django__django-15128 | django/django | 1-4 hours |
| verified_large_007 | large | django__django-15957 | django/django | 1-4 hours |
| verified_large_008 | large | django__django-16263 | django/django | 1-4 hours |
| verified_large_009 | large | pylint-dev__pylint-4551 | pylint-dev/pylint | 1-4 hours |
| verified_large_010 | large | pylint-dev__pylint-8898 | pylint-dev/pylint | 1-4 hours |
| verified_large_011 | large | pytest-dev__pytest-10356 | pytest-dev/pytest | 1-4 hours |
| verified_large_012 | large | pytest-dev__pytest-6197 | pytest-dev/pytest | 1-4 hours |
| verified_large_013 | large | sphinx-doc__sphinx-8548 | sphinx-doc/sphinx | 1-4 hours |
| verified_large_014 | large | sphinx-doc__sphinx-9461 | sphinx-doc/sphinx | 1-4 hours |

### Nine instances shared with tiered-small

| Balanced task ID | Tiered-small task ID | Upstream instance |
|---|---|---|
| verified_easy_001 | verified_easy_000 | django__django-11119 |
| verified_easy_003 | verified_easy_001 | django__django-13023 |
| verified_easy_006 | verified_easy_002 | pytest-dev__pytest-7205 |
| verified_easy_009 | verified_easy_003 | sympy__sympy-12481 |
| verified_medium_002 | verified_medium_000 | django__django-14559 |
| verified_medium_006 | verified_medium_001 | matplotlib__matplotlib-25960 |
| verified_medium_013 | verified_medium_002 | sympy__sympy-13551 |
| verified_large_006 | verified_large_000 | django__django-15128 |
| verified_large_007 | verified_large_001 | django__django-15957 |

### Preview list comparison

The instance list in the tracker preview matches the generated selection
exactly: no differences.

## Preparing the repositories

The only network step before a run; it calls no model.

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py --suite tiered-balanced \
  --cells simple-claude,quoin-claude --prepare-repos
```

## Budgets

The quoin arm runs the whole `/run` pipeline, so the cap is sized from the
observed per-session cost of the tiered-small-v3 run (Opus 5.5 in both arms),
with headroom for the large tier, which grows from 2 to 15 instances here.

Observed per-session cost in USD and wall time in seconds:

| Tier | Quoin sessions | Plain sessions |
|---|---|---|
| easy | 2.71/372, 2.55/452, 3.91/785, 4.87/899 | 0.14/38, 0.18/49, 0.21/44, 0.21/102 |
| medium | 3.51/1161, 5.68/1274, 3.15/882 | 0.27/76, 0.44/95, 0.27/168 |
| large | 11.07/2850, 5.79/1240 | 0.54/810, 0.69/178 |

```
cap          = ceil_to_5(3 x max session 11.07 = 33.21)          = 35
worst case   = cap x 40 tasks x 2 arms                           = 2800
target       = ceil_to_10(worst case)                            = 2800
review cap   = 2 per review; review total = 2 x 40 tasks         = 80
ceiling      = ceil_to_10((worst case + review total) x 1.1)     = 3170   (3168 rounded)
pilot worst  = cap x 10 easy tasks x 2 arms                      = 700
slowest rate = min quoin session $/s = 3.51 / 1161               = 0.003023
wall clock   = ceil_to_300(1.5 x cap / slowest rate = 17365)     = 17400 s
```

| Figure | Value |
|---|---|
| Per-session cap | $35 |
| Worst case (40 tasks, 2 arms) | $2,800 |
| Documented target | $2,800 |
| Enforced ceiling | $3,170 |
| Pairwise review cap per review | $2 |
| Pairwise review total cap | $80 |
| Pilot (easy tier) worst case | $700 |

The wall clock is **17,400 s** for every tier and both arms.

Why 3x: a cap that binds turns a quoin session into `budget_stopped`, which
counts as a fail and would bias against quoin, so the cap is meant to stay
non-binding. The observed large-tier sample is two sessions, so the tail is
unobserved. The per-review cap is about ten times a single-turn estimate
(at most about 35k input tokens plus 2k output tokens, roughly $0.21).

Pilot rule: run the easy tier first. If any pilot session costs more than 50%
of the cap ($17.50) or any pilot row is `budget_stopped`, stop, raise the cap,
regenerate the suite (a new suite hash, so a new run id) and start again.
The easy-tier pilot is a weak signal for the large tier.

Owner cap note: a higher cap is a constant change plus regeneration. For
example a $50 cap gives a worst case of $4,000, which needs
`--authorised-usd` and `--max-review-total-usd` overrides on the review step
and a matching authorised ceiling on the run.

Token estimates per tier are reused from tiered-small (250000/12000,
500000/25000, 900000/45000 tokens in/out). They are advisory only: the quoin
cell budgets on `wall_clock_seconds`, so a re-derived estimate would have no
consumer.

Expected spend, for orientation and not a budget: arms about $239 (tier means
times counts), reviews about $10 to $20.

## Reproducing

Offline, from the repository root:

```bash
python3 quoin/benchmarks/scripts/pick_subset.py --suite tiered-balanced --seed 1729 \
  --verify quoin/benchmarks/suite-tiered-balanced.json
```

The suite embeds the snapshot and problems-file sha256 values, so a successful
verify also shows the vendored data is unchanged. To rebuild only this suite's
problems file from the pinned parquet file (the refusal checks the parquet
sha256 and revision against the committed snapshot, and the snapshot is never
written):

```bash
python3 quoin/benchmarks/scripts/pick_subset.py --refresh-verified-snapshot --problems-only \
  --suite tiered-balanced --parquet-file "$S/swebv-dl/test-00000-of-00001.parquet" \
  --revision c104f840cc67f8b6eec6f759ebc8b2693d585d4a
```

The download commands are in `suite-tiered-small-provenance.md`.

## Contamination surface

The data is public. Models were likely trained on SWE-bench Verified issues and
on the upstream repositories, and each gold fix is reachable in the upstream
history after the base commit. Results should be read as relative comparisons
between arms on the same tasks, not as absolute scores.

## Untrusted descriptions and agent network access

Task descriptions are the upstream issue text, passed to the agent unchanged,
and are untrusted input. The agent runs with broad Bash on the host; only the
web tools and a few Bash prefixes are denied, so the network stays reachable in
both arms.

## Pair order

Each task runs for the two arms one after the other, plain arm first, then
quoin. Sessions never overlap.

## Blind pairwise review

The review step sees both patches in a seeded A/B order with the arm hidden.
Patch style and size may still reveal the arm, and position bias is counted
and reported rather than corrected. The review is a secondary measure and
never changes `resolved`.

## Statistical power

Forty pairs detect a larger difference than nine do, but a small difference in
pass rate will still be inside the noise; read the secondary measures as
supporting evidence only.
