# OpenCode adapter

OpenCode: generated project assets (`quoin install --runtime opencode`,
`quoin doctor --runtime opencode`, `quoin opencode uninstall`), statically
checked and offline smoke-tested; live runtime support is not yet verified.

This directory holds the generator, installer and doctor that produce and
check a project's OpenCode assets, plus the gateway-qualification tooling
used to decide whether a gateway and model are usable as an OpenCode agent
backend.

- `probe_gateway.py` — a standalone script that runs a short handshake against
  a chat-completions-shaped endpoint and reports whether it supports the
  protocol features an agent needs (text generation, native tool calling,
  streamed tool arguments).
- `fake_openai_server.py` — an offline fake provider used by the test suite
  (and available for local experimentation) so the probe can be exercised
  without a real gateway or API credentials.
- `fixtures/scenarios.json` — the data-only catalogue of request/response
  shapes the fake server plays back.
- `feature-manifest.json` — the classified catalogue of every Quoin skill,
  with a support status, a target milestone, and the OpenCode asset names
  generated for it. See "Support classification" below.

## Install, check and uninstall

Three commands manage a project's repo-local `.opencode/` scaffold:

- `quoin install --runtime opencode --project-root <path> [--profile <label>] [--check]` —
  renders the scaffold from the source tree and writes it under `<path>`. `--check`
  prints the plan and writes nothing.
- `quoin opencode uninstall --project-root <path> [--dry-run]` — removes everything
  Quoin owns under that project. `--dry-run` prints the plan and writes nothing.
- `quoin opencode script <name> [args...]` — runs one allowlisted Quoin script
  against the current project, without a full install. This command is a
  deliberate addition to the spec's CLI surface: the allowlist and its
  refusal behavior are described below.

A plan line names one of these actions for each file: `create` (new), `update`
(owned, content differs), `unchanged` (owned, content matches), `adopt` (an
existing file byte-identical to what Quoin would generate becomes owned —
a deliberate narrowing of "any existing unowned path is a conflict", chosen
so a clean re-render of an unmodified project never conflicts with itself),
`delete` (owned file removed because it's no longer generated), `forget`
(an owned file already missing is dropped from the metadata without an
error), and `conflict` (an existing path the installer cannot safely take
over — nothing is written when any conflict is present).

Ownership metadata lives at `.quoin/opencode-install.json`. It's plain JSON,
safe to commit, and should be committed together with the generated files
or not at all — committing one without the other leaves a teammate's `.quoin`
directory out of sync with what's actually on disk. An existing directory
that ends up holding only files and directories Quoin generated is recorded
as Quoin's own, the same as a directory Quoin created outright, and is
removed on uninstall once it's empty.

A hard kill mid-apply can leave a `.quoin-tmp-*` temporary file behind; the
next install or uninstall sweeps it automatically, so no manual cleanup is
needed.

Exit codes:

| Command | Code | Meaning |
|---|---|---|
| install | 0 | done, or already up to date |
| install | 1 | `--check` found changes pending |
| install | 2 | usage, metadata or generation error, or an apply was interrupted |
| install | 3 | one or more conflicts; nothing was written |
| uninstall | 0 | done, or nothing was installed |
| uninstall | 2 | usage or metadata error |
| uninstall | 4 | done, but one or more owned files had been modified and were left in place; the metadata still lists them, so a later install will conflict on those paths |
| script | (script's own) | the allowlisted script's own exit code |
| script | 1 | the script raised `SystemExit` with a non-integer value |
| script | 2 | an unknown script name, or the runner refused to run it |

`--dry-run` and `--check` return the same code the real run would return.

`quoin opencode script` only runs six allowlisted Quoin scripts:
`checkpoint_picker`, `classify_critic_issues`, `generate_discovery_map`,
`handoff_validate`, `path_resolve`, `validate_artifact`. One of them,
`generate_discovery_map`, writes a file; its output is confined to the
artifact root of the installed project, the same root the generated
permission rules edit inside, by walking every path component between the
project root and the destination with `lstat` — never resolving a symlink.
The runner refuses to run it when: a symlinked path component sits anywhere
in that walk (including a symlinked artifact root or a symlinked `.quoin`
directory); the destination's basename is anything other than
`discovery-map.json`; or a `discovery-map.json.tmp` left over from an
earlier crash already exists at that location (remove it by hand before
retrying). The script's own positional project-root argument is read with
whatever read posture a Quoin role already has — only the write path above
is confined. The runner also refuses to run at all when its own standard
output or standard error is a regular file, so the script's output never
lands in that file — but the shell still creates or empties the target
before the runner starts, and a redirection of any other file descriptor
(such as `3>`) is never seen by the refusal check at all; a hard kill
between the shell opening the target and the refusal firing can leave it
truncated. Passing `--` on the command line is not a transparent
pass-through to the script either: the outer command-line parser consumes
that `--` itself, and the arguments after it still reach the script and are
still checked by the runner's own parser.

## Doctor

`quoin doctor --runtime opencode --project-root <path> [--smoke] [--json]`
checks a project's generated OpenCode assets.

- Host checks (default, no flag): reads the host filesystem and
  environment. Checks the installed state against a fresh render, the
  manifest against the pinned source, `.quoin` install metadata (including
  leftover `.quoin-tmp-*` files), the Claude Code rules fallback
  (`AGENTS.md`/`CLAUDE.md`), OpenCode compatibility flags and config
  sources, a skill census (duplicate skill names, a skill outside the
  project's own `.opencode/skills`, legacy Claude Code skills OpenCode may
  also load), a user `permission` config that narrows a tool a generated
  role's own permission map still allows while it runs, and the `opencode`
  binary's presence, PATH position and version. Because it reads the real
  host, a machine that also has Quoin's Claude adapter installed normally
  reports warnings — its own Claude rules files and skills are visible to
  the census, and that is expected, not a failure. Use `--smoke` for a
  clean pass/fail signal, for example in CI.
- `--smoke`: renders the catalog offline into a temporary directory and
  checks the rendered bytes and metadata against themselves. No host
  filesystem or environment is read beyond the source tree, no network
  call is made, and no `opencode` binary is required.
- `--json`: emits the JSON schema below instead of text. Refused with
  exit 2 for any `--runtime` other than `opencode`.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | healthy — no `error` or `warn` findings |
| 1 | one or more `error` findings, or a `--smoke` check failed |
| 2 | usage error, such as `--json` or `--smoke` combined with a non-`opencode` runtime |
| 4 | warnings only — no `error` findings, at least one `warn` finding |

JSON schema (`--json`):

```json
{
  "schema_version": 1,
  "runtime": "opencode",
  "status": "healthy | warnings | errors",
  "findings": [
    {
      "id": "<finding id>",
      "severity": "error | warn | info | ok",
      "message": "<fixed-vocabulary message>",
      "path": "<display path, present when the finding names one>",
      "remediation": "<suggested next step, present when one applies>"
    }
  ]
}
```

Finding ids, grouped by severity (`legacy-claude-skills` reports `warn`
normally and `info` when the matching disable flag is already set):

| Severity | Finding ids |
|---|---|
| error | `smoke-render`, `smoke-roundtrip`, `smoke-names`, `smoke-frontmatter`, `smoke-digests`, `smoke-bundle`, `smoke-read-only-roles`, `smoke-task-graph`, `smoke-forbidden-output`, `smoke-script-refs`, `smoke-config`, `render-failed`, `manifest-drift`, `manifest-unreadable`, `install-metadata-invalid`, `project-config-disabled` |
| warn | `install-absent`, `owned-missing`, `owned-modified`, `owned-stale`, `owned-not-installed`, `owned-unreadable`, `rules-global-claude-md`, `rules-project-claude-md`, `config-unreadable`, `subagent-depth-raised`, `quoin-not-on-path`, `skill-duplicate`, `skill-duplicate-unnamed`, `quoin-skill-outside-project`, `legacy-claude-skills` |
| info | `temp-leftover`, `rules-agents-md-present`, `flags-set`, `config-env-set`, `opencode-binary-absent`, `opencode-version`, `opencode-version-unknown`, `install-version-differs`, `census-unverified`, `skills-url-not-scanned`, `skills-path-missing`, `census-truncated`, `permission-loosened` |
| ok | `smoke-ok`, `install-current` |

Redaction: every finding message is built only from a fixed message
template plus the names, paths, counts and version numbers the doctor is
explicitly allowed to substitute into it. A path is always rendered under
its matching root's display name (see the install and script-runner
sections above), never as a raw absolute path; the doctor never echoes
file content, exception text or an environment variable's value.

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
- `--timeout` — per-request timeout in seconds (default 30). The streaming
  phase has a total budget of `--timeout` seconds; header and non-stream body
  reads carry a per-read socket timeout instead, so a connection that goes
  silent mid-response is never waited on indefinitely.
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

On exit 2, the diagnostic goes to stderr; stdout carries at most the
one-word verdict `could_not_run` (nothing at all on a configuration error or
an unwritable `--output`). A script driving this tool should key off the
exit code or the written record's verdict status, not stdout content.

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

## Support classification

`feature-manifest.json` classifies every Quoin skill into one of three
statuses:

- `supported` — an OpenCode command and skill are generated for it; the
  generated pair is statically valid for the pinned release and
  offline-smoked, but not yet qualified against a live OpenCode run.
- `documentation-only` — the skill has a portable contract but no OpenCode
  asset is generated for it yet; it is listed as not yet available.
- `unsupported` — the skill depends on Claude-only mechanics that have no
  OpenCode equivalent.

`live_runtime_evidence` and `evidence` are `false`/empty for every row
today. A later change flips `live_runtime_evidence` to `true` and fills
`evidence` in once a row has been exercised against a real OpenCode run.

Each row also names a target milestone, in plain terms:

- `adapter-foundation` — the generator, install, doctor, and runtime
  configuration this adapter ships.
- `workflow-execution` — the runtime driver and live end-to-end workflow
  runs.
- `work-context` — profile isolation and read-only work integrations.
- `controlled-writes` — approved external writes.
- `release-hardening` — remaining catalog coverage, benchmarks, and release
  qualification.
- `none` — no OpenCode support is planned for this skill.

Naming rule: an OpenCode-facing name is `quoin-` plus the skill's id, with
every underscore turned into a hyphen. Names are checked for uniqueness
per namespace (commands, skills, agents), since OpenCode keeps those as
separate maps. The canonical id — the one used everywhere else in Quoin —
always stays in the manifest row, never in the generated name itself.

Run the drift check with (from a repo checkout, `quoin` is not installed, so
`src` must be on `PYTHONPATH`):

```
PYTHONPATH=src python3 -m quoin.opencode_adapter check-manifest --source-dir quoin
```

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
