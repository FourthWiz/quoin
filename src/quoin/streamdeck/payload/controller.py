#!/usr/bin/python3
"""Session-targeted Stream Deck control for Quoin Agent Desk (macOS)."""
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
STATE = Path(os.environ.get('QUOIN_DECK_STATE', str(Path.home() / '.config/agentdesk/streamdeck')))
ENV = dict(os.environ)
ENV['PATH'] = str(Path.home() / '.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:' + ENV.get('PATH', '/usr/bin:/bin')
ENV.setdefault('ZELLIJ_SOCKET_DIR', '/tmp/zellij-agentdesk-' + str(os.getuid()))
ZELLIJ = 'zellij'
NAV = {'prev-pane':'focus-previous-pane', 'next-pane':'focus-next-pane',
       'fullscreen':'toggle-fullscreen', 'prev-tab':'go-to-previous-tab',
       'next-tab':'go-to-next-tab', 'floating':'toggle-floating-panes',
       'save':'save-session'}


def run(args, check=True, timeout=20):
    result = subprocess.run(args, env=ENV, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Command failed')
    return result.stdout.strip()


def apple(source, *args):
    return run(['/usr/bin/osascript', '-e', source.replace('on run argv\n', 'on run argv\n activate\n').replace('on run\n', 'on run\n activate\n'), '--', *args], timeout=None)


def dialog(message):
    apple('on run argv\n display dialog (item 1 of argv) with title "Quoin Stream Deck" buttons {"OK"} default button "OK"\nend run', message)


def choose(items, prompt):
    answer = apple('on run argv\n set picked to choose from list (items 2 thru -1 of argv) with title "Quoin Stream Deck" with prompt (item 1 of argv)\n if picked is false then return ""\n return item 1 of picked\nend run', prompt, *items)
    return answer or None


def folder():
    return apple('on run\n try\n return POSIX path of (choose folder with prompt "Проект для Agent Desk")\n on error number -128\n return ""\n end try\nend run')


def load():
    p = STATE / 'config.json'
    return json.loads(p.read_text()) if p.exists() else {'project':str(Path.home()), 'session':None, 'projects':{}}


def save(config):
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / 'config.json.tmp'
    p.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    p.replace(STATE / 'config.json')


def sessions():
    result = run([ZELLIJ, 'list-sessions', '--no-formatting'], check=False)
    found = []
    for line in result.splitlines():
        match = re.match(r'^(.*?) \[Created ', line)
        if match:
            found.append((match.group(1), 'EXITED' not in line))
    return found


def select(config):
    available = sessions()
    if not available:
        raise RuntimeError('Нет сохранённых сессий. Нажми «Открыть Desk».')
    labels = [name + (' • active' if live else ' • saved') for name, live in available]
    picked = choose(labels, 'Выбери сессию. Все кнопки панели будут управлять ею.')
    if not picked:
        return False
    name = available[labels.index(picked)][0]
    project = config.get('projects', {}).get(name)
    if not project:
        project = folder()
        if not project:
            return False
    config.update(session=name, project=project)
    config.setdefault('projects', {})[name] = project
    save(config)
    return True


def terminal(mode, config):
    command = shlex.join(['/bin/zsh', str(BASE / 'terminal.zsh'), mode, config['session'], config['project']])
    surface = apple('on run argv\n tell application "Ghostty"\n set cfg to new surface configuration\n set initial working directory of cfg to item 1 of argv\n set command of cfg to item 2 of argv\n set environment variables of cfg to {"ZELLIJ_SOCKET_DIR=" & (item 3 of argv)}\n if (count of windows) is 0 then\n set w to new window with configuration cfg\n set t to selected tab of w\n else\n set t to new tab in front window with configuration cfg\n end if\n set term to focused terminal of t\n focus term\n return id of term\n end tell\nend run', config['project'], command, ENV['ZELLIJ_SOCKET_DIR'])
    config.setdefault('surfaces', {})[config['session']] = surface
    save(config)


def require_live(config):
    name = config.get('session')
    if not name:
        raise RuntimeError('Сначала нажми «Открыть Desk» или «Сессии».')
    if not dict(sessions()).get(name, False):
        raise RuntimeError('Сессия не запущена: ' + name + '. Нажми «Открыть Desk», чтобы подключить её.')
    # Verify the server exists before any action; never fall back to a different session.
    run([ZELLIJ, '--session', name, 'action', 'list-panes', '--json'])
    return name


def execute(action, config):
    if action in ('prev-tab', 'next-tab'):
        apple('on run argv\n tell application "Ghostty"\n if (count of windows) is 0 then return\n set w to front window\n set n to count of tabs of w\n set i to index of selected tab of w\n set targetIndex to ((i - 1 + (item 1 of argv as integer) + n) mod n) + 1\n select tab (tab targetIndex of w)\n activate window w\n end tell\nend run', '-1' if action == 'prev-tab' else '1')
        return
    if action == 'codex-app':
        run(['/usr/bin/open', '-a', 'ChatGPT'])
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
        if not Path(config['project']).is_dir():
            raise RuntimeError('Папка проекта недоступна. Выбери её кнопкой «Проект».')
        if config.get('session') and dict(sessions()).get(config['session']) and config.get('surfaces', {}).get(config['session']):
            try:
                apple('on run argv\n tell application \"Ghostty\"\n focus (first terminal whose id is (item 1 of argv))\n end tell\nend run', config['surfaces'][config['session']])
                return
            except Exception:
                pass
        if config.get('session'):
            terminal('attach' if config['session'] in dict(sessions()) else 'new', config)
        else:
            name = 'quoin-deck-' + time.strftime('%m%d-%H%M%S')
            config['session'] = name
            config.setdefault('projects', {})[name] = config['project']
            save(config)
            terminal('new', config)
        return
    if action in {'claude', 'codex', 'shell', 'git'}:
        if not config.get('session'):
            raise RuntimeError('Сначала нажми Desk или кнопку нужного агента.')
        surface = apple('on run\n tell application "Ghostty"\n return id of focused terminal of selected tab of front window\n end tell\nend run')
        matches = [session for session, value in config.get('surfaces', {}).items() if value == surface]
        if len(matches) != 1:
            raise RuntimeError('Сначала нажми кнопку нужного агента или Desk, чтобы выбрать вкладку для новой панели.')
        config['session'] = matches[0]
        config['project'] = config.get('projects', {}).get(matches[0], config['project'])
    name = require_live(config)
    prefix = [ZELLIJ, '--session', name]
    if action in NAV:
        run(prefix + ['action', NAV[action]])
    elif action in {'claude', 'codex', 'shell', 'git'}:
        run(prefix + ['run', '--name', {'claude':'Claude','codex':'Codex','shell':'Shell','git':'Git review'}[action],
                      '--cwd', config['project'], '--direction', 'right', '--', '/bin/zsh',
                      str(BASE / 'terminal.zsh'), action, name, config['project']])
    else:
        raise RuntimeError('Unknown action: ' + action)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'controller.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            execute(sys.argv[1], load())
        except Exception as exc:
            with (STATE / 'errors.log').open('a') as log:
                log.write(time.strftime('%Y-%m-%d %H:%M:%S ') + str(exc) + '\n')
            dialog(str(exc))

if __name__ == '__main__':
    main()
