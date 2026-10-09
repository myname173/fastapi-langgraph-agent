/* sw.js — minimal service worker for PWA installability + offline shell.
 *
 * Strategy:
 *   - Precache the app shell on install (index.html + assets).
 *   - Same-origin GET static assets: cache-first, fall back to network.
 *   - API calls (/api/*) and POST/SSE: always network (never cache).
 * Bump CACHE_VER when assets change to invalidate stale caches.
 */
var CACHE_VER = "ai-agent-v4";
var SHELL = [
  "/",
  "/index.html",
  "/css/styles.css",
  "/js/api.js",
  "/js/auth.js",
  "/js/investor.js",
  "/js/chat-history.js",
  "/js/sidebar.js",
  "/js/highlight.js",
  "/js/coder.js",
  "/js/app.js",
  "/vendor/md.js",
  "/manifest.json",
  "/icon.svg",
  "/icon-192.png",
  "/icon-512.png",
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches.open(CACHE_VER).then(function (c) { return c.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE_VER; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return; // never intercept POST/SSE
  // Navigation requests (history-API routes like /investor): network-first,
  // cache the result under a FIXED key "/index.html" (never the request URL,
  // which would create per-route stale copies). Offline -> cached index.html.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req).then(function (resp) {
        if (resp && resp.status === 200 && resp.type === "basic") {
          var copy = resp.clone();
          caches.open(CACHE_VER).then(function (c) { c.put("/index.html", copy); });
        }
        return resp;
      }).catch(function () { return caches.match("/index.html"); })
    );
    return;
  }
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // cross-origin
  if (url.pathname.indexOf("/api/") === 0 || url.pathname.indexOf("/docs") === 0 ||
      url.pathname.indexOf("/workspace") === 0) return; // dynamic, skip
  // Code assets (JS/CSS/HTML) use NETWORK-FIRST so frontend updates take
  // effect on the very first reload. This aligns with the server's
  // Cache-Control: no-cache on static files — a cache-first SW would
  // intercept BEFORE the HTTP cache and serve stale JS indefinitely, which
  // is exactly the bug where md.js/auth.js updates didn't appear until the
  // user manually cleared the SW cache. Images/fonts/icons stay cache-first
  // (rarely change, benefit from instant offline load).
  var isCodeAsset = /\.(js|css|html)(\?|$)/.test(url.pathname);
  if (isCodeAsset) {
    e.respondWith(
      fetch(req).then(function (resp) {
        if (resp && resp.status === 200) {
          var copy = resp.clone();
          caches.open(CACHE_VER).then(function (c) { c.put(req, copy); });
        }
        return resp;
      }).catch(function () { return caches.match(req); })
    );
    return;
  }
  e.respondWith(
    caches.match(req).then(function (cached) {
      return cached || fetch(req).then(function (resp) {
        if (resp && resp.status === 200) {
          var copy = resp.clone();
          caches.open(CACHE_VER).then(function (c) { c.put(req, copy); });
        }
        return resp;
      }).catch(function () { return caches.match("/index.html"); });
    })
  );
});
