#!/usr/bin/python3
"""Build a native, importable 15-key Stream Deck profile and app launchers."""
import argparse
import json
import plistlib
import shlex
import shutil
import uuid
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BUTTONS = [
 ('desk','Открыть\nDesk','DESK','#22586b'),
 ('sessions','Сессии','LIST','#22586b'),
 ('project','Проект','DIR','#22586b'),
 ('claude','+ Claude','C+','#88563f'),
 ('codex','+ Codex','O+','#31624e'),
 ('prev-pane','Панель <','<','#34445d'),
 ('next-pane','Панель >','>','#34445d'),
 ('fullscreen','Развернуть','FULL','#34445d'),
 ('prev-tab','Вкладка <','< TAB','#34445d'),
 ('next-tab','Вкладка >','TAB >','#34445d'),
 ('shell','+ Shell','>_','#555078'),
 ('git','Git обзор','GIT','#555078'),
 ('floating','Плавающие','FLOAT','#555078'),
 ('save','Сохранить','SAVE','#555078'),
 ('codex-app','Codex App','APP','#31624e')]


def build(out, install):
    out.mkdir(parents=True, exist_ok=True)
    launchers = out / 'Buttons'
    rootid = '5BD869D4-0D3C-4A42-9823-7051BBA16CF0'
    pageid = 'B9351352-8885-4B67-B454-7F24552D3D97'
    root = out / (rootid + '.sdProfile')
    page = root / 'Profiles' / pageid
    images = page / 'Images'
    images.mkdir(parents=True, exist_ok=True)
    actions = {}
    preview = Image.new('RGB', (940, 580), '#101720')
    large = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 23)
    small = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 15)
    for i, (action,title,symbol,color) in enumerate(BUTTONS):
        bundle = launchers / (action + '.app')
        macos = bundle / 'Contents/MacOS'
        macos.mkdir(parents=True, exist_ok=True)
        info = {'CFBundleName':'Quoin ' + action,'CFBundleIdentifier':'local.quoin.deck.'+action,
                'CFBundleExecutable':'launch','CFBundlePackageType':'APPL',
                'CFBundleVersion':'1','LSUIElement':True}
        (bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        script = '#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(install/'controller.py')) + ' ' + shlex.quote(action) + '\n'
        (macos/'launch').write_text(script)
        (macos/'launch').chmod(0o755)
        icon = Image.new('RGB',(144,144),color)
        d = ImageDraw.Draw(icon)
        d.rounded_rectangle((5,5,138,138),radius=18,outline='#a0b1c4',width=2)
        d.text((72,46),symbol,font=large,fill='white',anchor='mm')
        d.multiline_text((72,99),title,font=small,fill='white',anchor='mm',align='center',spacing=4)
        icon.save(images/(action+'.png'))
        preview.paste(icon,(22+(i%5)*185,18+(i//5)*185))
        target = install/'Buttons'/(action+'.app')
        actions[f'{i%5},{i//5}'] = {
          'ActionID':str(uuid.uuid4()),'LinkedTitle':False,'Name':'Open',
          'Plugin':{'Name':'Open','UUID':'com.elgato.streamdeck.system.open','Version':'1.0'},
          'Settings':{'path':'"'+str(target).replace('"','\\"')+'"'},'State':0,
          'States':[{'Title':title,'ShowTitle':False,'Image':'Images/'+action+'.png',
                     'FontSize':12,'TitleColor':'#ffffff','TitleAlignment':'bottom'}],
          'UUID':'com.elgato.streamdeck.system.open'}
    manifest={'Device':{'Model':'20GAA9901','UUID':''},'Name':'Quoin Agent Desk — 15',
              'Pages':{'Current':pageid.lower(),'Default':pageid.lower(),'Pages':[pageid.lower()]},'Version':'3.0'}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    (page/'manifest.json').write_text(json.dumps({'Controllers':[{'Type':'Keypad','Actions':actions}], 'Name':'Quoin','Icon':''},ensure_ascii=False,indent=2))
    with zipfile.ZipFile(out/'Quoin Agent Desk.streamDeckProfile','w',zipfile.ZIP_DEFLATED) as archive:
        for p in root.rglob('*'):
            if p.is_file(): archive.write(p,p.relative_to(out))
    preview.save(out/'layout.png')
    (out/'layout.json').write_text(json.dumps([{'button':i+1,'action':a,'title':t} for i,(a,t,_,_) in enumerate(BUTTONS)],ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--install',type=Path,required=True)
    args=parser.parse_args()
    build(args.out,args.install)
