'use strict';
/*
 * Reviewer S RB-02: executable negative test against the actual relay-permit
 * module, not a reimplementation. Real local temp filesystem, no browser.
 * A pre-effect receipt must be fsync'd before consumeRelayPermit says "ok".
 * A simulated flush error MUST refuse dispatch, and never make retry safe.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {pathToFileURL} = require('node:url');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'was-cr01-rb02-'));
const signer='rb02-offline-private-synthetic-' .repeat(3);
process.env.WEB_SWARM_SEND_MODE='relay_required';
process.env.WEB_SWARM_RELAY_PERMIT_KEY=signer;
process.env.WEB_SWARM_RELAY_RECEIPTS_DIR=path.join(tmp,'receipts');
const out=[];
function test(label,fn) {
  try{fn();out.push({label,verdict:'PASS'});}
  catch(err){out.push({label,verdict:'FAIL',error:String(err?.stack||err).slice(0,1000)});}
}
function request(id){
 const text='rb02 FIXTURE ONLY';
 const p={v:1,attemptId:id,recipient:'R',generation:1,commandId:'r8_'+id,
   tabId:73,expectedUrl:'https://chatgpt.com/c/rrrr3333',
   bodySha256:crypto.createHash('sha256').update(text).digest('hex'),
   issuedAt:Math.floor(Date.now()/1000)};
 const canonical=['R8WAS1',p.attemptId,p.recipient,p.generation,p.commandId,
   p.tabId,p.expectedUrl,p.bodySha256,p.issuedAt].join('\n');
 p.signature=crypto.createHmac('sha256',signer).update(canonical).digest('hex');
 return {tabId:73,text,commandId:p.commandId,expectedUrl:p.expectedUrl,r8Permit:p};
}
(async()=>{
 const moduleUrl=pathToFileURL(path.resolve(__dirname,'../../bridge/relay-permit.js')).href;
 const {verifyRelayPermit,consumeRelayPermit}=await import(moduleUrl);
 const originalFsync=fs.fsyncSync;
 try{
  test('RB02-1 actual receipt file explicitly flushed before successful admission',()=>{
   const v=verifyRelayPermit(request('a'.repeat(32)));
   assert.equal(v.ok,true);
   let flushes=0;
   fs.fsyncSync=function(fd){flushes++;return originalFsync.call(fs,fd);};
   let result;
   try{result=consumeRelayPermit(v);}finally{fs.fsyncSync=originalFsync;}
   assert.equal(result.ok,true,JSON.stringify(result));
   assert.ok(flushes>=1,'No actual fs.fsyncSync on pre-effect receipt');
   assert.equal(consumeRelayPermit(v).ok,false,'Duplicate must be denied');
  });
  test('RB02-2 injected filesystem flush error fails closed before Send',()=>{
   const v=verifyRelayPermit(request('b'.repeat(32)));
   assert.equal(v.ok,true);
   let flushes=0;
   fs.fsyncSync=function(fd){
     flushes++;
     const error=new Error('synthetic EIO while flushing receipt');
     error.code='EIO';
     throw error;
   };
   let result;
   try{result=consumeRelayPermit(v);}finally{fs.fsyncSync=originalFsync;}
   assert.ok(flushes>=1,'No explicit flush attempted');
   assert.equal(result.ok,false,'Flush failure must not authorise effect');
   assert.equal(consumeRelayPermit(v).ok,false,'Incomplete receipt must still deny replay');
  });
 }finally{
  fs.fsyncSync=originalFsync;
  fs.rmSync(tmp,{force:true,recursive:true});
 }
 const passed=out.filter(x=>x.verdict==='PASS').length;
 console.log(JSON.stringify({case:'CR01-RB02-PREFECT-FSYNC',source:process.env.GITHUB_SHA||'',
  platform:process.platform,executed:out.length,passed,failed:out.length-passed,
  limits:['No actual power cut','No NTFS power-loss durability proof','No browser effect'],cases:out},null,2));
 if(passed!==out.length)process.exitCode=1;
})().catch(err=>{fs.fsyncSync&&0;console.error(err);process.exitCode=1;});
