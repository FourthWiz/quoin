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

Status: decided
Decision: a whole task runs through the headless coordinator, `quoin run --runtime opencode --workflow`, which sequences the phases and gates each one; the bare form without `--phase` or `--workflow` is still refused.
Rationale: sequencing, gating and recovery belong in one coordinator that holds the task lock, not in the single-phase driver.

### Approval visibility for delegated work

Status: deferred
Decision: approval requests raised inside delegated (subagent) work are not visible to the driver, so such runs report partial evidence.
Rationale: seeing them needs a server or plugin event path, and plugins are refused because they could loosen permission handling.

### Installing OpenCode in CI

Status: deferred
Decision: binary contract tests stay opt-in and skip with a reason when no pinned executable is present.
Rationale: installing the pinned release in CI is a distribution choice for the maintainer, tracked with the release and install channel decision.

### Isolation attestation and trust rule

Status: decided
Decision: managed isolation is attested by one root-owned JSON file at a fixed path beside
the trusted managed policy. It counts only when the file and every directory above it are
root-owned and not group or world writable, the invoking user is not root, the declaration
is current and complete, an independent corroboration agrees with the declared
environment, and no other profile is visible to this one.
Rationale: a profile, a project file or an environment variable that could supply the
declaration could attest itself. Every probe turns an operating-system failure into a
missing-evidence token, so a launch never crashes inside the check.
Scope: the declaration describes a setup and does not prove enforcement; wording stays
"attested". Whether an administrator may trust a docker-group writer of the file is left
to the deployment.

### Enforcement layers without a launcher-owned proxy

Status: decided
Decision: Quoin enforces destination approval at configuration and launch time (host
approval, proxy route, remote MCP hosts, web tools, registry) and a launch-time identity
check. It provides no launcher proxy and no brokered registry; shell egress, redirects and
DNS after launch are delegated to the managed boundary and recorded as such.
Rationale: a proxy that sits between the child and the network would be a second security
boundary to build and attest. Recording each control with its layer and status keeps the
record from claiming more than was checked.
Scope: a control that could not run is recorded as not enforced.

### Launch-time destination identity check

Status: decided
Decision: the check connects to each provider endpoint, verifies the approved host name
against the configured CA, optionally pins the leaf certificate, and refuses a mismatch.
Observations are stored beside the effective-configuration record, not inside its digest.
Rationale: certificates rotate and DNS answers change, so inside the digest they would make
every comparison unstable and make the offline preview disagree with the launch.
Scope: through a proxy the name is checked end to end but address ranges are not; whether
the child honours the proxy stays unverified.

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
- A review entry carries a test result only when the operator configured a
  test command; without one the gate reports `tests-not-configured` as a
  warning.
- A test command that needs a proxy variable must set it itself under the
  launcher.
- A test result from an attempt other than the run's last is not copied; the
  coordinator runs the tests again after every implement run, so a relaunched
  run is covered, but a single-phase run still has to run them itself.
- A passing test result attests only to what the code in the copied tree
  reported: test-run runs implementer-written code unprompted with the user's
  privileges. The coordinator runs it after every implement, and
  `--test-command` is the operator's opt-in. An OS sandbox that confines writes
  to the workspace is the follow-up.
- The test workspace build is not capped: the binary diff is held in memory and
  untracked files are copied without a size limit, outside the test timeout.
- The overlay residue test checks "MUST run finalization" rather than "run
  finalization", because the replacement text "Never run finalization." contains
  the shorter phrase. The overlay-rewrite phase list stays hand-maintained
  beside the feature manifest.
- The gate role's own edits to the workflow record are denied in its permission
  map; the gate helper still writes the record from its own process, and the
  boundary check still treats that file as the gate's to change.
- Files over the hash cap, or past the total hash budget, are compared by size
  and mtime only, and code started by test-run can reset an mtime. The limit
  sits beside the other test-run residuals; comparing ctime as well is a
  possible tightening.
- Test settings hand-edited in the workflow record always refuse, by design:
  the digest is pinned under the driver's own state root, outside the project,
  and a run whose settings no longer match it is refused as
  `tests-settings-changed`.
- The printed resume hint carries the non-interactive flag while the stored
  hint does not, so advice built from the stored hint still suggests an
  interactive restart. This is deliberate for now.
- The window store that lets a resumed run keep its original boundary window
  lives under the driver's state directory and is trusted: a forged window file
  could hide a change made before the interruption, but cannot reach files in
  the project, and the workflow state file is baselined at resume because the
  coordinator rewrites it between attempts.
- The continuation record may lag the workflow state by recorded steps (a
  crash between an entry write and the record refresh); the coordinator accepts
  such a record when every hash in it appears in state's history and rewrites
  it. A record ahead of state still refuses.
- Output a child writes after the exit drain window (one second once the
  process has ended) is dropped from the output hash and tail.
