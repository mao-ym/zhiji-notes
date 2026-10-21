const CACHE_NAME = "writing-companion-mobile-v4";
const STATIC_ASSETS = [
  "/mobile/",
  "/mobile/index.html",
  "/mobile/app.js",
  "/mobile/image-utils.js",
  "/mobile/styles.css",
  "/mobile/manifest.webmanifest",
  "/mobile/service-worker.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((cacheNames) =>
        Promise.all(
          cacheNames
            .filter((cacheName) => cacheName !== CACHE_NAME)
            .map((cacheName) => caches.delete(cacheName)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const requestUrl = new URL(event.request.url);
  if (
    event.request.method !== "GET" ||
    requestUrl.pathname.startsWith("/mobile-api/")
  ) {
    event.respondWith(fetch(event.request));
    return;
  }
  if (!requestUrl.pathname.startsWith("/mobile/")) return;
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request)),
  );
});
