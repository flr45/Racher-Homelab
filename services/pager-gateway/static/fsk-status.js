(() => {
  const isAdmin = document.body.dataset.admin === '1';
  const byId = (id) => document.getElementById(id);
  const escape = (value) => {
    const node = document.createElement('div');
    node.textContent = value ?? '';
    return node.innerHTML;
  };
  const formatDate = (value) => value ? new Date(value).toLocaleString('da-DK') : '—';
  const setText = (id, value) => {
    const node = byId(id);
    if (node) node.textContent = value || '—';
  };

  function latency(value) {
    const ms = Number(value);
    if (!Number.isFinite(ms) || ms < 0) return '';
    return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
  }

  function deliveryMeta(row) {
    const delivery = row.delivery || {};
    const parts = [];
    const pushover = delivery.pushover;
    if (pushover) {
      if (pushover.status === 'sent') parts.push(`Pushover ✓${pushover.latency_ms != null ? ` ${latency(pushover.latency_ms)}` : ''}`);
      else if (pushover.status === 'failed') parts.push('Pushover ✕');
      else if (pushover.status === 'disabled') parts.push('Pushover fra');
    } else if (row.notification_sent) {
      parts.push('Pushover ✓');
    }
    const push = delivery.web_push;
    if (push) {
      if (push.status === 'sent') parts.push(`Push ✓ ${push.sent_count}/${push.target_count}${push.latency_ms != null ? ` ${latency(push.latency_ms)}` : ''}`);
      else if (push.status === 'partial') parts.push(`Push delvis ${push.sent_count}/${push.target_count}`);
      else if (push.status === 'failed') parts.push('Push ✕');
      else if (push.status === 'no-target') parts.push('Push ingen enheder');
    }
    return parts;
  }

  function operationalMessageRow(row) {
    const meta = [
      row.protocol,
      row.ric && `RIC ${row.ric}`,
      row.baud && `${row.baud} baud`,
      row.source,
      ...deliveryMeta(row),
    ].filter(Boolean).join(' · ');
    return `<div class="history-row"><div class="history-time">${escape(formatDate(row.received_at))}</div><div><strong>${escape(row.station || 'Pager-melding')}</strong><p>${escape(row.message)}</p><small>${escape(meta)}</small></div></div>`;
  }

  async function getMessages(scope, limit) {
    const response = await fetch(`/api/messages?scope=${encodeURIComponent(scope)}&limit=${limit}`, {credentials: 'same-origin'});
    if (response.status === 401) {
      window.location.href = '/login';
      throw new Error('Login udløbet');
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  // Replace only presentation of alarm/history rows. The main app still owns
  // polling, tabs and authentication; the server owns which alarms are current.
  window.messageRow = operationalMessageRow;
  window.refreshAlarms = async function refreshOperationalAlarms() {
    const rows = await getMessages('feed', 20);
    const latest = rows[0];
    if (latest) {
      setText('latest-title', latest.station || 'Pager-melding');
      setText('latest-time', formatDate(latest.received_at));
      setText('latest-message', latest.message);
      setText('latest-meta', [latest.protocol, latest.baud && `${latest.baud} baud`, latest.source, ...deliveryMeta(latest)].filter(Boolean).join(' · '));
    } else {
      setText('latest-title', 'Ingen aktuelle alarmer');
      setText('latest-time', '');
      setText('latest-message', 'Afventer en aktuel alarm, der er godkendt til videresendelse.');
      setText('latest-meta', '');
    }
    const list = byId('alarm-list');
    if (list) list.innerHTML = rows.length ? rows.map(operationalMessageRow).join('') : '<p class="muted">Ingen aktuelle alarmer.</p>';
  };
  window.refreshHistory = async function refreshOperationalHistory() {
    const rows = await getMessages('history', 100);
    const list = byId('history-list');
    if (list) list.innerHTML = rows.length ? rows.map(operationalMessageRow).join('') : '<p class="muted">Ingen historik endnu.</p>';
  };
  setTimeout(() => window.refreshAlarms().catch(() => {}), 0);

  if (!isAdmin) return;

  function hideLegacyAudioReadiness() {
    document.querySelectorAll('.readiness-row').forEach((row) => {
      const title = row.querySelector('strong');
      if (title && title.textContent.trim() === 'USB lydinput') row.hidden = true;
    });
  }

  function ensureOperationsCard() {
    if (byId('operations-quality-card')) return;
    const panel = byId('system');
    const anchor = panel?.querySelector('.readiness-card');
    if (!panel || !anchor) return;
    const card = document.createElement('article');
    card.id = 'operations-quality-card';
    card.className = 'card';
    card.innerHTML = `
      <div class="card-head"><div><span class="label">Drift & kvalitet</span><h2>Alarmkæden</h2></div><span id="ops-window" class="muted">Aktuelle alarmer: 2 timer</span></div>
      <div class="ops-grid">
        <div><span class="label">Seneste alarm</span><strong id="ops-last-alarm">—</strong><small>godkendt til videresendelse</small></div>
        <div><span class="label">Sidste time</span><strong id="ops-hour">—</strong><small id="ops-hour-detail">—</small></div>
        <div><span class="label">Seneste døgn</span><strong id="ops-day">—</strong><small id="ops-day-detail">—</small></div>
      </div>
      <div class="ops-grid">
        <div><span class="label">Dubletter</span><strong id="ops-duplicates">0</strong><small>undertrykt sidste døgn</small></div>
        <div><span class="label">Fejltegn ?</span><strong id="ops-question-marks">0</strong><small>observeret sidste døgn</small></div>
        <div><span class="label">Leveringsfejl</span><strong id="ops-delivery-failures">0</strong><small>fejl/delvise leveringer</small></div>
      </div>
      <div class="split-section"><div><h3>Systemtest</h3><p class="hint">Tester database, decoder-kilde, routing og dine aktive notifikationskanaler uden at oprette en alarm eller træningsdata.</p><div class="actions"><button id="run-delivery-test" class="primary">Kør systemtest</button></div></div><div><h3>Resultat</h3><div id="delivery-test-result" class="command-list"><p class="muted">Ikke kørt endnu.</p></div></div></div>`;
    anchor.insertAdjacentElement('afterend', card);
    byId('run-delivery-test')?.addEventListener('click', runSystemTest);
  }

  function renderQuality(data) {
    ensureOperationsCard();
    const quality = data.quality || {};
    const hour = quality.hour || {};
    const day = quality.day || {};
    const windowMinutes = Number(data.alarm_window_minutes || 120);
    setText('ops-window', `Aktuelle alarmer: ${windowMinutes >= 60 && windowMinutes % 60 === 0 ? `${windowMinutes / 60} timer` : `${windowMinutes} min.`}`);
    setText('ops-last-alarm', formatDate(hour.last_alarm_at || day.last_alarm_at));
    setText('ops-hour', `${hour.accepted_count || 0}/${hour.raw_count || 0} sendt videre`);
    setText('ops-hour-detail', `${hour.suppressed_count || 0} undertrykt · ${hour.acceptance_percent || 0}% accepteret`);
    setText('ops-day', `${day.accepted_count || 0}/${day.raw_count || 0} sendt videre`);
    setText('ops-day-detail', `${day.noise_count || 0} støj · ${day.fragment_count || 0} fragmenter`);
    setText('ops-duplicates', String(day.duplicate_count || 0));
    setText('ops-question-marks', String(day.question_marks || 0));
    setText('ops-delivery-failures', String((day.delivery_failed || 0) + (day.delivery_partial || 0)));
  }

  async function runSystemTest() {
    const button = byId('run-delivery-test');
    const result = byId('delivery-test-result');
    if (!button || !result) return;
    button.disabled = true;
    result.innerHTML = '<p class="muted">Tester hele kæden…</p>';
    try {
      const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
      const response = await fetch('/api/system/test-delivery', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: '{}',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const labels = {database: 'Database', source: 'Decoder-kilde', routing: 'Routing', pushover: 'Pushover', web_push: 'Web Push'};
      result.innerHTML = Object.entries(data.checks || {}).map(([key, check]) => {
        const ok = check.status === 'ok';
        const neutral = check.status === 'disabled';
        const icon = ok ? '✓' : neutral ? '○' : '✕';
        return `<div class="command-row"><div><strong>${icon} ${escape(labels[key] || key)}</strong><small>${escape(check.detail || '')}</small></div><small>${escape(check.status || '')}</small></div>`;
      }).join('') || '<p class="muted">Ingen testresultater.</p>';
    } catch (error) {
      result.innerHTML = `<p class="muted">Systemtesten fejlede: ${escape(error.message)}</p>`;
    } finally {
      button.disabled = false;
    }
  }

  function render(runtime) {
    const connected = runtime.fsk_usb_connected === '1';
    const inUse = runtime.fsk_usb_pdl_in_use === '1';
    const configured = runtime.fsk_usb_decode_mode && runtime.fsk_usb_decode_mode !== '0';
    const state = byId('fsk-state');
    if (state) {
      state.textContent = connected ? (inUse ? 'AKTIV' : 'TILSLUTTET') : 'AFVENTER';
      state.className = `status-badge ${connected ? 'active' : 'inactive'}`;
    }

    setText('fsk-device', runtime.fsk_usb_device || 'Ikke fundet');
    setText('fsk-summary', runtime.fsk_usb_summary || 'FSK-USB ikke tilsluttet');
    setText('fsk-serial-config', runtime.fsk_usb_serial_config || '19200 8N1');
    setText('fsk-driver', runtime.fsk_usb_driver || 'FTDI / Linux USB-serial');
    setText('fsk-pdl-state', inUse ? 'PDL læser enheden' : configured ? 'PDL konfigureret · afventer input' : 'PDL RS232 ikke aktiveret');
    setText('fsk-input-mode', runtime.fsk_usb_input_mode || 'fsk-usb');

    const serial = runtime.fsk_usb_serial || '';
    const real = runtime.fsk_usb_real_device || '';
    const details = [serial && `Serial ${serial}`, real && real !== runtime.fsk_usb_device ? real : '', connected ? `${runtime.fsk_usb_devices || '1'} seriel enhed fundet` : 'Tilslut FSK-USB til Pi'].filter(Boolean);
    setText('fsk-meta', details.join(' · '));
    hideLegacyAudioReadiness();
  }

  async function refresh() {
    const panel = byId('system');
    if (!panel || !panel.classList.contains('active')) return;
    try {
      const response = await fetch('/api/status', {credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      render(data.runtime || {});
      renderQuality(data);
    } catch (_) {
      // The main app owns login/error handling; this card is supplemental diagnostics.
    }
  }

  const readiness = byId('readiness-list');
  if (readiness) new MutationObserver(hideLegacyAudioReadiness).observe(readiness, {childList: true, subtree: true});

  document.querySelector('[data-tab="system"]')?.addEventListener('click', () => setTimeout(refresh, 0));
  setInterval(refresh, 10000);
  setTimeout(refresh, 0);
})();
