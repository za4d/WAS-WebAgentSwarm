import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

// Cryptographic *evidence* of Relay R8 final_send, not a second journal.
// Exclusive consumption is a deny-only no-replay fence; never grants a permit.
export const RELAY_SEND_MODE = process.env.WEB_SWARM_SEND_MODE || 'deny';
const KEY = String(process.env.WEB_SWARM_RELAY_PERMIT_KEY || '');
const RECEIPTS = String(process.env.WEB_SWARM_RELAY_RECEIPTS_DIR || '');

const ID = /^[a-f0-9]{32}$/;
const CMD = /^r8_[a-f0-9]{32}$/;
const HEX64 = /^[a-f0-9]{64}$/;
const RECIPIENT = /^[A-Z][A-Z0-9_]{0,20}$/;
const EXACT_KEYS = ['attemptId','bodySha256','commandId','expectedUrl','generation','issuedAt','recipient','signature','tabId','v'];

export function permitMessage(p) {
  return [
    'R8WAS1',p.attemptId,p.recipient,p.generation,p.commandId,p.tabId,
    p.expectedUrl,p.bodySha256,p.issuedAt
  ].join('\n');
}

export function verifyRelayPermit(data) {
  if (RELAY_SEND_MODE === 'legacy_explicit') {
    // Explicitly unsafe standalone WAS mode. Never use for Relay deployment.
    return {ok:true, mode:'legacy_explicit'};
  }
  if (RELAY_SEND_MODE !== 'relay_required') {
    return {ok:false, error:'Effectful Send disabled without Relay permit policy.'};
  }
  if (KEY.length < 32 || !RECEIPTS || !path.isAbsolute(RECEIPTS)) {
    return {ok:false, error:'Relay permit verifier/receipt store unavailable.'};
  }
  const p = data?.r8Permit;
  if (!p || typeof p !== 'object' || Array.isArray(p) ||
      Object.keys(p).sort().join(',') !== EXACT_KEYS.join(',')) {
    return {ok:false, error:'R8 permit missing or malformed.'};
  }
  if (p.v!==1 || typeof p.attemptId!=='string' || !ID.test(p.attemptId) ||
      p.commandId!=='r8_'+p.attemptId || !CMD.test(p.commandId) ||
      typeof p.recipient!=='string' || !RECIPIENT.test(p.recipient) ||
      !Number.isSafeInteger(p.generation) || p.generation<1 ||
      !Number.isSafeInteger(p.tabId) || p.tabId<=0 ||
      !Number.isSafeInteger(p.issuedAt) ||
      typeof p.bodySha256!=='string' || !HEX64.test(p.bodySha256) ||
      typeof p.expectedUrl!=='string' ||
      typeof p.signature!=='string' || !HEX64.test(p.signature) ||
      p.commandId!==data.commandId || p.tabId!==data.tabId ||
      p.expectedUrl!==data.expectedUrl) {
    return {ok:false, error:'R8 permit identity/target mismatch.'};
  }
  const now = Math.floor(Date.now()/1000);
  if (p.issuedAt>now+5 || now-p.issuedAt>60) {
    return {ok:false, error:'R8 permit expired or future-dated.'};
  }
  const exactBodyHash=crypto.createHash('sha256').update(data.text,'utf8').digest('hex');
  if(p.bodySha256!==exactBodyHash) {
    return {ok:false,error:'R8 permit body digest mismatch.'};
  }
  const signed=crypto.createHmac('sha256',KEY).update(permitMessage(p),'utf8').digest('hex');
  const a=Buffer.from(signed,'hex'),b=Buffer.from(p.signature,'hex');
  if(a.length!==b.length || !crypto.timingSafeEqual(a,b)) {
    return {ok:false,error:'R8 permit signature invalid.'};
  }
  return {ok:true, mode:'relay_required', permit:p};
}

// RB-02/SR3: The receipt directory must already exist under the trusted
// account. Never create this directory during a permitted physical Send:
// making it on demand leaves a parent-directory crash-persistence gap.
// On Windows a separate operator/host test MUST verify the NTFS ACL and
// service-account access. JS lstat alone does not establish Windows ACLs.
export function checkRelayReceiptStore() {
  if (RELAY_SEND_MODE !== 'relay_required') return {ok:true,mode:RELAY_SEND_MODE};
  if (!RECEIPTS || !path.isAbsolute(RECEIPTS)) {
    return {ok:false,error:'R8 receipt store must be an absolute pre-provisioned path.'};
  }
  try {
    const st=fs.lstatSync(RECEIPTS);
    if (!st.isDirectory() || st.isSymbolicLink()) {
      return {ok:false,error:'R8 receipt store is not a real directory.'};
    }
    if (process.platform !== 'win32') {
      // On POSIX the bridge must own a mode-0700 trusted receipt directory.
      if ((st.mode & 0o077) !== 0 ||
          (typeof process.getuid === 'function' && st.uid !== process.getuid())) {
        return {ok:false,error:'R8 receipt directory owner or mode is unsafe.'};
      }
    }
    return {ok:true,mode:'relay_required'};
  } catch {
    return {ok:false,error:'R8 receipt directory unavailable or unreadable.'};
  }
}

export function consumeRelayPermit(verified) {
  if (!verified?.ok) return {ok:false,error:'No authenticated R8 permit.'};
  if(verified.mode==='legacy_explicit')return {ok:true,mode:'legacy_explicit'};
  const p=verified.permit;
  const ready=checkRelayReceiptStore();
  if (!ready.ok) return ready;
  try {
    const file=path.join(RECEIPTS,p.attemptId+'.json');
    // Last effectful admission point: durable and exclusive before WebSocket
    // dispatch. A crash after creation sacrifices liveness, never retries.
    const record=Buffer.from(JSON.stringify({
      v:1,attemptId:p.attemptId,signature:p.signature,consumedAt:Date.now()
    })+'\n','utf8');
    // Exclusive creation protects against parallel processes. It does NOT
    // imply stable storage: explicitly flush before browser dispatch.
    // Any write/flush/close error leaves an unusable, deny-only attempt ID.
    const fd=fs.openSync(file,'wx',0o600);
    try {
      const written=fs.writeSync(fd,record,0,record.length,0);
      if(written!==record.length)throw new Error('Incomplete R8 permit receipt write');
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    // POSIX filesystems generally need a directory fsync to persist a new
    // filename. Node cannot portably flush directory handles on Windows.
    // Windows therefore has a file flush, not proven power-loss durability:
    // the Windows release gate must remain HOLD until OS-specific validation.
    if(process.platform!=='win32') {
      const dirFd=fs.openSync(RECEIPTS,fs.constants.O_RDONLY);
      try {fs.fsyncSync(dirFd);}finally{fs.closeSync(dirFd);}
    }
    return {ok:true,mode:'relay_required'};
  } catch(e) {
    return {ok:false,error:e?.code==='EEXIST'
      ? 'R8 permit already consumed; effect outcome uncertain.'
      : 'R8 permit receipt not durably committed.'};
  }
}
