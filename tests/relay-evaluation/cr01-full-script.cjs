'use strict';
/**
 * Workstream A / CR-01 whole-script, OFFLINE ONLY.
 * Tests actual content.js and background.js in isolated VM contexts with
 * synthetic Chrome, sessionStorage, WebSocket and DOM. No live browser, host
 * process, credentials, network or ChatGPT invocation.
 *
 * Exit 0 iff all deterministic checks pass. Original source hashes printed.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

const ROOT = path.resolve(__dirname, '..', '..');
const read = file => fs.readFileSync(path.join(ROOT, file), 'utf8');
const SOURCE = {
  content: read('browser-extension/content.js'),
  background: read('browser-extension/background.js'),
  bridge: read('bridge/server.js')
};
const hash = source => crypto.createHash('sha1')
  .update('blob ' + Buffer.byteLength(source) + '\0' + source)
  .digest('hex');
const GOOD = 'https://chatgpt.com/c/aaaa1111';
const WRONG = 'https://chatgpt.com/c/bbbb2222';
const TEXT = 'WAS-CR01-SYNTHETIC-TEXT';
const flush = async (n=25) => { for(let i=0;i<n;i++) await Promise.resolve(); };
const settle=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const storageMap = () => {
  const values = new Map();
  return {
    values,
    getItem(k) { return values.get(k) ?? null; },
    setItem(k, v) { values.set(k, String(v)); },
    removeItem(k) { values.delete(k); }
  };
};

function contentFixture({
  url=GOOD, draft='', attachment=false, disabled=false,
  storage=storageMap(), navigateOnInput=false, mutateOnInput=false,
  storageDenied=false
}={}) {
  const state = { clicks:0, draft, inputEvents:0, location:{href:url}, listeners:[] };
  const editor = {
    isConnected:true, focus() {},
    get innerText(){ return state.draft; },
    get textContent(){ return state.draft; },
    get value(){ return state.draft; },
    querySelector(){ return null; },
    replaceChildren(){ state.draft=''; },
    appendChild(elem){ state.draft=String(elem.textContent||''); },
    dispatchEvent(evt){
      if(evt.type==='input'){
        state.inputEvents++;
        if(navigateOnInput) state.location.href = WRONG;
        if(mutateOnInput) state.draft=TEXT+' ';
      }
    }
  };
  const button = { disabled, click(){ state.clicks++; } };
  const document = {
    title:'Synthetic ChatGPT',
    querySelector(selector){
      if(selector==='#prompt-textarea') return editor;
      if(selector==='[data-testid="send-button"]') return button;
      if(attachment && selector.includes('composer-attachments')) return {tagName:'DIV'};
      return null;
    },
    createElement(){ return {textContent:''}; }
  };
  const chrome = {runtime:{onMessage:{addListener(fn){state.listeners.push(fn);}}}};
  class FakeEvent {constructor(type){this.type=type;}}
  class FakeTextarea {}
  class FakeInput {}
  const currentStorage = storageDenied ? {
    getItem(){throw Error('storage denied');},
    setItem(){throw Error('storage denied');},
    removeItem(){throw Error('storage denied');}
  } : storage;
  const spawn = () => {
    const sandbox = {chrome,document,location:state.location,
      sessionStorage:currentStorage,URL,globalThis:null,
      Event:FakeEvent,InputEvent:FakeEvent,
      HTMLTextAreaElement:FakeTextarea,HTMLInputElement:FakeInput,
      setTimeout: fn=>{fn();return 1;}};
    sandbox.globalThis=sandbox;
    vm.runInNewContext(SOURCE.content,sandbox,{filename:'browser-extension/content.js'});
    return state.listeners.at(-1);
  };
  const handler = spawn();
  const call = (listener, commandId='cr01_command_1', expectedUrl=GOOD, text=TEXT) =>
    new Promise((resolve,reject)=>{
      try{
        const reply=listener({
          type:'send-chatgpt-message',text,commandId,expectedUrl
        },{},resolve);
        if(reply!==true) reject(Error('Listener did not retain async channel.'));
      }catch(e){reject(e);}
    });
  return {state,storage,spawn,handler,call,button,editor};
}

function callbackRegistry(){
  const listeners=[];
  return { listeners, addListener(f){listeners.push(f);},
    removeListener(f){const i=listeners.indexOf(f);if(i>=0)listeners.splice(i,1);} };
}

function backgroundFixture({
  persisted={}, onContent=null, rejectWrite=false, rejectRead=false,
  delayTab=false, holdFirstFinal=false, tabUrl=GOOD
}={}){
  const messages=[];
  const sockets=[];
  const tabs=[{id:73,url:tabUrl,title:'Synthetic ChatGPT',
    windowId:1,index:0,active:true,status:'complete',autoDiscardable:false}];
  const state = {sent:0, injected:0, messages, sockets, tabs,
    commandStore:persisted, failWrites:rejectWrite, handler:onContent,
    tabBarrier:null, releaseTab:null,
    firstFinalHeld:false, releaseFirstFinal:null};
  if(delayTab){
    state.tabBarrier=new Promise(resolve=>{state.releaseTab=resolve;});
  }
  const chrome={
    runtime:{
      getManifest(){return {version:'test-0.6.0'};},
      onInstalled:callbackRegistry(), onStartup:callbackRegistry()
    },
    tabs:{
      onUpdated:callbackRegistry(),onCreated:callbackRegistry(),
      onRemoved:callbackRegistry(),onActivated:callbackRegistry(),
      async query(){return tabs.map(t=>({...t}));},
      async get(id) {
        if(state.tabBarrier)await state.tabBarrier;
        const t=tabs.find(t=>t.id===id);
        if(!t)throw Error('tab missing');
        return {...t};
      },
      async update(id, update) {
        const tab=tabs.find(t=>t.id===id);
        if(!tab)throw Error('tab missing');
        Object.assign(tab,update);
        return {...tab};
      },
      async reload(){},
      async sendMessage(id,message){
        if(message.type==='send-chatgpt-message'){
          state.sent++;
          if(typeof state.handler==='function') return state.handler(message);
          return {ok:true,mayHaveExecuted:true,terminal:true};
        }
        return {ok:true,snapshot:{generating:false}};
      }
    },
    scripting:{async executeScript(){state.injected++;}},
    alarms:{create(){},onAlarm:callbackRegistry()},
    storage:{
      onChanged:callbackRegistry(),
      local:{
        async get(keys) {
          if(rejectRead && (keys===null || (keys||[]).includes('wasCommandResultsV1'))) {
            throw Error('Synthetic persisted receipt read denied');
          }
          const output={bridgeToken:'x'.repeat(32)};
          for(const k of (keys===null ? Object.keys(persisted) : keys||[])){
            if(Object.prototype.hasOwnProperty.call(persisted,k))output[k]=persisted[k];
          }
          return output;
        },
        async set(updates){
          if(state.failWrites)throw Error('synthetic storage denied');
          const copied=structuredClone(updates);
          const oldList=copied.wasCommandResultsV1;
          const firstOld=Array.isArray(oldList) && oldList.length===1
            && oldList[0]?.commandId==='cr01_interleaved_A'
            && oldList[0]?.result?.ok===true;
          const firstNew=Object.entries(copied).some(([k,v])=>
            k.endsWith('cr01_interleaved_A')
            && v?.result?.ok===true);
          if(holdFirstFinal && (firstOld || firstNew) && !state.firstFinalHeld){
            state.firstFinalHeld=true;
            await new Promise(resolve=>{state.releaseFirstFinal=resolve;});
          }
          Object.assign(persisted,copied);
        }
      }
    }
  };
  class FakeSocket {
    static OPEN=1;
    constructor(url){
      this.url=url;this.readyState=1;
      sockets.push(this);
      queueMicrotask(()=>{ if(this.onopen)this.onopen();});
    }
    send(raw){messages.push(JSON.parse(raw));}
    close(){this.readyState=3;if(this.onclose)this.onclose();}
  }
  const sandbox={chrome,WebSocket:FakeSocket,URL,console,
    crypto:crypto.webcrypto,TextEncoder,
    setInterval:()=>1,clearInterval:()=>{},setTimeout:()=>1,clearTimeout:()=>{},
    globalThis:null};
  sandbox.globalThis=sandbox;
  vm.runInNewContext(SOURCE.background,sandbox,{
    filename:'browser-extension/background.js'
  });
  const ready = async()=>{
    for(let i=0;i<150 && !sockets.length;i++)await flush(2);
    assert.equal(sockets.length,1,'Background must start one mock socket');
    await flush();
    await sockets[0].onmessage({data:JSON.stringify({
      type:'hello-ack',ok:true,protocolMin:5,protocolMax:5
    })});
    await flush();
    return sockets[0];
  };
  const dispatch = async (socket,id, commandId='cr01_bg_cmd_1', text=TEXT, expectedUrl=GOOD) => {
    await socket.onmessage({data:JSON.stringify({
      type:'send',id,tabId:73,text,commandId,expectedUrl
    })});
    await flush();
    return messages.filter(m=>m.type==='send-result' && m.id===id).at(-1);
  };
  const status = async (socket,id,commandId)=>{
    await socket.onmessage({data:JSON.stringify({type:'command-status',id,commandId})});
    return messages.filter(m=>m.type==='command-status-result' && m.id===id).at(-1);
  };
  return {state,ready,dispatch,status};
}

const cases=[];
async function testcase(name, fn){
  try {await fn();cases.push({name,verdict:'PASS'});}
  catch(e){cases.push({name,verdict:'FAIL',error:String(e.stack||e).slice(0,1200)});}
}

async function run(){
  await testcase('C01 empty draft pinned URL yields one click',async()=>{
    const f=contentFixture();
    const r=await f.call(f.handler);
    assert.equal(r.ok,true);
    assert.equal(f.state.clicks,1);
  });
  await testcase('C02 wrong conversation preserves owner draft',async()=>{
    const f=contentFixture({url:WRONG,draft:'OWNER DRAFT'});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);assert.equal(f.state.clicks,0);
    assert.equal(f.state.draft,'OWNER DRAFT');
  });
  await testcase('C03 occupied text draft never overwritten',async()=>{
    const f=contentFixture({draft:'OWNER DRAFT'});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);assert.equal(f.state.clicks,0);
    assert.equal(f.state.draft,'OWNER DRAFT');
  });
  await testcase('C03B whitespace-only owner draft survives exactly',async()=>{
    const f=contentFixture({draft:' \n\t '});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);
    assert.equal(f.state.clicks,0);
    assert.equal(f.state.draft,' \n\t ');
  });
  await testcase('C03C editor mutation after staging refuses click',async()=>{
    const f=contentFixture({mutateOnInput:true});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);
    assert.equal(f.state.clicks,0);
    assert.equal(f.state.draft,TEXT+' ','Owner edit after staging must be preserved');
  });
  await testcase('C04 attachment-only draft blocks Send',async()=>{
    const f=contentFixture({attachment:true});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);assert.equal(f.state.clicks,0);
  });
  await testcase('C05 navigation during input blocks click',async()=>{
    const f=contentFixture({navigateOnInput:true});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);assert.equal(f.state.clicks,0);
  });
  await testcase('C06 unavailable tab-scoped storage blocks click',async()=>{
    const f=contentFixture({storageDenied:true});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);assert.equal(f.state.clicks,0);
  });
  await testcase('C07 disabled Send button restores owned staged text',async()=>{
    const f=contentFixture({disabled:true});
    const r=await f.call(f.handler);
    assert.equal(r.ok,false);assert.equal(f.state.clicks,0);
    assert.equal(f.state.draft,'','Pre-click failure must not strand an unowned prompt');
  });
  await testcase('C08 repeated command is consumed even after ACK',async()=>{
    const f=contentFixture();
    assert.equal((await f.call(f.handler)).ok,true);
    const again=await f.call(f.handler);
    assert.equal(again.ok,false);
    assert.equal(again.mayHaveExecuted,true);
    assert.equal(f.state.clicks,1);
  });
  await testcase('C09 two isolated extension worlds share one attempted fence',async()=>{
    const f=contentFixture();
    const second=f.spawn();
    assert.equal((await f.call(f.handler)).ok,true);
    assert.equal((await f.call(second)).ok,false);
    assert.equal(f.state.clicks,1);
  });
  await testcase('C10 simultaneous different command IDs share active tab lock',async()=>{
    const f=contentFixture();
    const second=f.spawn();
    const [one,two]=await Promise.all([
      f.call(f.handler,'cr01_first'),
      f.call(second,'cr01_second')
    ]);
    assert.equal([one.ok,two.ok].filter(Boolean).length,1);
    assert.equal(f.state.clicks,1);
  });
  await testcase('C11 content document restart with same tab storage prevents replay',async()=>{
    const s=storageMap();
    const first=contentFixture({storage:s});
    assert.equal((await first.call(first.handler)).ok,true);
    const second=contentFixture({storage:s});
    const response=await second.call(second.handler);
    assert.equal(response.ok,false);
    assert.equal(response.mayHaveExecuted,true);
    assert.equal(second.state.clicks,0);
  });
  await testcase('C12 missing recipient pin/command ID fails closed',async()=>{
    const f=contentFixture();
    assert.equal((await f.call(f.handler,'','')).ok,false);
    assert.equal(f.state.clicks,0);
  });
  await testcase('B01 synthetic lost callback does not re-dispatch effectful command',async()=>{
    const persisted={};
    const f=backgroundFixture({
      persisted,
      onContent:async()=>{throw Error('Synthetic callback lost after possible click');}
    });
    const sock=await f.ready();
    const result=await f.dispatch(sock,'r1');
    assert.equal(result.ok,false);
    assert.equal(result.mayHaveExecuted,true);
    assert.equal(f.state.sent,1);
    assert.equal(f.state.injected,0);
    assert.ok(Object.keys(persisted).some(k=>k.startsWith('wasCommandReceiptV2:')));
  });
  await testcase('B02 durable unknown survives service-worker restart and replay',async()=>{
    const persisted={};
    const first=backgroundFixture({persisted,onContent:async()=>{throw Error('lost reply');}});
    const sock1=await first.ready();
    assert.equal((await first.dispatch(sock1,'r1')).ok,false);
    const later=backgroundFixture({persisted});
    const sock2=await later.ready();
    const repeat=await later.dispatch(sock2,'r2');
    assert.equal(repeat.ok,false);
    assert.equal(repeat.duplicate,true);
    assert.equal(later.state.sent,0);
  });
  await testcase('B03 crash after pre-effect durable marker remains uncertain',async()=>{
    const persisted={};
    const first=backgroundFixture({persisted,onContent:()=>new Promise(()=>{})});
    const sock1=await first.ready();
    void first.dispatch(sock1,'r1'); // intentionally do not await unresolved effect
    for(let i=0;i<100 && first.state.sent!==1;i++)await settle(5);
    assert.equal(first.state.sent,1);
    assert.ok(Object.entries(persisted).some(([k,v])=>
      k.startsWith('wasCommandReceiptV2:') && v.result.phase==='attempting'));
    const resumed=backgroundFixture({persisted});
    const sock2=await resumed.ready();
    const status=await resumed.status(sock2,'st1','cr01_bg_cmd_1');
    assert.equal(status.found,true);
    assert.equal(status.completed,false);
    assert.equal(status.uncertain,true);
    const repeat=await resumed.dispatch(sock2,'r2');
    assert.equal(repeat.ok,false);assert.equal(repeat.duplicate,true);
    assert.equal(resumed.state.sent,0);
  });
  await testcase('B04 failure persisting reservation blocks all physical calls',async()=>{
    const f=backgroundFixture({rejectWrite:true});
    const sock=await f.ready();
    const r=await f.dispatch(sock,'r1');
    assert.equal(r.ok,false);
    assert.equal(r.mayHaveExecuted,false);
    assert.equal(f.state.sent,0);
  });
  await testcase('B05 concurrent identical requests cannot double-send',async()=>{
    const f=backgroundFixture();
    const sock=await f.ready();
    const [a,b]=await Promise.all([
      f.dispatch(sock,'r1'),
      f.dispatch(sock,'r2')
    ]);
    assert.equal(a.ok,true);
    assert.equal(b.duplicate,true);
    assert.equal(f.state.sent,1);
  });
  await testcase('B06 target changed since runtime pin: zero content dispatches',async()=>{
    const f=backgroundFixture({tabUrl:WRONG});
    const sock=await f.ready();
    const result=await f.dispatch(sock,'r1');
    assert.equal(result.ok,false);
    assert.equal(result.mayHaveExecuted,false);
    assert.equal(f.state.sent,0);
  });
  await testcase('B07 real content handler and background compose one click',async()=>{
    const f=contentFixture();
    const b=backgroundFixture({
      onContent:msg=>f.call(f.handler,msg.commandId,msg.expectedUrl,msg.text)
    });
    const sock=await b.ready();
    const r=await b.dispatch(sock,'r1');
    assert.equal(r.ok,true);
    assert.equal(b.state.sent,1);assert.equal(f.state.clicks,1);
  });
  await testcase('B08 callback lost after modelled click + background restart no replay',async()=>{
    const persisted={};const content=contentFixture();
    const old=backgroundFixture({persisted,
      onContent:async msg=>{
        await content.call(content.handler,msg.commandId,msg.expectedUrl,msg.text);
        throw Error('Synthetic ACK lost AFTER modelled click');
      }
    });
    const sock1=await old.ready();
    const r=await old.dispatch(sock1,'r1');
    assert.equal(r.ok,false);assert.equal(r.mayHaveExecuted,true);
    const now=backgroundFixture({persisted,
      onContent:msg=>content.call(content.handler,msg.commandId,msg.expectedUrl,msg.text)
    });
    const sock2=await now.ready();
    const repeat=await now.dispatch(sock2,'r2');
    assert.equal(repeat.ok,false);assert.equal(repeat.duplicate,true);
    assert.equal(content.state.clicks,1);
    assert.equal(now.state.sent,0);
  });
  await testcase('B09 duplicate client bridge refuses ambiguous two extensions',async()=>{
    const start=SOURCE.bridge.indexOf('function readyClients()');
    const end=SOURCE.bridge.indexOf('function sendToCurrentProfile(',start);
    assert.ok(start>=0 && end>start,'Real bridge client-exclusion functions required');
    const block=SOURCE.bridge.slice(start,end);
    const clients=new Set([
      {readyState:1,wasReady:true},
      {readyState:1,wasReady:true}
    ]);
    const fn=new Function('clients','WebSocket',block+';return connectedClient;')(
      clients,{OPEN:1}
    );
    assert.equal(fn(),null);
    clients.delete([...clients][0]);
    assert.ok(fn());
  });
  await testcase('B10 missing expected URL is rejected at background boundary',async()=>{
    const f=backgroundFixture();
    const sock=await f.ready();
    const result=await f.dispatch(sock,'r1','cr01_bg_cmd_1',TEXT,'');
    assert.equal(result.ok,false);
    assert.equal(result.mayHaveExecuted,false);
    assert.equal(f.state.sent,0);
  });
  await testcase('C13 explicitly pinned homepage can bootstrap with empty draft',async()=>{
    const f=contentFixture({url:'https://chatgpt.com/'});
    const result=await f.call(f.handler,'cr01_home_boot','https://chatgpt.com/');
    assert.equal(result.ok,true);
    assert.equal(f.state.clicks,1);
  });
  await testcase('B11 storage history read denial blocks Send even after reconnect',async()=>{
    const f=backgroundFixture({rejectRead:true});
    const sock=await f.ready();
    const r=await f.dispatch(sock,'r1');
    assert.equal(r.ok,false);
    assert.equal(r.mayHaveExecuted,true);
    assert.equal(f.state.sent,0);
    const status=await f.status(sock,'st1','cr01_bg_cmd_1');
    assert.equal(status.uncertain,true);
  });
  await testcase('B12 corrupt durable command history blocks all Send attempts',async()=>{
    const f=backgroundFixture({persisted:{wasCommandResultsV1:[{
      commandId:'abc',result:null,recordedAt:0
    }]}});
    const sock=await f.ready();
    const r=await f.dispatch(sock,'r1');
    assert.equal(r.ok,false);assert.equal(f.state.sent,0);
  });
  await testcase('B13 two distinct concurrent commands on one document cannot both click',async()=>{
    const content=contentFixture();
    const f=backgroundFixture({onContent:msg=>
      content.call(content.handler,msg.commandId,msg.expectedUrl,msg.text)
    });
    const sock=await f.ready();
    const [a,b]=await Promise.all([
      f.dispatch(sock,'r1','cr01_mixed_1'),
      f.dispatch(sock,'r2','cr01_mixed_2')
    ]);
    assert.equal(content.state.clicks,1);
    assert.equal([a.ok,b.ok].filter(Boolean).length,1);
  });
  await testcase('B14 out-of-order independent Send writes cannot erase a durable receipt',async()=>{
    const persisted={};
    const f=backgroundFixture({persisted,holdFirstFinal:true});
    const sock=await f.ready();
    const first=f.dispatch(sock,'a','cr01_interleaved_A');
    for(let i=0;i<150&&!f.state.firstFinalHeld;i++)await settle(5);
    assert.equal(f.state.firstFinalHeld,true,'Must hold A terminal write');
    const second=await f.dispatch(sock,'b','cr01_interleaved_B');
    assert.equal(second.ok,true);
    assert.equal(f.state.sent,2,'Distinct command effects were modelled');
    f.state.releaseFirstFinal();
    await first;
    const reboot=backgroundFixture({persisted});
    const nextSocket=await reboot.ready();
    const repeat=await reboot.dispatch(nextSocket,'b-retry','cr01_interleaved_B');
    assert.equal(repeat.duplicate,true,
      'Late storage write lost B receipt: restarted worker would replay B');
    assert.equal(reboot.state.sent,0);
  });
  await testcase('B15 same command ID altered body/target rejects cached success',async()=>{
    const persisted={};
    const first=backgroundFixture({persisted});
    const s1=await first.ready();
    const command='cr01_same_id_changed_envelope_1';
    const acknowledged=await first.dispatch(s1,'first',command,'ALPHA',GOOD);
    assert.equal(acknowledged.ok,true);
    assert.equal(first.state.sent,1);
    const changedBody=await first.dispatch(s1,'changed',command,'BETA',GOOD);
    assert.equal(changedBody.ok,false);
    assert.equal(changedBody.status,'INTEGRITY_VIOLATION');
    assert.equal(first.state.sent,1);
    const afterRestart=backgroundFixture({persisted});
    const s2=await afterRestart.ready();
    const changedTarget=await afterRestart.dispatch(s2,'changed-url',command,'ALPHA',WRONG);
    assert.equal(changedTarget.ok,false);
    assert.equal(changedTarget.status,'INTEGRITY_VIOLATION');
    assert.equal(afterRestart.state.sent,0);
    const identical=await afterRestart.dispatch(s2,'identical',command,'ALPHA',GOOD);
    assert.equal(identical.duplicate,true);
    assert.equal(identical.ok,true);
    assert.equal(afterRestart.state.sent,0);
  });
  const pass=cases.filter(c=>c.verdict==='PASS').length;
  console.log(JSON.stringify({
    id:'CR01-FULL-SCRIPT-OFFLINE',
    environment:process.platform,
    evidence:'actual checkout JS in synthetic VM; NO LIVE Chrome or model',
    source_git_blobs:Object.fromEntries(
      Object.entries(SOURCE).map(([key,value])=>[key,hash(value)])),
    total:cases.length,passed:pass,failed:cases.length-pass,cases
  },null,2));
  if(pass!==cases.length)process.exitCode=1;
}
run().catch(e=>{console.error('CR01_HARNESS_ERROR',e);process.exitCode=1;});
