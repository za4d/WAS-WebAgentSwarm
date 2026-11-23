'use strict';
/**
 * CR01 independent CodeQL js/file-system-race negative/positive oracle.
 * Extracts the actual loadBridgeToken() function from bridge/server.js.
 * Uses controlled filesystem semantics; NEVER uses an account token.
 * Test 1 forces a competing writer to create the file between an existence
 * check and a write; only atomic exclusive creation may pass.
 */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', '..', 'bridge', 'server.js'), 'utf8');
const start = source.indexOf('function loadBridgeToken() {');
const end = source.indexOf('\nconst BRIDGE_TOKEN = loadBridgeToken();', start);
assert.ok(start >= 0 && end > start, 'Actual bridge token loader source must be found');
const fn = new Function('fs', 'path', 'crypto', 'TOKEN_FILE', 'console',
  source.slice(start, end) + '\nreturn loadBridgeToken;');
const load = (fileOps, filename, logger = {error() {}}) =>
  fn(fileOps, path, crypto, filename, logger)();

const cases = [];
async function testcase(name, job) {
  try {await job();cases.push({name, result:'PASS'});}
  catch(e) {cases.push({name, result:'FAIL', error:String(e.stack || e).slice(0,1200)});}
}

(async () => {
  await testcase('T01 exclusive create cannot overwrite a competing token', () => {
    const valid = 'c'.repeat(64);
    let writes = 0, reads = 0;
    const fileOps = {
      mkdirSync() {},
      // Deliberately wrong stale existence answer, to reproduce a TOCTOU.
      existsSync() {return false;},
      readFileSync() {reads++;return valid + '\n';},
      writeFileSync(filename, content, opts) {
        // A second process completed creation before this call.
        if (opts?.flag === 'wx') {
          const e = new Error('File already exists'); e.code = 'EEXIST'; throw e;
        }
        writes++;
        // Without wx, original code clobbers the already-created token.
      },
      chmodSync() {}
    };
    const actual = load(fileOps, '/test-only/token');
    assert.equal(actual,valid,'Read existing token without replacing it');
    assert.equal(reads,1,'Recovered the prior existing token');
    assert.equal(writes,0,'Never overwrite an existing token after stale check');
  });

  await testcase('T02 real filesystem new token and restart preserve identity', () => {
    const dir=fs.mkdtempSync(path.join(os.tmpdir(),'cr01-token-'));
    const file=path.join(dir,'bridge-token');
    try {
      const first=load(fs,file);
      const second=load(fs,file);
      assert.equal(first.length,64);
      assert.match(first,/^[a-f0-9]{64}$/);
      assert.equal(second,first,'Host restart must not rotate bridge token');
      assert.equal(fs.readFileSync(file,'utf8').trim(),first);
    } finally {fs.rmSync(dir,{recursive:true,force:true});}
  });

  await testcase('T03 pre-existing malformed token fails closed without replacement', () => {
    const dir=fs.mkdtempSync(path.join(os.tmpdir(),'cr01-token-malformed-'));
    const file=path.join(dir,'bridge-token');
    try {
      fs.writeFileSync(file,'short\n',{mode:0o600});
      assert.throws(()=>load(fs,file),/short/i);
      assert.equal(fs.readFileSync(file,'utf8'),'short\n');
    } finally {fs.rmSync(dir,{recursive:true,force:true});}
  });

  const passed=cases.filter(x=>x.result==='PASS').length;
  console.log(JSON.stringify({id:'CR01-TOKEN-ATOMIC-ORACLE',
    source_revision:process.env.GITHUB_SHA || 'local',
    operating_system:process.platform, total:cases.length,
    passed, failed:cases.length-passed, cases},null,2));
  if(passed!==cases.length)process.exitCode=1;
})().catch(e=>{console.error('TOKEN_ORACLE_FAILURE',e);process.exitCode=1;});
