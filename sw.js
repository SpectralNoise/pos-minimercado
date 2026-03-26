// Service Worker - POS Mini Mercado
// Estrategias por tipo de recurso:
//   index.html  → stale-while-revalidate (sirve caché al instante, actualiza en fondo)
//   /api/*      → network-only (datos manejados por localStorage en F1)
//   otros GET   → cache-first
const CACHE_NAME    = 'pos-v2';
const STATIC_ASSETS = ['/index.html', '/sw.js'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(c => c.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API: nunca interceptar — la red siempre es autoritativa para datos
  if (url.pathname.startsWith('/api/')) return;

  // Solo caché para GETs
  if (e.request.method !== 'GET') return;

  // index.html: stale-while-revalidate
  if (url.pathname === '/' || url.pathname === '/index.html') {
    e.respondWith(staleWhileRevalidate(e.request));
    return;
  }

  // Otros estáticos (sw.js, etc.): cache-first
  e.respondWith(cacheFirst(e.request));
});

async function staleWhileRevalidate(request) {
  const cache  = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  // Actualizar caché en segundo plano
  const networkFetch = fetch(request)
    .then(res => { if (res.ok) cache.put(request, res.clone()); return res; })
    .catch(() => null);
  // Responder con caché inmediatamente si existe, si no esperar red
  return cached ?? await networkFetch;
}

async function cacheFirst(request) {
  const cache  = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;
  const res = await fetch(request);
  if (res.ok) cache.put(request, res.clone());
  return res;
}
