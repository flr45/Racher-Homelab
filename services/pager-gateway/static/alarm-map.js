(() => {
  const STREET_SUFFIX = '(?:vej|gade|stræde|allé|alle|boulevard|torv|plads|vænge|vænget|parken|bakken|engen|toften|haven|kær|mose|stien|sti|gyde|passage|kaj|bro|brygge|lunden|lund|hovedvej|hovedgade|landevej|ringvej|ringgade)';
  const CITY = '(?:[A-ZÆØÅ][\\p{L}.\'’\\-]*\\s*){1,3}';
  const ADDRESS_WITH_PREFIX_RE = new RegExp(
    `\\b((?:[A-ZÆØÅ][\\p{L}.\'’\\-]*\\.?\\s+){0,3}[A-ZÆØÅ][\\p{L}.\'’\\-]*${STREET_SUFFIX}\\s+\\d{1,4}[A-Za-z]?(?:\\s*(?:,|·|-)?\\s*\\d{4}\\s+${CITY})?)`,
    'u'
  );
  const ADDRESS_FALLBACK_RE = new RegExp(
    `\\b([\\p{L}][\\p{L}.\'’\\-]*${STREET_SUFFIX}\\s+\\d{1,4}[A-Za-z]?(?:\\s*(?:,|·|-)?\\s*\\d{4}\\s+[\\p{L}][\\p{L}.\'’\\-]*(?:\\s+[\\p{L}][\\p{L}.\'’\\-]*){0,2})?)`,
    'iu'
  );
  const LEADING_NOISE_RE = /^(?:(?:AUT|ALARM|ISL|KA|MØ|VSBV|ØF|VCT|M\d+|V\d+|R\d+|S\d+)\s+)/i;

  function cleanAddress(value) {
    let address = String(value || '').replace(/[.,;:]+$/, '').trim();
    let previous = '';
    while (address !== previous) {
      previous = address;
      address = address.replace(LEADING_NOISE_RE, '').trim();
    }
    return address;
  }

  function detectDanishAddress(text) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    if (!value) return null;
    const match = ADDRESS_WITH_PREFIX_RE.exec(value) || ADDRESS_FALLBACK_RE.exec(value);
    if (!match?.[1]) return null;
    const address = cleanAddress(match[1]);
    return address.length >= 5 ? address : null;
  }

  function mapsUrl(address) {
    const query = /\b\d{4}\b/.test(address) ? address : `${address}, Danmark`;
    return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
  }

  function mapActions(address) {
    const actions = document.createElement('div');
    actions.className = 'actions alarm-map-actions';
    actions.dataset.alarmMapActions = '1';

    const label = document.createElement('span');
    label.className = 'muted';
    label.textContent = `Adresse: ${address}`;

    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Åbn i kort';
    button.title = `Åbn ${address} i Google Maps`;
    button.addEventListener('click', () => {
      window.open(mapsUrl(address), '_blank', 'noopener,noreferrer');
    });

    actions.append(label, button);
    return actions;
  }

  function decorateLatest() {
    const message = document.querySelector('#latest-message');
    const meta = document.querySelector('#latest-meta');
    if (!message || !meta) return;

    const card = message.closest('.latest-card');
    const existing = card?.querySelector('[data-alarm-map-actions]');
    const address = detectDanishAddress(message.textContent);
    if (!address) {
      existing?.remove();
      return;
    }
    if (existing?.dataset.address === address) return;
    existing?.remove();
    const actions = mapActions(address);
    actions.dataset.address = address;
    meta.insertAdjacentElement('afterend', actions);
  }

  function decorateRows() {
    document.querySelectorAll('.history-row').forEach((row) => {
      const message = row.querySelector('p');
      const content = row.children[1];
      if (!message || !content) return;
      const existing = content.querySelector('[data-alarm-map-actions]');
      const address = detectDanishAddress(message.textContent);
      if (!address) {
        existing?.remove();
        return;
      }
      if (existing?.dataset.address === address) return;
      existing?.remove();
      const actions = mapActions(address);
      actions.dataset.address = address;
      content.append(actions);
    });
  }

  function decorate() {
    decorateLatest();
    decorateRows();
  }

  let scheduled = false;
  function scheduleDecorate() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      decorate();
    });
  }

  const observer = new MutationObserver(scheduleDecorate);
  const targets = [
    document.querySelector('#latest-message'),
    document.querySelector('#alarm-list'),
    document.querySelector('#history-list'),
  ].filter(Boolean);
  targets.forEach((target) => observer.observe(target, {childList: true, subtree: true, characterData: true}));
  decorate();
})();
