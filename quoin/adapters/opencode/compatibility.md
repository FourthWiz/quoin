# OpenCode compatibility

## Pinned release

```
Version: 1.18.32
Install channel: latest
Verified on: 2026-09-24
Upstream repository: https://github.com/anomalyco/opencode
```

## Evidence rule

A claim is `verified` only when backed by at least one of:

- a tag-pinned GitHub artifact — a `blob`, `tree`, or `releases/tag` URL at the pinned
  tag or an earlier tag on the same release line;
- a docs page from a docs tree that is versioned for the pinned release line;
- a local run of the pinned binary, with the exact command shown in backticks.

A claim resting only on unversioned documentation is `unverified — documentation not
version-pinned`. Every row must carry a `Status` cell of exactly `verified` or
`unverified — REASON` (reason non-empty).

## Release lines

Two active release lines exist upstream as of the verification date.

| Line | Latest version | Published as | Docs |
|---|---|---|---|
| 1.18.x | 1.18.32 (published 2026-09-21) | GitHub Releases (changelog per release) | unversioned `opencode.ai/docs` |
| 2.0.x | 2.0.16 (tagged 2026-09-24) | git tags only — no GitHub Release published for this line yet | versioned `opencode.ai/v2/docs` |

**Chosen line: 1.18.x, pinned at 1.18.32.**

Evidence for the choice, applying the pin criteria in order:

1. **Default install channel.** The install script at `opencode.ai/install` resolves
   the default (`latest`) channel through the GitHub Releases API
   (`repos/anomalyco/opencode/releases/latest`), which returns `v1.18.32`. The npm
   package `opencode-ai`'s `latest` dist-tag independently agrees: `1.18.32`. The
   2.0.x line has no published GitHub Release, so `releases/latest` cannot resolve
   to it; npm's dist-tags carry no 2.0.x-versioned tag either (only unversioned
   `0.0.0-dev-*` snapshots), so the default channel decides the line by itself.
2. **Published schema.** The unversioned schema at `opencode.ai/config.json` uses a
   singular `provider` map with `npm`/`options.apiKey`/`options.baseURL` fields, a
   flat `mcp` object keyed by server name, and a singular `plugin` list — matching
   the 1.18.x doc examples at `opencode.ai/docs/config`. This is the V1 config shape.
3. **Versioned docs.** Moot for the pin, since rule 1 already selected 1.18.x, but
   recorded for completeness: a versioned docs tree exists at `opencode.ai/v2/docs`
   for the 2.0.x line (confirmed reachable, HTTP 200) and documents a different,
   incompatible shape — plural `providers`, nested `mcp.servers`, and plural
   `plugins`. No equivalent versioned tree exists for 1.18.x; its docs live at the
   unversioned `opencode.ai/docs`.

**V1 vs V2 config shape (for the pinned 1.18.x line, V1 applies):**

| | V1 (1.18.x, pinned) | V2 (2.0.x) |
|---|---|---|
| Providers | `provider` (singular map) | `providers` (plural map) |
| Provider auth/URL | `provider.<id>.options.apiKey` / `.baseURL` | not confirmed under this key path |
| Custom provider package | `provider.<id>.npm` | `providers.<id>.package` (per architecture reference; not independently re-verified this round) |
| MCP servers | flat `mcp.<name>` | `mcp.servers.<name>` |
| Plugins | `plugin` (singular list) | `plugins` (plural list) |

The Quoin for OpenCode development specification's gateway-qualification guidance
describes the provider configuration in the V1 shape (`provider`, `npm`, `options`).
That agrees with the pinned 1.18.x line and disagrees with the unpublished 2.0.x
line — the specification's guidance is correct for the release actually being
qualified against.
