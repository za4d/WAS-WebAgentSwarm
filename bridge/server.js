import fs from 'node:fs';
import http from 'node:http';
import crypto from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { WebSocketServer, WebSocket } from 'ws';
import { RELAY_SEND_MODE, verifyRelayPermit, consumeRelayPermit } from './relay-permit.js';

const HOST = process.env.WEB_SWARM_BIND || '127.0.0.1';
const PORT = Number.parseInt(process.env.WEB_SWARM_PORT || '8788', 10);
const BRIDGE_VERSION = '0.6.0';
const PROTOCOL_MIN = 5;
const PROTOCOL_MAX = 5;
const TOKEN_FILE = path.resolve(
  process.env.WEB_SWARM_BRIDGE_TOKEN_FILE
  || path.join(os.homedir(), '.web-agent-swarm', 'bridge-token')
);
const clients = new Set();
const pending = new Map();
const browserEvents = [];
const eventWaiters = new Set();
const MAX_BROWSER_EVENTS = 512;
function validPinnedChatUrl(raw) {
  try {
    const parsed = new URL(raw);
    return parsed.origin === 'https://chatgpt.com'
      && !parsed.search && !parsed.hash
      && (parsed.pathname === '/'
        || /^\/c\/[^/]+\/?$/.test(parsed.pathname)
        || /^\/g\/[^/]+(?:\/c\/[^/]+)?\/?$/.test(parsed.pathname));
  } catch {
    return false;
  }
}

let browserEventSeq = 0;

function loadBridgeToken() {
  const configured = String(process.env.WEB_SWARM_BRIDGE_TOKEN || '').trim();
  if (configured) return configured;
  fs.mkdirSync(path.dirname(TOKEN_FILE), { recursive: true });

  // Do not check existsSync and then blindly write: another bridge process
  // could create its durable authentication token between those operations.
  // The wx flag atomically creates a new token file or returns EEXIST.
  const generated = crypto.randomBytes(32).toString('hex');
  try {
    fs.writeFileSync(TOKEN_FILE, `${generated}\n`, {
      encoding: 'utf8', mode: 0o600, flag: 'wx'
    });
  } catch (error) {
    if (error?.code !== 'EEXIST') throw error;
    const existing = fs.readFileSync(TOKEN_FILE, 'utf8').trim();
    if (existing.length < 32) {
      // A concurrent creator may still be writing, or the prior file is
      // damaged. Fail closed rather than rotating someone else's token.
      throw new Error(`Bridge token file is unexpectedly short: ${TOKEN_FILE}`);
    }
    return existing;
  }
  try { fs.chmodSync(TOKEN_FILE, 0o600); } catch {}
  console.error(`Generated Web Agent Swarm bridge token at ${TOKEN_FILE}`);
  return generated;
}

const BRIDGE_TOKEN = loadBridgeToken();

function safeEqual(left, right) {
  const a = Buffer.from(String(left || ''), 'utf8');
  const b = Buffer.from(String(right || ''), 'utf8');
  if (a.length !== b.length || a.length === 0) return false;
  return crypto.timingSafeEqual(a, b);
}

function tokenFromRequest(req, url) {
  const explicit = String(req.headers['x-was-token'] || '').trim();
  if (explicit) return explicit;
  const auth = String(req.headers.authorization || '').trim();
  if (/^Bearer\s+/i.test(auth)) return auth.replace(/^Bearer\s+/i, '').trim();
  return String(url?.searchParams?.get('token') || '').trim();
}

function authorized(req, url) {
  return safeEqual(tokenFromRequest(req, url), BRIDGE_TOKEN);
}

function browserEventsSince(since = 0, limit = 100) {
  const cleanSince = Math.max(0, Number.parseInt(String(since || 0), 10) || 0);
  const cleanLimit = Math.max(1, Math.min(Number.parseInt(String(limit || 100), 10) || 100, 512));
  return browserEvents.filter(event => event.seq > cleanSince).slice(0, cleanLimit);
}

function flushEventWaiters() {
  for (const waiter of [...eventWaiters]) {
    const events = browserEventsSince(waiter.since, waiter.limit);
    if (!events.length) continue;
    clearTimeout(waiter.timer);
    eventWaiters.delete(waiter);
    json(waiter.res, 200, {
      ok: true,
      events,
      latestSeq: browserEventSeq
    });
  }
}

function appendBrowserEvent(kind, payload = {}) {
  const event = {
    seq: ++browserEventSeq,
    at: Date.now() / 1000,
    kind: String(kind || 'unknown'),
    ...payload
  };
  browserEvents.push(event);
  while (browserEvents.length > MAX_BROWSER_EVENTS) browserEvents.shift();
  flushEventWaiters();
  return event;
}

function json(res, status, body) {
  if (res.writableEnded) return;
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff'
  });
  res.end(JSON.stringify(body));
}

function readyClients() {
  return [...clients].filter(
    ws => ws.readyState === WebSocket.OPEN && ws.wasReady === true
  );
}

function connectedClient() {
  const openClients = readyClients();
  return openClients.length === 1 ? openClients[0] : null;
}

function sendToCurrentProfile(message) {
  const ws = connectedClient();
  if (!ws) return false;
  ws.send(message);
  return true;
}

function awaitResult(id, type, res, timeoutMs = 5000) {
  const timer = setTimeout(() => {
    const item = pending.get(id);
    if (!item) return;
    pending.delete(id);
    json(res, 504, {
      ok: false,
      error: 'Timed out waiting for extension response.',
      requestId: id,
      mayHaveExecuted: type === 'send-result'
    });
  }, timeoutMs);
  // Bind every outstanding receipt to the *specific* extension socket
  // which was selected for dispatch. Another profile may connect before
  // the acknowledgement returns; it must not be able to complete this
  // request by echoing a known request ID.
  pending.set(id, { type, res, timer, client: connectedClient() });
}

function ensureClient(res) {
  const openCount = readyClients().length;
  if (openCount === 1) return true;
  if (openCount === 0) {
    json(res, 503, {
      ok: false,
      error: 'Compatible browser extension is not connected.',
      policy: 'current_connected_browser_profile_only',
      protocolMin: PROTOCOL_MIN,
      protocolMax: PROTOCOL_MAX
    });
    return false;
  }
  json(res, 409, {
    ok: false,
    error: (
      'Multiple browser profiles are connected to this bridge. '
      + 'WAS refuses to guess which profile is current.'
    ),
    extensionClients: openCount,
    policy: 'current_connected_browser_profile_only',
    recovery: (
      'Keep the WAS extension connected in only the browser profile you want to control, '
      + 'or run a separate bridge port for each profile.'
    )
  });
  return false;
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url || '/', `http://${HOST}:${PORT}`);
  if (!authorized(req, url)) {
    return json(res, 401, {
      ok: false,
      error: 'Bridge authentication required.',
      tokenFile: TOKEN_FILE
    });
  }

  if (req.method === 'GET' && url.pathname === '/health') {
    const active = readyClients();
    return json(res, 200, {
      ok: true,
      bridgeVersion: BRIDGE_VERSION,
      extensionClients: active.length,
      extensionVersions: active.map(ws => ws.extensionVersion || '').filter(Boolean),
      protocolVersion: PROTOCOL_MAX,
      protocolMin: PROTOCOL_MIN,
      protocolMax: PROTOCOL_MAX,
      browserEventSeq,
      authRequired: true,
      sendPolicy: RELAY_SEND_MODE,
      profilePolicy: 'current_connected_browser_profile_only'
    });
  }

  if (req.method === 'GET' && url.pathname === '/events') {
    const since = Math.max(
      0,
      Number.parseInt(url.searchParams.get('since') || '0', 10) || 0
    );
    const limit = Math.max(
      1,
      Math.min(Number.parseInt(url.searchParams.get('limit') || '100', 10) || 100, 512)
    );
    const waitMs = Math.max(
      0,
      Math.min(Number.parseInt(url.searchParams.get('waitMs') || '0', 10) || 0, 30000)
    );
    const events = browserEventsSince(since, limit);
    if (events.length || waitMs <= 0) {
      return json(res, 200, { ok: true, events, latestSeq: browserEventSeq });
    }
    const waiter = {
      res,
      since,
      limit,
      timer: setTimeout(() => {
        eventWaiters.delete(waiter);
        json(res, 200, { ok: true, events: [], latestSeq: browserEventSeq });
      }, waitMs)
    };
    eventWaiters.add(waiter);
    res.on('close', () => {
      if (res.writableEnded || !eventWaiters.has(waiter)) return;
      clearTimeout(waiter.timer);
      eventWaiters.delete(waiter);
    });
    return;
  }

  if (req.method === 'GET' && url.pathname === '/tabs') {
    if (!ensureClient(res)) return;
    const id = crypto.randomUUID();
    awaitResult(id, 'tabs-result', res);
    sendToCurrentProfile(JSON.stringify({ type: 'list-tabs', id }));
    return;
  }

  if (req.method === 'GET' && url.pathname === '/inspect') {
    if (!ensureClient(res)) return;
    const tabId = Number.parseInt(url.searchParams.get('tabId') || '', 10);
    if (!Number.isInteger(tabId)) {
      return json(res, 400, { ok: false, error: 'tabId is required.' });
    }
    const tail = Math.max(1, Math.min(
      Number.parseInt(url.searchParams.get('tail') || '6', 10) || 6,
      20
    ));
    const id = crypto.randomUUID();
    awaitResult(id, 'inspect-result', res, 6000);
    sendToCurrentProfile(JSON.stringify({ type: 'inspect', id, tabId, tail }));
    return;
  }

  if (req.method === 'GET' && url.pathname === '/command') {
    if (!ensureClient(res)) return;
    const commandId = String(url.searchParams.get('commandId') || '').trim().slice(0, 128);
    if (!commandId) {
      return json(res, 400, { ok: false, error: 'commandId is required.' });
    }
    const id = crypto.randomUUID();
    awaitResult(id, 'command-status-result', res, 6000);
    sendToCurrentProfile(JSON.stringify({ type: 'command-status', id, commandId }));
    return;
  }

  if (req.method === 'POST' && (url.pathname === '/open' || url.pathname === '/close' || url.pathname === '/send')) {
    let body = '';
    req.setEncoding('utf8');
    req.on('data', chunk => {
      body += chunk;
      if (body.length > 200000) req.destroy();
    });
    req.on('end', () => {
      let data;
      try {
        data = JSON.parse(body || '{}');
      } catch {
        return json(res, 400, { ok: false, error: 'Invalid JSON.' });
      }
      if (!ensureClient(res)) return;

      const id = crypto.randomUUID();
      if (url.pathname === '/open') {
        const target = String(data.url || 'https://chatgpt.com/').trim();
        if (!target.startsWith('https://chatgpt.com/')) {
          return json(res, 400, { ok: false, error: 'Only chatgpt.com URLs may be opened.' });
        }
        awaitResult(id, 'open-result', res);
        sendToCurrentProfile(JSON.stringify({
          type: 'open-tab',
          id,
          url: target,
          active: Boolean(data.active)
        }));
        return;
      }

      if (url.pathname === '/close') {
        const tabId = Number.isInteger(data.tabId) ? data.tabId : undefined;
        if (!Number.isInteger(tabId)) {
          return json(res, 400, { ok: false, error: 'tabId is required.' });
        }
        awaitResult(id, 'close-result', res, 8000);
        sendToCurrentProfile(JSON.stringify({ type: 'close-tab', id, tabId }));
        return;
      }

      const text = String(data.text || '');
      if (!text.trim()) return json(res, 400, { ok: false, error: 'text is required.' });
      const tabId = Number.isInteger(data.tabId) ? data.tabId : undefined;
      const commandId = String(data.commandId || '').trim();
      const expectedUrl = String(data.expectedUrl || '').trim();
      if (!Number.isInteger(tabId) || tabId <= 0) {
        return json(res, 400, { ok: false, error: 'Explicit target tabId is required.' });
      }
      if (!/^[A-Za-z0-9_-]{1,128}$/.test(commandId)) {
        return json(res, 400, { ok: false, error: 'Valid unique commandId is required.' });
      }
      if (!validPinnedChatUrl(expectedUrl)) {
        return json(res, 400, { ok: false, error: 'Pinned ChatGPT target URL is required.' });
      }
      // Relay Send authority is not established by a WAS bearer token.
      // Default deny. In relay_required mode, verify the signed R8 final-send
      // capability and persist a *deny-only* single-use fence before dispatch.
      const candidate = { ...data, tabId, text, commandId, expectedUrl };
      const admission = verifyRelayPermit(candidate);
      if (!admission.ok) {
        return json(res, 403, {ok:false,error:admission.error,mayHaveExecuted:false});
      }
      const consumed = consumeRelayPermit(admission);
      if (!consumed.ok) {
        return json(res, 409, {ok:false,error:consumed.error,
          mayHaveExecuted:consumed.error.includes('already consumed')});
      }
      awaitResult(id, 'send-result', res, 12000);
      const r8 = admission.permit || null;
      sendToCurrentProfile(JSON.stringify({
        type: 'send', id, tabId, text, commandId, expectedUrl,
        r8AttemptId: r8?.attemptId || '',
        r8Recipient: r8?.recipient || '',
        r8Generation: r8?.generation || 0,
        bodySha256: r8?.bodySha256 || ''
      }));
    });
    return;
  }

  json(res, 404, { ok: false, error: 'Not found.' });
});

const wss = new WebSocketServer({
  server,
  path: '/ws',
  verifyClient(info, done) {
    const origin = info.origin || '';
    const originAllowed = !origin || origin.startsWith('chrome-extension://');
    let tokenAllowed = false;
    try {
      const url = new URL(info.req.url || '/ws', `http://${HOST}:${PORT}`);
      tokenAllowed = safeEqual(String(url.searchParams.get('token') || ''), BRIDGE_TOKEN);
    } catch {}
    const allowed = originAllowed && tokenAllowed;
    done(allowed, allowed ? 200 : 401, allowed ? 'OK' : 'Unauthorized');
  }
});

wss.on('connection', ws => {
  clients.add(ws);
  ws.wasReady = false;
  ws.extensionVersion = '';
  const handshakeTimer = setTimeout(() => {
    if (!ws.wasReady) {
      try { ws.close(1002, 'Protocol handshake required'); } catch {}
    }
  }, 5000);

  ws.on('message', raw => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === 'hello') {
        const clientMin = Number.parseInt(String(msg.protocolMin ?? msg.protocolVersion ?? 0), 10) || 0;
        const clientMax = Number.parseInt(String(msg.protocolMax ?? msg.protocolVersion ?? 0), 10) || 0;
        const compatible = clientMax >= PROTOCOL_MIN && clientMin <= PROTOCOL_MAX;
        if (!compatible) {
          ws.send(JSON.stringify({
            type: 'hello-ack',
            ok: false,
            error: 'Incompatible WAS bridge protocol.',
            bridgeVersion: BRIDGE_VERSION,
            protocolMin: PROTOCOL_MIN,
            protocolMax: PROTOCOL_MAX
          }));
          try { ws.close(1002, 'Incompatible WAS protocol'); } catch {}
          return;
        }
        ws.wasReady = true;
        ws.extensionVersion = String(msg.extensionVersion || '').trim().slice(0, 64);
        clearTimeout(handshakeTimer);
        ws.send(JSON.stringify({
          type: 'hello-ack',
          ok: true,
          bridgeVersion: BRIDGE_VERSION,
          protocolMin: PROTOCOL_MIN,
          protocolMax: PROTOCOL_MAX
        }));
        appendBrowserEvent('extension_connected', {
          extensionClients: readyClients().length,
          extensionVersion: ws.extensionVersion,
          protocolMin: clientMin,
          protocolMax: clientMax
        });
        return;
      }

      if (!ws.wasReady) return;
      if (msg.type === 'ping') return;
      if (msg.type === 'browser-event') {
        const event = msg.event && typeof msg.event === 'object' ? msg.event : {};
        appendBrowserEvent(event.kind || 'browser_event', {
          sourceAt: Number(event.at || 0) || null,
          tab: event.tab && typeof event.tab === 'object' ? event.tab : null,
          tabId: Number.isInteger(event.tabId) ? event.tabId : null,
          windowId: Number.isInteger(event.windowId) ? event.windowId : null,
          changes: event.changes && typeof event.changes === 'object' ? event.changes : {}
        });
        return;
      }
      const item = pending.get(msg.id);
      if (!item || item.type !== msg.type || item.client !== ws) return;
      clearTimeout(item.timer);
      pending.delete(msg.id);
      if (msg.type === 'tabs-result') {
        json(item.res, 200, { ok: true, tabs: msg.tabs || [] });
      } else if (msg.type === 'command-status-result') {
        json(item.res, 200, msg);
      } else {
        json(item.res, msg.ok ? 200 : 502, msg);
      }
    } catch {}
  });

  ws.on('close', () => {
    clearTimeout(handshakeTimer);
    const wasReady = ws.wasReady === true;
    clients.delete(ws);
    if (wasReady) {
      appendBrowserEvent('extension_disconnected', {
        extensionClients: readyClients().length,
        extensionVersion: ws.extensionVersion || ''
      });
    }
  });
});

server.listen(PORT, HOST, () => {
  console.error(`Web Agent Swarm bridge ${BRIDGE_VERSION} listening on http://${HOST}:${PORT}`);
  console.error(`Bridge token file: ${TOKEN_FILE}`);
});
