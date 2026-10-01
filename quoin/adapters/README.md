# Runtime Adapters

Adapters connect the portable Quoin workflow to a specific agent runtime.

The portable workflow contract lives in `quoin/docs/runtime-portability.md`. Runtime adapters own invocation syntax, install behavior, model mapping, session/cost plumbing, and runtime-specific instructions.

Current adapter status:

- Claude Code: supported today through the existing `quoin/install.sh`, `quoin/CLAUDE.md`, and `quoin/skills/` layout.
- Codex: scaffolded through repo-local instructions and documentation.
- OpenCode: generated project assets (`quoin install --runtime opencode`, `quoin doctor --runtime opencode`, `quoin opencode uninstall`; offline-verified configuration tooling in `quoin opencode config explain|compile|import-preview` and `quoin opencode probe`; single-phase headless runs with `quoin run --runtime opencode`, `quoin opencode status` and `quoin opencode start`; doctor findings grouped into problem categories), statically checked and offline smoke-tested; live runtime support is not yet verified. See `opencode/README.md`, `opencode/compatibility.md` and `opencode/decisions.md`.

Do not duplicate shared memory files, scripts, or skill templates into adapter folders until the shared core has been split from runtime overlays.
