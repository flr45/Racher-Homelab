(() => {
  if (document.body.dataset.admin !== '1') return;

  let currentTrainingRun = null;

  function scorePercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    return `${Math.round(number * 100)}%`;
  }

  function installRicNoiseUi() {
    if (document.querySelector('#ric-noise-filter-card')) return;
    const learningPanel = document.querySelector('#learning');
    const reviewCard = document.querySelector('#learning-review')?.closest('.card');
    if (!learningPanel || !reviewCard) return;

    const card = document.createElement('article');
    card.id = 'ric-noise-filter-card';
    card.className = 'card';
    card.innerHTML = `
      <div class="card-head"><div><span class="label">RIC-støjfilter</span><h2>Ignorer kendte støj-RIC'er</h2></div></div>
      <p class="hint">Filtrerede RIC'er gemmes fortsat i rå historik, men skjules fra Læringskøen. Brug det til faste diagnostik-/testadresser, som ikke skal træne relevansmodellen.</p>
      <form id="ric-noise-filter-form" class="form-grid compact-form">
        <label>RIC / capcode<input name="ric" inputmode="numeric" pattern="[0-9]{4,10}" minlength="4" maxlength="10" required placeholder="0174760"></label>
        <label>Beskrivelse<input name="label" maxlength="120" placeholder="fx Fast diagnostik"></label>
        <div class="wide actions"><button class="primary" type="submit">Tilføj RIC-filter</button></div>
      </form>
      <div id="learning-ric-filters" class="command-list"><p class="muted">Henter RIC-filtre…</p></div>`;
    learningPanel.insertBefore(card, reviewCard);
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

    const ricFilters = document.querySelector('#learning-ric-filters');
    const filters = status.ric_noise_filters || [];
    if (ricFilters) {
      ricFilters.innerHTML = filters.length ? filters.map((item) => `
        <div class="command-row">
          <div><strong>RIC ${escapeHtml(item.ric)}</strong><small>${escapeHtml(item.label || 'Støjfilter')}</small></div>
          <button data-delete-ric-noise="${escapeHtml(item.ric)}">Fjern filter</button>
        </div>`).join('') : '<p class="muted">Ingen RIC-støjfiltre.</p>';
      ricFilters.querySelectorAll('[data-delete-ric-noise]').forEach((button) => button.addEventListener('click', async () => {
        if (!confirm(`Fjern RIC ${button.dataset.deleteRicNoise} fra støjfilteret?`)) return;
        try {
          await api(`/api/adaptive/ric-filters/${encodeURIComponent(button.dataset.deleteRicNoise)}`, {method: 'DELETE'});
          await refreshLearning();
        } catch (error) { alert(error.message); }
      }));
    }

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
              ${row.ric ? `<button data-filter-ric="${escapeHtml(row.ric)}">Filtrer RIC</button>` : ''}
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

      review.querySelectorAll('[data-filter-ric]').forEach((button) => button.addEventListener('click', async () => {
        const ric = button.dataset.filterRic;
        if (!confirm(`Filtrer hele RIC ${ric} fra Læringskøen? Råmeldingerne bliver stadig gemt.`)) return;
        try {
          await api('/api/adaptive/ric-filters', {
            method: 'POST',
            body: JSON.stringify({ric, label: 'Tilføjet fra Læringskø'}),
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

  function installTrainingUi() {
    if (document.querySelector('[data-tab="training"]')) return;
    const settingsTab = document.querySelector('[data-tab="settings"]');
    const settingsPanel = document.querySelector('#settings');
    if (!settingsTab || !settingsPanel) return;

    const tab = document.createElement('button');
    tab.className = 'tab';
    tab.dataset.tab = 'training';
    tab.textContent = 'Træning';
    settingsTab.before(tab);

    const panel = document.createElement('section');
    panel.id = 'training';
    panel.className = 'panel';
    panel.innerHTML = `
      <article class="card">
        <div class="card-head"><div><span class="label">Training / Replay</span><h2>Analyser gamle PDW-meldinger</h2></div><span class="status-badge">Ingen udsendelse</span></div>
        <p class="routing-note">Replay er isoleret fra live-flowet. Linjerne sendes aldrig som Web Push eller Pushover og kommer ikke i den normale alarmhistorik.</p>
        <form id="training-replay-form" class="form-grid">
          <label class="wide">Navn på kørsel<input name="name" maxlength="120" placeholder="fx Gamle PDW-logs august"></label>
          <label class="wide">Læs logfil<input id="training-file" type="file" accept=".txt,.log,.csv,text/plain,text/csv"></label>
          <label class="wide">Meldinger / loglinjer<textarea id="training-text" name="text" required style="width:100%;min-height:240px;resize:vertical" placeholder="Indsæt én PDW-/POCSAG-linje pr. linje…"></textarea></label>
          <div class="wide actions"><button class="primary" type="submit">Analyser uden at sende</button><span id="training-state" class="muted"></span></div>
        </form>
      </article>

      <div id="training-report" class="grid stats" hidden>
        <article class="card"><span class="label">Rigtige</span><strong id="training-real">0</strong><small>ville blive leveret</small></article>
        <article class="card"><span class="label">Dubletter</span><strong id="training-duplicates">0</strong><small>sorteret fra</small></article>
        <article class="card"><span class="label">Støj</span><strong id="training-noise">0</strong><small>lært støj</small></article>
        <article class="card"><span class="label">Ukendte</span><strong id="training-unknown">0</strong><small>behandles som rigtige</small></article>
      </div>

      <article id="training-run-card" class="card" hidden>
        <div class="card-head"><div><span class="label">Læringsrapport</span><h2 id="training-run-title">—</h2></div><button id="training-apply" class="primary">Anvend godkendt læring</button></div>
        <p id="training-run-summary" class="hint"></p>
        <div class="split-section">
          <div><h3>Stations-/områdeforslag</h3><div id="training-station-candidates" class="command-list"></div></div>
          <div><h3>RIC-forslag · kun admin</h3><div id="training-ric-candidates" class="command-list"></div></div>
        </div>
        <div class="card-head"><div><span class="label">Replay-meldinger</span><h3>Vurder læring</h3></div><div class="actions wrap"><button id="training-selected-relevant">Valgte relevante</button><button id="training-selected-noise">Valgte støj</button></div></div>
        <div id="training-events" class="history-list"></div>
      </article>

      <article class="card">
        <div class="card-head"><div><span class="label">Tidligere træning</span><h2>Replay-kørsler</h2></div><button id="refresh-training-runs">Opdater</button></div>
        <div id="training-runs" class="command-list"><p class="muted">Ingen træningskørsler hentet endnu.</p></div>
      </article>

      <article class="card">
        <span class="label">Bulk RIC-import · kun admin</span><h2>Indlæs RIC-listen fra det gamle system</h2>
        <p class="hint">Understøtter semikolon, tabulator eller komma. Format: <code>RIC;Område;Beskrivelse;Aktiv</code>. Første headerlinje må gerne være med.</p>
        <label>Læs CSV/TXT<input id="ric-import-file" type="file" accept=".txt,.csv,text/plain,text/csv"></label>
        <textarea id="ric-import-text" style="width:100%;min-height:180px;resize:vertical" placeholder="1234567;Slagelse;Primær alarmgruppe;1"></textarea>
        <label class="checkbox"><input id="ric-import-create-stations" type="checkbox" checked> Opret manglende områder automatisk ved import</label>
        <div class="actions wrap"><button id="ric-import-preview">Forhåndsvis</button><button id="ric-import-apply" class="primary">Importér RIC-koder</button></div>
        <div id="ric-import-result" class="command-list"></div>
      </article>`;
    settingsPanel.before(panel);

    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((item) => item.classList.remove('active'));
      document.querySelectorAll('.panel').forEach((item) => item.classList.remove('active'));
      tab.classList.add('active');
      panel.classList.add('active');
      refreshTrainingRuns().catch((error) => alert(error.message));
    });
  }

  function trainingDecisionSelect(kind, keyA, keyB, selected) {
    const attrs = kind === 'station'
      ? `data-training-station="${escapeHtml(keyA)}"`
      : `data-training-ric="${escapeHtml(keyA)}" data-training-ric-station="${escapeHtml(keyB)}"`;
    return `<select ${attrs}>
      <option value="pending" ${selected === 'pending' ? 'selected' : ''}>Afventer</option>
      <option value="approved" ${selected === 'approved' ? 'selected' : ''}>Godkend</option>
      <option value="rejected" ${selected === 'rejected' ? 'selected' : ''}>Afvis</option>
    </select>`;
  }

  function renderTrainingRun(run) {
    currentTrainingRun = run;
    document.querySelector('#training-report').hidden = false;
    document.querySelector('#training-run-card').hidden = false;
    document.querySelector('#training-real').textContent = run.real_count || 0;
    document.querySelector('#training-duplicates').textContent = run.duplicate_count || 0;
    document.querySelector('#training-noise').textContent = run.noise_count || 0;
    document.querySelector('#training-unknown').textContent = run.unknown_count || 0;
    document.querySelector('#training-run-title').textContent = run.name || `Kørsel #${run.id}`;
    document.querySelector('#training-run-summary').textContent =
      `${run.parsed_count || 0}/${run.total_lines || 0} linjer analyseret · ${run.unclassified_count || 0} uden område · ${run.station_candidate_count || 0} stationsforslag · ${run.ric_candidate_count || 0} RIC-forslag${run.applied_at ? ' · læring allerede anvendt' : ''}`;
    document.querySelector('#training-apply').disabled = Boolean(run.applied_at);

    const stations = document.querySelector('#training-station-candidates');
    const stationRows = run.station_candidates || [];
    stations.innerHTML = stationRows.length ? stationRows.map((row) => `
      <div class="command-row"><div><strong>${escapeHtml(row.station_name)}</strong><small>${row.seen_count} tydelig(e) observation(er)</small><p>${escapeHtml(row.sample_message || '')}</p></div>${trainingDecisionSelect('station', row.station_name, '', row.decision)}</div>`).join('') : '<p class="muted">Ingen nye stationsforslag.</p>';

    const rics = document.querySelector('#training-ric-candidates');
    const ricRows = run.ric_candidates || [];
    rics.innerHTML = ricRows.length ? ricRows.map((row) => `
      <div class="command-row"><div><strong>RIC ${escapeHtml(row.ric)}</strong><small>${escapeHtml(row.station_name)} · ${row.seen_count} observation(er)</small><p>${escapeHtml(row.sample_message || '')}</p></div>${trainingDecisionSelect('ric', row.ric, row.station_name, row.decision)}</div>`).join('') : '<p class="muted">Ingen nye RIC-forslag.</p>';

    const events = document.querySelector('#training-events');
    const rows = (run.events || []).slice(0, 500);
    events.innerHTML = rows.length ? rows.map((row) => {
      const state = row.suppressed_reason === 'duplicate'
        ? `Dublet af replay #${row.duplicate_of_event_id}`
        : row.suppressed_reason === 'noise'
          ? 'Nuværende model: støj'
          : `${row.relevance_class || 'unknown'} · ${scorePercent(row.relevance_score)}`;
      return `<div class="history-row" data-training-event="${row.id}">
        <div class="history-time"><input type="checkbox" data-training-select value="${row.id}" aria-label="Vælg melding"></div>
        <div><strong>${escapeHtml(row.station || 'Ukendt område')}</strong><p>${escapeHtml(row.message || '')}</p><small>${escapeHtml(state)}${row.ric ? ` · RIC ${escapeHtml(row.ric)}` : ''}${row.feedback ? ` · valgt: ${escapeHtml(row.feedback)}` : ''}</small><div class="actions wrap"><button data-training-feedback="relevant" class="primary">Relevant</button><button data-training-feedback="noise">Støj</button><button data-training-feedback="">Nulstil</button></div></div>
      </div>`;
    }).join('') : '<p class="muted">Ingen parserbare meldinger i replayet.</p>';

    events.querySelectorAll('[data-training-feedback]').forEach((button) => button.addEventListener('click', async () => {
      const row = button.closest('[data-training-event]');
      await setTrainingFeedback([Number(row.dataset.trainingEvent)], button.dataset.trainingFeedback);
    }));
  }

  async function saveTrainingCandidates() {
    if (!currentTrainingRun) return;
    const stations = [...document.querySelectorAll('[data-training-station]')].map((select) => ({
      station_name: select.dataset.trainingStation,
      decision: select.value,
    }));
    const rics = [...document.querySelectorAll('[data-training-ric]')].map((select) => ({
      ric: select.dataset.trainingRic,
      station_name: select.dataset.trainingRicStation,
      decision: select.value,
    }));
    await api(`/api/training/runs/${currentTrainingRun.id}/candidates`, {
      method: 'PATCH', body: JSON.stringify({stations, rics}),
    });
  }

  async function setTrainingFeedback(ids, feedback) {
    for (const id of ids) {
      await api(`/api/training/events/${id}`, {
        method: 'PATCH', body: JSON.stringify({feedback}),
      });
    }
    if (currentTrainingRun) {
      const run = await api(`/api/training/runs/${currentTrainingRun.id}`);
      renderTrainingRun(run);
    }
  }

  function selectedTrainingIds() {
    return [...document.querySelectorAll('[data-training-select]:checked')].map((item) => Number(item.value));
  }

  async function refreshTrainingRuns() {
    const runs = await api('/api/training/runs');
    const target = document.querySelector('#training-runs');
    if (!target) return;
    target.innerHTML = runs.length ? runs.map((run) => `
      <div class="command-row"><div><strong>${escapeHtml(run.name)}</strong><small>#${run.id} · ${run.parsed_count}/${run.total_lines} analyseret · ${run.real_count} rigtige · ${run.duplicate_count} dubletter · ${run.noise_count} støj${run.applied_at ? ' · anvendt' : ''}</small></div><button data-open-training-run="${run.id}">Åbn</button></div>`).join('') : '<p class="muted">Ingen replay-kørsler endnu.</p>';
    target.querySelectorAll('[data-open-training-run]').forEach((button) => button.addEventListener('click', async () => {
      const run = await api(`/api/training/runs/${button.dataset.openTrainingRun}`);
      renderTrainingRun(run);
      document.querySelector('#training-run-card')?.scrollIntoView({behavior: 'smooth', block: 'start'});
    }));
  }

  function renderRicImportPreview(preview) {
    const target = document.querySelector('#ric-import-result');
    const rows = preview.rows || [];
    const errors = preview.errors || [];
    target.innerHTML = `<p><strong>${rows.length}</strong> gyldige RIC-rækker · <strong>${errors.length}</strong> fejl · separator: <code>${escapeHtml(preview.delimiter === '\t' ? 'TAB' : preview.delimiter)}</code></p>` +
      rows.slice(0, 100).map((row) => `<div class="command-row"><div><strong>RIC ${escapeHtml(row.ric)}</strong><small>${escapeHtml(row.station)}${row.label ? ` · ${escapeHtml(row.label)}` : ''}${row.station_exists ? '' : ' · nyt område'}</small></div></div>`).join('') +
      errors.slice(0, 50).map((row) => `<div class="command-row"><div><strong>Linje ${row.line}</strong><small>${escapeHtml(row.error)}</small><p>${escapeHtml(row.raw || '')}</p></div></div>`).join('');
  }

  async function loadFileInto(fileInput, textareaSelector) {
    const file = fileInput.files?.[0];
    if (!file) return;
    const text = await file.text();
    document.querySelector(textareaSelector).value = text;
  }

  installTrainingUi();
  installRicNoiseUi();

  document.querySelector('[data-tab="learning"]')?.addEventListener('click', () => refreshLearning().catch((error) => alert(error.message)));
  document.querySelector('#refresh-learning')?.addEventListener('click', () => refreshLearning().catch((error) => alert(error.message)));
  document.querySelector('#ric-noise-filter-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api('/api/adaptive/ric-filters', {
        method: 'POST',
        body: JSON.stringify({ric: form.elements.ric.value, label: form.elements.label.value}),
      });
      form.reset();
      await refreshLearning();
    } catch (error) { alert(error.message); }
  });

  document.querySelector('#training-file')?.addEventListener('change', (event) => loadFileInto(event.currentTarget, '#training-text').catch((error) => alert(error.message)));
  document.querySelector('#ric-import-file')?.addEventListener('change', (event) => loadFileInto(event.currentTarget, '#ric-import-text').catch((error) => alert(error.message)));
  document.querySelector('#refresh-training-runs')?.addEventListener('click', () => refreshTrainingRuns().catch((error) => alert(error.message)));

  document.querySelector('#training-replay-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const state = document.querySelector('#training-state');
    state.textContent = 'Analyserer…';
    try {
      const result = await api('/api/training/replay', {
        method: 'POST',
        body: JSON.stringify({name: form.elements.name.value, text: form.elements.text.value}),
      });
      renderTrainingRun(result.run);
      await refreshTrainingRuns();
      state.textContent = 'Færdig ✓';
    } catch (error) {
      state.textContent = '';
      alert(error.message);
    }
  });

  document.querySelector('#training-selected-relevant')?.addEventListener('click', async () => {
    const ids = selectedTrainingIds();
    if (!ids.length) return alert('Vælg mindst én replay-melding.');
    try { await setTrainingFeedback(ids, 'relevant'); } catch (error) { alert(error.message); }
  });
  document.querySelector('#training-selected-noise')?.addEventListener('click', async () => {
    const ids = selectedTrainingIds();
    if (!ids.length) return alert('Vælg mindst én replay-melding.');
    try { await setTrainingFeedback(ids, 'noise'); } catch (error) { alert(error.message); }
  });

  document.querySelector('#training-apply')?.addEventListener('click', async () => {
    if (!currentTrainingRun) return;
    if (!confirm('Anvend de godkendte stations-/RIC-forslag og den valgte Relevant/Støj-feedback på den rigtige læringsmodel?')) return;
    try {
      await saveTrainingCandidates();
      const result = await api(`/api/training/runs/${currentTrainingRun.id}/apply`, {method: 'POST', body: '{}'});
      alert(`Læring anvendt: ${result.result.stations_created} område(r), ${result.result.rics_created} RIC og ${result.result.feedback_applied} feedback-vurderinger.`);
      const run = await api(`/api/training/runs/${currentTrainingRun.id}`);
      renderTrainingRun(run);
      await Promise.all([refreshTrainingRuns(), refreshLearning()]);
    } catch (error) { alert(error.message); }
  });

  document.querySelector('#ric-import-preview')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/training/ric-import/preview', {
        method: 'POST', body: JSON.stringify({text: document.querySelector('#ric-import-text').value}),
      });
      renderRicImportPreview(result.preview);
    } catch (error) { alert(error.message); }
  });

  document.querySelector('#ric-import-apply')?.addEventListener('click', async () => {
    if (!confirm('Importér de gyldige RIC-koder til det rigtige RIC-register? Eksisterende RIC-koder overskrives ikke.')) return;
    try {
      const result = await api('/api/training/ric-import/apply', {
        method: 'POST',
        body: JSON.stringify({
          text: document.querySelector('#ric-import-text').value,
          create_missing_stations: document.querySelector('#ric-import-create-stations').checked,
        }),
      });
      const value = result.result;
      document.querySelector('#ric-import-result').innerHTML = `<p><strong>Import færdig:</strong> ${value.created} RIC oprettet · ${value.skipped_existing} eksisterende sprunget over · ${value.stations_created} nye områder · ${value.errors.length} fejl.</p>`;
    } catch (error) { alert(error.message); }
  });
})();
