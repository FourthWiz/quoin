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

### Trusted approval mechanism

Status: TODO
Owner: maintainer
Blocks: integrated writes
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

### Metadata-only event logging for work profiles

Status: decided
Decision: under a work classification Quoin's event sidecar, run record and checkpoint store the
length and a 12-hex digest of redacted text in place of text parts, reasoning, tool titles, tool
errors, provider error messages and approval patterns. The string form sits inside the existing
fields so the event schema does not change.
Rationale: a work profile's default logs must not hold prompts, retrieved messages or tool output.
The digest covers the redacted text and is not a secrecy control; a short or guessable text can be
confirmed offline.
Scope: OpenCode's own log and session database are not controlled by this setting.

### Classification-keyed defaults

Status: decided
Decision: a profile's classification (work or personal) chooses the defaults for logging
content, retention days, identity check, web tools, package installs and which inventory
entries are refused. The work values are strict; the personal values record and allow more.
An explicit profile or trusted managed policy value overrides a default; a project file can
only tighten.
Rationale: one switch keeps a work profile safe without per-key configuration, and a personal
profile keeps working without work-grade refusals.
Scope: defaults are local to this adapter; the maintainer retention and egress decisions
below are unchanged.

### Executable-fetch prevention

Status: decided
Decision: a work profile runs offline for package installs and the launcher refuses a launch
whose configuration would make OpenCode fetch code (a plugin package, a dependency install
for an owned directory) when the network policy cannot rule the fetch out, using
`executable-fetch-detected` and `executable-fetch-possible`.
Rationale: fetched code runs with the child's access, and a network policy alone cannot tell
which request carries code.
Scope: detection reads configuration and the offline policy; it does not observe what the
child fetches afterwards.

### Configuration inventory allowlist

Status: decided
Decision: before every launch the driver reads every configuration source OpenCode would read
and admits an entry only when it is on a short allowlist (declared providers and models,
allow-listed MCP servers defined in a file the user controls, owned agents and commands).
Anything that could change which models run, add code or widen permissions is refused with an
`inventory-*` code under a work profile and recorded under a personal one.
Rationale: later configuration layers can override compiled keys, so the launcher verifies the
merged inventory rather than trusting its own output.
Scope: the inventory covers what OpenCode reads at start; it does not cover files a tool writes
later.

### Retention sweep scope and evidence

Status: decided
Decision: a sweep at launch removes aged units per class, never the session database or
credentials, and keeps units that an unfinished task, a live lock, an active run or an unreadable
store may still need. A task is finished only after its folder moved under a finalized directory.
Rationale: the workflow state cannot tell a finished multi-stage task from one paused between
stages, so keeping more is the safe direction. Cache is removed as whole package units because a
partly removed package can break a session that offline policy cannot repair.
Scope: the 30-day work values are local defaults; the maintainer retention decision below is
unchanged. The worst-case walk is bounded by per-class budgets of 50000 entries plus three per
indexed project; a unit too large to measure within its budget is kept and needs a purge.

### Purge scope and kept credentials

Status: decided
Decision: purge removes every top-level entry of a profile directory except the stamp, the project
index and the credential files, plus the profile's run sidecars in every project it touched, and
needs an explicit confirmation flag.
Rationale: the session database cannot be recovered, so an accidental re-run must not delete. The
npm registry file can hold tokens and the MCP auth file is uncited, so both are kept.
Scope: purge refuses while a run or lock of the profile is active; an open interactive session is
not detected.

### Read-only integration broker

Status: decided
Decision: Jira, Slack and mail context reaches the agent only through `workctl`, a read-only broker
with one normalized operation table and one authorization policy that every transport calls. Its
configuration lives at a fixed path under the account's real home and holds keychain references
under the `quoin-workctl` service prefix only; it never reads credentials, configuration paths,
proxies or CA bundles from the caller's environment. Requests are pinned to the configured service
origin. The generated `quoin-*-context` skills teach the syntax and carry no credentials, and no
MCP server is enabled; `quoin.workctl.mcp` is an evaluation stub that routes through the same
broker entry point.
Rationale: the agent is untrusted once it reads retrieved text, so neither a flag, an environment
variable nor a cursor may decide which secret is read or where it is sent. Sharing one entry point
keeps a future MCP transport from bypassing validation or the read-only policy.
Scope: backends were exercised only against synthetic recorded exchanges; no tenant, client or
mail backend has been chosen (see the maintainer decisions above), so no live read has run. The
keychain backend is macOS-only, so Linux use needs the command backend with an organization CLI
that keeps its own credentials. The broker runs with the caller's Python environment, so a work
deployment should launch it with an isolated interpreter (`python -I -m quoin.workctl`).

### Approved-write path

Status: decided
Decision: the agent can only prepare an immutable local proposal (`workctl propose`), whose
SHA-256 hash covers the operation, binding, exact target, full content, expiry and idempotency key.
A person approves it with `workctl-write approve` at a terminal outside the agent's session, which
signs the hash with an approval key into a single-use, expiring approval. `workctl-write execute`
revalidates the proposal, the approval and the configured write policy, records its intent in a
private journal before sending, sends once with a write credential and records the receipt. After
a timeout, server error or crash it looks the write up by its idempotency marker and otherwise
reports `OUTCOME_UNKNOWN`; it never resends an unresolved write. Writes stay disabled unless the
configuration declares a trusted approval boundary and an approval key.
Rationale: an approve flag the model can pass is not authorization, and a resent email or a
duplicate post cannot be taken back. Signing the content hash makes any change to text or target
void the approval, and journaling intent first keeps a crash from turning into a blind resend.
Scope: approval keys and write credentials use their own keychain prefixes, which read credentials
may not use, but the approver and writer run as ordinary processes: keeping them out of the agent's
reach is the deployment's duty (a separate account, a managed container or a platform service),
which `approval_boundary` records and Quoin cannot verify. No trusted approval mechanism has been
chosen (see the maintainer decisions above), so the default outcome is local drafts submitted by
hand. Only Jira comments, Slack posts and Gmail sends are implemented, against synthetic exchanges;
attachments are refused, and the idempotency markers (Slack metadata, Jira comment property, Gmail
Message-ID) are unverified against live services. The CLI-versus-MCP comparison was not run and no MCP write tools exist.

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
  It is prevented only when isolation is verified and the managed boundary
  mounts the project read-only for the reviewer roles; otherwise it stays a
  residual.
- `.workspaces/` is outside the listing.
- Include directories given to the test command are shared with the real
  tree and are writable.
- There is no network isolation for snapshot or test runs of its own. Egress
  from them is denied only when isolation is verified and the managed boundary
  denies egress; otherwise it stays a residual.
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
  `--test-command` is the operator's opt-in. Confinement of writes and egress
  holds only when isolation is verified and the managed boundary mounts the
  project read-only and denies egress; otherwise the run stays a residual.
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
