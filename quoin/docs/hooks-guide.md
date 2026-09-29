# Hooks Guide — Verbose Reference

This file contains detailed documentation for the hooks deployed by `bash install.sh`. See `quoin/CLAUDE.md` `### Hooks deployed by quoin` for the summary table.

## S-4 banner dedup

The SessionStart hook writes a sentinel file at `$TMPDIR/quoin-s4-eod-banner-<YYYY-MM-DD>.tmp` after firing the missing-EOD banner. If a sentinel from within the last 5 minutes exists, the banner is suppressed for the current session start. `/start_of_day` also reads this sentinel (see `/start_of_day/SKILL.md`) and skips its own banner if the hook fired within the last 5 minutes — preventing duplicate noise when both mechanisms are active.

## Fail-OPEN / non-aborting deploy contract

Every hook exits 0 on any error (no abort). If jq is absent, hooks fail-OPEN silently (zero protection — see jq soft-required dependency below). The `userpromptsubmit.sh` high-context advisory is only emitted AFTER the pending-prompt file is successfully written; if the write fails, the hook exits 0 (passthrough).

## Exact-token exempt-list for `userpromptsubmit.sh`

The hook splits the prompt on whitespace (after stripping ALL leading whitespace including newlines and carriage returns) and matches the FIRST token verbatim. Exempt commands (the hook exits 0 immediately without threshold check): `/checkpoint`, `/compact`, `/clear`, `/help`. These are exact string matches — not regex. `/checkpointfoo` and `/checkpoint--restore` are NOT exempt (different tokens). **Destructive-subcommand exception:** `/checkpoint --purge` is the ONE documented carve-out from the exempt-list — it is treated as NON-exempt despite the `/checkpoint` first-token match, and the hook falls through to threshold logic. Since `IVG-258 D-06` the fall-through ends in the high-context advisory rather than a refusal: the advisory adds a clause naming `--purge` as destructive, and the command still runs. The carve-out is kept so that clause is reached at all. All other `/checkpoint` subcommands (`--restore`, no-arg) remain exempt.

## STDIN capture pattern

All six event hooks open with `STDIN=$(cat)` to capture the JSON payload into a variable, then parse with `printf '%s' "$STDIN" | jq -r '<filter> // empty'`. The `// empty` jq filter handles missing fields by returning the empty string instead of `null`, supporting the fail-OPEN discipline.

## `--dry-run` (scoped to `router setup`, not `install`)

`--dry-run` is registered only on the `quoin router setup` subparser (`src/quoin/cli.py:966-970`) — printing what would change without writing any files. The `install` subparser has no such flag, and `quoin/install.sh` forwards its arguments straight to `python -m quoin` with no `--dry-run` handling of its own; `bash install.sh --dry-run` is an argparse error today, not a preview mode.

## jq soft-required dependency

Runtime hooks parse stdin JSON via `jq`. If `jq` is absent, hooks fail-OPEN silently (zero protection). Install via `brew install jq` (macOS), `apt-get install jq` (Debian), `apk add jq` (Alpine). `bash install.sh` emits a warning if jq is absent at deploy time AND writes `~/.claude/HOOK_MERGE_TODO.md` with manual-merge instructions; install proceeds but hooks will not function until jq is installed.

## R-09 mitigation (settings.json corruption)

`installer.py::deploy_hooks` backs up `~/.claude/settings.json` to `settings.json.bak-<timestamp>` and starts fresh if the existing file fails to parse as JSON, so a corrupted settings file cannot abort the merge silently.

## WorktreeCreate hook (`worktreecreate.sh`, IVG-116)

Registered as the eighth stanza (`WorktreeCreate`/`*`, timeout 10s). Fires when a source-mutating skill (`/implement`, `/rollback`, `/end_of_task`, `/pr`) dispatches an Agent with `isolation: "worktree"`. The hook reads the dispatch sidecar (`<project_root>/.workflow_artifacts/.dispatch-hint.json`), calls `git_root_for_dispatch.py --sidecar`, and when a single nested git repo resolves it decides a worktree path:

- **Self-generation (the IVG-116 fix):** when the harness omits `worktree_path`/`branch_name` on the hook's stdin (the pre-fix 100%-skip cause on Google-Drive-synced projects), the hook SELF-GENERATES `BRANCH_NAME="quoin/wt-<ts>-<pid>"` and a `WORKTREE_PATH` under `${TMPDIR:-/tmp}/quoin-worktrees` (outside the Drive-synced tree; project `.worktrees/` fallback), then runs `git worktree add` and prints the created path to stdout. The audit log (`worktree-hook-audit.log`) records `selfgen=1`.
- **Timeout:** `git worktree add` is wrapped in `timeout "${QUOIN_SUBPROCESS_TIMEOUT:-30}s"` via a `git_wt()` helper; if the `timeout` binary is absent the command runs unwrapped (fail-OPEN).
- **Fail-OPEN:** any failure (no nested repo, timeout, git error) exits 0 with NO stdout, so the harness/skill falls back to a plain no-isolation dispatch.
- **Opt-out:** `QUOIN_WORKTREE_SELFGEN=0` restores the old `rc=skip result=missing-worktree-path-or-branch` behaviour.

Whether isolation is even attempted is gated UPSTREAM by the `worktree_isolation.py --decide` STEP A0 in the four source-mutating SKILL.md (default `skip`; see `dispatch-guide.md`). The authoritative hook source is `quoin/quoin/hooks/worktreecreate.sh`; the copy at `quoin/quoin/adapters/claude/hooks/worktreecreate.sh` is a mirror kept byte-identical by a `test_worktreecreate_hook.py` assertion.

## Stop hook (`stop.sh`, IVG-280)

Registered as the ninth stanza (`Stop`/`*`, timeout 10s). Fires at the end of every assistant turn. Two shell-level early exits (no `.workflow_artifacts/memory` under the resolved project root, no `run-continue-arm-*.txt` file present) run before any Python process starts, so the common case — no armed run in this project — costs a couple of `ls`/`test` calls. When an arm file is present, the hook hands off to `python3 scripts/auto_resume.py stop --project-root <root>` with the Stop hook's own stdin (session_id, transcript_path, background_tasks, stop_hook_active) passed straight through.

- **What it does:** continues an interrupted `/run --autonomous` span in the session that armed it — either by emitting an in-session continuation block, or, once the in-session block count reaches `QUOIN_AUTO_RESUME_HANDOFF_AT`, by spawning a detached `quoin run --autonomous` supervisor and letting the turn end quietly.
- **Fail-OPEN:** any error, missing `python3`, or a helper that raises internally exits 0 with empty stdout — the harness's own Stop-block cap remains the outer bound regardless.
- **Opt-out:** `QUOIN_AUTO_RESUME=0` disables the helper entirely; the hook still exits early on the two shell-level checks either way, but the helper's own subcommands additionally no-op under the knob.
- **State files:** `run-continue-arm-<sid>.txt` (armed by `/run`'s Setup/Resume steps), `run-continue-consent-<sid>.txt` and `session-ended-<sid>.txt` (written by `userpromptsubmit.sh`/`sessionend.sh`), `auto-resume-<task>.json` (the continuation counter), `run-supervisor-<task>.pid`/`.result`/`.log` (the hand-off lock and its outcome) — all under `.workflow_artifacts/memory/`, all read or written exclusively by `scripts/auto_resume.py` and `src/quoin/cli.py`'s `run` subcommand.
- **Un-registering.** `QUOIN_AUTO_RESUME=0` is the supported opt-out (above) and needs no settings.json edit. To remove the stanza itself instead:

  ```bash
  python3 - <<'EOF'
  import json, pathlib, shutil

  settings_path = pathlib.Path.home() / ".claude" / "settings.json"
  backup_path = settings_path.with_suffix(settings_path.suffix + ".bak")
  shutil.copyfile(settings_path, backup_path)

  settings = json.loads(settings_path.read_text())
  stanzas = settings.get("hooks", {}).get("Stop", [])
  settings.setdefault("hooks", {})["Stop"] = [
      s for s in stanzas
      if not any(h.get("command", "").endswith("stop.sh") for h in s.get("hooks", []))
  ]
  settings_path.write_text(json.dumps(settings, indent=2) + "\n")
  print(f"Backed up to {backup_path}; Stop stanza(s) ending in stop.sh removed.")
  EOF
  ```

  To fully roll back state as well, delete the state files listed above for
  the task(s) in question — they are inert once the stanza is gone, but
  removing them clears any stale counter or lock before re-enabling the
  feature later.

## Tunable constants

Hook scripts read these values at runtime via `${QUOIN_*:-default}` parameter expansion. Defaults are baked into the scripts:

| Constant | Default | Env var override | Notes |
|----------|---------|-----------------|-------|
| `BPT` | `8.0` | `QUOIN_BYTES_PER_TOKEN` | Bytes per token for byte-count utilization estimate (V-03 calibrated) |
| `LIMIT` | `150000` | `QUOIN_EFFECTIVE_CONTEXT_LIMIT` | Effective token limit used as 100% denominator |
| `STOP_BPS` | `7000` | `QUOIN_STOP_BPS` | Advisory threshold in basis-points (7000 = 70.00%) |
| `BLOCK_BPS` | `9500` | `QUOIN_BLOCK_BPS` | High-context advisory threshold in basis-points (9500 = 95.00%) |
| `STALE_DAYS` | `7` | `QUOIN_STALE_SENTINEL_DAYS` | Days after which pending-prompt-*.txt / pending-restore-*.txt are swept; long-lived sessions may extend to 14+ |
| `POLLUTION_THRESHOLD` | `5000` | `QUOIN_POLLUTION_THRESHOLD` | Score threshold for pollution dispatch (score = transcript_kB + weighted tool-use count); 5000 ≈ 5MB transcript or ~1MB + heavy tool use |
| `SUBPROCESS_TIMEOUT` | `30` | `QUOIN_SUBPROCESS_TIMEOUT` | Seconds bounding `git worktree add` in `worktreecreate.sh` (and short git subprocesses in the core scripts); ~2× the observed Drive baseline. Fail-OPEN if the `timeout` binary is absent |
| `WORKTREE_SELFGEN` | `1` (on) | `QUOIN_WORKTREE_SELFGEN` | `0` disables hook self-generation of branch/worktree path (restores the old skip-when-harness-omits behaviour) |
| `RUN_STATE_STALE_DAYS` | `1` (`14` in `run_state_probe`) | `QUOIN_RUN_STATE_STALE_DAYS` | Run-state record freshness window in days. Two defaults by design — see `hooks-table.md:25-28` for the full two-window explanation |
| `PRECOMPACT_NORUN_CHECKPOINT` | `0` | `QUOIN_PRECOMPACT_NORUN_CHECKPOINT` | Opt-in no-run checkpoint+sentinel row; see `hooks-table.md:25-28` |
| `TELEMETRY_MAX_BYTES` | `1048576` | `QUOIN_TELEMETRY_MAX_BYTES` | Compaction-telemetry sink rotation size in bytes; see `hooks-table.md:25-28` |

`read_constants()` exports 13 names; the table above now covers 8 of them (`POLLUTION_THRESHOLD`, `SUBPROCESS_TIMEOUT` and `WORKTREE_SELFGEN` are read inline elsewhere and are not among the 13). `hooks-table.md` is the complete list, including the 5 exported names still missing here (`SESSIONSTART_SWEEP_DAYS`, `COMPACT_FIRST_BPS`, `PANIC_BPS`, `DISCOVERY_STALE_DAYS`, `SERENA_STALE_DAYS` — pre-existing omissions, backlogged) plus every skill-side and script-side knob.

**Basis-points convention:** Utilization values and threshold comparisons use INTEGER basis-points (0..10000) throughout. POSIX `[ ]` does integer comparison only; basis-points eliminate all floating-point comparison hazards. `compute_utilization()` in `_lib.sh` returns a basis-point integer (e.g., `8540` = 85.40% utilization).

## Compaction continuity (IVG-258)

Three shipped mechanisms keep a `/run` task resumable across a compaction:

- **The task-keyed run-state record** (`run-state-<task>.json` + `run-notes-<task>.md`) is written by `run_state_select`/`run_state_probe` in `_lib.sh` and is the source of truth `userpromptsubmit.sh` reads to detect an active `/run` task.
- **`precompact.sh`'s three-row always-allow table** — the hook always emits `{"decision": "allow"}` (it never blocks compaction); which of its three rows fires determines whether a checkpoint is written and which sentinel is left behind. See `hooks-table.md` for the full truth table.
- **The `SessionStart`/`compact` re-entry branch** in `sessionstart.sh` reads the sentinel `precompact.sh` left and re-hydrates the resuming session (banner, pending-restore prompt, or silent pass-through, depending on which row fired).

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
