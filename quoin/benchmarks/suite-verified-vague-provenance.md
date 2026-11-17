# Suite verified-vague Provenance

This file covers two suites over the same 20 tasks:

- `suite-verified-vague.json`: each prompt is a short, underspecified rewrite of the issue
  that keeps only the symptom.
- `suite-verified-vague-ceiling.json`: each prompt is the original issue text.

Every other benchmark gives the agent a fully specified task, so a prompt-triage
stage has nothing to do there. These suites give it vague requests, and judging
stays the hidden SWE-bench Verified tests, so a better reading of a vague request
shows up as more resolved tasks.

## Dataset

| Item | Value |
|---|---|
| Dataset | `princeton-nlp/SWE-bench_Verified`, split `test` |
| Pinned revision | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` |
| Vendored snapshot | `quoin/benchmarks/data/swebench-verified-snapshot.json`, sha256 `8912b2480c5680497e34294a0ffcb2544aa39a6994b38cc5d403df48b26376b0` |
| Pool problem statements | `quoin/benchmarks/data/swebench-verified-problems-tiered-balanced.json`, sha256 `128544ecb2e0656a48063028cf32f581572aa8ab96b8e2305ec5641d50c7fcab` |
| Vendored rewrites | `quoin/benchmarks/data/swebench-verified-vague-rewrites.json`, sha256 `4d4458777101506cfee72523d2d4eab22dd7a3a4c566a8a756e8c4efd74b9ed9` |
| Rewrite instructions sha256 | `c6b20db8084c3d49a0d56f3ae57e2bb88d6ac63d5da084703745ac0de5cd23ad` |

No new data was downloaded. The problem statements come from the tiered-balanced
suite's vendored file, whose fetch procedure is in
`suite-tiered-small-provenance.md`.

## Subset selection

- Tool: `quoin/benchmarks/scripts/pick_subset.py --suite verified-vague`
  (`--suite verified-vague-ceiling` for the ceiling suite)
- Pool: the 40 tasks of `tiered-balanced`, built with seed 1729 and its own
  sub-seeds (easy 101, medium 102, large 103)
- Seed: 1729, reused from the other suites and chosen before any result existed
- Per tier, in the order easy, medium, large: sort the pool's instance ids for
  that tier, shuffle with `random.Random(seed + offset)`, take the first n, sort
  the chosen ids and number them `vague_<tier>_NNN`
- Sub-seed offsets: easy 201, medium 202, large 203. These differ from the
  pool's offsets so the subset is not the head of the pool's own shuffle.
- Counts: 6 easy, 7 medium, 7 large

Both suites list the same tasks with the same ids in the same order. Only
`description` and `prompt_variant` differ.

## Rewrite procedure

The rewrites were written once and then vendored. A model cannot reproduce its
own output byte for byte, so the vendored file is what makes the suite
deterministic, and its hashes record where each prompt came from.

1. `pick_subset.py --emit-vague-requests <file>` writes the fixed rewrite
   instructions and the 20 selected problem statements.
2. A model writes one prompt per instance from those instructions into a
   `{instance_id: prompt}` draft. For this file that model was
   `claude-opus-5-5`, on 2026-11-17.
3. `pick_subset.py --vendor-vague-draft <draft> --rewriter-model <model>
   --fetched-on <date>` checks the draft and writes the rewrites file. The file
   records the model, the date, the instructions and their sha256, and for each
   prompt the sha256 of the problem statement it was written from.
4. A maintainer reviews every prompt against its statement in the pull request
   that vendors the file. The check: the symptom is kept, and the reproduction,
   expected output, root cause and fix are gone.

The instructions, verbatim (also stored in the rewrites file):

> Rewrite the GitHub issue below as the short request a busy user would type
> into a coding agent. Keep only the observable symptom, in one to three plain
> sentences and at most 300 characters. Leave out reproduction steps, code
> snippets, tracebacks, expected output, the root cause and any proposed fix. Do
> not name the function, class or file at fault unless the symptom cannot be
> stated without it; a user-facing name (a command, a public API call, a
> setting) may stay. Do not invent details the issue does not give.

The generator refuses to build either suite in any of these cases:

- a prompt's recorded statement hash no longer matches the vendored statement;
- the rewrites do not cover exactly the selected instances;
- the instructions do not match their hash;
- a prompt is empty, longer than 300 characters, more than one paragraph, or
  contains code markup.

To change a prompt, edit it under the same instructions, re-vendor it, and
regenerate both suites. That gives a new suite hash, so use a new run id.

## Prompt wrapper

Both suites use the unchanged Verified prompt template (`verified-v1`), with
the issue text replaced by the rewrite. Each arm therefore sees the same
framing as on the other Verified suites:

- the repository and commit;
- "work only from this repository";
- "hidden tests will evaluate your change".

The upstream instance id is never shown.

## Tasks

| id | tier | source_id | repo | vague prompt |
|---|---|---|---|---|
| vague_easy_000 | easy | django__django-10999 | django/django | Parsing some negative durations with Django's parse_duration doesn't work. |
| vague_easy_001 | easy | django__django-11119 | django/django | Rendering a template through an Engine created with autoescape turned off still escapes the output. |
| vague_easy_002 | easy | django__django-11951 | django/django | bulk_create with an explicit batch_size can build batches bigger than the database backend supports. |
| vague_easy_003 | easy | pylint-dev__pylint-4970 | pylint-dev/pylint | Setting min-similarity-lines to 0 makes pylint flag every line as duplicate code. |
| vague_easy_004 | easy | sphinx-doc__sphinx-7889 | sphinx-doc/sphinx | Autodoc crashes with a TypeError when it documents a generic class whose dependencies are mocked. |
| vague_easy_005 | easy | sphinx-doc__sphinx-9711 | sphinx-doc/sphinx | needs_extensions rejects sphinx-gallery 0.10 as too old. |
| vague_medium_000 | medium | django__django-15252 | django/django | In a multi-database setup, migrate creates the migrations table on databases my router says not to migrate. |
| vague_medium_001 | medium | django__django-15916 | django/django | modelform_factory ignores the formfield_callback defined on the form class I pass in. |
| vague_medium_002 | medium | matplotlib__matplotlib-25960 | matplotlib/matplotlib | Changing the spacing between subfigures has no effect. |
| vague_medium_003 | medium | pydata__xarray-3151 | pydata/xarray | combine_by_coords raises a ValueError when the datasets share a coordinate whose values are not sorted. |
| vague_medium_004 | medium | pydata__xarray-7229 | pydata/xarray | xr.where with keep_attrs=True messes up the attributes of the coordinates. |
| vague_medium_005 | medium | sympy__sympy-13551 | sympy/sympy | Product(n + 1/2**k, (k, 0, n - 1)) evaluates to the wrong result. |
| vague_medium_006 | medium | sympy__sympy-15599 | sympy/sympy | Mod(3*i, 2) for an integer symbol i isn't simplified. |
| vague_large_000 | large | django__django-11400 | django/django | The admin's related-field list filters show their choices in the wrong order. |
| vague_large_001 | large | django__django-12325 | django/django | A child model in multi-table inheritance fails with ImproperlyConfigured when it also has a second one-to-one field pointing at its parent. |
| vague_large_002 | large | django__django-15957 | django/django | Prefetching related objects with a sliced queryset raises an error. |
| vague_large_003 | large | pylint-dev__pylint-8898 | pylint-dev/pylint | Pylint crashes when a regular expression in bad-name-rgxs contains a comma. |
| vague_large_004 | large | pytest-dev__pytest-6197 | pytest-dev/pytest | Since pytest 5.2.3, a plain pytest run imports __init__.py files it never used to, and the run fails. |
| vague_large_005 | large | sphinx-doc__sphinx-8548 | sphinx-doc/sphinx | autodoc's inherited-members option doesn't pick up the docstrings of inherited attributes. |
| vague_large_006 | large | sphinx-doc__sphinx-9461 | sphinx-doc/sphinx | Sphinx doesn't document methods decorated with both classmethod and property. |

## Preparing the repositories

These are tiered-balanced instances, so preparing the repositories works as
it does there. `quoin bench tiered` fetches any missing pinned commit before
the pilot.

## Budgets

The per-session cap, wall clock and per-review cap are the tiered-balanced
figures. That suite's derivation (`suite-tiered-balanced-provenance.md`,
Budgets) is unchanged; only the task count goes from 40 to 20.

```
cap          = tiered-balanced cap                               = 35
worst case   = cap x 20 tasks x 2 arms                           = 1400
target       = ceil_to_10(worst case)                            = 1400
review cap   = 2 per review; review total = 2 x 20 tasks         = 40
ceiling      = ceil_to_10((worst case + review total) x 1.1)     = 1590   (1584 rounded)
pilot worst  = cap x 6 easy tasks x 2 arms                       = 420
wall clock   = tiered-balanced wall clock                        = 17400 s
```

| Figure | Value, per run |
|---|---|
| Per-session cap | $35 |
| Worst case (20 tasks, 2 arms) | $1,400 |
| Documented target | $1,400 |
| Enforced ceiling | $1,590 |
| Pairwise review cap per review | $2 |
| Pairwise review total cap | $40 |
| Pilot (easy tier) worst case | $420 |

The vague run and the ceiling run are separate runs, and each has its own
budget. The pilot rule is the tiered-balanced one: stop and raise the cap if
any pilot session costs more than half the cap or any pilot row is
`budget_stopped`.

Expected spend, for orientation and not a budget: about $120 per run, about
$240 for the vague and ceiling runs together. This scales the tiered-balanced
arm estimate ($239 for 40 tasks) to 20 tasks. Quoin sessions on `/run`'s
default r0 route have cost less since then; pro-r1 averaged $1.16 per quoin
session. So the lower bound is nearer $35 per run.

## Reproducing

Offline, from the repository root:

```bash
python3 quoin/benchmarks/scripts/pick_subset.py --suite verified-vague --seed 1729 \
  --verify quoin/benchmarks/suite-verified-vague.json
python3 quoin/benchmarks/scripts/pick_subset.py --suite verified-vague-ceiling --seed 1729 \
  --verify quoin/benchmarks/suite-verified-vague-ceiling.json
```

Each suite embeds the sha256 of the snapshot, the pool problems file and the
rewrites file. A successful verify therefore also shows that the vendored data
is unchanged.

## Contamination surface

These instances are public SWE-bench Verified tasks, as in the other Verified
suites. A model that has seen an upstream fix may recognise the symptom even
from the vague prompt. This affects both arms equally.

## Untrusted descriptions and agent network access

The rewrites are short and model-written, and the ceiling prompts are the
upstream issue text. Both are untrusted input. Network access is the same as
on the other Verified suites: only the web tools and a few Bash prefixes are
denied in both arms.

## Statistical power

Twenty pairs can detect only a large difference. A difference of one or two
tasks is inside the noise. The report gives Wilson intervals and a sign test
for this reason. The README's decision rule keeps prompt triage on one clear
run but decides against it only on the pooled rows of a repeat (`--repeat 2`).
