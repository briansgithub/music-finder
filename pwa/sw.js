self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(clients.claim()); });
// Network-only for API; cache shell lightly
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.port === "8765") return; // never cache engine
  if (e.request.method !== "GET") return;
  e.respondWith(
    caches.open("musicrec-shell-v1").then(async (cache) => {
      const hit = await cache.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok && url.origin === location.origin) cache.put(e.request, res.clone());
      return res;
    })
  );
});
