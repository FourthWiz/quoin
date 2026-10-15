"""Live inventory and exact pane routing for the Stream Deck plugin."""
import fcntl
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deckenv  # noqa: E402
import controller as c  # noqa: E402
import monitor  # noqa: E402

PS = '/bin/ps'
TTL_S = 7 * 24 * 3600
PROBE_TIMEOUT = 3
MAX_PID = 99999
SNAPSHOT_BUDGET_S = 10
FOCUS_BUDGET_S = 15


def _zellij(args, socket, timeout=PROBE_TIMEOUT):
    e = deckenv.current()
    return subprocess.run([e.bins['zellij']] + list(args), env=e.zellij_env(socket), capture_output=True,
                          text=True, timeout=deckenv.bound(timeout))


def list_sessions(socket):
    """(ok, {name: live}); only a positive answer counts as ok."""
    try:
        result = _zellij(['list-sessions', '--no-formatting'], socket)
    except (subprocess.SubprocessError, OSError):
        return False, {}
    if c.NO_SESSIONS in (result.stdout or '') or c.NO_SESSIONS in (result.stderr or ''):
        return True, {}
    if result.returncode:
        return False, {}
    return True, dict(c.parse_sessions(result.stdout))


def list_panes(socket, session):
    try:
        result = _zellij(['--session', session, 'action', 'list-panes', '--json'], socket)
        if result.returncode:
            return False, None
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return False, None
    return (True, data) if isinstance(data, list) else (False, None)


def pane_present(panes, pane):
    return any(isinstance(p, dict) and str(p.get('id')) == str(pane) and not p.get('is_plugin')
               and not p.get('exited') for p in panes)


def valid_pid(value):
    return type(value) is int and 1 <= value <= MAX_PID


def pid_identities(pids):
    """(ok, {pid: lstart}) from ONE ps call in the pinned C locale."""
    wanted = sorted(set(p for p in pids if valid_pid(p)))
    if not wanted:
        return True, {}
    try:
        result = subprocess.run([PS, '-o', 'pid=,lstart=', '-p', ','.join(str(p) for p in wanted)],
                                env=deckenv.ps_env(), capture_output=True, text=True, errors='replace',
                                timeout=deckenv.bound(PROBE_TIMEOUT))
    except (subprocess.SubprocessError, OSError):
        return False, {}
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return True, {}
    if result.returncode != 0:
        return False, {}
    found = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        try:
            pid = int(parts[0])
        except ValueError:
            return False, {}
        if len(parts) != 2 or not parts[1].strip() or pid not in wanted:
            return False, {}
        found[pid] = parts[1].strip()
    return True, found


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _take_lock(path):
    try:
        fd = deckenv.open_lock(path)
    except OSError:
        return None
    deadline = time.monotonic() + 0.3
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.02)


def _delete_guarded(candidates, lock_path):
    """Unlink only files whose `updated` still equals the value the decision saw."""
    if not candidates:
        return
    fd = _take_lock(lock_path)
    if fd is None:
        return
    try:
        for path, seen in candidates:
            if seen is None:
                try:
                    if time.time() - os.stat(path).st_mtime <= TTL_S:
                        continue
                except OSError:
                    continue
            else:
                current = deckenv.read_json(path, None)
                if not isinstance(current, dict) or current.get('updated') != seen:
                    continue
            try:
                os.unlink(path)
            except OSError:
                pass
    finally:
        os.close(fd)


def snapshot():
    deckenv.set_deadline(SNAPSHOT_BUDGET_S)
    try:
        return _snapshot()
    finally:
        deckenv.clear_deadline()


def _prune_surfaces(e, sessions_cache):
    """Drop surface ids of sessions a successful list-sessions says no longer exist."""
    surfaces = deckenv.read_json(e.surfaces_path, None)
    if not isinstance(surfaces, dict) or not surfaces:
        return
    kept = {}
    for key, value in surfaces.items():
        socket, _, session = key.partition('\t')
        answer = sessions_cache.get(socket)
        if answer is not None and answer[0] and session not in answer[1]:
            continue
        kept[key] = value
    if len(kept) != len(surfaces):
        try:
            deckenv.write_json_atomic(e.surfaces_path, kept)
        except OSError:
            pass


def _snapshot():
    e = deckenv.current()
    now = time.time()
    items = list(deckenv.iter_records(e, include_shadowed=True))
    new_updated = {os.path.basename(p): r.get('updated') for p, r, layout in items
                   if layout == 'new' and isinstance(r, dict)}
    sessions_cache, panes_cache = {}, {}

    def sessions_of(socket):
        if socket not in sessions_cache:
            sessions_cache[socket] = list_sessions(socket)
        return sessions_cache[socket]

    def panes_of(socket, session):
        if (socket, session) not in panes_cache:
            panes_cache[(socket, session)] = list_panes(socket, session)
        return panes_cache[(socket, session)]

    doomed = {'new': [], 'legacy': []}
    parsed = []
    for path, rec, layout in items:
        side = 'new' if layout == 'new' else 'legacy'
        valid = (isinstance(rec, dict) and isinstance(rec.get('session'), str) and _number(rec.get('updated'))
                 and rec.get('pane') is not None)
        if not valid:
            doomed[side].append((path, None))  # only deleted when the file is older than the TTL
            continue
        if layout == 'legacy-shadowed':
            newer = new_updated.get(os.path.basename(path))
            if now - rec['updated'] > TTL_S or (_number(newer) and rec['updated'] <= newer):
                doomed['legacy'].append((path, rec['updated']))
            continue
        parsed.append((path, rec, side))

    eligible = [r['pid'] for _, r, _ in parsed if valid_pid(r.get('pid')) and deckenv.is_c_lstart(r.get('process_started'))]
    pid_ok, identities = pid_identities(eligible)

    visible = []
    for path, rec, side in parsed:
        seen = rec['updated']
        pane = str(rec['pane'])
        socket = deckenv.record_socket(rec, e)
        session = rec['session']
        status = rec.get('status')
        remove = False
        offline = False
        if status == 'closed' or now - seen > TTL_S:
            remove = True
        elif not deckenv.valid_session(session):
            offline = True
        elif (pid_ok and valid_pid(rec.get('pid')) and deckenv.is_c_lstart(rec.get('process_started'))
              and identities.get(rec['pid']) != rec['process_started'].strip()):
            remove = True
        else:
            ok, known = sessions_of(socket)
            if not ok:
                offline = True
            elif not known.get(session):
                if rec.get('socket_inferred') is True or (side == 'legacy' and 'socket_inferred' not in rec):
                    offline = True
                else:
                    remove = True
            else:
                pok, panes = panes_of(socket, session)
                if not pok:
                    offline = True
                elif not pane_present(panes, pane):
                    remove = True
        if remove:
            doomed[side].append((path, seen))
            continue
        entry = {k: rec[k] for k in monitor.RECORD_KEYS if k in rec}
        entry['pane'] = pane
        entry['socket'] = socket
        if offline:
            entry['status'] = 'offline'
        visible.append(entry)

    _delete_guarded(doomed['new'], e.monitor_lock)
    _delete_guarded(doomed['legacy'], e.legacy_monitor_lock)

    # One visible process per pane: a resumed conversation replaces the previous session id.
    dedup = {}
    for r in sorted(visible, key=lambda r: r['updated']):
        dedup[(r['socket'], r['session'], r['pane'])] = r
    records = list(dedup.values())

    # Show uninstrumented agent panes honestly, without guessing their working/approval state.
    scan = [e.socket_dir] + [s for s in sorted(set(r['socket'] for r in records)) if s != e.socket_dir]
    for socket in scan:
        ok, known = sessions_of(socket)
        if not ok:
            continue
        for session, live in sorted(known.items()):
            if not live or not deckenv.valid_session(session):
                continue
            pok, panes = panes_of(socket, session)
            if not pok:
                continue
            for p in panes:
                if not isinstance(p, dict) or p.get('is_plugin') or p.get('exited'):
                    continue
                title = str(p.get('title', '')).lower()
                kind = 'claude' if 'claude' in title else 'codex' if 'codex' in title else None
                pane = str(p.get('id'))
                if kind and not any(r['session'] == session and r['pane'] == pane and r['socket'] == socket
                                    for r in records):
                    records.append(dict(id='pane:' + socket + ':' + session + ':' + pane, kind=kind, session=session,
                                        pane=pane, socket=socket, project=p.get('pane_cwd', ''),
                                        status='unknown', created=0, updated=0))
    _prune_surfaces(e, sessions_cache)
    return sorted(records, key=lambda r: (r['session'], int(r['pane']) if r['pane'].isdigit() else 0))


def _name_is_unique(session, socket):
    e = deckenv.current()
    others = set([e.socket_dir] + [deckenv.record_socket(r, e) for _, r, _ in deckenv.iter_records(e)
                                   if isinstance(r, dict)])
    others.discard(socket)
    for other in sorted(others):
        ok, known = list_sessions(other)
        if not ok or session in known:
            return False
    return True


def focus(record):
    deckenv.set_deadline(FOCUS_BUDGET_S)
    try:
        _focus(record)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Could not switch in time.')
    finally:
        deckenv.clear_deadline()


def _focus(record):
    e = deckenv.current()
    session, pane = record.get('session'), record.get('pane')
    if not isinstance(session, str) or not session or pane is None:
        raise RuntimeError('This pane is already closed.')
    if not deckenv.valid_session(session):
        raise RuntimeError('Invalid session name.')
    pane = str(pane)
    socket = deckenv.record_socket(record, e)
    ok, panes = list_panes(socket, session)
    if not ok:
        raise RuntimeError('Session unavailable: ' + session)
    if not pane_present(panes, pane):
        raise RuntimeError('This pane is already closed.')
    config = c.load()
    surface = deckenv.get_surface(e, socket, session)
    if surface:
        try:
            c.apple(c.FOCUS_SCRIPT, surface, timeout=10)
        except subprocess.TimeoutExpired:
            surface = None
        except Exception:
            surface = None
    if not surface and _name_is_unique(session, socket):
        # Zellij sets the Ghostty title to "session | pane". Use only a unique match.
        surface = c.apple('on run argv\n tell application "Ghostty"\n set hits to {}\n repeat with t in terminals\n if (name of t) starts with ((item 1 of argv) & " | ") then set end of hits to t\n end repeat\n if (count of hits) is 1 then\n set t to item 1 of hits\n focus t\n return id of t\n end if\n return ""\n end tell\nend run',
                          session, timeout=10)
        if surface:
            deckenv.set_surface(e, socket, session, surface)
    if not surface:
        config.update(session=session, socket=socket, project=record.get('project') or config['project'])
        c.terminal('attach', config, socket=socket)
    try:
        c.run([e.bins['zellij'], '--session', session, 'action', 'focus-pane-id', 'terminal_' + pane], socket=socket, timeout=3)
    except RuntimeError as exc:
        # Zellij reports an error when the pane already has focus; that is the state we wanted.
        if 'already focused' not in str(exc):
            raise
    config = c.load()
    config.update(session=session, socket=socket, project=record.get('project') or config['project'])
    deckenv.remember_project(config, session, config['project'])
    c.save(config)


def main():
    action = sys.argv[1]
    if action == 'snapshot':
        print(json.dumps(snapshot()))
        return
    if action == 'focus':
        # Key presses carry an immutable target, never a mutable list index.
        focus(json.loads(sys.argv[2]))
        return
    if action == 'menu':
        labels = ['New Desk: 2 Codex + Shell', 'Choose project folder', 'Attach session', 'Add Shell', 'Git: local changes']
        picked = c.choose(labels, 'More Agent Desk actions')
        if not picked:
            return
        config = c.load()
        if picked == labels[0]:
            config['session'] = None
            c.execute('desk', config)
        else:
            c.execute(dict(zip(labels[1:], ['project', 'sessions', 'shell', 'git']))[picked], config)
        return
    c.execute(action, c.load())


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        try:
            deckenv.log_error(deckenv.current(), exc)
        except Exception:
            pass
        if len(sys.argv) > 1 and sys.argv[1] == 'snapshot':
            print('[]')
        else:
            print(str(exc), file=sys.stderr)
            if not (len(sys.argv) > 1 and sys.argv[1] == 'focus'):
                try:
                    c.dialog(str(exc))
                except Exception:
                    pass
        sys.exit(1)
