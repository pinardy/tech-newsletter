// Offline strategy, in one line each:
//   shell  -> cache-first  (it never changes between deploys)
//   data   -> network-first with a short timeout, cache fallback
//
// The timeout is the important part. On a weak connection, plain network-first
// hangs and the reader stares at a skeleton; falling back at 2.5s shows
// yesterday's cached digest instead, and the late network response still
// refreshes the cache for next time.

// Bump on every shell change. The shell is cache-first precisely because it
// "never changes between deploys" — which means a reader who installed the
// last version keeps it forever unless this string moves.
const VERSION = "v4";
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;
const NET_TIMEOUT = 2500;
const PRECACHE_DAYS = 4;

// The `latin` font subsets are shell, not an optimisation: without them the
// offline page — the whole reason this file exists — renders in Georgia and
// reflows. `latin-ext` is deliberately absent; it is ~75KB that only an
// accented headline needs, and runtime caching picks it up the first time one
// appears. ~148KB of type precached once, against ~200KB from a third party on
// every cold visit.
const SHELL_ASSETS = [
  "./",
  "./index.html",
  "./health.html",
  "./manifest.webmanifest",
  "./icon.svg",
  "./fonts/newsreader-latin.woff2",
  "./fonts/newsreader-italic-latin.woff2",
  "./fonts/bricolage-latin.woff2",
  "./fonts/plexmono-400-latin.woff2",
  "./fonts/plexmono-500-latin.woff2",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    const keep = new Set([SHELL, DATA]);
    for (const k of await caches.keys()) if (!keep.has(k)) await caches.delete(k);
    await self.clients.claim();
    await precacheRecent();
  })());
});

// Pull the most recent editions the reader actually subscribes to, so days
// they never opened while online are still readable on the train.
async function precacheRecent() {
  try {
    const cache = await caches.open(DATA);
    const res = await fetch("data/index.json", { cache: "no-cache" });
    if (!res.ok) return;
    await cache.put("data/index.json", res.clone());
    const manifest = await res.json();

    const targets = [];
    for (const date of (manifest.dates || []).slice(0, PRECACHE_DAYS))
      for (const topic of Object.keys(manifest.shards?.[date] || {}))
        targets.push(`data/${date}/${topic}.json`);

    // Sequential, not Promise.all: a burst of 60 requests on activation
    // competes with the page's own fetches for the same connection.
    for (const url of targets) {
      try {
        const r = await fetch(url, { cache: "no-cache" });
        if (r.ok) await cache.put(url, r);
      } catch { /* offline mid-precache is fine; next activation retries */ }
    }
  } catch { /* no manifest yet — first ever run */ }
}

function isData(url) {
  return url.pathname.includes("/data/") && url.pathname.endsWith(".json");
}

// Immutable, content-addressed-enough to keep forever, and not worth precaching
// in full: the latin-ext subsets and the PNG icons. Cache them when something
// actually asks for one.
function isAsset(url) {
  return /\.(woff2|png|svg)$/.test(url.pathname);
}

async function networkFirst(request) {
  const cache = await caches.open(DATA);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), NET_TIMEOUT);

  try {
    const res = await fetch(request, { signal: controller.signal });
    clearTimeout(timer);
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch {
    clearTimeout(timer);
    const hit = await cache.match(request);
    if (hit) return hit;
    // A missing shard is a legitimate answer, not an error: the app renders
    // its empty state for a topic that published nothing that day.
    return new Response(JSON.stringify({ sections: [], offline: true }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  }
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;

  if (url.origin === location.origin && isData(url)) {
    e.respondWith(networkFirst(e.request));
    return;
  }

  if (url.origin === location.origin && isAsset(url)) {
    e.respondWith((async () => {
      const cache = await caches.open(SHELL);
      const hit = await cache.match(e.request);
      if (hit) return hit;
      try {
        const res = await fetch(e.request);
        if (res.ok) cache.put(e.request, res.clone());
        return res;
      } catch {
        return new Response("", { status: 504 });
      }
    })());
    return;
  }

  if (url.origin === location.origin) {
    e.respondWith(
      caches.match(e.request).then((hit) => hit || fetch(e.request))
    );
  }
  // Nothing cross-origin left to handle: the type is served from this origin now.
});
