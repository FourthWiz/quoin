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
physical key. Esc and Enter also leave every mode except locked.

| Mode | Old key | New key | Russian layout | Leave the mode |
|------|---------|---------|----------------|----------------|
| locked | Ctrl g | Alt g | Alt п | Alt g, Alt п, Ctrl g, Ctrl п |
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
5. `Alt g` locks Zellij: `Alt s` then `q` does nothing while locked. `Ctrl g`
   (or `Alt g`) unlocks. Under the Russian layout `Ctrl п` unlocks as well.
6. In a pane running Claude Code: `Ctrl b`, `Ctrl t`, `Ctrl g`, `Ctrl s` and
   `Ctrl o` reach Claude Code; `Alt p` opens the model picker and `Alt o` toggles
   fast mode.
7. In a shell pane: `Ctrl p` and `Ctrl n` walk shell history, `Ctrl h` is
   backspace.
8. `Option+Left` and `Option+Right` move by word in the shell prompt.
9. In a locked session `Ctrl g` unlocks it, so you can always get back to normal.

### Per input source

| Check | ABC | Russian |
|-------|-----|---------|
| `Alt s` opens session mode | `Alt s` | `Alt ы` |
| `Alt g` locks | `Alt g` | `Alt п` |
| `Ctrl g` unlocks | `Ctrl g` | `Ctrl п` |
| quit from session mode | `q` | `й` |

Which character and encoding Ghostty sends under the Russian layout can differ
by version; if an alias does not react, note it and use the Latin layout for
that chord.

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
sessions from the command line instead:

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
| Alt b, d, f, t, w | readline and Claude Code word and toggle keys | not used |

## Decision record

An "unlock-first" preset (every mode behind a single unlock key) was evaluated
and not adopted: it replaces the whole interaction model, cannot be undone by a
marker-scoped revert, and depends on the same Option-as-Alt setting.
