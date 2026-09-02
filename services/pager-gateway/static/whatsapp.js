(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const $ = (selector) => document.querySelector(selector);

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
      <div class="actions wrap"><button id="wa-save" class="primary" type="button">Gem WhatsApp</button><button id="wa-test" type="button">Send test</button><span id="wa-status" class="muted"></span></div>`;
    notificationCard.insertAdjacentElement('afterend', card);
    $('#wa-save')?.addEventListener('click', save);
    $('#wa-test')?.addEventListener('click', test);
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
      const ready = data.gateway_enabled && data.gateway_configured;
      gateway.textContent = ready ? 'KLAR' : data.gateway_configured ? 'DEAKTIVERET' : 'AFVENTER SETUP';
      gateway.className = `status-badge ${ready ? 'active' : 'inactive'}`;
      $('#wa-test').disabled = !data.gateway_configured;
      if (!ready) $('#wa-status').textContent = data.gateway_configured ? 'Gatewayen er konfigureret, men global WhatsApp-afsendelse er slået fra.' : 'OpenWA mangler serveropsætning.';
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
