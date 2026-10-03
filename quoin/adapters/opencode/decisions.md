# OpenCode maintainer decisions

These are decisions only the maintainer can make. Nothing in this document is a
guessed value — every `Value:` below is `not set` until a maintainer sets it.

## Maintainer decisions

### Gateway API family

Status: TODO
Owner: maintainer
Blocks: provider configuration compilation (chat-completions vs Responses API package choice)
Value: not set

### Gateway base URL

Status: TODO
Owner: maintainer
Blocks: provider configuration compilation
Value: not set

### Authentication method

Status: TODO
Owner: maintainer
Blocks: credential wiring
Value: not set

### TLS and proxy

Status: TODO
Owner: maintainer
Blocks: network configuration
Value: not set

### Model IDs

Status: TODO
Owner: maintainer
Blocks: model registration
Value: not set

### Rate limits

Status: TODO
Owner: maintainer
Blocks: retry and backoff tuning
Value: not set

### Context limits

Status: TODO
Owner: maintainer
Blocks: context budgeting
Value: not set

### OpenCode release and install channel

Status: TODO
Owner: maintainer
Blocks: install automation
Value: not set
Proposed: 1.18.32 (see compatibility.md)
Install channel: latest

### Execution isolation

Status: TODO
Owner: maintainer
Blocks: work-profile isolation
Value: not set

### Jira, Slack and mail clients

Status: TODO
Owner: maintainer
Blocks: integration wiring
Value: not set

### Tenants

Status: TODO
Owner: maintainer
Blocks: multi-tenant routing
Value: not set

### Mail backend

Status: TODO
Owner: maintainer
Blocks: mail delivery wiring
Value: not set

### Retention

Status: TODO
Owner: maintainer
Blocks: data retention policy
Value: not set

### Egress

Status: TODO
Owner: maintainer
Blocks: network egress policy
Value: not set

### Interactive-first or headless-first

Status: TODO
Owner: maintainer
Blocks: session mode selection
Value: not set

## Adapter design decisions

Decisions the adapter has already made. Unlike the maintainer decisions above, these
carry no `Value:` line: nothing here depends on a value only the maintainer can supply.

### Provider access in compiled configuration

Status: decided
Decision: the compiled configuration sets `enabled_providers` to exactly the providers in
use and pins each provider to its model ids with `whitelist`. It also emits
`experimental.policies` with a deny-all `provider.use` rule first and one allow rule per
provider, so the last matching rule wins.
Rationale: an unmatched provider is allowed by default. `enabled_providers` and
`whitelist` are enforced by the provider loader on the pinned run path, while policy
statements are evaluated only by the newer catalog service, so the policies are
supplementary and harmless rather than the enforced control.
Scope: this supplements credential and network isolation and never replaces them. Later
configuration layers can override the compiled keys, so the launcher verifies them at
launch.

### Headless phase runs first

Status: decided
Decision: the driver runs one workflow phase headlessly with `quoin run --runtime opencode --phase`; the terminal interface (`quoin opencode start`) is a minimal launcher that validates the profile and replaces the quoin process.
Rationale: a headless phase has a bounded input and an observable event stream, so state, resume and cancellation can be verified offline; an interactive session cannot be observed or resumed the same way.

### Whole-task runs on OpenCode

Status: deferred
Decision: `quoin run --runtime opencode` without `--phase` is refused.
Rationale: a whole-task run needs a coordinator command that sequences the phases; that belongs with the workflow-parity work, not the driver.

### Approval visibility for delegated work

Status: deferred
Decision: approval requests raised inside delegated (subagent) work are not visible to the driver, so such runs report partial evidence.
Rationale: seeing them needs a server or plugin event path, and plugins are refused because they could loosen permission handling.

### Installing OpenCode in CI

Status: deferred
Decision: binary contract tests stay opt-in and skip with a reason when no pinned executable is present.
Rationale: installing the pinned release in CI is a distribution choice for the maintainer, tracked with the release and install channel decision.

## Gateway probe results

Paste values from a probe run's `--output` record here; no probe has been run yet.

| Key field | Value |
|---|---|
| provider | |
| model_id | |
| endpoint | |
| runtime.name | |
| runtime.version | |
| probe_date | |

| Capability | status | source | value | detail |
|---|---|---|---|---|
| text_generation | | | | |
| tool_calls | | | | |
| streamed_tool_arguments | | | | |
| structured_output | | | | |
| context_limit | | | | |
| output_limit | | | | |
| reasoning_parameters | | | | |
| parallel_tool_calls | | | | |
| usage_reporting | | | | |

| Verdict field | Value |
|---|---|
| status | |
| summary | |
| blocking_step | |

## Plugin need

Intentionally empty — filled only when a concrete event or diagnostic that the CLI path cannot provide has been measured.

## Workflow parity residuals

Design limits of role boundaries, snapshot runs and test runs that are accepted
rather than open questions:

- A write through an absolute path is detected after the run, not prevented.
- `.workspaces/` is outside the listing.
- Include directories given to the test command are shared with the real
  tree and are writable.
- There is no network isolation for snapshot or test runs.
- Run-store files written during a run (current and prior run records, the
  pointer, the lock and result files) are excluded by exact path and trusted to
  their schema-checked loaders; the boundary check does not re-verify their
  content against the last write. A forged record can alter only that run's own
  telemetry, because the driver's final write replaces it and the ledger is
  checked by its own mark.
- An interrupted snapshot run is restarted fresh, never resumed.
- The generated `.opencode/` surface is verified by owned-file checks and the
  scoped listing, not by the source digest, and unowned files there are not
  listed.
- Memory files outside continuation, runtime and lessons-learned are not
  listed.
- A human editing another task's folder during a run is still a violation when
  the other task's lock is not live and the run is a snapshot run.
- A source state over the digest budget blocks headless review.
- In the TUI a user can approve an `external_directory` write into the
  test-result directory.
- Review entries carry no test result until the coordinator exists.
- A test command that needs a proxy variable must set it itself under the
  launcher.
- A test result from an attempt other than the run's last is not copied, so a
  relaunched implement run must run its tests again.
- A passing test result attests only to what the code in the copied tree
  reported: test-run runs implementer-written code unprompted with the user's
  privileges. An OS sandbox that confines writes to the workspace, or an opt-in
  allow granted by the configuration call, is the follow-up.
- The test workspace build is not capped: the binary diff is held in memory and
  untracked files are copied without a size limit, outside the test timeout.
- The snapshot copier checks the final path component for links only, and the
  snapshot git deadline is checked after a read returns, not while a silent
  command runs. Neither is reachable from a production caller yet.
- The overlay residue test checks "MUST run finalization" rather than "run
  finalization", because the replacement text "Never run finalization." contains
  the shorter phrase. The overlay-rewrite phase list stays hand-maintained
  beside the feature manifest.
- The gate role's own edits to the workflow record are denied in its permission
  map; the gate helper still writes the record from its own process, and the
  boundary check still treats that file as the gate's to change.
