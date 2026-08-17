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

  function ensureCard() {
    let card = document.querySelector('#alarm-filter-card');
    if (card) return card;

    const form = document.querySelector('#settings-form');
    if (!form) return null;

    card = document.createElement('article');
    card.className = 'card';
    card.id = 'alarm-filter-card';
    card.innerHTML = `
      <span class="label">Manuelt alarmfilter</span>
      <h2>Filtrer alarmord</h2>
      <p class="hint">Hvis en pageralarm indeholder et af ordene eller fraserne herunder, gemmes råmeldingen stadig i adminhistorikken, men den vises ikke i Alarmfeed og sendes ikke som Web Push eller Pushover.</p>
      <div class="form-grid">
        <label class="wide">Ord eller fraser
          <input id="alarm-filter-terms" type="text" maxlength="4000" autocomplete="off" placeholder="fx TEST, ØVELSE, servicebesked">
        </label>
      </div>
      <p class="hint">Adskil flere filtre med komma eller semikolon. Store og små bogstaver behandles ens.</p>
      <div class="actions">
        <button id="save-alarm-filters" class="primary" type="button">Gem alarmfilter</button>
        <span id="alarm-filter-status" class="muted">Henter…</span>
      </div>`;

    const adaptiveCard = [...form.querySelectorAll(':scope > article.card')]
      .find((item) => item.textContent?.includes('Støj og dubletter'));
    if (adaptiveCard) adaptiveCard.insertAdjacentElement('afterend', card);
    else form.querySelector('.savebar')?.insertAdjacentElement('beforebegin', card);

    card.querySelector('#save-alarm-filters')?.addEventListener('click', save);
    return card;
  }

  async function load() {
    const card = ensureCard();
    if (!card) return;
    const field = card.querySelector('#alarm-filter-terms');
    const status = card.querySelector('#alarm-filter-status');
    if (!field || !status) return;
    status.textContent = 'Henter…';
    try {
      const data = await request('/api/alarm-filters');
      const terms = Array.isArray(data.terms) ? data.terms : [];
      field.value = terms.join(', ');
      status.textContent = terms.length
        ? `${terms.length} aktiv${terms.length === 1 ? 't' : 'e'} filter${terms.length === 1 ? '' : 'e'}`
        : 'Ingen aktive filtre';
    } catch (error) {
      status.textContent = error.message;
    }
  }

  async function save() {
    const card = ensureCard();
    if (!card) return;
    const field = card.querySelector('#alarm-filter-terms');
    const button = card.querySelector('#save-alarm-filters');
    const status = card.querySelector('#alarm-filter-status');
    if (!field || !button || !status) return;

    button.disabled = true;
    status.textContent = 'Gemmer…';
    try {
      const data = await request('/api/alarm-filters', {
        method: 'PUT',
        body: JSON.stringify({terms: field.value}),
      });
      const terms = Array.isArray(data.terms) ? data.terms : [];
      field.value = terms.join(', ');
      status.textContent = terms.length
        ? `Gemt · ${terms.length} aktiv${terms.length === 1 ? 't' : 'e'} filter${terms.length === 1 ? '' : 'e'}`
        : 'Gemt · ingen aktive filtre';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  function boot() {
    ensureCard();
    document.querySelector('[data-tab="settings"]')?.addEventListener('click', () => load().catch(console.error));
    load().catch(console.error);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true});
  else boot();
})();
