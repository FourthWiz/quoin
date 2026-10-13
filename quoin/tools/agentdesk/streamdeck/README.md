# Quoin Agent Desk — Live Agents

Local macOS Stream Deck plugin for a 15-key Elgato, Ghostty and Quoin Agent Desk (Zellij).

## Layout

| 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|
| Agent 1 | Agent 2 | Agent 3 | Agent 4 | Agent 5 |
| Agent 6 | Agent 7 | Agent 8 | Agent 9 | Attention queue |
| Desk | + Claude | + Codex | Previous Ghostty tab | Next Ghostty tab |

An agent key shows its runtime, project folder, session suffix, pane number and observed status.
Blue: working. Orange/pulsing: permission or answer needed. Green: finished. Grey: idle.
“Нет сигнала” means there is an agent-labelled pane but no hook observation; it is not a guessed live status.
A short sound plays when a new agent enters the attention queue. Approval/answer buttons remain in the agent itself.

Press an agent to focus its pane. Press Attention to visit waiting agents in turn, including agents on other pages.
Hold Attention for at least 650 ms to display the next nine agents. Normal status changes do not reorder the list.

Desk opens or returns to the selected session, with two Codex panes and Shell for a new session.
It uses Agent Desk's normal saved-layout path; Agent Desk also includes its standard Spend tab.
Hold Desk for a menu: new Desk, choose project folder, attach session, add Shell, local Git changes.
“Project” means the local working folder, not a Google Drive service or a Codex project.
“Git” opens lazygit when available, otherwise a local change summary; it is not a GitHub website link.
New agent panes go into the mapped, selected Ghostty tab. Select an agent key or Desk first for an unmapped tab.

## Install

Prerequisites: Stream Deck 6.6+, Ghostty with its AppleScript API, Zellij with `list-panes --json` / `focus-pane-id`, installed Quoin Agent Desk, `/usr/bin/python3`, `/opt/homebrew/bin/node` with native WebSocket (tested Node 26). Pillow is required only to build assets.

1. Quit Stream Deck.
2. Run `/usr/bin/python3 install_live.py` from this directory.
3. Reopen Stream Deck and import `dist/live/Quoin Live Agents.streamDeckProfile`.
4. Select **Quoin — Live Agents**.
5. In Codex CLI, open `/hooks` and review/trust the new Quoin `monitor.py` command hooks. Codex skips untrusted hooks; the installer never bypasses hook trust.
6. Start fresh Claude Code/Codex sessions to ensure the new hook settings are loaded. Existing sessions may need to be resumed/restarted; do not terminate work in progress solely for installation.

Installer merges observers into `~/.claude/settings.json` and `~/.codex/hooks.json`, preserving existing handlers and unrelated settings. Re-running does not duplicate handlers. Runtime lives in `~/Library/Application Support/QuoinStreamDeck`; plugin lives in Stream Deck's Plugins folder. Timestamped backups are in the runtime `backups` directory. Hook trust is tied to command definitions; review changed definitions again if Codex asks.

## Scope and routing

Observation applies to Claude Code/Codex processes **inside Zellij Agent Desk**. It ignores desktop Codex threads, direct standalone terminal agents and explicitly identified subagents. Hooks store only status, process identity, project and routing metadata, never prompts, tool arguments or transcripts. They always return success with no output and never approve or block an action.

Ghostty surfaces created by Desk are recorded. For existing manually opened tabs, focus first tries a unique Zellij title `session | pane`. If no unique tab can be identified, it opens a new attachment to that exact session, then focuses the exact pane. This can add a tab for legacy sessions rather than guessing the active tab. Closed or unavailable panes fail with a message.

A stopped turn is green “Готово”; an explicit permission request, user question, interruption or failure is orange. Arbitrary questions embedded in plain assistant prose cannot be recognized as structured requests. Hook events from other tools are matched by tool-use id so an unrelated parallel completion does not clear a pending request. Complex subagent lifecycle attribution depends on the runtime's hook payload; this is a session-level indicator, not a subagent dashboard.

Changes take effect on the next polling cycle (normally around 2.5 seconds). `health.json` in the runtime reports the last inventory refresh. No external server, marketplace dependency, npm package or paid plugin is required.

## Validation and limitations

See `VALIDATION.md`. The plugin and test state transitions were verified in Stream Deck. End-to-end focus in Ghostty and physical key presses still require user verification because the computer-use tool denied access to Ghostty. A CLI/Zellij test verified the two-Codex-plus-Shell layout and exact pane focusing. Real Claude/Codex permission events have not yet been exercised interactively after hook trust review.

For rollback, quit Stream Deck, select the old profile on next start, remove this plugin folder, and remove only the `monitor.py` observers from the two settings files. Original settings backups can be restored only if there have been no later unrelated changes. The old static profile is retained. `build.py` / `install.py` are the legacy static-profile tools; use `build_live.py` / `install_live.py` for this version.

References: [Claude hooks](https://code.claude.com/docs/en/hooks), [Codex hooks and trust](https://learn.chatgpt.com/docs/hooks), [Ghostty AppleScript](https://ghostty.org/docs/features/applescript), [Elgato SDK](https://docs.elgato.com/streamdeck/sdk/references/manifest/).
