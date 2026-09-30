/* Veridex BI service worker.
 *
 * Caches ONLY the app shell (page, CSS, JS, ECharts, icons, manifest) and UI translations, so the installed
 * app opens instantly and still starts offline (showing "you're offline" instead of numbers).
 * Business data (/api/..., except /api/i18n/) is NEVER stored on the device: every figure, customer name
 * and download always comes from the server, under the user's session and tenant checks.
 *
 * __VERSION__ is replaced by the server with the app version + a hash of the web files, so every
 * deployment gets a new cache and the page switches to it automatically (v15; "Reload" if the user is busy).
 */
const VERSION = "__VERSION__";
const SHELL = `vdash-shell-${VERSION}`;
const I18N = `vdash-i18n-${VERSION}`;
const SHELL_URLS = [
  "/",
  "/static/app.css",
  "/static/app.js",
  "/static/vendor/echarts.min.js",
  "/manifest.webmanifest",
  "/static/icons/favicon-32.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS.map((u) => new Request(u, { cache: "reload" })))));
  // first install activates immediately; later versions wait until the page asks for them (SKIP_WAITING) — since
  // v15 the page does that automatically as soon as the user is not in the middle of something
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("vdash-") && key !== SHELL && key !== I18N) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

async function networkFirst(request, cacheName, fallbackUrl) {
  try {
    const res = await fetch(request);
    if (res.ok) (await caches.open(cacheName)).put(fallbackUrl || request, res.clone());
    return res;
  } catch (e) {
    const hit = await caches.match(fallbackUrl || request);
    if (hit) return hit;
    throw e;
  }
}

async function cacheFirst(request) {
  const hit = await caches.match(request, { ignoreSearch: true });
  if (hit) return hit;
  const res = await fetch(request);
  if (res.ok) (await caches.open(SHELL)).put(request, res.clone());
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                              // logins, logouts: straight to the network
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/i18n/")) {                    // UI text only - safe to keep offline
    event.respondWith(networkFirst(req, I18N));
    return;
  }
  if (url.pathname.startsWith("/api/")) return;                   // business data: network only, never cached
  if (req.mode === "navigate") {                                  // the page itself: fresh when online
    event.respondWith(networkFirst(req, SHELL, "/"));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(cacheFirst(req));
  }
});
