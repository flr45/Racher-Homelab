(() => {
  const isAdmin = document.body.dataset.admin === '1';
  let feedCatalog = [];
  const routingState = new Map();

  const rssEscape = (value) => escapeHtml(value ?? '');
  const rssDate = (value) => formatDate(value || '');

  const RSS_GROUPS = [
    {key: 'national', label: 'Nationale', quick: 'Kun nationale'},
    {key: 'zealand', label: 'Sjælland', quick: 'Kun Sjælland'},
    {key: 'funen', label: 'Fyn', quick: 'Kun Fyn'},
    {key: 'jutland', label: 'Jylland', quick: 'Kun Jylland'},
    {key: 'islands', label: 'Øer', quick: 'Kun Øer'},
    {key: 'other', label: 'Andre RSS-feeds', quick: 'Kun andre'},
  ];

  function feedGroup(feed) {
    const name = String(feed.name || '').toLocaleLowerCase('da-DK');
    if (
      name.includes('politi update · alle') ||
      name.includes('rigspolitiet') ||
      name.includes('national enhed')
    ) return 'national';
    if (name.includes('bornholm')) return 'islands';
    if (name.includes('fyn')) return 'funen';
    if (
      name.includes('københavns') ||
      name.includes('nordsjællands') ||
      name.includes('vestsjællands') ||
      name.includes('sydsjællands')
    ) return 'zealand';
    if (
      name.includes('jylland') ||
      name.includes('sønderjylland')
    ) return 'jutland';
    return 'other';
  }

  function activeFeeds() {
    return feedCatalog.filter((feed) => feed.active);
  }

  function rssEntry(row) {
    const when = row.published_at || row.first_seen_at;
    const source = row.feed_names || 'RSS';
    const summary = row.summary && row.summary !== row.title ? `<p>${rssEscape(row.summary)}</p>` : '';
    const link = row.link ? `<div class="actions"><a href="${rssEscape(row.link)}" target="_blank" rel="noopener noreferrer">Åbn original</a></div>` : '';
    return `<div class="history-row"><div class="history-time">${rssEscape(rssDate(when))}</div><div><strong>${rssEscape(row.title || 'Politi Update')}</strong>${summary}<small>${rssEscape(source)}</small>${link}</div></div>`;
  }

  async function loadFeedCatalog(force = false) {
    if (!force && feedCatalog.length) return feedCatalog;
    feedCatalog = await api('/api/rss/feeds');
    return feedCatalog;
  }

  async function refreshPolitiUpdates() {
    const [rows, me] = await Promise.all([
      api('/api/rss/items?limit=60'),
      api('/api/me'),
    ]);
    const feeds = await loadFeedCatalog();
    const selected = new Set(me.rss_feeds || []);
    const selectedFeeds = feeds.filter((feed) => selected.has(feed.id));
    const summary = document.querySelector('#rss-feed-summary');
    if (summary) {
      summary.innerHTML = selectedFeeds.length
        ? selectedFeeds.map((feed) => `<span class="station-pill">${rssEscape(feed.name)}</span>`).join('')
        : '<span class="muted">Ingen RSS-feeds er tildelt din bruger.</span>';
    }
    const list = document.querySelector('#rss-item-list');
    if (list) {
      list.innerHTML = rows.length
        ? rows.map(rssEntry).join('')
        : `<p class="muted">${selectedFeeds.length ? 'Ingen Politi Update-meldinger hentet endnu.' : 'En administrator kan tildele RSS-feeds til din bruger under Brugere.'}</p>`;
    }
    const refreshed = document.querySelector('#rss-ui-refreshed');
    if (refreshed) refreshed.textContent = `Opdateret ${new Date().toLocaleTimeString('da-DK', {hour: '2-digit', minute: '2-digit'})}`;
  }

  function groupedFeeds() {
    const groups = new Map(RSS_GROUPS.map((group) => [group.key, []]));
    for (const feed of activeFeeds()) groups.get(feedGroup(feed)).push(feed);
    for (const rows of groups.values()) rows.sort((a, b) => String(a.name).localeCompare(String(b.name), 'da-DK'));
    return groups;
  }

  function selectedFromCard(card) {
    return new Set(
      [...card.querySelectorAll('.rss-feed-checkbox:checked')].map((input) => Number(input.value))
    );
  }

  function setFromValues(values) {
    return new Set([...values].map((value) => Number(value)));
  }

  function sameSet(a, b) {
    if (a.size !== b.size) return false;
    for (const value of a) if (!b.has(value)) return false;
    return true;
  }

  function selectedFeedRows(selected) {
    return feedCatalog.filter((feed) => feed.active && selected.has(Number(feed.id)));
  }

  function selectedChips(feeds) {
    if (!feeds.length) return '<span class="rss-empty-state">Ingen feeds valgt endnu</span>';
    const visible = feeds.slice(0, 4);
    const hidden = feeds.length - visible.length;
    return `${visible.map((feed) => `<span class="rss-selected-chip">${rssEscape(feed.name)}</span>`).join('')}${hidden > 0 ? `<span class="rss-selected-chip rss-selected-more">+${hidden} flere</span>` : ''}`;
  }

  function groupMarkup(group, feeds, selected) {
    if (!feeds.length) return '';
    const chosen = feeds.filter((feed) => selected.has(Number(feed.id))).length;
    return `<details class="rss-feed-group" data-rss-group="${group.key}">
      <summary>
        <span><strong>${rssEscape(group.label)}</strong><small>${feeds.length} feed${feeds.length === 1 ? '' : 's'}</small></span>
        <span class="rss-group-selected" data-group-selected>${chosen} valgt</span>
      </summary>
      <div class="rss-feed-rows">
        ${feeds.map((feed) => `<label class="rss-feed-row" data-feed-search="${rssEscape(String(feed.name || '').toLocaleLowerCase('da-DK'))}">
          <input class="rss-feed-checkbox" type="checkbox" value="${feed.id}" ${selected.has(Number(feed.id)) ? 'checked' : ''}>
          <span class="rss-feed-checkmark" aria-hidden="true"></span>
          <span class="rss-feed-copy"><strong>${rssEscape(feed.name)}</strong><small>${feed.kind === 'politi' ? 'Officiel Politi Update' : 'Brugerdefineret RSS'}</small></span>
        </label>`).join('')}
      </div>
    </details>`;
  }

  function userRoutingCard(user, groups) {
    const selected = setFromValues(user.rss_feeds || []);
    const activeSelected = new Set([...selected].filter((id) => activeFeeds().some((feed) => Number(feed.id) === id)));
    const feedRows = selectedFeedRows(activeSelected);
    const initials = String(user.display_name || user.username || '?')
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0])
      .join('')
      .toLocaleUpperCase('da-DK');

    routingState.set(Number(user.id), {
      originalFeeds: new Set(activeSelected),
      originalPush: Boolean(user.rss_push_enabled),
    });

    return `<section class="rss-routing-card" data-rss-routing-user="${user.id}">
      <header class="rss-user-header">
        <div class="rss-user-identity">
          <span class="rss-user-avatar" aria-hidden="true">${rssEscape(initials)}</span>
          <span><strong>${rssEscape(user.display_name)}</strong><small>${rssEscape(user.username)} · ${rssEscape(user.role)}</small></span>
        </div>
        <span class="rss-selection-count" data-rss-selection-count>${feedRows.length} valgt</span>
      </header>

      <div class="rss-selected-block">
        <span class="rss-mini-label">Valgte feeds</span>
        <div class="rss-selected-chips" data-rss-selected-chips>${selectedChips(feedRows)}</div>
      </div>

      <div class="rss-notification-row">
        <div><strong>RSS Web Push</strong><small>Kun nye poster fra de valgte feeds sendes som push.</small></div>
        <label class="rss-switch">
          <input data-rss-push type="checkbox" ${user.rss_push_enabled ? 'checked' : ''} aria-label="RSS Web Push for ${rssEscape(user.display_name)}">
          <span class="rss-switch-track"><span></span></span>
        </label>
      </div>

      <div class="rss-routing-tools">
        <label class="rss-feed-search"><span aria-hidden="true">⌕</span><input type="search" data-rss-search placeholder="Søg i feeds" autocomplete="off"></label>
        <div class="rss-quick-actions" aria-label="Hurtigvalg">
          <button type="button" data-rss-quick="all">Vælg alle</button>
          <button type="button" data-rss-quick="none">Ryd alle</button>
          ${RSS_GROUPS.filter((group) => (groups.get(group.key) || []).length).map((group) => `<button type="button" data-rss-quick="${group.key}">${rssEscape(group.quick)}</button>`).join('')}
        </div>
      </div>

      <div class="rss-feed-groups">
        ${RSS_GROUPS.map((group) => groupMarkup(group, groups.get(group.key) || [], activeSelected)).join('')}
      </div>

      <div class="rss-routing-savebar" data-rss-savebar hidden>
        <span><strong>Ikke gemte ændringer</strong><small data-rss-change-summary>Routing er ændret.</small></span>
        <div class="actions"><button type="button" data-rss-reset>Fortryd</button><button type="button" class="primary" data-rss-routing-save="${user.id}">Gem ændringer</button></div>
      </div>
    </section>`;
  }

  function updateGroupCounts(card) {
    card.querySelectorAll('[data-rss-group]').forEach((group) => {
      const total = group.querySelectorAll('.rss-feed-checkbox').length;
      const selected = group.querySelectorAll('.rss-feed-checkbox:checked').length;
      const label = group.querySelector('[data-group-selected]');
      if (label) label.textContent = `${selected}/${total} valgt`;
    });
  }

  function updateRoutingCard(card) {
    const userId = Number(card.dataset.rssRoutingUser);
    const state = routingState.get(userId);
    if (!state) return;
    const selected = selectedFromCard(card);
    const pushEnabled = Boolean(card.querySelector('[data-rss-push]')?.checked);
    const feeds = selectedFeedRows(selected);
    const count = card.querySelector('[data-rss-selection-count]');
    const chips = card.querySelector('[data-rss-selected-chips]');
    if (count) count.textContent = `${feeds.length} valgt`;
    if (chips) chips.innerHTML = selectedChips(feeds);
    updateGroupCounts(card);

    const feedChanged = !sameSet(selected, state.originalFeeds);
    const pushChanged = pushEnabled !== state.originalPush;
    const savebar = card.querySelector('[data-rss-savebar]');
    if (savebar) savebar.hidden = !(feedChanged || pushChanged);
    const summary = card.querySelector('[data-rss-change-summary]');
    if (summary) {
      const changes = [];
      if (feedChanged) changes.push('feedvalg');
      if (pushChanged) changes.push('Web Push');
      summary.textContent = changes.length ? `${changes.join(' og ')} er ændret.` : 'Routing er gemt.';
    }
  }

  function applyQuickChoice(card, choice) {
    const checkboxes = [...card.querySelectorAll('.rss-feed-checkbox')];
    if (choice === 'all' || choice === 'none') {
      checkboxes.forEach((input) => { input.checked = choice === 'all'; });
    } else {
      const allowed = new Set(
        activeFeeds().filter((feed) => feedGroup(feed) === choice).map((feed) => Number(feed.id))
      );
      checkboxes.forEach((input) => { input.checked = allowed.has(Number(input.value)); });
    }
    updateRoutingCard(card);
  }

  function resetRoutingCard(card) {
    const state = routingState.get(Number(card.dataset.rssRoutingUser));
    if (!state) return;
    card.querySelectorAll('.rss-feed-checkbox').forEach((input) => {
      input.checked = state.originalFeeds.has(Number(input.value));
    });
    const push = card.querySelector('[data-rss-push]');
    if (push) push.checked = state.originalPush;
    const search = card.querySelector('[data-rss-search]');
    if (search) search.value = '';
    card.querySelectorAll('.rss-feed-row').forEach((row) => { row.hidden = false; });
    card.querySelectorAll('.rss-feed-group').forEach((group) => { group.hidden = false; });
    updateRoutingCard(card);
  }

  function filterRoutingFeeds(card, query) {
    const needle = String(query || '').trim().toLocaleLowerCase('da-DK');
    card.querySelectorAll('.rss-feed-group').forEach((group) => {
      let visible = 0;
      group.querySelectorAll('.rss-feed-row').forEach((row) => {
        const show = !needle || String(row.dataset.feedSearch || '').includes(needle);
        row.hidden = !show;
        if (show) visible += 1;
      });
      group.hidden = visible === 0;
      if (needle && visible) group.open = true;
    });
  }

  function bindRoutingCard(card) {
    card.querySelectorAll('.rss-feed-checkbox, [data-rss-push]').forEach((input) => {
      input.addEventListener('change', () => updateRoutingCard(card));
    });
    card.querySelectorAll('[data-rss-quick]').forEach((button) => {
      button.addEventListener('click', () => applyQuickChoice(card, button.dataset.rssQuick));
    });
    card.querySelector('[data-rss-reset]')?.addEventListener('click', () => resetRoutingCard(card));
    card.querySelector('[data-rss-search]')?.addEventListener('input', (event) => filterRoutingFeeds(card, event.target.value));

    const saveButton = card.querySelector('[data-rss-routing-save]');
    saveButton?.addEventListener('click', async () => {
      const userId = Number(saveButton.dataset.rssRoutingSave);
      const selected = [...selectedFromCard(card)];
      const pushEnabled = Boolean(card.querySelector('[data-rss-push]')?.checked);
      saveButton.disabled = true;
      const previousText = saveButton.textContent;
      try {
        await api(`/api/users/${userId}/rss`, {
          method: 'PATCH',
          body: JSON.stringify({feeds: selected, push_enabled: pushEnabled}),
        });
        routingState.set(userId, {originalFeeds: new Set(selected), originalPush: pushEnabled});
        updateRoutingCard(card);
        saveButton.textContent = 'Gemt ✓';
        setTimeout(() => { saveButton.textContent = previousText; }, 1400);
      } catch (error) {
        alert(error.message);
      } finally {
        saveButton.disabled = false;
      }
    });
  }

  async function refreshUserRssRouting() {
    if (!isAdmin) return;
    await loadFeedCatalog(true);
    const users = await api('/api/users');
    const target = document.querySelector('#user-rss-routing-list');
    if (!target) return;
    routingState.clear();
    const groups = groupedFeeds();
    target.innerHTML = users.length
      ? `<div class="rss-routing-users">${users.map((user) => userRoutingCard(user, groups)).join('')}</div>`
      : '<p class="muted">Ingen brugere.</p>';
    target.querySelectorAll('[data-rss-routing-user]').forEach(bindRoutingCard);
  }

  function feedStatus(feed) {
    if (!feed.active) return 'Deaktiveret';
    if (feed.last_error) return `Fejl: ${feed.last_error}`;
    if (feed.last_success_at) return `Senest hentet ${rssDate(feed.last_success_at)}`;
    if (feed.subscriber_count) return 'Afventer første hentning';
    return 'Ingen abonnenter · hentes ikke endnu';
  }

  async function refreshAdminFeeds() {
    if (!isAdmin) return;
    await loadFeedCatalog(true);
    const target = document.querySelector('#rss-admin-feed-list');
    if (!target) return;
    target.innerHTML = feedCatalog.map((feed) => `
      <div class="command-row" data-rss-feed-id="${feed.id}">
        <div><strong>${rssEscape(feed.name)}</strong><small>${rssEscape(feed.kind === 'politi' ? 'Officiel Politi Update' : 'Brugerdefineret RSS')} · ${feed.subscriber_count || 0} bruger(e) · ${rssEscape(feedStatus(feed))}</small><small>${rssEscape(feed.url)}</small></div>
        <div class="actions"><span class="status-badge ${feed.active ? 'active' : 'inactive'}">${feed.active ? 'Aktiv' : 'Fra'}</span><button data-rss-feed-toggle="${feed.id}" data-active="${feed.active ? '1' : '0'}">${feed.active ? 'Deaktivér' : 'Aktivér'}</button></div>
      </div>`).join('') || '<p class="muted">Ingen RSS-feeds.</p>';

    target.querySelectorAll('[data-rss-feed-toggle]').forEach((button) => button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await api(`/api/rss/feeds/${button.dataset.rssFeedToggle}`, {
          method: 'PATCH',
          body: JSON.stringify({active: button.dataset.active !== '1'}),
        });
        await refreshAdminFeeds();
        await refreshUserRssRouting();
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
      }
    }));
  }

  document.querySelector('[data-tab="politi"]')?.addEventListener('click', () => {
    refreshPolitiUpdates().catch((error) => console.error(error));
    if (isAdmin) refreshAdminFeeds().catch((error) => console.error(error));
  });

  document.querySelector('[data-tab="users"]')?.addEventListener('click', () => {
    if (isAdmin) setTimeout(() => refreshUserRssRouting().catch(console.error), 0);
  });

  document.querySelector('#rss-refresh')?.addEventListener('click', () => refreshPolitiUpdates().catch((error) => alert(error.message)));
  document.querySelector('#refresh-user-rss-routing')?.addEventListener('click', () => refreshUserRssRouting().catch((error) => alert(error.message)));
  document.querySelector('#rss-fetch-now')?.addEventListener('click', async () => {
    try {
      await api('/api/rss/refresh', {method: 'POST', body: '{}'});
      const button = document.querySelector('#rss-fetch-now');
      button.textContent = 'Hentning startet ✓';
      setTimeout(() => { button.textContent = 'Hent nu'; }, 1500);
    } catch (error) { alert(error.message); }
  });

  document.querySelector('#rss-feed-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api('/api/rss/feeds', {method: 'POST', body: JSON.stringify(payload)});
      form.reset();
      await refreshAdminFeeds();
      await refreshUserRssRouting();
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
    }
  });

  if (window.location.hash === '#politi') {
    setTimeout(() => document.querySelector('[data-tab="politi"]')?.click(), 0);
  }
  setInterval(() => {
    if (document.querySelector('#politi')?.classList.contains('active')) refreshPolitiUpdates().catch(console.error);
  }, 60000);
})();