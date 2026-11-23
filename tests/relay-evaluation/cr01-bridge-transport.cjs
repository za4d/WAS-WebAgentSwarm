'use strict';
/* CR-01 real loopback HTTP/WS bridge, FAKE extension, never real Chrome/model. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const {createRequire}=require('node:module');
const ROOT=path.resolve(__dirname,'..','..');
const WS=createRequire(path.join(ROOT,'bridge','package.json'))('ws');
const TOKEN='q'.repeat(64); // Synthetic, not user authentication.
const BASE='http://127.0.0.1:18789';
const PIN='https://chatgpt.com/c/aaaa1111';
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const file=fs.readFileSync(path.join(ROOT,'bridge/server.js'));
const blob=crypto.createHash('sha1').update('blob '+file.length+'\0').update(file).digest('hex');
const results=[];
const suppressRepliesFor=new Set();
async function check(name,fn){
 try{await fn();results.push({name,verdict:'PASS'});}
 catch(e){results.push({name,verdict:'FAIL',error:String(e.stack||e).slice(0,800)});}
}
async function request(endpoint,method='GET',payload){
 const res=await fetch(BASE+endpoint,{method,headers:{
   'X-WAS-Token':TOKEN,'Content-Type':'application/json'
 },body:payload===undefined?undefined:JSON.stringify(payload),
 signal:AbortSignal.timeout(4000)});
 return {status:res.status,body:await res.json()};
}
async function waitReady(){
 for(let i=0;i<90;i++){
   try{if((await request('/health')).status===200)return;}
   catch{}
   await sleep(50);
 }
 throw Error('Timed out starting synthetic local bridge');
}
async function extension(received){
 return await new Promise((resolve,reject)=>{
  const ws=new WS('ws://127.0.0.1:18789/ws?token='+TOKEN,{
    headers:{Origin:'chrome-extension://synthetic-only'}
  });
  const timeout=setTimeout(()=>reject(Error('WS handshake timeout')),3000);
  ws.once('error',e=>{clearTimeout(timeout);reject(e);});
  ws.once('open',()=>ws.send(JSON.stringify({
    type:'hello',extensionVersion:'synthetic',protocolMin:5,protocolMax:5
  })));
  ws.on('message',bytes=>{
    const msg=JSON.parse(bytes.toString());
    if(msg.type==='hello-ack'){
      if(!msg.ok)return reject(Error('WS handshake failed'));
      clearTimeout(timeout);resolve(ws);return;
    }
    received.push(msg);
    if(msg.type==='send' && !suppressRepliesFor.has(msg.commandId))ws.send(JSON.stringify({
      type:'send-result',id:msg.id,ok:false,error:'Synthetic no-effect receipt'
    }));
  });
 });
}
async function main(){
 const child=spawn(process.execPath,['bridge/server.js'],{
   cwd:ROOT,env:{...process.env,WEB_SWARM_PORT:'18789',
     WEB_SWARM_BRIDGE_TOKEN:TOKEN,WEB_SWARM_BIND:'127.0.0.1'},
   stdio:'ignore',windowsHide:true
 });
 const clients=[];
 try{
  await waitReady();
  await check('T01 unauthorised HTTP request rejected',async()=>{
    const r=await fetch(BASE+'/health',{signal:AbortSignal.timeout(4000)});
    assert.equal(r.status,401);
  });
  await check('T02 no extension rejects /send',async()=>{
    const r=await request('/send','POST',{
      tabId:73,text:'fixture',commandId:'cr01_remote_1',expectedUrl:PIN
    });assert.equal(r.status,503);
  });
  const received=[];
  const one=await extension(received);clients.push(one);
  await sleep(50);
  await check('T03 good pinned envelope reaches one fake extension',async()=>{
    const r=await request('/send','POST',{
      tabId:73,text:'fixture',commandId:'cr01_remote_2',expectedUrl:PIN
    });
    assert.equal(r.status,502);assert.equal(received.length,1);
    assert.equal(received[0].expectedUrl,PIN);
    assert.equal(received[0].commandId,'cr01_remote_2');
    assert.equal(received[0].tabId,73);
  });
  await check('T04 absent target URL rejected without extension dispatch',async()=>{
    const r=await request('/send','POST',{
      tabId:73,text:'fixture',commandId:'cr01_remote_3'
    });assert.equal(r.status,400);assert.equal(received.length,1);
  });
  await check('T05 impersonating origin rejected without dispatch',async()=>{
    const r=await request('/send','POST',{
      tabId:73,text:'fixture',commandId:'cr01_remote_3',
      expectedUrl:'https://chatgpt.com.evil.invalid/c/other'
    });assert.equal(r.status,400);assert.equal(received.length,1);
  });
  await check('T06 missing unique command ID rejected without dispatch',async()=>{
    const r=await request('/send','POST',{
      tabId:73,text:'fixture',expectedUrl:PIN
    });assert.equal(r.status,400);assert.equal(received.length,1);
  });
  await check('T08 pending Send receipt must come from selected extension socket',async()=>{
    const commandId='cr01_origin_bound_ack_1';
    suppressRepliesFor.add(commandId);
    const before=received.length;
    let settled=false;
    const awaiting=request('/send','POST',{
      tabId:73,text:'fixture',commandId,expectedUrl:PIN
    }).then(response=>{settled=true;return response;});
    for(let i=0;i<70&&received.length===before;i++)await sleep(20);
    assert.equal(received.length,before+1,'Selected extension did not receive request');
    const original=received.at(-1);
    const other=[];
    const impostor=await extension(other);clients.push(impostor);
    try{
      impostor.send(JSON.stringify({
        type:'send-result',id:original.id,ok:true,
        claimedBy:'second-profile-not-selected'
      }));
      await sleep(120);
      assert.equal(settled,false,
        'Bridge accepted an ACK from a different connected profile');
      one.send(JSON.stringify({
        type:'send-result',id:original.id,ok:false,
        error:'Genuine selected client acknowledgement'
      }));
      const response=await awaiting;
      assert.equal(response.status,502);
      assert.equal(response.body.error,'Genuine selected client acknowledgement');
    }finally{
      suppressRepliesFor.delete(commandId);
      impostor.terminate();
      for(let i=0;i<50;i++){
        const status=await request('/health');
        if(status.body.extensionClients===1)break;
        await sleep(20);
      }
    }
  });
  await check('T07 two connected extensions cause fail-closed routing',async()=>{
    const previouslyReceived=received.length;
    const other=[];const second=await extension(other);clients.push(second);
    await sleep(50);
    const health=await request('/health');
    assert.equal(health.body.extensionClients,2);
    const r=await request('/send','POST',{
      tabId:73,text:'fixture',commandId:'cr01_remote_4',expectedUrl:PIN
    });
    assert.equal(r.status,409);
    assert.equal(received.length,previouslyReceived);assert.equal(other.length,0);
  });
 }finally{
  for(const c of clients){try{c.terminate();}catch{}}
  try{child.kill();}catch{}
 }
 const passed=results.filter(r=>r.verdict==='PASS').length;
 console.log(JSON.stringify({id:'CR01-REAL-BRIDGE-LOOPBACK',platform:process.platform,
  source_git_blob:blob,evidence:'actual Node HTTP/WebSocket bridge with fake clients only',
  total:results.length,passed,failed:results.length-passed,results},null,2));
 if(passed!==results.length)process.exitCode=1;
}
main().catch(e=>{console.error('HARNESS_ERROR',e);process.exitCode=1;});
