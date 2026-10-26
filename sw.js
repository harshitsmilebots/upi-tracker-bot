// Fleet Ledger service worker — push notifications only (no offline caching,
// so the dashboard is always fetched fresh).
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let title = 'Fleet Ledger', body = '';
  try {
    const txt = event.data ? event.data.text() : '';
    // first line = title, rest = body
    const lines = txt.split('\n').filter(l => l.trim().length);
    if (lines.length) { title = lines[0].slice(0, 80); body = lines.slice(1).join('\n').slice(0, 300); }
    if (!body) body = title, title = 'Fleet Ledger';
  } catch (e) {}
  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: 'fleet-ledger',
    renotify: true
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(cl => {
      for (const c of cl) { if ('focus' in c) return c.focus(); }
      if (self.clients.openWindow) return self.clients.openWindow('/');
    })
  );
});
