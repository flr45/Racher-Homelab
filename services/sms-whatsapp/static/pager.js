(() => {
  const setClock = () => {
    const label = document.getElementById('refresh-label');
    if (label) label.textContent = 'Opdateret ' + new Date().toLocaleTimeString('da-DK', {timeZone: 'Europe/Copenhagen', hour: '2-digit', minute: '2-digit'});
  };
  setClock();
  if (!document.getElementById('overall-label')) return;
  const labels = {online:'Online', ready:'Forbundet', offline:'Offline', missing:'Session mangler', unknown:'Ukendt', connecting:'Forbinder', initializing:'Starter', stale:'Status forældet', degraded:'Kræver opmærksomhed'};
  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/dashboard/status', {credentials: 'same-origin', headers:{Accept:'application/json'}, signal:AbortSignal.timeout(10000)});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Status utilgængelig');
      const data = await response.json();
      for (const [id, state, ready] of [['modem-state',data.modem.state,'online'], ['wa-state',data.openwa.state,'ready']]) {
        const node=document.getElementById(id); node.textContent=labels[state] || state; node.className='badge '+(state === ready ? 'green':'amber');
      }
      document.getElementById('queue-count').textContent=data.queue.active;
      document.getElementById('queue-note').textContent=data.queue.active ? 'nyt forsøg afventer':'ingen ventende leveringer';
      const banner=document.querySelector('.system-banner'); banner.classList.toggle('healthy',data.good); banner.classList.toggle('attention',!data.good);
      document.getElementById('overall-label').textContent=data.good ? 'Forbindelserne er klar':'Kontrollér forbindelser og leveringer';
      banner.querySelector('.banner-icon').textContent=data.good ? '✓':'!';
      setClock();
    } catch (_) {
      document.getElementById('refresh-label').textContent='Status kunne ikke opdateres';
      const banner=document.querySelector('.system-banner'); banner.classList.add('attention'); banner.classList.remove('healthy');
      document.getElementById('overall-label').textContent='Statusforbindelsen er afbrudt';
    }
  }
  setInterval(refresh, 30000);
})();
