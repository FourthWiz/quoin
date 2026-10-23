# Agent Desk keybinding checklist

`setup-agentdesk.sh` changes two things so that Claude Code, Codex and your
shell receive the keys they expect while Zellij keeps a full set of mode keys:

- **Ghostty**: the Option key has to send Alt (`macos-option-as-alt`), because
  every Zellij mode key below is an Alt chord.
- **Zellij**: a marked block is added at the end of the `keybinds` node in
  `~/.config/zellij/config.kdl` that frees the Ctrl keys and adds the Alt keys.
  Every line it adds ends with `// [agentdesk: added]` or
  `// [agentdesk: freed]`, so the change can be found and removed.

## Prerequisites

1. Let Option send Alt in Ghostty. Either let setup do it, or add this line to
   your Ghostty config yourself (`~/.config/ghostty/config` or
   `config.ghostty`, or the same file name under
   `~/Library/Application Support/com.mitchellh.ghostty/`):

   ```
   macos-option-as-alt = left
   ```

   `left` keeps the right Option key free for accented letters and symbols.
   `true` makes both keys send Alt.
2. Reload Ghostty (`cmd+shift+,`) or restart it. A change is not picked up until
   then.
3. Run the keybinding step again so the Ctrl keys are freed:

   ```sh
   bash ~/.config/agentdesk/setup-agentdesk.sh --keybinds-only
   ```

   Until Alt delivery is confirmed, setup only removes `Ctrl q` (the accidental
   quit) and leaves the Ctrl mode keys alone ("tier 1"). Once Option sends Alt
   it also frees them ("tier 2"). If you know Option already sends Alt, pass
   `--assume-alt`.

## Key map

Alt chords work on the Latin layout; the Russian layout has an alias on the same
physical key. On a stock-default config, Esc and Enter also leave every mode except locked; a custom config that clears the defaults may not keep them.

| Mode | Old key | New key | Russian layout | Leave the mode |
|------|---------|---------|----------------|----------------|
| locked | Ctrl g | Alt g | Alt п | Alt g, Alt п, Ctrl п (Ctrl g only on configs that keep Zellij's default keys) |
| pane | Ctrl p | Alt a | Alt ф | Alt a, Alt ф, Esc, Enter |
| tab | Ctrl t | Alt x | Alt ч | Alt x, Alt ч, Esc, Enter |
| resize | Ctrl n | Alt r | Alt к | Alt r, Alt к, Esc, Enter |
| move | Ctrl h | Alt m | Alt ь | Alt m, Alt ь, Esc, Enter |
| scroll | Ctrl s | Alt v | Alt м | Alt v, Alt м, Esc, Enter |
| session | Ctrl o | Alt s | Alt ы | Alt s, Alt ы, Esc, Enter |
| tmux | Ctrl b | Alt z | Alt я | Alt z, Alt я, Esc, Enter |
| quit | Ctrl q | Alt s, then q | Alt ы, then й | not applicable |

Existing Alt keys keep working and get Russian-layout aliases on the same
physical key:

| Action | Alt key | Russian layout |
|--------|---------|----------------|
| floating panes | Alt f | Alt а |
| new pane | Alt n | Alt т |
| focus left | Alt h | Alt р |
| focus down | Alt j | Alt о |
| focus up | Alt k | Alt л |
| focus right | Alt l | Alt д |
| move tab left | Alt i | Alt ш |
| previous swap layout | Alt [ | Alt х |
| next swap layout | Alt ] | Alt ъ |

Keys freed in normal mode at tier 2, so they reach the focused program:
`Ctrl t`, `Ctrl b`, `Ctrl g`, `Ctrl s`, `Ctrl p`, `Ctrl n`, `Ctrl h`, `Ctrl o`,
`Alt Left`, `Alt Right`, `Alt p` (Claude Code model picker) and `Alt o` (Claude
Code fast-mode toggle). The Ctrl keys still switch modes from inside the other
Zellij modes (for example `Ctrl t` leaves tab mode). `Alt p` and `Alt o` get no
Russian-layout alias, so Alt з and Alt щ stay unbound.

`Ctrl q` is removed at both tiers. The only quit left is `q` (or `й`) inside
session mode.

## Manual checks

Open a Zellij session from Ghostty (`agentdesk`) and walk the list. Run it once
with the ABC input source and once with Russian.

1. Reload the config if the session was already running: `zellij action reload-config`
   (or start a new session).
2. `Ctrl q` does nothing. The session stays open.
3. `Alt s`, then `q` quits Zellij. (Russian: `Alt ы`, then `й`.) Do this last.
4. Each mode key enters its mode and the same key leaves it again: Alt a, Alt x,
   Alt r, Alt m, Alt v, Alt s, Alt z. In tab mode, `n` opens a tab only while the
   mode is active.
5. `Alt g` locks Zellij: `Alt s` then `q` does nothing while locked. `Alt g`
   unlocks. Under the Russian layout `Alt п` and `Ctrl п` unlock as well. `Ctrl g`
   unlocks only on a config that keeps Zellij's default keys.
6. In a pane running Claude Code: `Ctrl b`, `Ctrl t`, `Ctrl g`, `Ctrl s` and
   `Ctrl o` reach Claude Code; `Alt p` opens the model picker and `Alt o` toggles
   fast mode.
7. In a shell pane: `Ctrl p` and `Ctrl n` walk shell history, `Ctrl h` is
   backspace.
8. `Option+Left` and `Option+Right` move by word in the shell prompt.
9. In a locked session `Alt g` unlocks it, so you can always get back to normal.

### Per input source

| Check | ABC | Russian |
|-------|-----|---------|
| `Alt s` opens session mode | `Alt s` | `Alt ы` |
| `Alt g` locks | `Alt g` | `Alt п` |
| unlock | `Alt g` | `Alt п` or `Ctrl п` |
| quit from session mode | `q` | `й` |

Which character and encoding Ghostty sends under the Russian layout can differ
by version; if an alias does not react, note it and use the Latin layout for
that chord.

## If the keyboard seems stuck

Work down this list; stop at the first step that helps.

1. Read the mode name in the status bar. `LOCKED` means keys go straight to the
   pane and the mode keys are off: press `Alt g` (Russian: `Alt п` or `Ctrl п`; `Ctrl g`
   only on a config that keeps Zellij's default keys) to leave it. Rename and
   search modes are left with `Esc` or `Enter`, not an Alt key. Any other mode
   name: press the same Alt key that entered it again (for example `Alt s` for
   session mode), or `Esc` on a config that keeps the default exit keys.
2. Look for a floating or plugin pane in front of the others. `Esc` closes a plugin pane;
   `Alt f` hides floating terminal panes.
3. If `Alt` does nothing at all, Ghostty is not sending Option as Alt. Reload the
   Ghostty config, then run `bash ~/.config/agentdesk/setup-agentdesk.sh --keybinds-only`
   and use the left Option key (the right
   one types accents and symbols).
4. `Ctrl+Q` does nothing by design: it is unbound so a stray press cannot end a
   session. Quit with `Alt s`, then `q`.
5. From another terminal run `quoin sessions` to list the sessions and
   `quoin attach NAME` to get a second client in the same session. Then close the
   stuck terminal tab or window; the session keeps running and the new client
   stays attached. (If the Alt keys still work in the stuck tab, `Alt s`, then
   `d` detaches that client, but a stuck client usually takes no keys. Do not
   press them in the new client: that detaches the working one.)
6. Last resort: `quoin sessions kill NAME --force` ends the session and
   everything running in it.
7. If the terminal itself stays garbled after Zellij is gone (typed keys echo as
   escape sequences), run `reset`.

The investigation into reports of a frozen keyboard found no cause in the
launcher or the keybinding region: a new session started with the Agent Desk
layout shows no startup tips or release notes, every mode can be left from the
merged config, and typed input reached the focused pane at once. The cause is
therefore still open and is tracked separately; the steps above are the way out
in the meantime.

## Verifying the config

```sh
zellij --config ~/.config/zellij/config.kdl setup --check
grep -n 'agentdesk: ' ~/.config/zellij/config.kdl
ls -la ~/.config/zellij/config.kdl.keybinds.*.bak
```

Expected: no parse errors, the marked lines, and at least one timestamped
backup from the first change. Running the step a second time prints
`Unchanged:` and creates no new backup.

## Without working Alt keys (tier 1)

If Option does not send Alt in your terminal, the Alt mode keys cannot be typed.
The Ctrl mode keys stay as they were and only `Ctrl q` is removed. Manage
sessions from the command line instead.

If an earlier setup commented out `Ctrl o` (session mode) in your config, it stays
commented out at this tier, so session mode is only reachable with `Alt s`. Until
Option sends Alt, use the command line:

```sh
zellij action detach          # from inside a pane
zellij kill-session NAME
agentdesk-delete NAME
```

## Reverting

```sh
bash ~/.config/agentdesk/setup-agentdesk.sh --revert-keybinds
```

This removes the marked block and restores any line commented out with
`// [agentdesk: freed]`, leaving the file as it was before the first change.
Backups sit next to the config as `config.kdl.keybinds.*.bak`. The Ghostty edit
is not undone automatically: the command prints which file holds the
`macos-option-as-alt` line and where its backup is
(`<ghostty config>.agentdesk.*.bak`). Delete the line, or restore the backup,
and reload Ghostty.

## Why Alt, and why these keys

Collision check for the new chords, against Zellij's own Alt binds, Claude Code's
default Meta binds and the zsh emacs Meta binds. Letters that type a dead key on
the ABC Option layer (`e`, `i`, `n`, `u`) are avoided, because a stray non-Alt
Option press would silently swallow the next keystroke.

| Key | Collision | Verdict |
|-----|-----------|---------|
| Alt a | zsh `accept-and-hold` (rare) | used for pane |
| Alt g | zsh `get-line` (rare) | used for lock |
| Alt m, Alt r, Alt v | none | used for move, resize, scroll |
| Alt x, Alt z | rare zsh widgets | used for tab, tmux |
| Alt s | Agent Desk session mode (kept) | used for session |
| Alt p, Alt o | Zellij group toggle / tab move vs Claude Code model picker / fast mode | freed at tier 2 |
| Alt b, d, t, w | readline and Claude Code word and toggle keys | not used |
| Alt f | Zellij floating panes (stock bind, with a Russian alias) | kept |

## Decision record

An "unlock-first" preset (every mode behind a single unlock key) was evaluated
and not adopted: it replaces the whole interaction model, cannot be undone by a
marker-scoped revert, and depends on the same Option-as-Alt setting.

## Layout checks

Manual checks of the generated layout. Run them in a real terminal after
installing; each row says what you should see.

| Check | Expected |
|-------|----------|
| `agentdesk claude claude shell` | Two Claude panes (`Claude · A`, `Claude · B`) side by side on top, a full-width `Shell` strip below. |
| Token Spend at start | Not visible. |
| `Alt f` (Russian layout: `Alt а`) | Shows Token Spend as a floating pane; the same key hides it again. If Option is not delivered as Alt, use pane mode then `w`. |
| Stream Deck "floating" key | Does the same as `Alt f`. |
| `Alt ]` (Russian layout: `Alt ъ`) | Cycles the arrangement: even, focus-left, stacked, then back to the start shape. `Alt [` (Russian layout: `Alt х`) cycles backwards. Focus-left needs at least two tiled panes. |
| `Alt n` in a fresh desk | Splits the focused pane and does not rearrange the others. |
| Stream Deck "Desk" key | Creates `Codex · A`, `Codex · B` and `Shell`; its agent buttons find both Codex panes. |
| `agentdesk --compact-bar` | One bar line at the bottom instead of the tab bar and status bar. On the Standard layout every tab shows the single bar line. |
| `agentdesk --pick`, option 1 | Opens three tabs: main, git and status. |
| Git tab in a single-repo project | lazygit opens on that repo; `q` leaves lazygit and gives a shell. |
| Git tab in a root with several repos | A refreshing status view with a `===== repo =====` block per repo; any key gives a shell. |
| Status tab | The pipeline graph, or a message to run quoin install when the script is missing. Ctrl+C stops the watch and leaves a shell. |
| `agentdesk --pick`, option 6 | Claude on top with a Shell 60 / Status 40 strip below. |
| Edit `~/.config/zellij/layouts/agent-desk.kdl`, launch again | Your edits are kept; setup writes the new Standard layout beside it as `agent-desk.kdl.new`. |
| Dashboard tab (when enabled) | The tab has its own tab bar and status bar (or the compact bar with `--compact-bar`). |
| Floating Codex helper open, then `Alt f` | The floating layer also shows the 80% x 75% Token Spend pane behind the helper. This is accepted; hide it with the same key. |

Swap layouts fill panes in Zellij's own pane order, so the even arrangement can
place the shell between two agents.
