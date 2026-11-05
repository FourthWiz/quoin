# OpenCode adapter: release acceptance, qualification, upgrade and rollback

This page decides which release shape the OpenCode adapter may claim, and how
to keep that claim true across upgrades. A row passes only on the evidence
named next to it. Generated documentation or a passing offline test is not live
runtime support.

## Release shapes

- **Personal pilot**: a personal OpenRouter (or other personal) model on
  synthetic or personal repositories. Writes to external services are disabled.
- **Read-only work**: the work profile on approved repositories behind the
  qualified corporate gateway, with Jira, Slack and mail read access only. This
  shape must describe itself as read-only; external writes are "not applicable,
  writes disabled".
- **Managed work**: read-only work plus approved external writes. It also needs
  the work-boundary rows (network separation, read-only reviewer, secret scans)
  to pass inside the actual supported execution environment, not only against
  mocked policy functions.

## Current status

Release shape claimed: **personal pilot, pending live qualification**. The
deterministic rows below pass in the test suite. The live rows have not been
run yet, so the work profile is not supported, and the personal pilot is not
claimed until its live row passes.

## Acceptance matrix

Status values: `pass` (evidence exists and passes), `pending` (evidence must
be collected live by a maintainer), `n/a` (not applicable to that shape, with
the reason). Verification pointers name test files under `quoin/dev/tests/`.

| ID | Criterion | Verification | Personal pilot | Read-only work | Managed work |
|---|---|---|---|---|---|
| AC01 | A clean fixture installs OpenCode assets twice without change; check mode writes nothing | `test_opencode_install.py` | pass | pass | pass |
| AC02 | Every core catalog entry has an explicit support status; normalized names are unique | `test_opencode_manifest.py`, `test_opencode_generate.py`; the manifest drift check runs in CI | pass | pass | pass |
| AC03 | Discover through gate creates valid existing-layout artifacts with current revision evidence | `test_opencode_workflow_e2e.py`, `test_quoin_run_opencode_e2e.py` | pass | pass | pass |
| AC04 | The corporate gateway model completes the qualified native tool loop and the scoped workflow thresholds | Live qualification (below); `test_probe_gateway_tool_loop.py`, `test_bench_opencode_cells.py` cover the tooling only | n/a: no corporate gateway in this shape | pending | pending |
| AC05 | A personal OpenRouter model runs the same workflow through configuration only | Live qualification (below); `test_opencode_compiler.py` covers the configuration path offline | pending | n/a: personal profile not used | n/a: personal profile not used |
| AC06 | Work requests never reach a personal provider, including auxiliary and child requests | `test_opencode_netpolicy.py`, `test_opencode_profile_policy.py`, `test_opencode_security_suite.py`; managed work also needs a controlled network capture in the real environment | n/a: no work profile | pass | pending: real environment |
| AC07 | The reviewer cannot modify source through editing tools, shell or subprocesses | `test_opencode_boundaries.py`; managed work also needs the boundary run in the real environment (the opt-in container test, `test_opencode_docker_boundary.py`, is not counted toward `pass`) | pass | pass | pending: real environment |
| AC08 | Jira, Slack and mail reads return bounded parseable data and source provenance | `test_workctl_core.py`, `test_workctl_backends.py`, `test_workctl_transport.py` (synthetic exchanges only) | n/a: no work integrations | pass | pass |
| AC09 | External writes require trusted approval of the exact operation; bypass and replay fail | `test_workctl_writes.py` (synthetic exchanges only) | n/a, writes disabled | n/a, writes disabled | pending: trusted approval boundary not chosen |
| AC10 | Killing a run preserves an honest interrupted state; resume does not duplicate effects | `test_opencode_driver_cancel.py`, `test_opencode_driver_resume.py`, `test_opencode_workflow_resume.py` | pass | pass | pass |
| AC11 | Claude and Codex install and workflow contracts remain valid | `test_validate_adapter_drift.py`, `test_codex_installable_feature.py`, `test_codex_runtime_smoke.py` | pass | pass | pass |
| AC12 | Uninstall and rollback preserve user edits and portable task artifacts | `test_opencode_install.py`, `test_opencode_purge.py`; rollback procedure below | pass | pass | pass |
| AC13 | Secret scans find no seeded credentials in artifacts, errors, logs or exported configuration | `test_opencode_secret_scan.py`, `test_opencode_security_secrets.py`; managed work also needs the scan in the real environment | pass | pass | pending: real environment |
| AC14 | Unsupported runtime, model or settings produce an actionable failure before use | `test_opencode_native_schema.py`, `test_opencode_runtime_config.py`, `test_opencode_doctor.py` | pass | pass | pass |
| AC15 | Cost data states unknown values explicitly and does not double-count parent and child usage | `test_opencode_cost.py`, `test_opencode_cost_ledger.py` | pass | pass | pass |

A shape is releasable when every row in its column is `pass` or `n/a`.
Today no column meets that bar: each still has at least one `pending` live row.

## Live qualification

A maintainer runs this with synthetic data only. No work repository, ticket,
message or credential belongs in a qualification run.

1. Pin the runtime: install OpenCode at the version in the compatibility
   matrix below and confirm `opencode --version` matches it.
2. Probe the endpoint: run `quoin opencode probe --profile NAME --synthetic-only`
   (or `python3 quoin/adapters/opencode/probe_gateway.py`) against the gateway or
   personal endpoint and keep the capability record. A failed tool loop here is
   a gateway or model problem; it is fixed at the gateway or by choosing another
   approved model, never by parsing tool calls out of chat text.
3. Compile the profile: `quoin opencode config compile --profile NAME` for the work profile
   (corporate gateway) or the personal profile (OpenRouter). The personal row
   must pass by changing configuration only, with no code or prompt edits.
4. Run the frozen task set with both benchmark cells on the same model and
   endpoint (see `quoin/benchmarks/README.md`, "OpenCode cells and pilot
   qualification"): 20 repetitions of the tool-call reliability scenario and 10
   scoped workflow tasks.
5. Evaluate: `python3 quoin/benchmarks/scripts/opencode_report.py rows.jsonl
   --invariants pass` after the full deterministic suite passes (it also
   accepts the run directory). The frozen thresholds are at least 19 of 20
   tool-loop runs in each cell, at least 8 of 10 scoped tasks in the Quoin
   cell, and every deterministic invariant passing. Each threshold judges the
   first runs up to its sample size; reruns are reported as surplus and never
   replace a failed run. The manual qualification job in the adapter CI
   workflow evaluates a committed results set with the invariants left
   pending, so a green run there is not a qualification pass on its own.
6. Record the result in "Qualification results" below: date, runtime version,
   provider route, model reference as configured, configuration digest, actual
   counts and every failure mode. Keep failed runs in the counts.

The thresholds and task set are frozen in
`quoin/benchmarks/opencode-qualification.json`; changing either needs
maintainer approval. Report task quality and overhead separately, and do not
claim that Quoin improves model quality without a paired measurement on the
same model and endpoint.

### Requalification

Rerun the live qualification, and update the results below, whenever any of
these changes: the OpenCode version, the model, the provider route or gateway,
the endpoint's API family, the declared context or output limits, or the
compiled profile in a way that changes providers, tools or permissions. Until
it passes again, the affected column reverts to `pending`.

## Qualification results

| Profile | Date | Runtime | Route | Tool loop | Scoped tasks | Invariants | Failure modes |
|---|---|---|---|---|---|---|---|
| Work (corporate gateway) | not run | n/a | n/a | pending | pending | pending | n/a |
| Personal (OpenRouter) | not run | n/a | n/a | pending | pending | pending | n/a |

## Compatibility matrix

| Component | Qualified version | Source of truth |
|---|---|---|
| OpenCode | 1.18.32 | `qualified-versions.json` |
| Quoin lifecycle plugin | 0.2.0 | `plugin/quoin-opencode-plugin.js` |
| Python | 3.10 or newer | `pyproject.toml` |
| Native configuration schema | one subset per qualified release | `schemas/opencode-<version>-config.subset.schema.json` |

The OpenCode row lists every qualified release; its lowest entry is the floor.
`qualified-versions.json` is the only place the list is kept; every other
place that names an OpenCode release reads it or is checked against it by
`test_opencode_versions.py`.

### Version policy

| Runtime release | Personal profile | Work profile |
|---|---|---|
| Older than the floor | refused | refused |
| A qualified release | launches | launches |
| Newer than the highest qualified release | launches with a warning | refused |
| Between two qualified releases, not itself qualified | launches with a warning | refused |

A warned launch prints the warning, `quoin doctor --runtime opencode` reports
the release as newer than qualified, and the run record stores the runtime
version with `unqualified_runtime: true`. It runs against the native schema
subset of the nearest lower qualified release, and the lifecycle plugin loads
with a warning; the doctor shows the release the plugin was tested with next to
the runtime's. Work profiles accept only qualified releases because their
isolation rests on verified configuration and plugin loading behaviour, which an
upstream release can change silently.

## Upgrade qualification

1. Run the **OpenCode qualification** workflow (`.github/workflows/opencode-qualification.yml`,
   manual only) with the release to qualify, or leave the input empty for the
   latest. It installs that release, runs the binary-tier tests against it and,
   when they pass, opens a pull request that adds the release to
   `qualified-versions.json`. Locally, the same check is
   `QUOIN_OPENCODE_QUALIFY_VERSION=X.Y.Z python3 -m pytest quoin/dev/tests/test_opencode_*binary*.py -rs`
   with that binary first on PATH. The workflow proposes a release only when
   every binary-tier test passes; a pull request it opens does not start the
   other workflows, so rerun the adapter checks on it before merging.
2. Before merging that pull request, read the upstream release notes and
   recheck every row in `compatibility.md` against the new tag; update rows that
   changed, and the `## Qualified releases` block there.
3. When the native configuration changed, add a schema subset named
   `schemas/opencode-<version>-config.subset.schema.json`; otherwise copy the
   previous subset under the new name. A release without its own subset cannot
   be qualified.
4. When the reference release (the highest entry) changes, move
   `feature-manifest.json`'s `opencode_version` with it, and rebuild and
   requalify the lifecycle plugin against the new binary
   (`PLUGIN_OPENCODE_VERSION` in `src/quoin/opencode_adapter/plugin_artifact.py`
   and the build project under `quoin/dev/opencode-plugin/`). Gateway
   qualification records are measured against the reference release, so a new
   reference release leaves every model unqualified until
   `quoin opencode probe` runs again for each profile. Then run the
   deterministic suite (`python3 -m pytest -n auto`) and
   `quoin doctor --runtime opencode`.
5. Rerun the live qualification for every profile in use.
6. Ship the new runtime, compiled configuration, generated scaffold and plugin
   as one bundle. Keep the previous bundle so rollback can restore it.

Removing a release from the list is the way to retire it; removing the lowest
entry raises the floor.

## Rollback

- **Select the previous adapter.** Continue the task with the Claude or Codex
  runtime (for example `quoin run --runtime claude`, or the previous adapter's
  own commands), starting from the last validated checkpoint or portable
  handoff (check it first with `quoin opencode handoff show --task NAME`, which
  validates the record against the tree and prints the next step). Task artifacts
  stay in the shared layout, so nothing has to be converted.
- **Restore the runtime and configuration as a unit.** When an upgrade fails,
  reinstall the previous OpenCode version together with its compiled profile,
  generated scaffold (`quoin install --runtime opencode` from the previous
  Quoin release) and plugin. Never mix a new binary with an old configuration
  or the reverse.
- **Leave incompatible native sessions alone.** Do not edit, migrate or delete
  OpenCode's own session data. Start a new runtime session from the portable
  handoff instead.
- `quoin opencode uninstall` removes only files Quoin owns and keeps user
  edits; portable task artifacts are never touched.
