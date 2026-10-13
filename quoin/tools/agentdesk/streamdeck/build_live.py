#!/usr/bin/python3
"""Build the local plugin and importable 3x5 dashboard."""
import json
from pathlib import Path
import shutil
import uuid
import zipfile
from PIL import Image, ImageDraw, ImageFont

BASE=Path(__file__).resolve().parent
OUT=BASE/'dist/live'
PLUGIN='local.quoin.agentdesk'
ACTION=PLUGIN+'.dashboard'

def build():
    out=OUT; out.mkdir(parents=True,exist_ok=True)
    plugin=out/(PLUGIN+'.sdPlugin');plugin.mkdir(exist_ok=True)
    shutil.copy2(BASE/'plugin.js',plugin/'plugin.js')
    launcher=plugin/'launch.sh';launcher.write_text('#!/bin/sh\ncd -- "$(dirname -- "$0")"\nexec /opt/homebrew/bin/node plugin.js "$@"\n');launcher.chmod(0o755)
    img=Image.new('RGB',(144,144),'#213a4e');d=ImageDraw.Draw(img)
    font=ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc',20)
    d.multiline_text((72,72),'AGENT\nDESK',font=font,fill='white',anchor='mm',align='center');img.save(plugin/'icon.png')
    manifest=dict(Name='Quoin Agent Desk Live',UUID=PLUGIN,Author='Quoin',Version='2.0.0.0',Description='Live Claude Code and Codex status with exact Ghostty/Zellij routing',Icon='icon',Category='Quoin Agent Desk',CategoryIcon='icon',CodePath='launch.sh',CodePathMac='launch.sh',SDKVersion=2,Software={'MinimumVersion':'6.6'},OS=[{'Platform':'mac','MinimumVersion':'13'}],Actions=[dict(UUID=ACTION,Name='Agent Desk Live',Icon='icon',Tooltip='Live agent status or Agent Desk control',UserTitleEnabled=False,States=[{'Image':'icon','ShowTitle':False}],SupportedInMultiActions=False,Controllers=['Keypad'])])
    (plugin/'manifest.json').write_text(json.dumps(manifest,indent=2))
    root=out/'3A28CB85-CAFE-426A-BC25-BD414402F62B.sdProfile'
    pid='30ff3085-bb1e-4f54-9c82-0e373957a761';page=root/'Profiles'/pid.upper();page.mkdir(parents=True,exist_ok=True)
    roles=[dict(role='agent',slot=i) for i in range(9)]+[dict(role='attention')]+[dict(role=r) for r in ['desk','claude','codex','prev-tab','next-tab']]
    actions={}
    for i,settings in enumerate(roles):
        actions[f'{i%5},{i//5}']=dict(ActionID=str(uuid.uuid4()),LinkedTitle=False,Name='Agent Desk Live',Plugin={'Name':'Quoin Agent Desk Live','UUID':PLUGIN,'Version':'2.0.0.0'},UUID=ACTION,Settings=settings,State=0,States=[{'ShowTitle':False,'Title':'','Image':''}])
    (root/'manifest.json').write_text(json.dumps({'Device':{'Model':'20GAA9901','UUID':''},'Name':'Quoin — Live Agents','Pages':{'Current':pid,'Default':pid,'Pages':[pid]},'Version':'3.0'},ensure_ascii=False,indent=2))
    (page/'manifest.json').write_text(json.dumps({'Controllers':[{'Type':'Keypad','Actions':actions}],'Name':'Live Agents','Icon':''},indent=2))
    with zipfile.ZipFile(out/'Quoin Live Agents.streamDeckProfile','w',zipfile.ZIP_DEFLATED) as archive:
        for p in root.rglob('*'):
            if p.is_file():archive.write(p,p.relative_to(out))
    print(out)

if __name__=='__main__':build()
