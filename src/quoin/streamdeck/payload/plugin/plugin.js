'use strict';
// Elgato local WebSocket API. No external network connections or npm dependencies.
const {spawn} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

function loadConfig(dir = __dirname){
  try{
    const cfg = JSON.parse(fs.readFileSync(path.join(dir,'plugin-config.json'),'utf8'));
    if(!cfg || typeof cfg.python!=='string' || typeof cfg.runtime!=='string' || typeof cfg.state_root!=='string')return null;
    return cfg;
  }catch(e){return null;}
}
function buildInvocation(cfg, action, extra){
  const argv = ['-E','-s',path.join(cfg.runtime,'current','dashboard.py'),action,...(extra?[JSON.stringify(extra)]:[])];
  const env = {...process.env, QUOIN_DECK_RUNTIME: cfg.runtime, QUOIN_DECK_STATE_ROOT: cfg.state_root};
  return [cfg.python, argv, env];
}
// Only the non-interactive calls are bounded; menus, sessions, project and error dialogs wait on the user.
const BOUNDED_ACTIONS = new Set(['snapshot','focus']);
function invoke(cfg, action, extra, timeoutMs = BOUNDED_ACTIONS.has(action) ? (cfg.invoke_timeout_ms || 20000) : 0){
  return new Promise((resolve,reject)=>{
    const [bin,argv,env] = buildInvocation(cfg, action, extra);
    const bounded = timeoutMs > 0;
    let done=false, out='', err='', timer=null;
    const settle = (fn,v)=>{ if(done)return; done=true; if(timer)clearTimeout(timer); fn(v); };
    // A bounded call leads its own process group so the timeout can reap python and anything it started.
    const child = spawn(bin, argv, {env, stdio:['ignore','pipe','pipe'], detached: bounded});
    if(bounded)timer = setTimeout(()=>{
      try{process.kill(-child.pid,'SIGKILL');}catch(e){try{child.kill('SIGKILL');}catch(e2){}}
      settle(reject,new Error('timeout'));
    }, timeoutMs);
    child.stdout.on('data',x=>out+=x); child.stderr.on('data',x=>err+=x);
    child.on('error',e=>settle(reject,e));
    child.on('close',code=>code?settle(reject,new Error(err||'Action failed')):settle(resolve,out));
  });
}
// Only the failure class is written, never an error message (messages can carry payload text).
function writeHealth(cfg, obj){
  const dir = path.join(cfg.state_root,'state');
  fs.mkdirSync(dir,{recursive:true,mode:0o700});
  const target = path.join(dir,'health.json'), temp = target+'.'+process.pid+'.tmp';
  fs.writeFileSync(temp, JSON.stringify(obj), {mode:0o600});
  fs.renameSync(temp, target);
}
function errorType(e){
  const m = String(e && e.message || '');
  if(m==='timeout')return 'timeout';
  if(e && e.code==='ENOENT')return 'spawn';
  if(e instanceof SyntaxError)return 'parse';
  return 'exit';
}

function start(){
const cfg = loadConfig();
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
const call=(action,extra)=>invoke(cfg,action,extra);
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
async function refresh(){if(busy||!cfg)return;busy=true;try{agents=JSON.parse(await call('snapshot'));writeHealth(cfg,{updated:Date.now(),agents:agents.length});render();}catch(e){try{writeHealth(cfg,{updated:Date.now(),error_type:errorType(e)});}catch(h){}send('logMessage',undefined,{message:'Quoin snapshot: '+errorType(e)});}finally{busy=false;}}
function renderNoConfig(){for(const [context] of contexts){send('setImage',context,{image:icon(['НЕТ КОНФИГА','plugin-config.json'],'#6b2b2b'),target:0});}}
ws.addEventListener('open',()=>{ws.send(JSON.stringify({event:arg('-registerEvent'),uuid:arg('-pluginUUID')}));refresh();});
ws.addEventListener('message',async ({data})=>{
 try{const m=JSON.parse(data);
 if(m.event==='willAppear'){contexts.set(m.context,{settings:m.payload.settings||{}});cfg?render():renderNoConfig();}
 if(m.event==='willDisappear')contexts.delete(m.context);
 if(m.event==='didReceiveSettings'){const v=contexts.get(m.context);if(v){v.settings=m.payload.settings||{};render();}}
 const v=contexts.get(m.context);if(!v)return;
 if(m.event==='keyDown'){v.down=Date.now();v.pressed=v.target;}
 if(m.event==='keyUp'){
   if(actionBusy)return;actionBusy=true;
   try{
   const held=Date.now()-(v.down||Date.now())>650,role=v.settings.role||'agent';
   if(role==='agent' && v.pressed)await call('focus',v.pressed);
   else if(role==='attention'){
     if(held){page=(page+1)%Math.max(1,Math.ceil(agents.length/9));render();}
     else{const waiting=agents.filter(a=>a.status==='attention');if(waiting.length)await call('focus',waiting[pressedAttention++%waiting.length]);}
   }else if(role!=='agent')await call(role==='desk'&&held?'menu':role);
   await refresh();
   }finally{actionBusy=false;}
 }
 }catch(e){send('showAlert',JSON.parse(data).context,{});send('logMessage',undefined,{message:'Quoin action: '+errorType(e)});}
});
ws.addEventListener('close',()=>process.exit(0));
if(!cfg)send('logMessage',undefined,{message:'Quoin: plugin-config.json missing or invalid'});
setInterval(refresh,2500);setInterval(()=>cfg?render():renderNoConfig(),1000);
}

module.exports = {loadConfig, buildInvocation, invoke, writeHealth};
if (require.main === module) start();
