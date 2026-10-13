'use strict';
// Elgato local WebSocket API. No external network connections or npm dependencies.
const {spawn} = require('node:child_process');
const os = require('node:os');
const path = require('node:path');
const fs = require('node:fs');
const base = process.env.QUOIN_DECK_RUNTIME || path.join(os.homedir(),'Library/Application Support/QuoinStreamDeck');
const args = process.argv.slice(2);
const arg = flag => args[args.indexOf(flag)+1];
const ws = new WebSocket('ws://127.0.0.1:'+arg('-port'));
const contexts = new Map();
let agents=[], page=0, lastAttention=null, busy=false, pressedAttention=0, actionBusy=false;
const labels={working:'РАБОТАЕТ',attention:'НУЖЕН ТЫ',done:'ГОТОВО',idle:'ОЖИДАЕТ',unknown:'НЕТ СИГНАЛА',offline:'НЕДОСТУПЕН'};
const colors={working:'#175583',attention:'#bd4d10',done:'#227648',idle:'#3c4859',unknown:'#4c4559',offline:'#453b3b'};
function send(event,context,payload){if(ws.readyState===1)ws.send(JSON.stringify({event,context,payload}));}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));}
function icon(lines,color){return 'data:image/svg+xml;base64,'+Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144"><rect width="144" height="144" rx="14" fill="${color}"/>${lines.map((s,i)=>`<text x="72" y="${30+i*28}" fill="white" font-family="Helvetica,Arial" font-size="${i===0?18:14}" text-anchor="middle">${esc(s)}</text>`).join('')}</svg>`).toString('base64');}
function invoke(action,extra){return new Promise((resolve,reject)=>{const child=spawn('/usr/bin/python3',[path.join(base,'dashboard.py'),action,...(extra?[JSON.stringify(extra)]:[])],{stdio:['ignore','pipe','pipe']});let out='',err=''; child.stdout.on('data',s=>out+=s);child.stderr.on('data',s=>err+=s);child.on('error',reject);child.on('close',code=>code?reject(new Error(err||'Action failed')):resolve(out));});}
function render(){
 const pages=Math.max(1,Math.ceil(agents.length/9));page=Math.min(page,pages-1);
 const waiting=agents.filter(a=>a.status==='attention');
 for(const [context,v] of contexts){
   let lines,color='#213a4e',target=null;
   const role=v.settings.role||'agent';
   if(role==='agent'){
     const slot=Number(v.settings.slot)||0; target=agents[page*9+slot]||null;
     if(target){let status=target.status; color=colors[status]||colors.unknown;
       if(status==='attention' && Math.floor(Date.now()/1000)%2)color='#e27916';
       lines=[target.kind==='claude'?'CLAUDE':'CODEX',path.basename(target.project||target.session).slice(0,16),`${target.session.slice(-9)} · ${target.pane}`,labels[status]||labels.unknown];
     }else{lines=['—','Агент '+(page*9+slot+1),'',''];color='#18202a';}
   }else if(role==='attention'){lines=['ВНИМАНИЕ',String(waiting.length),`Страница ${page+1}/${pages}`,'Удержать: листать'];color=waiting.length?'#bd4d10':'#273b43';}
   else{lines=({desk:['DESK','2 Codex + Shell','','Удержать: меню'],claude:['+ CLAUDE','Новая панель'],codex:['+ CODEX','Новая панель'],'prev-tab':['← ВКЛАДКА','Ghostty'],'next-tab':['ВКЛАДКА →','Ghostty']})[role]||['Agent Desk'];}
   v.target=target;
   const img=icon(lines,color);
   if(img!==v.image){send('setImage',context,{image:img,target:0});send('setTitle',context,{title:'',target:0});v.image=img;}
 }
 const signature=waiting.map(a=>a.id).sort();
 if(lastAttention!==null && signature.some(id=>!lastAttention.includes(id)))spawn('/usr/bin/afplay',['/System/Library/Sounds/Glass.aiff'],{stdio:'ignore'}).on('error',()=>{});
 lastAttention=signature;
}
async function refresh(){if(busy)return;busy=true;try{agents=JSON.parse(await invoke('snapshot'));fs.writeFileSync(path.join(base,'health.json'),JSON.stringify({updated:Date.now(),agents:agents.length}));render();}catch(e){fs.writeFileSync(path.join(base,'health.json'),JSON.stringify({updated:Date.now(),error:e.message}));send('logMessage',undefined,{message:'Quoin snapshot: '+e.message});}finally{busy=false;}}
ws.addEventListener('open',()=>{ws.send(JSON.stringify({event:arg('-registerEvent'),uuid:arg('-pluginUUID')}));refresh();});
ws.addEventListener('message',async ({data})=>{
 try{const m=JSON.parse(data);
 if(m.event==='willAppear'){contexts.set(m.context,{settings:m.payload.settings||{}});render();}
 if(m.event==='willDisappear')contexts.delete(m.context);
 if(m.event==='didReceiveSettings'){const v=contexts.get(m.context);if(v){v.settings=m.payload.settings||{};render();}}
 const v=contexts.get(m.context);if(!v)return;
 if(m.event==='keyDown'){v.down=Date.now();v.pressed=v.target;}
 if(m.event==='keyUp'){
   if(actionBusy)return;actionBusy=true;
   try{
   const held=Date.now()-(v.down||Date.now())>650,role=v.settings.role||'agent';
   if(role==='agent' && v.pressed)await invoke('focus',v.pressed);
   else if(role==='attention'){
     if(held){page=(page+1)%Math.max(1,Math.ceil(agents.length/9));render();}
     else{const waiting=agents.filter(a=>a.status==='attention');if(waiting.length)await invoke('focus',waiting[pressedAttention++%waiting.length]);}
   }else if(role!=='agent')await invoke(role==='desk'&&held?'menu':role);
   await refresh();
   }finally{actionBusy=false;}
 }
 }catch(e){send('showAlert',JSON.parse(data).context,{});send('logMessage',undefined,{message:'Quoin action: '+e.message});}
});
ws.addEventListener('close',()=>process.exit(0));
setInterval(refresh,2500);setInterval(render,1000);
