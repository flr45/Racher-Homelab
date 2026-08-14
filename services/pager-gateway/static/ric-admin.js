(() => {
  if (document.body.dataset.admin !== '1') return;

  let stations = [];

  const stationOptions = (selected = '') => stations.map((station) =>
    `<option value="${escapeHtml(station.key)}" ${station.key === selected ? 'selected' : ''}>${escapeHtml(station.name)}</option>`
  ).join('');

  async function loadStations() {
    if (!stations.length) stations = await api('/api/stations');
    const createSelect = document.querySelector('#ric-station');
    if (createSelect) createSelect.innerHTML = stationOptions();
  }

  async function refreshRicCodes() {
    await loadStations();
    const rows = await api('/api/ric-codes');
    const target = document.querySelector('#ric-list');
    if (!target) return;
    target.innerHTML = rows.length ? rows.map((row) => `
      <div class="ric-row" data-ric-id="${row.id}">
        <div class="ric-main">
          <label>RIC<input data-ric-field="ric" inputmode="numeric" pattern="[0-9]*" value="${escapeHtml(row.ric)}"></label>
          <label>Station<select data-ric-field="station_key">${stationOptions(row.station_key)}</select></label>
          <label class="ric-label-field">Beskrivelse<input data-ric-field="label" maxlength="120" value="${escapeHtml(row.label || '')}" placeholder="fx Primær alarmgruppe"></label>
          <label class="checkbox ric-active"><input data-ric-field="active" type="checkbox" ${row.active ? 'checked' : ''}> Aktiv</label>
        </div>
        <div class="ric-meta">
          <small>${row.message_count || 0} melding(er)${row.last_seen ? ` · senest ${formatDate(row.last_seen)}` : ''}</small>
          <div class="actions"><button data-ric-save="${row.id}" class="primary">Gem</button><button data-ric-delete="${row.id}">Slet</button></div>
        </div>
      </div>`).join('') : '<p class="muted">Ingen RIC-koder oprettet endnu.</p>';

    target.querySelectorAll('[data-ric-save]').forEach((button) => button.addEventListener('click', async () => {
      const row = button.closest('.ric-row');
      const payload = {
        ric: row.querySelector('[data-ric-field="ric"]').value,
        station_key: row.querySelector('[data-ric-field="station_key"]').value,
        label: row.querySelector('[data-ric-field="label"]').value,
        active: row.querySelector('[data-ric-field="active"]').checked,
      };
      try {
        await api(`/api/ric-codes/${button.dataset.ricSave}`, {method: 'PATCH', body: JSON.stringify(payload)});
        await refreshRicAdmin();
      } catch (error) { alert(error.message); }
    }));

    target.querySelectorAll('[data-ric-delete]').forEach((button) => button.addEventListener('click', async () => {
      if (!confirm('Slet denne RIC-kode? Historiske råmeldinger bliver ikke slettet.')) return;
      try {
        await api(`/api/ric-codes/${button.dataset.ricDelete}`, {method: 'DELETE'});
        await refreshRicAdmin();
      } catch (error) { alert(error.message); }
    }));
  }

  async function refreshUnknownRics() {
    await loadStations();
    const rows = await api('/api/ric-codes/unknown');
    const target = document.querySelector('#unknown-ric-list');
    if (!target) return;
    target.innerHTML = rows.length ? rows.map((row, index) => `
      <div class="unknown-ric-row" data-unknown-ric="${escapeHtml(row.ric)}">
        <div><strong>RIC ${escapeHtml(row.ric)}</strong><small>${row.message_count} melding(er) · senest ${formatDate(row.last_seen)}</small><p>${escapeHtml(row.sample_message || '')}</p></div>
        <div class="unknown-assign"><select data-unknown-station>${stationOptions()}</select><button class="primary" data-unknown-assign="${index}">Tildel</button></div>
      </div>`).join('') : '<p class="muted">Ingen ukendte RIC-koder i historikken.</p>';

    target.querySelectorAll('[data-unknown-assign]').forEach((button) => button.addEventListener('click', async () => {
      const row = button.closest('.unknown-ric-row');
      try {
        await api('/api/ric-codes', {
          method: 'POST',
          body: JSON.stringify({ric: row.dataset.unknownRic, station_key: row.querySelector('[data-unknown-station]').value, label: '', active: true}),
        });
        await refreshRicAdmin();
      } catch (error) { alert(error.message); }
    }));
  }

  async function refreshUserRouting() {
    await loadStations();
    const users = await api('/api/users');
    const target = document.querySelector('#user-routing-list');
    if (!target) return;
    target.innerHTML = users.map((user) => {
      const selected = new Set(user.stations || []);
      const checks = stations.map((station) => `<label class="station-pill"><input type="checkbox" value="${escapeHtml(station.key)}" ${selected.has(station.key) ? 'checked' : ''}> ${escapeHtml(station.name)}</label>`).join('');
      return `<div class="routing-user" data-routing-user="${user.id}"><div><strong>${escapeHtml(user.display_name)}</strong><small>${escapeHtml(user.username)} · ${escapeHtml(user.role)}</small></div><div class="station-pills">${checks}</div><button data-routing-save="${user.id}" class="primary">Gem stationer</button></div>`;
    }).join('');

    target.querySelectorAll('[data-routing-save]').forEach((button) => button.addEventListener('click', async () => {
      const row = button.closest('[data-routing-user]');
      const selected = [...row.querySelectorAll('input[type="checkbox"]:checked')].map((item) => item.value);
      try {
        await api(`/api/users/${button.dataset.routingSave}`, {method: 'PATCH', body: JSON.stringify({stations: selected})});
        button.textContent = 'Gemt ✓';
        setTimeout(() => { button.textContent = 'Gem stationer'; }, 1200);
      } catch (error) { alert(error.message); }
    }));
  }

  async function refreshRicAdmin() {
    await Promise.all([refreshRicCodes(), refreshUnknownRics()]);
  }

  document.querySelector('[data-tab="ric"]')?.addEventListener('click', () => refreshRicAdmin().catch((error) => alert(error.message)));
  document.querySelector('[data-tab="users"]')?.addEventListener('click', () => setTimeout(() => refreshUserRouting().catch(console.error), 0));
  document.querySelector('#refresh-ric')?.addEventListener('click', () => refreshRicAdmin().catch((error) => alert(error.message)));
  document.querySelector('#refresh-user-routing')?.addEventListener('click', () => refreshUserRouting().catch((error) => alert(error.message)));

  document.querySelector('#ric-create-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    values.active = form.elements.active.checked;
    try {
      await api('/api/ric-codes', {method: 'POST', body: JSON.stringify(values)});
      form.reset();
      form.elements.active.checked = true;
      await refreshRicAdmin();
    } catch (error) { alert(error.message); }
  });
})();
