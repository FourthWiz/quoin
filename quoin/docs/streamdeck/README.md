# Quoin Stream Deck component

A live 15-key Stream Deck panel for the coding agents running in your Agent Desk sessions. It is optional: Quoin
installs and upgrades it only when you ask.

## What it does

The panel shows one key per agent and a few action keys:

- Keys 1-9 are agents. Each shows the runtime (Claude or Codex), the project, the session and pane, and a status
  colour: orange NEEDS YOU (the agent is waiting for you), blue WORKING (working), green DONE (finished its
  turn). Other states are grey (IDLE, NO SIGNAL, OFFLINE). Pressing an agent key focuses that agent's
  Zellij pane in Ghostty.
- Key 10 is the attention queue: it shows how many agents wait for you and the current page. A press focuses the
  next waiting agent; holding it turns to the next page of nine agents.
- The bottom row holds Desk (two Codex panes and a shell through Agent Desk; hold for its menu), + Claude and
  + Codex (a new pane), and previous and next Ghostty tab.

It is opt-in. `pip install quoin` and `quoin install` never install it; you run `quoin streamdeck install`
yourself. It works on macOS only. On any other platform `install` and `upgrade` exit 2, `status` prints
"unsupported platform" and exits 0, and `uninstall` has nothing to do.

## Prerequisites

`quoin streamdeck install` checks these first and then explains, for each missing item, what it is, why it
matters and the command it proposes.

| Item | Used for | How it is checked | Proposed install |
|---|---|---|---|
| Python 3.9 or later with `fcntl` | the hooks and the plugin's snapshot helper | the path you pass with `--python`, else the recorded one, else `python3` from `PATH`; `/usr/bin/python3` is only probed when `xcode-select -p` succeeds. A path inside the Python environment Quoin runs from (a virtual environment or `site-packages`) is never used. The unresolved path is what gets recorded | `brew install python` (or `xcode-select --install`) |
| Stream Deck app 6.6 or later | the panel itself | `Elgato Stream Deck.app` in `/Applications`, or in the folder named by `QUOIN_STREAMDECK_APPS_DIR`, with a Spotlight lookup as the fallback | `brew install --cask elgato-stream-deck` |
| Node with a global `WebSocket` | the plugin process | a `node` on `PATH` that provides it is preferred; with app 7.1 or later the app's bundled Node can be used (`--node bundled`) | `brew install node` |
| Agent Desk | launching and attaching sessions | `agentdesk.zsh` containing an `AGENTDESK_API_VERSION` line; it is searched, never sourced | redeploys the same files `quoin install` deploys |
| zellij with `list-panes` and `focus-pane-id` | observing and focusing panes | `zellij action --help` is probed | `brew install zellij` |
| Ghostty with its AppleScript dictionary | focusing windows and tabs | the app's scripting dictionary is read | `brew install --cask ghostty` |
| Homebrew | only to run the commands above | found on `PATH` or through a login shell; never installed for you | see https://brew.sh |

Python, the Stream Deck app and Node are core: without them the component installs dormant. Agent Desk, zellij and
Ghostty are features: the panel installs without them and the missing ones show as informational lines in
`status`.

How the questions work:

- After the checks, each missing item gets a `[y/N]` question. `--prereqs all` answers yes to every proposed
  install, `--prereqs none` answers no, and the default `ask` asks.
- Without a terminal nothing third-party is ever installed, whatever `--prereqs` says.
- Declined items are recorded and `status` lists them as "not installed by choice".
- A core item missing on a fresh install produces a dormant install: the code, manifest, config and profile are
  written, but no plugin and no hooks. An active install is never deactivated without a yes on a terminal.

## Commands

```
quoin streamdeck install
quoin streamdeck upgrade
quoin streamdeck status
quoin streamdeck uninstall
```

Examples with options:

```
quoin streamdeck install --dry-run
quoin streamdeck install --python /path/to/python3 --prereqs none
quoin streamdeck status --json
quoin streamdeck uninstall --purge --state-root DIR
```

Options, by command:

- `install`, `upgrade`: `--runtime-dir`, `--plugins-dir`, `--state-root`, `--claude-settings`, `--codex-home`,
  `--agentdesk`, `--runtimes`, `--python`, `--node`, `--prereqs`, `--dry-run`, `--open-profile`, `--no-open-profile`,
  `--allow-downgrade`.
- `status`: `--runtime-dir`, `--plugins-dir`, `--state-root`, `--claude-settings`, `--codex-home`, `--agentdesk`,
  `--json`.
- `uninstall`: `--runtime-dir`, `--plugins-dir`, `--state-root`, `--claude-settings`, `--codex-home`,
  `--agentdesk`, `--runtimes`, `--dry-run`, `--purge`.

What they do:

- `install` is a fresh install, a repair, the adoption of a prototype install, or an upgrade when the manifest is
  older than the package (it says so). A manifest that is newer than the package is refused unless you pass
  `--allow-downgrade`.
- `upgrade` needs an existing install or a prototype. Otherwise it stops with
  "not installed; run `quoin streamdeck install`".
- `--runtimes` on `install` and `upgrade` defaults to both Claude and Codex; Codex is managed only when it is
  detected (on `PATH`, `CODEX_HOME`, a `~/.codex` folder, existing entries, or a recorded earlier install). An
  explicit shorter list removes the omitted runtime's entries. On `uninstall`, `--runtimes` removes only those
  runtimes' entries and keeps the component.
- `--dry-run` takes no lock and writes nothing (prerequisites are treated as `none`), and it reports a pending
  roll back or completion.
- `--open-profile` always opens the generated profile after the install; `--no-open-profile` never does. Without
  either, `install` opens it only when Stream Deck does not have it yet (see Profile).
- `--python PATH` and `--node auto|bundled|PATH` choose the interpreter and Node instead of discovering them.
- `--json` makes `status` print its report as JSON: `state`, `problems`, `info` and `exit_code`.

Exit codes of `install`, `upgrade` and `uninstall`: 0 success (including a dormant install and an adoption that is
pending), 1 refused or failed, 2 unsupported platform or a usage error.

Exit codes of `status`: 0 healthy (or unsupported platform), 10 not installed (including when only inert hook stubs
are left), 11 problems found, 12 an operation was interrupted.

## Upgrading

`pip install --upgrade quoin` changes the package payload only. It does not change the installed component, the
hooks, the plugin, the profile or the state: those live in the runtime folder, which the hooks and the plugin
reference directly. They never point into `site-packages` or a virtual environment, so upgrading with pip or
rebuilding the virtual environment never breaks a running deck.

When the installed component differs from the package, `quoin install` prints one line saying so. Run
`quoin streamdeck upgrade` to bring the component up to date. It is idempotent: when nothing changed, a re-run only
refreshes `last_verified_at` in the manifest. The hook command is stable, so an upgrade normally needs no new Codex
review; the output says when it does.

## Where things live

Runtime folder (default `~/Library/Application Support/QuoinStreamDeck`):

- `install-manifest.json`: what is installed and where.
- `operation.json`: only while an operation is running or was interrupted.
- `.install.lock`: held while an operation runs.
- `versions/<version>-<12 hex>/`: the deployed payload, with `current` pointing at the live one.
- `bin/quoin-deck-hook` and `bin/quoin-deck-terminal`: the entry points hooks and the plugin call.
- `profile/Quoin Live Agents.streamDeckProfile`: the profile to import.
- `staging/`: transient, empty between runs.
- `backups/quoin-<UTC timestamp>[-NN]/`: the newest five reference copies of your settings files from before each
  change. They are never restored automatically and `--purge` keeps them (delete them by hand), so the runtime
  folder itself survives a purge.
- `uninstalled.json` and inert stubs: left by `uninstall` so running agents keep working.
- Legacy forwarders (`monitor.py`, `dashboard.py`, `controller.py`, `terminal.zsh`) after adopting a prototype.
- Untouched leftovers from a prototype: older `backups/` entries, `Buttons/`, `prototype-backup.sdProfile`, a root
  `health.json`, `.DS_Store`.

Plugin folder: `local.quoin.agentdesk.sdPlugin` inside the Stream Deck plugins folder, holding `manifest.json`,
`plugin.js`, the icons, `plugin-config.json`, and `launch.sh` only when an external Node is used.

Config: `~/.config/agentdesk/streamdeck/config.json` (mode 0600: project, session, projects).

State: `~/.config/agentdesk/streamdeck/state/` (mode 0700): `agents/` records, `health.json`, `errors.log` plus one
rotation, and `surfaces.json`. A legacy `monitor.lock` or `controller.lock` stays in the state root. `--purge`
removes a legacy `monitor.lock` but leaves a legacy `controller.lock`, so an adopted install's state root may
survive a purge.

Overrides: the flags above, and these environment variables: `QUOIN_STREAMDECK_RUNTIME_DIR`,
`QUOIN_STREAMDECK_PLUGINS_DIR`, `QUOIN_STREAMDECK_STATE_ROOT`, `QUOIN_STREAMDECK_APPS_DIR`, `CLAUDE_CONFIG_DIR`
(core `quoin install` ignores it) and `CODEX_HOME`. After an install, `upgrade`, `uninstall` and `status` use the
recorded paths; a shell value that differs prints a note, and a flag overrides.

## Hooks and trust

The component adds one managed entry per event, in its own group without a matcher:

```
{"type":"command","command":"'<runtime>/bin/quoin-deck-hook' claude","timeout":3}
```

(`codex` instead of `claude` for Codex.) The events are:

- Claude Code (14): SessionStart, SessionEnd, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, Stop,
  PreCompact, PostCompact, PostToolUseFailure, Notification, StopFailure, Elicitation, ElicitationResult.
- Codex (10): SessionStart, SessionEnd, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, Stop,
  PreCompact, PostCompact, Interrupt.

Only the component's own entries are added, replaced in place or removed. Foreign entries, Quoin's own stanzas,
other keys and their order are untouched, and no extra JSON keys appear. A first install appends its entries at
the end. Files are written atomically to the symlink target with their mode kept. A settings file that is
malformed or unreadable stops the whole operation and is left unchanged; `uninstall --runtimes <other>` is the
escape hatch. A hook run always exits 0 and prints nothing.

Codex: Quoin never writes trust. Review changed entries in Codex `/hooks`. Migrating from the prototype changes
the definitions once. Uninstalling can shift later foreign entries, and Codex then asks again; the output lists
the events. Claude Code: new sessions pick up the changes; review in `/hooks` if you are asked.

## Profile

The generated profile is `profile/Quoin Live Agents.streamDeckProfile` in the runtime folder. An active `install`
opens it in Stream Deck, which imports it, unless Stream Deck already holds a "Quoin — Live Agents" profile (or a
renamed copy of it); then select it on your device. `upgrade` never opens it, so a profile you deleted stays deleted.
`--open-profile` opens it even when it is already imported, and `--no-open-profile` never opens it; you can also
double-click the file. Quoin never edits or deletes profiles. The plugin and action UUIDs are fixed, so an
imported "Quoin — Live Agents" profile keeps working across upgrades.

A Quoin key dragged onto another profile by hand has no role or slot, so it always shows the first agent. Use the
generated profile, or set each key's settings to match it.

Restart Stream Deck to load a new plugin version. Quoin never quits or restarts it; the output tells you when it
is running.

## Status and problem codes

`quoin streamdeck status` reads the manifest, the marker, the files, both settings files, the recorded binaries
and the plugin's health file, and changes nothing. With `--json` it prints `state`, `problems[]`, `info[]` and
`exit_code`.

Problems (exit 11, or 12 for an interrupted operation):

| Code | Meaning and action |
|---|---|
| `OPERATION_INCOMPLETE` | an operation was interrupted; the detail names the exact command that completes it |
| `MANIFEST_INVALID` | the manifest does not validate; fix it or run `quoin streamdeck uninstall` |
| `PROTOTYPE_UNADOPTED` | the prototype install is present and not yet adopted; run `quoin streamdeck install` |
| `SETTINGS_UNREADABLE` | a settings file could not be read |
| `SETTINGS_MALFORMED` | a settings file is not valid JSON of the expected shape |
| `FILE_MISSING` | a file the manifest lists is gone or is not a regular file |
| `FILE_MODIFIED` | a listed file no longer matches its recorded checksum |
| `MODE_DORMANT` | installed without plugin and hooks because core prerequisites are missing |
| `MODE_ADOPTION_PENDING` | the prototype was left untouched because core prerequisites are missing |
| `CURRENT_MISSING` | `current` is missing or not a symlink |
| `CURRENT_MISMATCH` | `current` points at a different version than the manifest records |
| `HOOKS_NONE_RECORDED` | an active install with no recorded hook entries |
| `HOOK_MISSING` | a managed hook entry is gone from a settings file |
| `HOOK_DUPLICATE` | a managed hook entry appears more than once |
| `HOOK_STALE` | a managed hook entry differs from the expected command, or sits on an event the component does not use |
| `HOOK_SHADOWED` | a managed hook entry shares its group with a matcher or other hooks, so it may not run for every event |
| `PREREQ_CORE_MISSING` | a core prerequisite recorded as missing |
| `BINARY_VANISHED` | a recorded binary no longer exists or is not executable |
| `HEALTH_STALE` | the plugin's health file is older than five minutes: is Stream Deck running? |

Informational lines (they do not change the exit code):

| Code | Meaning |
|---|---|
| `PATHS_DIFFER` | this shell resolves a path differently from the recorded one; `status` uses the recorded path |
| `UPGRADE_AVAILABLE` | the installed component differs from the package; run `quoin streamdeck upgrade` |
| `UNMANAGED_LEFTOVER` | a prototype leftover that Quoin does not manage |
| `CODEX_TRUST` | whether a Codex trust record exists for each managed entry |
| `PREREQ_FEATURE_UNAVAILABLE` | a feature prerequisite you chose not to install |
| `OPTIONAL_TOOL_MISSING` | an optional tool was not found, or no longer exists |
| `PLUGIN_NEVER_RAN` | no health file yet: start Stream Deck |
| `HEALTH_UNREADABLE` | the health file exists but cannot be read |
| `PLUGIN_SNAPSHOT_ERROR` | the plugin's last snapshot failed; the error type is named |
| `PROFILE_NOT_IMPORTED` | Stream Deck has no Quoin Live Agents profile; the file to open is named |
| `SETTINGS_UNREADABLE`, `SETTINGS_MALFORMED` | the same two codes, but only informational when nothing is installed |

For an installed component `SETTINGS_UNREADABLE` and `SETTINGS_MALFORMED` are problems; when nothing is installed
they are informational.

## Interrupted runs and recovery

Every install, upgrade and uninstall writes `operation.json` while it works.

- A run interrupted before its commit point is rolled back by the next run, which says
  "rolled back an interrupted operation".
- A run interrupted after it is completed by the next run, which says "completing an interrupted operation".
  `status` reports exit 12 until then and names the command.
- "another quoin streamdeck operation is running" means another run holds the lock; wait and retry.
- A run that waited for the lock and found the state changed stops with "another quoin streamdeck operation
  changed the install state while this one was waiting; re-run the command".
- The warning "ignored an unexpected path in the operation record" means the record named a path outside the
  folders the component owns, or a link out of them; that path was skipped and never deleted.
- "manifest invalid ...; fix it or run `quoin streamdeck uninstall`" means the manifest cannot be read as an
  install record.
- A manifest that records a different runtime folder is followed only when that folder holds a matching manifest;
  otherwise the run stops and asks for `--runtime-dir`.
- `uninstall --purge` refuses when the state is recorded somewhere other than this shell resolves, and names the
  `--state-root` to pass.
- A symlinked `state` or `agents` folder is refused rather than followed; replace the link with a real folder and
  re-run.

## Migrating the prototype install

This is a manual sequence; nothing is stopped or restarted for you.

1. Upgrade the package (`pip install --upgrade quoin`, or install from your checkout) and run `quoin install`, so
   Agent Desk gets its public launch interface.
2. `quoin streamdeck status`. Expect `PROTOTYPE_UNADOPTED` and exit 11.
3. `quoin streamdeck install --dry-run` to see the plan.
4. In a terminal, run `quoin streamdeck install` and answer the prerequisite questions.
5. `quoin streamdeck status` should now exit 0 (informational lines are fine).
6. In Codex, open `/hooks` and review and trust the changed entries once.
7. Claude Code: new sessions use the managed hooks; running ones keep working through the forwarders.
8. Restart the agents the output lists when convenient, and restart Stream Deck when convenient. The
   "Quoin — Live Agents" profile needs no re-import.
9. Check the keys.

Adoption keeps your config selections, merges the agent records into `state/agents`, moves surfaces to
`state/surfaces.json` and `errors.log` into the state folder, and leaves the legacy locks, `backups/`, `Buttons/`,
`prototype-backup.sdProfile` and the root `health.json` alone.

If a core prerequisite is missing the outcome is adoption pending: the prototype is left exactly as it was and
`status` exits 11. Fix the missing item and re-run `quoin streamdeck install`.

## Uninstall and rollback

`quoin streamdeck uninstall` removes the hook entries, the plugin (when its UUID matches), the versions, `current`,
the forwarders and the manifest. It leaves inert stubs where running agents still call the old hook paths, and it
keeps your config and state. A second run says "nothing to remove".

- `--purge` also removes the stubs, the config and the state. Restart the agents first; the output lists them.
  It keeps `backups/` in the runtime folder and a legacy `controller.lock` in the state root, so remove those by
  hand if you want nothing left.
- `--runtimes claude` or `--runtimes codex` removes only that runtime's entries and keeps the component.
- `--dry-run` prints what would be removed.
- Profiles are never removed; delete the profile in the Stream Deck app.

There is no automatic return to the prototype; its older `backups/` stay for reference.

## Privacy

Agent records keep only status and routing metadata: id, kind, session, pane, socket, project, status, pending
markers, the last event name, timestamps, the process id and its start time, and how the record arrived. They never
hold prompts, tool input or output, assistant text, transcript paths or notification bodies.

`errors.log` holds a timestamp, the exception type and a one-line message of at most 200 characters.
`health.json` holds `updated`, the agent count and the failure type, never a message.

## Troubleshooting

- "not found" inside panes the deck starts: in those sessions the agents and their own subprocesses (MCP servers
  through `npx` or `uvx`, `gh`, `jq` and so on) see only the directories of the recorded binaries and tools
  (python, node, zellij, claude, codex, ccr, lazygit, python3) plus the `PATH` Stream Deck was launched with, which
  is launchd's minimal one. Run `quoin streamdeck upgrade` from a shell where those tools resolve so they are
  recorded, or install them next to a recorded binary. Tools that resolve only inside the Python environment
  Quoin runs from (its virtual environment, or a `site-packages` path) are never recorded, so rebuilding that
  environment cannot change the agents' `PATH`.
- "Agent Desk is not installed" on a key press: Agent Desk is missing; `quoin streamdeck install` proposes it.
- The plugin does not show up: restart Stream Deck, then check `status` for `HEALTH_STALE` or `PLUGIN_NEVER_RAN`.
- `status` reports `MODE_DORMANT`: a core prerequisite was missing at install time; fix it and re-run
  `quoin streamdeck install`.
- Key presses are ignored while a picker or dialog from a previous press is open. That is by design.
- `agentdesk --if-exists attach` needs a terminal. The deck always starts it inside Ghostty; scripts without a
  terminal should use `--if-exists new` or `--if-exists fail`.
- "no usable python is recorded" from `install` or `upgrade` means the deck would stay active without a working
  interpreter. No component files, hooks or plugin were changed. Re-run with `--python PATH` (Python 3.9 or later),
  or run `quoin streamdeck uninstall`.
- The warning "ignored an unusable binary record in the operation record" after an interrupted run is
  informational: that record was dropped and this run checked the tool again.

## What is still checked by hand

Everything that needs real Stream Deck hardware, a trusted Codex session or a real Zellij session is verified by
hand, not by the automated tests. [`VALIDATION.md`](VALIDATION.md) separates what the tests cover, what was checked
manually, and what is not yet verified, with the steps to check each remaining item.
