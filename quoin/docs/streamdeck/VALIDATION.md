# Stream Deck component: what is verified

- Date: 2026-10-14
- Component version: 1.0.0
- Quoin version: 0.45.0
- Branch: `feat/streamdeck-optional-component`, HEAD at the time of writing: `3bccb267`

This record separates three things: what the automated tests prove, what was checked by hand, and what has not been
checked yet. Nothing here has been published; the package and the component exist only on this branch.

## Verified by automated tests

All tests run without touching the real home folder, Stream Deck, launchd or a live Zellij session, except where
the table says a child process runs with a temporary home.

| Area | Test files | What is asserted |
|---|---|---|
| Agent Desk public launch | `test_agentdesk_public_launch.py`, `test_agentdesk_help.py`, `test_agentdesk_zsh_args.py`, `test_agentdesk_setup_flag.py`, `test_agentdesk_deploy.py`, `test_agentdesk_keybinds.py`, `test_agentdesk_mktemp_templates.py` | the launch interface the deck uses (`--name`, `--panes`, `--no-prompt`, `--if-exists`), its help text, argument parsing, the setup flag and what `quoin install` deploys |
| Payload: monitor | `test_streamdeck_monitor.py`, `test_streamdeck_privacy.py` | record bounds and shape, deadlines, and that no prompt, tool data, assistant text, transcript path or notification body is stored |
| Payload: dashboard and controller | `test_streamdeck_dashboard.py`, `test_streamdeck_controller.py`, `test_streamdeck_terminal.py` | agent snapshots, garbage collection of dead records, pane addressing, controller actions and the Ghostty terminal entry |
| Payload: shared environment and plugin | `test_streamdeck_deckenv.py`, `test_streamdeck_plugin_js.py`, `test_streamdeck_plugin_grouping.py`, `test_streamdeck_payload_static.py`, `test_streamdeck_payload_build.py`, `test_streamdeck_payload_runtime.py`, `test_streamdeck_ported.py` | the Zellij socket directory rule, plugin invocation and health file, session grouping, the open-session view and key handling, Python 3.9 syntax of the payload, plugin and profile generation, and behaviour carried over from the prototype |
| Lifecycle library | `test_streamdeck_paths.py`, `test_streamdeck_jsonfile.py`, `test_streamdeck_hooks_merge.py`, `test_streamdeck_core_hooks_roundtrip.py`, `test_streamdeck_manifest.py`, `test_streamdeck_prereqs.py`, `test_streamdeck_status.py`, `test_streamdeck_migrations.py`, `test_streamdeck_shims.py` | path resolution, atomic JSON writes and backups, hook merging (including a round trip with the hooks Quoin's own installer deploys), manifest rules, the prerequisite consent matrix, every status class (read only), state migrations and the rendered entry points |
| Operations | `test_streamdeck_install.py`, `test_streamdeck_upgrade.py`, `test_streamdeck_faults.py`, `test_streamdeck_adoption.py`, `test_streamdeck_uninstall.py`, `test_streamdeck_engine_core.py`, `test_streamdeck_cli.py`, `test_streamdeck_core_install.py` | fresh install, repeat install, upgrade, a failure injected at every step with the hook commands executed afterwards, prototype adoption, uninstall with stubs and purge, the command line, and that plain `quoin install` creates nothing of the component (checked in process with every external effect stubbed) |
| Hardening | `test_streamdeck_hardening.py`, `test_streamdeck_marker_safety.py`, `test_streamdeck_guards.py`, `test_streamdeck_lifecycle_static.py` | operation records that name links, whole folders or other runtimes; recorded runtimes and purge roots; the recorded Python being usable; binary records inside an interrupted run's record; tools from the Python environment never recorded; state folder links; and static guards that keep the operation tests off the real machine |
| Packaging | `test_streamdeck_packaging.py` | a wheel and an sdist built from a copy of the tree with junk planted in the payload and Agent Desk folders ship none of it |
| Wheel end to end | `test_streamdeck_wheel_e2e.py` | a wheel built from the committed tree, installed into a clean virtual environment, runs the whole component lifecycle (install, repeat, upgrade, hooks, uninstall, purge) with a temporary home and no checkout on the path; the real home folder and the checkout are not touched |
| Documentation | `test_streamdeck_docs.py` | the guide lists every command option, exit code, problem code, hook event, environment variable and quoted message the code has, and this record has its three sections |

### Last run

Run on 2026-10-14 on macOS, Python 3.14, with the repository's own virtual environment. Every command was run
from the repository root as `python -m pytest -p no:cacheprovider` with the listed files.

| Selection | Result |
|---|---|
| `quoin/dev/tests/test_[a-l]*.py`, without the two slow files below | 4367 passed, 7 skipped |
| `quoin/dev/tests/test_[0-9]*.py` and `test_[m-q]*.py` | 6174 passed, 23 skipped |
| `quoin/dev/tests/test_[r-z]*.py`, without the two slow files below | 3796 passed |
| `test_streamdeck_packaging.py` and `test_streamdeck_wheel_e2e.py`, run alone | 8 passed, none skipped |

The three first selections cover every test file in the folder except the two slow ones. All skips are in tests
unrelated to the component; none of the Stream Deck tests was skipped, so both planted-tree builds, the wheel
build, the clean virtual environment and the full wheel lifecycle really ran.

- The preamble check (`quoin/scripts/build_preambles.py --check`) passes.
- The authored-content lint reports no findings on the files this branch adds.
- The component payload fingerprint is unchanged by this work.
- The real home folder, the installed prototype and the live Stream Deck setup were compared before and after the
  runs and are identical.

## Verified manually

Checked by hand on 2026-10-12, on the prototype before it became this component. They were not re-run against the
managed install.

- Stream Deck 7.6 loaded the plugin and rendered all 15 keys.
- Synthetic hook events on disposable Zellij panes turned keys orange, blue and green.
- Agent Desk opened the layout Codex, Codex, Shell plus Spend.
- Focusing a pane by id (`focus-pane-id`) worked.
- Codex 0.146.0 hook trust was left to the user.
- Nine AppleScript handlers compiled.

## Not yet verified

Each item says how to check it and what to expect.

- Real hook delivery.
  - Steps: with the component installed and the Codex entries trusted, start a Codex session and a new Claude Code
    session; trigger a permission request, approve it, then let the turn end.
  - Expected: the session key and the agent's key in the open session turn orange on the request, blue after the approval, green when the turn ends.
- Physical key presses for every role.
  - Steps: press a session key (it opens, with BACK on key 1), press an agent in it, press BACK, close the open
    session's last agent, press the attention key while a session is open, press and hold the attention key, use a
    session named with 20 or more characters, press and hold Desk, press + Claude, + Codex, and previous and next tab.
  - Expected: a session key opens its agents; an agent key focuses its pane and the session stays open; BACK returns
    to the page you left; closing the open session's last agent returns the panel to the session list; the attention
    key focuses a waiting agent in another session and the view stays; a hold pages the current view; a long session
    name still shows its agent count; a Desk hold opens its menu; + Claude and + Codex open a new pane; the tab keys
    move between Ghostty tabs.
- Ghostty transition, including a manually opened legacy tab.
  - Steps: open an agent's session in a Ghostty tab by hand, then press its session key and then its agent key.
  - Expected: the existing tab is focused instead of a duplicate being opened.
- Real-machine migration with the documented sequence.
  - Steps: follow "Migrating the prototype install" in the guide on the machine that runs the prototype.
  - Expected: `status` ends at exit 0, the imported profile keeps working without re-import, and no running agent
    is interrupted.
- Real-machine uninstall keeps foreign hooks and profiles.
  - Steps: run `quoin streamdeck uninstall` and compare your Claude and Codex settings and your profiles before and
    after.
  - Expected: only the component's entries are gone; your own hooks, other settings and profiles are unchanged.
- Bundled Node.
  - Steps: on Stream Deck 7.1 or later with no external Node, run `quoin streamdeck install --node bundled`.
  - Expected: the plugin starts and the keys render with no `launch.sh` in the plugin folder.
- Public launch in a real Zellij session.
  - Steps: run `agentdesk --name s1-check --panes codex,codex,shell --no-prompt`, then close that session
    yourself.
  - Expected: the session opens with two Codex panes and a shell.
- Agent subprocess `PATH` in deck-started sessions.
  - Steps: from a deck-started pane, run an MCP server through `npx` or `uvx`, and run `gh` and `jq`.
  - Expected: they resolve; if not, follow the Troubleshooting entry in the guide.
- Stream Deck restart loads the new plugin.
  - Steps: after an upgrade that changes the plugin, restart Stream Deck.
  - Expected: the new plugin version runs and the keys render.
- `--open-profile` import dialog.
  - Steps: run `quoin streamdeck install --open-profile` with the app running.
  - Expected: Stream Deck offers to import the profile.
- Homebrew installs after consent.
  - Steps: with a prerequisite missing, run `quoin streamdeck install` in a terminal and answer yes.
  - Expected: the proposed `brew` command runs and the item is re-checked. The tests never run `brew`.
- Install from a published package.
  - Steps: once a version is published, run `pip install quoin` in a clean environment, then the component
    commands.
  - Expected: the same lifecycle the wheel test shows. Nothing is published yet.
- Core `quoin install` from a built wheel.
  - Steps: install the wheel into a clean environment and run `quoin install` with a temporary home.
  - Expected: Quoin's own skills and hooks are deployed and no component files appear. The wheel test covers the
    component lifecycle only; that plain `quoin install` creates nothing of the component is checked in process
    with every external effect stubbed.
