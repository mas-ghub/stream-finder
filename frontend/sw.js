/* Stream Finder service worker.
   Caches the app shell (this file's origin) so it installs and launches offline.
   Live searches / ratings / trailers always go to the network (they need fresh
   TMDB/RT data); this just keeps the UI itself available and fast. */
// Bump on every app change so installed PWAs pick up the new index.html (a stale
// cached index is the usual "my phone still shows the old build" cause).
const CACHE = 'sf-shell-v26';
// Relative paths so the shell also caches when deployed under a repo subdir
// (GitHub Pages: /stream-finder/), while still working at the origin root.
const SHELL = ['./', './manifest.webmanifest', './icon.svg', './sw.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // never intercept cross-origin (TMDB, RT, CDN)
  // Never touch the API: results must be live, and a search can take 20s+ — iOS kills a
  // service worker that holds a fetch that long, which surfaced as a spurious "can't reach".
  if (url.pathname.includes('/api/')) return;
  // Shell assets by basename (works at the origin root and under a repo subdir).
  const isShell = ['manifest.webmanifest', 'icon.svg', 'sw.js'].includes(url.pathname.split('/').pop());

  // Navigations / the shell: network-first, fall back to cache when offline.
  if (req.mode === 'navigate' || isShell) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() =>
          caches.match(req).then((hit) => hit || caches.match('/') || Response.error())
        )
    );
    return;
  }

  // Same-origin assets: cache-first (fast + works offline after first visit).
  e.respondWith(
    caches.match(req).then((hit) =>
      hit ||
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      })
    )
  );
});
