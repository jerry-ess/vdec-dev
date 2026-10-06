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
  "/static/chart_options.js",
  "/static/report.js",
  "/static/schedule.js",
  "/static/share.js",
  "/static/settings.js",
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
  self.skipWaiting();
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS.map((u) => new Request(u, { cache: "reload" })))));
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
    // v20: the page is stored only if it is this version's dashboard page (not a host's start-up page)
    if (res.ok && (fallbackUrl !== "/" || res.headers.get("X-Veridex-App") === VERSION)) (await caches.open(cacheName)).put(fallbackUrl || request, res.clone());
    return res;
  } catch (e) {
    const hit = await caches.match(fallbackUrl || request);
    if (hit) return hit;
    throw e;
  }
}

// v20: the page and UI texts come from the device first, so the app opens at once even while the server is
// still starting up after a quiet period; the copy is refreshed in the background. Only the dashboard's own
// page is stored (it carries the X-Veridex-App header) - never a host's "starting up" page.
async function pageFromCache(event) {
  const cache = await caches.open(SHELL);
  const network = fetch(event.request).then(async (res) => {
    if (res.ok && res.headers.get("X-Veridex-App") === VERSION) await cache.put("/", res.clone()); // this version only
    return res;
  });
  const hit = await cache.match("/");
  if (hit) {
    event.waitUntil(network.catch(() => {}));
    return hit;
  }
  return network;
}

async function staleWhileRevalidate(event, cacheName) {
  const cache = await caches.open(cacheName);
  const network = fetch(event.request).then(async (res) => {
    if (res.ok && (res.headers.get("content-type") || "").includes("json")) await cache.put(event.request, res.clone());
    return res;
  });
  const hit = await cache.match(event.request);
  if (hit) {
    event.waitUntil(network.catch(() => {}));
    return hit;
  }
  return network;
}

async function cacheFirst(request) {
  // v20: an asset asked for with another version (?v=) than this worker's comes from the network, so a new page
  // never gets an old app.js / app.css from this cache
  const v = new URL(request.url).searchParams.get("v");
  if (v && v !== VERSION) return fetch(request);
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
    event.respondWith(staleWhileRevalidate(event, I18N));
    return;
  }
  if (url.pathname.startsWith("/api/")) return;                   // business data: network only, never cached
  if (req.mode === "navigate" && url.pathname === "/") {           // the page itself: device copy first (v20)
    event.respondWith(pageFromCache(event));
    return;
  }
  if (req.mode === "navigate") {                                  // any other page: fresh when online
    event.respondWith(networkFirst(req, SHELL, "/"));
    return;
  }
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(cacheFirst(req));
  }
});
