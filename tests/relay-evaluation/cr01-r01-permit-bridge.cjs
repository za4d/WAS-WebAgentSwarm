'use strict';
/* Actual bridge/server.js and localhost HTTP/WS; fake extension, no Chrome. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {createRequire}=require('node:module');
const ROOT=path.resolve(__dirname,'..','..');
const WS=createRequire(path.join(ROOT,'bridge','package.json'))('ws');
const BEARER='a'.repeat(64),SIGNER='r8-secret-'.repeat(7);
const PIN='https://chatgpt.com/c/rrrr3333',TEXT='  PRECISE\nmessage  ';
const results=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let port=18981;
async function check(label,fn){try{await fn();results.push({label,verdict:'PASS'});}
 catch(e){results.push({label,verdict:'FAIL',detail:String(e.stack||e).slice(0,900)});}}
function signed(override={}){
 const p={v:1,attemptId:'f'.repeat(32),recipient:'R',generation:1,
  commandId:'r8_'+'f'.repeat(32),tabId:73,expectedUrl:PIN,
  bodySha256:crypto.createHash('sha256').update(TEXT).digest('hex'),
  issuedAt:Math.floor(Date.now()/1000),...override};
 const msg=['R8WAS1',p.attemptId,p.recipient,p.generation,p.commandId,
  p.tabId,p.expectedUrl,p.bodySha256,p.issuedAt].join('\n');
 p.signature=crypto.createHmac('sha256',SIGNER).update(msg).digest('hex');
 return p;
}
async function start(policy,receiptDir){
 const current=++port,errors=[];
 const env={...process.env,WEB_SWARM_BIND:'127.0.0.1',WEB_SWARM_PORT:String(current),
  WEB_SWARM_BRIDGE_TOKEN:BEARER,WEB_SWARM_SEND_MODE:policy||'deny',
  WEB_SWARM_RELAY_PERMIT_KEY:SIGNER,WEB_SWARM_RELAY_RECEIPTS_DIR:receiptDir};
 const child=spawn(process.execPath,['bridge/server.js'],{
  cwd:ROOT,env,stdio:['ignore','ignore','pipe'],windowsHide:true});
 child.stderr.on('data',x=>errors.push(x.toString()));
 const url='http://127.0.0.1:'+current;
 const fetchit=async(payload)=>{const r=await fetch(url+'/send',{method:'POST',
  headers:{'X-WAS-Token':BEARER,'Content-Type':'application/json'},
  body:JSON.stringify(payload),signal:AbortSignal.timeout(5500)});
  return {status:r.status,body:await r.json()};};
 let ready=false;
 for(let i=0;i<85&&!ready;i++){
  try{const h=await fetch(url+'/health',{headers:{'X-WAS-Token':BEARER},
    signal:AbortSignal.timeout(500)});
    if(h.status===200)ready=true;}catch{}if(!ready)await sleep(35);
 }
 if(!ready)throw Error('Test bridge startup failed '+errors.join('').slice(-700));
 const received=[];
 const ws=await new Promise((resolve,reject)=>{
  const socket=new WS('ws://127.0.0.1:'+current+'/ws?token='+BEARER,{
    headers:{Origin:'chrome-extension://synthetic-authority'}
  });
  const timeout=setTimeout(()=>reject(Error('WS handshake timeout')),2500);
  socket.once('error',reject);
  socket.once('open',()=>socket.send(JSON.stringify({
   type:'hello',extensionVersion:'synthetic',protocolMin:5,protocolMax:5
  })));
  socket.on('message',bytes=>{
   const data=JSON.parse(bytes.toString());
   if(data.type==='hello-ack'){clearTimeout(timeout);resolve(socket);return;}
   if(data.type==='send'){
    received.push(data);
    socket.send(JSON.stringify({type:'send-result',id:data.id,ok:false,
     error:'Fake extension returned no physical effect',mayHaveExecuted:false}));
   }
  });
 });
 return {fetchit,received,stop:async()=>{ws.terminate();child.kill();try{await new Promise(r=>child.once('exit',r));}catch{}}};
}
const payload=p=>({tabId:73,text:TEXT,commandId:'r8_'+'f'.repeat(32),
 expectedUrl:PIN,...(p===null?{}:{r8Permit:p})});
(async()=>{
 const temp=fs.mkdtempSync(path.join(os.tmpdir(),'cr01-r8-admission-'));
 const receipts=path.join(temp,'receipts');
 let current;
 try{
  current=await start('deny',receipts);
  await check('R01-A default policy rejects bearer-only valid Send',async()=>{
   const r=await current.fetchit(payload(null));assert.equal(r.status,403);
   assert.equal(current.received.length,0);
  });
  await current.stop();current=null;
  current=await start('relay_required',receipts);
  await check('R01-B relay mode rejects bearer-only valid Send',async()=>{
   const r=await current.fetchit(payload(null));assert.equal(r.status,403);
   assert.equal(current.received.length,0);
  });
  await check('R01-C forged body cannot reuse signed permit',async()=>{
   const r=await current.fetchit({...payload(signed()),text:'OTHER BODY'});
   assert.equal(r.status,403);assert.equal(current.received.length,0);
  });
  await check('R01-D forged recipient/attempt signature rejected',async()=>{
   const p=signed();p.recipient='X';
   const r=await current.fetchit(payload(p));
   assert.equal(r.status,403);assert.equal(current.received.length,0);
  });
  await check('R01-E valid synthetic R8-bound permit dispatches exactly once',async()=>{
   const r=await current.fetchit(payload(signed()));
   assert.equal(r.status,502);assert.equal(current.received.length,1);
   assert.equal(current.received[0].text,TEXT);
   assert.ok(fs.existsSync(path.join(receipts,'f'.repeat(32)+'.json')));
  });
  await check('R01-F repeated identical permit blocked before fake extension',async()=>{
   const r=await current.fetchit(payload(signed()));
   assert.equal(r.status,409);assert.equal(current.received.length,1);
  });
  await current.stop();current=null;
  current=await start('relay_required',receipts);
  await check('R01-G restart retains atomic permit consumption fence',async()=>{
   const r=await current.fetchit(payload(signed()));
   assert.equal(r.status,409);assert.equal(current.received.length,0);
  });
  await check('R01-H expired permit fails before physical attempt',async()=>{
   const r=await current.fetchit(payload(signed({attemptId:'e'.repeat(32),
    commandId:'r8_'+'e'.repeat(32),issuedAt:Math.floor(Date.now()/1000)-120})));
   assert.equal(r.status,403);assert.equal(current.received.length,0);
  });
 }finally{
  if(current)await current.stop();
  fs.rmSync(temp,{recursive:true,force:true});
 }
 const passed=results.filter(x=>x.verdict==='PASS').length;
 console.log(JSON.stringify({suite:'R01-SIGNED-R8-SEND-AUTHORITY',platform:process.platform,
  source:process.env.GITHUB_SHA||'',passed,total:results.length,failed:results.length-passed,
  model:'real Node HTTP/WebSocket server with fake extension; no actual Chrome click',
  results},null,2));
 if(passed!==results.length)process.exitCode=1;
})().catch(e=>{console.error('R01 HARNESS',e);process.exitCode=1;});
