const CACHE = "job-autopilot-shell-3";
const SHELL_LIMIT = 2 * 1024 * 1024;
const SHELL = ["./", "./index.html", "./manifest.webmanifest"];

function shellRequest(url) {
  const path = new URL(url).pathname;
  if (path.includes("/feeds/")) return false;
  return /\/(?:index\.html|manifest\.webmanifest|sw\.js)$/.test(path) || /\.(?:js|css|svg|png|webmanifest)$/.test(path);
}

async function cacheBytes(cache) {
  const requests = await cache.keys();
  let total = 0;
  for (const request of requests) {
    const response = await cache.match(request);
    if (!response) continue;
    total += (await response.clone().arrayBuffer()).byteLength;
  }
  return total;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key.startsWith("job-autopilot-") && key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  if (new URL(event.request.url).origin !== self.location.origin) return;
  event.respondWith(
    fetch(event.request).then((response) => {
      if (shellRequest(event.request.url) && response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then(async (cache) => {
          const body = await copy.clone().arrayBuffer();
          if (await cacheBytes(cache) + body.byteLength > SHELL_LIMIT) return;
          await cache.put(event.request, new Response(body, { headers: copy.headers }));
        }).catch(() => {});
      }
      return response;
    }).catch(() => caches.match(event.request).then((hit) => hit || caches.match("./index.html")))
  );
});

self.addEventListener("periodicsync", (event) => {
  if (event.tag === "job-feed") {
    event.waitUntil(self.registration.showNotification("Job search", {
      body: "Open Job Autopilot to score new postings on this device.",
    }));
  }
});
