'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');
const QRCode = require('qrcode');
const pino = require('pino');

const HOST = '127.0.0.1';
const PORT = Number(process.env.SBR_WA_PORT || '8765');
const TOKEN = String(process.env.SBR_WA_TOKEN || '').trim();
const SESSION_ID = String(process.env.SBR_WA_SESSION_ID || 'sbr-pager-gateway').trim();
const DATA_DIR = path.resolve(process.env.SBR_WA_DATA_DIR || path.join(process.cwd(), 'data'));
const SESSION_DIR = path.join(DATA_DIR, 'session');
const QR_PATH = path.join(DATA_DIR, 'qr.png');

fs.mkdirSync(DATA_DIR, { recursive: true });
fs.mkdirSync(SESSION_DIR, { recursive: true });

const logger = pino({ level: process.env.SBR_WA_DEBUG === '1' ? 'debug' : 'silent' });

let client = null;
let stopping = false;
let manualLogout = false;
let reconnectTimer = null;
let connectGeneration = 0;
let state = {
  state: 'starting',
  detail: 'Starter WhatsApp-motor',
  sessionId: SESSION_ID,
  qrAvailable: false,
  updatedAt: new Date().toISOString(),
};

function setState(nextState, detail, extra = {}) {
  state = {
    ...state,
    ...extra,
    state: nextState,
    detail: String(detail || ''),
    updatedAt: new Date().toISOString(),
  };
}

function removeQrFile() {
  try {
    if (fs.existsSync(QR_PATH)) fs.unlinkSync(QR_PATH);
  } catch (_) {
    // A stale QR file is not fatal.
  }
}

function resetSessionFiles() {
  try {
    fs.rmSync(SESSION_DIR, { recursive: true, force: true });
  } catch (_) {}
  fs.mkdirSync(SESSION_DIR, { recursive: true });
}

function unauthorized(req) {
  if (!TOKEN) return false;
  const auth = String(req.headers.authorization || '');
  const supplied = auth.startsWith('Bearer ') ? auth.slice(7).trim() : '';
  return supplied !== TOKEN;
}

function jsonResponse(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on('data', chunk => {
      total += chunk.length;
      if (total > 1024 * 1024) {
        reject(new Error('Request too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        const text = Buffer.concat(chunks).toString('utf8');
        resolve(text ? JSON.parse(text) : {});
      } catch (error) {
        reject(error);
      }
    });
    req.on('error', reject);
  });
}

function toChatId(phone) {
  const digits = String(phone || '').replace(/\D/g, '');
  if (!digits) throw new Error('Ugyldigt WhatsApp-nummer');
  return `${digits}@s.whatsapp.net`;
}

function scheduleReconnect(delayMs = 1500) {
  if (stopping || manualLogout) return;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWhatsApp().catch(error => {
      console.error('[whatsapp] reconnect failed:', error);
      setState('error', error && error.message ? error.message : String(error));
      scheduleReconnect(4000);
    });
  }, delayMs);
}

async function connectWhatsApp() {
  const generation = ++connectGeneration;
  setState('starting', 'Forbinder til WhatsApp');
  console.log('[whatsapp] starting Baileys connection');

  const baileys = await import('@whiskeysockets/baileys');
  const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason,
    Browsers,
  } = baileys;

  const { state: authState, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

  const socket = makeWASocket({
    auth: authState,
    logger,
    printQRInTerminal: false,
    browser: Browsers.windows('SBR Pager Gateway'),
    syncFullHistory: false,
    markOnlineOnConnect: false,
    generateHighQualityLinkPreview: false,
    connectTimeoutMs: 30_000,
    defaultQueryTimeoutMs: 30_000,
    keepAliveIntervalMs: 20_000,
  });

  client = socket;

  socket.ev.on('creds.update', saveCreds);

  socket.ev.on('connection.update', async update => {
    if (generation !== connectGeneration || stopping) return;

    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        await QRCode.toFile(QR_PATH, qr, {
          type: 'png',
          width: 420,
          margin: 2,
          errorCorrectionLevel: 'M',
        });
        setState('qr', 'Scan QR-koden i WhatsApp → Forbundne enheder', {
          qrAvailable: true,
        });
        console.log('[whatsapp] QR code ready');
      } catch (error) {
        console.error('[whatsapp] QR write failed:', error);
        setState('error', `Kunne ikke gemme QR-kode: ${error.message || error}`);
      }
    }

    if (connection === 'open') {
      removeQrFile();
      manualLogout = false;
      setState('online', 'WhatsApp er forbundet', { qrAvailable: false });
      console.log('[whatsapp] connected');
      return;
    }

    if (connection !== 'close') return;

    if (client === socket) client = null;

    const error = lastDisconnect && lastDisconnect.error;
    const statusCode =
      (error && error.output && error.output.statusCode) ||
      (error && error.data && error.data.statusCode) ||
      (error && error.statusCode) ||
      null;

    console.error('[whatsapp] connection closed', statusCode || '', error || '');

    if (manualLogout || statusCode === DisconnectReason.loggedOut) {
      removeQrFile();
      setState('offline', 'WhatsApp er logget ud', { qrAvailable: false });
      return;
    }

    setState('starting', 'WhatsApp-forbindelsen blev afbrudt · prøver igen', {
      qrAvailable: false,
    });
    scheduleReconnect();
  });
}

async function sendText(phone, text) {
  if (!client || state.state !== 'online') {
    throw new Error('WhatsApp er ikke online');
  }
  const message = String(text || '').trim();
  if (!message) throw new Error('Beskeden er tom');
  const result = await client.sendMessage(toChatId(phone), {
    text: message.slice(0, 4096),
  });
  return result && result.key && result.key.id ? String(result.key.id) : null;
}

const server = http.createServer(async (req, res) => {
  if (unauthorized(req)) {
    jsonResponse(res, 401, { ok: false, error: 'Unauthorized' });
    return;
  }

  const url = new URL(req.url, `http://${HOST}:${PORT}`);

  if (req.method === 'GET' && url.pathname === '/status') {
    jsonResponse(res, 200, { ok: true, ...state, qrPath: QR_PATH });
    return;
  }

  if (req.method === 'GET' && url.pathname === '/qr') {
    if (!fs.existsSync(QR_PATH)) {
      jsonResponse(res, 404, { ok: false, error: 'QR not available' });
      return;
    }
    const image = fs.readFileSync(QR_PATH);
    res.writeHead(200, {
      'Content-Type': 'image/png',
      'Content-Length': image.length,
      'Cache-Control': 'no-store',
    });
    res.end(image);
    return;
  }

  if (req.method === 'POST' && url.pathname === '/send') {
    try {
      const payload = await readJson(req);
      const messageId = await sendText(payload.to, payload.text);
      jsonResponse(res, 200, { ok: true, messageId });
    } catch (error) {
      jsonResponse(res, 400, { ok: false, error: error.message || String(error) });
    }
    return;
  }

  if (req.method === 'POST' && url.pathname === '/logout') {
    try {
      manualLogout = true;
      connectGeneration += 1;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      const oldClient = client;
      client = null;
      if (oldClient) {
        try { await oldClient.logout(); } catch (_) {}
        try { oldClient.ws && oldClient.ws.close(); } catch (_) {}
      }
      removeQrFile();
      resetSessionFiles();
      setState('starting', 'WhatsApp er logget ud · opretter ny QR-kode', {
        qrAvailable: false,
      });
      jsonResponse(res, 200, { ok: true });
      setTimeout(() => {
        manualLogout = false;
        connectWhatsApp().catch(error => {
          console.error('[whatsapp] restart after logout failed:', error);
          setState('error', error && error.message ? error.message : String(error));
        });
      }, 800);
    } catch (error) {
      manualLogout = false;
      jsonResponse(res, 500, { ok: false, error: error.message || String(error) });
    }
    return;
  }

  jsonResponse(res, 404, { ok: false, error: 'Not found' });
});

async function shutdown() {
  if (stopping) return;
  stopping = true;
  connectGeneration += 1;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  setState('stopping', 'Stopper WhatsApp-motor');
  const oldClient = client;
  client = null;
  try {
    if (oldClient && oldClient.ws) oldClient.ws.close();
  } catch (_) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('uncaughtException', error => {
  console.error('[whatsapp] uncaught exception:', error);
  setState('error', error && error.message ? error.message : String(error));
});
process.on('unhandledRejection', error => {
  console.error('[whatsapp] unhandled rejection:', error);
  setState('error', error && error.message ? error.message : String(error));
});

server.listen(PORT, HOST, () => {
  setState('starting', `Lokal WhatsApp bridge lytter på ${HOST}:${PORT}`);
  console.log(`[whatsapp] bridge listening on ${HOST}:${PORT}`);
  connectWhatsApp().catch(error => {
    console.error('[whatsapp] initial connection failed:', error);
    setState('error', error && error.message ? error.message : String(error));
    scheduleReconnect(4000);
  });
});
