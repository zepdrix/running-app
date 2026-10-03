/*
 * Service worker: makes Running Log installable and usable when the Mac is unreachable.
 *
 * - Pages, app code and styles: network first (always fresh), cached copy when offline.
 * - Read-only API data (dashboard, status, plan, coach history): network first with a
 *   short timeout, falling back to the last copy. Those responses get an
 *   X-Offline-Cached-At header so the page can say it's showing saved data.
 * - Libraries and fonts from CDNs: cache first (their URLs are versioned).
 * - Anything that isn't a GET (coach messages, saves, sync, login) is never cached.
 */
const VERSION = "v5"; // bump to drop old cached files (e.g. the old icon)
const SHELL = `runapp-shell-${VERSION}`;
const DATA = `runapp-data-${VERSION}`;
const LIBS = `runapp-libs-${VERSION}`;
const SHELL_FILES = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/icons/icon-192.png",
  "/static/icons/apple-touch-icon.png",
  "/static/icons/icon-maskable.png",
  "/manifest.webmanifest",
];
const CACHED_API = ["/api/dashboard", "/api/status", "/api/plan", "/api/coach", "/api/coach/notes", "/api/races", "/api/workouts", "/api/form"];
const API_TIMEOUT_MS = 5000;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).catch(() => {}) // logged out: shell fills in later
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  const keep = new Set([SHELL, DATA, LIBS]);
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("runapp-") && !keep.has(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
  });
}

// Only keep real content: not login redirects, errors or opaque responses.
const cacheable = (res) => res && res.ok && res.type === "basic" && !res.redirected;

async function networkFirst(request, cacheName, { timeout, markOffline } = {}) {
  const cache = await caches.open(cacheName);
  try {
    const fetched = fetch(request);
    const res = await (timeout ? withTimeout(fetched, timeout) : fetched);
    if (cacheable(res)) {
      const copy = res.clone();
      const headers = new Headers(copy.headers);
      headers.set("X-Offline-Cached-At", new Date().toISOString());
      // The saved body is already decompressed, so these no longer describe it.
      headers.delete("Content-Encoding");
      headers.delete("Content-Length");
      cache.put(request, new Response(await copy.blob(), { status: copy.status, headers }));
    }
    return res;
  } catch (err) {
    // Exact match first; otherwise the same endpoint with other query parameters (e.g. another week range).
    const cached = (await cache.match(request)) || (await cache.match(request, { ignoreSearch: true }));
    if (!cached) throw err;
    if (!markOffline) return cached;
    const headers = new Headers(cached.headers);
    headers.set("X-Offline", "1");
    return new Response(cached.body, { status: cached.status, headers });
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(LIBS);
  const cached = await cache.match(request);
  if (cached) return cached;
  const res = await fetch(request);
  if (res && (res.ok || res.type === "opaque")) cache.put(request, res.clone());
  return res;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return; // never touch writes
  const url = new URL(request.url);

  if (url.origin !== self.location.origin) {
    if (/^(cdn\.jsdelivr\.net|fonts\.googleapis\.com|fonts\.gstatic\.com)$/.test(url.hostname)) {
      event.respondWith(cacheFirst(request));
    }
    return;
  }
  if (url.pathname === "/sw.js" || url.pathname === "/login" || url.pathname.startsWith("/api/login")) return;

  if (CACHED_API.includes(url.pathname)) {
    event.respondWith(networkFirst(request, DATA, { timeout: API_TIMEOUT_MS, markOffline: true }));
    return;
  }
  if (url.pathname.startsWith("/api/")) return; // other API calls: straight to the network

  if (request.mode === "navigate") {
    // Keep the browser's own redirect handling (e.g. to /login); offline, open the cached app shell.
    event.respondWith(
      fetch(request)
        .then((res) => {
          if (cacheable(res) && url.pathname === "/") {
            const copy = res.clone();
            caches.open(SHELL).then((c) => c.put("/", copy));
          }
          return res;
        })
        .catch(async () => (await caches.match("/")) || Response.error()),
    );
    return;
  }
  event.respondWith(networkFirst(request, SHELL));
});

// The page asks for this on logout so no training data stays on the device.
self.addEventListener("message", (event) => {
  if (event.data === "clear-data") event.waitUntil(caches.delete(DATA));
});
