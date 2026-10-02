/* Stream Finder service worker.
   Caches the app shell (this file's origin) so it installs and launches offline.
   Live searches / ratings / trailers always go to the network (they need fresh
   TMDB/RT data); this just keeps the UI itself available and fast. */
// Bump on every app change so installed PWAs pick up the new index.html (a stale
// cached index is the usual "my phone still shows the old build" cause).
const CACHE = 'sf-shell-v69';
// Relative paths so the shell also caches when deployed under a repo subdir
// (GitHub Pages: /stream-finder/), while still working at the origin root.
// NB: sw.js is intentionally NOT pre-cached here. It must always come from the
// network at update-check time, or a stale cached sw.js stops the update loop and
// an installed app stays on an old build forever.
const SHELL = ['./', './manifest.webmanifest', './icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
});

// Let the page pull a waiting new worker in immediately (it posts SKIP_WAITING
// when it detects one). This is what makes an already-stale installed PWA heal
// itself on the next load instead of waiting for the next navigation.
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
    // clients.claim() makes this newly-activated worker immediately control all
    // already-open clients (incl. the installed PWA) instead of waiting for the
    // next navigation. Combined with the page's one-time reload, an installed
    // PWA now picks up a new shell on its next launch instead of staying on the
    // old cached index.html forever (the iOS "Safari is new but the PWA is stale").
    .then(() => self.clients.claim())
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
    // sw.js is deliberately NOT in this list — it's served network-only (cache as a
    // backup, never from cache) so a deployed update is always detected.
    const isShell = ['manifest.webmanifest', 'icon.svg'].includes(url.pathname.split('/').pop());
    const isSW = url.pathname.split('/').pop() === 'sw.js';

    // Navigations / the shell: network-first, fall back to cache when offline.
    if (req.mode === 'navigate' || isShell || isSW) {
      e.respondWith(
        fetch(req)
          .then((res) => {
            // Refresh the cache copy in the background, but the request already
            // resolved from the network. (For sw.js this keeps a usable backup for
            // the rare offline update-check without ever serving it from cache.)
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
            return res;
          })
          .catch(() =>
            // Only allow a cached fallback for the shell; a failed sw.js fetch must
            // not be answered from an old cached copy (it would keep the stale worker).
            (isSW ? Promise.reject() : caches.match(req).then((hit) => hit || caches.match('/') || Response.error()))
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
