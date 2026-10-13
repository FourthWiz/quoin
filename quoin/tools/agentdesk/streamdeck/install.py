#!/usr/bin/python3
"""Install only Quoin's own launchers; import the profile separately in Elgato."""
import json
import shutil
from pathlib import Path
from build import build

source=Path(__file__).resolve().parent
destination=Path.home()/'Library/Application Support/QuoinStreamDeck'
destination.mkdir(parents=True,exist_ok=True)
for name in ('controller.py','terminal.zsh'):
    shutil.copy2(source/name,destination/name)
build(source/'dist',destination)
shutil.copytree(source/'dist/Buttons',destination/'Buttons',dirs_exist_ok=True)
state=Path.home()/'.config/agentdesk/streamdeck'
state.mkdir(parents=True,exist_ok=True)
if not (state/'config.json').exists():
    # Source is <repo>/quoin/tools/agentdesk/streamdeck.
    repo=source.parents[3]
    (state/'config.json').write_text(json.dumps({'project':str(repo),'session':None,'projects':{}},indent=2))
print(source/'dist/Quoin Agent Desk.streamDeckProfile')
