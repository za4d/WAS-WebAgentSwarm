'use strict';
/**
 * CR-01 experimental joined-stack synthetic browser worker.
 * Loads ACTUAL content.js and background.js, simulates Chrome and DOM only,
 * and connects to the ACTUAL localhost WAS bridge/server.js.
 *
 * No real browser, signed-in model, messages or external network.
 * Tests source selected by a private Relay R8 test runner.
 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const root = path.resolve(__dirname, '..', '..');
const WS = createRequire(path.join(root, 'bridge', 'package.json'))('ws');
const token = process.env.WAS_SYNTHETIC_TOKEN || '';
const port = Number(process.env.WAS_SYNTHETIC_PORT || 0);
const mode = process.env.WAS_SYNTHETIC_MODE || 'happy';
const stateFile = process.env.WAS_SYNTHETIC_STATE || '';
const storageFile = process.env.WAS_SYNTHETIC_STORAGE || '';
if (!token || !port || !stateFile || !storageFile) throw Error('Synthetic bridge test environment incomplete.');
const goodUrl = 'https://chatgpt.com/c/rrrr3333';
const otherUrl = 'https://chatgpt.com/c/zzzz9999';
const tab = {id: 73, url: mode === 'wrong_tab' ? otherUrl : goodUrl,
  active:true, index:0, windowId:1,title:'Synthetic',status:'complete',
  autoDiscardable:false};
const state={clicks:0, effectfulDispatches:0, lastInput:'', mode,
  activeUrl: mode === 'wrong_content' ? otherUrl : goodUrl};
let stored={bridgeToken:token,bridgeWsUrl:'ws://127.0.0.1:'+port+'/ws'};
try{if(fs.existsSync(storageFile))stored={
  ...stored,...JSON.parse(fs.readFileSync(storageFile,'utf8'))};}catch{
  throw Error('Synthetic worker persistent store corrupt.');
}
function writeAtomic(p, obj){
  fs.writeFileSync(p+'.tmp',JSON.stringify(obj));
  fs.renameSync(p+'.tmp',p);
}
function save(){
  writeAtomic(stateFile,state);
}
save();
const storage = {
  getItem(key){return state['session:'+key]??null;},
  setItem(key,val){state['session:'+key]=String(val);save();},
  removeItem(key){delete state['session:'+key];save();}
};
const editor = {
  isConnected:true,focus(){},
  get innerText(){return state.lastInput;},
  get textContent(){return state.lastInput;},
  get value(){return state.lastInput;},
  querySelector(){return null;},
  replaceChildren(){state.lastInput='';save();},
  appendChild(p){state.lastInput=String(p.textContent||'');save();},
  dispatchEvent(e){
    if(e.type==='input' && mode==='navigate')state.activeUrl=otherUrl;
    save();
  }
};
if(mode==='occupied')state.lastInput='OWNER DRAFT';
const button={disabled:false,click(){state.clicks++;save();}};
const document={
  title:'Synthetic ChatGPT',
  body:{innerText:''},
  querySelector(q){
    if(q==='#prompt-textarea')return editor;
    if(q==='[data-testid="send-button"]')return button;
    if(mode==='attachment'&&q.includes('composer-attachments'))return {};
    return null;
  },
  querySelectorAll(){return [];},
  createElement(){return {textContent:''};}
};
const location = {get href(){return state.activeUrl;},
  set href(val){state.activeUrl=val;save();}};
const listeners=[];
const register=()=>({addListener(fn){listeners.push(fn);},
  removeListener(fn){const i=listeners.indexOf(fn);if(i>=0)listeners.splice(i,1);}});
const contentListenerList=[];
const contentChrome={runtime:{onMessage:{addListener(fn){contentListenerList.push(fn);}}}};
class TestEvent{constructor(type){this.type=type;}}
class TestArea{};class TestInput{};
const contentContext={
  document,location,chrome:contentChrome,
  globalThis:null,sessionStorage:storage,URL,
  HTMLTextAreaElement:TestArea,HTMLInputElement:TestInput,
  Event:TestEvent,InputEvent:TestEvent,
  setTimeout,clearTimeout
};
contentContext.globalThis=contentContext;
vm.runInNewContext(fs.readFileSync(path.join(root,'browser-extension/content.js'),'utf8'),
  contentContext,{filename:'content.js'});
if(contentListenerList.length!==1)throw Error('No actual content callback registered.');
const contentMessage=msg=>new Promise((resolve,reject)=>{
  let completed=false;
  try{
    contentListenerList[0](msg,{},result=>{completed=true;resolve(result);});
  }catch(e){reject(e);}
  setTimeout(()=>{if(!completed)reject(Error('Synthetic content callback timed out'));},5000);
});
const chrome={
  runtime:{getManifest(){return{version:'fixture-0.6.0'};},
    onInstalled:register(),onStartup:register()},
  tabs:{
    onUpdated:register(),onCreated:register(),onRemoved:register(),onActivated:register(),
    async query(){return[{...tab}];},
    async get(id){if(id!==73)throw Error('Unknown synthetic tab');return {...tab};},
    async update(id,change){
      if(id!==73)throw Error('Unknown tab update');
      Object.assign(tab,change);return{...tab};
    },
    async reload(){},
    async sendMessage(id,msg){
      if(id!==73)throw Error('Wrong synthetic tab');
      if(msg.type==='inspect-chatgpt')return contentMessage(msg);
      if(msg.type==='send-chatgpt-message'){
        state.effectfulDispatches++;save();
        const result=await contentMessage(msg);
        if(mode==='lost_ack'&&state.clicks>0){
          throw Error('Synthetic callback lost after one modelled click');
        }
        return result;
      }
      throw Error('Unexpected Chrome content command');
    }
  },
  scripting:{async executeScript(){throw Error('No synthetic reinjection allowed on Send');}},
  alarms:{create(){},onAlarm:register()},
  storage:{
    onChanged:register(),
    local:{
      async get(keys){
        if(keys===null)return {...stored};
        const answer={};
        for(const k of keys||[])if(Object.hasOwn(stored,k))answer[k]=stored[k];
        return answer;
      },
      async set(updates){
        Object.assign(stored,JSON.parse(JSON.stringify(updates)));
        writeAtomic(storageFile,stored);
      }
    }
  }
};
const workerContext={
  chrome,URL,WebSocket:WS,crypto:crypto.webcrypto,TextEncoder,globalThis:null,
  setInterval,clearInterval,setTimeout,clearTimeout
};
workerContext.globalThis=workerContext;
vm.runInNewContext(fs.readFileSync(path.join(root,'browser-extension/background.js'),'utf8'),
  workerContext,{filename:'background.js'});
process.on('SIGTERM',()=>process.exit(0));
process.on('SIGINT',()=>process.exit(0));
