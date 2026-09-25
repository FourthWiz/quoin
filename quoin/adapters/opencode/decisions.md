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
