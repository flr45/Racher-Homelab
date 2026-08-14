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
  while (bytes >= 1024 && unit < units.length - 1) {
    bytes /= 1024;
    unit += 1;
  }
  return `${bytes.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`;
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
  const meta = [
    row.protocol,
    row.ric && `RIC ${row.ric}`,
    row.baud && `${row.baud} baud`,
    row.source,
    row.notification_sent ? 'Pushover ✓' : '',
  ].filter(Boolean).join(' · ');
  return `
    <div class="history-row">
      <div class="history-time">${formatDate(row.received_at)}</div>
      <div>
        <strong>${escapeHtml(row.station || 'Pager-melding')}</strong>
        <p>${escapeHtml(row.message)}</p>
        <small>${escapeHtml(meta)}</small>
      </div>
    </div>`;
}

async function refreshAlarms() {
  const rows = await api('/api/messages?limit=20');
  const latest = rows[0];
  if (latest) {
    $('#latest-title').textContent = latest.station || 'Pager-melding';
    $('#latest-time').textContent = formatDate(latest.received_at);
    $('#latest-message').textContent = latest.message;
    $('#latest-meta').textContent = [
      latest.protocol,
      latest.ric && `RIC ${latest.ric}`,
      latest.baud && `${latest.baud} baud`,
      latest.source,
    ].filter(Boolean).join(' · ');
  }
  $('#alarm-list').innerHTML = rows.length ? rows.map(messageRow).join('') : '<p class="muted">Ingen alarmer endnu.</p>';
}

async function refreshHistory() {
  const rows = await api('/api/messages?limit=100');
  $('#history-list').innerHTML = rows.length ? rows.map(messageRow).join('') : '<p class="muted">Ingen alarmer endnu.</p>';
}

$$('.tab').forEach((button) => button.addEventListener('click', async () => {
  $$('.tab').forEach((item) => item.classList.remove('active'));
  $$('.panel').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  $('#' + button.dataset.tab)?.classList.add('active');

  if (button.dataset.tab === 'history') await refreshHistory();
  if (button.dataset.tab === 'system' && isAdmin) await refreshAdminStatus();
  if (button.dataset.tab === 'users' && isAdmin) await refreshUsers();
  if (button.dataset.tab === 'settings' && isAdmin) await loadSettings();
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
    title.textContent = 'Ikke understøttet';
    help.textContent = 'Denne browser kan ikke modtage Web Push.';
    enable.disabled = true;
    return;
  }

  const registration = await serviceWorkerRegistration();
  const subscription = await registration.pushManager.getSubscription();
  const active = Boolean(subscription) && Notification.permission === 'granted';
  title.textContent = active ? 'Notifikationer aktive' : 'Notifikationer ikke aktiveret';
  help.textContent = active
    ? 'Denne enhed modtager nye pageralarmer som push.'
    : 'Aktivér notifikationer på denne enhed for at få nye alarmer som push.';
  enable.hidden = active;
  disable.hidden = !active;
  test.hidden = !active;
}

$('#push-enable')?.addEventListener('click', async () => {
  const button = $('#push-enable');
  button.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('Notifikationer blev ikke tilladt.');
    const registration = await serviceWorkerRegistration();
    const key = await api('/api/push/vapid-public-key');
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key.public_key),
      });
    }
    await api('/api/push/subscribe', {
      method: 'POST',
      body: JSON.stringify(subscription.toJSON()),
    });
    await refreshPushState();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});

$('#push-disable')?.addEventListener('click', async () => {
  try {
    const registration = await serviceWorkerRegistration();
    const subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      await api('/api/push/unsubscribe', {
        method: 'POST',
        body: JSON.stringify({endpoint: subscription.endpoint}),
      });
      await subscription.unsubscribe();
    }
    await refreshPushState();
  } catch (error) {
    alert(error.message);
  }
});

$('#push-test')?.addEventListener('click', async () => {
  try {
    const result = await api('/api/push/test', {method: 'POST', body: '{}'});
    if (!result.ok) throw new Error('Testnotifikationen kunne ikke sendes.');
  } catch (error) {
    alert(error.message);
  }
});

// ---- Admin ---------------------------------------------------------------------

function renderReadiness(data) {
  const list = $('#readiness-list');
  if (!list) return;
  const rows = data.readiness || [];
  list.innerHTML = rows.length ? rows.map((row) => `
    <div class="readiness-row ${escapeHtml(row.state || 'pending')}">
      <span class="readiness-icon">${row.state === 'ok' ? '✓' : '○'}</span>
      <div>
        <strong>${escapeHtml(row.label)}</strong>
        <small>${escapeHtml(row.detail)}</small>
      </div>
    </div>`).join('') : '<p class="muted">Ingen host-status endnu.</p>';

  const runtime = data.runtime || {};
  const heartbeat = runtime.agent_heartbeat;
  $('#runtime-meta').textContent = heartbeat ? `Host-agent ${formatDate(heartbeat)}` : 'Afventer host-agent';

  const metrics = [];
  if (runtime.cpu_temp_c) metrics.push(`CPU ${runtime.cpu_temp_c} °C`);
  if (runtime.disk_free_bytes) metrics.push(`${formatBytes(runtime.disk_free_bytes)} ledig`);
  if (runtime.host_uptime_seconds) metrics.push(`Pi uptime ${formatUptime(runtime.host_uptime_seconds)}`);
  if (runtime.gateway_container) metrics.push(`Container ${runtime.gateway_container}`);
  if (runtime.backup_count) metrics.push(`${runtime.backup_count} backup(s)`);
  $('#host-metrics').innerHTML = metrics.map((value) => `<span>${escapeHtml(value)}</span>`).join('');
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
  await refreshCommands();
}

$('#send-mock')?.addEventListener('click', async () => {
  const button = $('#send-mock');
  button.disabled = true;
  try {
    await api('/api/mock', {
      method: 'POST',
      body: JSON.stringify({message: $('#mock-message').value}),
    });
    await refreshAlarms();
    await refreshAdminStatus();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});

async function refreshCommands() {
  if (!isAdmin || !$('#command-list')) return;
  const rows = await api('/api/system/commands');
  $('#command-list').innerHTML = rows.length ? rows.slice(0, 8).map((row) => `
    <div class="command-row">
      <span>${escapeHtml(row.action)}</span>
      <small>${escapeHtml(row.status)} · ${formatDate(row.requested_at)}</small>
    </div>`).join('') : '<p class="muted">Ingen systemhandlinger endnu.</p>';
}

$$('[data-system-action]').forEach((button) => button.addEventListener('click', async () => {
  const action = button.dataset.systemAction;
  const descriptions = {
    'restart-pdl': 'genstarte PDL decoderen',
    'restart-gateway': 'genstarte Pager Gateway',
    'reboot': 'genstarte hele Raspberry Pi',
  };
  if (!confirm(`Vil du ${descriptions[action] || action}?`)) return;
  button.disabled = true;
  try {
    await api('/api/system/commands', {method: 'POST', body: JSON.stringify({action})});
    await refreshCommands();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}));

async function refreshUsers() {
  if (!isAdmin) return;
  const users = await api('/api/users');
  $('#user-list').innerHTML = users.map((user) => `
    <div class="user-row" data-user-id="${user.id}">
      <div>
        <strong>${escapeHtml(user.display_name)}</strong>
        <div class="muted">${escapeHtml(user.username)} · ${escapeHtml(user.role)} · ${user.push_devices} push-enhed(er)</div>
      </div>
      <div class="actions wrap">
        <span class="status-badge ${user.active ? 'active' : 'inactive'}">${user.active ? 'Aktiv' : 'Deaktiveret'}</span>
        <button data-user-toggle="${user.id}" data-active="${user.active ? '1' : '0'}">${user.active ? 'Deaktivér' : 'Aktivér'}</button>
        <button data-user-password="${user.id}">Ny adgangskode</button>
      </div>
    </div>`).join('');

  $$('[data-user-toggle]').forEach((button) => button.addEventListener('click', async () => {
    const active = button.dataset.active === '1';
    try {
      await api(`/api/users/${button.dataset.userToggle}`, {
        method: 'PATCH',
        body: JSON.stringify({active: !active}),
      });
      await refreshUsers();
    } catch (error) { alert(error.message); }
  }));

  $$('[data-user-password]').forEach((button) => button.addEventListener('click', async () => {
    const password = prompt('Indtast ny adgangskode (mindst 10 tegn):');
    if (password === null) return;
    try {
      await api(`/api/users/${button.dataset.userPassword}`, {
        method: 'PATCH',
        body: JSON.stringify({password}),
      });
      alert('Adgangskoden er ændret.');
    } catch (error) { alert(error.message); }
  }));
}

$('#refresh-users')?.addEventListener('click', refreshUsers);

$('#create-user-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  try {
    await api('/api/users', {method: 'POST', body: JSON.stringify(payload)});
    form.reset();
    await refreshUsers();
  } catch (error) {
    alert(error.message);
  }
});

async function loadSettings() {
  if (!isAdmin) return;
  const data = await api('/api/settings');
  const form = $('#settings-form');
  for (const [key, value] of Object.entries(data)) {
    const element = form.elements[key];
    if (!element || key.endsWith('_set')) continue;
    if (element.type === 'checkbox') element.checked = value === '1' || value === true;
    else element.value = value ?? '';
  }
}

$('#settings-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {};
  [...form.elements].forEach((element) => {
    if (!element.name) return;
    payload[element.name] = element.type === 'checkbox' ? element.checked : element.value;
  });
  $('#save-status').textContent = 'Gemmer…';
  try {
    await api('/api/settings', {method: 'POST', body: JSON.stringify(payload)});
    $('#save-status').textContent = 'Gemt';
  } catch (error) {
    $('#save-status').textContent = error.message;
  }
});

$('#test-pushover')?.addEventListener('click', async () => {
  try {
    await api('/api/pushover/test', {method: 'POST', body: '{}'});
    alert('Pushover-test er sendt.');
  } catch (error) { alert(error.message); }
});

// ---- Startup -------------------------------------------------------------------

(async function start() {
  try {
    await refreshAlarms();
    await refreshPushState();
    if (isAdmin) await refreshAdminStatus();
  } catch (error) {
    console.error(error);
  }

  setInterval(() => refreshAlarms().catch(console.error), 3000);
  if (isAdmin) setInterval(() => refreshAdminStatus().catch(console.error), 10000);
})();
