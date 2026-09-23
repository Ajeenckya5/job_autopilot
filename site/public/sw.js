const CACHE = "job-autopilot-shell-2";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(["./", "./index.html", "./manifest.webmanifest"])).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request).then((response) => {
      const copy = response.clone();
      caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => {});
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
