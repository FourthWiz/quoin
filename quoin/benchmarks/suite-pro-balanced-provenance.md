# Suite pro-balanced Provenance

## Dataset

| Item | Value |
|---|---|
| Dataset | `ScaleAI/SWE-bench_Pro`, config `default` (V2, 2026-09-22), split `test` |
| Pinned revision | `2d52cb3df914a3fcf80c7f66738b3a88ae37fc50` |
| Parquet file | `data/default/test-00000-of-00001.parquet`, sha256 `1067f72c1f43bbc578276888625db3b836cebb535546c9eaf824c5d5dd104a7f`, 6,511,689 bytes (fetched 2026-11-12) |
| Languages in V2 | go 256, python 237, js 145, ts 4 (642 tasks) |
| Vendored snapshot | `quoin/benchmarks/data/swebench-pro-snapshot.json`, sha256 `3e226bd4dcf0824c3bb368f1bdfe2daf4a76cea7752899a15251e25454559fbc` (237 Python rows: ids, repo, base commit, reference patch size, HARD flag) |
| Vendored agent text | `quoin/benchmarks/data/swebench-pro-problems.json`, sha256 `c9949d7247dc276c97b2a935bcb24bfc3c988788b7edf5f1993f396fee14c3b6` (problem statement, requirements and interface of the 40 selected tasks) |
| Verifier pins | `quoin/benchmarks/data/swebench-pro-verifiers.json`, sha256 `a66ac72ad07ccbd46fb37f6f9b914769ad1bf0755bd454ab5b1172e12c6338cb` |
| Official harness | `https://github.com/scaleapi/SWE-bench_Pro-os` at `66f92766bba642462d4bbe5479e83f91f9211862` (MIT licence) |
| Images | `ghcr.io/scaleapi/swe-bench_pro-v2:<instance_id>`, public, linux/amd64 only |

Python repositories: ansible/ansible 82, internetarchive/openlibrary 82, qutebrowser/qutebrowser 73.
Task content comes from those upstream projects (GPL-3.0 for ansible and
qutebrowser, AGPL-3.0 for openlibrary); only ids, commits, sizes and the
agent-visible text are vendored. Base commits are upstream commits, so the
repository mirrors come from GitHub as for Verified.

Hidden tests are never vendored. The verifiers file records, per selected
task, the sha256 of the five verifier files (`tests/test.sh`, `run_script.sh`,
`parser.py`, `config.json`, `test_patch.patch`) and of the reference patch
(`solution/gold_patch.diff`, used only by the smoke test) at the pinned
harness commit. All 240 values were checked against the harness's own
`v2/SHA256SUMS`. The judge fetches the files at judge time and refuses any
whose hash differs.

## Subset selection

- Tool: `quoin/benchmarks/scripts/pick_subset.py --suite pro-balanced`
- Python rows only. The request allowed other languages if they judge as
  easily; only a Python task was smoke-judged, so the suite stays Python.
- Tiers by reference patch size (changed `+`/`-` lines, headers excluded):
  order the 237 Python rows by (lines, instance id) and cut into thirds
  (small 22-49, medium 50-104, large 104-2028 lines; ties at a cut fall to the lower id)
- Seed 1729 with sub-seed offsets small 301, medium 302, large 303: per tier
  shuffle the sorted ids with `random.Random(seed + offset)`, take n, sort,
  number `pro_<tier>_NNN`
- Counts: 10 small, 15 medium, 15 large (40 tasks); pilot tier `small`

## Tasks

| Task ID | Tier | Upstream instance | Repo | Patch lines | Patch files |
|---|---|---|---|---|---|
| pro_small_000 | small | instance_ansible__ansible-11c1777d56664b1acb56b387a1ad6aeadef1391d-v0f01c69f1e2528b935359cfe578530722bca2c59 | ansible/ansible | 38 | 1 |
| pro_small_001 | small | instance_ansible__ansible-164881d871964aa64e0f911d03ae270acbad253c-v390e508d27db7a51eece36bb6d9698b63a5b638a | ansible/ansible | 38 | 3 |
| pro_small_002 | small | instance_internetarchive__openlibrary-3c48b4bb782189e0858e6c3fc7956046cf3e1cfb-v2d9a6c849c60ed19fd0858ce9e40b7cc8e097e59 | internetarchive/openlibrary | 27 | 1 |
| pro_small_003 | small | instance_internetarchive__openlibrary-6afdb09df692223c3a31df65cfa92f15e5614c01-v08d8e8889ec945ab821fb156c04c7d2e2810debb | internetarchive/openlibrary | 30 | 1 |
| pro_small_004 | small | instance_internetarchive__openlibrary-c8996ecc40803b9155935fd7ff3b8e7be6c1437c-ve8fc82d8aae8463b752a211156c5b7b59f349237 | internetarchive/openlibrary | 35 | 2 |
| pro_small_005 | small | instance_qutebrowser__qutebrowser-305e7c96d5e2fdb3b248b27dfb21042fb2b7e0b8-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | qutebrowser/qutebrowser | 28 | 3 |
| pro_small_006 | small | instance_qutebrowser__qutebrowser-70248f256f93ed9b1984494d0a1a919ddd774892-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | qutebrowser/qutebrowser | 37 | 2 |
| pro_small_007 | small | instance_qutebrowser__qutebrowser-8d05f0282a271bfd45e614238bd1b555c58b3fc1-v35616345bb8052ea303186706cec663146f0f184 | qutebrowser/qutebrowser | 31 | 1 |
| pro_small_008 | small | instance_qutebrowser__qutebrowser-c0be28ebee3e1837aaf3f30ec534ccd6d038f129-v9f8e9d96c85c85a605e382f1510bd08563afc566 | qutebrowser/qutebrowser | 32 | 1 |
| pro_small_009 | small | instance_qutebrowser__qutebrowser-ed19d7f58b2664bb310c7cb6b52c5b9a06ea60b2-v059c6fdc75567943479b23ebca7c07b5e9a7f34c | qutebrowser/qutebrowser | 34 | 4 |
| pro_medium_000 | medium | instance_ansible__ansible-29aea9ff3466e4cd2ed00524b9e56738d568ce8b-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5 | ansible/ansible | 61 | 3 |
| pro_medium_001 | medium | instance_ansible__ansible-379058e10f3dbc0fdcaf80394bd09b18927e7d33-v1055803c3a812189a1133297f7f5468579283f86 | ansible/ansible | 81 | 11 |
| pro_medium_002 | medium | instance_ansible__ansible-5f4e332e3762999d94af27746db29ff1729252c1-v0f01c69f1e2528b935359cfe578530722bca2c59 | ansible/ansible | 52 | 2 |
| pro_medium_003 | medium | instance_ansible__ansible-9759e0ca494de1fd5fc2df2c5d11c57adbe6007c-v1055803c3a812189a1133297f7f5468579283f86 | ansible/ansible | 102 | 6 |
| pro_medium_004 | medium | instance_ansible__ansible-be2c376ab87e3e872ca21697508f12c6909cf85a-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5 | ansible/ansible | 59 | 2 |
| pro_medium_005 | medium | instance_internetarchive__openlibrary-0d13e6b4bf80bced6c0946b969b9a1b6963f6bce-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c | internetarchive/openlibrary | 75 | 1 |
| pro_medium_006 | medium | instance_internetarchive__openlibrary-5069b09e5f64428dce59b33455c8bb17fe577070-v8717e18970bcdc4e0d2cea3b1527752b21e74866 | internetarchive/openlibrary | 54 | 6 |
| pro_medium_007 | medium | instance_internetarchive__openlibrary-5c6c22f3d2edf2f1b10f5dc335e32cb6a5f40341-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c | internetarchive/openlibrary | 82 | 2 |
| pro_medium_008 | medium | instance_internetarchive__openlibrary-f3b26c2c0721f8713353fe4b341230332e30008d-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4 | internetarchive/openlibrary | 51 | 2 |
| pro_medium_009 | medium | instance_internetarchive__openlibrary-fdbc0d8f418333c7e575c40b661b582c301ef7ac-v13642507b4fc1f8d234172bf8129942da2c2ca26 | internetarchive/openlibrary | 95 | 4 |
| pro_medium_010 | medium | instance_qutebrowser__qutebrowser-a84ecfb80a00f8ab7e341372560458e3f9cfffa2-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | qutebrowser/qutebrowser | 51 | 4 |
| pro_medium_011 | medium | instance_qutebrowser__qutebrowser-deeb15d6f009b3ca0c3bd503a7cef07462bd16b4-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d | qutebrowser/qutebrowser | 53 | 1 |
| pro_medium_012 | medium | instance_qutebrowser__qutebrowser-ec2dcfce9eee9f808efc17a1b99e227fc4421dea-v5149fcda2a9a6fe1d35dfed1bade1444a11ef271 | qutebrowser/qutebrowser | 96 | 4 |
| pro_medium_013 | medium | instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c | qutebrowser/qutebrowser | 64 | 3 |
| pro_medium_014 | medium | instance_qutebrowser__qutebrowser-ff1c025ad3210506fc76e1f604d8c8c27637d88e-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d | qutebrowser/qutebrowser | 95 | 4 |
| pro_large_000 | large | instance_ansible__ansible-7e1a347695c7987ae56ef1b6919156d9254010ad-v390e508d27db7a51eece36bb6d9698b63a5b638a | ansible/ansible | 328 | 1 |
| pro_large_001 | large | instance_ansible__ansible-8127abbc298cabf04aaa89a478fc5e5e3432a6fc-v30a923fb5c164d6cd18280c02422f75e611e8fb2 | ansible/ansible | 241 | 7 |
| pro_large_002 | large | instance_ansible__ansible-949c503f2ef4b2c5d668af0492a5c0db1ab86140-v0f01c69f1e2528b935359cfe578530722bca2c59 | ansible/ansible | 230 | 4 |
| pro_large_003 | large | instance_ansible__ansible-de5858f48dc9e1ce9117034e0d7e76806f420ca8-v1055803c3a812189a1133297f7f5468579283f86 | ansible/ansible | 254 | 4 |
| pro_large_004 | large | instance_internetarchive__openlibrary-111347e9583372e8ef91c82e0612ea437ae3a9c9-v2d9a6c849c60ed19fd0858ce9e40b7cc8e097e59 | internetarchive/openlibrary | 143 | 5 |
| pro_large_005 | large | instance_internetarchive__openlibrary-25858f9f0c165df25742acf8309ce909773f0cdd-v13642507b4fc1f8d234172bf8129942da2c2ca26 | internetarchive/openlibrary | 419 | 4 |
| pro_large_006 | large | instance_internetarchive__openlibrary-53e02a22972e9253aeded0e1981e6845e1e521fe-vfa6ff903cb27f336e17654595dd900fa943dcd91 | internetarchive/openlibrary | 403 | 6 |
| pro_large_007 | large | instance_internetarchive__openlibrary-6fdbbeee4c0a7e976ff3e46fb1d36f4eb110c428-v08d8e8889ec945ab821fb156c04c7d2e2810debb | internetarchive/openlibrary | 269 | 6 |
| pro_large_008 | large | instance_internetarchive__openlibrary-910b08570210509f3bcfebf35c093a48243fe754-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4 | internetarchive/openlibrary | 330 | 9 |
| pro_large_009 | large | instance_internetarchive__openlibrary-92db3454aeaa02f89b4cdbc3103f7e95c9759f92-v2c55207218fb8a0138425cbf7d9675272e240b90 | internetarchive/openlibrary | 703 | 11 |
| pro_large_010 | large | instance_internetarchive__openlibrary-9bdfd29fac883e77dcbc4208cab28c06fd963ab2-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c | internetarchive/openlibrary | 141 | 2 |
| pro_large_011 | large | instance_internetarchive__openlibrary-d109cc7e6e161170391f98f9a6fa1d02534c18e4-ve8c8d62a2b60610a3c4631f5f23ed866bada9818 | internetarchive/openlibrary | 629 | 10 |
| pro_large_012 | large | instance_internetarchive__openlibrary-e1e502986a3b003899a8347ac8a7ff7b08cbfc39-v08d8e8889ec945ab821fb156c04c7d2e2810debb | internetarchive/openlibrary | 123 | 6 |
| pro_large_013 | large | instance_qutebrowser__qutebrowser-de4a1c1a2839b5b49c3d4ce21d39de48d24e2091-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | qutebrowser/qutebrowser | 542 | 4 |
| pro_large_014 | large | instance_qutebrowser__qutebrowser-f631cd4422744160d9dcf7a0455da532ce973315-v35616345bb8052ea303186706cec663146f0f184 | qutebrowser/qutebrowser | 138 | 5 |

## What the agent sees

Scale's standard V2 instruction (`instruction.md` per task), template
`pro-v1` in `harness/pro_prompt.py`: the fixed preamble, the PR description
with the problem statement, `## Requirements` and `## New Interfaces`, and
the constraints line. The only change is the location (`/app` becomes the
current working directory), because our agent works in a host checkout. The
instance id, test names, fail-to-pass and pass-to-pass lists and test files
are never shown. Scale's own requirements text sometimes names test data
files, as it does in Scale's setup.

## Judging

`judge_swebench_pro` (harness/judge.py) runs the task's verifier unchanged in
its image: `docker run [--platform linux/amd64] -v tests:/tests:ro -v
model.patch:/tmp/model.patch:ro -v logs:/logs <image>`, applies the patch as
Scale's patch-replay re-grader does (`git apply`, then `git apply --3way`,
then `patch --fuzz=3`), and runs `bash /tests/test.sh`. Reward 1 is `pass`,
reward 0 is `fail` (both final); an empty patch is `fail` with no container;
a missing reward, docker error or timeout is a non-final `error`, retried by
`--judge-only`. Patch normalisation is the Verified one (UTF-8, trailing
newline). The images are amd64 only: on an arm64 host give
`--judge-emulation`. swebench is not needed for this suite.

Smoke, 2026-11-12, arm64 host under emulation: on
`instance_ansible__ansible-83fb24b9…` (not in the suite) the reference patch
scored 1 and an empty patch 0; on the smallest selected task the judge gave
`pass` for its reference patch (216 s with the image pull), `fail` for an
empty patch and `fail` for a harmless non-empty patch. Images are about
1.25 GB each; 40 can take 50 to 100 GB of Docker disk.

## Budgets

No Pro session cost has been observed, so the figures reuse tiered-balanced:

| Figure | Value |
|---|---|
| Per-session cap | $35 |
| Worst case (40 tasks, 2 arms) | $2,800 |
| Documented target | $2,800 |
| Enforced ceiling | $3,170 |
| Pairwise review cap per review | $2 |
| Pairwise review total cap | $80 |
| Pilot (small tier) worst case | $700 |

The wall clock is **17,400 s** for every tier and both arms. It is
derived from the cap (1.5 x cap / slowest observed spend rate), so on a larger
task the cap binds before the clock. The judge allows 6,000 s per verifier run
(Scale's 3,000 s, doubled for emulation).

Pilot rule, as for tiered-balanced: if any pilot session costs more than half
the cap or ends `budget_stopped`, stop, raise the cap and regenerate before
the full run.

Expected spend, for orientation and not a budget: Pro fixes are a median 69
changed lines against mostly single-file Verified fixes, so assume quoin
sessions near the tiered-balanced large-tier figures (about $6 to $11) and
plain sessions about $1: about $300 to $480 for a run.

## Reproducing

Offline, from the repository root:

```bash
python3 quoin/benchmarks/scripts/pick_subset.py --suite pro-balanced --seed 1729 \
  --verify quoin/benchmarks/suite-pro-balanced.json
```

To rebuild the vendored files: download the parquet file at the pinned
revision, check out the harness at the pinned commit with the selected
`v2/tasks/<id>/` directories, then:

```bash
curl -L -o "$S/pro.parquet" \
  https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/resolve/2d52cb3df914a3fcf80c7f66738b3a88ae37fc50/data/default/test-00000-of-00001.parquet
python3 quoin/benchmarks/scripts/pick_subset.py --refresh-pro-snapshot \
  --parquet-file "$S/pro.parquet" --revision 2d52cb3df914a3fcf80c7f66738b3a88ae37fc50 \
  --pro-os-dir "$S/SWE-bench_Pro-os" --pro-os-commit 66f92766bba642462d4bbe5479e83f91f9211862
```

## Contamination surface

The data and the fixing commits are public, and each fix is reachable in the
upstream history after the base commit (never from the prepared clone). Read
results as relative comparisons between arms on the same tasks.
