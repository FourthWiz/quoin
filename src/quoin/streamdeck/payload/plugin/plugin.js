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
// Bounded children lead their own process group; the plugin reaps them if it exits first.
const liveBounded = new Set();
function killBounded(){
  for(const pid of liveBounded){
    try{process.kill(-pid,'SIGKILL');}catch(e){try{process.kill(pid,'SIGKILL');}catch(e2){}}
  }
  liveBounded.clear();
}
function invoke(cfg, action, extra, timeoutMs = BOUNDED_ACTIONS.has(action) ? (cfg.invoke_timeout_ms || 20000) : 0){
  return new Promise((resolve,reject)=>{
    const [bin,argv,env] = buildInvocation(cfg, action, extra);
    const bounded = timeoutMs > 0;
    let done=false, out='', err='', timer=null;
    const settle = (fn,v)=>{ if(done)return; done=true; if(timer)clearTimeout(timer); fn(v); };
    // A bounded call leads its own process group so the timeout can reap python and anything it started.
    const child = spawn(bin, argv, {env, stdio:['ignore','pipe','pipe'], detached: bounded});
    if(bounded && child.pid)liveBounded.add(child.pid);
    const forget = ()=>{ if(child.pid)liveBounded.delete(child.pid); };
    if(bounded)timer = setTimeout(()=>{
      try{process.kill(-child.pid,'SIGKILL');}catch(e){try{child.kill('SIGKILL');}catch(e2){}}
      settle(reject,new Error('timeout'));
    }, timeoutMs);
    child.stdout.on('data',x=>out+=x); child.stderr.on('data',x=>err+=x);
    child.on('error',e=>{forget();settle(reject,e);});
    child.on('close',(code,sig)=>{forget();(code||sig)?settle(reject,new Error(err||'Action failed')):settle(resolve,out);});
  });
}
// Only the failure class is written, never an error message (messages can carry payload text).
function writeHealth(cfg, obj){
  const dir = path.join(cfg.state_root,'state');
  fs.mkdirSync(dir,{recursive:true,mode:0o700});
  const target = path.join(dir,'health.json'), temp = target+'.'+process.pid+'.tmp';
  try{fs.unlinkSync(temp);}catch(e){}
  fs.writeFileSync(temp, JSON.stringify(obj), {mode:0o600, flag:'wx'});
  fs.renameSync(temp, target);
}
function errorType(e){
  const m = String(e && e.message || '');
  if(m==='timeout')return 'timeout';
  if(e && e.code==='ENOENT')return 'spawn';
  if(e instanceof SyntaxError)return 'parse';
  return 'exit';
}

const labels={working:'WORKING',attention:'NEEDS YOU',done:'DONE',idle:'IDLE',unknown:'NO SIGNAL',offline:'OFFLINE'};
const colors={working:'#175583',attention:'#bd4d10',done:'#227648',idle:'#3c4859',unknown:'#4c4559',offline:'#453b3b'};
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));}
function icon(lines,color){return 'data:image/svg+xml;base64,'+Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144"><rect width="144" height="144" rx="14" fill="${color}"/>${lines.map((s,i)=>`<text x="72" y="${30+i*28}" fill="white" font-family="Helvetica,Arial" font-size="${i===0?18:14}" text-anchor="middle">${esc(s)}</text>`).join('')}</svg>`).toString('base64');}
function agentFace(rec, nowMs){
  const status=rec.status;
  let color=colors[status]||colors.unknown;
  if(status==='attention' && Math.floor(nowMs/1000)%2)color='#e27916';
  const session=String(rec.session ?? ''), pane=String(rec.pane ?? '');
  return {lines:[rec.kind==='claude'?'CLAUDE':'CODEX',path.basename(String(rec.project||rec.session||'')).slice(0,16),`${session.slice(-9)} · ${pane}`,labels[status]||labels.unknown],color};
}

// Panel model: pure functions over the snapshot list and a small view object. No I/O, no clock.
const STATUS_ORDER=['attention','done','working','idle','unknown','offline'];
function rank(status){const i=STATUS_ORDER.indexOf(status);return i<0?STATUS_ORDER.indexOf('unknown'):i;}
function hasSession(rec){return typeof rec.session==='string' && rec.session.trim()!=='';}
function groupKey(rec){return JSON.stringify([String(rec.socket ?? ''),rec.session]);}
function aggregateStatus(records){
  let best=STATUS_ORDER.indexOf('unknown');
  if(!records.length)return STATUS_ORDER[best];
  best=STATUS_ORDER.length;
  for(const r of records)best=Math.min(best,rank(r.status));
  return STATUS_ORDER[best];
}
function groupAgents(records){
  if(!Array.isArray(records))return [];
  const groups=new Map(), standalone=[];
  for(const rec of records){
    if(!rec || typeof rec!=='object' || Array.isArray(rec))continue;
    if(!hasSession(rec)){standalone.push(Object.freeze({type:'agent',agent:rec}));continue;}
    const key=groupKey(rec);
    if(!groups.has(key))groups.set(key,{key,socket:String(rec.socket ?? ''),session:rec.session,list:[]});
    groups.get(key).list.push(rec);
  }
  const sessions=[...groups.values()].sort((a,b)=>a.session<b.session?-1:a.session>b.session?1:a.socket<b.socket?-1:a.socket>b.socket?1:0).map(g=>{
    const projects=new Set(g.list.map(r=>typeof r.project==='string'?r.project:''));
    const shared=projects.size===1?[...projects][0]:'';
    return Object.freeze({type:'session',key:g.key,socket:g.socket,session:g.session,agents:Object.freeze(g.list),
      status:aggregateStatus(g.list),project:shared?path.basename(shared):''});
  });
  return [...sessions,...standalone];
}
function initialView(){return {mode:'collapsed',collapsedPage:0,expandedKey:null,expandedPage:0};}
function expandedGroup(view,items){return items.find(i=>i.type==='session' && i.key===view.expandedKey)||null;}
function pageCount(view,items){
  if(view.mode==='expanded'){const g=expandedGroup(view,items);return Math.max(1,Math.ceil((g?g.agents.length:0)/8));}
  return Math.max(1,Math.ceil(items.length/9));
}
function currentPage(view){return view.mode==='expanded'?view.expandedPage:view.collapsedPage;}
function reconcile(view,items){
  if(view.mode==='expanded' && !expandedGroup(view,items))view={...view,mode:'collapsed',expandedKey:null,expandedPage:0};
  const last=pageCount(view,items)-1;
  return view.mode==='expanded'
    ?{...view,expandedPage:Math.max(0,Math.min(view.expandedPage,last))}
    :{...view,collapsedPage:Math.max(0,Math.min(view.collapsedPage,last))};
}
function layoutSlots(view,items){
  const slots=[];
  if(view.mode==='expanded'){
    const g=expandedGroup(view,items);
    slots.push(g?Object.freeze({type:'back',group:g}):null);
    for(let i=1;i<9;i++){const a=g?g.agents[view.expandedPage*8+i-1]:undefined;slots.push(a?Object.freeze({type:'agent',agent:a}):null);}
    return slots;
  }
  for(let i=0;i<9;i++){
    const item=items[view.collapsedPage*9+i];
    slots.push(!item?null:item.type==='session'?Object.freeze({type:'session',group:item}):item);
  }
  return slots;
}
function press(view,target){
  if(!target)return {view,focus:null};
  if(target.type==='session')return {view:{...view,mode:'expanded',expandedKey:target.group.key,expandedPage:0},focus:null};
  if(target.type==='back')return {view:{...view,mode:'collapsed',expandedKey:null,expandedPage:0},focus:null};
  if(target.type==='agent')return {view,focus:target.agent};
  return {view,focus:null};
}
function nextPage(view,items){
  const next=(currentPage(view)+1)%pageCount(view,items);
  return view.mode==='expanded'?{...view,expandedPage:next}:{...view,collapsedPage:next};
}
function face(target,nowMs){
  if(!target)return {lines:['—','','',''],color:'#18202a'};
  if(target.type==='back')return {lines:['← BACK',target.group.session.slice(0,16),'',''],color:'#213a4e'};
  if(target.type==='agent')return agentFace(target.agent,nowMs);
  const g=target.group, suffix=' · '+g.agents.length;
  let color=colors[g.status]||colors.unknown;
  if(g.status==='attention' && Math.floor(nowMs/1000)%2)color='#e27916';
  return {lines:[g.session.slice(0,Math.max(0,12-suffix.length))+suffix,g.project.slice(0,16),'',labels[g.status]||labels.unknown],color};
}
function attentionFace(waiting,page,pages){
  return {lines:['ATTENTION',String(waiting),'Page '+(page+1)+'/'+pages,'Hold: next page'],color:waiting?'#bd4d10':'#273b43'};
}

function createPanel({send, call, refresh = async()=>{}, playSound = ()=>{}, now = Date.now, hasConfig = true}){
const contexts = new Map();
let agents=[], view=initialView(), lastAttention=null, pressedAttention=0, actionBusy=false;
function setAgents(list){agents=Array.isArray(list)?list:[];}
function render(){
 if(!hasConfig){renderNoConfig();return;}
 const items=groupAgents(agents);
 view=reconcile(view,items);
 const slots=layoutSlots(view,items);
 const waiting=agents.filter(a=>a && a.status==='attention');
 for(const [context,v] of contexts){
   let lines,color='#213a4e',target=null;
   const role=v.settings.role||'agent';
   if(role==='agent'){
     const slot=Number(v.settings.slot)||0; target=slots[slot]||null;
     ({lines,color}=face(target,now()));
   }else if(role==='attention'){({lines,color}=attentionFace(waiting.length,currentPage(view),pageCount(view,items)));}
   else{lines=({desk:['DESK','2 Codex + Shell','','Hold: menu'],claude:['+ CLAUDE','New pane'],codex:['+ CODEX','New pane'],'prev-tab':['← TAB','Ghostty'],'next-tab':['TAB →','Ghostty']})[role]||['Agent Desk'];}
   v.target=target;
   const img=icon(lines,color);
   if(img!==v.image){send('setImage',context,{image:img,target:0});send('setTitle',context,{title:'',target:0});v.image=img;}
 }
 const signature=waiting.map(a=>a.id).sort();
 if(lastAttention!==null && signature.some(id=>!lastAttention.includes(id)))playSound();
 lastAttention=signature;
}
function renderNoConfig(){for(const [context] of contexts){send('setImage',context,{image:icon(['NO CONFIG','plugin-config.json'],'#6b2b2b'),target:0});}}
async function onMessage(m){
 if(m.event==='willAppear'){contexts.set(m.context,{settings:m.payload.settings||{}});render();}
 if(m.event==='willDisappear')contexts.delete(m.context);
 if(m.event==='didReceiveSettings'){const v=contexts.get(m.context);if(v){v.settings=m.payload.settings||{};render();}}
 const v=contexts.get(m.context);if(!v)return;
 if(m.event==='keyDown'){v.down=now();v.pressed=v.target;}
 if(m.event==='keyUp'){
   const held=now()-(v.down||now())>650,role=v.settings.role||'agent';
   // Opening a session, going back and turning the page only change the view, so they never wait behind a slow focus call.
   // These presses (including holding the attention key) skip the busy flag and the trailing refresh, so the page shows data up to one refresh interval old.
   if(role==='agent'){
     const t=v.pressed;
     if(t && (t.type==='session' || t.type==='back')){view=reconcile(press(view,t).view,groupAgents(agents));render();return;}
   }else if(role==='attention' && held){const items=groupAgents(agents);view=reconcile(nextPage(view,items),items);render();return;}
   if(actionBusy)return;actionBusy=true;
   try{
   if(role==='agent'){const r=press(view,v.pressed);if(r.focus)await call('focus',r.focus);}
   else if(role==='attention'){const waiting=agents.filter(a=>a && a.status==='attention');if(waiting.length)await call('focus',waiting[pressedAttention++%waiting.length]);}
   else await call(role==='desk'&&held?'menu':role);
   await refresh();
   }finally{actionBusy=false;}
 }
}
return {contexts, setAgents, render, onMessage};
}

function start(){
const cfg = loadConfig();
const args = process.argv.slice(2);
const arg = flag => args[args.indexOf(flag)+1];
const ws = new WebSocket('ws://127.0.0.1:'+arg('-port'));
let busy=false;
function send(event,context,payload){if(ws.readyState===1)ws.send(JSON.stringify({event,context,payload}));}
const call=(action,extra)=>invoke(cfg,action,extra);
const playSound=()=>spawn('/usr/bin/afplay',['/System/Library/Sounds/Glass.aiff'],{stdio:'ignore'}).on('error',()=>{});
const panel=createPanel({send, call, refresh, playSound, now:Date.now, hasConfig:!!cfg});
async function refresh(){if(busy||!cfg)return;busy=true;try{const list=JSON.parse(await call('snapshot'));if(!Array.isArray(list))throw new SyntaxError('snapshot is not a list');panel.setAgents(list);writeHealth(cfg,{updated:Date.now(),agents:list.length});panel.render();}catch(e){try{writeHealth(cfg,{updated:Date.now(),error_type:errorType(e)});}catch(h){}send('logMessage',undefined,{message:'Quoin snapshot: '+errorType(e)});}finally{busy=false;}}
ws.addEventListener('open',()=>{ws.send(JSON.stringify({event:arg('-registerEvent'),uuid:arg('-pluginUUID')}));refresh();});
ws.addEventListener('message',async ({data})=>{
 try{await panel.onMessage(JSON.parse(data));}
 catch(e){send('showAlert',JSON.parse(data).context,{});send('logMessage',undefined,{message:'Quoin action: '+errorType(e)});}
});
ws.addEventListener('close',()=>{killBounded();process.exit(0);});
if(!cfg)send('logMessage',undefined,{message:'Quoin: plugin-config.json missing or invalid'});
setInterval(refresh,2500);setInterval(()=>panel.render(),1000);
}

module.exports = {loadConfig, buildInvocation, invoke, writeHealth, killBounded, createPanel, agentFace, icon, labels, colors,
  STATUS_ORDER, rank, hasSession, groupKey, aggregateStatus, groupAgents, initialView, pageCount, reconcile,
  layoutSlots, press, nextPage, currentPage, face, attentionFace};
if (require.main === module) start();
