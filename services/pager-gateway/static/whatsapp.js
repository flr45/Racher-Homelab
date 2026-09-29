(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const $ = (selector) => document.querySelector(selector);
  const isAdmin = document.body.dataset.admin === '1';

  function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
  }

  function maskPhone(phone) {
    const value = String(phone || '');
    if (value.length <= 6) return value;
    return `${value.slice(0, 4)}••••${value.slice(-2)}`;
  }

  function statusLabel(value) {
    const labels = {
      queued: 'Afventer',
      sending: 'Sender',
      sent: 'Sendt',
      failed: 'Fejlet',
      uncertain: 'Ukendt efter genstart',
    };
    return labels[value] || value || '—';
  }

  async function waApi(url, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) headers.set('X-CSRF-Token', csrfToken);
    const response = await fetch(url, {...options, method, headers, credentials: 'same-origin'});
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function installCard() {
    const alarms = $('#alarms');
    const notificationCard = alarms?.querySelector('.notification-card');
    if (!alarms || !notificationCard || $('#whatsapp-card')) return;
    const card = document.createElement('article');
    card.className = 'card';
    card.id = 'whatsapp-card';
    card.innerHTML = `
      <div class="card-head">
        <div><span class="label">WhatsApp</span><h2 id="wa-title">Henter status…</h2></div>
        <span id="wa-gateway" class="status-badge">—</span>
      </div>
      <p class="hint">Få de samme godkendte pageralarmer på WhatsApp. Dit stationsvalg bruges automatisk, og støj/dubletter sendes ikke.</p>
      <div class="form-grid compact-form">
        <label class="wide">WhatsApp-nummer<input id="wa-phone" type="tel" autocomplete="tel" placeholder="+4512345678"></label>
        <label class="checkbox wide"><input id="wa-enabled" type="checkbox"> <strong>Send mine pageralarmer på WhatsApp</strong></label>
      </div>
      <div class="actions wrap"><button id="wa-save" class="primary" type="button">Gem WhatsApp</button><button id="wa-test" type="button">Send test</button><span id="wa-status" class="muted"></span></div>
      ${isAdmin ? '<div class="split-section"><div><h3>Seneste WhatsApp-leveringer</h3><p class="hint">Viser også afbrudte eller usikre leveringer efter en genstart.</p><div id="wa-deliveries" class="command-list"><p class="muted">Henter leveringsstatus…</p></div></div></div>' : ''}`;
    notificationCard.insertAdjacentElement('afterend', card);
    $('#wa-save')?.addEventListener('click', save);
    $('#wa-test')?.addEventListener('click', test);
  }


  async function loadAdminDeliveries() {
    if (!isAdmin || !$('#wa-deliveries')) return;
    try {
      const rows = await waApi('/api/whatsapp/deliveries');
      const target = $('#wa-deliveries');
      const visible = Array.isArray(rows) ? rows.slice(0, 20) : [];
      target.innerHTML = visible.length ? visible.map((row) => `
        <div class="command-row">
          <div>
            <strong>${escapeHtml(statusLabel(row.status))} · ${escapeHtml(row.display_name || 'Bruger')}</strong>
            <small>${escapeHtml(maskPhone(row.phone_e164))} · melding #${Number(row.message_id || 0)} · ${escapeHtml(row.station || 'Ukendt område')}</small>
            ${row.error ? `<p>${escapeHtml(row.error)}</p>` : ''}
          </div>
        </div>`).join('') : '<p class="muted">Ingen WhatsApp-leveringer endnu.</p>';
    } catch (error) {
      $('#wa-deliveries').textContent = `Kunne ikke hente leveringsstatus: ${error.message}`;
    }
  }

  async function load() {
    installCard();
    const title = $('#wa-title');
    const gateway = $('#wa-gateway');
    if (!title || !gateway) return;
    try {
      const data = await waApi('/api/whatsapp/me');
      $('#wa-phone').value = data.phone_e164 || '';
      $('#wa-enabled').checked = Boolean(data.enabled);
      title.textContent = data.enabled ? 'WhatsApp-alarm aktiv' : 'WhatsApp-alarm ikke aktiveret';
      const ready = data.gateway_enabled && data.gateway_configured && data.gateway_ready;
      if (ready) {
        gateway.textContent = 'KLAR';
      } else if (!data.gateway_configured) {
        gateway.textContent = 'AFVENTER SETUP';
      } else if (!data.gateway_enabled) {
        gateway.textContent = 'DEAKTIVERET';
      } else {
        gateway.textContent = 'WHATSAPP OFFLINE';
      }
      gateway.className = `status-badge ${ready ? 'active' : 'inactive'}`;
      $('#wa-test').disabled = !data.gateway_ready;
      if (ready) {
        $('#wa-status').textContent = '';
      } else if (!data.gateway_configured) {
        $('#wa-status').textContent = 'OpenWA mangler serveropsætning.';
      } else if (!data.gateway_enabled) {
        $('#wa-status').textContent = 'Gatewayen er konfigureret, men global WhatsApp-afsendelse er slået fra.';
      } else {
        $('#wa-status').textContent = `OpenWA-sessionen er ikke klar (${data.gateway_status || 'ukendt status'}).`;
      }
      await loadAdminDeliveries();
    } catch (error) {
      title.textContent = 'WhatsApp-status kunne ikke hentes';
      $('#wa-status').textContent = error.message;
    }
  }

  async function save() {
    const button = $('#wa-save');
    button.disabled = true;
    $('#wa-status').textContent = 'Gemmer…';
    try {
      const data = await waApi('/api/whatsapp/me', {
        method: 'PUT',
        body: JSON.stringify({
          enabled: $('#wa-enabled').checked,
          phone_e164: $('#wa-phone').value,
        }),
      });
      $('#wa-phone').value = data.phone_e164 || '';
      $('#wa-status').textContent = 'Gemt';
      await load();
    } catch (error) {
      $('#wa-status').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  async function test() {
    const button = $('#wa-test');
    button.disabled = true;
    $('#wa-status').textContent = 'Sender test…';
    try {
      await waApi('/api/whatsapp/test', {method: 'POST', body: '{}'});
      $('#wa-status').textContent = 'Test sendt til WhatsApp';
    } catch (error) {
      $('#wa-status').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();
})();
