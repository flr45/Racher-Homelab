(() => {
  if (document.body.dataset.admin !== '1') return;

  let rules = [];

  function installUi() {
    if (document.querySelector('#ric-sms-card')) return;
    const panel = document.querySelector('#ric');
    const register = document.querySelector('#ric-list')?.closest('.card');
    if (!panel || !register) return;

    const card = document.createElement('article');
    card.id = 'ric-sms-card';
    card.className = 'card';
    card.innerHTML = `
      <div class="card-head">
        <div><span class="label">RIC → SMS</span><h2>Send SMS på bestemte RIC-koder</h2></div>
        <span id="ric-sms-state" class="status-badge">Deaktiveret</span>
      </div>
      <p class="hint">En SMS sendes kun efter den normale støj-, dublet- og burstbehandling. Flere kopier af samme alarm giver derfor kun én SMS pr. telefonnummer. Simulator og replay sender aldrig SMS.</p>

      <div class="split-section">
        <div>
          <h3>SMS Gateway</h3>
          <div class="form-grid compact-form">
            <label class="checkbox wide"><input id="ric-sms-enabled" type="checkbox"> Aktivér RIC → SMS</label>
            <label class="wide">Gateway URL<input id="ric-sms-gateway-url" type="url" placeholder="http://192.168.1.10:8090" autocomplete="off"></label>
          </div>
          <div class="actions wrap">
            <button id="ric-sms-save-config" class="primary" type="button">Gem SMS-indstillinger</button>
            <span id="ric-sms-config-status" class="muted"></span>
          </div>
          <div class="form-grid compact-form" style="margin-top:1rem">
            <label>Testnummer<input id="ric-sms-test-phone" inputmode="tel" placeholder="+4512345678"></label>
            <div class="actions"><button id="ric-sms-test" type="button">Send test-SMS</button></div>
          </div>
        </div>

        <div>
          <h3>Ny SMS-regel</h3>
          <form id="ric-sms-rule-form" class="form-grid compact-form">
            <label>RIC / capcode<input name="ric" inputmode="numeric" pattern="[0-9]{4,10}" minlength="4" maxlength="10" required placeholder="0006240"></label>
            <label>Telefonnummer<input name="phone" inputmode="tel" required placeholder="+4512345678"></label>
            <label class="wide">Navn / beskrivelse<input name="label" maxlength="120" placeholder="fx Vagttelefon"></label>
            <label class="checkbox wide"><input name="active" type="checkbox" checked> Aktiv</label>
            <div class="wide actions"><button class="primary" type="submit">Opret SMS-regel</button></div>
          </form>
        </div>
      </div>

      <div class="card-head" style="margin-top:1rem"><div><span class="label">Regler</span><h3>Aktive RIC → SMS-koblinger</h3></div><button id="ric-sms-refresh" type="button">Opdater</button></div>
      <div id="ric-sms-rules" class="command-list"><p class="muted">Henter SMS-regler…</p></div>

      <div class="card-head" style="margin-top:1rem"><div><span class="label">Levering</span><h3>Seneste SMS-kørsler</h3></div></div>
      <div id="ric-sms-deliveries" class="command-list"><p class="muted">Ingen SMS-data hentet endnu.</p></div>`;

    panel.insertBefore(card, register);
    bindUi();
  }

  function maskPhone(phone) {
    const value = String(phone || '');
    if (value.length <= 6) return value;
    return `${value.slice(0, 4)}••••${value.slice(-2)}`;
  }

  function statusLabel(value) {
    const names = {pending: 'Afventer', queued: 'I SMS-kø', failed: 'Fejlet'};
    return names[value] || value || '—';
  }

  function renderRules() {
    const target = document.querySelector('#ric-sms-rules');
    if (!target) return;
    target.innerHTML = rules.length ? rules.map((rule) => `
      <div class="command-row" data-ric-sms-rule="${rule.id}">
        <div>
          <strong>RIC ${escapeHtml(rule.ric)} · ${escapeHtml(rule.label || 'SMS-modtager')}</strong>
          <small>${escapeHtml(maskPhone(rule.phone))} · ${rule.active ? 'Aktiv' : 'Deaktiveret'}</small>
        </div>
        <div class="actions wrap">
          <button data-ric-sms-toggle="${rule.id}">${rule.active ? 'Deaktivér' : 'Aktivér'}</button>
          <button data-ric-sms-delete="${rule.id}">Slet</button>
        </div>
      </div>`).join('') : '<p class="muted">Ingen RIC → SMS-regler endnu.</p>';

    target.querySelectorAll('[data-ric-sms-toggle]').forEach((button) => button.addEventListener('click', async () => {
      const rule = rules.find((item) => String(item.id) === button.dataset.ricSmsToggle);
      if (!rule) return;
      try {
        await api(`/api/ric-sms/rules/${rule.id}`, {
          method: 'PATCH', body: JSON.stringify({active: !rule.active}),
        });
        await refresh();
      } catch (error) { alert(error.message); }
    }));

    target.querySelectorAll('[data-ric-sms-delete]').forEach((button) => button.addEventListener('click', async () => {
      if (!confirm('Slet denne RIC → SMS-regel?')) return;
      try {
        await api(`/api/ric-sms/rules/${button.dataset.ricSmsDelete}`, {method: 'DELETE'});
        await refresh();
      } catch (error) { alert(error.message); }
    }));
  }

  function renderDeliveries(rows) {
    const target = document.querySelector('#ric-sms-deliveries');
    if (!target) return;
    target.innerHTML = rows.length ? rows.map((row) => `
      <div class="command-row">
        <div>
          <strong>${escapeHtml(statusLabel(row.status))} · ${escapeHtml(maskPhone(row.recipient))}</strong>
          <small>RIC ${escapeHtml(String(row.matched_rics || '').replaceAll(',', ', '))} · melding #${row.message_id} · ${formatDate(row.created_at)}</small>
          ${row.error ? `<p>${escapeHtml(row.error)}</p>` : ''}
        </div>
      </div>`).join('') : '<p class="muted">Ingen RIC-SMS’er endnu.</p>';
  }

  async function refresh() {
    installUi();
    const [config, loadedRules, deliveries] = await Promise.all([
      api('/api/ric-sms/config'),
      api('/api/ric-sms/rules'),
      api('/api/ric-sms/deliveries?limit=30'),
    ]);
    rules = loadedRules || [];
    document.querySelector('#ric-sms-enabled').checked = Boolean(config.enabled);
    document.querySelector('#ric-sms-gateway-url').value = config.gateway_url || '';
    const state = document.querySelector('#ric-sms-state');
    state.textContent = config.enabled ? 'Aktiv' : 'Deaktiveret';
    state.classList.toggle('active', Boolean(config.enabled));
    renderRules();
    renderDeliveries(deliveries || []);
    decorateRicRows();
  }

  function prefillRic(ric) {
    installUi();
    const form = document.querySelector('#ric-sms-rule-form');
    if (!form) return;
    form.elements.ric.value = ric;
    document.querySelector('#ric-sms-card')?.scrollIntoView({behavior: 'smooth', block: 'start'});
    setTimeout(() => form.elements.phone.focus(), 250);
  }

  function decorateRicRows() {
    document.querySelectorAll('#ric-list .ric-row').forEach((row) => {
      if (row.querySelector('[data-create-ric-sms]')) return;
      const ric = row.querySelector('[data-ric-field="ric"]')?.value;
      const actions = row.querySelector('.actions');
      if (!ric || !actions) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.createRicSms = ric;
      button.textContent = 'SMS-regel';
      button.addEventListener('click', () => prefillRic(row.querySelector('[data-ric-field="ric"]')?.value || ric));
      actions.prepend(button);
    });

    document.querySelectorAll('#unknown-ric-list .unknown-ric-row').forEach((row) => {
      if (row.querySelector('[data-create-ric-sms]')) return;
      const ric = row.dataset.unknownRic;
      if (!ric) return;
      const area = row.querySelector('.unknown-assign') || row;
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.createRicSms = ric;
      button.textContent = 'SMS-regel';
      button.addEventListener('click', () => prefillRic(ric));
      area.appendChild(button);
    });
  }

  function bindUi() {
    document.querySelector('#ric-sms-save-config')?.addEventListener('click', async () => {
      const status = document.querySelector('#ric-sms-config-status');
      status.textContent = 'Gemmer…';
      try {
        const result = await api('/api/ric-sms/config', {
          method: 'PUT',
          body: JSON.stringify({
            enabled: document.querySelector('#ric-sms-enabled').checked,
            gateway_url: document.querySelector('#ric-sms-gateway-url').value,
          }),
        });
        status.textContent = 'Gemt ✓';
        document.querySelector('#ric-sms-state').textContent = result.config.enabled ? 'Aktiv' : 'Deaktiveret';
        setTimeout(() => { status.textContent = ''; }, 1600);
      } catch (error) {
        status.textContent = '';
        alert(error.message);
      }
    });

    document.querySelector('#ric-sms-rule-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      try {
        await api('/api/ric-sms/rules', {
          method: 'POST',
          body: JSON.stringify({
            ric: form.elements.ric.value,
            phone: form.elements.phone.value,
            label: form.elements.label.value,
            active: form.elements.active.checked,
          }),
        });
        form.reset();
        form.elements.active.checked = true;
        await refresh();
      } catch (error) { alert(error.message); }
    });

    document.querySelector('#ric-sms-test')?.addEventListener('click', async () => {
      const phone = document.querySelector('#ric-sms-test-phone').value;
      if (!phone) return alert('Indtast et telefonnummer til test-SMS.');
      try {
        const result = await api('/api/ric-sms/test', {
          method: 'POST', body: JSON.stringify({phone}),
        });
        alert(`Test-SMS er lagt i kø${result.gateway?.id ? ` som #${result.gateway.id}` : ''}.`);
        await refresh();
      } catch (error) { alert(error.message); }
    });

    document.querySelector('#ric-sms-refresh')?.addEventListener('click', () => refresh().catch((error) => alert(error.message)));
  }

  installUi();

  document.querySelector('[data-tab="ric"]')?.addEventListener('click', () => {
    setTimeout(() => refresh().catch((error) => alert(error.message)), 0);
  });

  const observer = new MutationObserver(() => decorateRicRows());
  const ricPanel = document.querySelector('#ric');
  if (ricPanel) observer.observe(ricPanel, {childList: true, subtree: true});
})();
