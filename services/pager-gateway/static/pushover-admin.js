(() => {
  const isAdmin = document.body?.dataset.admin === '1';
  if (!isAdmin) return;

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

  async function request(url, options = {}) {
    const method = String(options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) headers.set('X-CSRF-Token', csrfToken);
    const response = await fetch(url, {...options, method, headers, credentials: 'same-origin'});
    let data = {};
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
  }

  function findPushoverCard() {
    return [...document.querySelectorAll('#settings-form > article.card')]
      .find((card) => card.querySelector('h2')?.textContent?.trim() === 'Ekstra operatørkanal') || null;
  }

  function ensureUi() {
    const card = findPushoverCard();
    if (!card) return null;

    // The old single-key field is retained in the template only for backwards
    // compatibility. Managed destinations below replace it in the visible UI.
    const legacyField = card.querySelector('input[name="pushover_user_key"]');
    if (legacyField) {
      const label = legacyField.closest('label');
      if (label) label.hidden = true;
    }

    let section = card.querySelector('#pushover-destination-manager');
    if (section) return section;

    section = document.createElement('div');
    section.id = 'pushover-destination-manager';
    section.className = 'split-section';
    section.innerHTML = `
      <div>
        <h3>Pushover-modtagere</h3>
        <p class="hint">Her kan du se de Pushover user/group keys, der er tilføjet. Nøgler vises maskeret efter de er gemt.</p>
        <div id="pushover-destination-list" class="command-list"><p class="muted">Henter modtagere…</p></div>
      </div>
      <div>
        <h3>Tilføj modtager</h3>
        <div class="form-grid compact-form">
          <label>Navn<input id="pushover-destination-label" maxlength="80" autocomplete="off" placeholder="fx Frederik"></label>
          <label>User/group key<input id="pushover-destination-key" type="password" minlength="20" maxlength="80" autocomplete="off" placeholder="Pushover user/group key"></label>
        </div>
        <div class="actions"><button id="pushover-destination-add" type="button" class="primary">Tilføj modtager</button><span id="pushover-destination-status" class="muted"></span></div>
      </div>`;

    const hint = card.querySelector('.hint');
    if (hint) hint.insertAdjacentElement('afterend', section);
    else card.appendChild(section);

    section.querySelector('#pushover-destination-add')?.addEventListener('click', addDestination);
    return section;
  }

  function renderDestinationList(destinations) {
    const section = ensureUi();
    const list = section?.querySelector('#pushover-destination-list');
    if (!list) return;
    if (!destinations.length) {
      list.innerHTML = '<p class="muted">Ingen Pushover-modtagere er tilføjet endnu.</p>';
      return;
    }
    list.innerHTML = destinations.map((item) => `
      <div class="command-row" data-pushover-destination="${Number(item.id)}">
        <div>
          <strong>${escapeHtml(item.label || 'Pushover-modtager')}</strong>
          <small>${escapeHtml(item.key_masked || '—')}</small>
        </div>
        <div class="actions wrap">
          <span class="status-badge ${item.active ? 'active' : 'inactive'}">${item.active ? 'Aktiv' : 'Deaktiveret'}</span>
          <button type="button" data-pushover-toggle="${Number(item.id)}" data-active="${item.active ? '1' : '0'}">${item.active ? 'Deaktivér' : 'Aktivér'}</button>
          <button type="button" data-pushover-delete="${Number(item.id)}">Fjern</button>
        </div>
      </div>`).join('');

    list.querySelectorAll('[data-pushover-toggle]').forEach((button) => button.addEventListener('click', async () => {
      const id = Number(button.dataset.pushoverToggle);
      const active = button.dataset.active === '1';
      button.disabled = true;
      try {
        await request(`/api/pushover/destinations/${id}`, {
          method: 'PATCH',
          body: JSON.stringify({active: !active}),
        });
        await loadDestinations();
      } catch (error) {
        setStatus(error.message);
      } finally {
        button.disabled = false;
      }
    }));

    list.querySelectorAll('[data-pushover-delete]').forEach((button) => button.addEventListener('click', async () => {
      const id = Number(button.dataset.pushoverDelete);
      const row = button.closest('[data-pushover-destination]');
      const label = row?.querySelector('strong')?.textContent || 'denne modtager';
      if (!confirm(`Vil du fjerne ${label} fra Pushover?`)) return;
      button.disabled = true;
      try {
        await request(`/api/pushover/destinations/${id}`, {method: 'DELETE'});
        setStatus('Modtager fjernet.');
        await loadDestinations();
      } catch (error) {
        setStatus(error.message);
      } finally {
        button.disabled = false;
      }
    }));
  }

  function setStatus(message) {
    const section = ensureUi();
    const status = section?.querySelector('#pushover-destination-status');
    if (status) status.textContent = message || '';
  }

  async function loadDestinations() {
    const section = ensureUi();
    if (!section) return;
    try {
      const data = await request('/api/pushover/destinations');
      const destinations = Array.isArray(data.destinations) ? data.destinations : [];
      renderDestinationList(destinations);
      setStatus(destinations.length ? `${destinations.length} modtager${destinations.length === 1 ? '' : 'e'}` : 'Ingen modtagere');
    } catch (error) {
      renderDestinationList([]);
      setStatus(`Kunne ikke hente: ${error.message}`);
    }
  }

  async function addDestination() {
    const section = ensureUi();
    const button = section?.querySelector('#pushover-destination-add');
    const label = section?.querySelector('#pushover-destination-label');
    const key = section?.querySelector('#pushover-destination-key');
    if (!button || !label || !key) return;
    if (!key.value.trim()) {
      setStatus('Indtast en Pushover user/group key.');
      key.focus();
      return;
    }
    button.disabled = true;
    setStatus('Gemmer…');
    try {
      await request('/api/pushover/destinations', {
        method: 'POST',
        body: JSON.stringify({label: label.value, user_key: key.value}),
      });
      label.value = '';
      key.value = '';
      setStatus('Modtager tilføjet.');
      await loadDestinations();
    } catch (error) {
      setStatus(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function loadTokenState() {
    const card = findPushoverCard();
    const token = card?.querySelector('input[name="pushover_app_token"]');
    if (!card || !token) return;
    try {
      const settings = await request('/api/settings');
      let badge = card.querySelector('#pushover-token-state');
      if (!badge) {
        badge = document.createElement('small');
        badge.id = 'pushover-token-state';
        badge.className = 'muted';
        token.insertAdjacentElement('afterend', badge);
      }
      badge.textContent = settings.pushover_app_token_set ? 'App token er gemt · lad feltet være tomt for at beholde det' : 'App token er ikke gemt endnu';
    } catch (_) {
      // The normal settings UI will surface API errors; do not duplicate alerts.
    }
  }

  function boot() {
    ensureUi();
    const settingsTab = document.querySelector('[data-tab="settings"]');
    settingsTab?.addEventListener('click', () => {
      loadDestinations().catch(console.error);
      loadTokenState().catch(console.error);
    });
    loadDestinations().catch(console.error);
    loadTokenState().catch(console.error);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true});
  else boot();
})();