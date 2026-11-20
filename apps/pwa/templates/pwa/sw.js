/* TinyCRM Teren — service worker (zakres „/”).
 * Konfiguracja `self.TINYCRM_SW` jest doklejana przez widok pwa:service_worker.
 * Strategie: HTML aplikacji terenowej = network-first (z cache, gdy brak zasięgu, a w ostateczności strona offline),
 * statyki i biblioteki z CDN = cache-first. Obsługa web push: {title, body, url}. */
"use strict";

var CFG = self.TINYCRM_SW || {};
var VERSION = CFG.version || "dev";
var STATIC_CACHE = "tinycrm-static-" + VERSION;
var PAGE_CACHE = "tinycrm-pages-" + VERSION;
var KEEP = [STATIC_CACHE, PAGE_CACHE];

function putAll(cacheName, urls, init) {
  return caches.open(cacheName).then(function (cache) {
    return Promise.all((urls || []).map(function (url) {
      return fetch(new Request(url, init || {})).then(function (resp) {
        if (resp && (resp.ok || resp.type === "opaque") && !resp.redirected) return cache.put(url, resp);
        return null;
      }).catch(function () { return null; });
    }));
  });
}

self.addEventListener("install", function (event) {
  event.waitUntil(Promise.all([
    putAll(STATIC_CACHE, CFG.precache, { credentials: "same-origin", cache: "reload" }),
    // Strony aplikacji — tylko gdy użytkownik jest zalogowany (przekierowanie do logowania pomijamy).
    putAll(PAGE_CACHE, CFG.warmPages, { credentials: "same-origin", cache: "reload" })
  ]).then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (key) {
      return key.indexOf("tinycrm-") === 0 && KEEP.indexOf(key) === -1;
    }).map(function (key) { return caches.delete(key); }));
  }).then(function () { return self.clients.claim(); }));
});

function isAppPage(url) {
  return (CFG.pagePrefixes || []).some(function (prefix) { return url.pathname.indexOf(prefix) === 0; });
}

function isStatic(url) {
  if (url.origin === self.location.origin) return url.pathname.indexOf(CFG.staticUrl || "/static/") === 0;
  return (CFG.cdnHosts || []).indexOf(url.hostname) !== -1;
}

function isLoginPath(pathname) {
  return pathname === CFG.loginUrl || pathname === CFG.logoutUrl;
}

function clearPages() {
  return caches.delete(PAGE_CACHE);
}

// Strony „rozgrzewane” po każdym udanym, zalogowanym wejściu do aplikacji — tylko te, których brakuje w cache
// (np. po wyczyszczeniu przy logowaniu). Dzięki temu „Nagraj” działa offline także po ponownym zalogowaniu.
function warmMissingPages() {
  return caches.open(PAGE_CACHE).then(function (cache) {
    return Promise.all((CFG.warmPages || []).map(function (path) {
      return cache.match(path).then(function (hit) {
        if (hit) return null;
        return fetch(new Request(path, { credentials: "same-origin", redirect: "manual" })).then(function (resp) {
          // Przekierowanie (np. do logowania) albo błąd — nie zapisujemy.
          if (resp && resp.ok && resp.type === "basic" && !resp.redirected) return cache.put(path, resp);
          return null;
        }).catch(function () { return null; });
      });
    }));
  });
}

function handleNavigation(event, url) {
  var request = event.request;
  return fetch(request).then(function (resp) {
    var finalPath = "";
    try { finalPath = new URL(resp.url).pathname; } catch (e) { finalPath = ""; }
    if (isLoginPath(url.pathname) || isLoginPath(finalPath)) {
      // Ekran logowania (także po wygaśnięciu sesji — przeglądarka dochodzi tu nawigacją po przekierowaniu):
      // nie trzymamy w cache stron z danymi poprzedniego użytkownika.
      event.waitUntil(clearPages());
    } else if (resp.ok && !resp.redirected && resp.type !== "opaqueredirect" && isAppPage(url)) {
      // Zwykłe przekierowanie (np. logo → /teren/) NIE czyści cache. Cache'ujemy tylko strony bez parametrów:
      // `?klient=<id>` osadza wybranego klienta, a klucz cache to sama ścieżka.
      if (!url.search) {
        var copy = resp.clone();
        event.waitUntil(caches.open(PAGE_CACHE).then(function (cache) { return cache.put(url.pathname, copy); })
          .then(warmMissingPages));
      } else {
        event.waitUntil(warmMissingPages());
      }
    }
    return resp;
  }).catch(function () {
    return caches.open(PAGE_CACHE).then(function (cache) {
      return cache.match(url.pathname);
    }).then(function (cached) {
      if (cached) return cached;
      return caches.match(CFG.offlineUrl).then(function (offline) {
        return offline || new Response("<h1>Brak połączenia</h1>", {
          status: 503, headers: { "Content-Type": "text/html; charset=utf-8" }
        });
      });
    });
  });
}

function handleStatic(event) {
  var request = event.request;
  return caches.open(STATIC_CACHE).then(function (cache) {
    return cache.match(request).then(function (cached) {
      if (cached) return cached;
      return fetch(request).then(function (resp) {
        if (resp && (resp.ok || resp.type === "opaque")) cache.put(request, resp.clone());
        return resp;
      });
    });
  });
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") {
    // Wylogowanie (POST): czyścimy zapamiętane strony z danymi użytkownika, żądania nie przechwytujemy.
    try {
      if (new URL(request.url).pathname === CFG.logoutUrl) event.waitUntil(clearPages());
    } catch (e) { /* ignoruj */ }
    return;
  }
  // Upload notatek (POST) zawsze idzie do sieci — kolejka offline jest w IndexedDB po stronie strony.
  var url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (request.mode === "navigate" && url.origin === self.location.origin) {
    event.respondWith(handleNavigation(event, url));
    return;
  }
  if (isStatic(url)) {
    event.respondWith(handleStatic(event));
  }
});

/* --- Web push --- */
self.addEventListener("push", function (event) {
  var data = {};
  if (event.data) {
    try { data = event.data.json(); } catch (e) { data = { body: event.data.text() }; }
  }
  var title = data.title || "TinyCRM";
  var options = {
    body: data.body || "",
    icon: CFG.defaultIcon,
    badge: CFG.defaultIcon,
    lang: "pl",
    data: { url: data.url || CFG.defaultUrl || "/" }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || CFG.defaultUrl || "/";
  var absolute;
  try { absolute = new URL(target, self.location.origin); } catch (e) { absolute = new URL("/", self.location.origin); }
  if (absolute.origin !== self.location.origin) absolute = new URL(CFG.defaultUrl || "/", self.location.origin);
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
    function pathOf(client) {
      try { return new URL(client.url).pathname; } catch (e) { return ""; }
    }
    // 1) Okno, które już pokazuje cel — tylko je aktywujemy.
    for (var i = 0; i < list.length; i++) {
      if (list[i].url === absolute.href && "focus" in list[i]) return list[i].focus();
    }
    // 2) Okno, którego przejście nie zniszczy niewysłanej notatki (ekran nagrywania pomijamy —
    //    client.navigate nie pokazuje ostrzeżenia beforeunload, a nagranie jest tylko w pamięci strony).
    for (var j = 0; j < list.length; j++) {
      var client = list[j];
      if (pathOf(client) === CFG.recordPath) continue;
      if ("focus" in client && "navigate" in client) {
        return client.navigate(absolute.href).then(function (c) { return (c || client).focus(); })
          .catch(function () { return self.clients.openWindow(absolute.href); });
      }
    }
    // 3) W przeciwnym razie nowe okno.
    return self.clients.openWindow(absolute.href);
  }));
});

self.addEventListener("message", function (event) {
  if (event.data === "skipWaiting") self.skipWaiting();
  if (event.data === "clearPages") event.waitUntil(clearPages());
});
