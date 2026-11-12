# Suite cl-sequences Provenance

## Datasets

The tasks are SWE-bench Verified instances, arranged in the per-repository
sequences of SWE-Bench-CL (Joshi et al., "Agents Never Forget", arXiv
2507.00014). Two vendored sources are pinned.

| Item | Value |
|---|---|
| Sequence source | `https://github.com/thomasjoshi/agents-never-forget` (MIT licence) |
| Pinned commit | `74a38a90baace25635f3827ee2f98caff24b3768` |
| Source file | `data/SWE-Bench-CL-Curriculum.json`, sha256 `91bc39a769b6218419bd44308650e5d2c846ecd3e6f7a6c086f74a37b6db90f7`, 5,862,595 bytes (fetched 2026-11-12) |
| Vendored CL snapshot | `quoin/benchmarks/data/swebench-cl-snapshot.json`, sha256 `a14d42085e36b92bc21817ed0abd4fff491fa9e9953aec00235975a78f149cd0` |
| Task dataset | `princeton-nlp/SWE-bench_Verified`, split `test` |
| Pinned revision | `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` |
| Parquet sha256 | `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd` (re-downloaded at the pinned revision on 2026-11-12; equals the snapshot's `source_file_sha256`) |
| Vendored Verified snapshot | `quoin/benchmarks/data/swebench-verified-snapshot.json`, sha256 `8912b2480c5680497e34294a0ffcb2544aa39a6994b38cc5d403df48b26376b0` (shared with the tiered suites, unchanged) |
| Vendored problem statements | `quoin/benchmarks/data/swebench-verified-problems-cl-sequences.json`, sha256 `3d83825c1651c02f94223e4a377d4659fe20eccb4cf1b5b6a38697c4be0f101e` |

The CL snapshot keeps, for all eight upstream sequences (273 tasks), the
upstream order and per task the instance id, base commit, creation time,
difficulty label, upstream position and the files the reference fix modifies.
Problem statements, patches and tests are not copied from it.

All 273 CL instances are in the vendored Verified snapshot with the same repo,
base commit and difficulty label, and their CL problem statements equal the
Verified ones byte for byte, so the Verified snapshot needed no extension.
The suite generator re-checks repo, base commit and label for every selected
task. Repo preparation, the prompt template (`verified-v1`) and the judge
(including `--judge-emulation`) are the Verified ones, unchanged.

## Subset selection

- Tool: `quoin/benchmarks/scripts/pick_subset.py --suite cl-sequences`
- Seed: 1729 with sub-seed offsets django 201, sympy 202, sphinx 203
- Sequences: `django_django_sequence`, `sympy_sympy_sequence` and
  `sphinx-doc_sphinx_sequence`, the three longest
- Upstream orders each sequence by difficulty band, then by creation date, so
  only its head is strictly chronological. Per sequence: take the leading run
  of tasks whose `created_at` never decreases (django 50, sympy 25, sphinx 22
  tasks), draw a start with `random.Random(seed + offset).randrange(0, head - n + 1)`
  (django 15, sympy 12, sphinx 5, zero-based), and keep the 12 consecutive
  tasks from there in upstream order
- Task ids are `cl_<sequence>_NN`, NN the zero-based position in the window;
  each task records `sequence_id`, `sequence_position`, `sequence_length`, the
  upstream `cl_position` and `created_at`
- Counts: 12 per sequence, 36 tasks; the suite sets `tiered.sequence_mode`
  and `tiered.pilot_tier` (django)

## Tasks

| Task ID | Sequence | Position | Upstream instance | Repo | CL position | Created | Difficulty label |
|---|---|---|---|---|---|---|---|
| cl_django_00 | django | 0 | django__django-11490 | django/django | 16 | 2019-06-19 | <15 min fix |
| cl_django_01 | django | 1 | django__django-11555 | django/django | 17 | 2019-07-10 | <15 min fix |
| cl_django_02 | django | 2 | django__django-11603 | django/django | 18 | 2019-07-28 | <15 min fix |
| cl_django_03 | django | 3 | django__django-11820 | django/django | 19 | 2019-09-26 | <15 min fix |
| cl_django_04 | django | 4 | django__django-11880 | django/django | 20 | 2019-10-07 | <15 min fix |
| cl_django_05 | django | 5 | django__django-11951 | django/django | 21 | 2019-10-21 | <15 min fix |
| cl_django_06 | django | 6 | django__django-11964 | django/django | 22 | 2019-10-23 | <15 min fix |
| cl_django_07 | django | 7 | django__django-12125 | django/django | 23 | 2019-11-22 | <15 min fix |
| cl_django_08 | django | 8 | django__django-12193 | django/django | 24 | 2019-12-08 | <15 min fix |
| cl_django_09 | django | 9 | django__django-12209 | django/django | 25 | 2019-12-12 | <15 min fix |
| cl_django_10 | django | 10 | django__django-12276 | django/django | 26 | 2020-01-05 | <15 min fix |
| cl_django_11 | django | 11 | django__django-12304 | django/django | 27 | 2020-01-09 | <15 min fix |
| cl_sympy_00 | sympy | 0 | sympy__sympy-17139 | sympy/sympy | 13 | 2019-07-01 | <15 min fix |
| cl_sympy_01 | sympy | 1 | sympy__sympy-17655 | sympy/sympy | 14 | 2019-09-25 | <15 min fix |
| cl_sympy_02 | sympy | 2 | sympy__sympy-18189 | sympy/sympy | 15 | 2019-12-31 | <15 min fix |
| cl_sympy_03 | sympy | 3 | sympy__sympy-18763 | sympy/sympy | 16 | 2020-03-03 | <15 min fix |
| cl_sympy_04 | sympy | 4 | sympy__sympy-19495 | sympy/sympy | 17 | 2020-06-05 | <15 min fix |
| cl_sympy_05 | sympy | 5 | sympy__sympy-19637 | sympy/sympy | 18 | 2020-06-24 | <15 min fix |
| cl_sympy_06 | sympy | 6 | sympy__sympy-19954 | sympy/sympy | 19 | 2020-08-12 | <15 min fix |
| cl_sympy_07 | sympy | 7 | sympy__sympy-20916 | sympy/sympy | 20 | 2021-02-06 | <15 min fix |
| cl_sympy_08 | sympy | 8 | sympy__sympy-21847 | sympy/sympy | 21 | 2021-08-10 | <15 min fix |
| cl_sympy_09 | sympy | 9 | sympy__sympy-22714 | sympy/sympy | 22 | 2021-12-19 | <15 min fix |
| cl_sympy_10 | sympy | 10 | sympy__sympy-23534 | sympy/sympy | 23 | 2022-05-22 | <15 min fix |
| cl_sympy_11 | sympy | 11 | sympy__sympy-24539 | sympy/sympy | 24 | 2023-01-17 | <15 min fix |
| cl_sphinx_00 | sphinx | 0 | sphinx-doc__sphinx-8269 | sphinx-doc/sphinx | 6 | 2020-10-03 | <15 min fix |
| cl_sphinx_01 | sphinx | 1 | sphinx-doc__sphinx-8459 | sphinx-doc/sphinx | 7 | 2020-11-20 | <15 min fix |
| cl_sphinx_02 | sphinx | 2 | sphinx-doc__sphinx-8475 | sphinx-doc/sphinx | 8 | 2020-11-22 | <15 min fix |
| cl_sphinx_03 | sphinx | 3 | sphinx-doc__sphinx-8595 | sphinx-doc/sphinx | 9 | 2020-12-27 | <15 min fix |
| cl_sphinx_04 | sphinx | 4 | sphinx-doc__sphinx-8621 | sphinx-doc/sphinx | 10 | 2020-12-30 | <15 min fix |
| cl_sphinx_05 | sphinx | 5 | sphinx-doc__sphinx-8721 | sphinx-doc/sphinx | 11 | 2021-01-21 | <15 min fix |
| cl_sphinx_06 | sphinx | 6 | sphinx-doc__sphinx-9230 | sphinx-doc/sphinx | 12 | 2021-05-15 | <15 min fix |
| cl_sphinx_07 | sphinx | 7 | sphinx-doc__sphinx-9258 | sphinx-doc/sphinx | 13 | 2021-05-21 | <15 min fix |
| cl_sphinx_08 | sphinx | 8 | sphinx-doc__sphinx-9281 | sphinx-doc/sphinx | 14 | 2021-05-29 | <15 min fix |
| cl_sphinx_09 | sphinx | 9 | sphinx-doc__sphinx-9320 | sphinx-doc/sphinx | 15 | 2021-06-11 | <15 min fix |
| cl_sphinx_10 | sphinx | 10 | sphinx-doc__sphinx-9367 | sphinx-doc/sphinx | 16 | 2021-06-20 | <15 min fix |
| cl_sphinx_11 | sphinx | 11 | sphinx-doc__sphinx-9591 | sphinx-doc/sphinx | 17 | 2021-08-29 | <15 min fix |

## Sequence mode

The tiers are the sequences, in suite order, and the paired runner runs tasks
in suite order, plain arm then quoin arm per task, one at a time. Each task
gets a fresh checkout at its own base commit. For the quoin arm on the memory
route only, the
`.workflow_artifacts/memory/` the previous task of the same sequence ended
with is copied into the new checkout before the session starts (lessons
learned, discovery files, daily insights). Session state under `memory/`
(`sessions/`, `checkpoints/`, `workspaces/`, `telemetry/`, run-state,
autonomous, auto-resume, supervisor, continuation, run-notes, 1m-tier,
needs-decision and worktree-probe records) is not carried, nor is anything outside
`memory/`: no code, branches, `cache/`, session or the arm's auto memory. The
plain arm is stateless, as on the tiered suites.

After each quoin task the runner snapshots the memory the session ended with
into the row's own directory (`memory-snapshot/`) and writes
`memory-carry.json`: which earlier task it inherited from, the inherited
files with sizes and hashes, the lessons inherited and written. A resumed run
seeds the next task from the nearest earlier snapshot of the same sequence in
that run, so it sees what a single pass would have shown it.

Arms: plain; quoin with memory (`QUOIN_BENCH_QUOIN_ROUTE=memory`, which sets
`QUOIN_RUN_PROFILE=bench-memory`: `/run` skips only run isolation and keeps
the lessons read and append, discovery and the dirty marker); and, as the
ablation, quoin lean (`QUOIN_BENCH_QUOIN_ROUTE=lean`) as a separate run id
over the same suite. Memory carries only on the memory route: lean tasks each
start from an empty `.workflow_artifacts/`, as on the tiered suites.

## Budgets

Per-session figures reuse the tiered-balanced derivation: the windows sit in
the easy and medium labels, which that cap already covers.

```
cap          = 35 (tiered-balanced)
worst case   = cap x 36 tasks x 2 arms                           = 2520
target       = ceil_to_10(worst case)                            = 2520
ceiling      = ceil_to_10(worst case x 1.1)                      = 2780   (2772 rounded)
pilot worst  = cap x 12 django tasks x 2 arms                    = 840
```

| Figure | Value |
|---|---|
| Per-session cap | $35 |
| Worst case per run id (36 tasks, 2 arms) | $2,520 |
| Documented target | $2,520 |
| Enforced ceiling | $2,780 |
| Pilot (django sequence) worst case | $840 |

The wall clock is **17,400 s** for every sequence and both arms.

Expected spend, for orientation and not a budget, from the tiered-balanced
observed easy and medium sessions (quoin mean about $3.80, plain about $0.25):
about $160 for the memory run (including one discovery per sequence) and
about $145 for the lean run.

## Reproducing

Offline, from the repository root:

```bash
python3 quoin/benchmarks/scripts/pick_subset.py --suite cl-sequences --seed 1729 \
  --verify quoin/benchmarks/suite-cl-sequences.json
```

To rebuild the CL snapshot from the pinned upstream file, then this suite's
problem statements from the pinned Verified parquet file:

```bash
git clone https://github.com/thomasjoshi/agents-never-forget "$S/cl" && \
  git -C "$S/cl" checkout 74a38a90baace25635f3827ee2f98caff24b3768
python3 quoin/benchmarks/scripts/pick_subset.py --refresh-cl-snapshot \
  --cl-file "$S/cl/data/SWE-Bench-CL-Curriculum.json" \
  --revision 74a38a90baace25635f3827ee2f98caff24b3768 --fetched-on 2026-11-12
python3 quoin/benchmarks/scripts/pick_subset.py --refresh-verified-snapshot --problems-only \
  --suite cl-sequences --parquet-file "$S/swebv-dl/test-00000-of-00001.parquet" \
  --revision c104f840cc67f8b6eec6f759ebc8b2693d585d4a
```

The Verified download commands are in `suite-tiered-small-provenance.md`.

## Preparing the repositories

```bash
python3 quoin/benchmarks/scripts/run_benchmark.py --suite cl-sequences \
  --cells simple-claude,quoin-claude --prepare-repos
```

## Contamination surface

As for the tiered suites: the data is public, models were likely trained on
these issues and repositories, and each gold fix is in the upstream history
after its base commit. Within a sequence, a later task's base commit usually
contains the earlier tasks' merged fixes, which is the setting SWE-Bench-CL intends.
Read results as relative comparisons between arms on the same tasks.
