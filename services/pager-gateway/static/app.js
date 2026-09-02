const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const isAdmin = document.body.dataset.admin === '1';
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value ?? '';
  return div.innerHTML;
}

function formatDate(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString('da-DK');
}

function formatAlarmTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleTimeString('da-DK', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

function formatAlarmDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleDateString('da-DK');
}

function formatUptime(seconds) {
  const total = Number(seconds || 0);
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  return d ? `${d}d ${h}t` : h ? `${h}t ${m}m` : `${m}m`;
}

function formatBytes(value) {
  let bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit += 1; }
  return `${bytes.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`;
}

function safeJson(value, fallback = []) {
  try { return JSON.parse(value || ''); } catch (_) { return fallback; }
}

function shortSha(value) {
  return value ? String(value).slice(0, 12) : '—';
}

async function api(url, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type') && options.body) headers.set('Content-Type', 'application/json');
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(url, {...options, method, headers, credentials: 'same-origin'});
  let data = {};
  try { data = await response.json(); } catch (_) { data = {}; }
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('Login udløbet');
  }
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function messageRow(row) {
  const wordFilter = String(row.suppressed_reason || '').startsWith('word-filter:')
    ? `Filtreret: ${String(row.suppressed_reason).slice('word-filter:'.length)}`
    : '';
  const meta = [row.protocol, row.ric && `RIC ${row.ric}`, row.baud && `${row.baud} baud`, row.source, wordFilter, row.notification_sent ? 'Pushover ✓' : ''].filter(Boolean).join(' · ');
  return `<div class="history-row"><div class="history-time"><strong>Alarmtid ${escapeHtml(formatAlarmTime(row.received_at))}</strong><br>${escapeHtml(formatAlarmDate(row.received_at))}</div><div><strong>${escapeHtml(row.station || 'Pager-melding')}</strong><p>${escapeHtml(row.message)}</p><small>${escapeHtml(meta)}</small></div></div>`;
}

async function refreshAlarms() {
  const rows = await api('/api/messages?scope=feed&limit=20');
  const latest = rows[0];
  if (latest) {
    $('#latest-title').textContent = latest.station || 'Pager-melding';
    $('#latest-time').textContent = `Alarmtid ${formatAlarmTime(latest.received_at)}`;
    $('#latest-message').textContent = latest.message;
    $('#latest-meta').textContent = [latest.protocol, latest.ric && `RIC ${latest.ric}`, latest.baud && `${latest.baud} baud`, latest.source].filter(Boolean).join(' · ');
  } else {
    $('#latest-title').textContent = 'Ingen aktuelle alarmer';
    $('#latest-time').textContent = '';
    $('#latest-message').textContent = 'Afventer en alarm, der er godkendt til videresendelse.';
    $('#latest-meta').textContent = '';
  }
  $('#alarm-list').innerHTML = rows.length ? rows.map(messageRow).join('') : '<p class="muted">Ingen aktuelle alarmer endnu.</p>';
}

async function refreshHistory() {
  const rows = await api('/api/messages?scope=history&limit=100');
  $('#history-list').innerHTML = rows.length ? rows.map(messageRow).join('') : '<p class="muted">Ingen historik endnu.</p>';
}

$$('.tab').forEach((button) => button.addEventListener('click', async () => {
  $$('.tab').forEach((item) => item.classList.remove('active'));
  $$('.panel').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  $('#' + button.dataset.tab)?.classList.add('active');
  if (button.dataset.tab === 'history') await refreshHistory();
  if (button.dataset.tab === 'system' && isAdmin) { await refreshAdminStatus(); await refreshAudit(); }
  if (button.dataset.tab === 'users' && isAdmin) await refreshUsers();
  if (button.dataset.tab === 'settings' && isAdmin) { await loadSettings(); await loadAlarmFilters(); }
}));

$('#refresh-alarms')?.addEventListener('click', refreshAlarms);
$('#refresh-history')?.addEventListener('click', refreshHistory);

// ---- PWA / Web Push ------------------------------------------------------------

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

async function serviceWorkerRegistration() {
  if (!('serviceWorker' in navigator)) throw new Error('Denne browser understøtter ikke service workers.');
  await navigator.serviceWorker.register('/sw.js', {scope: '/'});
  return navigator.serviceWorker.ready;
}

async function refreshPushState() {
  const enable = $('#push-enable');
  const disable = $('#push-disable');
  const test = $('#push-test');
  const title = $('#push-title');
  const help = $('#push-help');
  if (!window.isSecureContext) {
    title.textContent = 'HTTPS kræves';
    help.textContent = 'PWA push virker på HTTPS. På localhost kan den testes uden certifikat.';
    enable.disabled = true;
    return;
  }
  if (!('Notification' in window) || !('serviceWorker' in navigator)) {
    title.textContent = 'Ikke understøttet'; help.textContent = 'Denne browser kan ikke modtage Web Push.'; enable.disabled = true; return;
  }
  const registration = await serviceWorkerRegistration();
  const subscription = await registration.pushManager.getSubscription();
  const active = Boolean(subscription) && Notification.permission === 'granted';
  title.textContent = active ? 'Notifikationer aktive' : 'Notifikationer ikke aktiveret';
  help.textContent = active ? 'Denne enhed modtager nye pageralarmer som push.' : 'Aktivér notifikationer på denne enhed for at få nye alarmer som push.';
  enable.hidden = active; disable.hidden = !active; test.hidden = !active;
}

$('#push-enable')?.addEventListener('click', async () => {
  const button = $('#push-enable'); button.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('Notifikationer blev ikke tilladt.');
    const registration = await serviceWorkerRegistration();
    const key = await api('/api/push/vapid-public-key');
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key.public_key)});
    await api('/api/push/subscribe', {method: 'POST', body: JSON.stringify(subscription.toJSON())});
    await refreshPushState();
  } catch (error) { alert(error.message); } finally { button.disabled = false; }
});

$('#push-disable')?.addEventListener('click', async () => {
  try {
    const registration = await serviceWorkerRegistration();
    const subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      await api('/api/push/unsubscribe', {method: 'POST', body: JSON.stringify({endpoint: subscription.endpoint})});
      await subscription.unsubscribe();
    }
    await refreshPushState();
  } catch (error) { alert(error.message); }
});

$('#push-test')?.addEventListener('click', async () => {
  try { const result = await api('/api/push/test', {method: 'POST', body: '{}'}); if (!result.ok) throw new Error('Testnotifikationen kunne ikke sendes.'); }
  catch (error) { alert(error.message); }
});

// ---- Admin ---------------------------------------------------------------------

async function queueAction(action, payload = {}, confirmText = '') {
  if (confirmText && !confirm(confirmText)) return false;
  await api('/api/system/commands', {method: 'POST', body: JSON.stringify({action, payload})});
  await refreshCommands();
  return true;
}

function renderReadiness(data) {
  const list = $('#readiness-list');
  if (!list) return;
  const rows = data.readiness || [];
  list.innerHTML = rows.length ? rows.map((row) => `<div class="readiness-row ${escapeHtml(row.state || 'pending')}"><span class="readiness-icon">${row.state === 'ok' ? '✓' : '○'}</span><div><strong>${escapeHtml(row.label)}</strong><small>${escapeHtml(row.detail)}</small></div></div>`).join('') : '<p class="muted">Ingen host-status endnu.</p>';
  const runtime = data.runtime || {};
  $('#runtime-meta').textContent = runtime.agent_heartbeat ? `Host-agent ${formatDate(runtime.agent_heartbeat)}` : 'Afventer host-agent';
  const metrics = [];
  if (runtime.cpu_temp_c) metrics.push(`CPU ${runtime.cpu_temp_c} °C`);
  if (runtime.disk_free_bytes) metrics.push(`${formatBytes(runtime.disk_free_bytes)} ledig`);
  if (runtime.host_uptime_seconds) metrics.push(`Pi uptime ${formatUptime(runtime.host_uptime_seconds)}`);
  if (runtime.gateway_container) metrics.push(`Container ${runtime.gateway_container}`);
  if (runtime.backup_count) metrics.push(`${runtime.backup_count} backup(s)`);
  $('#host-metrics').innerHTML = metrics.map((value) => `<span>${escapeHtml(value)}</span>`).join('');
}

function renderNetwork(runtime) {
  const online = runtime.internet_online === '1';
  const hotspot = runtime.hotspot_active === '1';
  const state = $('#network-state');
  state.textContent = online ? 'ONLINE' : hotspot ? 'SETUP-HOTSPOT' : 'OFFLINE';
  state.className = `status-badge ${online ? 'active' : 'inactive'}`;
  $('#wifi-name').textContent = runtime.wifi_connection || (hotspot ? runtime.hotspot_ssid : 'Ikke forbundet');
  $('#wifi-ip').textContent = runtime.wifi_ip || 'Ingen Wi-Fi IP';
  $('#wifi-signal').textContent = runtime.wifi_signal_percent ? `${runtime.wifi_signal_percent}%` : '—';
  $('#internet-state').textContent = online ? '✓ Online' : '✕ Offline';
  $('#hotspot-ssid').textContent = runtime.hotspot_ssid || 'Racher-Pager-Setup';
  $('#hotspot-portal').textContent = runtime.hotspot_portal || 'http://10.42.0.1/';
  const password = runtime.hotspot_password || '';
  $('#hotspot-password').dataset.value = password;
  if ($('#hotspot-password').dataset.revealed !== '1') $('#hotspot-password').textContent = password ? '••••••••••••••••' : 'Ikke installeret';

  const profiles = safeJson(runtime.wifi_profiles_json, []);
  $('#wifi-profile-list').innerHTML = profiles.length ? profiles.map((item) => `<div class="command-row"><span>${escapeHtml(item.ssid || item.profile)}</span><div><small>${escapeHtml(item.profile)}</small> <button data-wifi-remove="${escapeHtml(item.profile)}">Fjern</button></div></div>`).join('') : '<p class="muted">Ingen Racher-administrerede Wi-Fi-profiler endnu.</p>';
  $$('[data-wifi-remove]').forEach((button) => button.addEventListener('click', async () => {
    if (!confirm('Vil du fjerne denne gemte Wi-Fi-profil?')) return;
    try { await queueAction('wifi-remove', {profile: button.dataset.wifiRemove}); }
    catch (error) { alert(error.message); }
  }));
}

function renderTunnel(runtime) {
  const active = runtime.tunnel_service === 'active';
  const installed = runtime.tunnel_installed === '1';
  $('#tunnel-state').textContent = active ? 'ONLINE' : installed ? 'OFFLINE' : 'IKKE SAT OP';
  $('#tunnel-state').className = `status-badge ${active ? 'active' : 'inactive'}`;
  $('#public-hostname').textContent = runtime.public_hostname || 'Ikke sat';
  $('#tunnel-service').textContent = active ? 'Kører' : installed ? (runtime.tunnel_service || 'Stoppet') : 'Ikke installeret';
  $('#tunnel-version').textContent = runtime.tunnel_version || 'cloudflared';
}

function renderBackups(runtime) {
  const backups = safeJson(runtime.backup_catalog_json, []);
  $('#backup-summary').textContent = backups.length ? `${backups.length} backup(s) · seneste ${formatDate(backups[0].created_at)}` : 'Ingen backups endnu';
  $('#backup-list').innerHTML = backups.length ? backups.map((item) => `<div class="command-row"><div><strong>${escapeHtml(formatDate(item.created_at))}</strong><small>${escapeHtml(formatBytes(item.size))} · ${escapeHtml(item.filename)}</small></div><button data-restore-backup="${escapeHtml(item.filename)}">Gendan</button></div>`).join('') : '<p class="muted">Ingen backupdata endnu.</p>';
  $$('[data-restore-backup]').forEach((button) => button.addEventListener('click', async () => {
    const filename = button.dataset.restoreBackup;
    if (!confirm(`Gendan ${filename}? Nuværende tilstand sikkerhedsbackes først, og gatewayen genstarter.`)) return;
    try { await queueAction('restore-backup', {filename}); }
    catch (error) { alert(error.message); }
  }));
}

function renderUpdate(runtime) {
  const current = runtime.deploy_current_sha || '';
  const remote = runtime.deploy_remote_sha || '';
  const previous = runtime.deploy_previous_sha || '';
  $('#deploy-current').textContent = shortSha(current);
  $('#deploy-remote').textContent = shortSha(remote);
  $('#deploy-previous').textContent = shortSha(previous);
  $('#deploy-branch').textContent = runtime.deploy_branch || '—';
  $('#update-state').textContent = remote && current && remote !== current ? 'Opdatering tilgængelig' : current ? 'Installeret' : 'Afventer Pi';
}

async function refreshAudit() {
  if (!isAdmin || !$('#audit-list')) return;
  const rows = await api('/api/audit');
  $('#audit-list').innerHTML = rows.length ? rows.slice(0, 20).map((row) => `<div class="command-row"><div><strong>${escapeHtml(row.action)}</strong><small>${escapeHtml(row.detail || '')}</small></div><small>${escapeHtml(row.username || 'system')} · ${formatDate(row.created_at)}</small></div>`).join('') : '<p class="muted">Ingen audit-hændelser endnu.</p>';
}

async function refreshAdminStatus() {
  if (!isAdmin) return;
  const data = await api('/api/status');
  $('#message-count').textContent = data.message_count;
  $('#uptime').textContent = formatUptime(data.uptime_seconds);
  $('#hostname').textContent = data.hostname;
  $('#source-state').textContent = data.source.state === 'running' ? 'ONLINE' : String(data.source.state || 'ukendt').toUpperCase();
  $('#source-error').textContent = data.source.error || (data.source_mode === 'mock' ? 'Simulator klar' : 'PDL input aktivt');
  renderReadiness(data);
  const runtime = data.runtime || {};
  renderNetwork(runtime); renderTunnel(runtime); renderBackups(runtime); renderUpdate(runtime);
  await refreshCommands();
}

$('#send-mock')?.addEventListener('click', async () => {
  const button = $('#send-mock'); button.disabled = true;
  try {
    await api('/api/mock', {method: 'POST', body: JSON.stringify({message: $('#mock-message').value})});
    await refreshAlarms(); await refreshAdminStatus();
  } catch (error) { alert(error.message); } finally { button.disabled = false; }
});

async function refreshCommands() {
  if (!isAdmin || !$('#command-list')) return;
  const rows = await api('/api/system/commands');
  $('#command-list').innerHTML = rows.length ? rows.slice(0, 12).map((row) => `<div class="command-row"><div><span>${escapeHtml(row.action)}</span>${row.result ? `<small>${escapeHtml(row.result)}</small>` : ''}</div><small>${escapeHtml(row.status)} · ${formatDate(row.requested_at)}</small></div>`).join('') : '<p class="muted">Ingen systemhandlinger endnu.</p>';
}

const actionDescriptions = {
  'restart-pdl': 'genstarte PDL decoderen?',
  'restart-gateway': 'genstarte Pager Gateway?',
  'reboot': 'genstarte hele Raspberry Pi?',
  'backup-now': 'lave en manuel backup nu?',
  'update-gateway': 'hente og installere den nyeste gateway-version? Der laves backup først.',
  'rollback-gateway': 'rulle gatewayen tilbage til den tidligere fungerende version?',
  'hotspot-start': 'starte Racher-Pager-Setup? Wi-Fi-forbindelsen kan blive afbrudt.',
  'hotspot-stop': 'stoppe setup-hotspottet?',
  'restart-tunnel': 'genstarte Cloudflare Tunnel?',
};

$$('[data-system-action]').forEach((button) => button.addEventListener('click', async () => {
  const action = button.dataset.systemAction;
  button.disabled = true;
  try {
    const ok = await queueAction(action, {}, `Vil du ${actionDescriptions[action] || action}`);
    if (ok && ['update-gateway', 'rollback-gateway', 'reboot', 'restart-gateway'].includes(action)) alert('Handlingen er lagt i kø. Forbindelsen kan kortvarigt forsvinde.');
  } catch (error) { alert(error.message); } finally { button.disabled = false; }
}));

$('#wifi-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  if (!confirm(`Gem Wi-Fi '${values.ssid}' og forsøg at forbinde? Din forbindelse til Pi'en kan kortvarigt forsvinde.`)) return;
  try {
    await queueAction('wifi-add', {ssid: values.ssid, password: values.password});
    form.reset();
    alert('Wi-Fi-profilen er lagt i kø. Pi’en forsøger at skifte forbindelse.');
  } catch (error) { alert(error.message); }
});

$('#reveal-hotspot')?.addEventListener('click', () => {
  const field = $('#hotspot-password');
  const reveal = field.dataset.revealed !== '1';
  field.dataset.revealed = reveal ? '1' : '0';
  field.textContent = reveal ? (field.dataset.value || 'Ikke installeret') : (field.dataset.value ? '••••••••••••••••' : 'Ikke installeret');
  $('#reveal-hotspot').textContent = reveal ? 'Skjul Password/PIN' : 'Vis Password/PIN';
});

$('#refresh-audit')?.addEventListener('click', () => refreshAudit().catch((error) => alert(error.message)));

async function refreshUsers() {
  if (!isAdmin) return;
  const users = await api('/api/users');
  $('#user-list').innerHTML = users.map((user) => `<div class="user-row" data-user-id="${user.id}"><div><strong>${escapeHtml(user.display_name)}</strong><div class="muted">${escapeHtml(user.username)} · ${escapeHtml(user.role)} · ${user.push_devices} push-enhed(er)</div></div><div class="actions wrap"><span class="status-badge ${user.active ? 'active' : 'inactive'}">${user.active ? 'Aktiv' : 'Deaktiveret'}</span><button data-user-toggle="${user.id}" data-active="${user.active ? '1' : '0'}">${user.active ? 'Deaktivér' : 'Aktivér'}</button><button data-user-password="${user.id}">Ny adgangskode</button></div></div>`).join('');
  $$('[data-user-toggle]').forEach((button) => button.addEventListener('click', async () => {
    const active = button.dataset.active === '1';
    try { await api(`/api/users/${button.dataset.userToggle}`, {method: 'PATCH', body: JSON.stringify({active: !active})}); await refreshUsers(); }
    catch (error) { alert(error.message); }
  }));
  $$('[data-user-password]').forEach((button) => button.addEventListener('click', async () => {
    const password = prompt('Indtast ny adgangskode (mindst 10 tegn):');
    if (password === null) return;
    try { await api(`/api/users/${button.dataset.userPassword}`, {method: 'PATCH', body: JSON.stringify({password})}); alert('Adgangskoden er ændret.'); }
    catch (error) { alert(error.message); }
  }));
}

$('#refresh-users')?.addEventListener('click', refreshUsers);
$('#create-user-form')?.addEventListener('submit', async (event) => {
  event.preventDefault(); const form = event.currentTarget; const payload = Object.fromEntries(new FormData(form).entries());
  try { await api('/api/users', {method: 'POST', body: JSON.stringify(payload)}); form.reset(); await refreshUsers(); }
  catch (error) { alert(error.message); }
});

function installAlarmFilterUi() {
  if (!isAdmin || $('#alarm-filter-card')) return;
  const form = $('#settings-form');
  const savebar = form?.querySelector('.savebar');
  if (!form || !savebar) return;
  const card = document.createElement('article');
  card.className = 'card';
  card.id = 'alarm-filter-card';
  card.innerHTML = `
    <span class="label">Manuelt alarmfilter</span>
    <h2>Ord og fraser</h2>
    <p class="hint">Hvis en pageralarm indeholder et af disse ord eller fraser, gemmes råmeldingen i adminhistorikken, men den vises ikke i Alarmfeed og sendes ikke som Web Push eller Pushover. Match er ikke forskel på store og små bogstaver.</p>
    <div class="form-grid">
      <label class="wide">Filtrer på
        <input id="alarm-filter-terms" type="text" maxlength="4000" autocomplete="off" placeholder="fx TEST, ØVELSE, servicebesked">
      </label>
    </div>
    <p class="hint">Adskil flere filtre med komma eller semikolon. Hele fraser kan også bruges.</p>
    <div class="actions"><button id="save-alarm-filters" class="primary" type="button">Gem alarmfilter</button><span id="alarm-filter-status" class="muted"></span></div>`;
  form.insertBefore(card, savebar);
  $('#save-alarm-filters')?.addEventListener('click', saveAlarmFilters);
}

async function loadAlarmFilters() {
  if (!isAdmin) return;
  installAlarmFilterUi();
  const field = $('#alarm-filter-terms');
  const status = $('#alarm-filter-status');
  if (!field) return;
  try {
    const data = await api('/api/alarm-filters');
    const terms = Array.isArray(data.terms) ? data.terms : [];
    field.value = terms.join(', ');
    if (status) status.textContent = terms.length ? `${terms.length} aktiv${terms.length === 1 ? 't' : 'e'} filter${terms.length === 1 ? '' : 'e'}` : 'Ingen aktive filtre';
  } catch (error) {
    if (status) status.textContent = error.message;
  }
}

async function saveAlarmFilters() {
  const button = $('#save-alarm-filters');
  const field = $('#alarm-filter-terms');
  const status = $('#alarm-filter-status');
  if (!button || !field) return;
  button.disabled = true;
  if (status) status.textContent = 'Gemmer…';
  try {
    const result = await api('/api/alarm-filters', {
      method: 'PUT',
      body: JSON.stringify({terms: field.value}),
    });
    const terms = Array.isArray(result.terms) ? result.terms : [];
    field.value = terms.join(', ');
    if (status) status.textContent = terms.length ? `Gemt · ${terms.length} aktiv${terms.length === 1 ? 't' : 'e'} filter${terms.length === 1 ? '' : 'e'}` : 'Gemt · filteret er tomt';
  } catch (error) {
    if (status) status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function loadSettings() {
  if (!isAdmin) return;
  const data = await api('/api/settings'); const form = $('#settings-form');
  for (const [key, value] of Object.entries(data)) {
    const element = form.elements[key]; if (!element || key.endsWith('_set')) continue;
    if (element.type === 'checkbox') element.checked = value === '1' || value === true; else element.value = value ?? '';
  }
}

$('#settings-form')?.addEventListener('submit', async (event) => {
  event.preventDefault(); const form = event.currentTarget; const payload = {};
  [...form.elements].forEach((element) => { if (element.name) payload[element.name] = element.type === 'checkbox' ? element.checked : element.value; });
  $('#save-status').textContent = 'Gemmer…';
  try { await api('/api/settings', {method: 'POST', body: JSON.stringify(payload)}); $('#save-status').textContent = 'Gemt'; }
  catch (error) { $('#save-status').textContent = error.message; }
});

$('#test-pushover')?.addEventListener('click', async () => {
  try { await api('/api/pushover/test', {method: 'POST', body: '{}'}); alert('Pushover-test er sendt.'); }
  catch (error) { alert(error.message); }
});

// ---- Startup -------------------------------------------------------------------

(async function start() {
  try {
    if (isAdmin) installAlarmFilterUi();
    await refreshAlarms(); await refreshPushState();
    if (isAdmin) { await refreshAdminStatus(); await refreshAudit(); }
  } catch (error) { console.error(error); }
  setInterval(() => refreshAlarms().catch(console.error), 3000);
  if (isAdmin) setInterval(() => refreshAdminStatus().catch(console.error), 10000);
})();
