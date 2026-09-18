const imported = await import('@whiskeysockets/baileys');
const common = imported?.default && typeof imported.default === 'object' ? imported.default : {};
const makeWASocket =
  (typeof imported.default === 'function' && imported.default) ||
  imported.makeWASocket || common.makeWASocket || common.default;
const useMultiFileAuthState = imported.useMultiFileAuthState || common.useMultiFileAuthState;
const Browsers = imported.Browsers || common.Browsers;
if (typeof makeWASocket !== 'function') throw new Error('makeWASocket export missing');
if (typeof useMultiFileAuthState !== 'function') throw new Error('useMultiFileAuthState export missing');
if (!Browsers || typeof Browsers.windows !== 'function') throw new Error('Browsers.windows export missing');
console.log('Baileys runtime exports OK');
