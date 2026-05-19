/* RoadWalk service worker
   ----------------------------------------------------------------
   Gives the app an offline-capable shell. The HTML, scripts, styles
   and any project data fetched while online are stored in the Cache
   API and served back when there is no network. Map tiles and other
   cross-origin requests are left to the network (not cached here).

   Updating: bump CACHE_VERSION whenever the deployed app changes so
   the previous cache is purged on the user's next visit.
   ---------------------------------------------------------------- */
'use strict';

var CACHE_VERSION = 'roadwalk-v12';

// Pre-cached on install so the app opens even on a fully cold start.
var PRECACHE = ['./', './index.html', './roadwalk.html'];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(CACHE_VERSION).then(function (cache) {
      // Cache each item independently — one missing file must not abort install.
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(url).catch(function () {});
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        if (k !== CACHE_VERSION) return caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;

  var url;
  try { url = new URL(req.url); } catch (_) { return; }

  // Only manage our own origin — map tiles / fonts / CDNs go straight to network.
  if (url.origin !== self.location.origin) return;

  // Stale-while-revalidate: serve the cached copy instantly, refresh it in the
  // background; fall back to the network (then the cached app shell) when the
  // request is not yet cached.
  e.respondWith(
    caches.match(req).then(function (cached) {
      var network = fetch(req).then(function (res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(CACHE_VERSION).then(function (c) { c.put(req, copy); });
        }
        return res;
      }).catch(function () {
        // Offline and uncached — for page navigations, fall back to the app shell.
        if (req.mode === 'navigate') return caches.match('./roadwalk.html');
        return cached;
      });
      return cached || network;
    })
  );
});
