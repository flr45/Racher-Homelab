(() => {
  const isAdmin = document.body.dataset.admin === '1';
  let feedCatalog = [];

  const rssEscape = (value) => escapeHtml(value ?? '');
  const rssDate = (value) => formatDate(value || '');

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

  function feedChecks(selected) {
    return feedCatalog.filter((feed) => feed.active).map((feed) =>
      `<label class="station-pill"><input type="checkbox" value="${feed.id}" ${selected.has(feed.id) ? 'checked' : ''}> ${rssEscape(feed.name)}</label>`
    ).join('') || '<span class="muted">Ingen aktive RSS-feeds.</span>';
  }

  async function refreshUserRssRouting() {
    if (!isAdmin) return;
    await loadFeedCatalog(true);
    const users = await api('/api/users');
    const target = document.querySelector('#user-rss-routing-list');
    if (!target) return;
    target.innerHTML = users.map((user) => {
      const selected = new Set(user.rss_feeds || []);
      return `<div class="routing-user" data-rss-routing-user="${user.id}">
        <div><strong>${rssEscape(user.display_name)}</strong><small>${rssEscape(user.username)} · ${rssEscape(user.role)}</small></div>
        <div class="station-pills rss-feed-pills">${feedChecks(selected)}</div>
        <label class="station-pill all-messages-toggle"><input data-rss-push type="checkbox" ${user.rss_push_enabled ? 'checked' : ''}> <strong>RSS Web Push</strong> · kun nye poster fra valgte feeds</label>
        <button data-rss-routing-save="${user.id}" class="primary">Gem RSS-routing</button>
      </div>`;
    }).join('') || '<p class="muted">Ingen brugere.</p>';

    target.querySelectorAll('[data-rss-routing-save]').forEach((button) => button.addEventListener('click', async () => {
      const row = button.closest('[data-rss-routing-user]');
      const selected = [...row.querySelectorAll('.rss-feed-pills input[type="checkbox"]:checked')].map((item) => Number(item.value));
      const pushEnabled = row.querySelector('[data-rss-push]').checked;
      button.disabled = true;
      try {
        await api(`/api/users/${button.dataset.rssRoutingSave}/rss`, {
          method: 'PATCH',
          body: JSON.stringify({feeds: selected, push_enabled: pushEnabled}),
        });
        button.textContent = 'Gemt ✓';
        setTimeout(() => { button.textContent = 'Gem RSS-routing'; }, 1200);
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
      }
    }));
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