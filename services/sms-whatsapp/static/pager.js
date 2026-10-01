(() => {
  const setClock = () => {
    const label = document.getElementById('refresh-label');
    if (label) label.textContent = 'Opdateret ' + new Date().toLocaleTimeString('da-DK', {timeZone: 'Europe/Copenhagen', hour: '2-digit', minute: '2-digit'});
  };
  setClock();
  if (document.getElementById('single-test-list')) {
    const names={pending:'I kø',running:'Afsender',sent:'Kvitteret af OpenWA',failed:'Fejlet',cancelled:'Annulleret',uncertain:'Uafklaret'};
    setInterval(async()=>{
      if(document.hidden)return;
      try{
        const response=await fetch('/api/enkelt-test/status',{credentials:'same-origin',signal:AbortSignal.timeout(10000)});
        if(!response.ok || !response.headers.get('content-type')?.includes('application/json'))throw Error();
        const data=await response.json();
        for(const test of data.tests){
          const row=document.querySelector('[data-test-id="'+test.id+'"]');if(!row)continue;
          const status=row.querySelector('[data-test-status]');status.textContent=names[test.status]||test.status;status.className='badge '+(test.status==='sent'?'green':'amber');
          row.querySelector('[data-test-duration]').textContent=test.elapsed_ms==null?'—':(test.elapsed_ms/1000).toLocaleString('da-DK',{maximumFractionDigits:2})+' sek.';
          row.querySelector('[data-test-detail]').textContent=test.error||test.message_id||'Afventer kvittering';
        }
        document.getElementById('single-test-refresh').textContent='Resultater opdateres automatisk hvert femte sekund.';
      }catch(_){document.getElementById('single-test-refresh').textContent='Status kunne ikke hentes. Genindlæs siden; testen genudsendes ikke.';}
    },5000);
  }
  if (!document.getElementById('overall-label')) return;
  const labels = {online:'Online', ready:'Forbundet', offline:'Offline', missing:'Session mangler', unknown:'Ukendt', connecting:'Forbinder', initializing:'Starter', stale:'Status forældet', degraded:'Kræver opmærksomhed'};
  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/dashboard/status', {credentials: 'same-origin', headers:{Accept:'application/json'}, signal:AbortSignal.timeout(10000)});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Status utilgængelig');
      const data = await response.json();
      for (const [id, state, ready] of [['internet-state',data.internet.state,'online'], ['modem-state',data.modem.state,'online'], ['wa-state',data.openwa.state,'ready']]) {
        const node=document.getElementById(id); node.textContent=labels[state] || state; node.className='badge '+(state === ready ? 'green':'amber');
      }
      document.getElementById('internet-detail').textContent=data.internet.detail;
      const timeLabel=value=>value ? new Date(value).toLocaleString('da-DK',{timeZone:'Europe/Copenhagen',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
      if(data.ops){
        document.getElementById('last-sms').textContent=timeLabel(data.ops.last_sms);
        document.getElementById('last-sent').textContent=timeLabel(data.ops.last_sent);
        document.getElementById('active-transport').textContent=data.modem.transport==='cudy'?'LT300 · LAN':data.modem.transport==='usb'?'USB · backup under indkøring':'Ukendt';
        const held=document.getElementById('held-warning');held.hidden=!data.ops.held;held.querySelector('span').textContent=data.ops.held+' gamle leveringer afventer vurdering.';
        const traffic=document.getElementById('traffic-warning');traffic.hidden=!(data.ops.traffic_warning||data.ops.failure_warning);traffic.querySelector('span').textContent='Usædvanlig aktivitet: '+data.ops.traffic_count+' SMS’er på fem minutter.'+(data.ops.failure_warning?' Gentagne leveringsfejl.':'');
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

const fullscreenButton = document.querySelector('[data-fullscreen]');
if (document.body.dataset.readonly === 'true') {
  for (const form of document.querySelectorAll('form[method="post"],form[method="POST"]')) {
    for (const control of form.querySelectorAll('input,select,textarea,button')) control.disabled = true;
  }
}
if (fullscreenButton) fullscreenButton.addEventListener('click', async () => {
  try { if (!document.fullscreenElement) await document.documentElement.requestFullscreen(); else await document.exitFullscreen(); }
  catch { fullscreenButton.textContent = 'Fuld skærm er ikke tilgængelig'; }
});
if (document.querySelector('[data-display-refresh]')) setTimeout(() => window.location.reload(), 30000);

const menuToggle=document.getElementById('menu-toggle');
if(menuToggle)menuToggle.addEventListener('click',()=>{
 const sidebar=document.querySelector('.pager-sidebar');const open=sidebar.classList.toggle('menu-open');menuToggle.setAttribute('aria-expanded',String(open));menuToggle.textContent=open?'Luk menu ×':'Menu ☰';
});
