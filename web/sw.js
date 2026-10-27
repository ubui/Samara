/* elmsamaras Service Worker
 * 由 /sw.js 路由动态提供（__VERSION__ 占位符在服务端替换）。
 * 策略：
 *   - 导航请求（页面）：网络优先，失败回退缓存，再回退 /offline
 *   - /static/* 静态资源：stale-while-revalidate（先缓存后更新）
 *   - /api/*、/dist/*、登录/登出：完全走网络，绝不缓存
 */
'use strict';

const VERSION = '__VERSION__';
const SHELL = 'elmsamaras-shell-' + VERSION;
const RUNTIME = 'elmsamaras-rt-' + VERSION;

const SHELL_URLS = [
  '/offline',
  '/static/css/style.css?v=1.1',
  '/static/js/alertifyjs/alertify.min.js',
  '/static/fonts/DIN.woff',
  '/static/fonts/icon.woff',
  '/static/img/pwa/icon-192.png',
  '/static/img/pwa/icon-512.png',
  '/static/img/favicon.ico',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    // 逐个缓存：任一资源失败不影响整体安装
    await Promise.allSettled(SHELL_URLS.map((u) => cache.add(u)));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL, RUNTIME]);
    const names = await caches.keys();
    await Promise.all(names.filter((n) => !keep.has(n)).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener('message', (event) => {
  const msg = event.data || {};
  if (msg.type === 'SKIP_WAITING') self.skipWaiting();
  if (msg.type === 'CLEAR_SHELL') {
    // 登出时清理页面缓存，避免离线状态能看到登录后的外壳
    event.waitUntil((async () => {
      for (const name of [SHELL, RUNTIME]) {
        const cache = await caches.open(name);
        const keys = await cache.keys();
        await Promise.all(keys.filter((r) => r.mode === 'navigate').map((r) => cache.delete(r)));
      }
    })());
  }
});

/** 是否始终直连（不读缓存也不写缓存） */
function isPassthrough(url) {
  return url.pathname.startsWith('/api/')
    || url.pathname.startsWith('/dist/')
    || url.pathname === '/get.sh'
    || url.pathname === '/install.sh'
    || url.pathname === '/logout'
    || url.pathname === '/sw.js'
    || url.pathname === '/manifest.webmanifest';
}

async function handleNavigation(request) {
  try {
    const resp = await fetch(request);
    if (resp.ok && resp.type === 'basic') {
      // 只缓存真正的页面响应；重定向（如未登录跳 /login）按最终地址存
      const finalUrl = new URL(resp.url);
      const key = (finalUrl.origin === self.location.origin
                   && finalUrl.pathname === '/login') ? '/login' : request;
      const cache = await caches.open(SHELL);
      cache.put(key, resp.clone());
    }
    return resp;
  } catch (err) {
    const shell = await caches.open(SHELL);
    return (await shell.match(request))
      || (await shell.match('/'))
      || (await shell.match('/offline'))
      || new Response('<h1>offline</h1>', {status: 503, headers: {'Content-Type': 'text/html'}});
  }
}

async function handleStatic(request) {
  const cache = await caches.open(RUNTIME);
  const cached = await cache.match(request);
  const network = fetch(request).then((resp) => {
    if (resp.ok) cache.put(request, resp.clone());
    return resp;
  }).catch(() => null);
  return cached || (await network)
    || new Response('', {status: 504, statusText: 'offline'});
}

async function handleOther(request) {
  try {
    return await fetch(request);
  } catch (err) {
    const cache = await caches.open(RUNTIME);
    return (await cache.match(request))
      || new Response(JSON.stringify({ok: false, error: 'offline'}),
                      {status: 503, headers: {'Content-Type': 'application/json'}});
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isPassthrough(url)) return;

  if (request.mode === 'navigate') {
    event.respondWith(handleNavigation(request));
  } else if (url.pathname.startsWith('/static/')) {
    event.respondWith(handleStatic(request));
  } else {
    event.respondWith(handleOther(request));
  }
});
