"""Session-targeted Stream Deck control for Quoin Agent Desk (macOS)."""
import fcntl
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
MISSING_AGENTDESK = 'Agent Desk не установлен — `quoin streamdeck install` предложит установить.'
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
    return apple('on run\n try\n return POSIX path of (choose folder with prompt "Проект для Agent Desk")\n on error number -128\n return ""\n end try\nend run',
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
        raise RuntimeError('Недопустимое имя сессии: ' + repr(name)[:80])
    return name


def select(config):
    socket = config_socket(config)
    available = sessions(socket)
    if not available:
        raise RuntimeError('Нет сохранённых сессий. Нажми «Открыть Desk».')
    labels = [name + (' • active' if live else ' • saved') for name, live in available]
    picked = choose(labels, 'Выбери сессию. Все кнопки панели будут управлять ею.')
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


def require_live(config):
    name = config.get('session')
    if not name:
        raise RuntimeError('Сначала нажми «Открыть Desk» или «Сессии».')
    check_session(name)
    socket = config_socket(config)
    if not dict(sessions(socket)).get(name, False):
        raise RuntimeError('Сессия не запущена: ' + name + '. Нажми «Открыть Desk», чтобы подключить её.')
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
            raise RuntimeError('Папка проекта недоступна. Выбери её кнопкой «Проект».')
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
            raise RuntimeError('Сначала нажми Desk или кнопку нужного агента.')
        surface = apple('on run\n tell application "Ghostty"\n return id of focused terminal of selected tab of front window\n end tell\nend run')
        owner = deckenv.find_surface_owner(e, surface)
        if owner is None:
            raise RuntimeError('Сначала нажми кнопку нужного агента или Desk, чтобы выбрать вкладку для новой панели.')
        check_session(owner[1])
        config['socket'], config['session'] = owner
        config['project'] = config.get('projects', {}).get(owner[1], config['project'])
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
