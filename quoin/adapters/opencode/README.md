# OpenCode adapter (qualification)

This directory holds the tools used to decide whether a gateway and model are
usable as an OpenCode agent backend, before any runtime integration exists.
There is no OpenCode runtime integration here yet — this is a qualification
step only.

- `probe_gateway.py` — a standalone script that runs a short handshake against
  a chat-completions-shaped endpoint and reports whether it supports the
  protocol features an agent needs (text generation, native tool calling,
  streamed tool arguments).
- `fake_openai_server.py` — an offline fake provider used by the test suite
  (and available for local experimentation) so the probe can be exercised
  without a real gateway or API credentials.
- `fixtures/scenarios.json` — the data-only catalogue of request/response
  shapes the fake server plays back.

## Running the probe

```
python3 quoin/adapters/opencode/probe_gateway.py \
  --base-url https://gateway.example.invalid/v1 \
  --model example-model \
  --credential-env EXAMPLE_API_KEY \
  --provider example \
  --output probe-record.json
```

The credential is read from the environment variable **named** by
`--credential-env` — never pass a credential value on the command line. The
variable's value is used once, held in memory only for the duration of the
run, and never printed; only the variable's name ever appears in output.

Other flags:

- `--ca-file` — a custom CA bundle, for gateways behind a corporate proxy
  with a private certificate authority. Certificate verification is never
  disabled by this tool.
- `--use-env-proxy` — honour `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` from the
  environment. Off by default, so a stray proxy variable in your shell can't
  silently redirect a probe run.
- `--runtime-version` — the OpenCode version this run is meant to qualify;
  recorded in the output but does not change probe behaviour.
- `--declared-context-limit` / `--declared-output-limit` — record a limit you
  already know from the gateway's documentation. These are recorded as
  declared, not observed — the probe does not verify them.
- `--timeout` — per-request timeout in seconds (default 30). Each request
  phase — waiting for response headers, then reading the body or stream —
  gets its own budget of `--timeout` seconds, so one request can take up to
  about twice `--timeout` in the worst case. Every individual read also
  carries a socket timeout, so a connection that goes silent mid-response is
  never waited on indefinitely.
- `--active-error-checks` — optional, comma-separated (`invalid-token`,
  `context-overflow`). See the caution below before enabling these.

The probe never follows redirects, never retries a failed request, and never
falls back to a different endpoint or model.

**Caution:** `--base-url` must not carry a credential or tenant token in its
path (userinfo, query strings and URL fragments are already dropped or
rejected). The endpoint path is recorded in the capability record exactly as
given, so anything sensitive placed there ends up on disk.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | qualified — the endpoint supports the protocol this tool checks |
| 1 | not qualified — the endpoint answered, but showed a protocol incompatibility |
| 2 | could not run — the check was inconclusive |

Exit 2 covers every cause that doesn't tell you anything about protocol
support: bad configuration, connection failures, TLS problems, timeouts,
redirects, invalid or forbidden credentials, rate limiting, server errors,
and an endpoint or model that could not be found. None of those disprove
tool-calling support, so none of them are reported as "not qualified" — a
maintainer deciding whether to invest in an integration needs to be able to
tell "this gateway doesn't support tools" apart from "I couldn't even reach
it".

On exit 2, the diagnostic is written to stderr and nothing is printed on
stdout; a script driving this tool should key off the exit code or the
written record's verdict status, not stdout content.

## Live error provocations (`--active-error-checks`)

By default the probe only classifies failures it happens to see while
running its normal steps. `--active-error-checks` opts into two live
provocations instead:

- `invalid-token` sends one request with a deliberately wrong credential.
  On a gateway with lockout or anomaly-detection policies, this could
  trigger a temporary block on the real credential.
- `context-overflow` sends an oversized prompt to force a context-length
  error. This costs real tokens against your quota.

Both are off by default for these reasons. Neither check can change the
overall verdict — that depends only on the first three steps.

## Capability record

`--output` writes a JSON record (schema `quoin.opencode.capability-record`)
describing the endpoint, the model, which steps passed or failed, and a
status for nine capability fields (text generation, tool calls, streamed
tool arguments, usage reporting, structured output, context/output limits,
reasoning parameters, parallel tool calls). Several fields stay `unknown` in
this version because the probe deliberately avoids sending optional
parameters that some gateways reject outright — sending them would turn an
untested feature into a false failure. The record is written whenever the
output path is writable, on every exit code, and is redacted before being
written to disk.

## Status documents

- `compatibility.md` — which upstream OpenCode release this adapter is
  qualified against, and the per-claim verification status of that release's
  documented behavior.
- `decisions.md` — configuration choices that only a maintainer can make,
  plus the empty template a real probe run's record gets pasted into.

## Using the fake server

```python
from fake_openai_server import FakeProviderServer

with FakeProviderServer() as server:
    print(server.base_url)   # http://127.0.0.1:<port>/v1
    ...
```

It can also run standalone for manual experimentation:

```
python3 quoin/adapters/opencode/fake_openai_server.py --scenario default_ok
```

Scenario selection, in priority order: the `X-Fake-Scenario` request header,
then the request's `model` field (when it names a known scenario), then the
server's configured default. `GET /v1/models` lists every scenario name, so
a model picker can drive the server through its model ID alone.

## Adding a scenario

Scenarios live entirely in `fixtures/scenarios.json` — no server code needs
to change. Each scenario is a list of turns; the server picks a turn by
matching the request's depth (how many assistant turns have already
happened) and shape (whether tools were sent, whether streaming was
requested) against each turn's optional `depth`/`match` fields, falling back
to the deepest turn that declares no shape restriction. A turn is either a
plain JSON body, a non-stream chat-completion `message`, or a list of SSE
`chunks`. Two template placeholders, `{{last_tool_content}}` and
`{{last_tool_call_id}}`, can appear in a turn at depth 1 or deeper to echo
back the previous tool result.
