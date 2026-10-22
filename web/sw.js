/* 离线壳：让「麦麦成长时刻」在手机断网时也能打开。
 *
 * 场景：手机是要带出门的。离开家里的 WiFi 就访问不到电脑上跑的服务，
 * 但等餐那几分钟恰恰是最该背单词的时候。这里把页面骨架和词库缓存到手机，
 * 断网时照样能打开页面、照样能背——背完的记录先排进浏览器的待发队列，
 * 回到同一个 WiFi 后由 app.js 自动补传到电脑。
 *
 * 只缓存 GET；所有写请求（背词/记一餐/记通勤）都不拦，交给页面去排队。
 */
var CACHE = "t2m-v1";
var CORE = [
  "./",
  "./index.html",
  "./app.js",
  "./style.css",
  "./stats.js",
  "./words.js"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(CORE);
    }).then(function () {
      return self.skipWaiting();
    }).catch(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

/* 有缓存先给缓存，同时后台更新（离线也能看到上一次的看板与词库） */
function staleWhileRevalidate(request) {
  return caches.open(CACHE).then(function (cache) {
    return cache.match(request).then(function (hit) {
      var net = fetch(request).then(function (res) {
        if (res && res.status === 200) cache.put(request, res.clone());
        return res;
      }).catch(function () {
        return hit;
      });
      return hit || net;
    });
  });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;   // 写请求不拦：离线时由页面排队

  var url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;

  if (url.pathname.indexOf("/api/") === 0) {
    // 看板快照与词库允许「离线看旧的」，其余 API 交给网络，页面自己兜底
    if (url.pathname === "/api/stats" || url.pathname === "/api/words") {
      event.respondWith(staleWhileRevalidate(request));
    }
    return;
  }

  event.respondWith(
    caches.match(request).then(function (hit) {
      if (hit) return hit;
      return fetch(request).then(function (res) {
        if (res && res.status === 200 && res.type === "basic") {
          var copy = res.clone();
          caches.open(CACHE).then(function (cache) { cache.put(request, copy); });
        }
        return res;
      }).catch(function () {
        // 断网时打开页面：给缓存的首页，别让手机显示「无法访问此网站」
        if (request.mode === "navigate") return caches.match("./index.html");
        return new Response("", { status: 504, statusText: "offline" });
      });
    })
  );
});
