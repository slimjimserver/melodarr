// @ts-nocheck -- the app's DOM-only tsconfig does not include WebWorker lib types.
/* Root-scoped worker: never trust provider payload URLs. */
self.addEventListener("push", (event) => {
  let data: { title?: unknown; body?: unknown; url?: unknown } = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {}; }
  const title = typeof data.title === "string" ? data.title.slice(0, 180) : "Melodarr";
  const body = typeof data.body === "string" ? data.body.slice(0, 500) : "New music is available.";
  const raw = typeof data.url === "string" ? data.url : "/";
  let url = "/";
  try { const parsed = new URL(raw, self.location.origin); if (parsed.origin === self.location.origin) url = `${parsed.pathname}${parsed.search}${parsed.hash}`; } catch (_) { /* fallback */ }
  event.waitUntil(self.registration.showNotification(title, {
    body, data: { url }, icon: "/icons/melodarr-180.png", badge: "/icons/melodarr.svg",
  }));
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const raw = event.notification.data && typeof event.notification.data.url === "string"
    ? event.notification.data.url : "/";
  let url = "/";
  try { const parsed = new URL(raw, self.location.origin); if (parsed.origin === self.location.origin) url = `${parsed.pathname}${parsed.search}${parsed.hash}`; } catch (_) { /* fallback */ }
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    const existing = windows.find((client) => new URL(client.url).origin === self.location.origin);
    return existing ? existing.focus().then(() => existing.navigate(url)) : clients.openWindow(url);
  }));
});
