self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {}; }
  const title = data.title || 'Pageralarm';
  const options = {
    body: data.body || 'Ny pageralarm',
    icon: '/static/icon.svg',
    badge: '/static/icon.svg',
    tag: data.message_id ? `pager-${data.message_id}` : 'pager-alarm',
    renotify: true,
    data: {url: data.url || '/'},
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || '/';
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const client of windows) {
      if ('focus' in client) {
        await client.navigate(targetUrl);
        return client.focus();
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
  })());
});
