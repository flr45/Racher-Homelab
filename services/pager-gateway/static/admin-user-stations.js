(() => {
  if (document.body.dataset.admin !== '1') return;

  let stationData = {stations: [], assignments: []};
  let users = [];
  let refreshTimer = null;

  const assignmentMap = () => new Map(
    (stationData.assignments || []).map((item) => [Number(item.user_id), Number(item.station_id)])
  );

  function stationOptions(selectedId) {
    const options = ['<option value="">Uden station</option>'];
    for (const station of stationData.stations || []) {
      const selected = Number(selectedId) === Number(station.id) ? ' selected' : '';
      options.push(`<option value="${station.id}"${selected}>${escapeHtml(station.name)}</option>`);
    }
    return options.join('');
  }

  function installUi() {
    if (document.querySelector('#admin-user-stations-card')) return;
    const userList = document.querySelector('#user-list');
    const legacyCard = userList?.closest('.card');
    const panel = document.querySelector('#users');
    if (!panel || !legacyCard) return;

    legacyCard.hidden = true;

    const card = document.createElement('article');
    card.id = 'admin-user-stations-card';
    card.className = 'card';
    card.innerHTML = `
      <div class="card-head">
        <div>
          <span class="label">Admin-overblik</span>
          <h2>Brugere pr. station</h2>
        </div>
        <button id="refresh-admin-user-stations" type="button">Opdater</button>
      </div>
      <p class="hint"><strong>Kun administrativ gruppering.</strong> Stationen her påvirker ikke alarmrouting, RIC-koder eller hvilke meldinger brugeren modtager.</p>
      <form id="admin-user-station-create-form" class="form-grid compact-form">
        <label>Opret station<input name="name" maxlength="80" required placeholder="fx Station Slagelse"></label>
        <div class="actions"><button class="primary" type="submit">Opret station</button></div>
      </form>
      <div id="admin-user-station-groups" class="user-list"><p class="muted">Henter brugere…</p></div>`;

    legacyCard.parentNode.insertBefore(card, legacyCard);

    document.querySelector('#refresh-admin-user-stations')?.addEventListener('click', () => {
      refreshOverview().catch((error) => alert(error.message));
    });

    document.querySelector('#admin-user-station-create-form')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const name = form.elements.name.value.trim();
      if (!name) return;
      try {
        await api('/api/admin/user-stations', {
          method: 'POST',
          body: JSON.stringify({name}),
        });
        form.reset();
        await refreshOverview();
      } catch (error) {
        alert(error.message);
      }
    });

    const observer = new MutationObserver(() => {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => refreshOverview().catch(console.error), 100);
    });
    observer.observe(userList, {childList: true});
  }

  function renderUser(user, selectedStationId) {
    return `<div class="user-row" data-admin-user="${user.id}">
      <div>
        <strong>${escapeHtml(user.display_name)}</strong>
        <div class="muted">${escapeHtml(user.username)} · ${escapeHtml(user.role)} · ${user.push_devices} push-enhed(er)</div>
      </div>
      <div class="actions wrap">
        <span class="status-badge ${user.active ? 'active' : 'inactive'}">${user.active ? 'Aktiv' : 'Deaktiveret'}</span>
        <select data-admin-station-select="${user.id}" aria-label="Administrativ station for ${escapeHtml(user.display_name)}">
          ${stationOptions(selectedStationId)}
        </select>
        <button class="primary" data-admin-station-save="${user.id}" type="button">Gem station</button>
        <button data-admin-user-toggle="${user.id}" data-active="${user.active ? '1' : '0'}" type="button">${user.active ? 'Deaktivér' : 'Aktivér'}</button>
        <button data-admin-user-password="${user.id}" type="button">Ny adgangskode</button>
      </div>
    </div>`;
  }

  function renderOverview() {
    const target = document.querySelector('#admin-user-station-groups');
    if (!target) return;

    const assignments = assignmentMap();
    const groups = new Map();
    for (const station of stationData.stations || []) groups.set(Number(station.id), []);
    const unassigned = [];

    for (const user of users) {
      const stationId = assignments.get(Number(user.id));
      if (stationId && groups.has(stationId)) groups.get(stationId).push(user);
      else unassigned.push(user);
    }

    const sections = [];
    if (unassigned.length) {
      sections.push(`
        <div class="card">
          <div class="card-head"><div><span class="label">Skal placeres</span><h3>Uden station</h3></div><span class="muted">${unassigned.length} bruger${unassigned.length === 1 ? '' : 'e'}</span></div>
          <div class="user-list">${unassigned.map((user) => renderUser(user, null)).join('')}</div>
        </div>`);
    }

    for (const station of stationData.stations || []) {
      const members = groups.get(Number(station.id)) || [];
      sections.push(`
        <div class="card">
          <div class="card-head"><div><span class="label">Station</span><h3>${escapeHtml(station.name)}</h3></div><span class="muted">${members.length} bruger${members.length === 1 ? '' : 'e'}</span></div>
          <div class="user-list">${members.length ? members.map((user) => renderUser(user, station.id)).join('') : '<p class="muted">Ingen brugere på stationen endnu.</p>'}</div>
        </div>`);
    }

    if (!sections.length) {
      sections.push('<p class="muted">Ingen brugere eller administrative stationer endnu.</p>');
    }
    target.innerHTML = sections.join('');

    target.querySelectorAll('[data-admin-station-save]').forEach((button) => button.addEventListener('click', async () => {
      const userId = Number(button.dataset.adminStationSave);
      const select = target.querySelector(`[data-admin-station-select="${userId}"]`);
      const stationId = select?.value ? Number(select.value) : null;
      try {
        await api(`/api/admin/users/${userId}/station`, {
          method: 'PATCH',
          body: JSON.stringify({station_id: stationId}),
        });
        await refreshOverview();
      } catch (error) {
        alert(error.message);
      }
    }));

    target.querySelectorAll('[data-admin-user-toggle]').forEach((button) => button.addEventListener('click', async () => {
      const active = button.dataset.active === '1';
      try {
        await api(`/api/users/${button.dataset.adminUserToggle}`, {
          method: 'PATCH',
          body: JSON.stringify({active: !active}),
        });
        await refreshOverview();
      } catch (error) {
        alert(error.message);
      }
    }));

    target.querySelectorAll('[data-admin-user-password]').forEach((button) => button.addEventListener('click', async () => {
      const password = prompt('Indtast ny adgangskode (mindst 10 tegn):');
      if (password === null) return;
      try {
        await api(`/api/users/${button.dataset.adminUserPassword}`, {
          method: 'PATCH',
          body: JSON.stringify({password}),
        });
        alert('Adgangskoden er ændret.');
      } catch (error) {
        alert(error.message);
      }
    }));
  }

  async function refreshOverview() {
    installUi();
    const [nextUsers, nextStations] = await Promise.all([
      api('/api/users'),
      api('/api/admin/user-stations'),
    ]);
    users = Array.isArray(nextUsers) ? nextUsers : [];
    stationData = nextStations || {stations: [], assignments: []};
    renderOverview();
  }

  installUi();
  document.querySelector('[data-tab="users"]')?.addEventListener('click', () => {
    setTimeout(() => refreshOverview().catch((error) => alert(error.message)), 0);
  });

  if (document.querySelector('#users')?.classList.contains('active')) {
    refreshOverview().catch(console.error);
  }
})();
