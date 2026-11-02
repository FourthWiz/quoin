# Managed work deployment for the OpenCode adapter

This document is for the administrator who sets up the machine or container an
OpenCode work profile runs in. It separates what the environment must provide
from what Quoin checks at launch. Quoin checks configuration and records what it
found; it does not sandbox the child process.

## Scope and status

- The work profile is not yet supported on an unmanaged machine. A work profile
  with `isolation: managed` refuses to launch until the attestation below is in
  place. With the default `convenience` isolation it launches, and the
  managed-boundary controls are recorded as `not-enforced`.
- Isolation is verified only by an attestation the user cannot edit: a
  root-owned declaration plus an independent corroboration that agrees with it.
  Without both, the isolation verdict is unverified and a work profile that
  requires managed isolation refuses to launch.
- An attestation describes how the environment was set up. It does not prove
  that the operating system enforces it, and Quoin words every such claim as
  "attested".
- Nothing here changes that: the work profile is not supported by this document.
  The corporate gateway is not qualified, and the maintainer actions at the end of this document are open.

## Environment duties

The administrator owns every item in this section. Quoin does not check them
beyond what "What Quoin verifies" lists.

### Filesystem mounts

- Mount the project read-write only for the implementer role. Reviewer and other
  read-only roles get the project read-only.
- Mount only the work profile's storage directory. The personal profile's
  directories must not be visible, because a visible opposite profile makes the
  isolation verdict unverified.
- Give the child no write access to anything outside the project and the work
  profile's storage directory.

### Secrets

- Inject only the work credential. No personal API key, keychain entry, cloud
  credential file or shell history may be readable from inside the boundary.
- Quoin resolves the work credential at launch and hands it to the child by
  environment variable only. It never writes the value to a record.
- If integrated writes are enabled, keep the `workctl` approval key
  (`quoin-workctl-approver*`) and write credentials (`quoin-workctl-writer*`)
  outside the boundary, and run `workctl-write approve` and `execute` there,
  as a separate account or outside the container. Inside the boundary the agent
  may only prepare proposals. Record the arrangement in the broker
  configuration's `writes.approval_boundary`; Quoin cannot verify it.

### Process environment

- Start the launcher from a clean environment. The launcher drops ambient
  OpenCode configuration variables and sets the hardening variables itself, but
  a parent that exports a proxy, a trust-store override or a credential variable
  widens what the child can reach.
- Do not export the personal profile's variables into the work environment.

### Egress

- Run a firewall or an egress proxy that allows only the approved gateway hosts.
  The rule must cover redirects and name resolution after launch; Quoin checks
  destinations once, at launch, and cannot see what the child connects to
  afterwards.
- Quoin treats every loopback destination as approved, so no loopback listener
  on the machine (a local relay, proxy or port forward) may forward to an
  unapproved destination. This is an environment duty, not a Quoin check.
- An identity check through a proxy that requires credentials is not supported:
  the check's CONNECT request carries no proxy credentials.

### Attestation

- Install the declaration as a root-owned file in the trusted policy directory
  (`/Library/Application Support/quoin/opencode` on macOS, `/etc/quoin/opencode`
  elsewhere), named `isolation-attestation.json`. The file and every directory
  above it must be root-owned and not group or world writable, and the invoking
  user must not be root.
- Provide one corroboration that matches the declared environment: a container
  marker or cgroup name, a hypervisor identity, or on macOS a root-owned managed
  preferences file for the user.
- Keep the declaration current. An expired or incomplete declaration is treated
  as missing.

### Read-only Quoin profile files

- Mount the Quoin profile and qualification files under
  `XDG_CONFIG_HOME/quoin/opencode` read-only. They are the source of approval
  (allowed hosts, repository roots, models), so a child that can write them can
  approve itself.

### Employer-managed OpenCode settings

- An employer-managed OpenCode configuration file or macOS managed preference
  that sets `permission` or `tools` makes every work launch refuse, because it
  can loosen what the compiled configuration denies. Express such policy through
  the Quoin trusted managed policy instead, which Quoin merges and verifies.

## What Quoin verifies

Each line names the refusal code a launch reports, or the doctor finding that
reports the same condition. `quoin opencode doctor` runs the same checks without
launching.

- Configuration inventory: every configuration source OpenCode would read is
  judged against an allowlist. Refusals are the `inventory-provider`,
  `inventory-mcp-local`, `inventory-mcp-remote`, `inventory-plugin`,
  `inventory-lsp`, `inventory-formatter` and related `inventory-*` codes;
  doctor reports `inventory-refused` and `inventory-clean`.
- Destinations: each provider endpoint must be under an approved host. Refusals
  are `destination-not-approved`, `proxy-route-not-approved`,
  `web-tools-denied` and `mcp-remote-denied`.
- Destination identity: when the identity check is required, the endpoint's
  certificate must match the approved name. Refusals are
  `destination-identity-mismatch`, `destination-unverifiable` and
  `destination-plaintext`.
- Attestation chain: the declaration, its ownership chain, its expiry, the
  corroboration and the absence of the opposite profile. A work profile that
  requires managed isolation refuses with `isolation-required-unverified`;
  doctor reports `isolation-verified` or `isolation-unverified`.
- Repositories: the project must sit under an approved repository root.
  Refusals are `repository-not-approved` and `approved-repositories-unset`.
- Storage layout and permissions: the profile directories must be private and
  owned by the user. Refusals are `profile-dir-unsafe`, `data-dir-unsafe` and
  `profile-stamp-mismatch`.
- Metadata logging: under a work classification Quoin's own run files hold
  lengths and digests instead of text. There is no refusal; the effective
  configuration record shows the logging mode, and `quoin opencode config
  explain` prints it.
- Retention and purge: a launch sweeps aged units and keeps evidence an
  unfinished task needs. `quoin opencode purge` refuses with `purge-run-active`,
  `purge-inside-launch`, `purge-profile-invalid`, `purge-profile-unsafe` or
  `purge-confirm-required`.

## Enforcement layers

The effective-configuration record lists each control with a layer and a status.
Statuses are `enforced` (Quoin refuses or checks it), `delegated-verified` and
`delegated-unverified` (a duty the managed boundary carries, with or without a
valid attestation), `not-enforced`, and `not-provided`.

| Control | Layer | Status |
|---|---|---|
| `provider-destinations` | Quoin configuration refusal | `enforced` for a work profile |
| `destination-identity` | Quoin launch identity check | `enforced` when the identity check is required |
| `proxy-route` | Quoin configuration refusal | `enforced` for a work profile |
| `web-tools` | Quoin configuration refusal | `enforced` when web tools are denied |
| `remote-mcp` | Quoin configuration refusal | `enforced` unless remote servers are allowed anywhere |
| `remote-config-and-instructions` | Quoin configuration refusal | `enforced` for a work profile |
| `launcher-proxy` | launcher proxy | `not-provided` |
| `brokered-registry` | launcher proxy | `not-provided` |
| `shell-egress`, `redirects`, `dns-after-launch` | managed boundary | `delegated-verified` only when the attestation is valid, otherwise `delegated-unverified` or `not-enforced` |
| `opposite-profile-files`, `profile-config-edits`, `path-executables` | managed boundary | same as above |
| `keychain-access`, `child-trust-store` | managed boundary | same as above |
| `npm-global-config` | managed boundary | same as above; listed only when package installs are allowed |

Quoin has no launcher proxy: a proxy between the child and the network would be a
second boundary to build and attest. The managed boundary carries shell egress,
redirects and name resolution after launch.

## Maintainer actions

Neither checklist has been completed. Until both are, the work profile stays
unsupported.

In-environment managed-isolation pass, run inside the real managed environment:

- [ ] Install the attestation and corroboration and confirm `quoin opencode
  doctor` reports `isolation-verified` for the work profile.
- [ ] Confirm the opposite profile is not visible from inside the environment
  and that a launch refuses while it is.
- [ ] From a shell the agent can start, try a connection to an unapproved host
  and confirm the firewall or proxy blocks it, including after a redirect.
- [ ] Confirm the child cannot write the Quoin profile files, the attestation or
  any path outside the project and the work profile's storage directory.
- [ ] Confirm no loopback listener on the machine forwards to an unapproved
  destination.

Live corporate capture of zero personal requests:

- [ ] Run a work launch with a network capture on the corporate side and confirm
  no request reaches a personal provider endpoint.
- [ ] Repeat with the personal profile installed on the same account and confirm
  the work launch refuses instead of running.
- [ ] Record the capture result in the maintainer decisions and update the
  compatibility notes for any control the capture could not confirm.
