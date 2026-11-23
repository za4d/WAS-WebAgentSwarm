'use strict';
/*
 * Safe OFFLINE product regression test for WAS browser Send.
 * No real Chrome instance, network call, local bridge or ChatGPT account is used.
 *
 * Run: node tests/relay-evaluation/send-safety.cjs
 * Exit: 0 = all safety acceptance criteria pass; 1 = product defect/harness failure.
 *
 * IMPORTANT: The retry injection models loss of the callback after a possibly
 * effectful content-script request. It counts effectful dispatch attempts, not
 * observed real ChatGPT clicks.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');

const ROOT = path.resolve(__dirname, '..', '..');
const INPUTS = [
  'browser-extension/content.js',
  'browser-extension/background.js'
];
function gitBlobSha(buf) {
  return crypto.createHash('sha1')
    .update('blob ' + buf.length + '\0')
    .update(buf)
    .digest('hex');
}
function source(rel) {
  const data = fs.readFileSync(path.join(ROOT, rel));
  return { js: data.toString('utf8'), git_blob_sha1: gitBlobSha(data) };
}

function extractAsyncFunction(js, name) {
  const start = js.indexOf('async function ' + name + '(');
  assert.ok(start >= 0, 'Expected source function not found: ' + name);
  const open = js.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < js.length; i++) {
    if (js[i] === '{') depth++;
    if (js[i] === '}' && --depth === 0) return js.slice(start, i + 1);
  }
  throw Error('Function extraction failed: ' + name);
}

async function testContent(js, { url, expectedUrl, initialDraft = '', navigateOnInput = false }) {
  const s = { draft: initialDraft, clicks: 0, changes: 0, handler: null };
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); }
  };
  const location = { href: url };
  const sendButton = { disabled: false, click() { s.clicks++; } };
  const editor = {
    focus() {},
    get textContent() { return s.draft; },
    get innerText() { return s.draft; },
    get value() { return s.draft; },
    replaceChildren() { s.draft = ''; },
    appendChild(elem) { s.draft = String(elem.textContent || ''); },
    dispatchEvent(event) {
      if (event.type === 'input') {
        s.changes++;
        if (navigateOnInput) location.href = 'https://chatgpt.com/c/changed';
      }
    }
  };
  const document = {
    querySelector(selector) {
      if (selector === '#prompt-textarea') return editor;
      if (selector === '[data-testid="send-button"]') return sendButton;
      return null;
    },
    createElement() { return { textContent: '' }; }
  };
  const chrome = {
    runtime: { onMessage: { addListener(fn) { s.handler = fn; } } }
  };
  class TestEvent {
    constructor(type) { this.type = type; }
  }
  class TestTextArea {}
  class TestInput {}
  // The pinned content script is instantiated against SYNTHETIC globals.
  // No actual browser or external API calls can occur here.
  new Function(
    'chrome', 'document', 'location', 'globalThis',
    'HTMLTextAreaElement', 'HTMLInputElement', 'InputEvent',
    'Event', 'setTimeout', 'sessionStorage', js
  )(chrome, document, location, {}, TestTextArea, TestInput,
    TestEvent, TestEvent, callback => callback(), storage);
  assert.equal(typeof s.handler, 'function', 'Missing content-script listener');
  const reply = await new Promise((resolve, reject) => {
    try {
      s.handler({
        type: 'send-chatgpt-message',
        text: 'WAS-OFFLINE-TEST-MESSAGE',
        commandId: 'WAS_CR01_SYNTHETIC_01',
        expectedUrl
      }, {}, resolve);
    } catch (err) { reject(err); }
  });
  return {
    reply_ok: Boolean(reply && reply.ok),
    terminal: Boolean(reply && reply.terminal),
    may_have_executed: reply && reply.mayHaveExecuted === true,
    clicks: s.clicks,
    original_draft_preserved: s.draft === initialDraft,
    replaced_with_test_text: s.draft === 'WAS-OFFLINE-TEST-MESSAGE',
    input_events: s.changes
  };
}

async function testLostCallback(js) {
  const observed = { effectful_dispatch_attempts: 0, content_reinjections: 0 };
  const fakeChrome = {
    tabs: {
      async sendMessage() {
        observed.effectful_dispatch_attempts++;
        if (observed.effectful_dispatch_attempts === 1)
          throw Error('Synthetic lost callback AFTER potentially effectful Send');
        return { ok: true };
      }
    },
    scripting: {
      async executeScript() { observed.content_reinjections++; }
    }
  };
  const pass = async value => value;
  const callContent = new Function(
    'chrome', 'protectChatTab', 'reviveDiscardedChatTab',
    'waitForTabComplete',
    extractAsyncFunction(js, 'callContent') + '; return callContent;'
  )(fakeChrome, pass, pass, pass);
  let reply = null;
  let error_type = null;
  try {
    reply = await callContent({
      id: 77, url: 'https://chatgpt.com/c/good', status: 'complete'
    }, { type: 'send-chatgpt-message', text: 'WAS-OFFLINE-TEST-MESSAGE' });
  } catch (err) {
    // A fail-closed UNKNOWN/HELD outcome may reject after one attempted effect;
    // that is safer than replay, and must not crash this safety oracle.
    error_type = err && err.constructor ? err.constructor.name : 'Error';
  }
  return { ...observed, reply_ok: Boolean(reply && reply.ok), error_type };
}

async function run() {
  const inputs = Object.fromEntries(INPUTS.map(rel => [rel, source(rel)]));
  const c = inputs['browser-extension/content.js'].js;
  const b = inputs['browser-extension/background.js'].js;
  const expected = 'https://chatgpt.com/c/good';
  const good = await testContent(c, { url: expected, expectedUrl: expected });
  const wrong = await testContent(c, {
    url: 'https://chatgpt.com/c/wrong',
    expectedUrl: expected,
    initialDraft: 'USER DRAFT'
  });
  const draft = await testContent(c, {
    url: expected, expectedUrl: expected, initialDraft: 'USER DRAFT'
  });
  const navigation = await testContent(c, {
    url: expected, expectedUrl: expected, navigateOnInput: true
  });
  const callback = await testLostCallback(b);
  const cases = [
    { id: 'SEND-01', name: 'valid empty composer', pass: good.reply_ok && good.clicks === 1, observed: good },
    { id: 'SEND-02', name: 'wrong target URL rejects without effects', pass: wrong.clicks === 0 && wrong.original_draft_preserved, observed: wrong },
    { id: 'SEND-03', name: 'existing owner draft preserved', pass: draft.clicks === 0 && draft.original_draft_preserved, observed: draft },
    { id: 'SEND-04', name: 'navigation mid-compose blocks click', pass: navigation.clicks === 0, observed: navigation },
    { id: 'SEND-05', name: 'lost callback does not repeat effectful dispatch', pass: callback.effectful_dispatch_attempts <= 1, observed: callback }
  ];
  const passed = cases.filter(t => t.pass).length;
  const report = {
    name: 'WAS Send safety, simulated original functions',
    timestamp_utc: new Date().toISOString(),
    evidence_type: 'SIMULATED SOURCE-LEVEL; NOT LIVE BROWSER',
    sources: Object.fromEntries(Object.entries(inputs).map(([rel, obj]) => [rel, obj.git_blob_sha1])),
    passed, failed: cases.length - passed,
    cases: cases.map(t => ({ id: t.id, name: t.name, verdict: t.pass ? 'PASS' : 'FAIL', observed: t.observed }))
  };
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  if (passed !== cases.length) process.exitCode = 1;
}

run().catch(err => {
  process.stderr.write('HARNESS_ERROR: ' + String(err.message || err) + '\n');
  process.exitCode = 1;
});
