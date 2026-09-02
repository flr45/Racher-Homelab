(() => {
  if (document.body.dataset.admin !== '1') return;

  function escape(value) {
    const div = document.createElement('div');
    div.textContent = value ?? '';
    return div.innerHTML;
  }

  function formatUptime(seconds) {
    const total = Number(seconds || 0);
    if (!Number.isFinite(total) || total <= 0) return '—';
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return days ? `${days}d ${hours}t` : hours ? `${hours}t ${minutes}m` : `${minutes}m`;
  }

  function ensureCard() {
    let card = document.querySelector('#system-overview-card');
    if (card) return card;
    const stats = document.querySelector('#system .grid.stats');
    if (!stats) return null;
    card = document.createElement('article');
    card.id = 'system-overview-card';
    card.className = 'card readiness-card';
    card.innerHTML = `
      <div class="card-head">
        <div><span class="label">Hele alarmkæden</span><h2>Systemoverblik</h2></div>
        <span id="system-overview-state" class="status-badge">HENTER</span>
      </div>
      <p id="system-overview-summary" class="hint">Kontrollerer scanner, decoder, gateway og SMS-kæde…</p>
      <div id="system-overview-chain" class="readiness-list"></div>
      <div id="system-overview-quality" class="host-metrics"></div>
    `;
    stats.insertAdjacentElement('afterend', card);
    return card;
  }

  function render(data) {
    const card = ensureCard();
    if (!card) return;
    const overview = data.system_overview || {};
    const chain = Array.isArray(overview.chain) ? overview.chain : [];
    const badge = document.querySelector('#system-overview-state');
    const summary = document.querySelector('#system-overview-summary');
    const list = document.querySelector('#system-overview-chain');
    const quality = document.querySelector('#system-overview-quality');

    if (badge) {
      if (overview.end_to_end_ready) {
        badge.textContent = 'HELE KÆDEN ONLINE';
        badge.className = 'status-badge active';
      } else if (overview.local_ready) {
        badge.textContent = 'PAGER OK · SMS TJEKKES';
        badge.className = 'status-badge inactive';
      } else {
        badge.textContent = 'KRÆVER OPMÆRKSOMHED';
        badge.className = 'status-badge inactive';
      }
    }

    if (summary) {
      summary.textContent = overview.end_to_end_ready
        ? `Scanner → PDL → Pager Gateway → SMS Gateway → GSM er online. Pi uptime ${formatUptime(overview.host_uptime_seconds)}.`
        : `Mindst ét led er ikke grønt. Pi uptime ${formatUptime(overview.host_uptime_seconds)}.`;
    }

    if (list) {
      list.innerHTML = chain.length ? chain.map((item) => {
        const ok = item.state === 'ok';
        const warning = item.state === 'warning';
        return `<div class="readiness-row ${ok ? 'ok' : 'pending'}">
          <span class="readiness-icon">${ok ? '✓' : warning ? '!' : '×'}</span>
          <div><strong>${escape(item.label)}</strong><small>${escape(item.detail || '—')}</small></div>
        </div>`;
      }).join('') : '<p class="muted">Ingen kædestatus endnu.</p>';
    }

    if (quality) {
      const hour = data.quality?.hour || {};
      const day = data.quality?.day || {};
      const values = [
        `1t: ${Number(hour.accepted_count || 0)} alarmer / ${Number(hour.raw_count || 0)} rå`,
        `1t: ${Number(hour.duplicate_count || 0)} dubletter`,
        `1t: ${Number(hour.suppressed_count || 0)} undertrykt`,
        `24t: ${Number(day.accepted_count || 0)} alarmer / ${Number(day.raw_count || 0)} rå`,
      ];
      quality.innerHTML = values.map((value) => `<span>${escape(value)}</span>`).join('');
    }
  }

  async function refresh() {
    try {
      const response = await fetch('/api/status', {credentials: 'same-origin', cache: 'no-store'});
      if (response.status === 401) return;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      const card = ensureCard();
      if (!card) return;
      const badge = document.querySelector('#system-overview-state');
      if (badge) {
        badge.textContent = 'STATUSFEJL';
        badge.className = 'status-badge inactive';
      }
      const summary = document.querySelector('#system-overview-summary');
      if (summary) summary.textContent = `Kunne ikke hente systemoverblik: ${error.message}`;
    }
  }

  ensureCard();
  refresh();
  setInterval(refresh, 10000);
})();
