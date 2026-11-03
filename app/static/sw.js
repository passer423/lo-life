/* Service Worker：只缓存前端外壳。
 *
 * 刻意不缓存两样东西：
 *   /api/*    —— 数据必须实时。缓存住会让「刚记的单品不出现」变成玄学问题。
 *   /media/*  —— 照片动辄几百 KB，缓存会迅速吃满配额，而它本来也不常重复请求。
 *
 * 注意它只在 HTTPS 或 localhost 下才注册成功。局域网 http://192.168.x.x 上
 * 浏览器会直接拒绝注册 Service Worker —— 这是规范要求，不是 bug。详见 README。
 */

// 版本号必须跟着前端资源一起改。Service Worker 对静态资源是 cache-first，
// 改完 index.html / app.js / style.css 却不改这里，用户会一直看到旧的页面外壳
// ——这个坑很隐蔽：服务端明明是新代码，浏览器里却是旧的。
const CACHE = 'lo-life-v24';
const SHELL = [
  '/',
  '/app.js',
  '/chat.js',
  '/profile.js',
  '/style.css',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/media/')) return;

  // 页面导航：网络优先，断网时回退到缓存的外壳
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/')));
    return;
  }

  event.respondWith(
    caches.match(request).then((hit) => {
      if (hit) return hit;
      return fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => {});
        return response;
      });
    })
  );
});
