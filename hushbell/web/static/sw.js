// Hushbell's service worker: makes the page installable as an app and shows a friendly page
// when Hushbell can't be reached. Pages and the API always come from the network, so nothing
// about your messages is ever cached on the phone.
const CACHE = "hushbell-shell-v1";
const SHELL = ["/offline.html", "/icons/icon-192.png", "/favicon.svg"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/offline.html")));
    return;
  }
  const url = new URL(request.url);
  if (url.origin === location.origin && SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(request).then(hit => hit || fetch(request)));
  }
});
