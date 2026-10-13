#!/usr/bin/python3
"""Install reversible local integration. Existing hooks are preserved."""
import json
from pathlib import Path
import shlex
import shutil
import time
import build_live

BASE=Path(__file__).resolve().parent
RUNTIME=Path.home()/'Library/Application Support/QuoinStreamDeck'
EVENTS=['SessionStart','SessionEnd','UserPromptSubmit','PreToolUse','PermissionRequest','PostToolUse','Stop','PreCompact','PostCompact']

def merge(data,kind,script):
    data=json.loads(json.dumps(data));hooks=data.setdefault('hooks',{})
    events=EVENTS+(['PostToolUseFailure','Notification','StopFailure','Elicitation','ElicitationResult'] if kind=='claude' else ['Interrupt'])
    command='/usr/bin/python3 '+shlex.quote(str(script))+' '+kind
    for event in events:
        entries=hooks.setdefault(event,[])
        if not any(h.get('command')==command for e in entries for h in e.get('hooks',[])):
            entries.append({'hooks':[{'type':'command','command':command,'timeout':3}]})
    return data

def install():
    build_live.build()
    backup=RUNTIME/'backups'/time.strftime('%Y%m%d-%H%M%S');backup.mkdir(parents=True)
    for name in ['controller.py','terminal.zsh','dashboard.py','monitor.py']:
        dest=RUNTIME/name
        if dest.exists():shutil.copy2(dest,backup/name)
        shutil.copy2(BASE/name,dest)
    dest=Path.home()/'Library/Application Support/com.elgato.StreamDeck/Plugins'/ 'local.quoin.agentdesk.sdPlugin'
    if dest.exists():shutil.copytree(dest,backup/dest.name)
    shutil.copytree(build_live.OUT/dest.name,dest,dirs_exist_ok=True)
    for kind,rel in [('claude','.claude/settings.json'),('codex','.codex/hooks.json')]:
        target=Path.home()/rel
        data=json.loads(target.read_text()) if target.exists() else {}
        if target.exists():shutil.copy2(target,backup/(kind+'-hooks.json'))
        else:(backup/(kind+'-hooks.absent')).touch()
        target.parent.mkdir(parents=True,exist_ok=True)
        tmp=target.with_suffix('.quoin-tmp');tmp.write_text(json.dumps(merge(data,kind,RUNTIME/'monitor.py'),ensure_ascii=False,indent=2)+'\n');tmp.replace(target)
    print('Installed; backup:',backup)
    print('Codex requires /hooks review; no trust bypass is installed.')

if __name__=='__main__':install()
