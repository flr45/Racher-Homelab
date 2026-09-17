'use strict';

const fs = require('fs');
const http = require('http');
const path = require('path');
const wa = require('@open-wa/wa-automate');

const HOST = '127.0.0.1';
const PORT = Number(process.env.SBR_WA_PORT || '8765');
const TOKEN = String(process.env.SBR_WA_TOKEN || '').trim();
const SESSION_ID = String(process.env.SBR_WA_SESSION_ID || 'sbr-pager-gateway').trim();
const DATA_DIR = path.resolve(process.env.SBR_WA_DATA_DIR || path.join(process.cwd(), 'data'));
const SESSION_DIR = path.join(DATA_DIR, 'session');
const QR_PATH = path.join(DATA_DIR, 'qr.png');

fs.mkdirSync(DATA_DIR, { recursive: true });
fs.mkdirSync(SESSION_DIR, { recursive: true });

let client = null;
let stopping = false;
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
  return `${digits}@c.us`;
}

wa.ev.on('qr.**', async (qrcode, sessionId) => {
  if (sessionId && sessionId !== SESSION_ID) return;
  try {
    const imageBuffer = Buffer.from(
      String(qrcode).replace('data:image/png;base64,', ''),
      'base64',
    );
    fs.writeFileSync(QR_PATH, imageBuffer);
    setState('qr', 'Scan QR-koden i WhatsApp → Forbundne enheder', {
      qrAvailable: true,
    });
  } catch (error) {
    setState('error', `Kunne ikke gemme QR-kode: ${error.message}`);
  }
});

async function startWhatsApp() {
  try {
    setState('starting', 'Starter WhatsApp Web-session');
    client = await wa.create({
      sessionId: SESSION_ID,
      multiDevice: true,
      headless: true,
      qrTimeout: 0,
      authTimeout: 0,
      qrLogSkip: true,
      disableSpins: true,
      logConsole: false,
      sessionDataPath: SESSION_DIR,
      deleteSessionDataOnLogout: true,
      killClientOnLogout: false,
    });

    removeQrFile();
    setState('online', 'WhatsApp er forbundet', { qrAvailable: false });

    if (client.onStateChanged) {
      await client.onStateChanged(async newState => {
        const value = String(newState || '').toUpperCase();
        if (value === 'CONNECTED') {
          removeQrFile();
          setState('online', 'WhatsApp er forbundet', { qrAvailable: false });
        } else if (['UNPAIRED', 'UNPAIRED_IDLE', 'CONFLICT', 'TIMEOUT', 'UNLAUNCHED'].includes(value)) {
          setState('offline', `WhatsApp-status: ${value}`);
        }
      });
    }

    if (client.onLogout) {
      await client.onLogout(() => {
        client = null;
        setState('offline', 'WhatsApp er logget ud', { qrAvailable: false });
      });
    }
  } catch (error) {
    client = null;
    setState('error', error && error.message ? error.message : String(error));
  }
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
      if (!client || state.state !== 'online') {
        jsonResponse(res, 503, { ok: false, error: 'WhatsApp er ikke online' });
        return;
      }
      const payload = await readJson(req);
      const text = String(payload.text || '').trim();
      if (!text) throw new Error('Beskeden er tom');
      const chatId = toChatId(payload.to);
      const messageId = await client.sendText(chatId, text.slice(0, 4096));
      jsonResponse(res, 200, { ok: true, messageId: messageId || null });
    } catch (error) {
      jsonResponse(res, 400, { ok: false, error: error.message || String(error) });
    }
    return;
  }

  if (req.method === 'POST' && url.pathname === '/logout') {
    try {
      if (client) {
        await client.logout();
        try { await client.kill(); } catch (_) {}
      }
      client = null;
      removeQrFile();
      setState('offline', 'WhatsApp er logget ud', { qrAvailable: false });
      jsonResponse(res, 200, { ok: true });
    } catch (error) {
      jsonResponse(res, 500, { ok: false, error: error.message || String(error) });
    }
    return;
  }

  jsonResponse(res, 404, { ok: false, error: 'Not found' });
});

async function shutdown() {
  if (stopping) return;
  stopping = true;
  setState('stopping', 'Stopper WhatsApp-motor');
  try {
    if (client) await client.kill();
  } catch (_) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('uncaughtException', error => {
  setState('error', error && error.message ? error.message : String(error));
});
process.on('unhandledRejection', error => {
  setState('error', error && error.message ? error.message : String(error));
});

server.listen(PORT, HOST, () => {
  setState('starting', `Lokal WhatsApp bridge lytter på ${HOST}:${PORT}`);
  startWhatsApp();
});
