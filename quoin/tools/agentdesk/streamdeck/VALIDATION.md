# Local validation — 2026-10-12

- 13 Python unit tests passed: exact session routing, safe argument boundaries, cancelled selection, persistence, parallel permission/other-tool events, user questions, hook metadata minimization, idempotent configuration merge, closed-pane refusal.
- Python modules compiled; zsh syntax and Node JavaScript syntax passed.
- Nine complete AppleScript handlers compiled with the installed Ghostty dictionary (compile only).
- Stream Deck 7.6 loaded and connected `local.quoin.agentdesk`, imported and selected **Quoin — Live Agents**, and rendered all 15 keys.
- Synthetic, explicitly TEST-ONLY hook events on two real disposable Zellij panes visibly produced orange attention + blue working, then green done with attention count 1 → 0. Fixtures removed after verification.
- Separate Zellij CLI session verified Agent Desk's generated main tab has Codex, Codex, Shell, and its standard Spend tab. `focus-pane-id terminal_2` succeeded. Test session killed and deleted after validation.
- Codex CLI 0.146.0 exposes hook trust support. New hooks were installed but not trusted by the installer.
- NOT verified: real interactive hook delivery from trusted Codex or a newly started Claude session; physical hardware key presses; final Ghostty tab/pane focusing. Computer-use access to Ghostty was explicitly denied. No alternate UI inspection was used.
- First launch test exposed Agent Desk's rejection of `--name` combined with positional pane types. Fixed to seed the named saved layout before normal `agentdesk --name` startup. The CLI layout test uses the corrected path.
