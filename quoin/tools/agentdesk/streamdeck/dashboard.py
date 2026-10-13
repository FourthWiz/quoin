#!/usr/bin/python3
"""Live inventory and exact pane routing for the Stream Deck plugin."""
import json
import os
from pathlib import Path
import shlex
import sys
import time
import controller as c
import monitor


def panes(session, socket=None):
    env=c.ENV.copy()
    if socket: env['ZELLIJ_SOCKET_DIR']=socket
    import subprocess
    p=subprocess.run(['zellij','--session',session,'action','list-panes','--json'],env=env,capture_output=True,text=True,timeout=3)
    if p.returncode: raise RuntimeError('Сессия недоступна: '+session)
    return json.loads(p.stdout)


def snapshot():
    records=[]; inventories={}
    for path in (c.STATE/'agents').glob('*.json'):
        try:
            r=json.loads(path.read_text())
            if r['status']=='closed': continue
            if r.get('pid') and monitor.process_identity(r['pid'])!=r.get('process_started'): continue
            k=(r['session'],r.get('socket'))
            if k not in inventories:
                try: inventories[k]=panes(*k)
                except Exception: inventories[k]=None
            if inventories[k] is None:
                r['status']='offline'
            elif not any(str(p['id'])==r['pane'] and not p.get('is_plugin') and not p.get('exited') for p in inventories[k]): continue
            records.append(r)
        except (ValueError,KeyError,OSError): continue
    # One visible process per pane: a resumed conversation replaces the previous session id.
    dedup={}
    for r in sorted(records,key=lambda r:r['updated']): dedup[(r['socket'],r['session'],r['pane'])]=r
    records=list(dedup.values())
    # Show uninstrumented agent panes honestly, without guessing their working/approval state.
    try:
        for session,live in c.sessions():
            if not live: continue
            key=(session,c.ENV['ZELLIJ_SOCKET_DIR'])
            data=inventories.get(key)
            if data is None:
                try: data=panes(*key)
                except Exception: continue
            for p in data:
                if p.get('is_plugin') or p.get('exited'): continue
                title=p.get('title','').lower()
                kind='claude' if 'claude' in title else 'codex' if 'codex' in title else None
                if kind and not any(r['session']==session and r['pane']==str(p['id']) and r['socket']==key[1] for r in records):
                    records.append(dict(id='pane:'+session+':'+str(p['id']),kind=kind,session=session,pane=str(p['id']),socket=key[1],project=p.get('pane_cwd',''),status='unknown',created=0,updated=0))
    except Exception: pass
    return sorted(records,key=lambda r:(r['session'],int(r['pane']) if r['pane'].isdigit() else 0))


def focus(record):
    config=c.load()
    data=panes(record['session'],record.get('socket'))
    if not any(str(p['id'])==record['pane'] and not p.get('is_plugin') and not p.get('exited') for p in data):
        raise RuntimeError('Эта панель уже закрыта.')
    # Focus a known Ghostty surface. Legacy/manual sessions get one explicitly mapped attachment.
    surface=config.get('surfaces',{}).get(record['session'])
    if surface:
        try:
            c.apple('on run argv\n tell application "Ghostty"\n focus (first terminal whose id is (item 1 of argv))\n end tell\nend run',surface)
        except Exception: surface=None
    if not surface:
        # Zellij sets the Ghostty title to "session | pane". Use only a unique match.
        surface=c.apple('on run argv\n tell application "Ghostty"\n set hits to {}\n repeat with t in terminals\n if (name of t) starts with ((item 1 of argv) & " | ") then set end of hits to t\n end repeat\n if (count of hits) is 1 then\n set t to item 1 of hits\n focus t\n return id of t\n end if\n return ""\n end tell\nend run',record['session'])
        if surface:
            config.setdefault('surfaces',{})[record['session']]=surface; c.save(config)
    if not surface:
        config.update(session=record['session'],project=record.get('project') or config['project'])
        previous_socket=c.ENV['ZELLIJ_SOCKET_DIR']
        try:
            c.ENV['ZELLIJ_SOCKET_DIR']=record.get('socket') or previous_socket
            c.terminal('attach',config)
        finally: c.ENV['ZELLIJ_SOCKET_DIR']=previous_socket
    old=c.ENV.get('ZELLIJ_SOCKET_DIR')
    try:
        c.ENV['ZELLIJ_SOCKET_DIR']=record.get('socket') or old
        c.run(['zellij','--session',record['session'],'action','focus-pane-id','terminal_'+record['pane']])
    finally: c.ENV['ZELLIJ_SOCKET_DIR']=old
    config=c.load(); config.update(session=record['session'],project=record.get('project') or config['project']); config.setdefault('projects', {})[record['session']]=config['project']; c.save(config)


def main():
    action=sys.argv[1]
    if action=='snapshot': print(json.dumps(snapshot())); return
    if action=='focus':
        # Key presses carry an immutable target, never a mutable list index.
        focus(json.loads(sys.argv[2])); return
    if action=='menu':
        labels=['Новый Desk: 2 Codex + Shell','Выбрать папку проекта','Подключить сессию','Добавить Shell','Git: локальные изменения']
        picked=c.choose(labels,'Дополнительные действия Agent Desk')
        if not picked:return
        config=c.load()
        if picked==labels[0]: config['session']=None; c.execute('desk',config)
        else:c.execute(dict(zip(labels[1:],['project','sessions','shell','git']))[picked],config)
        return
    c.execute(action,c.load())

if __name__=='__main__':
    try:main()
    except Exception as exc:
        if len(sys.argv)>1 and sys.argv[1]=='snapshot':print('[]')
        else:
            print(str(exc),file=sys.stderr)
            c.dialog(str(exc))
        sys.exit(1)
