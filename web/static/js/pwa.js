/* elmsamaras PWA 前端引导：注册 Service Worker、离线提示条、安装按钮。
 * 由 index.html / login.html 以 defer 方式引入，无任何外部依赖。 */
(function () {
  'use strict';

  /* ---------- 样式（内联注入，两个页面共用） ---------- */
  var style = document.createElement('style');
  style.textContent = [
    '#nsu-offline-bar{position:fixed;bottom:0;left:0;right:0;z-index:99999;display:none;',
    'padding:7px 16px;text-align:center;font-size:12.5px;color:#ffe9c9;',
    'background:linear-gradient(90deg,#a8571a,#c47a1e);box-shadow:0 1px 6px rgba(0,0,0,.5)}',
    '#nsu-offline-bar.on{display:block}',
    '#nsu-install{position:fixed;right:16px;bottom:16px;z-index:99999;display:none;',
    'align-items:center;gap:8px;padding:9px 18px;font-size:13px;color:#fafafa;cursor:pointer;',
    'border:1px solid #799037;border-radius:20px;user-select:none;',
    'background:radial-gradient(ellipse at center,#7eaa0f 0%,#6d8006 100%);',
    'box-shadow:inset 0 0 20px 0 #606f11,0 2px 10px rgba(0,0,0,.55)}',
    '#nsu-install.on{display:flex}',
    '#nsu-install:active{transform:translateY(1px)}',
    '#nsu-install svg{width:15px;height:15px;fill:currentColor}'
  ].join('');
  document.head.appendChild(style);

  /* ---------- 离线提示条 ---------- */
  var bar = document.createElement('div');
  bar.id = 'nsu-offline-bar';
  bar.textContent = '⚠ 网络已断开，当前显示的界面可能不是最新状态';
  document.body.appendChild(bar);

  function syncBar() {
    if (navigator.onLine === false) bar.classList.add('on');
    else bar.classList.remove('on');
  }
  window.addEventListener('online', syncBar);
  window.addEventListener('offline', syncBar);
  syncBar();

  /* ---------- Service Worker 注册 ---------- */
  // isSecureContext 在 HTTPS 与 localhost/127.0.0.1 下为 true；
  // 局域网 HTTP 访问时为 false，此时浏览器不允许注册，静默跳过即可。
  if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(function () { /* 老浏览器或不支持时忽略 */ });
    });
  }

  /* ---------- 登出时清掉页面缓存 ---------- */
  document.addEventListener('click', function (ev) {
    var a = ev.target && ev.target.closest && ev.target.closest('a[href="/logout"]');
    if (!a) return;
    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({type: 'CLEAR_SHELL'});
    }
  });

  /* ---------- 安装按钮（Chromium 系浏览器） ---------- */
  var deferredPrompt = null;
  var btn = null;

  function showButton() {
    if (btn) { btn.classList.add('on'); return; }
    btn = document.createElement('button');
    btn.id = 'nsu-install';
    btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 20h14v-2H5v2zM12 2 6.5 10h3.5v6h4v-6h3.5L12 2z"/></svg>安装到桌面';
    btn.addEventListener('click', function () {
      if (!deferredPrompt) return;
      deferredPrompt.prompt();
      deferredPrompt.userChoice.then(function () { deferredPrompt = null; btn.classList.remove('on'); });
    });
    document.body.appendChild(btn);
    btn.classList.add('on');
  }

  window.addEventListener('beforeinstallprompt', function (ev) {
    // 已安装（standalone 模式运行）时不显示按钮
    if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return;
    ev.preventDefault();
    deferredPrompt = ev;
    showButton();
  });

  window.addEventListener('appinstalled', function () {
    if (btn) btn.classList.remove('on');
    deferredPrompt = null;
  });
})();
