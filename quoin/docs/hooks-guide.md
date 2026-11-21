# Hooks Guide — Verbose Reference

This file contains detailed documentation for the hooks deployed by `bash install.sh`. See `quoin/CLAUDE.md` `### Hooks deployed by quoin` for the summary table.

## S-4 banner dedup

The SessionStart hook writes a sentinel file at `$TMPDIR/quoin-s4-eod-banner-<YYYY-MM-DD>.tmp` after firing the missing-EOD banner. If a sentinel from within the last 5 minutes exists, the banner is suppressed for the current session start. `/start_of_day` also reads this sentinel (see `/start_of_day/SKILL.md`) and skips its own banner if the hook fired within the last 5 minutes — preventing duplicate noise when both mechanisms are active.

## Fail-OPEN / non-aborting deploy contract

Every hook exits 0 on any error (no abort). If jq is absent, hooks fail-OPEN silently (zero protection — see jq soft-required dependency below). `userpromptsubmit.sh` prints nothing on any path, so there is no output to hold back on a failed write.

## STDIN capture pattern

All six event hooks open with `STDIN=$(cat)` to capture the JSON payload into a variable, then parse with `printf '%s' "$STDIN" | jq -r '<filter> // empty'`. The `// empty` jq filter handles missing fields by returning the empty string instead of `null`, supporting the fail-OPEN discipline.

## `--dry-run` (scoped to `router setup`, not `install`)

`--dry-run` is registered only on the `quoin router setup` subparser (`src/quoin/cli.py:966-970`) — printing what would change without writing any files. The `install` subparser has no such flag, and `quoin/install.sh` forwards its arguments straight to `python -m quoin` with no `--dry-run` handling of its own; `bash install.sh --dry-run` is an argparse error today, not a preview mode.

## jq soft-required dependency

Runtime hooks parse stdin JSON via `jq`. If `jq` is absent, hooks fail-OPEN silently (zero protection). Install via `brew install jq` (macOS), `apt-get install jq` (Debian), `apk add jq` (Alpine). `bash install.sh` emits a warning if jq is absent at deploy time AND writes `~/.claude/HOOK_MERGE_TODO.md` with manual-merge instructions; install proceeds but hooks will not function until jq is installed.

## R-09 mitigation (settings.json corruption)

`installer.py::deploy_hooks` copies `~/.claude/settings.json` to `settings.json.broken-<timestamp>` and starts fresh if the existing file fails to parse as JSON, so a corrupted settings file cannot abort the merge silently. That copy may be the only record of your old settings, so the installer never prunes `.broken-` files; delete them yourself once recovered. It also writes a `settings.json.bak-<timestamp>` copy of a valid file before rewriting it, only when the merge changes the content, and then keeps the newest 5 such backups (set `QUOIN_SETTINGS_BACKUP_KEEP` to keep a different number, minimum 1). Files with other names, such as `.broken-` copies or hand-made `settings.json.bak-smoke`, are never pruned.

## WorktreeCreate hook (`worktreecreate.sh`, IVG-116)

Registered as the eighth stanza (`WorktreeCreate`/`*`, timeout 10s). Fires when a source-mutating skill (`/implement`, `/rollback`, `/end_of_task`, `/pr`) dispatches an Agent with `isolation: "worktree"`. The hook reads the dispatch sidecar (`<project_root>/.workflow_artifacts/.dispatch-hint.json`), calls `git_root_for_dispatch.py --sidecar`, and when a single nested git repo resolves it decides a worktree path:

- **Self-generation (the IVG-116 fix):** when the harness omits `worktree_path`/`branch_name` on the hook's stdin (the pre-fix 100%-skip cause on Google-Drive-synced projects), the hook SELF-GENERATES `BRANCH_NAME="quoin/wt-<ts>-<pid>"` and a `WORKTREE_PATH` under `${TMPDIR:-/tmp}/quoin-worktrees` (outside the Drive-synced tree; project `.worktrees/` fallback), then runs `git worktree add` and prints the created path to stdout. The audit log (`worktree-hook-audit.log`) records `selfgen=1`.
- **Timeout:** `git worktree add` is wrapped in `timeout "${QUOIN_SUBPROCESS_TIMEOUT:-30}s"` via a `git_wt()` helper; if the `timeout` binary is absent the command runs unwrapped (fail-OPEN).
- **Fail-OPEN:** any failure (no nested repo, timeout, git error) exits 0 with NO stdout, so the harness/skill falls back to a plain no-isolation dispatch.
- **Opt-out:** `QUOIN_WORKTREE_SELFGEN=0` restores the old `rc=skip result=missing-worktree-path-or-branch` behaviour.

Whether isolation is even attempted is gated UPSTREAM by the `worktree_isolation.py --decide` STEP A0 in the four source-mutating SKILL.md (default `skip`; see `dispatch-guide.md`). The hook source is `quoin/quoin/hooks/worktreecreate.sh`.

## Tunable constants

Hook scripts read these values at runtime via `${QUOIN_*:-default}` parameter expansion. Defaults are baked into the scripts:

| Constant | Default | Env var override | Notes |
|----------|---------|-----------------|-------|
| `BPT` | `8.0` | `QUOIN_BYTES_PER_TOKEN` | Bytes per token for the byte-estimate fallback (V-03 calibrated) |
| `LIMIT` | `150000` | `QUOIN_EFFECTIVE_CONTEXT_LIMIT` | Unset: the window is detected per model (200k / 1M). Set: an explicit window override on the real path, and the estimate denominator (150000 when unset) |
| `CONTEXT_TAIL_BYTES` | `2097152` (cap `67108864`) | `QUOIN_CONTEXT_TAIL_BYTES` | Bytes of transcript tail scanned for the newest usage record; read inline, not exported |
| `STALE_DAYS` | `7` | `QUOIN_STALE_SENTINEL_DAYS` | Days after which pending-prompt-*.txt (left by older installs) and pending-restore-*.txt are swept; long-lived sessions may extend to 14+ |
| `POLLUTION_THRESHOLD` | `5000` | `QUOIN_POLLUTION_THRESHOLD` | Score threshold for pollution dispatch, read by `scripts/pollution_guard.py` (score = transcript_kB + weighted tool-use count); 5000 ≈ 5MB transcript or ~1MB + heavy tool use |
| `SUBPROCESS_TIMEOUT` | `30` | `QUOIN_SUBPROCESS_TIMEOUT` | Seconds bounding `git worktree add` in `worktreecreate.sh` (and short git subprocesses in the core scripts); ~2× the observed Drive baseline. Fail-OPEN if the `timeout` binary is absent |
| `WORKTREE_SELFGEN` | `1` (on) | `QUOIN_WORKTREE_SELFGEN` | `0` disables hook self-generation of branch/worktree path (restores the old skip-when-harness-omits behaviour) |
| `RUN_STATE_STALE_DAYS` | `1` | `QUOIN_RUN_STATE_STALE_DAYS` | Run-state record freshness window in days; see `hooks-table.md` |
| `TELEMETRY_MAX_BYTES` | `1048576` | `QUOIN_TELEMETRY_MAX_BYTES` | Compaction-telemetry sink rotation size in bytes; see `hooks-table.md:25-28` |

`read_constants()` exports 8 names; the table above covers 5 of them (`POLLUTION_THRESHOLD`, `SUBPROCESS_TIMEOUT` and `WORKTREE_SELFGEN` are read inline elsewhere). `hooks-table.md` is the complete list, including the exported names missing here (`SESSIONSTART_SWEEP_DAYS`, `DISCOVERY_STALE_DAYS`, `SERENA_STALE_DAYS`) plus every skill-side and script-side knob. `QUOIN_STOP_BPS`, `QUOIN_BLOCK_BPS`, `QUOIN_COMPACT_FIRST_BPS`, `QUOIN_PANIC_BPS` and `QUOIN_PRECOMPACT_NORUN_CHECKPOINT` are no longer read; exporting one has no effect.

**Basis-points convention:** Utilization values and threshold comparisons use INTEGER basis-points (0..10000) throughout. POSIX `[ ]` does integer comparison only; basis-points eliminate all floating-point comparison hazards. `compute_utilization()` in `_lib.sh` returns a basis-point integer (e.g., `8540` = 85.40% utilization).

## Context reading

The context reading used by the phase-boundary guard and the compaction telemetry is measured from real token usage, not from transcript size.

- **What is read.** The newest main-chain assistant record's usage: `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens`. Sidechain (subagent) records and synthetic error records never count. A compact-boundary record carrying `postTokens` replaces the reading when it is newer than the last usage record.
- **Window.** Resolved in this order: an explicit `QUOIN_EFFECTIVE_CONTEXT_LIMIT` override; `CLAUDE_CODE_DISABLE_1M_CONTEXT` (200k); a recorded "Usage credits required for 1M context" clamp (200k); a `[1m]` suffix on the record's model, on `ANTHROPIC_MODEL`, or on the `model` in project `settings.local.json`, project `settings.json` or user `settings.json` (1M); the model table (models that are natively 1M, or 200k); an observed peak above 200k (1M); otherwise 200k.
- **Fallback.** When no usage record can be read (no `jq`, an empty or unparseable tail, a transcript with no usage), the shell reader falls back to a byte estimate: transcript bytes divided by `QUOIN_BYTES_PER_TOKEN`, then by `QUOIN_EFFECTIVE_CONTEXT_LIMIT` (150000 when unset).
- **Labels.** Every notice names its source: `context at 17.08% (real) — …` for a measured value and `context at ~40.12% (estimated) — …` for the fallback.
- **Scan window.** Only the last `QUOIN_CONTEXT_TAIL_BYTES` of the transcript are scanned (default 2097152, capped at 67108864; read inline, not exported). The scan is paid only by the callers that ask for a reading (the guard, the compaction hooks); no prompt hook scans the transcript.

**Migration.** `QUOIN_EFFECTIVE_CONTEXT_LIMIT` used to be the 100% denominator for every reading. It now pins the window on the real path, so a value exported under the old meaning (for example 150000) makes a 1M-window session over-warn. Unset it unless you want to force a window.

**Known divergences between the shell and Python readers.**
- Without `jq` the shell reader can only estimate, while the phase-boundary guard (`context_budget_guard.py`) still reads real usage.
- An invalid `QUOIN_EFFECTIVE_CONTEXT_LIMIT` on the estimate path: the awk estimate has no validation, the Python estimate falls back to 150000.
- Lines nested deeper than 256 levels: `jq` skips the line, Python parses it until its own recursion limit (from about a thousand to over a hundred thousand levels, depending on the interpreter; the 256 figure is the `jq` 1.7 limit). A usage record is never nested that deeply in practice.
- A 1M-credits clamp is forgotten once its record scrolls out of the scanned tail.
- Settings files holding several concatenated JSON values are read by `jq` value by value: a `[1m]` in any of them counts, while Python skips the whole file ("Extra data").

**Telemetry fields.** Compaction telemetry rows gain `real_tokens_before` (pre rows), `real_tokens_after` (post rows), `real_window` and `reading_source`. All are `null` when no real value exists. `reading_source` names the source of the `real_tokens_*` value: `real` when filled, `estimated` when only the byte estimate exists, `null` when neither does. In measurements of six compactions, the compact-boundary record was written after the PostCompact hook returned (the hook's measured transcript size equalled the boundary line's start offset every time), so `real_tokens_after` is normally `null` today; the next reading after the compaction reads the post-compaction tokens instead. If Claude Code starts writing the boundary first, the field fills with no change here.

### 200k-window tier reachability

Claude Code auto-compacts at `window − min(max output, 20000) − 13000` tokens: 167,000 tokens (83.50%) on a 200k window and about 967,000 tokens (96.70%) on a 1M window. The context-threshold tiers that used to sit around that point are retired, so nothing in quoin keys on a fill level any more except the over-budget phase-boundary guard (`QUOIN_PHASE_BOUNDARY_BPS`). `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` and the auto-compact window setting move Claude Code's own point.

## Compaction continuity (IVG-258)

Three shipped mechanisms keep a `run_hard` (`/run hard:`) task resumable across a compaction; the light `/run` keeps no run-state record and resumes from its task folder and git history:

- **The task-keyed run-state record** (`run-state-<task>.json` + `run-notes-<task>.md`) is read through `run_state_select` in `_lib.sh` and is the source of truth the consent rule in `userpromptsubmit.sh` and the other hooks read to detect an active `run_hard` task.
- **`precompact.sh`'s always-allow path** — the hook always emits `{"decision": "allow"}` (it never blocks compaction) and writes no checkpoint or sentinel; it appends the session record, a run-notes block while a run is mid-stage, and the telemetry pre-event. See `hooks-table.md`.
- **The `SessionStart`/`compact` re-entry branch** in `sessionstart.sh` reads the session's run-state record and re-hydrates the resuming session (an advisory plus the resume command, or silence when no run is active).

The **compaction-telemetry sink** (`_tel_sink`, capped by `TELEMETRY_MAX_BYTES` above) records a "pre" event before compaction and a matching "post" event after, so real firing frequency can be measured before any threshold tuning.

### Opt-in platform-threshold delegation

Two environment variables, read natively by Claude Code (not by any quoin hook), can be written into `settings.json`'s `env` block by the installer:

- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` — `1..100`. Can only *lower* the auto-compaction trigger point; a value at or above the platform default is a no-op.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW` — `100000..1000000`, a plain integer token count with **no suffix** (quoin's own installer requirement — `500k` is rejected at install time).

Both are settable through `settings.json`'s `env` block as **strings**, both require `autoCompactEnabled: true` to have any effect, and the two are independent — supplying either alone is a complete opt-in. Neither is deprecated, and neither interacts with `/compact` or the `PreCompact`/`PostCompact`/`SessionStart(compact)` hooks. For context, the published default for a 1M-window Sonnet session is approximately 967K tokens — already before the limit — so a percentage override is a real change even with no window configured.

This is the only place quoin ever writes a non-`QUOIN_*` variable. It is off by default (a plain install never creates the `env` key), it writes to whichever `settings.json` the install `--scope` targets, and it is set via three `quoin install` flags:

- `--autocompact-pct N`
- `--autocompact-window TOKENS`
- `--clear-autocompact-env` — removes exactly the two keys above, leaving every other `env` key untouched.

## Opt-in workflow-tasks mod

`/quoin-tasks` opens a pane listing the active quoin tasks of the current project as a parent/child tree. Each row shows the task's stage (spec, architecture, plan, implement, review, and whether the last gate passed or failed) and how long ago it changed, and the detail block names the next workflow command. Arrow keys and Enter select a row, `f` fills the suggested command into the prompt box, and `r` refreshes. It ships as a small Claude Code mod and is never installed unless you ask for it.

- **Fills, never sends:** the command is only placed in the prompt box (replacing the draft); you press Enter yourself. The pane closes after a successful fill so the keys return to the prompt, and stays open with a message when the prompt box cannot take it (a dialog is open, or there is no box).
- **Read-only:** the mod only lists and reads files under the project's `.workflow_artifacts/`. It writes no files, runs no commands and touches no settings. It scans only when you run `/quoin-tasks` or press `r`; nothing runs at session start.
- **Install:** `bash quoin/install.sh --with-workflow-tasks` (or `quoin install --with-workflow-tasks`, or answer yes when the installer asks) copies five files into `skills/workflow-tasks/` under the install scope. Claude Code auto-loads that folder as `workflow-tasks@skills-dir`. No `settings.json` key, hook stanza or environment variable is written.
- **Sticky refresh:** once installed, a plain reinstall refreshes the copy and never creates one where none exists.
- **Remove:** `--remove-workflow-tasks` deletes the folder.
- **Ownership is by name only:** a hand-copied `workflow-tasks` folder is overwritten or removed, but any other folder in that spot, or a symlink, is left alone and the opt-in refuses.
- **Scope:** the default is user scope. Project scope works once the workspace is trusted, and refuses while a user-scope copy is installed (both would load).
- **Disable without reinstalling:** `claude plugin disable workflow-tasks@skills-dir`. That entry survives removal and must be re-enabled by hand if you install again.
- **Requirements:** verified on Claude Code 2.1.288; older builds ignore the folder.
- **Drift check:** the deploy drift check compares the mod's files only when the mod is installed, and only for the scope it checks (user by default).

## Install questions

In a terminal, `quoin install` (and `install.sh`, which delegates to it) asks about the optional workflow-tasks mod before it writes anything.

- **When it asks:** only when both stdin and stdout are terminals. Piped, redirected or CI installs never ask. `QUOIN_INSTALL_NO_PROMPT=1` also suppresses the mod question. The install-scope prompt is separate, so pass `--scope` as well for a fully silent install.
- **Mod:** the workflow-tasks mod defaults to Yes. It is asked about only when it is not installed and its flag was not given; an installed copy is refreshed silently. If it cannot be installed (foreign folder, missing sources, user-scope copy during a project install), the reason is reported in one line and it is not offered.
- **Flags win:** `--with-workflow-tasks` and `--remove-workflow-tasks` answer the question up front.
- **Abort:** Ctrl-C or end of input at the question exits with status 1 before anything is written.
