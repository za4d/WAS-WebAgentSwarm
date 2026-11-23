'use strict';
/*
 * WAS-DEEP-20261123-SOURCE-01
 * Execute pinned, unmodified WAS fork functions in a SYNTHETIC Chrome/DOM.
 * This script performs NO live browser operations and NO ChatGPT requests.
 * Usage:
 *   node source-function-regression.cjs
 *   node source-function-regression.cjs C:\path\to\MrWrecks-WAS-checkout
 * Without a local checkout, HTTPS fetches raw pinned public GitHub source.
 * The exact Git blob SHA is checked before any function is executed.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const crypto = require('node:crypto');

const REV = '7f96d4288cbfbed4188ae0f13d8334dd81b7833a';
const EXPECTED = {
  'browser-extension/content.js': 'fb147adc00da0694b1fc71da6e59e3c52b7ca040',
  'browser-extension/background.js': 'e209cba8c033cbef2bb17ef0a0af0e622ae35ee5'
};
const BASE = 'https://raw.githubusercontent.com/MrWrecks/WAS-WebAgentSwarm/' + REV + '/';

function download(url) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, { timeout: 12000 }, response => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error('Pinned source fetch status: ' + response.statusCode));
        return;
      }
      const chunks = [];
      let total = 0;
      response.on('data', chunk => {
        total += chunk.length;
        if (total > 256 * 1024) response.destroy(new Error('Source unexpectedly large'));
        else chunks.push(chunk);
      });
      response.on('end', () => resolve(Buffer.concat(chunks)));
      response.on('error', reject);
    });
    request.on('timeout', () => request.destroy(new Error('Source fetch timeout')));
    request.on('error', reject);
  });
}

async function originalSource(rel) {
  const supplied = process.argv[2];
  const bytes = supplied
    ? fs.readFileSync(path.join(supplied, rel))
    : await download(BASE + rel);
  const sha = crypto.createHash('sha1')
    .update('blob ' + bytes.length + '\0')
    .update(bytes)
    .digest('hex');
  assert.equal(sha, EXPECTED[rel], 'Source blob does not match pinned SHA: ' + rel);
  return bytes.toString('utf8');
}

// Isolate a named function from the exact pinned source. Used only for the
// recorded deterministic functions; not a generic JavaScript parser.
function functionSlice(src, name) {
  const start = src.indexOf('async function ' + name + '(');
  assert.ok(start >= 0, 'Missing expected async function: ' + name);
  const brace = src.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < src.length; i += 1) {
    if (src[i] === '{') depth += 1;
    if (src[i] === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('Unterminated function ' + name);
}

async function testContent(content, opts) {
  const current = { href: opts.url };
  const state = { draft: opts.draft || '', clicks: 0, changes: 0, listener: null };
  const send = { disabled: false, click() { state.clicks += 1; } };
  const editor = {
    focus() {},
    replaceChildren() { state.draft = ''; },
    appendChild(element) { state.draft = String(element.textContent || ''); },
    dispatchEvent(event) {
      if (event.type === 'input') {
        state.changes += 1;
        if (opts.navigateOnInput) current.href = 'https://chatgpt.com/c/other';
      }
    }
  };
  const document = {
    querySelector(query) {
      if (query === '#prompt-textarea') return editor;
      if (query === '[data-testid="send-button"]') return send;
      return null;
    },
    createElement() { return { textContent: '' }; }
  };
  const chrome = { runtime: { onMessage: { addListener(fn) { state.listener = fn; } } } };
  class FakeEvent { constructor(type) { this.type = type; } }
  class FakeTextArea {}
  class FakeInput {}
  // Source initialises a content script and registers one callback. The DOM,
  // chrome API and timers below are entirely synthetic.
  new Function(
    'chrome', 'document', 'location', 'globalThis', 'HTMLTextAreaElement',
    'HTMLInputElement', 'InputEvent', 'Event', 'setTimeout', content
  )(
    chrome, document, current, {}, FakeTextArea, FakeInput,
    FakeEvent, FakeEvent, callback => callback()
  );
  assert.equal(typeof state.listener, 'function');
  const reply = await new Promise((resolve, reject) => {
    try {
      state.listener({
        type: 'send-chatgpt-message',
        text: 'WAS-DEEP-TEST-PAYLOAD',
        expectedUrl: opts.expected
      }, {}, resolve);
    } catch (error) { reject(error); }
  });
  return {
    ok: Boolean(reply.ok),
    terminal: Boolean(reply.terminal),
    mayHaveExecuted: reply.mayHaveExecuted === true,
    clicks: state.clicks,
    changes: state.changes,
    draftPreserved: state.draft === (opts.draft || ''),
    draftReplaced: state.draft === 'WAS-DEEP-TEST-PAYLOAD'
  };
}

async function testCallbackLoss(background) {
  const tab = { id: 7, url: 'https://chatgpt.com/c/synthetic-a', status: 'complete' };
  const observed = { effectfulDispatches: 0, reinjections: 0 };
  const chrome = {
    tabs: {
      async sendMessage() {
        observed.effectfulDispatches += 1;
        if (observed.effectfulDispatches === 1)
          throw new Error('synthetic acknowledgement lost AFTER possible Send');
        return { ok: true };
      }
    },
    scripting: { async executeScript() { observed.reinjections += 1; } }
  };
  const factory = new Function(
    'chrome', 'protectChatTab', 'reviveDiscardedChatTab',
    'waitForTabComplete',
    functionSlice(background, 'callContent') + '; return callContent;'
  );
  const passThrough = async value => value;
  const fn = factory(chrome, passThrough, passThrough, passThrough);
  const reply = await fn(tab, {
    type: 'send-chatgpt-message',
    text: 'WAS-DEEP-TEST-PAYLOAD'
  });
  return { ...observed, replyOk: Boolean(reply.ok) };
}

async function run() {
  const content = await originalSource('browser-extension/content.js');
  const background = await originalSource('browser-extension/background.js');
  const expected = 'https://chatgpt.com/c/synthetic-a';
  const ordinary = await testContent(content, { url: expected, expected, draft: '' });
  const wrong = await testContent(content, {
    url: 'https://chatgpt.com/c/synthetic-b', expected, draft: 'OWNER DRAFT'
  });
  const ownerDraft = await testContent(content, {
    url: expected, expected, draft: 'OWNER DRAFT'
  });
  const midNavigation = await testContent(content, {
    url: expected, expected, draft: '', navigateOnInput: true
  });
  const callbackLoss = await testCallbackLoss(background);
  assert.ok(ordinary.ok && ordinary.clicks === 1);
  assert.ok(!wrong.ok && wrong.terminal && wrong.clicks === 0 && wrong.draftPreserved);
  assert.ok(ownerDraft.ok && ownerDraft.clicks === 1 && ownerDraft.draftReplaced);
  assert.ok(!midNavigation.ok && midNavigation.terminal && midNavigation.clicks === 0);
  assert.equal(callbackLoss.effectfulDispatches, 2);
  assert.equal(callbackLoss.reinjections, 1);
  const result = {
    run_id: 'WAS-DEEP-20261123-SOURCE-01',
    source_revision: REV,
    provenance: 'SIMULATED; actual pinned source functions, synthetic Chrome and DOM',
    harness: 'PASS; reproduces known expected positives and defects',
    product_cases: {
      S01_valid_empty_composer: { status: 'PASS_SIMULATED', ...ordinary },
      S02_wrong_url: { status: 'PASS_SIMULATED', ...wrong },
      S03_mid_navigation: { status: 'PASS_SIMULATED', ...midNavigation },
      S04_owner_draft: { status: 'FAIL_REPRODUCED', ...ownerDraft },
      S05_callback_loss: { status: 'FAIL_REPRODUCED', ...callbackLoss }
    },
    no_live_browser_effects: true
  };
  process.stdout.write(JSON.stringify(result, null, 2) + '\n');
}

run().catch(error => {
  process.stderr.write('HARNESS FAILURE: ' + error.message + '\n');
  process.exitCode = 1;
});
