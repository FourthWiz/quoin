"""Session-targeted Stream Deck control for Quoin Agent Desk (macOS)."""
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deckenv  # noqa: E402

NAV = {'prev-pane': 'focus-previous-pane', 'next-pane': 'focus-next-pane',
       'fullscreen': 'toggle-fullscreen', 'prev-tab': 'go-to-previous-tab',
       'next-tab': 'go-to-next-tab', 'floating': 'toggle-floating-panes',
       'save': 'save-session'}
NO_SESSIONS = 'No active zellij sessions found'
MISSING_AGENTDESK = 'Agent Desk is not installed: `quoin streamdeck install` offers to install it.'
FOCUS_SCRIPT = ('on run argv\n tell application "Ghostty"\n focus (first terminal whose id is (item 1 of argv))\n'
                ' end tell\nend run')


def env():
    return deckenv.current()


def run_raw(args, timeout=20, socket=None):
    """Run an argv list with the deck's zellij environment; returns the CompletedProcess."""
    e = env()
    return subprocess.run(args, env=e.zellij_env(socket or e.socket_dir), text=True,
                          capture_output=True, timeout=deckenv.bound(timeout))


def run(args, check=True, timeout=20, socket=None):
    result = run_raw(args, timeout=timeout, socket=socket)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Command failed')
    return result.stdout.strip()


def apple(source, *args, timeout=15):
    script = source.replace('on run argv\n', 'on run argv\n activate\n').replace('on run\n', 'on run\n activate\n')
    return run([env().bins['osascript'], '-e', script, '--', *args], timeout=timeout)


def dialog(message):
    apple('on run argv\n display dialog (item 1 of argv) with title "Quoin Stream Deck" buttons {"OK"} default button "OK"\nend run',
          message, timeout=None)


def choose(items, prompt):
    answer = apple('on run argv\n set picked to choose from list (items 2 thru -1 of argv) with title "Quoin Stream Deck" with prompt (item 1 of argv)\n if picked is false then return ""\n return item 1 of picked\nend run',
                   prompt, *items, timeout=None)
    return answer or None


def folder():
    return apple('on run\n try\n return POSIX path of (choose folder with prompt "Project for Agent Desk")\n on error number -128\n return ""\n end try\nend run',
                 timeout=None)


def load():
    return deckenv.load_config(env())


def save(config):
    deckenv.save_config(env(), config)


def parse_sessions(text):
    found = []
    for line in text.splitlines():
        match = re.match(r'^(.*?) \[Created ', line)
        if match:
            found.append((match.group(1), 'EXITED' not in line))
    return found


def sessions(socket=None):
    return parse_sessions(run([env().bins['zellij'], 'list-sessions', '--no-formatting'], check=False, socket=socket))


def config_socket(config):
    return config.get('socket') or env().socket_dir


def check_session(name):
    if not deckenv.valid_session(name):
        raise RuntimeError('Invalid session name: ' + repr(name)[:80])
    return name


def select(config):
    socket = config_socket(config)
    available = sessions(socket)
    if not available:
        raise RuntimeError('No saved sessions. Press Desk first.')
    labels = [name + (' • active' if live else ' • saved') for name, live in available]
    picked = choose(labels, 'Pick a session. Every key on the panel will control it.')
    if not picked:
        return False
    name = check_session(available[labels.index(picked)][0])
    project = config.get('projects', {}).get(name)
    if not project:
        project = folder()
        if not project:
            return False
    config.update(session=name, project=project, socket=socket)
    deckenv.remember_project(config, name, project)
    save(config)
    return True


def terminal(mode, config, socket=None):
    e = env()
    socket = socket or config_socket(config)
    if mode == 'new' and e.agentdesk is None:
        raise RuntimeError(MISSING_AGENTDESK)
    command = shlex.join([e.terminal_entry, mode, config['session'], config['project']])
    values = ['"ZELLIJ_SOCKET_DIR=" & (item 3 of argv)', '"PATH=" & (item 4 of argv)']
    extra = []
    if e.agentdesk is not None:
        values.append('"QUOIN_DECK_AGENTDESK=" & (item 5 of argv)')
        extra.append(e.agentdesk)
    source = ('on run argv\n tell application "Ghostty"\n set cfg to new surface configuration\n'
              ' set initial working directory of cfg to item 1 of argv\n set command of cfg to item 2 of argv\n'
              ' set environment variables of cfg to {' + ', '.join(values) + '}\n'
              ' if (count of windows) is 0 then\n set w to new window with configuration cfg\n set t to selected tab of w\n'
              ' else\n set t to new tab in front window with configuration cfg\n end if\n'
              ' set term to focused terminal of t\n focus term\n return id of term\n end tell\nend run')
    surface = apple(source, config['project'], command, socket, e.path_env, *extra)
    deckenv.set_surface(e, socket, config['session'], surface)


def owner_from_title(e, config, surface, title):
    """Resolve the session of a tab the deck did not open (zellij titles its terminal "session | pane")."""
    name = title.split(' | ', 1)[0].strip() if ' | ' in title else ''
    if not name or not deckenv.valid_session(name):
        return None
    candidates = [config_socket(config), e.socket_dir]
    candidates += [deckenv.record_socket(r, e) for _, r, _ in deckenv.iter_records(e) if isinstance(r, dict)]
    live = []
    for socket in dict.fromkeys(candidates):
        try:
            if dict(sessions(socket)).get(name, False):
                live.append(socket)
        except Exception:
            continue
    # The same name live on two servers cannot be told apart from the title.
    if len(live) != 1:
        return None
    deckenv.set_surface(e, live[0], name, surface)
    return live[0], name


def adopted_project(e, config, socket, name):
    """Project folder of a session the deck never started: remembered, else its agents', else a pane's cwd."""
    remembered = config.get('projects', {}).get(name)
    if isinstance(remembered, str) and os.path.isdir(remembered):
        return remembered
    records = [r for _, r, _ in deckenv.iter_records(e)
               if isinstance(r, dict) and r.get('session') == name and deckenv.record_socket(r, e) == socket
               and isinstance(r.get('project'), str) and os.path.isdir(r['project'])]
    if records:
        return max(records, key=lambda r: r.get('updated') or 0)['project']
    try:
        panes = json.loads(run([e.bins['zellij'], '--session', name, 'action', 'list-panes', '--json'], socket=socket))
    except Exception:
        return None
    panes = [p for p in panes if isinstance(p, dict) and not p.get('is_plugin') and not p.get('exited')]
    for pane in sorted(panes, key=lambda p: not p.get('is_focused')):
        cwd = pane.get('pane_cwd')
        if isinstance(cwd, str) and os.path.isdir(cwd):
            return cwd
    return None


def require_live(config):
    name = config.get('session')
    if not name:
        raise RuntimeError('Press Desk first, or attach a session from the Desk menu.')
    check_session(name)
    socket = config_socket(config)
    if not dict(sessions(socket)).get(name, False):
        raise RuntimeError('Session is not running: ' + name + '. Press Desk to attach it.')
    # Verify the server exists before any action; never fall back to a different session.
    run([env().bins['zellij'], '--session', name, 'action', 'list-panes', '--json'], socket=socket)
    return name


def execute(action, config):
    e = env()
    if config.get('session'):
        check_session(config['session'])
    if action in ('prev-tab', 'next-tab'):
        apple('on run argv\n tell application "Ghostty"\n if (count of windows) is 0 then return\n set w to front window\n set n to count of tabs of w\n set i to index of selected tab of w\n set targetIndex to ((i - 1 + (item 1 of argv as integer) + n) mod n) + 1\n select tab (tab targetIndex of w)\n activate window w\n end tell\nend run',
              '-1' if action == 'prev-tab' else '1')
        return
    if action == 'codex-app':
        run([e.bins['open'], '-a', 'ChatGPT'])
        return
    if action == 'project':
        project = folder()
        if project:
            config.update(project=project, session=None)
            save(config)
        return
    if action == 'sessions':
        if select(config):
            terminal('attach', config)
        return
    if action == 'desk':
        if not os.path.isdir(config['project']):
            raise RuntimeError('The project folder is not available. Choose it from the Desk menu.')
        socket = config_socket(config)
        session = config.get('session')
        if session and dict(sessions(socket)).get(session) and deckenv.get_surface(e, socket, session):
            try:
                apple(FOCUS_SCRIPT, deckenv.get_surface(e, socket, session))
                return
            except Exception:
                pass
        if session:
            terminal('attach' if session in dict(sessions(socket)) else 'new', config, socket)
        else:
            name = 'quoin-deck-' + time.strftime('%m%d-%H%M%S')
            config.update(session=name, socket=e.socket_dir)
            deckenv.remember_project(config, name, config['project'])
            save(config)
            terminal('new', config, e.socket_dir)
        return
    if action in {'claude', 'codex', 'shell', 'git'}:
        if not config.get('session'):
            raise RuntimeError('Press Desk or the key of the agent you want first.')
        found = apple('on run\n tell application "Ghostty"\n set t to focused terminal of selected tab of front window\n return (id of t) & linefeed & (name of t)\n end tell\nend run')
        surface, _, title = found.partition('\n')
        owner = deckenv.find_surface_owner(e, surface)
        adopted = owner is None
        if adopted:
            owner = owner_from_title(e, config, surface, title)
        if owner is None:
            raise RuntimeError('Press the key of an agent, or Desk, first to pick the tab for the new pane.')
        check_session(owner[1])
        if adopted:
            project = adopted_project(e, config, *owner)
            if project is None:
                raise RuntimeError('Could not tell which project ' + owner[1] + ' belongs to. Press the key of one of its agents first.')
            config['project'] = project
            deckenv.remember_project(config, owner[1], project)
        else:
            config['project'] = config.get('projects', {}).get(owner[1], config['project'])
        config['socket'], config['session'] = owner
        if adopted:
            save(config)
    name = require_live(config)
    socket = config_socket(config)
    prefix = [e.bins['zellij'], '--session', name]
    if action in NAV:
        run(prefix + ['action', NAV[action]], socket=socket)
    elif action in {'claude', 'codex', 'shell', 'git'}:
        run(prefix + ['run', '--name', {'claude': 'Claude', 'codex': 'Codex', 'shell': 'Shell', 'git': 'Git review'}[action],
                      '--cwd', config['project'], '--direction', 'right', '--',
                      e.terminal_entry, action, name, config['project']], socket=socket)
    else:
        raise RuntimeError('Unknown action: ' + action)


def main():
    e = env()
    deckenv.ensure_dir(e.state_dir)
    try:
        fd = deckenv.open_lock(e.controller_lock)
    except OSError as exc:
        deckenv.log_error(e, exc)
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return
        try:
            execute(sys.argv[1], load())
        except Exception as exc:
            deckenv.log_error(e, exc)
            try:
                dialog(str(exc))
            except Exception as dialog_exc:
                deckenv.log_error(e, dialog_exc)
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
