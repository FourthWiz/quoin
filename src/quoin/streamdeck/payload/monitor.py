#!/usr/bin/python3
"""Observation only: never emits hook output or makes approval decisions."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

STATE = Path(os.environ.get('QUOIN_DECK_STATE', str(Path.home()/'.config/agentdesk/streamdeck')))

def process_identity(pid):
    try:
        return subprocess.check_output(['/bin/ps','-p',str(pid),'-o','lstart='],text=True,timeout=1).strip()
    except (subprocess.SubprocessError, OSError):
        return ''

def agent_process(kind):
    pid = os.getppid()
    for _ in range(8):
        try:
            row = subprocess.check_output(['/bin/ps','-p',str(pid),'-o','ppid=,comm='],text=True,timeout=1).strip().split(None,1)
            if len(row)!=2: break
            parent, command = row
            name = Path(command).name.lower()
            if name == kind or name.startswith(kind+'-') or (kind=='claude' and '/claude/versions/' in command):
                return pid, process_identity(pid)
            pid = int(parent)
            if pid < 2: break
        except (ValueError, subprocess.SubprocessError, OSError): break
    return None, ''

def transition(old, event):
    """Keep unrelated parallel completions from hiding a pending question."""
    state = dict(old)
    pending = dict(state.get('pending',{}))
    name = event.get('hook_event_name','')
    tool = event.get('tool_name','')
    token = event.get('tool_use_id') or tool or 'approval'
    if name == 'UserPromptSubmit':
        pending = {}; state['status']='working'
    elif name in ('PermissionRequest','Elicitation') or (name=='PreToolUse' and tool in ('AskUserQuestion','request_user_input')):
        pending[token] = 'question' if name=='Elicitation' or tool in ('AskUserQuestion','request_user_input') else 'permission'
        state['status']='attention'
    elif name == 'Notification' and event.get('notification_type') in ('permission_prompt','idle_prompt','elicitation_dialog'):
        # PermissionRequest carries the matching tool id; don't add a second unresolvable key.
        if not pending: pending['notification']='attention'
        state['status']='attention'
    elif name in ('PostToolUse','PostToolUseFailure','ElicitationResult'):
        pending.pop(token,None)
        pending.pop('notification',None)
        state['status']='attention' if pending else 'working'
    elif name in ('PreToolUse','PreCompact','PostCompact'):
        state['status']='attention' if pending else 'working'
    elif name in ('Stop','StopFailure','Interrupt'):
        pending={}; state['status']='done' if name=='Stop' else 'attention'
    elif name == 'SessionStart':
        pending={}; state['status']='idle'
    elif name == 'SessionEnd':
        pending={}; state['status']='closed'
    state['pending']=pending
    state['event']=name
    state['updated']=time.time()
    return state

def record(kind, event, environ=None, identity=None):
    env = os.environ if environ is None else environ
    session = env.get('ZELLIJ_SESSION_NAME')
    pane = env.get('ZELLIJ_PANE_ID')
    sid = event.get('session_id')
    # Desktop threads and background subagents are not separately addressable panes.
    if not session or pane is None or not sid or event.get('agent_id'): return
    key = hashlib.sha256((kind+':'+sid).encode()).hexdigest()[:24]
    directory=STATE/'agents'; directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (STATE/'monitor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=directory/(key+'.json')
        old=json.loads(path.read_text()) if path.exists() else {}
        value=transition(old,event)
        pid, started = identity if identity is not None else agent_process(kind)
        value.update(id=key,kind=kind,session=session,pane=str(pane),
                     socket=env.get('ZELLIJ_SOCKET_DIR','/tmp/zellij-agentdesk-'+str(os.getuid())),
                     project=event.get('cwd',''),created=old.get('created',time.time()))
        if pid: value.update(pid=pid,process_started=started)
        # No prompts, tool arguments, transcripts, responses, or permission decisions are persisted.
        temporary=path.with_suffix('.tmp'); temporary.write_text(json.dumps(value)); temporary.replace(path)

if __name__=='__main__':
    try:
        record(sys.argv[1],json.load(sys.stdin))
    except Exception:
        pass
    # Failure of the indicator must never block the agent or alter its approval flow.
    sys.exit(0)
