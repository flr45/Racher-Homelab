(() => {
  if (document.body.dataset.admin !== '1') return;

  const byId = (id) => document.getElementById(id);
  const setText = (id, value) => {
    const node = byId(id);
    if (node) node.textContent = value || '—';
  };

  function hideLegacyAudioReadiness() {
    document.querySelectorAll('.readiness-row').forEach((row) => {
      const title = row.querySelector('strong');
      if (title && title.textContent.trim() === 'USB lydinput') row.hidden = true;
    });
  }

  function render(runtime) {
    const connected = runtime.fsk_usb_connected === '1';
    const inUse = runtime.fsk_usb_pdl_in_use === '1';
    const configured = runtime.fsk_usb_decode_mode && runtime.fsk_usb_decode_mode !== '0';
    const state = byId('fsk-state');
    if (state) {
      state.textContent = connected ? (inUse ? 'AKTIV' : 'TILSLUTTET') : 'AFVENTER';
      state.className = `status-badge ${connected ? 'active' : 'inactive'}`;
    }

    setText('fsk-device', runtime.fsk_usb_device || 'Ikke fundet');
    setText('fsk-summary', runtime.fsk_usb_summary || 'FSK-USB ikke tilsluttet');
    setText('fsk-serial-config', runtime.fsk_usb_serial_config || '19200 8N1');
    setText('fsk-driver', runtime.fsk_usb_driver || 'FTDI / Linux USB-serial');
    setText('fsk-pdl-state', inUse ? 'PDL læser enheden' : configured ? 'PDL konfigureret · afventer input' : 'PDL RS232 ikke aktiveret');
    setText('fsk-input-mode', runtime.fsk_usb_input_mode || 'fsk-usb');

    const serial = runtime.fsk_usb_serial || '';
    const real = runtime.fsk_usb_real_device || '';
    const details = [serial && `Serial ${serial}`, real && real !== runtime.fsk_usb_device ? real : '', connected ? `${runtime.fsk_usb_devices || '1'} seriel enhed fundet` : 'Tilslut FSK-USB til Pi'].filter(Boolean);
    setText('fsk-meta', details.join(' · '));
    hideLegacyAudioReadiness();
  }

  async function refresh() {
    const panel = byId('system');
    if (!panel || !panel.classList.contains('active')) return;
    try {
      const response = await fetch('/api/status', {credentials: 'same-origin'});
      if (!response.ok) return;
      const data = await response.json();
      render(data.runtime || {});
    } catch (_) {
      // The main app owns login/error handling; this card is supplemental diagnostics.
    }
  }

  const readiness = byId('readiness-list');
  if (readiness) new MutationObserver(hideLegacyAudioReadiness).observe(readiness, {childList: true, subtree: true});

  document.querySelector('[data-tab="system"]')?.addEventListener('click', () => setTimeout(refresh, 0));
  setInterval(refresh, 10000);
  setTimeout(refresh, 0);
})();
