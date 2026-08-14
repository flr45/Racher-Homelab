(() => {
  if (document.body.dataset.admin !== '1') return;

  function scorePercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    return `${Math.round(number * 100)}%`;
  }

  async function refreshLearning() {
    const [status, rows] = await Promise.all([
      api('/api/adaptive/status'),
      api('/api/adaptive/review'),
    ]);
    const stats = status.stats || {};
    document.querySelector('#learn-messages').textContent = stats.messages || 0;
    document.querySelector('#learn-feedback').textContent = stats.feedback || 0;
    document.querySelector('#learn-suppressed').textContent =
      Number(stats.noise_suppressed || 0) + Number(stats.duplicates_suppressed || 0);

    const review = document.querySelector('#learning-review');
    if (review) {
      review.innerHTML = rows.length ? rows.map((row) => {
        const statusText = row.suppressed_reason === 'duplicate'
          ? `Dublet af #${row.duplicate_of}`
          : row.suppressed_reason === 'noise'
            ? 'Lært støj · ikke sendt'
            : `${row.relevance_class || 'unknown'} · ${scorePercent(row.relevance_score)}`;
        return `<div class="history-row learning-row" data-learning-message="${row.id}">
          <div class="history-time">${formatDate(row.received_at)}</div>
          <div>
            <strong>${escapeHtml(row.station || 'Ukendt område')}</strong>
            <p>${escapeHtml(row.message || '')}</p>
            <small>${escapeHtml(statusText)}${row.feedback ? ` · admin: ${escapeHtml(row.feedback)}` : ''}${row.ric ? ` · RIC ${escapeHtml(row.ric)}` : ''}</small>
            <div class="actions wrap">
              <button data-feedback="relevant" class="primary">Relevant</button>
              <button data-feedback="noise">Støj</button>
            </div>
          </div>
        </div>`;
      }).join('') : '<p class="muted">Ingen meldinger at lære fra endnu.</p>';

      review.querySelectorAll('[data-feedback]').forEach((button) => button.addEventListener('click', async () => {
        const row = button.closest('[data-learning-message]');
        const verdict = button.dataset.feedback;
        try {
          await api(`/api/adaptive/messages/${row.dataset.learningMessage}/feedback`, {
            method: 'POST', body: JSON.stringify({verdict}),
          });
          await refreshLearning();
        } catch (error) { alert(error.message); }
      }));
    }

    const suggestions = document.querySelector('#station-suggestions');
    const candidates = status.station_suggestions || [];
    if (suggestions) {
      suggestions.innerHTML = candidates.length ? candidates.map((item, index) => `
        <div class="command-row" data-station-candidate="${index}">
          <div><strong>${escapeHtml(item.candidate_name)}</strong><small>${item.seen_count} tydelig(e) observation(er) · ${item.ric_count || 0} RIC · senest ${formatDate(item.last_seen_at)}</small><p>${escapeHtml(item.sample_message || '')}</p></div>
          <button class="primary" data-create-suggested="${escapeHtml(item.candidate_name)}">Opret nu</button>
        </div>`).join('') : '<p class="muted">Ingen uafklarede stationsforslag lige nu.</p>';
      suggestions.querySelectorAll('[data-create-suggested]').forEach((button) => button.addEventListener('click', async () => {
        try {
          await api('/api/stations', {
            method: 'POST', body: JSON.stringify({name: button.dataset.createSuggested}),
          });
          await refreshLearning();
        } catch (error) { alert(error.message); }
      }));
    }
  }

  document.querySelector('[data-tab="learning"]')?.addEventListener('click', () => refreshLearning().catch((error) => alert(error.message)));
  document.querySelector('#refresh-learning')?.addEventListener('click', () => refreshLearning().catch((error) => alert(error.message)));
})();
